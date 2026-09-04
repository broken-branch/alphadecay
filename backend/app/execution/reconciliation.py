from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from math import gcd
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from backend.app.contracts.v1 import AccountRole, PositionIntent
from backend.app.contracts.v1.models import canonical_decimal
from backend.app.execution.order_status import (
    PENDING_BROKER_ORDER_STATES,
    broker_state_matches_fill,
)
from backend.app.order_limits import MAX_STRUCTURAL_OPTION_QUANTITY

MAX_SWEEP_AGE = timedelta(seconds=15)
MAX_SWEEP_DURATION = timedelta(seconds=15)
ACTIVITY_VISIBILITY_HORIZON = timedelta(hours=24)
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ACTIVE_ORDER_STATES = PENDING_BROKER_ORDER_STATES
ASSIGNMENT_ACTIVITY_TYPES = {"OPASN", "OPEXC", "OPEXP", "OPXRC"}
ACCOUNT_ADJUSTMENT_ACTIVITY_TYPES = {
    "RESET",
    "DEPOSIT",
    "WITHDRAWAL",
    "TRANSFER",
    "JOURNAL",
    "UNKNOWN_CASH",
    "DIVIDEND",
    "FEE",
    "INTEREST",
    "CORPORATE_ACTION",
}


class InventoryKind(StrEnum):
    OPTION = "OPTION"
    EQUITY = "EQUITY"


class ActivityType(StrEnum):
    OPASN = "OPASN"
    OPEXC = "OPEXC"
    OPEXP = "OPEXP"
    OPXRC = "OPXRC"
    OPTRD = "OPTRD"
    FILL = "FILL"
    RESET = "RESET"
    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    TRANSFER = "TRANSFER"
    JOURNAL = "JOURNAL"
    UNKNOWN_CASH = "UNKNOWN_CASH"
    INITIAL_FUNDING = "INITIAL_FUNDING"
    DIVIDEND = "DIVIDEND"
    FEE = "FEE"
    INTEREST = "INTEREST"
    CORPORATE_ACTION = "CORPORATE_ACTION"


class ReconciliationPurpose(StrEnum):
    SUBMIT = "SUBMIT"
    REPLACE = "REPLACE"
    CANCEL = "CANCEL"
    BASELINE_INITIALIZATION = "BASELINE_INITIALIZATION"


class ReconciliationBlockCode(StrEnum):
    ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"
    ACCOUNT_NOT_EXECUTABLE = "ACCOUNT_NOT_EXECUTABLE"
    STALE_OBSERVATION = "STALE_OBSERVATION"
    FUTURE_OBSERVATION = "FUTURE_OBSERVATION"
    INCOMPLETE_SWEEP = "INCOMPLETE_SWEEP"
    ACTIVITY_WATERMARK_UNKNOWN = "ACTIVITY_WATERMARK_UNKNOWN"
    ACTIVITY_WINDOW_INCOMPLETE = "ACTIVITY_WINDOW_INCOMPLETE"
    KNOWN_ACTIVITY_MISSING = "KNOWN_ACTIVITY_MISSING"
    UNSTABLE_SWEEP = "UNSTABLE_SWEEP"
    UNEXPECTED_INVENTORY = "UNEXPECTED_INVENTORY"
    UNEXPECTED_OPEN_ORDER = "UNEXPECTED_OPEN_ORDER"
    UNEXPECTED_ACTIVITY = "UNEXPECTED_ACTIVITY"
    ASSIGNMENT_SUSPECTED = "ASSIGNMENT_SUSPECTED"
    ACCOUNT_ADJUSTMENT = "ACCOUNT_ADJUSTMENT"


@dataclass(frozen=True, init=False)
class AccountReconciliationState:
    state_id: UUID
    account_role: AccountRole
    account_fingerprint: str
    baseline_captured_at: datetime
    accepted_at: datetime
    expected_cash: Decimal
    expected_positions: tuple[InventoryItem, ...]
    expected_open_orders: tuple[OpenOrderItem, ...]
    known_activities: tuple[ActivityItem, ...]
    activity_complete_through: datetime
    resolved_activity_hashes: tuple[str, ...]
    state_hash: str

    @classmethod
    def _from_repository_state(
        cls,
        *,
        account_role: AccountRole,
        account_fingerprint: str,
        baseline_captured_at: datetime,
        accepted_at: datetime,
        expected_cash: Decimal,
        expected_positions: tuple[InventoryItem, ...],
        expected_open_orders: tuple[OpenOrderItem, ...],
        known_activities: tuple[ActivityItem, ...],
        activity_complete_through: datetime,
        resolved_activity_hashes: tuple[str, ...] = (),
    ) -> AccountReconciliationState:
        known_hashes = {activity.activity_id_hash for activity in known_activities}
        if (
            resolved_activity_hashes != tuple(sorted(resolved_activity_hashes))
            or len(set(resolved_activity_hashes)) != len(resolved_activity_hashes)
            or any(item not in known_hashes for item in resolved_activity_hashes)
        ):
            raise ValueError("RESOLVED_ACTIVITY_HASHES_INVALID")
        material = {
            "domain": "alphadecay.account-reconciliation-state.v1",
            "account_role": account_role,
            "account_fingerprint": account_fingerprint,
            "baseline_captured_at": baseline_captured_at,
            "accepted_at": accepted_at,
            "expected_cash": expected_cash,
            "expected_positions": expected_positions,
            "expected_open_orders": expected_open_orders,
            "known_activities": known_activities,
            "activity_complete_through": activity_complete_through,
        }
        if resolved_activity_hashes:
            material["resolved_activity_hashes"] = resolved_activity_hashes
        state_hash = _canonical_hash(_canonical_value(material))
        state = object.__new__(cls)
        values = {
            **{name: value for name, value in material.items() if name != "domain"},
            "resolved_activity_hashes": resolved_activity_hashes,
            "state_id": uuid5(NAMESPACE_URL, f"alphadecay:account-state:{state_hash}"),
            "state_hash": state_hash,
        }
        for name, value in values.items():
            object.__setattr__(state, name, value)
        return state


@dataclass(frozen=True)
class AccountObservation:
    role: AccountRole
    account_fingerprint: str
    paper: bool
    status: str
    account_blocked: bool
    trading_blocked: bool
    options_trading_blocked: bool
    equity: Decimal
    buying_power: Decimal
    cash: Decimal
    observed_at: datetime
    time_quality: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, AccountRole) or self.role not in {
            AccountRole.SUBMISSION,
            AccountRole.DEVELOPMENT,
        }:
            raise ValueError("ACCOUNT_ROLE_INVALID")
        _require_hash(self.account_fingerprint, "ACCOUNT_FINGERPRINT_INVALID")
        _require_utc(self.observed_at, "ACCOUNT_OBSERVED_AT_INVALID")
        if (
            not isinstance(self.paper, bool)
            or not isinstance(self.account_blocked, bool)
            or not isinstance(self.trading_blocked, bool)
            or not isinstance(self.options_trading_blocked, bool)
            or not isinstance(self.equity, Decimal)
            or not self.equity.is_finite()
            or not isinstance(self.buying_power, Decimal)
            or not self.buying_power.is_finite()
            or not isinstance(self.cash, Decimal)
            or not self.cash.is_finite()
            or not isinstance(self.status, str)
            or not self.status
            or self.time_quality != "RETRIEVAL_TIME_ONLY"
        ):
            raise ValueError("ACCOUNT_TIME_QUALITY_INVALID")


@dataclass(frozen=True)
class InventoryItem:
    kind: InventoryKind
    symbol: str
    signed_quantity: Decimal
    multiplier: int

    def __post_init__(self) -> None:
        _require_token(self.symbol, 64, "INVENTORY_ITEM_INVALID")
        if (
            not isinstance(self.kind, InventoryKind)
            or not isinstance(self.signed_quantity, Decimal)
            or not self.signed_quantity.is_finite()
            or self.signed_quantity == 0
            or type(self.multiplier) is not int
            or self.multiplier <= 0
            or (self.kind == InventoryKind.OPTION and self.multiplier != 100)
            or (
                self.kind == InventoryKind.OPTION
                and self.signed_quantity != self.signed_quantity.to_integral_value()
            )
            or (self.kind == InventoryKind.EQUITY and self.multiplier != 1)
        ):
            raise ValueError("INVENTORY_ITEM_INVALID")


@dataclass(frozen=True)
class OpenOrderLeg:
    symbol: str
    intent: PositionIntent
    ratio: int

    def __post_init__(self) -> None:
        _require_token(self.symbol, 64, "OPEN_ORDER_LEG_INVALID")
        if (
            not isinstance(self.intent, PositionIntent)
            or type(self.ratio) is not int
            or not 1 <= self.ratio <= 6
        ):
            raise ValueError("OPEN_ORDER_LEG_INVALID")


@dataclass(frozen=True)
class OpenOrderItem:
    provider_order_id: str
    client_order_id: str
    state: str
    quantity: int
    filled_quantity: int
    replaces_client_order_id: str | None
    replaced_by_client_order_id: str | None
    order_class: str
    legs: tuple[OpenOrderLeg, ...]

    def __post_init__(self) -> None:
        _require_token(self.provider_order_id, 128, "OPEN_ORDER_ITEM_INVALID")
        _require_token(self.client_order_id, 64, "OPEN_ORDER_ITEM_INVALID")
        if self.replaces_client_order_id is not None:
            _require_token(self.replaces_client_order_id, 64, "OPEN_ORDER_ITEM_INVALID")
        if self.replaced_by_client_order_id is not None:
            _require_token(self.replaced_by_client_order_id, 64, "OPEN_ORDER_ITEM_INVALID")
        if (
            not isinstance(self.state, str)
            or self.state not in ACTIVE_ORDER_STATES
            or type(self.quantity) is not int
            or not 1 <= self.quantity <= MAX_STRUCTURAL_OPTION_QUANTITY
            or type(self.filled_quantity) is not int
            or not 0 <= self.filled_quantity <= self.quantity
            or not broker_state_matches_fill(self.state, self.filled_quantity, self.quantity)
            or self.order_class != "MLEG"
            or not isinstance(self.legs, tuple)
            or len(self.legs) not in {2, 4}
            or any(not isinstance(leg, OpenOrderLeg) for leg in self.legs)
            or len({leg.symbol for leg in self.legs}) != len(self.legs)
            or gcd(*(leg.ratio for leg in self.legs)) != 1
            or self.replaces_client_order_id == self.client_order_id
            or self.replaced_by_client_order_id == self.client_order_id
        ):
            raise ValueError("OPEN_ORDER_ITEM_INVALID")


@dataclass(frozen=True)
class ActivityItem:
    activity_id_hash: str
    activity_type: ActivityType
    occurred_at: datetime
    symbol: str | None
    signed_quantity: Decimal | None
    provider_order_id: str | None = None
    client_order_id: str | None = None
    time_quality: str = "EXACT_TRANSACTION_TIME"
    provider_activity_type: str | None = None

    def __post_init__(self) -> None:
        _require_hash(self.activity_id_hash, "ACTIVITY_HASH_INVALID")
        _require_utc(self.occurred_at, "ACTIVITY_OCCURRED_AT_INVALID")
        if self.symbol is not None:
            _require_token(self.symbol, 64, "ACTIVITY_FIELDS_INVALID")
        if self.provider_order_id is not None:
            _require_token(self.provider_order_id, 128, "ACTIVITY_FIELDS_INVALID")
        if self.client_order_id is not None:
            _require_token(self.client_order_id, 64, "ACTIVITY_FIELDS_INVALID")
        if self.provider_activity_type is not None:
            _require_token(self.provider_activity_type, 32, "ACTIVITY_FIELDS_INVALID")
        if not isinstance(self.activity_type, ActivityType) or (
            self.signed_quantity is not None
            and (
                not isinstance(self.signed_quantity, Decimal)
                or not self.signed_quantity.is_finite()
            )
        ):
            raise ValueError("ACTIVITY_TYPE_INVALID")
        if self.time_quality not in {"EXACT_TRANSACTION_TIME", "DATE_ONLY"}:
            raise ValueError("ACTIVITY_TIME_QUALITY_INVALID")
        if self.activity_type in {
            ActivityType.OPASN,
            ActivityType.OPEXC,
            ActivityType.OPEXP,
            ActivityType.OPXRC,
            ActivityType.OPTRD,
            ActivityType.FILL,
        } and (not self.symbol or self.signed_quantity is None or self.signed_quantity == 0):
            raise ValueError("ACTIVITY_FIELDS_INVALID")


def activity_predates_window(item: ActivityItem, window_start: datetime) -> bool:
    """True when a known activity happened before the sweep window and so cannot be observed.

    The baseline funding journal predates the baseline capture that starts every activity
    window; it is retained as known state, not re-observed. Date-only activities compare by
    calendar day, matching the provider adapter's window rule.
    """
    if item.time_quality == "DATE_ONLY":
        return item.occurred_at.date() < window_start.astimezone(UTC).date()
    return item.occurred_at < window_start


@dataclass(frozen=True)
class ActivityPaginationEvidence:
    requested_start: datetime
    requested_end: datetime
    retrieved_through: datetime
    established_at: datetime
    page_count: int
    terminal_page_seen: bool
    visibility_complete_through: datetime
    visibility_horizon: timedelta

    def __post_init__(self) -> None:
        for value in (
            self.requested_start,
            self.requested_end,
            self.retrieved_through,
            self.established_at,
            self.visibility_complete_through,
        ):
            _require_utc(value, "ACTIVITY_PAGINATION_TIME_INVALID")
        if (
            not self.requested_start
            <= self.retrieved_through
            <= self.requested_end
            <= self.established_at
            # The watermark may precede the requested start: a window shorter than the
            # visibility horizon has no activity that is guaranteed visible yet.
            or self.visibility_complete_through > self.requested_end
            or type(self.page_count) is not int
            or not 1 <= self.page_count <= 1000
            or not isinstance(self.terminal_page_seen, bool)
            or self.visibility_horizon != ACTIVITY_VISIBILITY_HORIZON
            or self.established_at - self.visibility_complete_through < self.visibility_horizon
        ):
            raise ValueError("ACTIVITY_PAGINATION_INVALID")

    @property
    def complete(self) -> bool:
        return self.terminal_page_seen and self.retrieved_through == self.requested_end


@dataclass(frozen=True)
class SweepObservation:
    retrieval_started_at: datetime
    retrieval_completed_at: datetime
    activity_pagination: ActivityPaginationEvidence
    first_account: AccountObservation
    final_account: AccountObservation
    first_positions: tuple[InventoryItem, ...]
    final_positions: tuple[InventoryItem, ...]
    first_open_orders: tuple[OpenOrderItem, ...]
    final_open_orders: tuple[OpenOrderItem, ...]
    activities: tuple[ActivityItem, ...]
    positions_complete: bool
    orders_complete: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.first_positions, tuple)
            or not isinstance(self.final_positions, tuple)
            or not isinstance(self.first_open_orders, tuple)
            or not isinstance(self.final_open_orders, tuple)
            or not isinstance(self.activities, tuple)
            or not isinstance(self.activity_pagination, ActivityPaginationEvidence)
            or any(
                not isinstance(item, InventoryItem)
                for item in self.first_positions + self.final_positions
            )
            or any(
                not isinstance(item, OpenOrderItem)
                for item in self.first_open_orders + self.final_open_orders
            )
            or any(not isinstance(item, ActivityItem) for item in self.activities)
            or any(
                not isinstance(value, bool)
                for value in (
                    self.positions_complete,
                    self.orders_complete,
                )
            )
        ):
            raise ValueError("SWEEP_TYPES_INVALID")
        _require_utc(self.retrieval_started_at, "SWEEP_TIME_INVALID")
        _require_utc(self.retrieval_completed_at, "SWEEP_TIME_INVALID")
        if self.retrieval_completed_at < self.retrieval_started_at:
            raise ValueError("SWEEP_TIME_INVALID")
        if not (
            self.retrieval_started_at
            <= self.first_account.observed_at
            <= self.final_account.observed_at
            <= self.retrieval_completed_at
            and self.retrieval_started_at
            <= self.activity_pagination.established_at
            <= self.retrieval_completed_at
            and self.first_account.observed_at
            <= self.activity_pagination.established_at
            <= self.final_account.observed_at
            and self.activity_pagination.requested_end == self.first_account.observed_at
        ):
            raise ValueError("SWEEP_CHRONOLOGY_INVALID")
        _require_canonical(
            self.first_positions,
            key=lambda item: (item.kind.value, item.symbol),
            code="INVENTORY_NOT_CANONICAL",
        )
        _require_canonical(
            self.final_positions,
            key=lambda item: (item.kind.value, item.symbol),
            code="INVENTORY_NOT_CANONICAL",
        )
        _require_canonical(
            self.first_open_orders,
            key=lambda item: item.client_order_id,
            code="OPEN_ORDERS_NOT_CANONICAL",
        )
        _require_canonical(
            self.final_open_orders,
            key=lambda item: item.client_order_id,
            code="OPEN_ORDERS_NOT_CANONICAL",
        )
        _require_unique_provider_orders(self.first_open_orders)
        _require_unique_provider_orders(self.final_open_orders)
        _require_canonical(
            self.activities,
            key=lambda item: item.activity_id_hash,
            code="ACTIVITIES_NOT_CANONICAL",
        )


@dataclass(frozen=True, init=False)
class ReconciliationExpectation:
    purpose: ReconciliationPurpose
    account_role: AccountRole
    account_fingerprint: str
    expected_cash: Decimal
    baseline_captured_at: datetime
    expected_positions: tuple[InventoryItem, ...]
    expected_open_orders: tuple[OpenOrderItem, ...]
    known_activities: tuple[ActivityItem, ...]
    resolved_activity_hashes: tuple[str, ...]
    required_activity_window_start: datetime
    required_activity_complete_through: datetime
    intent_id: UUID
    intent_digest: str
    attempt_ordinal: int
    request_hash: str
    expectation_hash: str

    @classmethod
    def _from_repository_state(
        cls,
        *,
        purpose: ReconciliationPurpose,
        account_role: AccountRole,
        account_fingerprint: str,
        expected_cash: Decimal,
        baseline_captured_at: datetime,
        expected_positions: tuple[InventoryItem, ...],
        expected_open_orders: tuple[OpenOrderItem, ...],
        known_activities: tuple[ActivityItem, ...],
        resolved_activity_hashes: tuple[str, ...],
        required_activity_window_start: datetime,
        required_activity_complete_through: datetime,
        intent_id: UUID,
        intent_digest: str,
        attempt_ordinal: int,
        request_hash: str,
    ) -> ReconciliationExpectation:
        """Build integrity-bound material already derived from locked repository rows."""
        if (
            not isinstance(purpose, ReconciliationPurpose)
            or not isinstance(account_role, AccountRole)
            or account_role not in {AccountRole.SUBMISSION, AccountRole.DEVELOPMENT}
        ):
            raise ValueError("RECONCILIATION_EXPECTATION_INVALID")
        _require_hash(account_fingerprint, "ACCOUNT_FINGERPRINT_INVALID")
        _require_hash(intent_digest, "INTENT_DIGEST_INVALID")
        _require_hash(request_hash, "REQUEST_HASH_INVALID")
        _require_utc(baseline_captured_at, "BASELINE_TIME_INVALID")
        _require_utc(required_activity_window_start, "ACTIVITY_WINDOW_INVALID")
        _require_utc(required_activity_complete_through, "ACTIVITY_WINDOW_INVALID")
        if (
            required_activity_window_start != baseline_captured_at
            or required_activity_complete_through < required_activity_window_start
            or not isinstance(expected_cash, Decimal)
            or not expected_cash.is_finite()
            or not isinstance(intent_id, UUID)
            or type(attempt_ordinal) is not int
            or not 0 <= attempt_ordinal <= 3
        ):
            raise ValueError("RECONCILIATION_EXPECTATION_INVALID")
        if (
            not isinstance(expected_positions, tuple)
            or any(not isinstance(item, InventoryItem) for item in expected_positions)
            or not isinstance(expected_open_orders, tuple)
            or any(not isinstance(item, OpenOrderItem) for item in expected_open_orders)
            or not isinstance(known_activities, tuple)
            or any(not isinstance(item, ActivityItem) for item in known_activities)
        ):
            raise ValueError("RECONCILIATION_EXPECTATION_INVALID")
        _require_canonical(
            expected_positions,
            key=lambda item: (item.kind.value, item.symbol),
            code="INVENTORY_NOT_CANONICAL",
        )
        _require_canonical(
            expected_open_orders,
            key=lambda item: item.client_order_id,
            code="OPEN_ORDERS_NOT_CANONICAL",
        )
        _require_unique_provider_orders(expected_open_orders)
        _require_canonical(
            known_activities,
            key=lambda item: item.activity_id_hash,
            code="ACTIVITIES_NOT_CANONICAL",
        )
        if resolved_activity_hashes != tuple(sorted(resolved_activity_hashes)) or len(
            set(resolved_activity_hashes)
        ) != len(resolved_activity_hashes):
            raise ValueError("ACTIVITY_HASHES_NOT_CANONICAL")
        known_hashes = {item.activity_id_hash for item in known_activities}
        for activity_hash in resolved_activity_hashes:
            _require_hash(activity_hash, "ACTIVITY_HASH_INVALID")
            if activity_hash not in known_hashes:
                raise ValueError("RESOLVED_ACTIVITY_NOT_KNOWN")
        expectation = object.__new__(cls)
        values = {
            "purpose": purpose,
            "account_role": account_role,
            "account_fingerprint": account_fingerprint,
            "expected_cash": expected_cash,
            "baseline_captured_at": baseline_captured_at,
            "expected_positions": expected_positions,
            "expected_open_orders": expected_open_orders,
            "known_activities": known_activities,
            "resolved_activity_hashes": resolved_activity_hashes,
            "required_activity_window_start": required_activity_window_start,
            "required_activity_complete_through": required_activity_complete_through,
            "intent_id": intent_id,
            "intent_digest": intent_digest,
            "attempt_ordinal": attempt_ordinal,
            "request_hash": request_hash,
        }
        expectation_hash = _canonical_hash(
            {
                "domain": "alphadecay.reconciliation-expectation.v1",
                **{name: _canonical_value(value) for name, value in values.items()},
            }
        )
        for name, value in values.items():
            object.__setattr__(expectation, name, value)
        object.__setattr__(expectation, "expectation_hash", expectation_hash)
        return expectation

    def _verify_integrity(self) -> None:
        material = {
            name: _canonical_value(getattr(self, name))
            for name in self.__dataclass_fields__
            if name != "expectation_hash"
        }
        expected = _canonical_hash(
            {"domain": "alphadecay.reconciliation-expectation.v1", **material}
        )
        if self.expectation_hash != expected:
            raise ValueError("RECONCILIATION_EXPECTATION_INTEGRITY_INVALID")


# Paper option fills carry small regulatory fees that reduce cash without a matching
# activity at fill time. Cash may trail the expectation by at most this much; it may
# never exceed it.
MAX_UNREPORTED_FEE_DRAG = Decimal("1.00")


def _cash_within_fee_tolerance(observed: Decimal, expected: Decimal) -> bool:
    return Decimal(0) <= expected - observed <= MAX_UNREPORTED_FEE_DRAG


@dataclass(frozen=True, init=False)
class WholeAccountReconciliation:
    reconciliation_id: UUID
    reconciliation_hash: str
    expectation: ReconciliationExpectation
    sweep: SweepObservation
    accepted_at: datetime
    positions_manifest_hash: str
    orders_manifest_hash: str
    activities_manifest_hash: str
    safe: bool
    block_codes: tuple[ReconciliationBlockCode, ...]

    @classmethod
    def evaluate(
        cls,
        sweep: SweepObservation,
        expectation: ReconciliationExpectation,
        *,
        accepted_at: datetime,
    ) -> WholeAccountReconciliation:
        """Evaluate domain evidence; this result alone never authorizes a broker mutation."""
        _require_utc(accepted_at, "RECONCILIATION_ACCEPTED_AT_INVALID")
        expectation._verify_integrity()
        blocks: set[ReconciliationBlockCode] = set()

        if not sweep.positions_complete or not sweep.orders_complete:
            blocks.add(ReconciliationBlockCode.INCOMPLETE_SWEEP)
        pagination = sweep.activity_pagination
        if not pagination.complete or (
            pagination.visibility_complete_through < expectation.required_activity_complete_through
            or pagination.established_at > accepted_at
        ):
            blocks.add(ReconciliationBlockCode.ACTIVITY_WATERMARK_UNKNOWN)
        if pagination.requested_start != expectation.required_activity_window_start:
            blocks.add(ReconciliationBlockCode.ACTIVITY_WINDOW_INCOMPLETE)
        if sweep.retrieval_completed_at - sweep.retrieval_started_at > MAX_SWEEP_DURATION:
            blocks.add(ReconciliationBlockCode.STALE_OBSERVATION)

        if (
            _account_material(sweep.first_account) != _account_material(sweep.final_account)
            or sweep.first_positions != sweep.final_positions
            or sweep.first_open_orders != sweep.final_open_orders
        ):
            blocks.add(ReconciliationBlockCode.UNSTABLE_SWEEP)

        for observation_time in (
            sweep.first_account.observed_at,
            sweep.final_account.observed_at,
            sweep.retrieval_started_at,
            sweep.retrieval_completed_at,
        ):
            if observation_time > accepted_at:
                blocks.add(ReconciliationBlockCode.FUTURE_OBSERVATION)
            elif accepted_at - observation_time > MAX_SWEEP_AGE:
                blocks.add(ReconciliationBlockCode.STALE_OBSERVATION)

        for observed in (sweep.first_account, sweep.final_account):
            if (
                observed.role != expectation.account_role
                or observed.account_fingerprint != expectation.account_fingerprint
            ):
                blocks.add(ReconciliationBlockCode.ACCOUNT_MISMATCH)
            if (
                not observed.paper
                or observed.status != "ACTIVE"
                or observed.account_blocked
                or observed.trading_blocked
                or observed.options_trading_blocked
            ):
                blocks.add(ReconciliationBlockCode.ACCOUNT_NOT_EXECUTABLE)
            if not _cash_within_fee_tolerance(observed.cash, expectation.expected_cash):
                blocks.add(ReconciliationBlockCode.ACCOUNT_ADJUSTMENT)

        if sweep.final_positions != expectation.expected_positions:
            blocks.add(ReconciliationBlockCode.UNEXPECTED_INVENTORY)
            if any(item.kind == InventoryKind.EQUITY for item in sweep.final_positions):
                blocks.add(ReconciliationBlockCode.ASSIGNMENT_SUSPECTED)
        if sweep.final_open_orders != expectation.expected_open_orders:
            blocks.add(ReconciliationBlockCode.UNEXPECTED_OPEN_ORDER)
        observed_activities = {item.activity_id_hash: item for item in sweep.activities}
        known_activities = {item.activity_id_hash: item for item in expectation.known_activities}
        if any(
            observed_activities.get(activity_hash) != known
            for activity_hash, known in known_activities.items()
            if not activity_predates_window(known, expectation.required_activity_window_start)
        ):
            blocks.add(ReconciliationBlockCode.KNOWN_ACTIVITY_MISSING)
        if any(
            activity_hash not in known_activities
            for activity_hash, item in observed_activities.items()
            if item.activity_type is not ActivityType.FEE
        ):
            blocks.add(ReconciliationBlockCode.UNEXPECTED_ACTIVITY)
        if any(item.occurred_at > accepted_at for item in sweep.activities):
            blocks.add(ReconciliationBlockCode.FUTURE_OBSERVATION)
        unresolved_activities = tuple(
            item
            for item in sweep.activities
            if item.activity_id_hash not in expectation.resolved_activity_hashes
        )
        activity_types = {item.activity_type.value for item in unresolved_activities}
        if activity_types & ASSIGNMENT_ACTIVITY_TYPES:
            blocks.add(ReconciliationBlockCode.ASSIGNMENT_SUSPECTED)
        if activity_types & ACCOUNT_ADJUSTMENT_ACTIVITY_TYPES:
            blocks.add(ReconciliationBlockCode.ACCOUNT_ADJUSTMENT)

        positions_hash = _canonical_hash(_canonical_value(sweep.final_positions))
        orders_hash = _canonical_hash(_canonical_value(sweep.final_open_orders))
        activities_hash = _canonical_hash(_canonical_value(sweep.activities))
        ordered_blocks = tuple(sorted(blocks, key=str))
        material = {
            "domain": "alphadecay.whole-account-reconciliation.v1",
            "expectation": _canonical_value(expectation),
            "sweep": _canonical_value(sweep),
            "accepted_at": _utc_text(accepted_at),
            "positions_manifest_hash": positions_hash,
            "orders_manifest_hash": orders_hash,
            "activities_manifest_hash": activities_hash,
            "block_codes": [code.value for code in ordered_blocks],
        }
        reconciliation_hash = _canonical_hash(material)
        reconciliation = object.__new__(cls)
        values = {
            "reconciliation_id": uuid5(
                NAMESPACE_URL, f"alphadecay:whole-account:{reconciliation_hash}"
            ),
            "reconciliation_hash": reconciliation_hash,
            "expectation": expectation,
            "sweep": sweep,
            "accepted_at": accepted_at,
            "positions_manifest_hash": positions_hash,
            "orders_manifest_hash": orders_hash,
            "activities_manifest_hash": activities_hash,
            "safe": not ordered_blocks,
            "block_codes": ordered_blocks,
        }
        for name, value in values.items():
            object.__setattr__(reconciliation, name, value)
        return reconciliation


def _account_material(value: AccountObservation) -> tuple[object, ...]:
    return (
        value.role,
        value.account_fingerprint,
        value.paper,
        value.status,
        value.account_blocked,
        value.trading_blocked,
        value.options_trading_blocked,
        value.cash,
        value.time_quality,
    )


def _require_hash(value: str, code: str) -> None:
    if not isinstance(value, str) or not HASH_PATTERN.fullmatch(value):
        raise ValueError(code)


def _require_token(value: object, maximum: int, code: str) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or not value.isascii()
        or any(character.isspace() or ord(character) < 33 for character in value)
    ):
        raise ValueError(code)


def _require_utc(value: datetime, code: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(code)


def _require_canonical(items: tuple[Any, ...], *, key: Callable[[Any], object], code: str) -> None:
    keys = tuple(key(item) for item in items)
    if keys != tuple(sorted(keys)) or len(set(keys)) != len(keys):
        raise ValueError(code)


def _require_unique_provider_orders(items: tuple[OpenOrderItem, ...]) -> None:
    provider_order_values = tuple(item.provider_order_id for item in items)
    if len(set(provider_order_values)) != len(provider_order_values):
        raise ValueError("OPEN_ORDER_PROVIDER_ID_DUPLICATE")


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("NONFINITE_DECIMAL")
        return canonical_decimal(value)
    if isinstance(value, timedelta):
        return {
            "days": value.days,
            "seconds": value.seconds,
            "microseconds": value.microseconds,
        }
    if isinstance(value, datetime):
        return _utc_text(value)
    if isinstance(value, ActivityItem):
        material = {
            name: _canonical_value(getattr(value, name))
            for name in (
                "activity_id_hash",
                "activity_type",
                "occurred_at",
                "symbol",
                "signed_quantity",
                "provider_order_id",
                "client_order_id",
            )
        }
        if value.time_quality != "EXACT_TRANSACTION_TIME":
            material["time_quality"] = value.time_quality
        if value.provider_activity_type is not None:
            material["provider_activity_type"] = value.provider_activity_type
        return material
    if hasattr(value, "__dataclass_fields__"):
        return {name: _canonical_value(getattr(value, name)) for name in value.__dataclass_fields__}
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, tuple):
        return [_canonical_value(item) for item in value]
    return value


def _canonical_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()

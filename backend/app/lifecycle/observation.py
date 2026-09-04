from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from backend.app.alpaca.execution_evidence import LifecycleAccountEvidence, LifecycleOptionEvidence
from backend.app.alpaca.market_data import NormalizedLifecycleMarketEvidence
from backend.app.contracts.v1 import AccountRole
from backend.app.execution import (
    AccountObservation,
    ActivityType,
    InventoryItem,
    InventoryKind,
    SweepObservation,
)
from backend.app.services.acquisition import (
    LifecycleOptionObservation,
    LifecycleProviderObservation,
    RetainedLifecycleContext,
)

_LOGGER = logging.getLogger(__name__)


class LifecycleObservationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class LifecycleAccountCollector(Protocol):
    def collect(
        self,
        *,
        context: RetainedLifecycleContext,
        trusted_at: datetime,
    ) -> LifecycleAccountEvidence: ...


class LifecycleMarketCollector(Protocol):
    def collect(
        self,
        *,
        context: RetainedLifecycleContext,
        trusted_at: datetime,
    ) -> NormalizedLifecycleMarketEvidence: ...


class LifecycleObservationSink(Protocol):
    def persist_account_observation(
        self,
        *,
        context: RetainedLifecycleContext,
        sweep: SweepObservation,
        trusted_at: datetime,
    ) -> None: ...

    def persist_market_session(
        self,
        *,
        context: RetainedLifecycleContext,
        evidence: NormalizedLifecycleMarketEvidence,
        trusted_at: datetime,
    ) -> None: ...


class AlpacaLifecycleObservationAdapter:
    def __init__(
        self,
        accounts: LifecycleAccountCollector,
        markets: LifecycleMarketCollector,
        sink: LifecycleObservationSink,
    ) -> None:
        self._accounts = accounts
        self._markets = markets
        self._sink = sink

    def observe(
        self,
        context: RetainedLifecycleContext,
        trusted_at: datetime,
    ) -> LifecycleProviderObservation:
        if context.account_role is not AccountRole.DEVELOPMENT and not _structural_submission(
            context
        ):
            raise LifecycleObservationError("DEVELOPMENT_AUTHORITY_REQUIRED")
        account = _call(self._accounts.collect, context=context, trusted_at=trusted_at)
        _validate_account_evidence(context, account)
        options = _option_observations(context, account, trusted_at)
        _call(
            self._sink.persist_account_observation,
            context=context,
            sweep=account.sweep,
            trusted_at=trusted_at,
        )
        market = _call(self._markets.collect, context=context, trusted_at=trusted_at)
        _validate_market_evidence(context, market, trusted_at)
        _call(
            self._sink.persist_market_session,
            context=context,
            evidence=market,
            trusted_at=trusted_at,
        )
        return LifecycleProviderObservation(
            sweep=account.sweep,
            underlying=market.underlying,
            options=options,
            atm_iv=market.atm_iv,
            boundaries=market.boundaries,
            roll_candidates=market.roll_candidates,
        )


def _structural_submission(context: RetainedLifecycleContext) -> bool:
    """The judged account observes its lifecycle only under a registered structural pilot."""
    if context.account_role is not AccountRole.SUBMISSION:
        return False
    from backend.app.lifecycle.structural_pilot import structural_pilot_lifecycle

    return structural_pilot_lifecycle(context) is not None


def _validate_account_evidence(
    context: RetainedLifecycleContext,
    evidence: LifecycleAccountEvidence,
) -> None:
    sweep = evidence.sweep
    expected = tuple(
        InventoryItem(InventoryKind.OPTION, item.symbol, item.signed_quantity, item.multiplier)
        for item in context.account_expected_positions
    )
    first_material = _account_material(sweep.first_account)
    final_material = _account_material(sweep.final_account)
    known_activity_hashes = tuple(sorted(value for value in context.account_activity_hashes))
    observed_activity_hashes = tuple(
        sorted(
            item.activity_id_hash
            for item in sweep.activities
            if item.activity_type is not ActivityType.INITIAL_FUNDING
        )
    )
    if any(
        account.role is not context.account_role
        or account.account_fingerprint != context.account_fingerprint
        or account.paper is not True
        for account in (sweep.first_account, sweep.final_account)
    ):
        raise LifecycleObservationError("ACCOUNT_AUTHORITY_MISMATCH")
    if any(
        account.status != "ACTIVE"
        or account.account_blocked
        or account.trading_blocked
        or account.options_trading_blocked
        for account in (sweep.first_account, sweep.final_account)
    ):
        raise LifecycleObservationError("ACCOUNT_NOT_EXECUTABLE")
    if (
        sweep.first_positions != expected
        or sweep.final_positions != expected
        or any(item.kind is InventoryKind.EQUITY for item in sweep.final_positions)
    ):
        raise LifecycleObservationError("ACCOUNT_INVENTORY_INVALID")
    if sweep.first_open_orders or sweep.final_open_orders:
        raise LifecycleObservationError("OPEN_ORDER_EXISTS")
    if first_material != final_material:
        raise LifecycleObservationError("ACCOUNT_BOOKEND_UNSTABLE")
    if (
        not sweep.positions_complete
        or not sweep.orders_complete
        or sweep.retrieval_started_at > sweep.retrieval_completed_at
        or sweep.retrieval_completed_at - sweep.retrieval_started_at > timedelta(seconds=15)
    ):
        raise LifecycleObservationError("ACCOUNT_SWEEP_INCOMPLETE")
    if (
        not sweep.activity_pagination.complete
        or sweep.activity_pagination.requested_start != context.account_lifecycle_origin_at
        or sweep.activity_pagination.visibility_complete_through
        < sweep.activity_pagination.requested_end - sweep.activity_pagination.visibility_horizon
    ):
        raise LifecycleObservationError("ACTIVITY_PAGINATION_INCOMPLETE")
    if any(
        item.activity_type
        in {ActivityType.OPASN, ActivityType.OPEXC, ActivityType.OPEXP, ActivityType.OPXRC}
        for item in sweep.activities
    ):
        raise LifecycleObservationError("ASSIGNMENT_ACTIVITY_PRESENT")
    if any(
        item.activity_type
        not in {ActivityType.INITIAL_FUNDING, ActivityType.OPTRD, ActivityType.FILL}
        for item in sweep.activities
    ):
        raise LifecycleObservationError("ACCOUNT_ACTIVITY_UNSAFE")
    if observed_activity_hashes != known_activity_hashes:
        raise LifecycleObservationError("ACTIVITY_LINEAGE_INCOMPLETE")


def _option_observations(
    context: RetainedLifecycleContext,
    account: LifecycleAccountEvidence,
    trusted_at: datetime,
) -> tuple[LifecycleOptionObservation, LifecycleOptionObservation]:
    by_symbol: dict[str, LifecycleOptionEvidence] = {item.symbol: item for item in account.options}
    if len(by_symbol) != 2 or set(by_symbol) != {
        item.symbol for item in context.expected_positions
    }:
        raise LifecycleObservationError("OPTION_EVIDENCE_INCOMPLETE")
    results: list[LifecycleOptionObservation] = []
    for position in context.expected_positions:
        item = by_symbol[position.symbol]
        if item.feed != "indicative":
            raise LifecycleObservationError("OPTION_FEED_NOT_INDICATIVE")
        if (
            item.signed_quantity != position.signed_quantity
            or item.multiplier != position.multiplier
            or item.bid_price <= 0
            or item.ask_price < item.bid_price
            or item.source_timestamp > item.retrieved_at
        ):
            raise LifecycleObservationError("OPTION_EVIDENCE_INVALID")
        if trusted_at - item.source_timestamp > timedelta(seconds=30):
            raise LifecycleObservationError("OPTION_EVIDENCE_STALE")
        results.append(
            LifecycleOptionObservation(
                symbol=item.symbol,
                signed_quantity=item.signed_quantity,
                multiplier=item.multiplier,
                active=True,
                tradable=True,
                feed=item.feed,
                bid_price=item.bid_price,
                ask_price=item.ask_price,
                delta=item.delta,
                gamma=item.gamma,
                theta_per_day=item.theta_per_day,
                vega_per_iv_point=item.vega_per_iv_point,
                quote_observed_at=item.source_timestamp,
                greek_observed_at=item.source_timestamp,
                retrieved_at=item.retrieved_at,
                greek_authority_id=context.greek_authority.authority_id,
                greek_timestamp_source_hash=context.greek_authority.timestamp_contract_hash,
                greek_units_source_hash=context.greek_authority.units_source_hash,
                source_hash=item.source_hash,
            )
        )
    return results[0], results[1]


def _validate_market_evidence(
    context: RetainedLifecycleContext,
    evidence: NormalizedLifecycleMarketEvidence,
    trusted_at: datetime,
) -> None:
    if evidence.atm_iv.feed != "indicative":
        raise LifecycleObservationError("ATM_IV_FEED_NOT_INDICATIVE")
    if (
        evidence.atm_iv.underlying != context.thesis.thesis.underlying
        or evidence.underlying.underlying != context.thesis.thesis.underlying
        or evidence.underlying.benchmark_symbol != context.launch_authority.benchmark_symbol
        or evidence.atm_iv.observed_at > evidence.atm_iv.retrieved_at
        or evidence.underlying.quote_observed_at > evidence.underlying.quote_retrieved_at
        # Provider retrieval may complete shortly after the trusted tick time.
        or evidence.boundaries.market_session.retrieved_at > trusted_at + timedelta(seconds=30)
    ):
        raise LifecycleObservationError("MARKET_EVIDENCE_INVALID")
    if (
        trusted_at - evidence.atm_iv.observed_at > timedelta(seconds=30)
        or trusted_at - evidence.underlying.quote_observed_at > timedelta(seconds=30)
        # The bar that closes at the tick boundary is published seconds after it; until then
        # the latest completed bar is the previous five-minute bar, so allow one full
        # cadence plus the publication delay.
        or trusted_at - evidence.underlying.completed_bar_at > timedelta(seconds=420)
        or any(
            trusted_at - item.completed_bar_at > timedelta(seconds=420)
            for item in evidence.boundaries.price_confirmation
        )
    ):
        raise LifecycleObservationError("MARKET_EVIDENCE_STALE")


def _account_material(account: AccountObservation) -> tuple[object, ...]:
    return (
        account.role,
        account.account_fingerprint,
        account.paper,
        account.status,
        account.account_blocked,
        account.trading_blocked,
        account.options_trading_blocked,
        # Equity and buying power move with option marks between the two bookend reads;
        # cash and the authority fields are what must hold still.
        account.cash,
    )


def _call[T](operation: Callable[..., T], **kwargs: object) -> T:
    try:
        return operation(**kwargs)
    except LifecycleObservationError:
        raise
    except Exception as error:
        candidate = getattr(error, "code", None)
        code = (
            candidate
            if isinstance(candidate, str) and _ERROR_CODE.fullmatch(candidate)
            else "PROVIDER_EVIDENCE_FAILED"
        )
        _LOGGER.warning(
            "lifecycle observation %s failed: %s: %s -> %s",
            getattr(operation, "__qualname__", repr(operation)),
            type(error).__name__,
            error,
            code,
        )
        raise LifecycleObservationError(code) from error

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from backend.app.contracts.v1 import (
    AccountRole,
    BaselineStatus,
    CompetitionPerformanceProofResponse,
    MeasurementStatus,
    PerformancePoint,
)

from .models import (
    FINAL_PERFORMANCE_BOUNDARY,
    FINAL_PERFORMANCE_BOUNDARY_KEY,
    FINAL_PUBLICATION_NOT_BEFORE,
    PerformanceSnapshot,
    canonical_hash,
)

FINAL_CAPTURE_CONFIRMATION = "CAPTURE_FINAL_2026-09-04T14:30:00Z"
_DEVELOPMENT_BOUNDARY_PREFIX = "DEVELOPMENT_REHEARSAL_"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class PerformanceCaptureError(RuntimeError):
    pass


class CaptureMode(StrEnum):
    DEVELOPMENT_REHEARSAL = "DEVELOPMENT_REHEARSAL"
    SUBMISSION_FINAL = "SUBMISSION_FINAL"


@dataclass(frozen=True)
class CaptureRequest:
    mode: CaptureMode
    role: AccountRole
    boundary_key: str
    scheduled_for: datetime
    activity_history_start: datetime | None = None
    confirmation: str | None = None

    @classmethod
    def final_submission(cls) -> CaptureRequest:
        return cls(
            mode=CaptureMode.SUBMISSION_FINAL,
            role=AccountRole.SUBMISSION,
            boundary_key=FINAL_PERFORMANCE_BOUNDARY_KEY,
            scheduled_for=FINAL_PERFORMANCE_BOUNDARY,
            confirmation=FINAL_CAPTURE_CONFIRMATION,
        )


@dataclass(frozen=True)
class CaptureAuthority:
    role: AccountRole
    account_fingerprint: str
    baseline_id: UUID | None
    baseline_status: BaselineStatus
    baseline_equity: Decimal | None
    baseline_captured_at: datetime | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.role, AccountRole)
            or _SHA256_PATTERN.fullmatch(self.account_fingerprint) is None
        ):
            raise ValueError("CAPTURE_AUTHORITY_FINGERPRINT_INVALID")
        if self.baseline_equity is not None and (
            not isinstance(self.baseline_equity, Decimal)
            or not self.baseline_equity.is_finite()
            or self.baseline_equity <= 0
        ):
            raise ValueError("CAPTURE_AUTHORITY_BASELINE_INVALID")
        has_baseline = self.baseline_id is not None
        if has_baseline != (self.baseline_equity is not None) or has_baseline != (
            self.baseline_captured_at is not None
        ):
            raise ValueError("CAPTURE_AUTHORITY_BASELINE_INVALID")
        if has_baseline != (self.baseline_status != BaselineStatus.NOT_CAPTURED):
            raise ValueError("CAPTURE_AUTHORITY_BASELINE_INVALID")
        if self.baseline_captured_at is not None:
            _require_utc(
                self.baseline_captured_at,
                "CAPTURE_AUTHORITY_BASELINE_TIME_INVALID",
            )


@dataclass(frozen=True)
class FixedBoundaryAccount:
    role: AccountRole
    account_fingerprint: str
    paper: bool
    equity: Decimal
    observed_at: datetime

    def __post_init__(self) -> None:
        if (
            not isinstance(self.role, AccountRole)
            or _SHA256_PATTERN.fullmatch(self.account_fingerprint) is None
            or self.paper is not True
            or not isinstance(self.equity, Decimal)
            or not self.equity.is_finite()
            or self.equity < 0
        ):
            raise ValueError("FIXED_BOUNDARY_ACCOUNT_INVALID")
        _require_utc(self.observed_at, "FIXED_BOUNDARY_ACCOUNT_TIME_INVALID")


@dataclass(frozen=True)
class ActivityManifest:
    items: tuple[dict[str, object], ...]
    complete_from: datetime
    complete_through: datetime
    coverage_gaps: tuple[tuple[datetime, datetime], ...] = ()

    def __post_init__(self) -> None:
        _require_utc(self.complete_from, "ACTIVITY_COMPLETENESS_TIME_INVALID")
        _require_utc(self.complete_through, "ACTIVITY_COMPLETENESS_TIME_INVALID")
        if self.complete_through < self.complete_from:
            raise ValueError("ACTIVITY_COMPLETENESS_TIME_INVALID")
        for gap_start, gap_end in self.coverage_gaps:
            _require_utc(gap_start, "ACTIVITY_COMPLETENESS_TIME_INVALID")
            _require_utc(gap_end, "ACTIVITY_COMPLETENESS_TIME_INVALID")
            if gap_end <= gap_start:
                raise ValueError("ACTIVITY_COMPLETENESS_TIME_INVALID")
        _canonical_manifest(self.items, "ACTIVITY_MANIFEST_INVALID")


@dataclass(frozen=True)
class FixedBoundaryObservation:
    role: AccountRole
    account_fingerprint: str
    retrieval_started_at: datetime
    retrieval_completed_at: datetime
    current_equity: Decimal
    positions_manifest_hash: str
    orders_manifest_hash: str
    activities_manifest_hash: str


@dataclass(frozen=True)
class CaptureOutcome:
    snapshot: PerformanceSnapshot
    persisted: bool


class FixedBoundaryReadPort(Protocol):
    def read_account(self) -> FixedBoundaryAccount: ...

    def read_positions(self) -> tuple[dict[str, object], ...]: ...

    def read_open_orders(self) -> tuple[dict[str, object], ...]: ...

    def read_activities(
        self, *, complete_from: datetime, through: datetime
    ) -> ActivityManifest: ...


class CaptureAuthorityPort(Protocol):
    def trusted_performance_time(self) -> datetime: ...

    def performance_capture_authority(self, role: AccountRole) -> CaptureAuthority: ...


class SnapshotPort(Protocol):
    def append_snapshot(self, snapshot: PerformanceSnapshot) -> None: ...


class PerformancePublisher(Protocol):
    def publish_latest_eligible(self) -> CompetitionPerformanceProofResponse: ...


class FixedBoundaryCollector:
    def __init__(
        self,
        source: FixedBoundaryReadPort,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._source = source
        self._clock = clock

    def collect(
        self, *, boundary: datetime, activity_history_start: datetime
    ) -> FixedBoundaryObservation:
        _require_utc(boundary, "PERFORMANCE_BOUNDARY_TIME_INVALID")
        _require_utc(activity_history_start, "ACTIVITY_COMPLETENESS_TIME_INVALID")
        if activity_history_start > boundary:
            raise PerformanceCaptureError("ACTIVITY_WINDOW_INVALID")
        started_at = self._time()
        if started_at < boundary:
            raise PerformanceCaptureError("PERFORMANCE_CAPTURE_TOO_EARLY")

        first_account = self._source.read_account()
        first_positions = _canonical_manifest(
            self._source.read_positions(), "POSITION_MANIFEST_INVALID"
        )
        first_orders = _canonical_manifest(
            self._source.read_open_orders(), "ORDER_MANIFEST_INVALID"
        )
        first_activity_material = _activity_material(
            self._source.read_activities(
                complete_from=activity_history_start,
                through=boundary,
            ),
            expected_from=activity_history_start,
            expected_through=boundary,
        )
        final_activity_material = _activity_material(
            self._source.read_activities(
                complete_from=activity_history_start,
                through=boundary,
            ),
            expected_from=activity_history_start,
            expected_through=boundary,
        )
        final_positions = _canonical_manifest(
            self._source.read_positions(), "POSITION_MANIFEST_INVALID"
        )
        final_orders = _canonical_manifest(
            self._source.read_open_orders(), "ORDER_MANIFEST_INVALID"
        )
        final_account = self._source.read_account()
        completed_at = self._time()

        if completed_at < started_at or not (
            started_at
            <= first_account.observed_at
            <= final_account.observed_at
            <= completed_at
        ):
            raise PerformanceCaptureError("PERFORMANCE_CAPTURE_TIME_INVALID")
        if _account_material(first_account) != _account_material(final_account):
            raise PerformanceCaptureError("ACCOUNT_STATE_UNSTABLE")
        if first_positions != final_positions:
            raise PerformanceCaptureError("POSITION_STATE_UNSTABLE")
        if first_orders != final_orders:
            raise PerformanceCaptureError("ORDER_STATE_UNSTABLE")
        if first_activity_material != final_activity_material:
            raise PerformanceCaptureError("ACTIVITY_STATE_UNSTABLE")
        return FixedBoundaryObservation(
            role=first_account.role,
            account_fingerprint=first_account.account_fingerprint,
            retrieval_started_at=started_at,
            retrieval_completed_at=completed_at,
            current_equity=first_account.equity,
            positions_manifest_hash=canonical_hash(first_positions),
            orders_manifest_hash=canonical_hash(first_orders),
            activities_manifest_hash=canonical_hash(first_activity_material),
        )

    def _time(self) -> datetime:
        value = self._clock()
        _require_utc(value, "PERFORMANCE_CAPTURE_TIME_INVALID")
        return value


class PerformanceCaptureWorkflow:
    def __init__(
        self,
        *,
        authority: CaptureAuthorityPort,
        snapshots: SnapshotPort,
        publisher: PerformancePublisher,
        collector: FixedBoundaryCollector,
    ) -> None:
        self._authority = authority
        self._snapshots = snapshots
        self._publisher = publisher
        self._collector = collector

    def capture(self, request: CaptureRequest) -> CaptureOutcome:
        self._validate_request(request)
        before = self._trusted_time()
        self._validate_capture_time(request, before)
        authority = self._authority.performance_capture_authority(request.role)
        self._validate_authority(request, authority)

        activity_history_start = (
            authority.baseline_captured_at
            if request.mode == CaptureMode.SUBMISSION_FINAL
            else request.activity_history_start
        )
        if activity_history_start is None:
            raise PerformanceCaptureError("ACTIVITY_HISTORY_START_REQUIRED")
        observation = self._collector.collect(
            boundary=request.scheduled_for,
            activity_history_start=activity_history_start,
        )
        after = self._trusted_time()
        if after < before or observation.retrieval_completed_at > after:
            raise PerformanceCaptureError("CAPTURE_TIME_AUTHORITY_MISMATCH")
        self._validate_capture_time(request, after)
        if (
            observation.role != authority.role
            or observation.account_fingerprint != authority.account_fingerprint
        ):
            raise PerformanceCaptureError("CAPTURE_ACCOUNT_AUTHORITY_MISMATCH")

        change: Decimal | None = None
        return_pct: Decimal | None = None
        if authority.baseline_status == BaselineStatus.CLEAN:
            assert authority.baseline_equity is not None
            change = observation.current_equity - authority.baseline_equity
            return_pct = change / authority.baseline_equity * Decimal(100)
        point = PerformancePoint(
            scheduled_for=request.scheduled_for,
            attempted_at=observation.retrieval_started_at,
            measured_at=observation.retrieval_completed_at,
            status=MeasurementStatus.COMPLETE,
            failure_code=None,
            current_equity_usd=observation.current_equity,
            account_equity_change_usd=change,
            account_equity_return_pct=return_pct,
            reconciled_lifecycle_cashflow_usd=None,
            open_position_liquidation_pnl_usd=None,
            simulator_limitations_code="ALPACA_PAPER_SIMULATION",
        )
        identity_material = {
            "domain": "alphadecay.performance-capture.v1",
            "role": request.role.value,
            "boundary_key": request.boundary_key,
            "point": point.model_dump(mode="json"),
            "account_fingerprint": authority.account_fingerprint,
            "positions_manifest_hash": observation.positions_manifest_hash,
            "orders_manifest_hash": observation.orders_manifest_hash,
            "activities_manifest_hash": observation.activities_manifest_hash,
        }
        snapshot = PerformanceSnapshot.create(
            snapshot_id=uuid5(NAMESPACE_URL, canonical_hash(identity_material)),
            submission_baseline_id=authority.baseline_id,
            boundary_key=request.boundary_key,
            point=point,
            baseline_status=authority.baseline_status,
            account_fingerprint=authority.account_fingerprint,
            positions_manifest_hash=observation.positions_manifest_hash,
            orders_manifest_hash=observation.orders_manifest_hash,
            activities_manifest_hash=observation.activities_manifest_hash,
        )
        persisted = request.mode == CaptureMode.SUBMISSION_FINAL
        if persisted:
            self._snapshots.append_snapshot(snapshot)
        return CaptureOutcome(snapshot=snapshot, persisted=persisted)

    def publish_final(
        self, *, confirmation: str
    ) -> CompetitionPerformanceProofResponse:
        if confirmation != FINAL_CAPTURE_CONFIRMATION:
            raise PerformanceCaptureError("SUBMISSION_CONFIRMATION_REQUIRED")
        if self._trusted_time() < FINAL_PUBLICATION_NOT_BEFORE:
            raise PerformanceCaptureError("FINAL_PUBLICATION_TOO_EARLY")
        authority = self._authority.performance_capture_authority(AccountRole.SUBMISSION)
        self._validate_submission_authority(authority)
        return self._publisher.publish_latest_eligible()

    @staticmethod
    def _validate_request(request: CaptureRequest) -> None:
        _require_utc(request.scheduled_for, "PERFORMANCE_BOUNDARY_TIME_INVALID")
        if request.mode == CaptureMode.SUBMISSION_FINAL:
            if request.role != AccountRole.SUBMISSION:
                raise PerformanceCaptureError("SUBMISSION_ROLE_REQUIRED")
            if request.confirmation != FINAL_CAPTURE_CONFIRMATION:
                raise PerformanceCaptureError("SUBMISSION_CONFIRMATION_REQUIRED")
            if (
                request.boundary_key != FINAL_PERFORMANCE_BOUNDARY_KEY
                or request.scheduled_for != FINAL_PERFORMANCE_BOUNDARY
            ):
                raise PerformanceCaptureError("SUBMISSION_BOUNDARY_INVALID")
            if request.activity_history_start is not None:
                raise PerformanceCaptureError("SUBMISSION_HISTORY_START_FORBIDDEN")
            return
        if request.role != AccountRole.DEVELOPMENT:
            raise PerformanceCaptureError("DEVELOPMENT_ROLE_REQUIRED")
        if not request.boundary_key.startswith(_DEVELOPMENT_BOUNDARY_PREFIX):
            raise PerformanceCaptureError("DEVELOPMENT_BOUNDARY_INVALID")
        if request.activity_history_start is None:
            raise PerformanceCaptureError("ACTIVITY_HISTORY_START_REQUIRED")
        _require_utc(
            request.activity_history_start,
            "ACTIVITY_COMPLETENESS_TIME_INVALID",
        )
        if request.activity_history_start > request.scheduled_for:
            raise PerformanceCaptureError("ACTIVITY_WINDOW_INVALID")
        if request.confirmation is not None:
            raise PerformanceCaptureError("DEVELOPMENT_CONFIRMATION_FORBIDDEN")

    @staticmethod
    def _validate_capture_time(request: CaptureRequest, now: datetime) -> None:
        if request.scheduled_for > now:
            code = (
                "SUBMISSION_CAPTURE_WINDOW_CLOSED"
                if request.mode == CaptureMode.SUBMISSION_FINAL
                else "DEVELOPMENT_BOUNDARY_IN_FUTURE"
            )
            raise PerformanceCaptureError(code)
        if (
            request.mode == CaptureMode.SUBMISSION_FINAL
            and now >= FINAL_PUBLICATION_NOT_BEFORE
        ):
            raise PerformanceCaptureError("SUBMISSION_CAPTURE_WINDOW_CLOSED")

    @classmethod
    def _validate_authority(
        cls, request: CaptureRequest, authority: CaptureAuthority
    ) -> None:
        if authority.role != request.role:
            raise PerformanceCaptureError("CAPTURE_ROLE_AUTHORITY_MISMATCH")
        if request.mode == CaptureMode.SUBMISSION_FINAL:
            cls._validate_submission_authority(authority)
        elif authority.baseline_id is not None:
            raise PerformanceCaptureError("DEVELOPMENT_BASELINE_FORBIDDEN")

    @staticmethod
    def _validate_submission_authority(authority: CaptureAuthority) -> None:
        if authority.role != AccountRole.SUBMISSION:
            raise PerformanceCaptureError("SUBMISSION_ROLE_REQUIRED")
        if (
            authority.baseline_id is None
            or authority.baseline_status != BaselineStatus.CLEAN
            or authority.baseline_equity != Decimal("100000")
            or authority.baseline_captured_at is None
        ):
            raise PerformanceCaptureError("SUBMISSION_BASELINE_NOT_CLEAN")

    def _trusted_time(self) -> datetime:
        value = self._authority.trusted_performance_time()
        _require_utc(value, "TRUSTED_PERFORMANCE_TIME_INVALID")
        return value


def _account_material(value: FixedBoundaryAccount) -> tuple[object, ...]:
    return (
        value.role,
        value.account_fingerprint,
        value.paper,
        value.equity,
    )


def _canonical_manifest(
    values: tuple[dict[str, object], ...], code: str
) -> tuple[dict[str, object], ...]:
    if not isinstance(values, tuple) or any(not isinstance(item, dict) for item in values):
        raise PerformanceCaptureError(code)
    try:
        encoded = tuple(
            json.dumps(item, sort_keys=True, separators=(",", ":"), allow_nan=False)
            for item in values
        )
    except (TypeError, ValueError) as exc:
        raise PerformanceCaptureError(code) from exc
    if len(set(encoded)) != len(encoded):
        raise PerformanceCaptureError(code)
    return tuple(json.loads(item) for item in sorted(encoded))


def _activity_material(
    manifest: ActivityManifest,
    *,
    expected_from: datetime,
    expected_through: datetime,
) -> dict[str, object]:
    if (
        manifest.complete_from != expected_from
        or manifest.complete_through != expected_through
        or manifest.coverage_gaps
    ):
        raise PerformanceCaptureError("ACTIVITY_WINDOW_INCOMPLETE")
    return {
        "complete_from": manifest.complete_from.isoformat(),
        "complete_through": manifest.complete_through.isoformat(),
        "coverage_gaps": (),
        "items": _canonical_manifest(manifest.items, "ACTIVITY_MANIFEST_INVALID"),
    }


def _require_utc(value: datetime, code: str) -> None:
    if (
        value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset() != UTC.utcoffset(value)
    ):
        raise PerformanceCaptureError(code)

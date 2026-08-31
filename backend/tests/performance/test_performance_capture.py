from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from backend.app.contracts.v1 import AccountRole, BaselineStatus
from backend.app.performance.capture import (
    FINAL_CAPTURE_CONFIRMATION,
    ActivityManifest,
    CaptureAuthority,
    CaptureMode,
    CaptureRequest,
    FixedBoundaryAccount,
    FixedBoundaryCollector,
    PerformanceCaptureError,
    PerformanceCaptureWorkflow,
)
from backend.app.performance.models import (
    FINAL_PERFORMANCE_BOUNDARY,
    FINAL_PERFORMANCE_BOUNDARY_KEY,
    FINAL_PUBLICATION_NOT_BEFORE,
)

FINGERPRINT = "f" * 64
BASELINE_ID = UUID("00000000-0000-0000-0000-000000000500")


class ReadPort:
    def __init__(self, *, role: AccountRole, boundary: datetime) -> None:
        self.role = role
        self.boundary = boundary
        self.calls: list[str] = []
        self.equity = Decimal("100125.50")
        self.positions = ({"symbol": "OPTION_A", "quantity": "1"},)
        self.orders = ({"client_order_id": "order-1", "state": "open"},)
        self.activities = ({"activity_id_hash": "a" * 64, "type": "fill"},)
        self.final_activities: tuple[dict[str, object], ...] | None = None
        self.final_equity: Decimal | None = None
        self.activity_complete_from = boundary - timedelta(days=2)
        self.activity_complete_through = boundary
        self.activity_coverage_gaps: tuple[tuple[datetime, datetime], ...] = ()

    def read_account(self) -> FixedBoundaryAccount:
        self.calls.append("account")
        equity = (
            self.equity
            if self.final_equity is None or self.calls.count("account") == 1
            else self.final_equity
        )
        offset = len(self.calls)
        return FixedBoundaryAccount(
            role=self.role,
            account_fingerprint=FINGERPRINT,
            paper=True,
            equity=equity,
            observed_at=self.boundary + timedelta(seconds=offset),
        )

    def read_positions(self) -> tuple[dict[str, object], ...]:
        self.calls.append("positions")
        return self.positions

    def read_open_orders(self) -> tuple[dict[str, object], ...]:
        self.calls.append("orders")
        return self.orders

    def read_activities(
        self, *, complete_from: datetime, through: datetime
    ) -> ActivityManifest:
        self.calls.append("activities")
        assert complete_from == self.boundary - timedelta(days=2)
        assert through == self.boundary
        items = (
            self.activities
            if self.final_activities is None or self.calls.count("activities") == 1
            else self.final_activities
        )
        return ActivityManifest(
            items=items,
            complete_from=self.activity_complete_from,
            complete_through=self.activity_complete_through,
            coverage_gaps=self.activity_coverage_gaps,
        )


class MutableAliasingReadPort(ReadPort):
    def read_activities(
        self, *, complete_from: datetime, through: datetime
    ) -> ActivityManifest:
        if self.calls.count("activities") == 1:
            self.activities[0]["type"] = "journal"
        return super().read_activities(
            complete_from=complete_from,
            through=through,
        )


class Clock:
    def __init__(self, *values: datetime) -> None:
        self.values = list(values)

    def __call__(self) -> datetime:
        if len(self.values) > 1:
            return self.values.pop(0)
        return self.values[0]


@dataclass
class Harness:
    role: AccountRole
    now: datetime
    baseline_status: BaselineStatus = BaselineStatus.CLEAN

    def __post_init__(self) -> None:
        self.appended = []
        self.publish_calls = 0
        self.source_calls = 0

    def trusted_performance_time(self) -> datetime:
        return self.now

    def performance_capture_authority(self, role: AccountRole) -> CaptureAuthority:
        assert role == self.role
        if role == AccountRole.SUBMISSION:
            return CaptureAuthority(
                role=role,
                account_fingerprint=FINGERPRINT,
                baseline_id=BASELINE_ID,
                baseline_status=self.baseline_status,
                baseline_equity=Decimal("100000"),
                baseline_captured_at=FINAL_PERFORMANCE_BOUNDARY - timedelta(days=2),
            )
        return CaptureAuthority(
            role=role,
            account_fingerprint=FINGERPRINT,
            baseline_id=None,
            baseline_status=BaselineStatus.NOT_CAPTURED,
            baseline_equity=None,
            baseline_captured_at=None,
        )

    def append_snapshot(self, snapshot) -> None:
        self.appended.append(snapshot)

    def publish_latest_eligible(self):
        self.publish_calls += 1
        return "published"


def collector(port: ReadPort, *, completed_at: datetime | None = None) -> FixedBoundaryCollector:
    started = port.boundary
    completed = completed_at or port.boundary + timedelta(seconds=10)
    return FixedBoundaryCollector(port, clock=Clock(started, completed))


def test_collector_reads_a_stable_paper_book_and_hashes_private_manifests() -> None:
    boundary = datetime(2026, 8, 31, 14, 30, tzinfo=UTC)
    port = ReadPort(role=AccountRole.DEVELOPMENT, boundary=boundary)

    observation = collector(port).collect(
        boundary=boundary,
        activity_history_start=port.activity_complete_from,
    )

    assert port.calls == [
        "account",
        "positions",
        "orders",
        "activities",
        "activities",
        "positions",
        "orders",
        "account",
    ]
    assert observation.current_equity == Decimal("100125.50")
    assert len(observation.positions_manifest_hash) == 64
    assert len(observation.orders_manifest_hash) == 64
    assert len(observation.activities_manifest_hash) == 64
    assert "OPTION_A" not in repr(observation)
    assert "order-1" not in repr(observation)


def test_collector_rejects_an_unstable_book_or_incomplete_activity_window() -> None:
    boundary = datetime(2026, 8, 31, 14, 30, tzinfo=UTC)
    unstable = ReadPort(role=AccountRole.DEVELOPMENT, boundary=boundary)
    unstable.final_equity = Decimal("100126")
    with pytest.raises(PerformanceCaptureError, match="ACCOUNT_STATE_UNSTABLE"):
        collector(unstable).collect(
            boundary=boundary,
            activity_history_start=unstable.activity_complete_from,
        )

    incomplete = ReadPort(role=AccountRole.DEVELOPMENT, boundary=boundary)
    incomplete.activity_complete_through = boundary - timedelta(microseconds=1)
    with pytest.raises(PerformanceCaptureError, match="ACTIVITY_WINDOW_INCOMPLETE"):
        collector(incomplete).collect(
            boundary=boundary,
            activity_history_start=incomplete.activity_complete_from,
        )


@pytest.mark.parametrize("defect", ("mutation", "truncation", "gap"))
def test_collector_rejects_unstable_or_incomplete_activity_history(defect: str) -> None:
    boundary = datetime(2026, 8, 31, 14, 30, tzinfo=UTC)
    port = ReadPort(role=AccountRole.DEVELOPMENT, boundary=boundary)
    if defect == "mutation":
        port.final_activities = (
            *port.activities,
            {"activity_id_hash": "b" * 64, "type": "journal"},
        )
        expected = "ACTIVITY_STATE_UNSTABLE"
    elif defect == "truncation":
        port.activity_complete_from += timedelta(microseconds=1)
        expected = "ACTIVITY_WINDOW_INCOMPLETE"
    else:
        port.activity_coverage_gaps = (
            (boundary - timedelta(hours=1), boundary - timedelta(minutes=30)),
        )
        expected = "ACTIVITY_WINDOW_INCOMPLETE"

    with pytest.raises(PerformanceCaptureError, match=expected):
        collector(port).collect(
            boundary=boundary,
            activity_history_start=boundary - timedelta(days=2),
        )


def test_collector_freezes_each_activity_sweep_before_a_reused_dict_is_mutated() -> None:
    boundary = datetime(2026, 8, 31, 14, 30, tzinfo=UTC)
    port = MutableAliasingReadPort(role=AccountRole.DEVELOPMENT, boundary=boundary)

    with pytest.raises(PerformanceCaptureError, match="ACTIVITY_STATE_UNSTABLE"):
        collector(port).collect(
            boundary=boundary,
            activity_history_start=port.activity_complete_from,
        )


def test_development_rehearsal_exercises_capture_without_persistence_or_publication() -> None:
    boundary = datetime(2026, 8, 31, 14, 30, tzinfo=UTC)
    now = boundary + timedelta(minutes=1)
    port = ReadPort(role=AccountRole.DEVELOPMENT, boundary=boundary)
    harness = Harness(AccountRole.DEVELOPMENT, now)
    workflow = PerformanceCaptureWorkflow(
        authority=harness,
        snapshots=harness,
        publisher=harness,
        collector=collector(port),
    )

    outcome = workflow.capture(
        CaptureRequest(
            mode=CaptureMode.DEVELOPMENT_REHEARSAL,
            role=AccountRole.DEVELOPMENT,
            boundary_key="DEVELOPMENT_REHEARSAL_2026-08-31T14:30:00Z",
            scheduled_for=boundary,
            activity_history_start=boundary - timedelta(days=2),
        )
    )

    assert outcome.persisted is False
    assert outcome.snapshot.point.account_equity_change_usd is None
    assert outcome.snapshot.point.account_equity_return_pct is None
    assert outcome.snapshot.point.reconciled_lifecycle_cashflow_usd is None
    assert outcome.snapshot.point.open_position_liquidation_pnl_usd is None
    assert harness.appended == []
    assert harness.publish_calls == 0


@pytest.mark.parametrize(
    ("role", "mode", "confirmation", "message"),
    [
        (
            AccountRole.DEVELOPMENT,
            CaptureMode.SUBMISSION_FINAL,
            FINAL_CAPTURE_CONFIRMATION,
            "SUBMISSION_ROLE_REQUIRED",
        ),
        (
            AccountRole.SUBMISSION,
            CaptureMode.SUBMISSION_FINAL,
            None,
            "SUBMISSION_CONFIRMATION_REQUIRED",
        ),
        (
            AccountRole.SUBMISSION,
            CaptureMode.DEVELOPMENT_REHEARSAL,
            None,
            "DEVELOPMENT_ROLE_REQUIRED",
        ),
    ],
)
def test_capture_mode_and_role_are_not_interchangeable(
    role: AccountRole,
    mode: CaptureMode,
    confirmation: str | None,
    message: str,
) -> None:
    now = FINAL_PERFORMANCE_BOUNDARY + timedelta(minutes=1)
    harness = Harness(role, now)
    port = ReadPort(role=role, boundary=FINAL_PERFORMANCE_BOUNDARY)
    workflow = PerformanceCaptureWorkflow(
        authority=harness,
        snapshots=harness,
        publisher=harness,
        collector=collector(port),
    )

    with pytest.raises(PerformanceCaptureError, match=message):
        workflow.capture(
            CaptureRequest(
                mode=mode,
                role=role,
                boundary_key=FINAL_PERFORMANCE_BOUNDARY_KEY,
                scheduled_for=FINAL_PERFORMANCE_BOUNDARY,
                confirmation=confirmation,
            )
        )
    assert port.calls == []
    assert harness.appended == []


@pytest.mark.parametrize(
    "now",
    [
        FINAL_PERFORMANCE_BOUNDARY - timedelta(microseconds=1),
        FINAL_PUBLICATION_NOT_BEFORE,
    ],
)
def test_submission_capture_is_limited_to_the_fixed_grace_window(now: datetime) -> None:
    harness = Harness(AccountRole.SUBMISSION, now)
    port = ReadPort(role=AccountRole.SUBMISSION, boundary=FINAL_PERFORMANCE_BOUNDARY)
    workflow = PerformanceCaptureWorkflow(
        authority=harness,
        snapshots=harness,
        publisher=harness,
        collector=collector(port),
    )

    with pytest.raises(PerformanceCaptureError, match="SUBMISSION_CAPTURE_WINDOW_CLOSED"):
        workflow.capture(CaptureRequest.final_submission())
    assert port.calls == []


def test_submission_capture_persists_exact_baseline_math_without_fabricated_attribution() -> None:
    now = FINAL_PERFORMANCE_BOUNDARY + timedelta(minutes=1)
    harness = Harness(AccountRole.SUBMISSION, now)
    port = ReadPort(role=AccountRole.SUBMISSION, boundary=FINAL_PERFORMANCE_BOUNDARY)
    workflow = PerformanceCaptureWorkflow(
        authority=harness,
        snapshots=harness,
        publisher=harness,
        collector=collector(port),
    )

    outcome = workflow.capture(CaptureRequest.final_submission())

    assert outcome.persisted is True
    assert harness.appended == [outcome.snapshot]
    assert outcome.snapshot.point.current_equity_usd == Decimal("100125.50")
    assert outcome.snapshot.point.account_equity_change_usd == Decimal("125.50")
    assert outcome.snapshot.point.account_equity_return_pct == Decimal("0.125500")
    assert outcome.snapshot.point.reconciled_lifecycle_cashflow_usd is None
    assert outcome.snapshot.point.open_position_liquidation_pnl_usd is None
    assert harness.publish_calls == 0


def test_submission_capture_requires_clean_fixed_baseline_authority() -> None:
    now = FINAL_PERFORMANCE_BOUNDARY + timedelta(minutes=1)
    harness = Harness(
        AccountRole.SUBMISSION,
        now,
        baseline_status=BaselineStatus.CONTAMINATED,
    )
    port = ReadPort(role=AccountRole.SUBMISSION, boundary=FINAL_PERFORMANCE_BOUNDARY)
    workflow = PerformanceCaptureWorkflow(
        authority=harness,
        snapshots=harness,
        publisher=harness,
        collector=collector(port),
    )

    with pytest.raises(PerformanceCaptureError, match="SUBMISSION_BASELINE_NOT_CLEAN"):
        workflow.capture(CaptureRequest.final_submission())
    assert port.calls == []


def test_final_publication_has_a_separate_confirmation_and_time_gate() -> None:
    harness = Harness(
        AccountRole.SUBMISSION,
        FINAL_PUBLICATION_NOT_BEFORE - timedelta(microseconds=1),
    )
    port = ReadPort(role=AccountRole.SUBMISSION, boundary=FINAL_PERFORMANCE_BOUNDARY)
    workflow = PerformanceCaptureWorkflow(
        authority=harness,
        snapshots=harness,
        publisher=harness,
        collector=collector(port),
    )

    with pytest.raises(PerformanceCaptureError, match="FINAL_PUBLICATION_TOO_EARLY"):
        workflow.publish_final(confirmation=FINAL_CAPTURE_CONFIRMATION)
    with pytest.raises(PerformanceCaptureError, match="SUBMISSION_CONFIRMATION_REQUIRED"):
        workflow.publish_final(confirmation="wrong")
    harness.now = FINAL_PUBLICATION_NOT_BEFORE
    assert workflow.publish_final(confirmation=FINAL_CAPTURE_CONFIRMATION) == "published"
    assert harness.publish_calls == 1

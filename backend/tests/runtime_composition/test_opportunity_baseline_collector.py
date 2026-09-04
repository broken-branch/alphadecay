from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from backend.app.alpaca.execution_evidence import baseline_account_fingerprint
from backend.app.alpaca.opportunity import OpportunitySnapshotRequest
from backend.app.contracts.v1 import AccountRole
from backend.app.persistence.opportunity_evidence import OpportunityPlanSpec
from backend.app.policy.opportunity import OpportunityPolicy
from backend.app.services.opportunity_baseline import (
    ActivityPage,
    OpportunityBaselineCollectionError,
    SubmissionReconciliationBinding,
    collect_development_opportunity_bootstrap,
    collect_opportunity_bootstrap,
)
from backend.app.services.opportunity_bootstrap import (
    bootstrap_development_opportunity,
    parse_development_opportunity_bootstrap,
    parse_opportunity_bootstrap,
)

ACCOUNT_ID = UUID("11111111-1111-1111-1111-111111111111")
ACCOUNT = baseline_account_fingerprint(ACCOUNT_ID)
FROZEN = datetime(2026, 8, 30, 14, tzinfo=UTC)
CAPTURED = datetime(2026, 8, 30, 15, tzinfo=UTC)


def _policy() -> OpportunityPolicy:
    return OpportunityPolicy(
        version="test-v1",
        opportunity_key="ACME_EARNINGS",
        underlying="ACME",
        selected_decision_boundary=CAPTURED + timedelta(days=1),
        last_entry_boundary=CAPTURED + timedelta(days=1, minutes=30),
        maximum_decision_delay=timedelta(minutes=5),
        maximum_underlying_age=timedelta(minutes=2),
        maximum_catalyst_age=timedelta(days=1),
        maximum_option_quote_age=timedelta(minutes=1),
        maximum_leg_quote_skew=timedelta(seconds=5),
        minimum_vwap_distance=Decimal("0.01"),
        maximum_vwap_distance=Decimal("0.05"),
        minimum_relative_return=Decimal("0.01"),
        minimum_beta=Decimal("0.1"),
        maximum_beta=Decimal("3"),
        required_trend_hits=3,
        maximum_first_reaction=Decimal("0.2"),
        minimum_catalyst_score=50,
        minimum_candidate_score=50,
        minimum_dte=7,
        maximum_dte=45,
        maximum_relative_spread=Decimal("0.25"),
        minimum_debit_width_fraction=Decimal("0.1"),
        maximum_debit_width_fraction=Decimal("0.8"),
        minimum_credit_width_fraction=Decimal("0.1"),
        maximum_position_loss=Decimal("1250"),
        maximum_equity_risk_fraction=Decimal("0.02"),
        maximum_lifetime_entries=3,
        maximum_lifetime_risk=Decimal("3000"),
        equity_floor=Decimal("90000"),
        maximum_quantity=2,
    )


def _plan(
    *, account: str = ACCOUNT, account_role: AccountRole = AccountRole.DEVELOPMENT
) -> OpportunityPlanSpec:
    return OpportunityPlanSpec(
        opportunity_key="ACME_EARNINGS",
        version=1,
        underlying="ACME",
        event_session=date(2026, 8, 28),
        pre_event_session=date(2026, 8, 27),
        reaction_session=date(2026, 8, 31),
        signal_session=date(2026, 8, 31),
        daily_start_session=date(2026, 6, 1),
        allowed_event_codes=("RESULTS",),
        evidence_window_start=FROZEN,
        evidence_window_end=CAPTURED + timedelta(days=1),
        policy=_policy(),
        request_contract=OpportunitySnapshotRequest(
            account_role=account_role,
            expected_account_fingerprint=account,
            underlying="ACME",
            benchmark="QQQ",
            decision_boundary=CAPTURED + timedelta(days=1),
            minimum_expiry=date(2026, 9, 8),
            maximum_expiry=date(2026, 9, 18),
            minimum_strike=Decimal("50"),
            maximum_strike=Decimal("150"),
        ),
        thesis_code="POST_EVENT_CONTINUATION",
        thesis_target_contract={"target_kind": "session_close"},
        exposure_limit_contract={"shape": "defined_risk_vertical"},
        invalidation_codes=("RELATIVE_STRENGTH_LOST",),
        frozen_at=FROZEN,
        account_role=account_role,
    )


def _account(account_id: UUID = ACCOUNT_ID) -> dict[str, object]:
    return {
        "id": str(account_id),
        "account_number": "SECRET-ACCOUNT-NUMBER",
        "status": "ACTIVE",
        "currency": "USD",
        "created_at": "2026-08-28T15:00:00Z",
        "equity": "100000.00",
        "cash": "100000.00",
        "last_equity": "100000.00",
        "portfolio_value": "100000.00",
        "buying_power": "200000.00",
        "options_buying_power": "100000.00",
        "options_approved_level": "3",
        "options_trading_level": "3",
        "pending_transfer_in": "0",
        "pending_transfer_out": "0",
        "trading_blocked": False,
        "transfers_blocked": False,
        "account_blocked": False,
        "trade_suspended_by_user": False,
        "api_secret": "DO-NOT-LEAK",
    }


def _activity(index: int, *, account_id: UUID = ACCOUNT_ID) -> dict[str, object]:
    return {
        "id": f"activity-{index:04d}",
        "account_id": str(account_id),
        "activity_type": "CSD" if index == 0 else "OPTRD",
        "transaction_time": (
            datetime(2026, 8, 28, 15, tzinfo=UTC) + timedelta(seconds=index)
        ).isoformat(),
        "net_amount": "100000" if index == 0 else "-12.5",
        "description": "initial funding" if index == 0 else "option fill",
        "private_token": "DO-NOT-LEAK",
    }


class FakeProvider:
    def __init__(
        self,
        *,
        account: dict[str, object] | None = None,
        positions: list[dict[str, object]] | None = None,
        orders: list[dict[str, object]] | None = None,
        activities: list[dict[str, object]] | None = None,
    ) -> None:
        self.account = account or _account()
        self.positions = positions or []
        self.orders = orders or []
        self.activities = activities if activities is not None else [_activity(0)]
        self.calls: list[object] = []

    def get_account(self) -> object:
        self.calls.append("account")
        return self.account

    def get_all_positions(self) -> object:
        self.calls.append("positions")
        return self.positions

    def get_open_orders(self, *, limit: int) -> object:
        self.calls.append(("orders", limit))
        return self.orders

    def get_activity_page(self, **kwargs: object) -> ActivityPage:
        self.calls.append(("activity", kwargs.copy()))
        token = kwargs["page_token"]
        start = (
            0
            if token is None
            else next(
                index + 1 for index, item in enumerate(self.activities) if item["id"] == token
            )
        )
        size = int(kwargs["page_size"])
        return ActivityPage(tuple(self.activities[start : start + size]))

    def submit_order(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("write method called")


def _submission_capture(
    account: dict[str, object],
    *,
    positions: list[dict[str, object]] | None = None,
    orders: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return collect_opportunity_bootstrap(
        _plan(account_role=AccountRole.SUBMISSION),
        FakeProvider(account=account, positions=positions, orders=orders),
        account_role=AccountRole.SUBMISSION,
        submission_baseline_id=uuid4(),
        captured_at=CAPTURED,
    )


def test_collects_every_activity_page_and_emits_exact_bootstrap_input() -> None:
    provider = FakeProvider(activities=[_activity(index) for index in range(205)])

    payload = collect_development_opportunity_bootstrap(_plan(), provider, captured_at=CAPTURED)

    bootstrap = parse_development_opportunity_bootstrap(payload)
    preview = bootstrap_development_opportunity(bootstrap)
    assert preview.mode == "PREVIEW"
    assert len(bootstrap.baseline.activity_manifest) == 205
    assert [call[1]["page_token"] for call in provider.calls if call[0] == "activity"] == [
        None,
        "activity-0099",
        "activity-0199",
        None,
        "activity-0099",
        "activity-0199",
    ]
    assert provider.calls[:3] == ["account", "positions", ("orders", 500)]
    assert bootstrap.baseline.captured_at == CAPTURED
    assert bootstrap.baseline.positions_complete
    assert bootstrap.baseline.orders_complete
    assert bootstrap.baseline.activity_complete


def test_repeated_capture_has_stable_manifests_and_source_digests() -> None:
    first = collect_development_opportunity_bootstrap(
        _plan(), FakeProvider(activities=[_activity(0), _activity(1)]), captured_at=CAPTURED
    )
    second = collect_development_opportunity_bootstrap(
        _plan(), FakeProvider(activities=[_activity(0), _activity(1)]), captured_at=CAPTURED
    )

    assert first == second


@pytest.mark.parametrize("account_id", [uuid4(), UUID(int=0)])
def test_rejects_account_substitution(account_id: UUID) -> None:
    with pytest.raises(OpportunityBaselineCollectionError, match="DEVELOPMENT_ACCOUNT_MISMATCH"):
        collect_development_opportunity_bootstrap(
            _plan(), FakeProvider(account=_account(account_id)), captured_at=CAPTURED
        )


def test_rejects_activity_from_another_account() -> None:
    with pytest.raises(OpportunityBaselineCollectionError, match="DEVELOPMENT_ACCOUNT_MISMATCH"):
        collect_development_opportunity_bootstrap(
            _plan(),
            FakeProvider(activities=[_activity(0, account_id=uuid4())]),
            captured_at=CAPTURED,
        )


@pytest.mark.parametrize(
    ("positions", "orders"),
    [
        (
            [
                {
                    "asset_id": "asset-1",
                    "symbol": "ACME",
                    "asset_class": "us_option",
                    "side": "long",
                    "qty": "1",
                }
            ],
            [],
        ),
        ([], [{"id": "order-1", "status": "new"}]),
    ],
)
def test_rejects_non_clean_book_after_collecting_history(
    positions: list[dict[str, object]], orders: list[dict[str, object]]
) -> None:
    provider = FakeProvider(positions=positions, orders=orders)

    with pytest.raises(
        OpportunityBaselineCollectionError, match="OPPORTUNITY_BASELINE_BOOK_NOT_CLEAN"
    ):
        collect_development_opportunity_bootstrap(_plan(), provider, captured_at=CAPTURED)

    assert any(call[0] == "activity" for call in provider.calls if isinstance(call, tuple))


def test_rejects_nonterminal_activity_pagination() -> None:
    activities = [_activity(index) for index in range(10_000)]

    with pytest.raises(
        OpportunityBaselineCollectionError, match="OPPORTUNITY_BASELINE_ACTIVITY_INCOMPLETE"
    ):
        collect_development_opportunity_bootstrap(
            _plan(), FakeProvider(activities=activities), captured_at=CAPTURED
        )


def test_rejects_nonmonotonic_activity_history() -> None:
    activities = [_activity(1), _activity(0)]

    with pytest.raises(
        OpportunityBaselineCollectionError,
        match="OPPORTUNITY_BASELINE_ACTIVITY_NONMONOTONIC",
    ):
        collect_development_opportunity_bootstrap(
            _plan(), FakeProvider(activities=activities), captured_at=CAPTURED
        )


def test_accepts_date_only_activity_after_exact_activity_on_the_same_day() -> None:
    exact = _activity(0)
    date_only = _activity(1)
    date_only.pop("transaction_time")
    date_only["date"] = "2026-08-28"

    payload = collect_development_opportunity_bootstrap(
        _plan(), FakeProvider(activities=[exact, date_only]), captured_at=CAPTURED
    )

    assert len(parse_development_opportunity_bootstrap(payload).baseline.activity_manifest) == 2


def test_rejects_activity_before_account_creation() -> None:
    activity = _activity(0)
    activity["transaction_time"] = "2026-08-27T15:00:00Z"

    with pytest.raises(
        OpportunityBaselineCollectionError,
        match="OPPORTUNITY_BASELINE_CHRONOLOGY_INVALID",
    ):
        collect_development_opportunity_bootstrap(
            _plan(), FakeProvider(activities=[activity]), captured_at=CAPTURED
        )


def test_rejects_non_string_activity_pagination_identity() -> None:
    activity = _activity(0)
    activity["id"] = 1

    with pytest.raises(
        OpportunityBaselineCollectionError,
        match="OPPORTUNITY_BASELINE_ACTIVITY_INVALID",
    ):
        collect_development_opportunity_bootstrap(
            _plan(), FakeProvider(activities=[activity]), captured_at=CAPTURED
        )


def test_rejects_conflicting_activity_date_and_exact_time() -> None:
    activity = _activity(0)
    activity["date"] = "2026-08-29"

    with pytest.raises(
        OpportunityBaselineCollectionError,
        match="OPPORTUNITY_BASELINE_CHRONOLOGY_INVALID",
    ):
        collect_development_opportunity_bootstrap(
            _plan(), FakeProvider(activities=[activity]), captured_at=CAPTURED
        )


def test_rejects_activity_history_change_between_sweeps() -> None:
    class ChangingHistoryProvider(FakeProvider):
        def get_activity_page(self, **kwargs: object) -> ActivityPage:
            completed_sweeps = sum(
                1
                for call in self.calls
                if isinstance(call, tuple)
                and call[0] == "activity"
                and call[1]["page_token"] is None
            )
            if completed_sweeps:
                self.activities = [_activity(0), _activity(1)]
            return super().get_activity_page(**kwargs)

    with pytest.raises(
        OpportunityBaselineCollectionError,
        match="OPPORTUNITY_BASELINE_HISTORY_CHANGED",
    ):
        collect_development_opportunity_bootstrap(
            _plan(), ChangingHistoryProvider(), captured_at=CAPTURED
        )


def test_rejects_book_change_during_capture() -> None:
    class ChangingProvider(FakeProvider):
        def get_all_positions(self) -> object:
            self.calls.append("positions")
            if self.calls.count("positions") == 1:
                return []
            return [
                {
                    "asset_id": "asset-1",
                    "symbol": "ACME",
                    "asset_class": "us_option",
                    "side": "long",
                    "qty": "1",
                }
            ]

    with pytest.raises(
        OpportunityBaselineCollectionError, match="OPPORTUNITY_BASELINE_BOOK_CHANGED"
    ):
        collect_development_opportunity_bootstrap(_plan(), ChangingProvider(), captured_at=CAPTURED)


def test_output_removes_raw_account_identity_and_unknown_secret_fields() -> None:
    payload = collect_development_opportunity_bootstrap(
        _plan(), FakeProvider(), captured_at=CAPTURED
    )
    encoded = str(payload)

    assert str(ACCOUNT_ID) not in encoded
    assert "SECRET-ACCOUNT-NUMBER" not in encoded
    assert "DO-NOT-LEAK" not in encoded


def test_submission_capture_binds_exact_role_and_clean_100k_baseline() -> None:
    submission_baseline_id = uuid4()
    payload = collect_opportunity_bootstrap(
        _plan(account_role=AccountRole.SUBMISSION),
        FakeProvider(),
        account_role=AccountRole.SUBMISSION,
        submission_baseline_id=submission_baseline_id,
        captured_at=CAPTURED,
    )

    bootstrap = parse_opportunity_bootstrap(payload, account_role=AccountRole.SUBMISSION)

    assert bootstrap.plan.account_role is AccountRole.SUBMISSION
    assert bootstrap.baseline.account_role is AccountRole.SUBMISSION
    assert bootstrap.baseline.submission_baseline_id == submission_baseline_id


def test_submission_capture_accepts_absent_pending_transfers_reported_as_none() -> None:
    account = _account()
    account["pending_transfer_in"] = None
    account["pending_transfer_out"] = None

    payload = _submission_capture(account)

    bootstrap = parse_opportunity_bootstrap(payload, account_role=AccountRole.SUBMISSION)
    assert bootstrap.baseline.account_role is AccountRole.SUBMISSION


def test_submission_capture_can_bind_an_open_book_to_the_latest_reconciliation_state() -> None:
    account = _account()
    account["cash"] = "99825"
    positions = [
        {
            "asset_id": "long-leg",
            "symbol": "SPY261009C00777000",
            "asset_class": "us_option",
            "side": "long",
            "qty": "1",
        },
        {
            "asset_id": "short-leg",
            "symbol": "SPY261009C00781000",
            "asset_class": "us_option",
            "side": "short",
            "qty": "1",
        },
    ]
    payload = collect_opportunity_bootstrap(
        _plan(account_role=AccountRole.SUBMISSION),
        FakeProvider(account=account, positions=positions),
        account_role=AccountRole.SUBMISSION,
        submission_baseline_id=uuid4(),
        submission_reconciliation=SubmissionReconciliationBinding(
            account_fingerprint=ACCOUNT,
            expected_cash=Decimal("99825"),
            expected_positions=(
                ("SPY261009C00777000", Decimal("1")),
                ("SPY261009C00781000", Decimal("-1")),
            ),
        ),
        captured_at=CAPTURED,
    )

    assert parse_opportunity_bootstrap(payload, account_role=AccountRole.SUBMISSION)


@pytest.mark.parametrize("value", ["1", "-1"])
def test_submission_capture_rejects_nonzero_pending_transfers(value: str) -> None:
    account = _account()
    account["pending_transfer_in"] = None
    account["pending_transfer_out"] = value

    with pytest.raises(
        OpportunityBaselineCollectionError, match="CLEAN_SUBMISSION_BASELINE_REQUIRED"
    ):
        _submission_capture(account)


@pytest.mark.parametrize("value", ["", "not-a-number", True])
def test_submission_capture_rejects_malformed_pending_transfers(value: object) -> None:
    account = _account()
    account["pending_transfer_in"] = value
    account["pending_transfer_out"] = None

    with pytest.raises(
        OpportunityBaselineCollectionError,
        match="OPPORTUNITY_BASELINE_PROVIDER_RECORD_INVALID",
    ):
        _submission_capture(account)


def test_submission_capture_rejects_missing_pending_transfer_field() -> None:
    account = _account()
    del account["pending_transfer_in"]
    account["pending_transfer_out"] = None

    with pytest.raises(
        OpportunityBaselineCollectionError, match="CLEAN_SUBMISSION_BASELINE_REQUIRED"
    ):
        _submission_capture(account)


@pytest.mark.parametrize("field", ["equity", "cash"])
def test_null_pending_transfers_do_not_weaken_submission_balance_checks(field: str) -> None:
    account = _account()
    account["pending_transfer_in"] = None
    account["pending_transfer_out"] = None
    account[field] = "99999.99"

    with pytest.raises(
        OpportunityBaselineCollectionError, match="CLEAN_SUBMISSION_BASELINE_REQUIRED"
    ):
        _submission_capture(account)


@pytest.mark.parametrize(
    ("positions", "orders"),
    [
        (
            [
                {
                    "asset_id": "asset-1",
                    "symbol": "ACME",
                    "asset_class": "us_option",
                    "side": "long",
                    "qty": "1",
                }
            ],
            [],
        ),
        ([], [{"id": "order-1", "status": "new"}]),
    ],
)
def test_null_pending_transfers_do_not_weaken_submission_book_checks(
    positions: list[dict[str, object]], orders: list[dict[str, object]]
) -> None:
    account = _account()
    account["pending_transfer_in"] = None
    account["pending_transfer_out"] = None

    with pytest.raises(
        OpportunityBaselineCollectionError, match="OPPORTUNITY_BASELINE_BOOK_NOT_CLEAN"
    ):
        _submission_capture(account, positions=positions, orders=orders)


def test_submission_capture_rejects_non_100k_account_and_cross_role_substitution() -> None:
    account = _account()
    account["equity"] = "99999.99"
    with pytest.raises(
        OpportunityBaselineCollectionError, match="CLEAN_SUBMISSION_BASELINE_REQUIRED"
    ):
        collect_opportunity_bootstrap(
            _plan(account_role=AccountRole.SUBMISSION),
            FakeProvider(account=account),
            account_role=AccountRole.SUBMISSION,
            submission_baseline_id=uuid4(),
            captured_at=CAPTURED,
        )

    provider = FakeProvider()
    with pytest.raises(OpportunityBaselineCollectionError, match="AUTHORITY_INVALID"):
        collect_opportunity_bootstrap(
            _plan(account_role=AccountRole.SUBMISSION),
            provider,
            account_role=AccountRole.DEVELOPMENT,
            captured_at=CAPTURED,
        )
    assert provider.calls == []

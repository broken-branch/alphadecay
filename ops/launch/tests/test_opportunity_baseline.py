from __future__ import annotations

import json
import os
import stat
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from backend.app.alpaca.execution_evidence import baseline_account_fingerprint
from backend.app.alpaca.opportunity import OpportunitySnapshotRequest
from backend.app.contracts.v1 import AccountRole
from backend.app.persistence.opportunity_evidence import (
    OpportunityBaselineSeal,
    OpportunityPlanSpec,
    opportunity_baseline_identity,
    opportunity_plan_identity,
)
from backend.app.policy.opportunity import OpportunityPolicy
from backend.app.services.opportunity_bootstrap import (
    OpportunityBootstrapInput,
    opportunity_bootstrap_payload,
    parse_development_opportunity_bootstrap,
    parse_opportunity_bootstrap,
)
from ops.launch.opportunity_baseline import main

ACCOUNT_ID = UUID("11111111-1111-1111-1111-111111111111")
ACCOUNT = baseline_account_fingerprint(ACCOUNT_ID)
FROZEN = datetime(2026, 8, 30, 14, tzinfo=UTC)
CAPTURED = datetime(2026, 8, 30, 15, tzinfo=UTC)


def _plan(account_role: AccountRole = AccountRole.DEVELOPMENT) -> OpportunityPlanSpec:
    policy = OpportunityPolicy(
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
        policy=policy,
        request_contract=OpportunitySnapshotRequest(
            account_role=account_role,
            expected_account_fingerprint=ACCOUNT,
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


def _plan_payload(account_role: AccountRole = AccountRole.DEVELOPMENT) -> dict[str, object]:
    plan = _plan(account_role)
    plan_id, _ = opportunity_plan_identity(plan)
    seal = OpportunityBaselineSeal(
        plan_id=plan_id,
        account_fingerprint=ACCOUNT,
        account_source_hash="1" * 64,
        positions_manifest=(),
        positions_source_hash="2" * 64,
        orders_manifest=(),
        orders_source_hash="3" * 64,
        activity_manifest=(),
        activity_source_hash="4" * 64,
        book_hash="5" * 64,
        history_hash="6" * 64,
        captured_at=CAPTURED,
        account_role=account_role,
        submission_baseline_id=(
            UUID("22222222-2222-2222-2222-222222222222")
            if account_role is AccountRole.SUBMISSION
            else None
        ),
    )
    payload = opportunity_bootstrap_payload(OpportunityBootstrapInput(plan, seal))
    result = payload["plan"]
    assert isinstance(result, dict)
    return result


class FakeTrading:
    def __init__(self) -> None:
        self.closed = False
        self.calls: list[str] = []

    def get_account(self) -> object:
        self.calls.append("get_account")
        return {
            "id": str(ACCOUNT_ID),
            "account_number": "PRIVATE-ACCOUNT-NUMBER",
            "status": "ACTIVE",
            "currency": "USD",
            "created_at": "2026-08-28T15:00:00Z",
            "equity": "100000",
            "cash": "100000",
            "last_equity": "100000",
            "portfolio_value": "100000",
            "buying_power": "200000",
            "options_buying_power": "100000",
            "options_approved_level": "3",
            "options_trading_level": "3",
            "pending_transfer_in": None,
            "pending_transfer_out": None,
            "trading_blocked": False,
            "transfers_blocked": False,
            "account_blocked": False,
            "trade_suspended_by_user": False,
        }

    def get_all_positions(self) -> object:
        self.calls.append("get_all_positions")
        return []

    def get_orders(self, _filter: object = None) -> object:
        self.calls.append("get_orders")
        return []

    def close(self) -> None:
        self.closed = True


class FakeResponse:
    status_code = 200
    history: tuple[object, ...] = ()

    def json(self) -> object:
        return [
            {
                "id": "activity-0001",
                "account_id": str(ACCOUNT_ID),
                "activity_type": "CSD",
                "transaction_time": "2026-08-28T15:00:00Z",
                "net_amount": "100000",
            }
        ]


class FakeHttp:
    def __init__(self) -> None:
        self.closed = False
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> object:
        self.calls.append((url, kwargs))
        return FakeResponse()

    def close(self) -> None:
        self.closed = True


def _private_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def test_launcher_uses_explicit_files_and_writes_only_private_bootstrap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_file = tmp_path / "plan.json"
    credentials_file = tmp_path / "credentials.json"
    output = tmp_path / "baseline.json"
    _private_json(plan_file, _plan_payload())
    _private_json(
        credentials_file,
        {"api_key": "EXPLICIT-KEY", "secret_key": "EXPLICIT-SECRET"},
    )
    monkeypatch.setenv("ALPACA_API_KEY", "ENV-KEY-MUST-NOT-BE-USED")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "ENV-SECRET-MUST-NOT-BE-USED")
    trading = FakeTrading()
    activity_http = FakeHttp()
    factory_kwargs: dict[str, object] = {}
    clock_calls = 0

    def trading_factory(**kwargs: object) -> FakeTrading:
        factory_kwargs.update(kwargs)
        return trading

    def clock() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        return CAPTURED

    assert (
        main(
            [
                "--plan-file",
                str(plan_file),
                "--credentials-file",
                str(credentials_file),
                "--output",
                str(output),
            ],
            trading_factory=trading_factory,
            http_factory=lambda **_kwargs: activity_http,
            clock=clock,
        )
        == 0
    )

    bootstrap = parse_development_opportunity_bootstrap(
        json.loads(output.read_text(encoding="utf-8"))
    )
    assert bootstrap.baseline.captured_at == CAPTURED
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert factory_kwargs["api_key"] == "EXPLICIT-KEY"
    assert factory_kwargs["secret_key"] == "EXPLICIT-SECRET"
    assert factory_kwargs["paper"] is True
    assert factory_kwargs["url_override"] == "https://paper-api.alpaca.markets"
    assert clock_calls == 1
    assert trading.calls == [
        "get_account",
        "get_all_positions",
        "get_orders",
        "get_orders",
        "get_all_positions",
        "get_account",
    ]
    assert trading.closed and activity_http.closed
    assert len(activity_http.calls) == 2
    url, request = activity_http.calls[0]
    assert url == "https://paper-api.alpaca.markets/v2/account/activities"
    assert request["follow_redirects"] is False
    assert request["params"]["direction"] == "asc"
    console = capsys.readouterr().out
    summary = json.loads(console)
    expected_baseline_id, _ = opportunity_baseline_identity(bootstrap.baseline)
    assert summary == {
        "account_role": "DEVELOPMENT",
        "baseline_id": str(expected_baseline_id),
        "mode": "READ_ONLY_CAPTURE",
        "output_written": True,
        "plan_id": str(bootstrap.baseline.plan_id),
    }
    artifact = output.read_text(encoding="utf-8")
    for sensitive in (
        "EXPLICIT-KEY",
        "EXPLICIT-SECRET",
        "ENV-KEY-MUST-NOT-BE-USED",
        "ENV-SECRET-MUST-NOT-BE-USED",
        str(ACCOUNT_ID),
        "PRIVATE-ACCOUNT-NUMBER",
    ):
        assert sensitive not in console
        assert sensitive not in artifact


def test_launcher_rejects_nonprivate_input_before_creating_clients(tmp_path: Path) -> None:
    plan_file = tmp_path / "plan.json"
    credentials_file = tmp_path / "credentials.json"
    output = tmp_path / "baseline.json"
    _private_json(plan_file, _plan_payload())
    credentials_file.write_text(
        json.dumps({"api_key": "KEY", "secret_key": "SECRET"}), encoding="utf-8"
    )
    credentials_file.chmod(0o644)
    calls = 0

    def trading_factory(**_kwargs: object) -> FakeTrading:
        nonlocal calls
        calls += 1
        return FakeTrading()

    with pytest.raises(SystemExit):
        main(
            [
                "--role",
                "DEVELOPMENT",
                "--plan-file",
                str(plan_file),
                "--credentials-file",
                str(credentials_file),
                "--output",
                str(output),
            ],
            trading_factory=trading_factory,
        )

    assert calls == 0
    assert not output.exists()


def test_launcher_refuses_to_follow_private_input_symlink(tmp_path: Path) -> None:
    plan_target = tmp_path / "plan-target.json"
    plan_file = tmp_path / "plan.json"
    credentials_file = tmp_path / "credentials.json"
    output = tmp_path / "baseline.json"
    _private_json(plan_target, _plan_payload())
    os.symlink(plan_target, plan_file)
    _private_json(credentials_file, {"api_key": "KEY", "secret_key": "SECRET"})

    with pytest.raises(SystemExit):
        main(
            [
                "--role",
                "DEVELOPMENT",
                "--plan-file",
                str(plan_file),
                "--credentials-file",
                str(credentials_file),
                "--output",
                str(output),
            ],
            trading_factory=lambda **_kwargs: pytest.fail("client created"),
        )

    assert not output.exists()


def test_launcher_refuses_to_overwrite_or_follow_output(tmp_path: Path) -> None:
    plan_file = tmp_path / "plan.json"
    credentials_file = tmp_path / "credentials.json"
    output = tmp_path / "baseline.json"
    target = tmp_path / "target.json"
    _private_json(plan_file, _plan_payload())
    _private_json(credentials_file, {"api_key": "KEY", "secret_key": "SECRET"})
    target.write_text("preserve", encoding="utf-8")
    os.symlink(target, output)

    with pytest.raises(SystemExit):
        main(
            [
                "--role",
                "DEVELOPMENT",
                "--plan-file",
                str(plan_file),
                "--credentials-file",
                str(credentials_file),
                "--output",
                str(output),
            ],
            trading_factory=lambda **_kwargs: FakeTrading(),
            http_factory=lambda **_kwargs: FakeHttp(),
            clock=lambda: CAPTURED,
        )

    assert target.read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize("api_key", ["KEY\nINJECTED", "KEY\x00INJECTED", "KÉY"])
def test_launcher_rejects_non_ascii_or_control_credentials(tmp_path: Path, api_key: str) -> None:
    plan_file = tmp_path / "plan.json"
    credentials_file = tmp_path / "credentials.json"
    output = tmp_path / "baseline.json"
    _private_json(plan_file, _plan_payload())
    _private_json(credentials_file, {"api_key": api_key, "secret_key": "SECRET"})

    with pytest.raises(SystemExit):
        main(
            [
                "--role",
                "DEVELOPMENT",
                "--plan-file",
                str(plan_file),
                "--credentials-file",
                str(credentials_file),
                "--output",
                str(output),
            ],
            trading_factory=lambda **_kwargs: pytest.fail("client created"),
        )

    assert not output.exists()


def test_submission_launcher_binds_role_baseline_and_sanitizes_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan_file = tmp_path / "submission-plan.json"
    credentials_file = tmp_path / "credentials.json"
    output = tmp_path / "submission-baseline.json"
    submission_baseline_id = UUID("22222222-2222-2222-2222-222222222222")
    _private_json(plan_file, _plan_payload(AccountRole.SUBMISSION))
    _private_json(credentials_file, {"api_key": "KEY", "secret_key": "SECRET"})

    assert (
        main(
            [
                "--role",
                "SUBMISSION",
                "--submission-baseline-id",
                str(submission_baseline_id),
                "--plan-file",
                str(plan_file),
                "--credentials-file",
                str(credentials_file),
                "--output",
                str(output),
            ],
            trading_factory=lambda **_kwargs: FakeTrading(),
            http_factory=lambda **_kwargs: FakeHttp(),
            clock=lambda: CAPTURED,
        )
        == 0
    )

    bootstrap = parse_opportunity_bootstrap(
        json.loads(output.read_text(encoding="utf-8")),
        account_role=AccountRole.SUBMISSION,
    )
    console = capsys.readouterr().out
    assert bootstrap.baseline.submission_baseline_id == submission_baseline_id
    assert json.loads(console)["account_role"] == "SUBMISSION"
    assert ACCOUNT not in console
    assert str(ACCOUNT_ID) not in console
    assert "KEY" not in console
    assert "SECRET" not in console


def test_launcher_rejects_cross_role_plan_before_clients(tmp_path: Path) -> None:
    plan_file = tmp_path / "submission-plan.json"
    credentials_file = tmp_path / "credentials.json"
    output = tmp_path / "baseline.json"
    _private_json(plan_file, _plan_payload(AccountRole.SUBMISSION))
    _private_json(credentials_file, {"api_key": "KEY", "secret_key": "SECRET"})

    with pytest.raises(SystemExit):
        main(
            [
                "--role",
                "DEVELOPMENT",
                "--plan-file",
                str(plan_file),
                "--credentials-file",
                str(credentials_file),
                "--output",
                str(output),
            ],
            trading_factory=lambda **_kwargs: pytest.fail("client created"),
        )

    assert not output.exists()

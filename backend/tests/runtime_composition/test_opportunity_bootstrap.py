from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.alpaca.opportunity import OpportunitySnapshotRequest
from backend.app.contracts.v1 import AccountRole
from backend.app.persistence.opportunity_evidence import (
    OpportunityBaselineSeal,
    OpportunityEvidenceError,
    OpportunityPlanSpec,
    SQLAlchemyOpportunityEvidenceRepository,
    _json_value,
    _request_material,
    opportunity_baseline_identity,
    opportunity_plan_identity,
)
from backend.app.persistence.sqlalchemy_models import (
    AccountRoleRow,
    Base,
    DevelopmentOpportunityBaselineRow,
    DevelopmentOpportunityPlanRow,
    SubmissionBaselineRow,
)
from backend.app.policy.opportunity import OpportunityPolicy
from backend.app.services.opportunity_bootstrap import (
    OpportunityBootstrapError,
    OpportunityBootstrapInput,
    bootstrap_development_opportunity,
    bootstrap_opportunity,
    opportunity_bootstrap_payload,
    parse_development_opportunity_bootstrap,
    parse_opportunity_bootstrap,
)
from ops.launch.opportunity_bootstrap import main

ACCOUNT = "a" * 64
NOW = datetime(2026, 8, 30, 15, tzinfo=UTC)


def _policy() -> OpportunityPolicy:
    return OpportunityPolicy(
        version="test-v1",
        opportunity_key="ACME_EARNINGS",
        underlying="ACME",
        selected_decision_boundary=NOW + timedelta(days=1, hours=2),
        last_entry_boundary=NOW + timedelta(days=1, hours=3),
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
        maximum_first_reaction=Decimal("0.20"),
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


def _plan() -> OpportunityPlanSpec:
    return OpportunityPlanSpec(
        opportunity_key="ACME_EARNINGS",
        version=1,
        underlying="ACME",
        event_session=date(2026, 8, 28),
        pre_event_session=date(2026, 8, 27),
        reaction_session=date(2026, 8, 31),
        signal_session=date(2026, 8, 31),
        daily_start_session=date(2026, 6, 1),
        allowed_event_codes=("RESULTS", "GUIDANCE"),
        evidence_window_start=NOW,
        evidence_window_end=NOW + timedelta(days=1),
        policy=_policy(),
        request_contract=OpportunitySnapshotRequest(
            account_role=AccountRole.DEVELOPMENT,
            expected_account_fingerprint=ACCOUNT,
            underlying="ACME",
            benchmark="QQQ",
            decision_boundary=NOW + timedelta(days=1, hours=2),
            minimum_expiry=date(2026, 9, 8),
            maximum_expiry=date(2026, 9, 18),
            minimum_strike=Decimal("50"),
            maximum_strike=Decimal("150"),
        ),
        thesis_code="POST_EVENT_CONTINUATION",
        thesis_target_contract={"target_kind": "session_close"},
        exposure_limit_contract={"shape": "defined_risk_vertical"},
        invalidation_codes=("RELATIVE_STRENGTH_LOST", "CATALYST_CONTRADICTED"),
        frozen_at=NOW,
    )


def _baseline(plan: OpportunityPlanSpec) -> OpportunityBaselineSeal:
    plan_id, _ = opportunity_plan_identity(plan)
    return OpportunityBaselineSeal(
        plan_id=plan_id,
        account_fingerprint=ACCOUNT,
        account_source_hash="1" * 64,
        positions_manifest=(),
        positions_source_hash="2" * 64,
        orders_manifest=(),
        orders_source_hash="3" * 64,
        activity_manifest=({"activity_id": "prior-1"},),
        activity_source_hash="4" * 64,
        book_hash="5" * 64,
        history_hash="6" * 64,
        captured_at=NOW + timedelta(minutes=1),
    )


def _payload(plan: OpportunityPlanSpec, baseline: OpportunityBaselineSeal) -> dict[str, object]:
    policy = _json_value(plan.policy)
    assert isinstance(policy, dict)
    return {
        "account_role": "DEVELOPMENT",
        "submission_baseline_id": None,
        "plan": {
            "opportunity_key": plan.opportunity_key,
            "version": plan.version,
            "underlying": plan.underlying,
            "event_session": plan.event_session.isoformat(),
            "pre_event_session": plan.pre_event_session.isoformat(),
            "reaction_session": plan.reaction_session.isoformat(),
            "signal_session": plan.signal_session.isoformat(),
            "daily_start_session": plan.daily_start_session.isoformat(),
            "allowed_event_codes": list(plan.allowed_event_codes),
            "evidence_window_start": plan.evidence_window_start.isoformat(),
            "evidence_window_end": plan.evidence_window_end.isoformat(),
            "policy": policy,
            "request_contract": _request_material(plan.request_contract),
            "thesis_code": plan.thesis_code,
            "thesis_target_contract": plan.thesis_target_contract,
            "exposure_limit_contract": plan.exposure_limit_contract,
            "invalidation_codes": list(plan.invalidation_codes),
            "frozen_at": plan.frozen_at.isoformat(),
        },
        "baseline": {
            "plan_id": str(baseline.plan_id),
            "account_fingerprint": baseline.account_fingerprint,
            "account_source_hash": baseline.account_source_hash,
            "positions_manifest": list(baseline.positions_manifest),
            "positions_source_hash": baseline.positions_source_hash,
            "positions_complete": baseline.positions_complete,
            "orders_manifest": list(baseline.orders_manifest),
            "orders_source_hash": baseline.orders_source_hash,
            "orders_complete": baseline.orders_complete,
            "activity_manifest": list(baseline.activity_manifest),
            "activity_source_hash": baseline.activity_source_hash,
            "activity_complete": baseline.activity_complete,
            "book_hash": baseline.book_hash,
            "history_hash": baseline.history_hash,
            "captured_at": baseline.captured_at.isoformat(),
        },
    }


def _repository(*roles: AccountRole):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    if not roles:
        roles = (AccountRole.DEVELOPMENT,)
    with sessions.begin() as session:
        session.add_all(
            [
                AccountRoleRow(
                    role=role.value,
                    account_fingerprint=ACCOUNT,
                    equity=Decimal("100000"),
                    autonomous_enabled=False,
                )
                for role in roles
            ]
        )
    return SQLAlchemyOpportunityEvidenceRepository(sessions), sessions, engine


def _submission_bootstrap() -> OpportunityBootstrapInput:
    development_plan = _plan()
    plan = replace(
        development_plan,
        request_contract=replace(
            development_plan.request_contract,
            account_role=AccountRole.SUBMISSION,
        ),
        account_role=AccountRole.SUBMISSION,
    )
    baseline = replace(
        _baseline(plan),
        account_role=AccountRole.SUBMISSION,
        submission_baseline_id=uuid4(),
    )
    return OpportunityBootstrapInput(plan, baseline)


def test_preview_validates_authority_without_repository_writes() -> None:
    plan = _plan()
    bootstrap = parse_development_opportunity_bootstrap(_payload(plan, _baseline(plan)))

    result = bootstrap_development_opportunity(bootstrap)

    assert result.mode == "PREVIEW"
    assert result.plan_id == bootstrap.baseline.plan_id
    assert set(result.sanitized_payload()) == {
        "mode",
        "plan_id",
        "plan_hash",
        "baseline_id",
        "baseline_hash",
    }


def test_persist_freezes_then_replays_and_reloads_exact_authority() -> None:
    plan = _plan()
    bootstrap = OpportunityBootstrapInput(plan, _baseline(plan))
    repository, sessions, engine = _repository()

    first = bootstrap_development_opportunity(bootstrap, persist=True, repository=repository)
    replay = bootstrap_development_opportunity(bootstrap, persist=True, repository=repository)

    assert first == replay
    assert first.mode == "PERSISTED"
    with sessions() as session:
        assert len(session.scalars(select(DevelopmentOpportunityPlanRow)).all()) == 1
        assert len(session.scalars(select(DevelopmentOpportunityBaselineRow)).all()) == 1
    engine.dispose()


def test_submission_persist_replays_with_submission_authority() -> None:
    bootstrap = _submission_bootstrap()
    repository, sessions, engine = _repository(AccountRole.SUBMISSION)
    with sessions.begin() as session:
        session.add(
            SubmissionBaselineRow(
                baseline_id=bootstrap.baseline.submission_baseline_id,
                account_role=AccountRole.SUBMISSION.value,
                account_fingerprint=ACCOUNT,
                equity=Decimal("100000"),
                captured_at=NOW,
                positions_hash="7" * 64,
                orders_hash="8" * 64,
                activities_hash="9" * 64,
                contaminated=False,
            )
        )

    first = bootstrap_opportunity(
        bootstrap,
        account_role=AccountRole.SUBMISSION,
        persist=True,
        repository=repository,
    )
    replay = bootstrap_opportunity(
        bootstrap,
        account_role=AccountRole.SUBMISSION,
        persist=True,
        repository=repository,
    )

    assert first == replay
    assert first.mode == "PERSISTED"
    with sessions() as session:
        plan = session.scalar(select(DevelopmentOpportunityPlanRow))
        baseline = session.scalar(select(DevelopmentOpportunityBaselineRow))
        assert plan is not None and plan.account_role == AccountRole.SUBMISSION.value
        assert baseline is not None and baseline.account_role == AccountRole.SUBMISSION.value
    with pytest.raises(OpportunityEvidenceError, match="OPPORTUNITY_ACCOUNT_MISSING"):
        repository.load_plan(
            bootstrap.plan.opportunity_key,
            version=bootstrap.plan.version,
            account_role=AccountRole.DEVELOPMENT,
        )
    engine.dispose()


def test_persist_rejects_repository_replay_mismatch() -> None:
    plan = _plan()
    bootstrap = OpportunityBootstrapInput(plan, _baseline(plan))
    repository, _, engine = _repository()

    class MismatchedReplayRepository:
        def __init__(self) -> None:
            self.plan_writes = 0

        def freeze_plan(self, spec):
            self.plan_writes += 1
            persisted = repository.freeze_plan(spec)
            if self.plan_writes == 2:
                return replace(persisted, plan_hash="f" * 64)
            return persisted

        def seal_baseline(self, seal):
            return repository.seal_baseline(seal)

        def load_plan(
            self,
            opportunity_key,
            *,
            version=None,
            account_role=AccountRole.DEVELOPMENT,
        ):
            return repository.load_plan(
                opportunity_key,
                version=version,
                account_role=account_role,
            )

        def load_baseline(self, plan_id, *, account_role=AccountRole.DEVELOPMENT):
            return repository.load_baseline(plan_id, account_role=account_role)

    with pytest.raises(OpportunityBootstrapError, match="REPLAY_MISMATCH"):
        bootstrap_development_opportunity(
            bootstrap,
            persist=True,
            repository=MismatchedReplayRepository(),
        )
    engine.dispose()


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda plan, baseline: replace(baseline, plan_id=uuid4()), "AUTHORITY_MISMATCH"),
        (
            lambda plan, baseline: replace(baseline, account_fingerprint="b" * 64),
            "AUTHORITY_MISMATCH",
        ),
        (
            lambda plan, baseline: replace(baseline, positions_complete=False),
            "BASELINE_INCOMPLETE",
        ),
    ],
)
def test_rejects_mismatched_or_incomplete_baseline(change, code: str) -> None:
    plan = _plan()
    baseline = change(plan, _baseline(plan))

    with pytest.raises(OpportunityBootstrapError, match=code):
        bootstrap_development_opportunity(OpportunityBootstrapInput(plan, baseline))


def test_parser_rejects_non_development_role_and_unknown_fields() -> None:
    plan = _plan()
    payload = _payload(plan, _baseline(plan))
    payload["account_role"] = "SUBMISSION"
    with pytest.raises(OpportunityBootstrapError, match="AUTHORITY_MISMATCH"):
        parse_development_opportunity_bootstrap(payload)

    payload = _payload(plan, _baseline(plan))
    assert isinstance(payload["plan"], dict)
    payload["plan"]["candidate_override"] = "FORBIDDEN"
    with pytest.raises(OpportunityBootstrapError, match="PAYLOAD_INVALID"):
        parse_development_opportunity_bootstrap(payload)


def test_cli_defaults_to_preview_and_prints_only_sanitized_identity(tmp_path, capsys) -> None:
    plan = _plan()
    input_path = tmp_path / "private-opportunity.json"
    payload = _payload(plan, _baseline(plan))
    del payload["submission_baseline_id"]
    input_path.write_text(json.dumps(payload))

    assert main(["--input", str(input_path)]) == 0

    output = capsys.readouterr().out
    decoded = json.loads(output)
    assert decoded["mode"] == "PREVIEW"
    assert ACCOUNT not in output
    assert "ACME" not in output


def test_persist_requires_explicit_database_url_file(tmp_path) -> None:
    plan = _plan()
    input_path = tmp_path / "private-opportunity.json"
    input_path.write_text(json.dumps(_payload(plan, _baseline(plan))))

    with pytest.raises(SystemExit, match="2"):
        main(["--role", "DEVELOPMENT", "--input", str(input_path), "--persist"])


def test_persist_rejects_nonprivate_database_url_file(tmp_path) -> None:
    plan = _plan()
    input_path = tmp_path / "private-opportunity.json"
    database_url_path = tmp_path / "database-url.txt"
    input_path.write_text(json.dumps(_payload(plan, _baseline(plan))))
    database_url_path.write_text("postgresql://user:secret@localhost/alphadecay")
    database_url_path.chmod(0o644)

    with pytest.raises(SystemExit, match="2"):
        main(
            [
                "--input",
                str(input_path),
                "--persist",
                "--database-url-file",
                str(database_url_path),
            ]
        )


def test_submission_preview_requires_exact_role_and_clean_baseline_binding() -> None:
    plan = _plan()
    request = replace(plan.request_contract, account_role=AccountRole.SUBMISSION)
    plan = replace(plan, request_contract=request, account_role=AccountRole.SUBMISSION)
    submission_baseline_id = uuid4()
    baseline = replace(
        _baseline(plan),
        account_role=AccountRole.SUBMISSION,
        submission_baseline_id=submission_baseline_id,
    )
    payload = opportunity_bootstrap_payload(OpportunityBootstrapInput(plan, baseline))

    bootstrap = parse_opportunity_bootstrap(payload, account_role=AccountRole.SUBMISSION)
    result = bootstrap_opportunity(bootstrap, account_role=AccountRole.SUBMISSION)

    assert result.mode == "PREVIEW"
    assert bootstrap.baseline.submission_baseline_id == submission_baseline_id
    with pytest.raises(OpportunityBootstrapError, match="AUTHORITY_MISMATCH"):
        parse_opportunity_bootstrap(payload, account_role=AccountRole.DEVELOPMENT)
    with pytest.raises(OpportunityBootstrapError, match="ROLE_INVALID"):
        parse_opportunity_bootstrap(payload, account_role=AccountRole.REPLAY)


def test_submission_baseline_identity_binds_the_immutable_baseline_id() -> None:
    plan = _plan()
    request = replace(plan.request_contract, account_role=AccountRole.SUBMISSION)
    plan = replace(plan, request_contract=request, account_role=AccountRole.SUBMISSION)
    first = replace(
        _baseline(plan),
        account_role=AccountRole.SUBMISSION,
        submission_baseline_id=uuid4(),
    )
    second = replace(first, submission_baseline_id=uuid4())

    assert opportunity_baseline_identity(first) != opportunity_baseline_identity(second)


def test_submission_cli_preview_prints_no_account_authority(tmp_path, capsys) -> None:
    plan = _plan()
    request = replace(plan.request_contract, account_role=AccountRole.SUBMISSION)
    plan = replace(plan, request_contract=request, account_role=AccountRole.SUBMISSION)
    baseline = replace(
        _baseline(plan),
        account_role=AccountRole.SUBMISSION,
        submission_baseline_id=uuid4(),
    )
    input_path = tmp_path / "submission-opportunity.json"
    input_path.write_text(
        json.dumps(opportunity_bootstrap_payload(OpportunityBootstrapInput(plan, baseline)))
    )
    input_path.chmod(0o600)

    assert main(["--role", "SUBMISSION", "--input", str(input_path)]) == 0

    output = capsys.readouterr().out
    decoded = json.loads(output)
    assert decoded["mode"] == "PREVIEW"
    assert decoded["account_role"] == "SUBMISSION"
    assert ACCOUNT not in output
    assert "ACME" not in output


def test_submission_cli_rejects_nonprivate_bootstrap_input(tmp_path) -> None:
    plan = _plan()
    request = replace(plan.request_contract, account_role=AccountRole.SUBMISSION)
    plan = replace(plan, request_contract=request, account_role=AccountRole.SUBMISSION)
    baseline = replace(
        _baseline(plan),
        account_role=AccountRole.SUBMISSION,
        submission_baseline_id=uuid4(),
    )
    input_path = tmp_path / "submission-opportunity.json"
    input_path.write_text(
        json.dumps(opportunity_bootstrap_payload(OpportunityBootstrapInput(plan, baseline)))
    )
    input_path.chmod(0o644)

    with pytest.raises(SystemExit, match="2"):
        main(["--role", "SUBMISSION", "--input", str(input_path)])

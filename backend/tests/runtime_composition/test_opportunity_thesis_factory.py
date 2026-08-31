from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from backend.app.alpaca.opportunity import (
    OpportunityAccountBook,
    OpportunityBar,
    OpportunityMarketSession,
    OpportunityMarketSnapshot,
    OpportunityOption,
    OpportunitySnapshotRequest,
    opportunity_account_book_digest,
    opportunity_bar_digest,
    opportunity_market_session_digest,
    opportunity_market_snapshot_digest,
    opportunity_option_digest,
    opportunity_snapshot_request_digest,
)
from backend.app.contracts.v1 import (
    AccountResponse,
    AccountRole,
    DataQuality,
    PositionListResponse,
)
from backend.app.persistence.opportunity_evidence import (
    OpportunityBaselineSeal,
    OpportunityObservationSpec,
    OpportunityPlanSpec,
    PersistedOpportunityBaseline,
    PersistedOpportunityObservation,
    PersistedOpportunityPlan,
    SQLAlchemyOpportunityEvidenceRepository,
    opportunity_baseline_digest,
    opportunity_observation_digest,
    opportunity_plan_digest,
)
from backend.app.persistence.runtime import apply_migrations, discover_migrations
from backend.app.persistence.sqlalchemy_models import AccountRoleRow, Base, SubmissionBaselineRow
from backend.app.policy import (
    CatalystQuality,
    OpportunityPolicy,
    evaluate_opportunity,
)
from backend.app.policy.opportunity import (
    OpportunityDirection,
    TradingHaltState,
    opportunity_policy_hash,
)
from backend.app.services.opportunity_input import (
    AccountBudgetAuthority,
    CatalystAuthority,
    DecimalSignalAuthority,
    OpportunitySignalAuthority,
    PriorDecisionAuthority,
    TrendSignalAuthority,
    assemble_opportunity_input,
)
from backend.app.services.opportunity_selection import (
    CandidateSelectionAuthority,
    GreekUnitConvention,
    select_vertical_candidate,
)
from backend.app.services.opportunity_thesis import (
    OpportunityThesisError,
    OpportunityThesisFactoryInput,
    SQLAlchemyOpportunityThesisRepository,
    _canonical_hash,
    _database_thesis_material,
    _stable_uuid,
    build_frozen_opportunity_thesis,
    finalize_frozen_opportunity_thesis,
)

BOUNDARY = datetime(2026, 8, 31, 15, 30, tzinfo=UTC)
TRUSTED = BOUNDARY + timedelta(seconds=20)
ACCOUNT = "a" * 64
BOOK = "b" * 64
CALENDAR = "1" * 64
DAILY = "2" * 64
INTRADAY = "3" * 64
BUDGET = "4" * 64
GREEK = "5" * 64
MIGRATIONS = Path(__file__).parents[3] / "migrations"
POSTGRES_URL_ENV = "ALPHADECAY_TEST_POSTGRES_URL"


def _policy(opportunity_key: str = "ACME_EARNINGS") -> OpportunityPolicy:
    return OpportunityPolicy(
        version="test-v1",
        opportunity_key=opportunity_key,
        underlying="ACME",
        selected_decision_boundary=BOUNDARY,
        last_entry_boundary=BOUNDARY + timedelta(minutes=15),
        maximum_decision_delay=timedelta(minutes=2),
        maximum_underlying_age=timedelta(minutes=2),
        maximum_catalyst_age=timedelta(hours=1),
        maximum_option_quote_age=timedelta(seconds=30),
        maximum_leg_quote_skew=timedelta(seconds=5),
        minimum_vwap_distance=Decimal("0.0075"),
        maximum_vwap_distance=Decimal("0.10"),
        minimum_relative_return=Decimal("0.0075"),
        minimum_beta=Decimal("0.1"),
        maximum_beta=Decimal("3"),
        required_trend_hits=3,
        maximum_first_reaction=Decimal("0.20"),
        minimum_catalyst_score=50,
        minimum_candidate_score=10,
        minimum_dte=7,
        maximum_dte=45,
        maximum_relative_spread=Decimal("0.25"),
        minimum_debit_width_fraction=Decimal("0.10"),
        maximum_debit_width_fraction=Decimal("0.80"),
        minimum_credit_width_fraction=Decimal("0.10"),
        maximum_position_loss=Decimal("1250"),
        maximum_equity_risk_fraction=Decimal("0.02"),
        maximum_lifetime_entries=3,
        maximum_lifetime_risk=Decimal("3000"),
        equity_floor=Decimal("90000"),
        maximum_quantity=2,
    )


def _request(
    role: AccountRole = AccountRole.DEVELOPMENT,
    account_fingerprint: str = ACCOUNT,
) -> OpportunitySnapshotRequest:
    return OpportunitySnapshotRequest(
        account_role=role,
        expected_account_fingerprint=account_fingerprint,
        underlying="ACME",
        benchmark="QQQ",
        decision_boundary=BOUNDARY,
        minimum_expiry=date(2026, 9, 14),
        maximum_expiry=date(2026, 9, 30),
        minimum_strike=Decimal("90"),
        maximum_strike=Decimal("110"),
        maximum_contracts=16,
    )


def _bar(symbol: str, close: str) -> OpportunityBar:
    bar = OpportunityBar(
        symbol=symbol,
        started_at=BOUNDARY - timedelta(minutes=5),
        completed_at=BOUNDARY,
        open=Decimal(close) - 1,
        high=Decimal(close) + 1,
        low=Decimal(close) - 2,
        close=Decimal(close),
        volume=Decimal("1000"),
        vwap=Decimal(close) - Decimal("0.5"),
        source_hash="",
    )
    return replace(bar, source_hash=opportunity_bar_digest(bar))


def _option(
    symbol: str,
    *,
    right: str,
    strike: str,
    bid: str,
    ask: str,
    iv: str,
    delta: str,
    gamma: str,
    theta: str,
    vega: str,
) -> OpportunityOption:
    option = OpportunityOption(
        symbol=symbol,
        underlying="ACME",
        expiry=date(2026, 9, 18),
        right=right,
        strike=Decimal(strike),
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=10,
        ask_size=10,
        quote_at=TRUSTED - timedelta(seconds=3),
        retrieved_at=TRUSTED - timedelta(seconds=1),
        implied_volatility=Decimal(iv),
        delta=Decimal(delta),
        gamma=Decimal(gamma),
        theta_per_day=Decimal(theta),
        vega_per_iv_point=Decimal(vega),
        source_hash="",
    )
    return replace(option, source_hash=opportunity_option_digest(option))


def _snapshot(request: OpportunitySnapshotRequest) -> OpportunityMarketSnapshot:
    account_response = AccountResponse(
        role=request.account_role,
        paper=True,
        equity=Decimal("100000"),
        buying_power=Decimal("200000"),
        baseline_status=DataQuality.UNKNOWN,
        autonomous_enabled=False,
    )
    book = OpportunityAccountBook(
        account=account_response,
        positions=PositionListResponse(positions=()),
        open_orders=(),
        account_fingerprint=request.expected_account_fingerprint,
        source_hash="",
    )
    book = replace(book, source_hash=opportunity_account_book_digest(book))
    session = OpportunityMarketSession(
        session_date=date(2026, 8, 31),
        open_at=datetime(2026, 8, 31, 13, 30, tzinfo=UTC),
        close_at=datetime(2026, 8, 31, 20, 0, tzinfo=UTC),
        clock_at=TRUSTED - timedelta(seconds=2),
        market_open=True,
        next_open_at=datetime(2026, 9, 1, 13, 30, tzinfo=UTC),
        next_close_at=datetime(2026, 9, 1, 20, 0, tzinfo=UTC),
        source_hash="",
    )
    session = replace(session, source_hash=opportunity_market_session_digest(session))
    snapshot = OpportunityMarketSnapshot(
        trusted_at=TRUSTED,
        account_book=book,
        session=session,
        underlying_bar=_bar("ACME", "101"),
        benchmark_bar=_bar("QQQ", "501"),
        options=(
            _option(
                "ACME260918C00100000",
                right="C",
                strike="100",
                bid="2.08",
                ask="2.10",
                iv="0.40",
                delta="0.60",
                gamma="0.03",
                theta="-0.08",
                vega="0.20",
            ),
            _option(
                "ACME260918C00105000",
                right="C",
                strike="105",
                bid="0.93",
                ask="0.94",
                iv="0.80",
                delta="0.30",
                gamma="0.02",
                theta="-0.04",
                vega="0.10",
            ),
            _option(
                "ACME260918P00100000",
                right="P",
                strike="100",
                bid="1.50",
                ask="1.55",
                iv="0.50",
                delta="-0.40",
                gamma="0.03",
                theta="-0.07",
                vega="0.18",
            ),
        ),
        request_hash=opportunity_snapshot_request_digest(request),
        source_hash="",
    )
    return replace(snapshot, source_hash=opportunity_market_snapshot_digest(snapshot))


def _factory_input(
    opportunity_key: str = "ACME_EARNINGS",
    *,
    role: AccountRole = AccountRole.DEVELOPMENT,
    submission_baseline_id: UUID | None = None,
    account_fingerprint: str = ACCOUNT,
) -> OpportunityThesisFactoryInput:
    policy = _policy(opportunity_key)
    request = _request(role, account_fingerprint)
    snapshot = _snapshot(request)
    plan_spec = OpportunityPlanSpec(
        opportunity_key=policy.opportunity_key,
        version=1,
        underlying="ACME",
        event_session=date(2026, 8, 28),
        pre_event_session=date(2026, 8, 27),
        reaction_session=date(2026, 8, 31),
        signal_session=date(2026, 8, 31),
        daily_start_session=date(2026, 6, 1),
        allowed_event_codes=("RESULTS", "GUIDANCE"),
        evidence_window_start=BOUNDARY - timedelta(days=1),
        evidence_window_end=BOUNDARY,
        policy=policy,
        request_contract=request,
        thesis_code="POST_EVENT_CONTINUATION",
        thesis_target_contract={
            "target_at": "2026-09-04T19:45:00Z",
            "volatility_view": "NEUTRAL",
        },
        exposure_limit_contract={
            "delta_low": "0",
            "delta_high": "100",
            "vega_low": "0",
            "vega_high": "40",
            "maximum_daily_theta": "20",
            "minimum_dte": 7,
            "maximum_dte": 45,
            "portfolio_risk_cap": "1250",
        },
        invalidation_codes=("RELATIVE_STRENGTH_LOST", "CATALYST_CONTRADICTED"),
        frozen_at=BOUNDARY - timedelta(days=1),
        account_role=role,
    )
    plan_hash = opportunity_plan_digest(plan_spec)
    plan_id = uuid5(NAMESPACE_URL, f"alphadecay:opportunity-plan:{plan_hash}")
    policy_hash = opportunity_policy_hash(policy)
    plan = PersistedOpportunityPlan(
        plan_id=plan_id,
        opportunity_key=plan_spec.opportunity_key,
        version=1,
        policy_hash=policy_hash,
        request_contract_hash=opportunity_snapshot_request_digest(request),
        plan_hash=plan_hash,
        frozen_at=plan_spec.frozen_at,
        account_role=role,
    )
    baseline_seal = OpportunityBaselineSeal(
        plan_id=plan_id,
        account_fingerprint=account_fingerprint,
        account_source_hash="6" * 64,
        positions_manifest=(),
        positions_source_hash="7" * 64,
        orders_manifest=(),
        orders_source_hash="8" * 64,
        activity_manifest=(),
        activity_source_hash="9" * 64,
        book_hash=BOOK,
        history_hash="c" * 64,
        captured_at=BOUNDARY - timedelta(hours=1),
        account_role=role,
        submission_baseline_id=submission_baseline_id,
    )
    baseline_hash = opportunity_baseline_digest(baseline_seal)
    baseline_id = uuid5(NAMESPACE_URL, f"alphadecay:opportunity-baseline:{baseline_hash}")
    baseline = PersistedOpportunityBaseline(
        baseline_id=baseline_id,
        plan_id=plan_id,
        account_fingerprint=account_fingerprint,
        baseline_hash=baseline_hash,
        captured_at=baseline_seal.captured_at,
        account_role=role,
        submission_baseline_id=submission_baseline_id,
    )
    selection_authority = CandidateSelectionAuthority(
        snapshot_request_hash=snapshot.request_hash,
        snapshot_source_hash=snapshot.source_hash,
        account_fingerprint=account_fingerprint,
        observed_equity=Decimal("100000"),
        observed_buying_power=Decimal("200000"),
        available_risk=Decimal("1250"),
        available_buying_power=Decimal("200000"),
        greek_unit_convention=GreekUnitConvention.ALPACA_GOPRICEOPTIONS_RAW_V1,
        greek_unit_evidence_hash=GREEK,
    )
    signals = OpportunitySignalAuthority(
        snapshot_source_hash=snapshot.source_hash,
        calculation_source_hash="3" * 64,
        beta=DecimalSignalAuthority(Decimal("1.2"), BOUNDARY + timedelta(seconds=5), DAILY),
        vwap_distance=DecimalSignalAuthority(
            Decimal("0.02"), BOUNDARY + timedelta(seconds=5), INTRADAY
        ),
        relative_return=DecimalSignalAuthority(
            Decimal("0.015"), BOUNDARY + timedelta(seconds=5), INTRADAY
        ),
        trend=TrendSignalAuthority(3, 0, BOUNDARY + timedelta(seconds=5), INTRADAY),
        absolute_first_reaction=DecimalSignalAuthority(
            Decimal("0.03"), BOUNDARY + timedelta(seconds=5), DAILY
        ),
        trading_halt_state=TradingHaltState.NOT_HALTED,
        trading_status_observed_at=BOUNDARY + timedelta(seconds=5),
        trading_status_source_hash="d" * 64,
    )
    catalyst = CatalystAuthority(
        opportunity_key=policy.opportunity_key,
        quality=CatalystQuality.CLEAR,
        score=80,
        observed_at=BOUNDARY + timedelta(seconds=5),
        source_hash="e" * 64,
    )
    account = AccountBudgetAuthority(
        account_role=role,
        account_fingerprint=account_fingerprint,
        snapshot_book_source_hash=snapshot.account_book.source_hash,
        observed_at=TRUSTED,
        baseline_clean=True,
        baseline_source_hash=baseline_hash,
        book_fingerprint=BOOK,
        book_source_hash="f" * 64,
        clean_equity=Decimal("100000"),
        open_position_count=0,
        open_order_count=0,
        filled_entry_count=0,
        lifetime_approved_risk=Decimal("0"),
        entry_reservation_active=False,
        reserved_approved_risk=Decimal("0"),
        event_already_attempted=False,
        history_source_hash="c" * 64,
    )
    prior = PriorDecisionAuthority(
        opportunity_key=policy.opportunity_key,
        decision_boundary=BOUNDARY,
        outcome=None,
        observed_at=TRUSTED,
        source_hash="0" * 64,
    )
    selection = select_vertical_candidate(
        snapshot, policy, OpportunityDirection.BULLISH, 2, selection_authority
    )
    assembly = assemble_opportunity_input(
        request=request,
        snapshot=snapshot,
        policy=policy,
        requested_maximum_quantity=2,
        selection_authority=selection_authority,
        selection=selection,
        signals=signals,
        catalyst=catalyst,
        account=account,
        prior_decision=prior,
    )
    decision = evaluate_opportunity(policy, assembly.values)
    observation_spec = OpportunityObservationSpec(
        plan_id=plan_id,
        baseline_id=baseline_id,
        account_fingerprint=account_fingerprint,
        policy_hash=policy_hash,
        request_hash=snapshot.request_hash,
        snapshot_hash=snapshot.source_hash,
        calendar_hash=CALENDAR,
        daily_hash=DAILY,
        intraday_hash=INTRADAY,
        signal_authority_hash=signals.calculation_source_hash,
        halt_hash=signals.trading_status_source_hash,
        catalyst_hash=catalyst.source_hash,
        greek_hash=GREEK,
        account_hash=snapshot.account_book.source_hash,
        activity_hash=account.history_source_hash,
        budget_hash=BUDGET,
        prior_decision_hash=prior.source_hash,
        trusted_at=TRUSTED,
        evaluated_at=TRUSTED,
        account_role=role,
    )
    observation_hash = opportunity_observation_digest(observation_spec)
    observation_id = uuid5(NAMESPACE_URL, f"alphadecay:opportunity-observation:{observation_hash}")
    observation = PersistedOpportunityObservation(
        observation_id=observation_id,
        plan_id=plan_id,
        baseline_id=baseline_id,
        account_fingerprint=account_fingerprint,
        manifest_hash=observation_hash,
        trusted_at=TRUSTED,
        evaluated_at=TRUSTED,
        account_role=role,
    )
    return OpportunityThesisFactoryInput(
        plan_spec,
        plan,
        baseline_seal,
        baseline,
        observation_spec,
        observation,
        request,
        snapshot,
        2,
        selection_authority,
        selection,
        signals,
        catalyst,
        account,
        prior,
        assembly,
        decision,
        CALENDAR,
        DAILY,
        INTRADAY,
        signals.calculation_source_hash,
        BUDGET,
    )


def _rebind_snapshot(
    inputs: OpportunityThesisFactoryInput, snapshot: OpportunityMarketSnapshot
) -> OpportunityThesisFactoryInput:
    selection_authority = replace(
        inputs.selection_authority,
        snapshot_source_hash=snapshot.source_hash,
    )
    signals = replace(inputs.signals, snapshot_source_hash=snapshot.source_hash)
    selection = select_vertical_candidate(
        snapshot,
        inputs.plan_spec.policy,
        OpportunityDirection.BULLISH,
        inputs.requested_maximum_quantity,
        selection_authority,
    )
    assembly = assemble_opportunity_input(
        request=inputs.request,
        snapshot=snapshot,
        policy=inputs.plan_spec.policy,
        requested_maximum_quantity=inputs.requested_maximum_quantity,
        selection_authority=selection_authority,
        selection=selection,
        signals=signals,
        catalyst=inputs.catalyst,
        account=inputs.account,
        prior_decision=inputs.prior_decision,
    )
    decision = evaluate_opportunity(inputs.plan_spec.policy, assembly.values)
    observation_spec = replace(inputs.observation_spec, snapshot_hash=snapshot.source_hash)
    observation_hash = opportunity_observation_digest(observation_spec)
    observation = replace(
        inputs.observation,
        observation_id=uuid5(
            NAMESPACE_URL,
            f"alphadecay:opportunity-observation:{observation_hash}",
        ),
        manifest_hash=observation_hash,
    )
    return replace(
        inputs,
        snapshot=snapshot,
        selection_authority=selection_authority,
        selection=selection,
        signals=signals,
        assembly=assembly,
        decision=decision,
        observation_spec=observation_spec,
        observation=observation,
    )


def test_builds_deterministic_plan_owned_thesis_with_scaled_greeks_and_paired_atm_iv() -> None:
    inputs = _factory_input()

    first = build_frozen_opportunity_thesis(inputs)
    second = build_frozen_opportunity_thesis(inputs)

    assert first == second
    assert first.account_role is AccountRole.DEVELOPMENT
    assert first.intended_exposure == {
        "schema_version": "v1",
        "delta": "60",
        "gamma": "2",
        "theta_per_day": "-8",
        "vega_per_iv_point": "20",
    }
    assert first.entry_atm_iv == Decimal("0.45")
    assert first.entry_atm_iv != inputs.snapshot.options[1].implied_volatility
    assert first.target_at == datetime(2026, 9, 4, 19, 45, tzinfo=UTC)
    assert first.exposure_limits == {
        "delta_low": "0",
        "delta_high": "100",
        "vega_low": "0",
        "vega_high": "40",
        "maximum_daily_theta": "20",
        "minimum_dte": 7,
        "maximum_dte": 45,
        "maximum_relative_spread": "0.25",
        "liquidity_authority_hash": (
            "21b5ce376e81b365590ebd4435e1e1a58c9818466f91bb79f6ca1f36329070e9"
        ),
    }
    assert first.portfolio_risk_cap == Decimal("1250")
    assert first.invalidation_codes == inputs.plan_spec.invalidation_codes
    assert (
        first.thesis_payload["origin_material"]["signal_authority_hash"]
        == inputs.signals.calculation_source_hash
    )
    json.dumps(first.intended_exposure, allow_nan=False)
    json.dumps(first.exposure_limits, allow_nan=False)
    json.dumps(first.thesis_payload, allow_nan=False)


def test_factory_rejects_every_cross_role_lineage_substitution() -> None:
    inputs = _factory_input()
    submission_book = replace(
        inputs.snapshot.account_book,
        account=inputs.snapshot.account_book.account.model_copy(
            update={"role": AccountRole.SUBMISSION}
        ),
    )
    substitutions = (
        replace(inputs, plan_spec=replace(inputs.plan_spec, account_role=AccountRole.SUBMISSION)),
        replace(inputs, plan=replace(inputs.plan, account_role=AccountRole.SUBMISSION)),
        replace(
            inputs,
            baseline_seal=replace(
                inputs.baseline_seal,
                account_role=AccountRole.SUBMISSION,
            ),
        ),
        replace(inputs, baseline=replace(inputs.baseline, account_role=AccountRole.SUBMISSION)),
        replace(
            inputs,
            observation_spec=replace(
                inputs.observation_spec,
                account_role=AccountRole.SUBMISSION,
            ),
        ),
        replace(
            inputs,
            observation=replace(inputs.observation, account_role=AccountRole.SUBMISSION),
        ),
        replace(inputs, request=replace(inputs.request, account_role=AccountRole.SUBMISSION)),
        replace(inputs, account=replace(inputs.account, account_role=AccountRole.SUBMISSION)),
        replace(
            inputs,
            snapshot=replace(inputs.snapshot, account_book=submission_book),
        ),
    )

    for changed in substitutions:
        with pytest.raises(OpportunityThesisError, match="THESIS_ROLE_AUTHORITY_MISMATCH"):
            build_frozen_opportunity_thesis(changed)


def test_factory_rejects_request_account_fingerprint_substitution() -> None:
    inputs = _factory_input()

    with pytest.raises(OpportunityThesisError, match="THESIS_SOURCE_AUTHORITY_MISMATCH"):
        build_frozen_opportunity_thesis(
            replace(
                inputs,
                account=replace(inputs.account, account_fingerprint="f" * 64),
            )
        )


def test_persistence_assigns_account_global_versions_and_replays_exactly() -> None:
    first_draft = build_frozen_opportunity_thesis(_factory_input())
    second_draft = build_frozen_opportunity_thesis(_factory_input("ACME_SECOND_EVENT"))
    assert first_draft.version == second_draft.version == 1

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            AccountRoleRow(
                role=AccountRole.DEVELOPMENT.value,
                account_fingerprint=ACCOUNT,
                equity=Decimal("100000"),
                autonomous_enabled=False,
            )
        )
    repository = SQLAlchemyOpportunityThesisRepository(sessions)

    first = repository.persist(first_draft)
    replay = repository.persist(first_draft)
    second = repository.persist(second_draft)

    assert replay == first
    assert (first.version, second.version) == (1, 2)
    assert first.thesis_hash == _canonical_hash(
        "alphadecay.lifecycle.thesis.v2", _database_thesis_material(first)
    )
    assert second.thesis_hash == _canonical_hash(
        "alphadecay.lifecycle.thesis.v2", _database_thesis_material(second)
    )
    engine.dispose()


def test_submission_evidence_and_thesis_persist_and_reload_with_exact_role() -> None:
    competition_baseline_id = uuid4()
    inputs = _factory_input(
        role=AccountRole.SUBMISSION,
        submission_baseline_id=competition_baseline_id,
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            AccountRoleRow(
                role=AccountRole.SUBMISSION.value,
                account_fingerprint=ACCOUNT,
                equity=Decimal("100000"),
                autonomous_enabled=False,
            )
        )
        session.add(
            SubmissionBaselineRow(
                baseline_id=competition_baseline_id,
                account_role=AccountRole.SUBMISSION.value,
                account_fingerprint=ACCOUNT,
                equity=Decimal("100000"),
                captured_at=inputs.baseline_seal.captured_at,
                positions_hash="6" * 64,
                orders_hash="7" * 64,
                activities_hash="8" * 64,
                contaminated=False,
            )
        )

    evidence = SQLAlchemyOpportunityEvidenceRepository(sessions)
    plan = evidence.freeze_plan(inputs.plan_spec)
    baseline = evidence.seal_baseline(inputs.baseline_seal)
    observation = evidence.append_observation(inputs.observation_spec)
    draft = build_frozen_opportunity_thesis(
        replace(inputs, plan=plan, baseline=baseline, observation=observation)
    )
    persisted = SQLAlchemyOpportunityThesisRepository(sessions).persist(draft)

    restarted_evidence = SQLAlchemyOpportunityEvidenceRepository(sessions)
    loaded_plan = restarted_evidence.load_plan(
        inputs.plan_spec.opportunity_key,
        account_role=AccountRole.SUBMISSION,
    )
    loaded_baseline = restarted_evidence.load_baseline(
        plan.plan_id,
        account_role=AccountRole.SUBMISSION,
    )
    loaded_observation = restarted_evidence.load_observation(
        plan.plan_id,
        account_role=AccountRole.SUBMISSION,
    )
    replay = SQLAlchemyOpportunityThesisRepository(sessions).persist(draft)

    assert loaded_plan is not None and loaded_plan.spec == inputs.plan_spec
    assert loaded_plan.persisted == plan
    assert loaded_plan.spec.request_contract.account_role is AccountRole.SUBMISSION
    assert loaded_baseline is not None and loaded_baseline.seal == inputs.baseline_seal
    assert loaded_baseline.persisted == baseline
    assert loaded_observation is not None
    assert loaded_observation.spec == inputs.observation_spec
    assert loaded_observation.persisted == observation
    assert persisted.account_role is AccountRole.SUBMISSION
    assert replay == persisted
    engine.dispose()


def test_thesis_versions_are_independent_for_each_executable_role() -> None:
    competition_baseline_id = uuid4()
    submission_account = "b" * 64
    development = (
        build_frozen_opportunity_thesis(_factory_input("DEV_ONE")),
        build_frozen_opportunity_thesis(_factory_input("DEV_TWO")),
    )
    submission = (
        build_frozen_opportunity_thesis(
            _factory_input(
                "SUB_ONE",
                role=AccountRole.SUBMISSION,
                submission_baseline_id=competition_baseline_id,
                account_fingerprint=submission_account,
            )
        ),
        build_frozen_opportunity_thesis(
            _factory_input(
                "SUB_TWO",
                role=AccountRole.SUBMISSION,
                submission_baseline_id=competition_baseline_id,
                account_fingerprint=submission_account,
            )
        ),
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add_all(
            (
                AccountRoleRow(
                    role=AccountRole.DEVELOPMENT.value,
                    account_fingerprint=ACCOUNT,
                    equity=Decimal("100000"),
                    autonomous_enabled=False,
                ),
                AccountRoleRow(
                    role=AccountRole.SUBMISSION.value,
                    account_fingerprint=submission_account,
                    equity=Decimal("100000"),
                    autonomous_enabled=False,
                ),
            )
        )
    repository = SQLAlchemyOpportunityThesisRepository(sessions)

    persisted_development = tuple(repository.persist(item) for item in development)
    persisted_submission = tuple(repository.persist(item) for item in submission)

    assert [item.version for item in persisted_development] == [1, 2]
    assert [item.version for item in persisted_submission] == [1, 2]
    assert all(item.account_role is AccountRole.DEVELOPMENT for item in persisted_development)
    assert all(item.account_role is AccountRole.SUBMISSION for item in persisted_submission)
    engine.dispose()


def test_persistence_rejects_same_origin_with_changed_authority() -> None:
    draft = build_frozen_opportunity_thesis(_factory_input())
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            AccountRoleRow(
                role=AccountRole.DEVELOPMENT.value,
                account_fingerprint=ACCOUNT,
                equity=Decimal("100000"),
                autonomous_enabled=False,
            )
        )
    repository = SQLAlchemyOpportunityThesisRepository(sessions)
    repository.persist(draft)

    with pytest.raises(OpportunityThesisError, match="THESIS_PERSISTENCE_INPUT_INVALID"):
        repository.persist(replace(draft, policy_hash="f" * 64))
    engine.dispose()


def test_persistence_rejects_origin_for_another_development_account() -> None:
    draft = build_frozen_opportunity_thesis(_factory_input())
    other_account = "f" * 64
    origin_material = dict(draft.thesis_payload["origin_material"])
    origin_material["account_fingerprint"] = other_account
    origin_hash = _canonical_hash("alphadecay.opportunity.thesis-origin.v1", origin_material)
    thesis_id = _stable_uuid(
        "alphadecay.opportunity.thesis.v1",
        origin_material["plan_id"],
        other_account,
        origin_material["opportunity_key"],
    )
    payload = dict(draft.thesis_payload)
    payload.update(
        thesis_id=str(thesis_id),
        thesis_hash="0" * 64,
        origin_hash=origin_hash,
        origin_material=origin_material,
    )
    other_draft = finalize_frozen_opportunity_thesis(
        replace(
            draft,
            thesis_version_id=UUID(int=0),
            thesis_id=thesis_id,
            thesis_hash="0" * 64,
            origin_hash=origin_hash,
            thesis_payload=payload,
        ),
        draft.version,
    )

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            AccountRoleRow(
                role=AccountRole.DEVELOPMENT.value,
                account_fingerprint=ACCOUNT,
                equity=Decimal("100000"),
                autonomous_enabled=False,
            )
        )
    repository = SQLAlchemyOpportunityThesisRepository(sessions)

    with pytest.raises(OpportunityThesisError, match="THESIS_ACCOUNT_AUTHORITY_MISMATCH"):
        repository.persist(other_draft)
    engine.dispose()


@pytest.mark.skipif(
    POSTGRES_URL_ENV not in os.environ,
    reason="requires a dedicated PostgreSQL integration URL",
)
def test_postgres_serializes_distinct_plan_theses_and_supports_legacy_inserts() -> None:
    admin = create_engine(os.environ[POSTGRES_URL_ENV])
    schema = f"opportunity_thesis_{uuid4().hex}"
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(os.environ[POSTGRES_URL_ENV])

    @event.listens_for(engine, "connect")
    def set_search_path(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute(f'SET search_path TO "{schema}"')
        cursor.close()

    sessions = sessionmaker(engine, expire_on_commit=False)
    try:
        apply_migrations(engine, discover_migrations(MIGRATIONS))
        with sessions.begin() as session:
            session.add_all(
                (
                    AccountRoleRow(
                        role=AccountRole.DEVELOPMENT.value,
                        account_fingerprint=ACCOUNT,
                        equity=Decimal("100000"),
                        autonomous_enabled=False,
                    ),
                    AccountRoleRow(
                        role=AccountRole.SUBMISSION.value,
                        account_fingerprint="9" * 64,
                        equity=Decimal("100000"),
                        autonomous_enabled=False,
                    ),
                )
            )
        evidence = SQLAlchemyOpportunityEvidenceRepository(sessions)
        inputs = (_factory_input(), _factory_input("ACME_SECOND_EVENT"))
        for item in inputs:
            assert evidence.freeze_plan(item.plan_spec) == item.plan
            assert evidence.seal_baseline(item.baseline_seal) == item.baseline
            assert evidence.append_observation(item.observation_spec) == item.observation
        drafts = tuple(build_frozen_opportunity_thesis(item) for item in inputs)

        def persist(draft):
            return SQLAlchemyOpportunityThesisRepository(sessions).persist(draft)

        with ThreadPoolExecutor(max_workers=2) as pool:
            persisted = tuple(pool.map(persist, drafts))
        assert {item.version for item in persisted} == {1, 2}
        for draft, thesis in zip(drafts, persisted, strict=True):
            assert persist(draft) == thesis
            assert thesis.thesis_hash == _canonical_hash(
                "alphadecay.lifecycle.thesis.v2", _database_thesis_material(thesis)
            )

        legacy_hash = "8" * 64
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO thesis_versions ("
                    "thesis_version_id, thesis_id, account_role, version, thesis_hash, "
                    "policy_hash, underlying, thesis_code, frozen_at, target_at, "
                    "intended_exposure, exposure_limits, volatility_view, entry_atm_iv, "
                    "approved_max_loss, portfolio_risk_cap, invalidation_codes, "
                    "thesis_payload, created_at) VALUES ("
                    ":version_id, :thesis_id, 'SUBMISSION', 1, :thesis_hash, :policy_hash, "
                    "'SPY', 'LEGACY_FIXTURE', :frozen_at, :target_at, '{}'::jsonb, "
                    "'{}'::jsonb, 'NEUTRAL', 0.4, 100, 100, "
                    "'[\"LEGACY_INVALIDATION\"]'::jsonb, CAST(:thesis_payload AS jsonb), "
                    ":frozen_at)"
                ),
                {
                    "version_id": UUID(int=9100),
                    "thesis_id": UUID(int=9101),
                    "thesis_hash": legacy_hash,
                    "policy_hash": "7" * 64,
                    "frozen_at": BOUNDARY,
                    "target_at": BOUNDARY + timedelta(days=1),
                    "thesis_payload": '{"frozen":true}',
                },
            )
            row = connection.execute(
                text(
                    "SELECT origin_hash, thesis_hash FROM thesis_versions "
                    "WHERE thesis_version_id = :version_id"
                ),
                {"version_id": UUID(int=9100)},
            ).one()
        assert row.origin_hash == legacy_hash
        assert row.thesis_hash != legacy_hash
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def test_rejects_signal_calculation_hash_substitution() -> None:
    inputs = _factory_input()
    changed_signals = replace(inputs.signals, calculation_source_hash="f" * 64)

    with pytest.raises(OpportunityThesisError, match="THESIS_SOURCE_AUTHORITY_MISMATCH"):
        build_frozen_opportunity_thesis(replace(inputs, signals=changed_signals))


@pytest.mark.parametrize(
    ("field", "replacement", "code"),
    (
        (
            "plan",
            lambda value: replace(value, plan_hash="f" * 64),
            "THESIS_PLAN_AUTHORITY_MISMATCH",
        ),
        (
            "baseline",
            lambda value: replace(value, baseline_hash="f" * 64),
            "THESIS_BASELINE_AUTHORITY_MISMATCH",
        ),
        (
            "observation",
            lambda value: replace(value, manifest_hash="f" * 64),
            "THESIS_OBSERVATION_AUTHORITY_MISMATCH",
        ),
        (
            "decision",
            lambda value: replace(value, result_hash="f" * 64),
            "THESIS_DECISION_AUTHORITY_MISMATCH",
        ),
    ),
)
def test_rejects_substituted_lineage(field, replacement, code: str) -> None:
    inputs = _factory_input()

    with pytest.raises(OpportunityThesisError, match=code):
        changed = replacement(getattr(inputs, field))
        build_frozen_opportunity_thesis(replace(inputs, **{field: changed}))


def test_rejects_selected_leg_that_no_longer_matches_exact_snapshot_row() -> None:
    inputs = _factory_input()
    candidate = inputs.selection.candidate
    assert candidate is not None
    changed_leg = replace(candidate.legs[0], strike=Decimal("99"))
    changed_candidate = replace(candidate, legs=(changed_leg, candidate.legs[1]))
    changed_selection = replace(inputs.selection, candidate=changed_candidate)

    with pytest.raises(OpportunityThesisError, match="THESIS_INPUT_AUTHORITY_MISMATCH"):
        build_frozen_opportunity_thesis(replace(inputs, selection=changed_selection))


def test_rejects_missing_same_expiry_call_put_pair() -> None:
    inputs = _factory_input()
    snapshot = replace(inputs.snapshot, options=inputs.snapshot.options[:2], source_hash="")
    snapshot = replace(snapshot, source_hash=opportunity_market_snapshot_digest(snapshot))
    inputs = _rebind_snapshot(inputs, snapshot)

    with pytest.raises(OpportunityThesisError, match="THESIS_ATM_IV_PAIR_MISSING"):
        build_frozen_opportunity_thesis(inputs)


def test_freezes_at_decision_time_when_manifest_is_persisted_later() -> None:
    inputs = _factory_input()
    changed_spec = replace(
        inputs.observation_spec,
        evaluated_at=inputs.observation_spec.evaluated_at + timedelta(seconds=1),
    )
    changed_hash = opportunity_observation_digest(changed_spec)
    changed_observation = replace(
        inputs.observation,
        observation_id=uuid5(
            NAMESPACE_URL,
            f"alphadecay:opportunity-observation:{changed_hash}",
        ),
        manifest_hash=changed_hash,
        evaluated_at=changed_spec.evaluated_at,
    )

    thesis = build_frozen_opportunity_thesis(
        replace(
            inputs,
            observation_spec=changed_spec,
            observation=changed_observation,
        )
    )

    assert thesis.frozen_at == inputs.snapshot.trusted_at
    assert thesis.created_at == inputs.snapshot.trusted_at


def test_rejects_entry_iv_outside_lifecycle_schema() -> None:
    inputs = _factory_input()
    changed_options = tuple(
        replace(option, implied_volatility=Decimal("101"), source_hash="")
        for option in inputs.snapshot.options
    )
    changed_options = tuple(
        replace(option, source_hash=opportunity_option_digest(option)) for option in changed_options
    )
    snapshot = replace(inputs.snapshot, options=changed_options, source_hash="")
    snapshot = replace(snapshot, source_hash=opportunity_market_snapshot_digest(snapshot))
    inputs = _rebind_snapshot(inputs, snapshot)

    with pytest.raises(OpportunityThesisError, match="THESIS_LIFECYCLE_SCHEMA_INVALID"):
        build_frozen_opportunity_thesis(inputs)


def test_rejects_caller_owned_target_or_exposure_limits() -> None:
    inputs = _factory_input()
    bad_target = replace(
        inputs.plan_spec,
        thesis_target_contract={
            "target_at": "2026-09-04T19:45:00Z",
            "volatility_view": "LONG",
            "extra": True,
        },
    )
    with pytest.raises(OpportunityThesisError, match="THESIS_PLAN_AUTHORITY_MISMATCH"):
        build_frozen_opportunity_thesis(replace(inputs, plan_spec=bad_target))

    bad_limits = replace(
        inputs.plan_spec,
        exposure_limit_contract={
            **inputs.plan_spec.exposure_limit_contract,
            "portfolio_risk_cap": "1",
        },
    )
    with pytest.raises(OpportunityThesisError, match="THESIS_PLAN_AUTHORITY_MISMATCH"):
        build_frozen_opportunity_thesis(replace(inputs, plan_spec=bad_limits))

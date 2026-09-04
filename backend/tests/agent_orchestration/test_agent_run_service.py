from __future__ import annotations

import asyncio
from dataclasses import MISSING, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from backend.app.contracts.v1 import (
    AccountRole,
    DataQuality,
    EvidenceState,
    GreekExposure,
    OptionRight,
    PositionIntent,
    ThesisStatus,
)
from backend.app.execution import (
    Actor,
    AssessmentCertificate,
    EntryApprovalAuthorization,
    ExecutionAction,
    ExecutionBlocked,
    ExecutionCertificate,
    ExecutionPending,
    ExecutionPendingCode,
    OrderEnvelope,
    OrderLegIntent,
    intent_digest,
    order_envelope_hash,
)
from backend.app.execution.models import ExecutionIntent, IntentState
from backend.app.experiment_lineage import ExperimentExecutionLineage
from backend.app.lifecycle import LifecycleLaunchAuthority
from backend.app.persistence.agent_authority import agent_result_material, canonical_agent_hash
from backend.app.persistence.agent_codec import decode_agent_value, encode_agent_value
from backend.app.policy import (
    AccountOpportunityState,
    AssessmentInput,
    CatalystQuality,
    HardGateInput,
    InstrumentKind,
    OpportunityInput,
    OpportunityPolicy,
    OptionFeed,
    OptionLeg,
    ScoreInput,
    VerticalCandidate,
    VerticalStrategy,
    VolatilityView,
    evaluate_assessment,
    evaluate_opportunity,
)
from backend.app.policy.opportunity import TradingHaltState
from backend.app.services import (
    AcquisitionFailure,
    AcquisitionKind,
    AgentDecision,
    AgentRunResult,
    AgentRunService,
    AgentTick,
    AuthorizationIntentProposal,
    CalibrationBinding,
    LifecycleAcquisition,
    ObservedPaperAccountAuthority,
    OpportunityAcquisition,
    OpportunityNoTradeAcquisition,
    PermanentAccountLatch,
    PersistedAgentDecision,
)

NOW = datetime(2026, 8, 29, 15, tzinfo=UTC)
CALIBRATION_BOUNDARY = datetime(2026, 8, 28, 20, tzinfo=UTC)
FINGERPRINT = "a" * 64
TICK_ID = UUID("00000000-0000-0000-0000-000000000801")
RESERVATION_ID = UUID("00000000-0000-0000-0000-000000000809")
AUTHORIZATION_ID = UUID("00000000-0000-0000-0000-000000000802")
INTENT_ID = UUID("00000000-0000-0000-0000-000000000803")
LIFECYCLE_AUTHORIZATION_ID = UUID("00000000-0000-0000-0000-000000000807")
LIFECYCLE_INTENT_ID = UUID("00000000-0000-0000-0000-000000000808")
THESIS_VERSION_ID = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")


def test_acquisitions_require_explicit_frozen_thesis_authority() -> None:
    opportunity_fields = OpportunityAcquisition.__dataclass_fields__
    lifecycle_fields = LifecycleAcquisition.__dataclass_fields__

    assert opportunity_fields["thesis_version_id"].default is MISSING
    assert opportunity_fields["thesis_version_id"].default_factory is MISSING
    assert lifecycle_fields["thesis_version_id"].default is MISSING
    assert lifecycle_fields["thesis_version_id"].default_factory is MISSING


def test_calibration_binding_requires_the_exact_no_trade_decision() -> None:
    with pytest.raises(ValueError, match="CALIBRATION_BINDING_INVALID"):
        CalibrationBinding(
            account_role=AccountRole.SUBMISSION,
            account_fingerprint=FINGERPRINT,
            decision_code="ENTRY_APPROVED",
            machine_binding_hash="b" * 64,
            calibration_hash="c" * 64,
            decision_boundary=CALIBRATION_BOUNDARY,
            sealed_at=CALIBRATION_BOUNDARY,
        )


class FixedAuthority:
    def __init__(self, *, persistent_autonomy_enabled: bool = True) -> None:
        self.persistent_autonomy_enabled = persistent_autonomy_enabled

    def observe(self) -> ObservedPaperAccountAuthority:
        return ObservedPaperAccountAuthority(
            role=AccountRole.SUBMISSION,
            account_fingerprint=FINGERPRINT,
            paper=True,
            persistent_autonomy_enabled=self.persistent_autonomy_enabled,
        )


class DevelopmentAuthority:
    def __init__(self, *, persistent_autonomy_enabled: bool = True) -> None:
        self.persistent_autonomy_enabled = persistent_autonomy_enabled

    def observe(self) -> ObservedPaperAccountAuthority:
        return ObservedPaperAccountAuthority(
            role=AccountRole.DEVELOPMENT,
            account_fingerprint=FINGERPRINT,
            paper=True,
            persistent_autonomy_enabled=self.persistent_autonomy_enabled,
        )


class FixedClock:
    def now(self) -> datetime:
        return NOW


class FixedCalibration:
    def binding_for(self, authority: ObservedPaperAccountAuthority) -> CalibrationBinding:
        assert authority.account_fingerprint == FINGERPRINT
        return CalibrationBinding(
            account_role=AccountRole.SUBMISSION,
            account_fingerprint=FINGERPRINT,
            decision_code="CALIBRATION_BINDING_NO_TRADE",
            machine_binding_hash="b" * 64,
            calibration_hash="c" * 64,
            decision_boundary=CALIBRATION_BOUNDARY,
            sealed_at=CALIBRATION_BOUNDARY,
        )


class ForbiddenAcquisition:
    calls = 0

    async def acquire(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("submission calibration reached acquisition")


class FailingAcquisition:
    def __init__(self, kind: AcquisitionKind) -> None:
        self.kind = kind
        self.calls = 0

    async def acquire(self, authority, trusted_at, tick_id, *, actor):
        self.calls += 1
        assert tick_id == TICK_ID
        assert actor in {Actor.OWNER, Actor.SCHEDULER}
        raise AcquisitionFailure(self.kind, "PROVIDER_UNAVAILABLE")


class FixedAcquisition:
    def __init__(self, bundle, *, role: AccountRole = AccountRole.DEVELOPMENT) -> None:
        self.bundle = bundle
        self.role = role
        self.calls = 0
        self.actors: list[Actor] = []

    async def acquire(self, authority, trusted_at, tick_id, *, actor):
        self.calls += 1
        self.actors.append(actor)
        assert authority.role is self.role
        assert trusted_at == NOW
        assert tick_id == TICK_ID
        assert actor in {Actor.OWNER, Actor.SCHEDULER}
        return self.bundle


class RecordingDecisions:
    def __init__(self, *, return_proposal: bool = False) -> None:
        self.decisions: list[AgentDecision] = []
        self.proposals: list[object | None] = []
        self.terminals: list[str] = []
        self.completion_reservations: list[UUID] = []
        self.return_proposal = return_proposal
        self.approved_intent: ExecutionIntent | None = None
        self.submission_previews: list[UUID] = []

    def begin_tick(self, authority, actor, trusted_at):
        return AgentTick(TICK_ID, RESERVATION_ID, authority, actor, trusted_at)

    def permanent_latch(self, authority):
        return PermanentAccountLatch(False)

    def persist_decision(self, tick, decision, proposal):
        self.decisions.append(decision)
        self.proposals.append(proposal)
        self.approved_intent = proposal.intent if proposal and self.return_proposal else None
        return PersistedAgentDecision(
            decision=decision,
            approved_intent=self.approved_intent,
        )

    def complete_tick(self, tick, terminal_code, certificate):
        self.terminals.append(terminal_code)
        self.completion_reservations.append(tick.reservation_token)
        return AgentRunResult(
            tick_id=tick.tick_id,
            terminal_code=terminal_code,
            decision=self.decisions[-1],
            approved_intent_id=(
                self.approved_intent.intent_id if self.approved_intent is not None else None
            ),
            execution_certificate_id=(
                certificate.certificate_id if certificate is not None else None
            ),
            proof_hash="d" * 64,
        )

    def submission_order_preview(self, intent_id: UUID):
        self.submission_previews.append(intent_id)
        return object()

    def pending_submission_lifecycle_intents(self, _authority):
        return ()


class LatchedDecisions(RecordingDecisions):
    def permanent_latch(self, authority):
        return PermanentAccountLatch(True, "AMBIGUOUS_BROKER_OUTCOME")


class DuplicateDecisions(RecordingDecisions):
    def __init__(self, result: AgentRunResult) -> None:
        super().__init__()
        self.result = result

    def begin_tick(self, authority, actor, trusted_at):
        return self.result

    def permanent_latch(self, authority):
        raise AssertionError("completed tick reached latch lookup")


class ForbiddenRuntime:
    class Execution:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("submission calibration reached execution")

    execution = Execution()


class RecordingRuntime:
    class Execution:
        def __init__(self, status: str) -> None:
            self.calls: list[tuple[UUID, Actor, datetime]] = []
            self.status = status

        def execute(self, intent_id: UUID, actor: Actor, now: datetime):
            self.calls.append((intent_id, actor, now))
            return ExecutionCertificate(
                certificate_id=UUID("00000000-0000-0000-0000-000000000804"),
                intent_id=intent_id,
                entry_approval_id=AUTHORIZATION_ID,
                assessment_certificate_id=None,
                execution_status=self.status,
                attempt_ids=("approved-a0",),
                actual_exposure=None,
                reconciliation_checks=("TERMINAL",),
                created_at=now,
            )

    def __init__(self, status: str = "FILLED") -> None:
        self.execution = self.Execution(status)


class RecordingMaterializer:
    def __init__(
        self,
        *,
        prepare_error: bool = False,
        materialize_error: bool = False,
    ) -> None:
        self.prepare_error = prepare_error
        self.materialize_error = materialize_error
        self.prepares: list[tuple[UUID, LifecycleLaunchAuthority, datetime]] = []
        self.calls: list[tuple[UUID, LifecycleLaunchAuthority]] = []

    def recover_pending(self, *, account_role, account_fingerprint):
        return ()

    def pending_execution_intents(self, *, account_role, account_fingerprint):
        return ()

    def prepare(self, *, execution_intent_id, launch_authority, prepared_at):
        self.prepares.append((execution_intent_id, launch_authority, prepared_at))
        if self.prepare_error:
            raise RuntimeError("ENTRY_MATERIALIZATION_PREPARATION_INVALID")

    def materialize(self, *, execution_certificate_id, launch_authority):
        self.calls.append((execution_certificate_id, launch_authority))
        if self.materialize_error:
            raise RuntimeError("ENTRY_LINEAGE_INVALID")
        return UUID("00000000-0000-0000-0000-000000000899")


class RecoveringMaterializer(RecordingMaterializer):
    def __init__(self) -> None:
        super().__init__()
        self.recoveries: list[tuple[str, str]] = []

    def recover_pending(self, *, account_role, account_fingerprint):
        self.recoveries.append((account_role, account_fingerprint))
        return (UUID("00000000-0000-0000-0000-000000000899"),)


class PendingExecutionMaterializer(RecordingMaterializer):
    def __init__(self) -> None:
        super().__init__()
        self.pending = True
        self.recovery_calls = 0

    def recover_pending(self, *, account_role, account_fingerprint):
        self.recovery_calls += 1
        if self.recovery_calls > 1:
            self.pending = False
            return (UUID("00000000-0000-0000-0000-000000000899"),)
        return ()

    def pending_execution_intents(self, *, account_role, account_fingerprint):
        return (INTENT_ID,) if self.pending else ()


class RecordingLifecycleRuntime:
    class Execution:
        def __init__(
            self,
            status: str,
            *,
            entry_approval_id: UUID | None = None,
            assessment_certificate_id: UUID | None = LIFECYCLE_AUTHORIZATION_ID,
            returned_intent_id: UUID | None = None,
        ) -> None:
            self.calls: list[tuple[UUID, Actor, datetime]] = []
            self.status = status
            self.entry_approval_id = entry_approval_id
            self.assessment_certificate_id = assessment_certificate_id
            self.returned_intent_id = returned_intent_id

        def execute(self, intent_id: UUID, actor: Actor, now: datetime):
            self.calls.append((intent_id, actor, now))
            return ExecutionCertificate(
                certificate_id=UUID("00000000-0000-0000-0000-000000000809"),
                intent_id=self.returned_intent_id or intent_id,
                entry_approval_id=self.entry_approval_id,
                assessment_certificate_id=self.assessment_certificate_id,
                execution_status=self.status,
                attempt_ids=("close-a0",),
                actual_exposure=None,
                reconciliation_checks=("TERMINAL",),
                created_at=now,
            )

    def __init__(
        self,
        status: str = "CANCELED",
        *,
        entry_approval_id: UUID | None = None,
        assessment_certificate_id: UUID | None = LIFECYCLE_AUTHORIZATION_ID,
        returned_intent_id: UUID | None = None,
    ) -> None:
        self.execution = self.Execution(
            status,
            entry_approval_id=entry_approval_id,
            assessment_certificate_id=assessment_certificate_id,
            returned_intent_id=returned_intent_id,
        )


class RecordingLifecycleTerminalMaterializer:
    def __init__(
        self,
        *,
        decisions: RecordingDecisions | None = None,
        fail: bool = False,
    ) -> None:
        self.decisions = decisions
        self.fail = fail
        self.calls: list[UUID] = []

    def materialize(self, *, execution_certificate_id: UUID) -> UUID:
        if self.decisions is not None:
            assert self.decisions.terminals == []
        self.calls.append(execution_certificate_id)
        if self.fail:
            raise RuntimeError("LIFECYCLE_MATERIALIZATION_LINEAGE_INVALID")
        return UUID("00000000-0000-0000-0000-000000000898")


class BlockedRuntime:
    class Execution:
        def execute(self, *_args, **_kwargs):
            raise ExecutionPending(ExecutionPendingCode.ADVANCE)

    execution = Execution()


class PendingThenRecoveredRuntime:
    class Execution(RecordingRuntime.Execution):
        def __init__(self, pending_code: ExecutionPendingCode) -> None:
            super().__init__("FILLED")
            self.completed = False
            self.pending_code = pending_code

        def execute(self, intent_id: UUID, actor: Actor, now: datetime):
            if not self.calls:
                self.calls.append((intent_id, actor, now))
                raise ExecutionPending(self.pending_code)
            certificate = super().execute(intent_id, actor, now)
            self.completed = True
            return certificate

    def __init__(self, pending_code: ExecutionPendingCode) -> None:
        self.execution = self.Execution(pending_code)


class PendingUntilExecutionCompletesMaterializer(RecordingMaterializer):
    def __init__(self, runtime: PendingThenRecoveredRuntime) -> None:
        super().__init__()
        self.runtime = runtime
        self.recovery_calls = 0

    def recover_pending(self, *, account_role, account_fingerprint):
        self.recovery_calls += 1
        return ()

    def pending_execution_intents(self, *, account_role, account_fingerprint):
        return () if self.runtime.execution.completed else (INTENT_ID,)


def opportunity_policy() -> OpportunityPolicy:
    return OpportunityPolicy(
        version="agent-opportunity-v1",
        opportunity_key="ACME_EVENT",
        underlying="ACME",
        selected_decision_boundary=NOW,
        last_entry_boundary=NOW + timedelta(days=2),
        maximum_decision_delay=timedelta(seconds=30),
        maximum_underlying_age=timedelta(seconds=30),
        maximum_catalyst_age=timedelta(minutes=15),
        maximum_option_quote_age=timedelta(seconds=30),
        maximum_leg_quote_skew=timedelta(seconds=30),
        minimum_vwap_distance=Decimal("0.003"),
        maximum_vwap_distance=Decimal("0.03"),
        minimum_relative_return=Decimal("0.0075"),
        minimum_beta=Decimal("0"),
        maximum_beta=Decimal("3"),
        required_trend_hits=3,
        maximum_first_reaction=Decimal("0.12"),
        minimum_catalyst_score=10,
        minimum_candidate_score=70,
        minimum_dte=14,
        maximum_dte=35,
        maximum_relative_spread=Decimal("0.05"),
        minimum_debit_width_fraction=Decimal("0.20"),
        maximum_debit_width_fraction=Decimal("0.60"),
        minimum_credit_width_fraction=Decimal("0.20"),
        maximum_position_loss=Decimal("1250"),
        maximum_equity_risk_fraction=Decimal("0.0125"),
        maximum_lifetime_entries=3,
        maximum_lifetime_risk=Decimal("3000"),
        equity_floor=Decimal("97500"),
        maximum_quantity=5,
    )


def option_leg(symbol: str, strike: str, intent: PositionIntent, bid: str, ask: str) -> OptionLeg:
    return OptionLeg(
        instrument_kind=InstrumentKind.OPTION,
        symbol=symbol,
        underlying="ACME",
        right=OptionRight.CALL,
        strike=Decimal(strike),
        expiry=date(2026, 9, 18),
        intent=intent,
        ratio=1,
        multiplier=100,
        active=True,
        tradable=True,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=10,
        ask_size=12,
        quote_at=NOW,
        feed=OptionFeed.INDICATIVE_MODIFIED,
        greeks_complete=True,
        greek_units_verified=True,
    )


def opportunity_values() -> OpportunityInput:
    candidate = VerticalCandidate(
        strategy=VerticalStrategy.BULL_CALL_DEBIT,
        legs=(
            option_leg(
                "ACME260918C00100000",
                "100",
                PositionIntent.BUY_TO_OPEN,
                "2.00",
                "2.10",
            ),
            option_leg(
                "ACME260918C00105000",
                "105",
                PositionIntent.SELL_TO_OPEN,
                "0.90",
                "0.94",
            ),
        ),
        quantity=4,
        dte=20,
        approved_limit=Decimal("1.20"),
        candidate_score=82,
        selection_rank=1,
        buying_power_sufficient=True,
    )
    return OpportunityInput(
        opportunity_key="ACME_EVENT",
        underlying="ACME",
        observed_decision_boundary=NOW,
        evaluated_at=NOW,
        completed_bar_at=NOW,
        decision_boundary_complete=True,
        prior_decision_outcome=None,
        data_quality=DataQuality.COMPLETE,
        market_open=True,
        trading_halted=TradingHaltState.NOT_HALTED,
        underlying_observed_at=NOW,
        catalyst_observed_at=NOW,
        catalyst_quality=CatalystQuality.CLEAR,
        catalyst_score=25,
        vwap_distance=Decimal("0.01"),
        relative_return=Decimal("0.008"),
        beta=Decimal("1.2"),
        bull_trend_hits=3,
        bear_trend_hits=0,
        absolute_first_reaction=Decimal("0.08"),
        candidate=candidate,
        account=AccountOpportunityState(
            account_role=AccountRole.DEVELOPMENT,
            book_fingerprint=FINGERPRINT,
            baseline_clean=True,
            clean_equity=Decimal("100000"),
            open_position_count=0,
            open_order_count=0,
            filled_entry_count=0,
            lifetime_approved_risk=Decimal("0"),
            entry_reservation_active=False,
            reserved_approved_risk=Decimal("0"),
            event_already_attempted=False,
        ),
    )


def approved_opportunity_bundle() -> OpportunityAcquisition:
    policy = opportunity_policy()
    values = opportunity_values()
    decision = evaluate_opportunity(policy, values)
    assert decision.approved_max_loss == Decimal("480.00")
    envelope = OrderEnvelope(
        action=ExecutionAction.ENTRY,
        authorization_certificate_id=AUTHORIZATION_ID,
        policy_hash=decision.policy_hash,
        account_fingerprint=FINGERPRINT,
        position_or_book_fingerprint=FINGERPRINT,
        legs=tuple(
            OrderLegIntent(leg.symbol, leg.intent, leg.ratio) for leg in values.candidate.legs
        ),
        quantity=4,
        minimum_limit=Decimal("1.20"),
        maximum_limit=Decimal("1.20"),
        approved_max_loss=decision.approved_max_loss,
        event_key=values.opportunity_key,
        trading_day=NOW.date(),
    )
    approval = EntryApprovalAuthorization(
        approval_id=AUTHORIZATION_ID,
        thesis_version_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
        account_role=AccountRole.DEVELOPMENT,
        policy_hash=decision.policy_hash,
        book_fingerprint=FINGERPRINT,
        envelope_hash=order_envelope_hash(envelope),
        approved_max_loss=decision.approved_max_loss,
        quantity=4,
        valid_from=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )
    intent = ExecutionIntent(
        intent_id=INTENT_ID,
        account_role=AccountRole.DEVELOPMENT,
        envelope=envelope,
        digest=intent_digest(envelope),
        state=IntentState.APPROVED,
    )
    return OpportunityAcquisition(
        policy,
        values,
        THESIS_VERSION_ID,
        AuthorizationIntentProposal(approval, intent),
        LifecycleLaunchAuthority(
            beta60=values.beta,
            benchmark_symbol="QQQ",
            entry_boundary_at=NOW - timedelta(minutes=1),
            entry_policy_hash=decision.policy_hash,
            underlying_source_hash="1" * 64,
            benchmark_source_hash="2" * 64,
            completed_bar_source_hash="3" * 64,
        ),
    )


def approved_submission_opportunity_bundle() -> OpportunityAcquisition:
    bundle = approved_opportunity_bundle()
    values = replace(
        bundle.values,
        account=replace(bundle.values.account, account_role=AccountRole.SUBMISSION),
    )
    assert bundle.proposal is not None
    proposal = AuthorizationIntentProposal(
        replace(bundle.proposal.authorization, account_role=AccountRole.SUBMISSION),
        replace(bundle.proposal.intent, account_role=AccountRole.SUBMISSION),
    )
    return replace(bundle, values=values, proposal=proposal)


def with_entry_envelope(
    bundle: OpportunityAcquisition,
    envelope: OrderEnvelope,
) -> OpportunityAcquisition:
    assert bundle.proposal is not None
    authorization = replace(
        bundle.proposal.authorization,
        envelope_hash=order_envelope_hash(envelope),
    )
    intent = replace(
        bundle.proposal.intent,
        envelope=envelope,
        digest=intent_digest(envelope),
    )
    return replace(
        bundle,
        proposal=AuthorizationIntentProposal(authorization, intent),
    )


def no_action_lifecycle_bundle() -> LifecycleAcquisition:
    return LifecycleAcquisition(
        AssessmentInput(
            assessment_id=UUID("00000000-0000-0000-0000-000000000805"),
            run_id=UUID("00000000-0000-0000-0000-000000000806"),
            policy_hash="e" * 64,
            quality=DataQuality.MISSING,
            actual_exposure=None,
            thesis_status=ThesisStatus.UNKNOWN,
            evidence_state=EvidenceState.UNKNOWN,
            scores=None,
        ),
        THESIS_VERSION_ID,
    )


def risk_close_lifecycle_bundle() -> LifecycleAcquisition:
    policy_hash = "e" * 64
    assessment_id = UUID("00000000-0000-0000-0000-000000000810")
    values = AssessmentInput(
        assessment_id=assessment_id,
        run_id=UUID("00000000-0000-0000-0000-000000000811"),
        policy_hash=policy_hash,
        quality=DataQuality.COMPLETE,
        actual_exposure=GreekExposure(
            delta=Decimal("20"),
            gamma=Decimal("1"),
            theta_per_day=Decimal("-5"),
            vega_per_iv_point=Decimal("8"),
        ),
        thesis_status=ThesisStatus.UNKNOWN,
        evidence_state=EvidenceState.UNKNOWN,
        scores=ScoreInput(
            evidence_drift=Decimal("0"),
            delta=Decimal("20"),
            delta_low=Decimal("-25"),
            delta_high=Decimal("25"),
            vega=Decimal("8"),
            vega_low=Decimal("0"),
            vega_high=Decimal("20"),
            theta_per_day=Decimal("-5"),
            max_daily_theta=Decimal("10"),
            dte=10,
            minimum_dte=7,
            maximum_dte=35,
            horizon_fraction=Decimal("0.50"),
            volatility_view=VolatilityView.NEUTRAL,
            entry_atm_iv=Decimal("0.40"),
            current_atm_iv=Decimal("0.40"),
            liquidation_pnl=Decimal("-100"),
            approved_max_loss=Decimal("480"),
        ),
        hard_gates=HardGateInput(contest_end_window=True),
    )
    envelope = OrderEnvelope(
        action=ExecutionAction.CLOSE,
        authorization_certificate_id=LIFECYCLE_AUTHORIZATION_ID,
        policy_hash=policy_hash,
        account_fingerprint=FINGERPRINT,
        position_or_book_fingerprint=FINGERPRINT,
        legs=(
            OrderLegIntent(
                "ACME260918C00100000",
                PositionIntent.SELL_TO_CLOSE,
                1,
            ),
            OrderLegIntent(
                "ACME260918C00105000",
                PositionIntent.BUY_TO_CLOSE,
                1,
            ),
        ),
        quantity=4,
        minimum_limit=Decimal("0.90"),
        maximum_limit=Decimal("1.20"),
        approved_max_loss=Decimal("480"),
        event_key="ACME_EVENT_CLOSE",
        trading_day=NOW.date(),
    )
    authorization = AssessmentCertificate(
        certificate_id=LIFECYCLE_AUTHORIZATION_ID,
        assessment_id=assessment_id,
        thesis_version_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
        account_role=AccountRole.DEVELOPMENT,
        action=ExecutionAction.CLOSE,
        position_fingerprint=FINGERPRINT,
        envelope_hash=order_envelope_hash(envelope),
        approved_max_loss=Decimal("480"),
        quantity=4,
        expected_after_exposure=None,
        policy_hash=policy_hash,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )
    intent = ExecutionIntent(
        intent_id=LIFECYCLE_INTENT_ID,
        account_role=AccountRole.DEVELOPMENT,
        envelope=envelope,
        digest=intent_digest(envelope),
        state=IntentState.APPROVED,
    )
    return LifecycleAcquisition(
        values,
        THESIS_VERSION_ID,
        AuthorizationIntentProposal(authorization, intent),
    )


def test_submission_calibration_is_terminal_before_acquisition_or_intent() -> None:
    acquisition = ForbiddenAcquisition()
    decisions = RecordingDecisions()
    materializer = RecoveringMaterializer()
    service = AgentRunService(
        account_authority=FixedAuthority(),
        clock=FixedClock(),
        calibration=FixedCalibration(),
        acquisition=acquisition,
        decisions=decisions,
        runtime=ForbiddenRuntime(),
        server_autonomy_enabled=True,
        entry_materializer=materializer,
    )

    result = asyncio.run(service.run(Actor.SCHEDULER))

    assert result.terminal_code == "CALIBRATION_BINDING_NO_TRADE"
    assert result.decision.code == "CALIBRATION_BINDING_NO_TRADE"
    assert result.approved_intent_id is None
    assert acquisition.calls == 0
    assert decisions.proposals == [None]
    assert decisions.completion_reservations == [RESERVATION_ID]
    assert decisions.decisions[0].calibration.decision_boundary == CALIBRATION_BOUNDARY
    assert materializer.recoveries == []


@pytest.mark.parametrize(
    ("gate", "server", "durable", "actor"),
    (
        (False, True, True, Actor.SCHEDULER),
        (True, False, True, Actor.SCHEDULER),
        (True, True, False, Actor.SCHEDULER),
        (True, True, True, Actor.OWNER),
    ),
)
def test_submission_requires_every_runtime_authority(
    gate: bool,
    server: bool,
    durable: bool,
    actor: Actor,
) -> None:
    acquisition = ForbiddenAcquisition()
    result = asyncio.run(
        AgentRunService(
            account_authority=FixedAuthority(persistent_autonomy_enabled=durable),
            clock=FixedClock(),
            calibration=FixedCalibration(),
            acquisition=acquisition,
            decisions=RecordingDecisions(),
            runtime=ForbiddenRuntime(),
            server_autonomy_enabled=server,
            submission_opportunity_enabled=gate,
        ).run(actor)
    )

    assert result.terminal_code == "CALIBRATION_BINDING_NO_TRADE"
    assert acquisition.calls == 0


def test_authorized_submission_scheduler_reaches_exact_role_acquisition() -> None:
    acquisition = FailingAcquisition(AcquisitionKind.OPPORTUNITY)
    result = asyncio.run(
        AgentRunService(
            account_authority=FixedAuthority(),
            clock=FixedClock(),
            calibration=FixedCalibration(),
            acquisition=acquisition,
            decisions=RecordingDecisions(),
            runtime=ForbiddenRuntime(),
            server_autonomy_enabled=True,
            submission_opportunity_enabled=True,
        ).run(Actor.SCHEDULER)
    )

    assert result.terminal_code == "PROVIDER_FAILURE_NO_TRADE"
    assert result.decision.provider_failure_code == "PROVIDER_UNAVAILABLE"


def test_permanent_latch_persists_no_action_without_acquisition() -> None:
    acquisition = ForbiddenAcquisition()
    decisions = LatchedDecisions()
    service = AgentRunService(
        account_authority=DevelopmentAuthority(),
        clock=FixedClock(),
        calibration=FixedCalibration(),
        acquisition=acquisition,
        decisions=decisions,
        runtime=ForbiddenRuntime(),
        server_autonomy_enabled=True,
    )

    result = asyncio.run(service.run(Actor.SCHEDULER))

    assert result.terminal_code == "ACCOUNT_PERMANENTLY_LATCHED"
    assert result.decision.code == "ACCOUNT_PERMANENTLY_LATCHED"
    assert result.approved_intent_id is None
    assert acquisition.calls == 0
    assert decisions.proposals == [None]


def test_provider_failure_is_persisted_as_path_specific_terminal_decision() -> None:
    for kind, expected_code in (
        (AcquisitionKind.OPPORTUNITY, "PROVIDER_FAILURE_NO_TRADE"),
        (AcquisitionKind.LIFECYCLE, "PROVIDER_FAILURE_NO_ACTION"),
    ):
        decisions = RecordingDecisions()
        service = AgentRunService(
            account_authority=DevelopmentAuthority(),
            clock=FixedClock(),
            calibration=FixedCalibration(),
            acquisition=FailingAcquisition(kind),
            decisions=decisions,
            runtime=ForbiddenRuntime(),
            server_autonomy_enabled=True,
        )

        result = asyncio.run(service.run(Actor.SCHEDULER))

        assert result.terminal_code == expected_code
        assert result.decision.code == expected_code
        assert result.decision.provider_failure_code == "PROVIDER_UNAVAILABLE"
        assert result.decision.provider_failure_kind is kind
        assert decisions.proposals == [None]


def test_autonomous_scheduler_dispatches_only_exact_policy_approved_entry() -> None:
    bundle = approved_opportunity_bundle()
    acquisition = FixedAcquisition(bundle)
    decisions = RecordingDecisions(return_proposal=True)
    runtime = RecordingRuntime()
    materializer = RecordingMaterializer()
    service = AgentRunService(
        account_authority=DevelopmentAuthority(),
        clock=FixedClock(),
        calibration=FixedCalibration(),
        acquisition=acquisition,
        decisions=decisions,
        runtime=runtime,
        server_autonomy_enabled=True,
        entry_materializer=materializer,
    )

    result = asyncio.run(service.run(Actor.SCHEDULER))

    assert result.decision.code == "ENTRY_APPROVED"
    assert result.approved_intent_id == INTENT_ID
    assert decisions.decisions[0].normalized_input is bundle.values
    assert result.execution_certificate_id == UUID("00000000-0000-0000-0000-000000000804")
    assert runtime.execution.calls == [(INTENT_ID, Actor.SCHEDULER, NOW)]
    assert decisions.proposals == [bundle.proposal]
    assert materializer.calls == [
        (
            UUID("00000000-0000-0000-0000-000000000804"),
            bundle.launch_authority,
        )
    ]
    assert materializer.prepares == [(INTENT_ID, bundle.launch_authority, NOW)]


def test_submission_order_preview_is_rendered_from_durable_intent_before_dispatch() -> None:
    bundle = approved_submission_opportunity_bundle()
    decisions = RecordingDecisions(return_proposal=True)

    class PreviewOrderedRuntime(RecordingRuntime):
        class Execution(RecordingRuntime.Execution):
            def execute(self, intent_id: UUID, actor: Actor, now: datetime):
                assert decisions.submission_previews == [intent_id]
                return super().execute(intent_id, actor, now)

    runtime = PreviewOrderedRuntime()
    result = asyncio.run(
        AgentRunService(
            account_authority=FixedAuthority(),
            clock=FixedClock(),
            calibration=FixedCalibration(),
            acquisition=FixedAcquisition(bundle, role=AccountRole.SUBMISSION),
            decisions=decisions,
            runtime=runtime,
            server_autonomy_enabled=True,
            submission_opportunity_enabled=True,
            entry_materializer=RecordingMaterializer(),
        ).run(Actor.SCHEDULER)
    )

    assert result.terminal_code == "FILLED"
    assert decisions.submission_previews == [INTENT_ID]


def test_submission_dispatch_fails_closed_when_durable_preview_is_unavailable() -> None:
    bundle = approved_submission_opportunity_bundle()

    class UnavailablePreviewDecisions(RecordingDecisions):
        def submission_order_preview(self, intent_id: UUID):
            raise ExecutionBlocked("SUBMISSION_ORDER_PREVIEW_UNAVAILABLE")

    runtime = RecordingRuntime()
    result = asyncio.run(
        AgentRunService(
            account_authority=FixedAuthority(),
            clock=FixedClock(),
            calibration=FixedCalibration(),
            acquisition=FixedAcquisition(bundle, role=AccountRole.SUBMISSION),
            decisions=UnavailablePreviewDecisions(return_proposal=True),
            runtime=runtime,
            server_autonomy_enabled=True,
            submission_opportunity_enabled=True,
            entry_materializer=RecordingMaterializer(),
        ).run(Actor.SCHEDULER)
    )

    assert result.terminal_code == "SUBMISSION_ORDER_PREVIEW_UNAVAILABLE"
    assert runtime.execution.calls == []


def test_filled_entry_materialization_failure_is_terminal_and_not_redispatched() -> None:
    bundle = approved_opportunity_bundle()
    runtime = RecordingRuntime()
    decisions = RecordingDecisions(return_proposal=True)
    first = asyncio.run(
        AgentRunService(
            account_authority=DevelopmentAuthority(),
            clock=FixedClock(),
            calibration=FixedCalibration(),
            acquisition=FixedAcquisition(bundle),
            decisions=decisions,
            runtime=runtime,
            server_autonomy_enabled=True,
            entry_materializer=RecordingMaterializer(materialize_error=True),
        ).run(Actor.SCHEDULER)
    )

    assert first.terminal_code == "ENTRY_FILLED_MATERIALIZATION_FAILED"
    assert first.execution_certificate_id == UUID("00000000-0000-0000-0000-000000000804")
    duplicate_acquisition = ForbiddenAcquisition()
    repeated = asyncio.run(
        AgentRunService(
            account_authority=DevelopmentAuthority(),
            clock=FixedClock(),
            calibration=FixedCalibration(),
            acquisition=duplicate_acquisition,
            decisions=DuplicateDecisions(first),
            runtime=runtime,
            server_autonomy_enabled=True,
            entry_materializer=RecordingMaterializer(),
        ).run(Actor.SCHEDULER)
    )

    assert repeated is first
    assert runtime.execution.calls == [(INTENT_ID, Actor.SCHEDULER, NOW)]
    assert duplicate_acquisition.calls == 0


def test_entry_materialization_preparation_failure_never_executes() -> None:
    runtime = RecordingRuntime()
    result = asyncio.run(
        AgentRunService(
            account_authority=DevelopmentAuthority(),
            clock=FixedClock(),
            calibration=FixedCalibration(),
            acquisition=FixedAcquisition(approved_opportunity_bundle()),
            decisions=RecordingDecisions(return_proposal=True),
            runtime=runtime,
            server_autonomy_enabled=True,
            entry_materializer=RecordingMaterializer(prepare_error=True),
        ).run(Actor.SCHEDULER)
    )

    assert result.terminal_code == "ENTRY_MATERIALIZATION_PREPARATION_FAILED"
    assert result.execution_certificate_id is None
    assert runtime.execution.calls == []


def test_recovered_entry_continues_into_lifecycle_acquisition_without_entry_dispatch() -> None:
    materializer = RecoveringMaterializer()
    bundle = no_action_lifecycle_bundle()
    decisions = RecordingDecisions()
    result = asyncio.run(
        AgentRunService(
            account_authority=DevelopmentAuthority(),
            clock=FixedClock(),
            calibration=FixedCalibration(),
            acquisition=FixedAcquisition(bundle),
            decisions=decisions,
            runtime=ForbiddenRuntime(),
            server_autonomy_enabled=True,
            entry_materializer=materializer,
        ).run(Actor.SCHEDULER)
    )

    assert result.terminal_code == "NO_ACTION"
    assert decisions.decisions[0].normalized_input is bundle.values
    assert materializer.recoveries == [("DEVELOPMENT", FINGERPRINT)]
    assert materializer.prepares == []
    assert materializer.calls == []


def test_prepared_entry_recovery_advances_exact_intent_before_new_acquisition() -> None:
    materializer = PendingExecutionMaterializer()
    runtime = RecordingRuntime()
    acquisition = FixedAcquisition(no_action_lifecycle_bundle())

    result = asyncio.run(
        AgentRunService(
            account_authority=DevelopmentAuthority(),
            clock=FixedClock(),
            calibration=FixedCalibration(),
            acquisition=acquisition,
            decisions=RecordingDecisions(),
            runtime=runtime,
            server_autonomy_enabled=True,
            entry_materializer=materializer,
        ).run(Actor.SCHEDULER)
    )

    assert result.terminal_code == "NO_ACTION"
    assert runtime.execution.calls == [(INTENT_ID, Actor.SCHEDULER, NOW)]
    assert materializer.recovery_calls == 2
    assert acquisition.calls == 1


def test_authorized_submission_recovers_pending_entry_before_acquisition() -> None:
    materializer = PendingExecutionMaterializer()
    decisions = RecordingDecisions()

    class PreviewOrderedRuntime(RecordingRuntime):
        class Execution(RecordingRuntime.Execution):
            def execute(self, intent_id: UUID, actor: Actor, now: datetime):
                assert decisions.submission_previews == [intent_id]
                return super().execute(intent_id, actor, now)

    runtime = PreviewOrderedRuntime()
    result = asyncio.run(
        AgentRunService(
            account_authority=FixedAuthority(),
            clock=FixedClock(),
            calibration=FixedCalibration(),
            acquisition=FailingAcquisition(AcquisitionKind.OPPORTUNITY),
            decisions=decisions,
            runtime=runtime,
            server_autonomy_enabled=True,
            submission_opportunity_enabled=True,
            entry_materializer=materializer,
        ).run(Actor.SCHEDULER)
    )

    assert result.terminal_code == "PROVIDER_FAILURE_NO_TRADE"
    assert runtime.execution.calls == [(INTENT_ID, Actor.SCHEDULER, NOW)]
    assert decisions.submission_previews == [INTENT_ID]
    assert materializer.recovery_calls == 2


def test_submission_recovery_fails_closed_when_durable_preview_is_unavailable() -> None:
    materializer = PendingExecutionMaterializer()

    class UnavailablePreviewDecisions(RecordingDecisions):
        def submission_order_preview(self, intent_id: UUID):
            raise ExecutionBlocked("SUBMISSION_ORDER_PREVIEW_UNAVAILABLE")

    runtime = RecordingRuntime()
    acquisition = ForbiddenAcquisition()
    with pytest.raises(ExecutionBlocked, match="SUBMISSION_ORDER_PREVIEW_UNAVAILABLE"):
        asyncio.run(
            AgentRunService(
                account_authority=FixedAuthority(),
                clock=FixedClock(),
                calibration=FixedCalibration(),
                acquisition=acquisition,
                decisions=UnavailablePreviewDecisions(),
                runtime=runtime,
                server_autonomy_enabled=True,
                submission_opportunity_enabled=True,
                entry_materializer=materializer,
            ).run(Actor.SCHEDULER)
        )

    assert runtime.execution.calls == []
    assert acquisition.calls == 0


@pytest.mark.parametrize("pending_code", tuple(ExecutionPendingCode))
def test_pending_prepared_entry_persists_accepted_tick_then_recovers_on_later_tick(
    pending_code: ExecutionPendingCode,
) -> None:
    runtime = PendingThenRecoveredRuntime(pending_code)
    materializer = PendingUntilExecutionCompletesMaterializer(runtime)
    acquisition = FailingAcquisition(AcquisitionKind.OPPORTUNITY)
    decisions = RecordingDecisions()

    class AdvancingClock:
        values = iter((NOW, NOW + timedelta(minutes=5)))

        def now(self) -> datetime:
            return next(self.values)

    service = AgentRunService(
        account_authority=FixedAuthority(),
        clock=AdvancingClock(),
        calibration=FixedCalibration(),
        acquisition=acquisition,
        decisions=decisions,
        runtime=runtime,
        server_autonomy_enabled=True,
        submission_opportunity_enabled=True,
        entry_materializer=materializer,
    )

    pending = asyncio.run(service.run(Actor.SCHEDULER))

    assert pending.terminal_code == "ENTRY_EXECUTION_RECOVERY_PENDING"
    assert pending.decision.code == "ENTRY_EXECUTION_RECOVERY_PENDING"
    assert pending.approved_intent_id is None
    assert decisions.proposals == [None]
    assert decisions.completion_reservations == [RESERVATION_ID]
    assert decisions.submission_previews == [INTENT_ID]
    assert acquisition.calls == 0
    assert materializer.recovery_calls == 1

    recovered = asyncio.run(service.run(Actor.SCHEDULER))

    assert recovered.terminal_code == "PROVIDER_FAILURE_NO_TRADE"
    assert runtime.execution.calls == [
        (INTENT_ID, Actor.SCHEDULER, NOW),
        (INTENT_ID, Actor.SCHEDULER, NOW + timedelta(minutes=5)),
    ]
    assert decisions.submission_previews == [INTENT_ID, INTENT_ID]
    assert materializer.recovery_calls == 3


def test_nonpending_entry_execution_block_still_fails_closed() -> None:
    materializer = PendingExecutionMaterializer()
    acquisition = ForbiddenAcquisition()

    class RejectedRuntime:
        class Execution:
            def execute(self, *_args, **_kwargs):
                raise ExecutionBlocked("BROKER_PREFLIGHT_BLOCKED")

        execution = Execution()

    with pytest.raises(ExecutionBlocked, match="BROKER_PREFLIGHT_BLOCKED"):
        asyncio.run(
            AgentRunService(
                account_authority=DevelopmentAuthority(),
                clock=FixedClock(),
                calibration=FixedCalibration(),
                acquisition=acquisition,
                decisions=RecordingDecisions(),
                runtime=RejectedRuntime(),
                server_autonomy_enabled=True,
                entry_materializer=materializer,
            ).run(Actor.SCHEDULER)
        )

    assert acquisition.calls == 0
    assert materializer.recovery_calls == 1


def test_owner_cannot_advance_a_pending_entry() -> None:
    materializer = PendingExecutionMaterializer()
    acquisition = ForbiddenAcquisition()
    runtime = RecordingRuntime()

    with pytest.raises(
        ExecutionBlocked,
        match="ENTRY_EXECUTION_RECOVERY_SCHEDULER_REQUIRED",
    ):
        asyncio.run(
            AgentRunService(
                account_authority=DevelopmentAuthority(),
                clock=FixedClock(),
                calibration=FixedCalibration(),
                acquisition=acquisition,
                decisions=RecordingDecisions(),
                runtime=runtime,
                server_autonomy_enabled=True,
                entry_materializer=materializer,
            ).run(Actor.OWNER)
        )

    assert runtime.execution.calls == []
    assert acquisition.calls == 0


def test_multiple_pending_entries_fail_closed_without_broker_mutation() -> None:
    class ConflictingMaterializer(PendingExecutionMaterializer):
        def pending_execution_intents(self, *, account_role, account_fingerprint):
            return (INTENT_ID, UUID(int=999))

    acquisition = ForbiddenAcquisition()
    runtime = RecordingRuntime()
    with pytest.raises(ExecutionBlocked, match="ENTRY_EXECUTION_RECOVERY_CONFLICT"):
        asyncio.run(
            AgentRunService(
                account_authority=DevelopmentAuthority(),
                clock=FixedClock(),
                calibration=FixedCalibration(),
                acquisition=acquisition,
                decisions=RecordingDecisions(),
                runtime=runtime,
                server_autonomy_enabled=True,
                entry_materializer=ConflictingMaterializer(),
            ).run(Actor.SCHEDULER)
        )

    assert runtime.execution.calls == []
    assert acquisition.calls == 0


@pytest.mark.parametrize(
    "status",
    (
        "REJECTED",
        "CANCELED",
        "EXPIRED",
        "REPLACED",
        "PARTIAL_CANCELED_RECONCILED",
        "PARTIAL_EXPIRED_RECONCILED",
        "PARTIAL_REPLACED_RECONCILED",
    ),
)
def test_nonfilled_entry_terminal_never_materializes(status: str) -> None:
    materializer = RecordingMaterializer()
    result = asyncio.run(
        AgentRunService(
            account_authority=DevelopmentAuthority(),
            clock=FixedClock(),
            calibration=FixedCalibration(),
            acquisition=FixedAcquisition(approved_opportunity_bundle()),
            decisions=RecordingDecisions(return_proposal=True),
            runtime=RecordingRuntime(status),
            server_autonomy_enabled=True,
            entry_materializer=materializer,
        ).run(Actor.SCHEDULER)
    )

    assert result.terminal_code == status
    assert materializer.calls == []


def test_future_launch_boundary_rejects_entry_before_dispatch() -> None:
    bundle = approved_opportunity_bundle()
    assert bundle.launch_authority is not None
    bundle = replace(
        bundle,
        launch_authority=replace(
            bundle.launch_authority,
            entry_boundary_at=NOW + timedelta(seconds=1),
        ),
    )
    runtime = RecordingRuntime()
    result = asyncio.run(
        AgentRunService(
            account_authority=DevelopmentAuthority(),
            clock=FixedClock(),
            calibration=FixedCalibration(),
            acquisition=FixedAcquisition(bundle),
            decisions=RecordingDecisions(return_proposal=True),
            runtime=runtime,
            server_autonomy_enabled=True,
            entry_materializer=RecordingMaterializer(),
        ).run(Actor.SCHEDULER)
    )

    assert result.terminal_code == "ENTRY_APPROVED_WITHOUT_INTENT"
    assert runtime.execution.calls == []


@pytest.mark.parametrize(
    "substitution",
    ("vertical", "event", "day", "minimum_limit", "maximum_limit", "approved_max_loss"),
)
def test_entry_proposal_rejects_substituted_material(substitution: str) -> None:
    approved = approved_opportunity_bundle()
    assert approved.proposal is not None
    envelope = approved.proposal.intent.envelope
    if substitution == "vertical":
        envelope = replace(
            envelope,
            legs=(
                replace(envelope.legs[0], symbol="OTHER260918C00100000"),
                envelope.legs[1],
            ),
        )
    elif substitution == "event":
        envelope = replace(envelope, event_key="OTHER_EVENT")
    elif substitution == "day":
        envelope = replace(
            envelope,
            trading_day=envelope.trading_day + timedelta(days=1),
        )
    elif substitution == "minimum_limit":
        envelope = replace(envelope, minimum_limit=envelope.minimum_limit - Decimal("0.01"))
    elif substitution == "maximum_limit":
        envelope = replace(envelope, maximum_limit=envelope.maximum_limit + Decimal("0.01"))
    else:
        envelope = replace(
            envelope,
            approved_max_loss=envelope.approved_max_loss + Decimal("1"),
        )
    bundle = with_entry_envelope(approved, envelope)
    decisions = RecordingDecisions(return_proposal=True)
    runtime = RecordingRuntime()
    service = AgentRunService(
        account_authority=DevelopmentAuthority(),
        clock=FixedClock(),
        calibration=FixedCalibration(),
        acquisition=FixedAcquisition(bundle),
        decisions=decisions,
        runtime=runtime,
        server_autonomy_enabled=True,
    )

    result = asyncio.run(service.run(Actor.SCHEDULER))

    assert result.terminal_code == "ENTRY_APPROVED_WITHOUT_INTENT"
    assert decisions.proposals == [None]
    assert runtime.execution.calls == []


def test_incomplete_lifecycle_bundle_persists_deterministic_no_action() -> None:
    bundle = no_action_lifecycle_bundle()
    decisions = RecordingDecisions()
    service = AgentRunService(
        account_authority=DevelopmentAuthority(),
        clock=FixedClock(),
        calibration=FixedCalibration(),
        acquisition=FixedAcquisition(bundle),
        decisions=decisions,
        runtime=ForbiddenRuntime(),
        server_autonomy_enabled=True,
    )

    result = asyncio.run(service.run(Actor.SCHEDULER))

    assert result.terminal_code == "NO_ACTION"
    assert result.decision.code == "NO_ACTION"
    assert result.decision.lifecycle.response.rationale_code == "EXECUTION_DATA_MISSING"
    assert decisions.decisions[0].normalized_input is bundle.values
    assert decisions.proposals == [None]


def test_complete_unknown_lifecycle_can_dispatch_mandatory_risk_close() -> None:
    bundle = risk_close_lifecycle_bundle()
    decisions = RecordingDecisions(return_proposal=True)
    runtime = RecordingLifecycleRuntime()
    service = AgentRunService(
        account_authority=DevelopmentAuthority(),
        clock=FixedClock(),
        calibration=FixedCalibration(),
        acquisition=FixedAcquisition(bundle),
        decisions=decisions,
        runtime=runtime,
        server_autonomy_enabled=True,
    )

    result = asyncio.run(service.run(Actor.SCHEDULER))

    assert result.decision.code == "CLOSE_RISK_ONLY"
    assert result.decision.lifecycle.response.rationale_code == "CONTEST_END_CLOSE"
    assert result.approved_intent_id == LIFECYCLE_INTENT_ID
    assert runtime.execution.calls == [(LIFECYCLE_INTENT_ID, Actor.SCHEDULER, NOW)]


def test_filled_lifecycle_materializes_before_tick_completion() -> None:
    decisions = RecordingDecisions(return_proposal=True)
    runtime = RecordingLifecycleRuntime("FILLED")
    materializer = RecordingLifecycleTerminalMaterializer(decisions=decisions)

    result = asyncio.run(
        AgentRunService(
            account_authority=DevelopmentAuthority(),
            clock=FixedClock(),
            calibration=FixedCalibration(),
            acquisition=FixedAcquisition(risk_close_lifecycle_bundle()),
            decisions=decisions,
            runtime=runtime,
            server_autonomy_enabled=True,
            lifecycle_terminal_materializer=materializer,
        ).run(Actor.SCHEDULER)
    )

    assert result.terminal_code == "FILLED"
    assert result.execution_certificate_id == UUID("00000000-0000-0000-0000-000000000809")
    assert materializer.calls == [result.execution_certificate_id]
    assert decisions.terminals == ["FILLED"]

    repeated = asyncio.run(
        AgentRunService(
            account_authority=DevelopmentAuthority(),
            clock=FixedClock(),
            calibration=FixedCalibration(),
            acquisition=ForbiddenAcquisition(),
            decisions=DuplicateDecisions(result),
            runtime=runtime,
            server_autonomy_enabled=True,
            lifecycle_terminal_materializer=materializer,
        ).run(Actor.SCHEDULER)
    )

    assert repeated is result
    assert runtime.execution.calls == [(LIFECYCLE_INTENT_ID, Actor.SCHEDULER, NOW)]
    assert materializer.calls == [result.execution_certificate_id]


@pytest.mark.parametrize(
    "status",
    (
        "REJECTED",
        "CANCELED",
        "EXPIRED",
        "REPLACED",
        "PARTIAL_CANCELED_RECONCILED",
        "PARTIAL_EXPIRED_RECONCILED",
        "PARTIAL_REPLACED_RECONCILED",
    ),
)
def test_nonfilled_lifecycle_terminal_never_materializes(status: str) -> None:
    materializer = RecordingLifecycleTerminalMaterializer()
    result = asyncio.run(
        AgentRunService(
            account_authority=DevelopmentAuthority(),
            clock=FixedClock(),
            calibration=FixedCalibration(),
            acquisition=FixedAcquisition(risk_close_lifecycle_bundle()),
            decisions=RecordingDecisions(return_proposal=True),
            runtime=RecordingLifecycleRuntime(status),
            server_autonomy_enabled=True,
            lifecycle_terminal_materializer=materializer,
        ).run(Actor.SCHEDULER)
    )

    assert result.terminal_code == status
    assert result.execution_certificate_id is not None
    assert materializer.calls == []


@pytest.mark.parametrize(
    ("entry_approval_id", "assessment_certificate_id"),
    (
        (AUTHORIZATION_ID, None),
        (None, UUID("00000000-0000-0000-0000-000000000899")),
    ),
)
def test_filled_lifecycle_rejects_substituted_certificate_authority(
    entry_approval_id: UUID | None,
    assessment_certificate_id: UUID | None,
) -> None:
    materializer = RecordingLifecycleTerminalMaterializer()
    result = asyncio.run(
        AgentRunService(
            account_authority=DevelopmentAuthority(),
            clock=FixedClock(),
            calibration=FixedCalibration(),
            acquisition=FixedAcquisition(risk_close_lifecycle_bundle()),
            decisions=RecordingDecisions(return_proposal=True),
            runtime=RecordingLifecycleRuntime(
                "FILLED",
                entry_approval_id=entry_approval_id,
                assessment_certificate_id=assessment_certificate_id,
            ),
            server_autonomy_enabled=True,
            lifecycle_terminal_materializer=materializer,
        ).run(Actor.SCHEDULER)
    )

    assert result.terminal_code == "LIFECYCLE_FILLED_MATERIALIZATION_FAILED"
    assert result.execution_certificate_id == UUID("00000000-0000-0000-0000-000000000809")
    assert materializer.calls == []


def test_filled_lifecycle_rejects_certificate_for_another_intent() -> None:
    materializer = RecordingLifecycleTerminalMaterializer()
    result = asyncio.run(
        AgentRunService(
            account_authority=DevelopmentAuthority(),
            clock=FixedClock(),
            calibration=FixedCalibration(),
            acquisition=FixedAcquisition(risk_close_lifecycle_bundle()),
            decisions=RecordingDecisions(return_proposal=True),
            runtime=RecordingLifecycleRuntime(
                "FILLED",
                returned_intent_id=UUID("00000000-0000-0000-0000-000000000897"),
            ),
            server_autonomy_enabled=True,
            lifecycle_terminal_materializer=materializer,
        ).run(Actor.SCHEDULER)
    )

    assert result.terminal_code == "LIFECYCLE_FILLED_MATERIALIZATION_FAILED"
    assert result.execution_certificate_id == UUID("00000000-0000-0000-0000-000000000809")
    assert materializer.calls == []


def test_filled_lifecycle_materialization_failure_is_terminal_and_not_redispatched() -> None:
    runtime = RecordingLifecycleRuntime("FILLED")
    decisions = RecordingDecisions(return_proposal=True)
    first = asyncio.run(
        AgentRunService(
            account_authority=DevelopmentAuthority(),
            clock=FixedClock(),
            calibration=FixedCalibration(),
            acquisition=FixedAcquisition(risk_close_lifecycle_bundle()),
            decisions=decisions,
            runtime=runtime,
            server_autonomy_enabled=True,
            lifecycle_terminal_materializer=RecordingLifecycleTerminalMaterializer(fail=True),
        ).run(Actor.SCHEDULER)
    )

    assert first.terminal_code == "LIFECYCLE_FILLED_MATERIALIZATION_FAILED"
    assert first.execution_certificate_id == UUID("00000000-0000-0000-0000-000000000809")
    duplicate_acquisition = ForbiddenAcquisition()
    repeated = asyncio.run(
        AgentRunService(
            account_authority=DevelopmentAuthority(),
            clock=FixedClock(),
            calibration=FixedCalibration(),
            acquisition=duplicate_acquisition,
            decisions=DuplicateDecisions(first),
            runtime=runtime,
            server_autonomy_enabled=True,
            lifecycle_terminal_materializer=RecordingLifecycleTerminalMaterializer(),
        ).run(Actor.SCHEDULER)
    )

    assert repeated is first
    assert runtime.execution.calls == [(LIFECYCLE_INTENT_ID, Actor.SCHEDULER, NOW)]
    assert duplicate_acquisition.calls == 0


@pytest.mark.parametrize("kind", ("opportunity", "lifecycle"))
def test_proposal_rejects_authorization_for_a_different_valid_thesis(kind: str) -> None:
    bundle = (
        approved_opportunity_bundle() if kind == "opportunity" else risk_close_lifecycle_bundle()
    )
    assert bundle.proposal is not None
    bundle = replace(
        bundle,
        proposal=replace(
            bundle.proposal,
            authorization=replace(
                bundle.proposal.authorization,
                thesis_version_id=UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
            ),
        ),
    )
    decisions = RecordingDecisions(return_proposal=True)
    runtime = RecordingRuntime() if kind == "opportunity" else RecordingLifecycleRuntime()
    service = AgentRunService(
        account_authority=DevelopmentAuthority(),
        clock=FixedClock(),
        calibration=FixedCalibration(),
        acquisition=FixedAcquisition(bundle),
        decisions=decisions,
        runtime=runtime,
        server_autonomy_enabled=True,
    )

    result = asyncio.run(service.run(Actor.SCHEDULER))

    assert result.terminal_code in {
        "ENTRY_APPROVED_WITHOUT_INTENT",
        "ACTION_APPROVED_WITHOUT_INTENT",
    }
    assert decisions.proposals == [None]
    assert runtime.execution.calls == []


@pytest.mark.parametrize("substitution", ("assessment", "expected_exposure", "action"))
def test_lifecycle_proposal_rejects_substituted_material(substitution: str) -> None:
    approved = risk_close_lifecycle_bundle()
    assert approved.proposal is not None
    authorization = approved.proposal.authorization
    if substitution == "assessment":
        authorization = replace(
            authorization,
            assessment_id=UUID("00000000-0000-0000-0000-000000000812"),
        )
    elif substitution == "expected_exposure":
        authorization = replace(
            authorization,
            expected_after_exposure=GreekExposure(
                delta=Decimal("0"),
                gamma=Decimal("0"),
                theta_per_day=Decimal("0"),
                vega_per_iv_point=Decimal("0"),
            ),
        )
    if substitution == "action":
        intent = replace(
            approved.proposal.intent,
            envelope=replace(
                approved.proposal.intent.envelope,
                action=ExecutionAction.ENTRY,
            ),
        )
        bundle = replace(approved, proposal=replace(approved.proposal, intent=intent))
    else:
        bundle = replace(
            approved,
            proposal=replace(approved.proposal, authorization=authorization),
        )
    decisions = RecordingDecisions(return_proposal=True)
    runtime = RecordingLifecycleRuntime()
    service = AgentRunService(
        account_authority=DevelopmentAuthority(),
        clock=FixedClock(),
        calibration=FixedCalibration(),
        acquisition=FixedAcquisition(bundle),
        decisions=decisions,
        runtime=runtime,
        server_autonomy_enabled=True,
    )

    result = asyncio.run(service.run(Actor.SCHEDULER))

    assert result.terminal_code == "ACTION_APPROVED_WITHOUT_INTENT"
    assert decisions.proposals == [None]
    assert runtime.execution.calls == []


def test_deterministic_opportunity_no_trade_never_persists_an_intent() -> None:
    approved = approved_opportunity_bundle()
    values = replace(approved.values, market_open=False)
    decision = evaluate_opportunity(approved.policy, values)
    bundle = OpportunityNoTradeAcquisition(
        approved.policy,
        values,
        decision,
    )
    decisions = RecordingDecisions(return_proposal=True)
    service = AgentRunService(
        account_authority=DevelopmentAuthority(),
        clock=FixedClock(),
        calibration=FixedCalibration(),
        acquisition=FixedAcquisition(bundle),
        decisions=decisions,
        runtime=ForbiddenRuntime(),
        server_autonomy_enabled=True,
    )

    result = asyncio.run(service.run(Actor.SCHEDULER))

    assert result.terminal_code == "NO_TRADE"
    assert result.decision.opportunity == decision
    assert result.decision.thesis_version_id is None
    assert result.approved_intent_id is None
    assert decisions.proposals == [None]


def test_no_trade_acquisition_rejects_a_substituted_decision() -> None:
    approved = approved_opportunity_bundle()
    values = replace(approved.values, market_open=False)

    with pytest.raises(ValueError, match="OPPORTUNITY_NO_TRADE_ACQUISITION_INVALID"):
        OpportunityNoTradeAcquisition(
            approved.policy,
            values,
            evaluate_opportunity(approved.policy, approved.values),
        )

    with pytest.raises(ValueError, match="OPPORTUNITY_APPROVED_ACQUISITION_INVALID"):
        OpportunityAcquisition(approved.policy, values, THESIS_VERSION_ID)


def test_dispatch_requires_scheduler_and_both_autonomy_gates() -> None:
    for actor in Actor:
        for server_enabled, persistent_enabled in (
            (False, False),
            (False, True),
            (True, False),
            (True, True),
        ):
            should_dispatch = actor is Actor.SCHEDULER and server_enabled and persistent_enabled
            bundle = approved_opportunity_bundle()
            decisions = RecordingDecisions(return_proposal=True)
            runtime = RecordingRuntime()
            service = AgentRunService(
                account_authority=DevelopmentAuthority(
                    persistent_autonomy_enabled=persistent_enabled
                ),
                clock=FixedClock(),
                calibration=FixedCalibration(),
                acquisition=FixedAcquisition(bundle),
                decisions=decisions,
                runtime=runtime,
                server_autonomy_enabled=server_enabled,
                entry_materializer=RecordingMaterializer(),
            )

            result = asyncio.run(service.run(actor))

            assert bool(runtime.execution.calls) is should_dispatch
            assert (result.approved_intent_id is not None) is should_dispatch
            assert decisions.proposals == [bundle.proposal if should_dispatch else None]


def test_owner_assesses_approved_entry_without_intent_or_dispatch() -> None:
    bundle = approved_opportunity_bundle()
    acquisition = FixedAcquisition(bundle)
    decisions = RecordingDecisions(return_proposal=True)
    runtime = RecordingRuntime()
    service = AgentRunService(
        account_authority=DevelopmentAuthority(),
        clock=FixedClock(),
        calibration=FixedCalibration(),
        acquisition=acquisition,
        decisions=decisions,
        runtime=runtime,
        server_autonomy_enabled=True,
    )

    result = asyncio.run(service.run(Actor.OWNER))

    assert result.decision.code == "ENTRY_APPROVED"
    assert acquisition.actors == [Actor.OWNER]
    assert result.approved_intent_id is None
    assert decisions.proposals == [None]
    assert runtime.execution.calls == []


def test_owner_assesses_mandatory_risk_close_without_intent_or_dispatch() -> None:
    bundle = risk_close_lifecycle_bundle()
    decisions = RecordingDecisions(return_proposal=True)
    runtime = RecordingLifecycleRuntime()
    service = AgentRunService(
        account_authority=DevelopmentAuthority(),
        clock=FixedClock(),
        calibration=FixedCalibration(),
        acquisition=FixedAcquisition(bundle),
        decisions=decisions,
        runtime=runtime,
        server_autonomy_enabled=True,
    )

    result = asyncio.run(service.run(Actor.OWNER))

    assert result.decision.code == "CLOSE_RISK_ONLY"
    assert result.approved_intent_id is None
    assert decisions.proposals == [None]
    assert runtime.execution.calls == []


def test_risk_close_dispatch_requires_scheduler_and_both_autonomy_gates() -> None:
    for actor in Actor:
        for server_enabled, persistent_enabled in (
            (False, False),
            (False, True),
            (True, False),
            (True, True),
        ):
            should_dispatch = actor is Actor.SCHEDULER and server_enabled and persistent_enabled
            bundle = risk_close_lifecycle_bundle()
            decisions = RecordingDecisions(return_proposal=True)
            runtime = RecordingLifecycleRuntime()
            service = AgentRunService(
                account_authority=DevelopmentAuthority(
                    persistent_autonomy_enabled=persistent_enabled
                ),
                clock=FixedClock(),
                calibration=FixedCalibration(),
                acquisition=FixedAcquisition(bundle),
                decisions=decisions,
                runtime=runtime,
                server_autonomy_enabled=server_enabled,
            )

            result = asyncio.run(service.run(actor))

            assert bool(runtime.execution.calls) is should_dispatch
            assert (result.approved_intent_id is not None) is should_dispatch
            assert decisions.proposals == [bundle.proposal if should_dispatch else None]


def test_completed_tick_returns_durable_result_without_duplicate_acquisition() -> None:
    decision = AgentDecision("NO_ACTION", NOW)
    completed = AgentRunResult(
        tick_id=TICK_ID,
        terminal_code="NO_ACTION",
        decision=decision,
        approved_intent_id=None,
        execution_certificate_id=None,
        proof_hash="d" * 64,
    )
    acquisition = ForbiddenAcquisition()
    service = AgentRunService(
        account_authority=DevelopmentAuthority(),
        clock=FixedClock(),
        calibration=FixedCalibration(),
        acquisition=acquisition,
        decisions=DuplicateDecisions(completed),
        runtime=ForbiddenRuntime(),
        server_autonomy_enabled=True,
    )

    result = asyncio.run(service.run(Actor.SCHEDULER))

    assert result is completed
    assert acquisition.calls == 0


def test_runtime_execution_block_records_terminal_tick_proof() -> None:
    decisions = RecordingDecisions(return_proposal=True)
    service = AgentRunService(
        account_authority=DevelopmentAuthority(),
        clock=FixedClock(),
        calibration=FixedCalibration(),
        acquisition=FixedAcquisition(approved_opportunity_bundle()),
        decisions=decisions,
        runtime=BlockedRuntime(),
        server_autonomy_enabled=True,
        entry_materializer=RecordingMaterializer(),
    )

    result = asyncio.run(service.run(Actor.SCHEDULER))

    assert result.terminal_code == "EXECUTION_BLOCKED"
    assert result.decision.code == "ENTRY_APPROVED"
    assert result.proof_hash == "d" * 64


def test_agent_codec_round_trips_all_persisted_policy_types_canonically() -> None:
    opportunity = approved_opportunity_bundle()
    opportunity_result = evaluate_opportunity(opportunity.policy, opportunity.values)
    lifecycle = risk_close_lifecycle_bundle()
    lifecycle_result = evaluate_assessment(lifecycle.values)
    calibration = FixedCalibration().binding_for(FixedAuthority().observe())
    experiment_lineage = ExperimentExecutionLineage(
        UUID("90000000-0000-0000-0000-000000000001"),
        "6" * 64,
        "7" * 64,
    )

    for value in (
        calibration,
        experiment_lineage,
        opportunity.values,
        opportunity_result,
        lifecycle.values,
        lifecycle_result,
    ):
        encoded = encode_agent_value(value)
        assert decode_agent_value(encoded) == value
        assert encode_agent_value(decode_agent_value(encoded)) == encoded


def test_agent_codec_legacy_decision_stays_parent_byte_compatible() -> None:
    decision = AgentDecision(
        code="NO_ACTION",
        decided_at=datetime(2026, 9, 1, 17, tzinfo=UTC),
    )

    encoded = encode_agent_value(decision)

    assert encoded == {
        "codec": "alphadecay.agent-value.v1",
        "value": {
            "$type": "dataclass",
            "class": "backend.app.services.agent.AgentDecision",
            "fields": {
                "code": "NO_ACTION",
                "decided_at": {
                    "$type": "datetime",
                    "value": "2026-09-01T17:00:00+00:00",
                },
                "thesis_version_id": None,
                "calibration": None,
                "submission_authority": None,
                "opportunity": None,
                "lifecycle": None,
                "provider_failure_code": None,
                "provider_failure_kind": None,
                "normalized_input": None,
            },
        },
    }
    assert decode_agent_value(encoded) == decision
    result_hash = canonical_agent_hash(
        agent_result_material(
            input_hash="1" * 64,
            outcome="NO_ACTION",
            reason_code="NO_ACTION",
            policy_hash="2" * 64,
            thesis_version_id=None,
            result_payload={"typed": encoded},
            authorization_id=None,
            intent_id=None,
            intent_digest=None,
            autonomy_authorized=False,
        )
    )
    assert result_hash == "1c3f87a48f4b21b8ac334a5610875a29a0c8bf9568907ca5cd1474a643bafa6f"
    assert uuid5(NAMESPACE_URL, f"alphadecay:agent-decision:{result_hash}") == UUID(
        "33eb4d91-1a87-540b-9735-0f93ec8c705b"
    )
    lineaged = replace(
        decision,
        experiment_lineage=ExperimentExecutionLineage(
            UUID("90000000-0000-0000-0000-000000000001"),
            "6" * 64,
            "7" * 64,
        ),
    )
    lineaged_payload = encode_agent_value(lineaged)
    assert "experiment_lineage" in lineaged_payload["value"]["fields"]
    assert decode_agent_value(lineaged_payload) == lineaged

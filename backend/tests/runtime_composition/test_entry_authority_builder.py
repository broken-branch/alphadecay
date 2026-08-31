from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from backend.app.contracts.v1 import AccountRole, DataQuality, OptionRight, PositionIntent
from backend.app.execution import Actor, ExecutionCertificate, OrderLegIntent
from backend.app.policy import (
    AccountOpportunityState,
    CatalystQuality,
    InstrumentKind,
    OpportunityInput,
    OpportunityPolicy,
    OptionFeed,
    OptionLeg,
    VerticalCandidate,
    VerticalStrategy,
    evaluate_opportunity,
)
from backend.app.policy.opportunity import TradingHaltState
from backend.app.services import (
    AgentRunResult,
    AgentRunService,
    AgentTick,
    EntryProposalAuthorityInput,
    ObservedPaperAccountAuthority,
    PermanentAccountLatch,
    PersistedAgentDecision,
    build_development_entry_proposal,
)

BOUNDARY = datetime(2026, 8, 31, 14, 30, tzinfo=UTC)
TRUSTED_AT = BOUNDARY + timedelta(seconds=2)
FINGERPRINT = "a" * 64
BOOK_FINGERPRINT = "b" * 64
THESIS_VERSION_ID = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
TICK_ID = UUID("00000000-0000-0000-0000-000000000901")
RESERVATION_ID = UUID("00000000-0000-0000-0000-000000000902")


def _policy() -> OpportunityPolicy:
    return OpportunityPolicy(
        version="entry-builder-test-v1",
        opportunity_key="ACME_EVENT",
        underlying="ACME",
        selected_decision_boundary=BOUNDARY,
        last_entry_boundary=BOUNDARY + timedelta(hours=1),
        maximum_decision_delay=timedelta(seconds=30),
        maximum_underlying_age=timedelta(seconds=30),
        maximum_catalyst_age=timedelta(minutes=15),
        maximum_option_quote_age=timedelta(seconds=30),
        maximum_leg_quote_skew=timedelta(seconds=5),
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


def _leg(symbol: str, strike: str, intent: PositionIntent, bid: str, ask: str) -> OptionLeg:
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
        ask_size=10,
        quote_at=TRUSTED_AT,
        feed=OptionFeed.INDICATIVE_MODIFIED,
        greeks_complete=True,
        greek_units_verified=True,
    )


def _values() -> OpportunityInput:
    return OpportunityInput(
        opportunity_key="ACME_EVENT",
        underlying="ACME",
        observed_decision_boundary=BOUNDARY,
        evaluated_at=TRUSTED_AT,
        completed_bar_at=BOUNDARY,
        decision_boundary_complete=True,
        prior_decision_outcome=None,
        data_quality=DataQuality.COMPLETE,
        market_open=True,
        trading_halted=TradingHaltState.NOT_HALTED,
        underlying_observed_at=TRUSTED_AT,
        catalyst_observed_at=TRUSTED_AT,
        catalyst_quality=CatalystQuality.CLEAR,
        catalyst_score=25,
        vwap_distance=Decimal("0.01"),
        relative_return=Decimal("0.008"),
        beta=Decimal("1.2"),
        bull_trend_hits=3,
        bear_trend_hits=0,
        absolute_first_reaction=Decimal("0.08"),
        candidate=VerticalCandidate(
            strategy=VerticalStrategy.BULL_CALL_DEBIT,
            legs=(
                _leg(
                    "ACME260918C00100000",
                    "100",
                    PositionIntent.BUY_TO_OPEN,
                    "2.00",
                    "2.10",
                ),
                _leg(
                    "ACME260918C00105000",
                    "105",
                    PositionIntent.SELL_TO_OPEN,
                    "0.90",
                    "0.94",
                ),
            ),
            quantity=4,
            dte=18,
            approved_limit=Decimal("1.20"),
            candidate_score=82,
            selection_rank=1,
            buying_power_sufficient=True,
        ),
        account=AccountOpportunityState(
            account_role=AccountRole.DEVELOPMENT,
            book_fingerprint=BOOK_FINGERPRINT,
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


def _inputs() -> EntryProposalAuthorityInput:
    policy = _policy()
    values = _values()
    decision = evaluate_opportunity(policy, values)
    return EntryProposalAuthorityInput(
        policy=policy,
        values=values,
        decision=decision,
        thesis_version_id=THESIS_VERSION_ID,
        thesis_account_role=AccountRole.DEVELOPMENT,
        thesis_policy_hash=decision.policy_hash,
        thesis_underlying="ACME",
        thesis_frozen_at=BOUNDARY + timedelta(seconds=1),
        account_role=AccountRole.DEVELOPMENT,
        account_fingerprint=FINGERPRINT,
        valid_from=TRUSTED_AT,
        expires_at=TRUSTED_AT + timedelta(seconds=30),
        benchmark_symbol="QQQ",
        underlying_source_hash="1" * 64,
        benchmark_source_hash="2" * 64,
        completed_bar_source_hash="3" * 64,
    )


def test_builder_is_exact_and_replay_stable() -> None:
    first = build_development_entry_proposal(_inputs())
    replay = build_development_entry_proposal(_inputs())

    assert replay == first
    assert first.proposal.authorization is first.authorization
    assert first.proposal.intent is first.intent
    assert first.acquisition.proposal is first.proposal
    assert first.acquisition.launch_authority is first.launch_authority
    assert first.envelope.authorization_certificate_id == first.authorization.approval_id
    assert first.envelope.account_fingerprint == FINGERPRINT
    assert first.envelope.position_or_book_fingerprint == BOOK_FINGERPRINT
    assert first.authorization.book_fingerprint == BOOK_FINGERPRINT
    assert first.launch_authority.entry_policy_hash == first.authorization.policy_hash
    assert first.authorization.approval_id != first.intent.intent_id


def test_account_and_observed_book_authorities_are_independent_and_bound() -> None:
    original = build_development_entry_proposal(_inputs())
    changed_account = build_development_entry_proposal(
        replace(_inputs(), account_fingerprint="c" * 64)
    )
    inputs = _inputs()
    changed_values = replace(
        inputs.values,
        account=replace(inputs.values.account, book_fingerprint="d" * 64),
    )
    changed_book = build_development_entry_proposal(
        replace(
            inputs,
            values=changed_values,
            decision=evaluate_opportunity(inputs.policy, changed_values),
        )
    )

    assert original.envelope.account_fingerprint == FINGERPRINT
    assert original.envelope.position_or_book_fingerprint == BOOK_FINGERPRINT
    assert changed_account.envelope.account_fingerprint == "c" * 64
    assert changed_account.envelope.position_or_book_fingerprint == BOOK_FINGERPRINT
    assert changed_book.envelope.account_fingerprint == FINGERPRINT
    assert changed_book.envelope.position_or_book_fingerprint == "d" * 64
    assert changed_book.authorization.book_fingerprint == "d" * 64
    assert (
        len(
            {
                original.authorization.approval_id,
                changed_account.authorization.approval_id,
                changed_book.authorization.approval_id,
            }
        )
        == 3
    )
    assert (
        len(
            {
                original.intent.intent_id,
                changed_account.intent.intent_id,
                changed_book.intent.intent_id,
            }
        )
        == 3
    )


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: replace(
            value,
            thesis_frozen_at=value.thesis_frozen_at + timedelta(microseconds=1),
        ),
        lambda value: replace(value, valid_from=value.valid_from + timedelta(microseconds=1)),
        lambda value: replace(value, expires_at=value.expires_at - timedelta(microseconds=1)),
        lambda value: replace(value, underlying_source_hash="4" * 64),
        lambda value: replace(value, benchmark_source_hash="5" * 64),
        lambda value: replace(value, completed_bar_source_hash="6" * 64),
    ),
)
def test_approval_identity_binds_time_and_source_authority(mutate) -> None:
    inputs = _inputs()
    changed_inputs = mutate(inputs)
    if changed_inputs.valid_from != inputs.valid_from:
        changed_inputs = replace(
            changed_inputs,
            values=replace(inputs.values, evaluated_at=changed_inputs.valid_from),
        )
        changed_inputs = replace(
            changed_inputs,
            decision=evaluate_opportunity(changed_inputs.policy, changed_inputs.values),
        )

    original = build_development_entry_proposal(inputs)
    changed = build_development_entry_proposal(changed_inputs)

    assert changed.authorization.approval_id != original.authorization.approval_id
    assert changed.intent.intent_id != original.intent.intent_id


def test_envelope_matches_the_exact_approved_candidate_and_risk() -> None:
    inputs = _inputs()
    built = build_development_entry_proposal(inputs)
    candidate = inputs.values.candidate
    assert candidate is not None

    assert built.envelope.legs == tuple(
        OrderLegIntent(leg.symbol, leg.intent, leg.ratio) for leg in candidate.legs
    )
    assert built.envelope.quantity == candidate.quantity == built.authorization.quantity
    assert built.envelope.minimum_limit == candidate.approved_limit
    assert built.envelope.maximum_limit == candidate.approved_limit
    assert (
        built.envelope.approved_max_loss
        == built.authorization.approved_max_loss
        == built.acquisition.proposal.authorization.approved_max_loss
    )


class _Authority:
    def observe(self) -> ObservedPaperAccountAuthority:
        return ObservedPaperAccountAuthority(
            role=AccountRole.DEVELOPMENT,
            account_fingerprint=FINGERPRINT,
            paper=True,
            persistent_autonomy_enabled=True,
        )


class _Clock:
    def now(self) -> datetime:
        return TRUSTED_AT


class _Acquisition:
    def __init__(self, value) -> None:
        self.value = value

    async def acquire(self, authority, trusted_at, tick_id, *, actor):
        assert authority.account_fingerprint == FINGERPRINT
        assert trusted_at == TRUSTED_AT
        assert tick_id == TICK_ID
        assert actor is Actor.SCHEDULER
        return self.value


class _Decisions:
    def __init__(self) -> None:
        self.decision = None
        self.intent = None

    def begin_tick(self, authority, actor, trusted_at):
        return AgentTick(TICK_ID, RESERVATION_ID, authority, actor, trusted_at)

    def permanent_latch(self, authority):
        return PermanentAccountLatch(False)

    def persist_decision(self, tick, decision, proposal):
        self.decision = decision
        self.intent = proposal.intent if proposal is not None else None
        return PersistedAgentDecision(decision, self.intent)

    def complete_tick(self, tick, terminal_code, certificate):
        return AgentRunResult(
            tick_id=tick.tick_id,
            terminal_code=terminal_code,
            decision=self.decision,
            approved_intent_id=self.intent.intent_id if self.intent is not None else None,
            execution_certificate_id=(
                certificate.certificate_id if certificate is not None else None
            ),
            proof_hash="4" * 64,
        )


class _Materializer:
    def __init__(self) -> None:
        self.prepared = []

    def recover_pending(self, **_kwargs):
        return ()

    def pending_execution_intents(self, **_kwargs):
        return ()

    def prepare(self, **kwargs):
        self.prepared.append(kwargs)


class _Runtime:
    class Execution:
        def __init__(self, approval_id: UUID) -> None:
            self.approval_id = approval_id
            self.calls = []

        def execute(self, intent_id, actor, now):
            self.calls.append((intent_id, actor, now))
            return ExecutionCertificate(
                certificate_id=UUID("00000000-0000-0000-0000-000000000903"),
                intent_id=intent_id,
                entry_approval_id=self.approval_id,
                assessment_certificate_id=None,
                execution_status="CANCELED",
                attempt_ids=("entry-a0",),
                actual_exposure=None,
                reconciliation_checks=("TERMINAL",),
                created_at=now,
            )

    def __init__(self, approval_id: UUID) -> None:
        self.execution = self.Execution(approval_id)


def test_builder_output_passes_agent_run_service_entry_validation() -> None:
    built = build_development_entry_proposal(_inputs())
    runtime = _Runtime(built.authorization.approval_id)
    materializer = _Materializer()
    result = asyncio.run(
        AgentRunService(
            account_authority=_Authority(),
            clock=_Clock(),
            calibration=object(),
            acquisition=_Acquisition(built.acquisition),
            decisions=_Decisions(),
            runtime=runtime,
            server_autonomy_enabled=True,
            entry_materializer=materializer,
        ).run(Actor.SCHEDULER)
    )

    assert result.terminal_code == "CANCELED"
    assert result.approved_intent_id == built.intent.intent_id
    assert runtime.execution.calls == [(built.intent.intent_id, Actor.SCHEDULER, TRUSTED_AT)]
    assert materializer.prepared[0]["launch_authority"] == built.launch_authority


def test_agent_run_service_rejects_builder_output_for_a_different_account() -> None:
    built = build_development_entry_proposal(replace(_inputs(), account_fingerprint="c" * 64))
    runtime = _Runtime(built.authorization.approval_id)
    materializer = _Materializer()
    decisions = _Decisions()

    result = asyncio.run(
        AgentRunService(
            account_authority=_Authority(),
            clock=_Clock(),
            calibration=object(),
            acquisition=_Acquisition(built.acquisition),
            decisions=decisions,
            runtime=runtime,
            server_autonomy_enabled=True,
            entry_materializer=materializer,
        ).run(Actor.SCHEDULER)
    )

    assert result.terminal_code == "ENTRY_APPROVED_WITHOUT_INTENT"
    assert result.approved_intent_id is None
    assert decisions.intent is None
    assert runtime.execution.calls == []
    assert materializer.prepared == []


@pytest.mark.parametrize(
    ("mutate", "error"),
    (
        (
            lambda value: replace(value, account_role=AccountRole.SUBMISSION),
            "ENTRY_ACCOUNT_BINDING_INVALID",
        ),
        (
            lambda value: replace(value, account_fingerprint="invalid"),
            "ENTRY_AUTHORITY_HASH_INVALID",
        ),
        (
            lambda value: replace(value, thesis_version_id="not-a-uuid"),
            "ENTRY_THESIS_BINDING_INVALID",
        ),
        (
            lambda value: replace(value, thesis_policy_hash="b" * 64),
            "ENTRY_THESIS_BINDING_INVALID",
        ),
        (
            lambda value: replace(value, thesis_underlying="OTHER"),
            "ENTRY_THESIS_BINDING_INVALID",
        ),
        (
            lambda value: replace(
                value,
                valid_from=value.thesis_frozen_at - timedelta(seconds=1),
            ),
            "ENTRY_AUTHORITY_CHRONOLOGY_INVALID",
        ),
        (
            lambda value: replace(
                value,
                expires_at=value.valid_from + timedelta(seconds=31),
            ),
            "ENTRY_AUTHORITY_CHRONOLOGY_INVALID",
        ),
        (
            lambda value: replace(
                value,
                valid_from=value.valid_from.astimezone(timezone(timedelta(hours=1))),
            ),
            "ENTRY_AUTHORITY_TIME_INVALID",
        ),
        (
            lambda value: replace(value, benchmark_symbol="SPY"),
            "ENTRY_LAUNCH_AUTHORITY_INVALID",
        ),
        (
            lambda value: replace(value, underlying_source_hash="invalid"),
            "ENTRY_AUTHORITY_HASH_INVALID",
        ),
        (
            lambda value: replace(
                value,
                decision=replace(value.decision, quantity=1),
            ),
            "ENTRY_DECISION_AUTHORITY_INVALID",
        ),
    ),
)
def test_builder_rejects_substituted_authority(mutate, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        build_development_entry_proposal(mutate(_inputs()))


@pytest.mark.parametrize(("quantity", "approved_limit"), ((0, "1.20"), (4, "-1")))
def test_builder_rejects_structurally_invalid_candidate(
    quantity: int,
    approved_limit: str,
) -> None:
    inputs = _inputs()
    assert inputs.values.candidate is not None
    candidate = replace(
        inputs.values.candidate,
        quantity=quantity,
        approved_limit=Decimal(approved_limit),
    )
    values = replace(inputs.values, candidate=candidate)
    decision = evaluate_opportunity(inputs.policy, values)

    with pytest.raises(ValueError, match="ENTRY_DECISION_AUTHORITY_INVALID"):
        build_development_entry_proposal(replace(inputs, values=values, decision=decision))

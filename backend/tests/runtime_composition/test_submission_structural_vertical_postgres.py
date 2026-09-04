from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.exc import DBAPIError
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
    PositionIntent,
    PositionListResponse,
)
from backend.app.execution import (
    AccountObservation,
    ActivityItem,
    ActivityPaginationEvidence,
    ActivityType,
    Actor,
    BrokerResult,
    InventoryItem,
    InventoryKind,
    SweepObservation,
)
from backend.app.execution.models import PositionGreekObservation
from backend.app.lifecycle.materialization import SQLAlchemyEntryMaterializer
from backend.app.lifecycle.repository import SQLAlchemyLifecycleRepository
from backend.app.lifecycle.terminal_materialization import (
    SQLAlchemyLifecycleTerminalMaterializer,
)
from backend.app.order_limits import EntryBudgetLimits
from backend.app.persistence import (
    AgentDecisionRepository,
    SQLAlchemyAgentServiceRepository,
    SQLAlchemyExecutionRepository,
)
from backend.app.persistence.opportunity_evidence import (
    OpportunityBaselineSeal,
    OpportunityObservationSpec,
    OpportunityPlanSpec,
    SQLAlchemyOpportunityEvidenceRepository,
)
from backend.app.persistence.runtime import apply_migrations, discover_migrations
from backend.app.persistence.sqlalchemy_models import (
    AlpacaMarketSessionRow,
    DevelopmentOpportunityPlanRow,
    ExecutionIntentRow,
    GreekAuthorityVersionRow,
    ManagedLifecyclePositionRow,
    ManagedPositionTransitionRow,
    SubmissionBaselineRow,
)
from backend.app.policy import (
    STRUCTURAL_BULLISH_PILOT_ID,
    AccountOpportunityState,
    CatalystQuality,
    OpportunityDirection,
    OpportunityInput,
    OpportunityPolicy,
    evaluate_assessment,
    evaluate_opportunity,
)
from backend.app.policy.opportunity import TradingHaltState
from backend.app.services import (
    AgentRunService,
    AlpacaMarketSession,
    AtmIvObservation,
    CalibrationBinding,
    DevelopmentLifecycleAcquisition,
    EntryProposalAuthorityInput,
    ExecutionService,
    LifecycleBoundaryObservation,
    LifecycleOptionObservation,
    LifecycleProviderObservation,
    ObservedPaperAccountAuthority,
    PriceConfirmationPoint,
    UnderlyingMarketObservation,
    WholeAccountEvidence,
    build_development_entry_proposal,
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
    OpportunityThesisFactoryInput,
    SQLAlchemyOpportunityThesisRepository,
    build_frozen_opportunity_thesis,
)
from backend.tests.runtime_composition.test_development_acquisition import (
    ContextSource,
    ManifestSink,
    ObservationSource,
)
from ops.launch.submission_reconciliation_init import initialize_submission_reconciliation

POSTGRES_URL_ENV = "ALPHADECAY_TEST_POSTGRES_URL"
MIGRATIONS = Path(__file__).parents[3] / "migrations"
FINGERPRINT = "a" * 64
ENTRY_BOUNDARY = datetime(2026, 8, 31, 13, 50, tzinfo=UTC)
ENTRY_AT = ENTRY_BOUNDARY + timedelta(seconds=5)
TARGET_AT = datetime(2026, 9, 1, 13, 50, tzinfo=UTC)
BASELINE_AT = datetime(2026, 8, 28, 15, 13, tzinfo=UTC)
EXPIRY = date(2026, 10, 9)
LONG = "SPY261009C00765000"
SHORT = "SPY261009C00769000"
QUANTITY = 5

pytestmark = pytest.mark.skipif(
    POSTGRES_URL_ENV not in os.environ,
    reason="requires a dedicated PostgreSQL integration URL",
)


@dataclass
class _Clock:
    value: datetime

    def now(self, _session=None) -> datetime:
        return self.value


@dataclass(frozen=True)
class _Authority:
    autonomous: bool = True

    def observe(self) -> ObservedPaperAccountAuthority:
        return ObservedPaperAccountAuthority(
            AccountRole.SUBMISSION,
            FINGERPRINT,
            True,
            self.autonomous,
        )


@dataclass(frozen=True)
class _Calibration:
    def binding_for(self, authority) -> CalibrationBinding:
        assert authority == _Authority().observe()
        return CalibrationBinding(
            account_role=AccountRole.SUBMISSION,
            account_fingerprint=FINGERPRINT,
            decision_code="CALIBRATION_BINDING_NO_TRADE",
            machine_binding_hash="b" * 64,
            calibration_hash="c" * 64,
            decision_boundary=ENTRY_BOUNDARY,
            sealed_at=ENTRY_AT,
        )


@dataclass(frozen=True)
class _Acquisition:
    value: object

    async def acquire(self, authority, trusted_at, tick_id, *, actor):
        del tick_id
        assert authority == _Authority().observe()
        assert trusted_at == ENTRY_AT
        assert actor is Actor.SCHEDULER
        return self.value


@dataclass(frozen=True)
class _LifecycleAcquisition:
    target: DevelopmentLifecycleAcquisition
    trusted_at: datetime

    async def acquire(self, authority, trusted_at, tick_id, *, actor):
        assert trusted_at == self.trusted_at
        return await self.target.acquire(authority, trusted_at, tick_id, actor=actor)


class _ForbiddenAcquisition:
    async def acquire(self, *_args, **_kwargs):
        raise AssertionError("recovery reached acquisition")


class _ForbiddenResearch:
    async def research(self, *_args, **_kwargs):
        raise AssertionError("structural lifecycle reached research")


class _ForbiddenClassifier:
    def classify(self, *_args, **_kwargs):
        raise AssertionError("structural lifecycle reached classification")


class _FailingTerminalMaterializer:
    def materialize(self, *, execution_certificate_id):
        del execution_certificate_id
        raise RuntimeError("simulated post-fill interruption")


@dataclass(frozen=True)
class _Runtime:
    execution: object


class _FilledBroker:
    def __init__(self, cashflow: Decimal, before_submit=None) -> None:
        self.cashflow = cashflow
        self.before_submit = before_submit
        self.envelope = None
        self.client_id: str | None = None
        self.broker_reference = f"fake-provider-{uuid4().hex}"

    def submit(self, request, client_id: str) -> BrokerResult:
        if self.before_submit is not None:
            self.before_submit(request)
        self.envelope = request
        self.client_id = client_id
        return BrokerResult(
            self.broker_reference,
            "FILLED",
            request.quantity,
            request.quantity,
            fill_cash_flow=self.cashflow,
        )

    def lookup(self, client_id: str):
        raise AssertionError(f"unexpected lookup: {client_id}")

    def replace(self, provider_order_id: str, client_id: str, limit: Decimal):
        raise AssertionError(f"unexpected replace: {provider_order_id}, {client_id}, {limit}")

    def cancel(self, provider_order_id: str):
        raise AssertionError(f"unexpected cancel: {provider_order_id}")


class _FilledSweepPort:
    def __init__(
        self,
        *,
        clock: _Clock,
        baseline_at: datetime,
        broker: _FilledBroker,
        starting_cash: Decimal,
        starting_positions: tuple[InventoryItem, ...],
        known_activities: tuple[ActivityItem, ...],
        activity_complete_through: datetime | None = None,
    ) -> None:
        self.clock = clock
        self.baseline_at = baseline_at
        self.broker = broker
        self.starting_cash = starting_cash
        self.starting_positions = starting_positions
        self.known_activities = known_activities
        self.activity_complete_through = activity_complete_through
        self.calls = 0

    def collect(self, _expectation) -> WholeAccountEvidence:
        self.calls += 1
        final = self.calls > 1
        if final:
            self.clock.value += timedelta(seconds=1)
        positions = () if final and self.broker.cashflow > 0 else self.starting_positions
        if final and self.broker.cashflow < 0:
            positions = _entry_inventory()
        cash = self.starting_cash + (self.broker.cashflow if final else Decimal(0))
        activities = self.known_activities
        if final:
            assert self.broker.envelope is not None and self.broker.client_id is not None
            activities = (*activities, *_fill_activities(self.broker, self.clock.value))
        sweep = _execution_sweep(
            now=self.clock.value,
            baseline_at=self.baseline_at,
            cash=cash,
            positions=positions,
            activities=activities,
        )
        if self.activity_complete_through is not None:
            sweep = replace(
                sweep,
                activity_pagination=replace(
                    sweep.activity_pagination,
                    visibility_complete_through=self.activity_complete_through,
                ),
            )
        greeks = _position_greeks(sweep.retrieval_completed_at) if positions else ()
        return WholeAccountEvidence(sweep, greeks)


def _hash(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()


def _policy(decision_boundary: datetime = ENTRY_BOUNDARY) -> OpportunityPolicy:
    return OpportunityPolicy(
        version="structural-pilot-v1",
        opportunity_key=STRUCTURAL_BULLISH_PILOT_ID,
        underlying="SPY",
        selected_decision_boundary=decision_boundary,
        last_entry_boundary=decision_boundary + timedelta(minutes=25),
        maximum_decision_delay=timedelta(minutes=25),
        maximum_underlying_age=timedelta(seconds=30),
        maximum_catalyst_age=timedelta(minutes=5),
        maximum_option_quote_age=timedelta(seconds=20),
        maximum_leg_quote_skew=timedelta(seconds=3),
        minimum_vwap_distance=Decimal("0"),
        maximum_vwap_distance=Decimal("1"),
        minimum_relative_return=Decimal("0"),
        minimum_beta=Decimal("0"),
        maximum_beta=Decimal("10"),
        required_trend_hits=1,
        maximum_first_reaction=Decimal("10"),
        minimum_catalyst_score=0,
        minimum_candidate_score=0,
        minimum_dte=30,
        maximum_dte=45,
        maximum_relative_spread=Decimal("0.05"),
        minimum_debit_width_fraction=Decimal("0.01"),
        maximum_debit_width_fraction=Decimal("0.99"),
        minimum_credit_width_fraction=Decimal("0.01"),
        maximum_position_loss=Decimal("1125"),
        maximum_equity_risk_fraction=Decimal("0.011"),
        maximum_lifetime_entries=1,
        maximum_lifetime_risk=Decimal("1125"),
        equity_floor=Decimal("99775"),
        maximum_quantity=QUANTITY,
    )


def _bar(symbol: str, close: str) -> OpportunityBar:
    value = OpportunityBar(
        symbol=symbol,
        started_at=ENTRY_BOUNDARY - timedelta(minutes=5),
        completed_at=ENTRY_BOUNDARY,
        open=Decimal(close) - 1,
        high=Decimal(close) + 1,
        low=Decimal(close) - 2,
        close=Decimal(close),
        volume=Decimal("1000"),
        vwap=Decimal(close) - Decimal("0.5"),
        source_hash="",
    )
    return replace(value, source_hash=opportunity_bar_digest(value))


def _option(
    symbol: str,
    strike: str,
    bid: str,
    ask: str,
    delta: str,
    *,
    right: str = "C",
) -> OpportunityOption:
    value = OpportunityOption(
        symbol=symbol,
        underlying="SPY",
        expiry=EXPIRY,
        right=right,
        strike=Decimal(strike),
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=10,
        ask_size=10,
        quote_at=ENTRY_AT - timedelta(seconds=2),
        retrieved_at=ENTRY_AT - timedelta(seconds=1),
        implied_volatility=Decimal("0.30"),
        delta=Decimal(delta),
        gamma=Decimal("0.02"),
        theta_per_day=Decimal("-0.05"),
        vega_per_iv_point=Decimal("0.10"),
        source_hash="",
    )
    return replace(value, source_hash=opportunity_option_digest(value))


def _snapshot(request: OpportunitySnapshotRequest) -> OpportunityMarketSnapshot:
    account = AccountResponse(
        role=AccountRole.SUBMISSION,
        paper=True,
        equity=Decimal("100000"),
        buying_power=Decimal("200000"),
        baseline_status=DataQuality.COMPLETE,
        autonomous_enabled=True,
    )
    book = OpportunityAccountBook(
        account=account,
        positions=PositionListResponse(positions=()),
        open_orders=(),
        account_fingerprint=FINGERPRINT,
        source_hash="",
    )
    book = replace(book, source_hash=opportunity_account_book_digest(book))
    market = OpportunityMarketSession(
        session_date=ENTRY_BOUNDARY.date(),
        open_at=ENTRY_BOUNDARY - timedelta(minutes=15),
        close_at=ENTRY_BOUNDARY + timedelta(hours=6, minutes=15),
        clock_at=ENTRY_AT,
        market_open=True,
        next_open_at=ENTRY_BOUNDARY + timedelta(days=1),
        next_close_at=ENTRY_BOUNDARY + timedelta(days=1, hours=6, minutes=15),
        source_hash="",
    )
    market = replace(market, source_hash=opportunity_market_session_digest(market))
    value = OpportunityMarketSnapshot(
        trusted_at=ENTRY_AT,
        account_book=book,
        session=market,
        underlying_bar=_bar("SPY", "767"),
        benchmark_bar=_bar("QQQ", "570"),
        options=(
            _option(LONG, "765", "3.00", "3.04", "0.60"),
            _option(SHORT, "769", "1.20", "1.22", "0.35"),
            _option(
                "SPY261009P00765000",
                "765",
                "1.95",
                "1.97",
                "-0.40",
                right="P",
            ),
        ),
        request_hash=opportunity_snapshot_request_digest(request),
        source_hash="",
    )
    return replace(value, source_hash=opportunity_market_snapshot_digest(value))


def _account(
    at: datetime,
    cash: Decimal,
    *,
    account_fingerprint: str = FINGERPRINT,
) -> AccountObservation:
    return AccountObservation(
        role=AccountRole.SUBMISSION,
        account_fingerprint=account_fingerprint,
        paper=True,
        status="ACTIVE",
        account_blocked=False,
        trading_blocked=False,
        options_trading_blocked=False,
        equity=Decimal("100000"),
        buying_power=Decimal("200000"),
        cash=cash,
        observed_at=at,
        time_quality="RETRIEVAL_TIME_ONLY",
    )


def _funding() -> ActivityItem:
    return ActivityItem(
        activity_id_hash="b" * 64,
        activity_type=ActivityType.INITIAL_FUNDING,
        occurred_at=BASELINE_AT - timedelta(minutes=1),
        symbol=None,
        signed_quantity=Decimal("100000"),
    )


def _execution_sweep(
    *,
    now: datetime,
    baseline_at: datetime,
    cash: Decimal,
    positions: tuple[InventoryItem, ...],
    activities: tuple[ActivityItem, ...],
) -> SweepObservation:
    completed = now - timedelta(milliseconds=1)
    first_at = completed - timedelta(milliseconds=3)
    return SweepObservation(
        retrieval_started_at=completed - timedelta(milliseconds=5),
        retrieval_completed_at=completed,
        activity_pagination=ActivityPaginationEvidence(
            requested_start=baseline_at,
            requested_end=first_at,
            retrieved_through=first_at,
            established_at=completed - timedelta(milliseconds=2),
            page_count=1,
            terminal_page_seen=True,
            visibility_complete_through=baseline_at,
            visibility_horizon=timedelta(hours=24),
        ),
        first_account=_account(first_at, cash),
        final_account=_account(completed, cash),
        first_positions=positions,
        final_positions=positions,
        first_open_orders=(),
        final_open_orders=(),
        activities=tuple(sorted(activities, key=lambda item: item.activity_id_hash)),
        positions_complete=True,
        orders_complete=True,
    )


def _entry_inventory() -> tuple[InventoryItem, ...]:
    return (
        InventoryItem(InventoryKind.OPTION, LONG, Decimal(QUANTITY), 100),
        InventoryItem(InventoryKind.OPTION, SHORT, Decimal(-QUANTITY), 100),
    )


def _position_greeks(retrieved_at: datetime) -> tuple[PositionGreekObservation, ...]:
    return (
        PositionGreekObservation(
            LONG,
            Decimal(QUANTITY),
            100,
            Decimal("0.60"),
            Decimal("0.02"),
            Decimal("-0.05"),
            Decimal("0.10"),
            "indicative",
            retrieved_at - timedelta(milliseconds=1),
            retrieved_at,
            "c" * 64,
        ),
        PositionGreekObservation(
            SHORT,
            Decimal(-QUANTITY),
            100,
            Decimal("0.35"),
            Decimal("0.01"),
            Decimal("-0.02"),
            Decimal("0.05"),
            "indicative",
            retrieved_at - timedelta(milliseconds=1),
            retrieved_at,
            "d" * 64,
        ),
    )


def _fill_activities(broker: _FilledBroker, occurred_at: datetime) -> tuple[ActivityItem, ...]:
    assert broker.envelope is not None and broker.client_id is not None
    result = []
    for index, leg in enumerate(broker.envelope.legs):
        positive = leg.intent in {PositionIntent.BUY_TO_OPEN, PositionIntent.BUY_TO_CLOSE}
        result.append(
            ActivityItem(
                activity_id_hash=_hash(f"{broker.broker_reference}:{index}"),
                activity_type=ActivityType.FILL,
                occurred_at=occurred_at - timedelta(milliseconds=1),
                symbol=leg.symbol,
                signed_quantity=Decimal(
                    broker.envelope.quantity if positive else -broker.envelope.quantity
                ),
                provider_order_id=broker.broker_reference,
                client_order_id=broker.client_id,
            )
        )
    return tuple(result)


def _lifecycle_observation(
    context,
    trusted_at: datetime,
    close_credit: Decimal,
    *,
    cash: Decimal = Decimal("99095"),
):
    started = trusted_at - timedelta(seconds=5)
    completed = trusted_at - timedelta(seconds=1)
    positions = tuple(
        InventoryItem(InventoryKind.OPTION, item.symbol, item.signed_quantity, item.multiplier)
        for item in context.expected_positions
    )
    transition_activities = tuple(
        ActivityItem(
            activity_id_hash=activity_hash,
            activity_type=ActivityType.FILL,
            occurred_at=context.lifecycle_origin_at,
            symbol=position.symbol,
            signed_quantity=position.signed_quantity,
        )
        for activity_hash, position in zip(
            context.lifecycle_transitions[0].activity_hashes,
            context.expected_positions,
            strict=True,
        )
    )
    sweep = SweepObservation(
        retrieval_started_at=started,
        retrieval_completed_at=completed,
        activity_pagination=ActivityPaginationEvidence(
            requested_start=context.lifecycle_origin_at,
            requested_end=trusted_at - timedelta(seconds=4),
            retrieved_through=trusted_at - timedelta(seconds=4),
            established_at=trusted_at - timedelta(seconds=3),
            page_count=1,
            terminal_page_seen=True,
            visibility_complete_through=trusted_at - timedelta(days=1, seconds=4),
            visibility_horizon=timedelta(hours=24),
        ),
        first_account=_account(
            trusted_at - timedelta(seconds=4),
            cash,
            account_fingerprint=context.account_fingerprint,
        ),
        final_account=_account(
            trusted_at - timedelta(seconds=2),
            cash,
            account_fingerprint=context.account_fingerprint,
        ),
        first_positions=positions,
        final_positions=positions,
        first_open_orders=(),
        final_open_orders=(),
        activities=transition_activities,
        positions_complete=True,
        orders_complete=True,
    )
    short_ask = Decimal("0.65")
    long_bid = close_credit + short_ask

    def option(symbol, quantity, bid, ask, delta, gamma, theta, vega):
        return LifecycleOptionObservation(
            symbol=symbol,
            signed_quantity=quantity,
            multiplier=100,
            active=True,
            tradable=True,
            feed="indicative",
            bid_price=bid,
            ask_price=ask,
            delta=delta,
            gamma=gamma,
            theta_per_day=theta,
            vega_per_iv_point=vega,
            quote_observed_at=trusted_at - timedelta(seconds=3),
            greek_observed_at=trusted_at - timedelta(seconds=3),
            retrieved_at=trusted_at - timedelta(seconds=2),
            greek_authority_id=context.greek_authority.authority_id,
            greek_timestamp_source_hash=context.greek_authority.timestamp_contract_hash,
            greek_units_source_hash=context.greek_authority.units_source_hash,
            source_hash=_hash(f"quote:{symbol}:{trusted_at.isoformat()}:{close_credit}"),
        )

    options = tuple(
        option(
            position.symbol,
            position.signed_quantity,
            long_bid if position.signed_quantity > 0 else short_ask - Decimal("0.01"),
            long_bid + Decimal("0.02") if position.signed_quantity > 0 else short_ask,
            Decimal("0.60") if position.signed_quantity > 0 else Decimal("0.35"),
            Decimal("0.02") if position.signed_quantity > 0 else Decimal("0.01"),
            Decimal("-0.05") if position.signed_quantity > 0 else Decimal("-0.02"),
            Decimal("0.10") if position.signed_quantity > 0 else Decimal("0.05"),
        )
        for position in context.expected_positions
    )
    underlying_hash = _hash(f"underlying:{trusted_at.isoformat()}")
    benchmark_hash = _hash(f"benchmark:{trusted_at.isoformat()}")
    market = AlpacaMarketSession(
        market_session_id=uuid5(NAMESPACE_URL, f"session:{trusted_at.date()}"),
        session_date=trusted_at.date(),
        open_at=trusted_at - timedelta(minutes=20),
        close_at=trusted_at + timedelta(hours=6),
        source_hash=_hash(f"market:{trusted_at.date()}"),
        request_hash=_hash(f"market-request:{trusted_at.date()}"),
        retrieved_at=trusted_at - timedelta(seconds=2),
    )
    boundaries = LifecycleBoundaryObservation(
        market_session=market,
        observed_at=trusted_at - timedelta(seconds=3),
        source_hash=_hash(f"boundaries:{trusted_at.isoformat()}"),
        price_confirmation=(
            PriceConfirmationPoint(
                trusted_at - timedelta(seconds=110),
                Decimal("1"),
                Decimal("1"),
                _hash(f"confirmation-1:{trusted_at.isoformat()}"),
                _hash(f"underlying-1:{trusted_at.isoformat()}"),
                _hash(f"benchmark-1:{trusted_at.isoformat()}"),
            ),
            PriceConfirmationPoint(
                trusted_at - timedelta(seconds=60),
                Decimal("1"),
                Decimal("1"),
                _hash(f"confirmation-2:{trusted_at.isoformat()}"),
                underlying_hash,
                benchmark_hash,
            ),
        ),
        short_call_close_at=trusted_at + timedelta(days=1),
        weekend_close_at=trusted_at + timedelta(days=1),
        contest_end_at=trusted_at + timedelta(days=2),
    )
    return LifecycleProviderObservation(
        sweep=sweep,
        underlying=UnderlyingMarketObservation(
            underlying="SPY",
            bid_price=Decimal("767"),
            ask_price=Decimal("767.01"),
            quote_observed_at=trusted_at - timedelta(seconds=3),
            quote_retrieved_at=trusted_at - timedelta(seconds=2),
            quote_source_hash=_hash(f"underlying-quote:{trusted_at.isoformat()}"),
            completed_bar_at=trusted_at - timedelta(seconds=60),
            completed_bar_source_hash=underlying_hash,
            request_hash=_hash(f"underlying-request:{trusted_at.isoformat()}"),
            benchmark_symbol="QQQ",
            benchmark_completed_bar_at=trusted_at - timedelta(seconds=60),
            benchmark_completed_bar_source_hash=benchmark_hash,
        ),
        options=options,
        atm_iv=AtmIvObservation(
            "SPY",
            Decimal("0.30"),
            "indicative",
            trusted_at - timedelta(seconds=3),
            trusted_at - timedelta(seconds=2),
            _hash(f"atm:{trusted_at.isoformat()}"),
            _hash(f"atm-request:{trusted_at.isoformat()}"),
            _hash(f"atm-call:{trusted_at.isoformat()}"),
            _hash(f"atm-put:{trusted_at.isoformat()}"),
        ),
        boundaries=boundaries,
    )


def test_frozen_submission_schedule_profile() -> None:
    policy = _policy(datetime(2026, 9, 2, 13, 50, tzinfo=UTC))

    assert policy.selected_decision_boundary == datetime(2026, 9, 2, 13, 50, tzinfo=UTC)
    assert policy.last_entry_boundary == datetime(2026, 9, 2, 14, 15, tzinfo=UTC)
    assert policy.maximum_decision_delay == timedelta(minutes=25)


def test_submission_structural_pilot_full_postgres_lifecycle() -> None:
    admin = create_engine(os.environ[POSTGRES_URL_ENV])
    schema = f"submission_structural_{uuid4().hex}"
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
        clock = _Clock(ENTRY_AT)
        policy = _policy()
        assert policy.selected_decision_boundary == datetime(2026, 8, 31, 13, 50, tzinfo=UTC)
        assert policy.last_entry_boundary == datetime(2026, 8, 31, 14, 15, tzinfo=UTC)
        assert policy.maximum_decision_delay == timedelta(minutes=25)
        limits = EntryBudgetLimits(
            policy_hash=evaluate_opportunity(
                policy,
                OpportunityInput(
                    opportunity_key=STRUCTURAL_BULLISH_PILOT_ID,
                    underlying="SPY",
                    observed_decision_boundary=ENTRY_BOUNDARY,
                    evaluated_at=ENTRY_AT,
                    completed_bar_at=ENTRY_BOUNDARY,
                    decision_boundary_complete=True,
                    prior_decision_outcome=None,
                    data_quality=DataQuality.COMPLETE,
                    market_open=True,
                    trading_halted=TradingHaltState.NOT_HALTED,
                    underlying_observed_at=ENTRY_BOUNDARY,
                    catalyst_observed_at=ENTRY_BOUNDARY,
                    catalyst_quality=CatalystQuality.MISSING,
                    catalyst_score=0,
                    vwap_distance=Decimal("0"),
                    relative_return=Decimal("0"),
                    beta=Decimal("1"),
                    bull_trend_hits=0,
                    bear_trend_hits=0,
                    absolute_first_reaction=Decimal("0"),
                    candidate=None,
                    account=AccountOpportunityState(
                        AccountRole.SUBMISSION,
                        "f" * 64,
                        True,
                        Decimal("100000"),
                        0,
                        0,
                        0,
                        Decimal("0"),
                        False,
                        Decimal("0"),
                        False,
                    ),
                ),
            ).policy_hash,
            equity_floor=Decimal("99775"),
            maximum_lifetime_entries=1,
            maximum_lifetime_risk=Decimal("1125"),
            maximum_position_loss=Decimal("1125"),
            maximum_entry_quantity=QUANTITY,
        )
        execution_repository = SQLAlchemyExecutionRepository(
            sessions,
            entry_limits=limits,
            trusted_clock=clock,
        )
        execution_repository.register_account(
            role=AccountRole.SUBMISSION,
            fingerprint=FINGERPRINT,
            equity=Decimal("100000"),
            autonomous_enabled=False,
        )
        execution_repository.capture_baseline(
            role=AccountRole.SUBMISSION,
            fingerprint=FINGERPRINT,
            equity=Decimal("100000"),
            captured_at=BASELINE_AT,
            positions_hash="1" * 64,
            orders_hash="2" * 64,
            activities_hash="3" * 64,
        )
        reconciliation_receipt = initialize_submission_reconciliation(
            execution_repository,
            _execution_sweep(
                now=ENTRY_AT,
                baseline_at=BASELINE_AT,
                cash=Decimal("100000"),
                positions=(),
                activities=(_funding(),),
            ),
            persist=True,
        )
        assert reconciliation_receipt["mode"] == "PERSISTED"
        execution_repository.set_autonomous_enabled(
            AccountRole.SUBMISSION,
            True,
            actor=Actor.OWNER,
        )
        with sessions() as session:
            submission_baseline_id = session.scalar(select(SubmissionBaselineRow.baseline_id))
        assert isinstance(submission_baseline_id, UUID)

        request = OpportunitySnapshotRequest(
            account_role=AccountRole.SUBMISSION,
            expected_account_fingerprint=FINGERPRINT,
            underlying="SPY",
            benchmark="QQQ",
            decision_boundary=ENTRY_BOUNDARY,
            minimum_expiry=ENTRY_BOUNDARY.date() + timedelta(days=30),
            maximum_expiry=ENTRY_BOUNDARY.date() + timedelta(days=45),
            minimum_strike=Decimal("750"),
            maximum_strike=Decimal("780"),
            maximum_contracts=16,
            maximum_quote_age=timedelta(seconds=20),
            maximum_quote_skew=timedelta(seconds=3),
        )
        plan_spec = OpportunityPlanSpec(
            opportunity_key=STRUCTURAL_BULLISH_PILOT_ID,
            version=1,
            underlying="SPY",
            event_session=date(2026, 8, 28),
            pre_event_session=date(2026, 8, 27),
            reaction_session=date(2026, 8, 31),
            signal_session=ENTRY_BOUNDARY.date(),
            daily_start_session=date(2026, 6, 1),
            allowed_event_codes=("MACRO",),
            evidence_window_start=ENTRY_BOUNDARY - timedelta(days=1),
            evidence_window_end=ENTRY_BOUNDARY,
            policy=policy,
            request_contract=request,
            thesis_code=STRUCTURAL_BULLISH_PILOT_ID,
            thesis_target_contract={
                "target_at": TARGET_AT.isoformat(),
                "volatility_view": "NEUTRAL",
            },
            exposure_limit_contract={
                "delta_low": "0",
                "delta_high": "500",
                "vega_low": "0",
                "vega_high": "200",
                "maximum_daily_theta": "20",
                "minimum_dte": 30,
                "maximum_dte": 45,
                "portfolio_risk_cap": "1125",
            },
            invalidation_codes=("STRUCTURAL_PILOT_INVALIDATED",),
            frozen_at=ENTRY_BOUNDARY - timedelta(days=1),
            account_role=AccountRole.SUBMISSION,
        )
        evidence_repository = SQLAlchemyOpportunityEvidenceRepository(sessions)
        persisted_plan = evidence_repository.freeze_plan(plan_spec)
        baseline_seal = OpportunityBaselineSeal(
            plan_id=persisted_plan.plan_id,
            account_fingerprint=FINGERPRINT,
            account_source_hash="4" * 64,
            positions_manifest=(),
            positions_source_hash="5" * 64,
            orders_manifest=(),
            orders_source_hash="6" * 64,
            activity_manifest=(),
            activity_source_hash="7" * 64,
            book_hash="8" * 64,
            history_hash="9" * 64,
            captured_at=ENTRY_BOUNDARY - timedelta(minutes=1),
            account_role=AccountRole.SUBMISSION,
            submission_baseline_id=submission_baseline_id,
        )
        persisted_baseline = evidence_repository.seal_baseline(baseline_seal)
        assert (
            evidence_repository.load_plan(
                STRUCTURAL_BULLISH_PILOT_ID,
                account_role=AccountRole.SUBMISSION,
            ).persisted
            == persisted_plan
        )
        assert (
            evidence_repository.load_baseline(
                persisted_plan.plan_id,
                account_role=AccountRole.SUBMISSION,
            ).persisted
            == persisted_baseline
        )

        snapshot = _snapshot(request)
        selection_authority = CandidateSelectionAuthority(
            snapshot_request_hash=snapshot.request_hash,
            snapshot_source_hash=snapshot.source_hash,
            account_fingerprint=FINGERPRINT,
            observed_equity=Decimal("100000"),
            observed_buying_power=Decimal("200000"),
            available_risk=Decimal("1125"),
            available_buying_power=Decimal("1125"),
            greek_unit_convention=GreekUnitConvention.ALPACA_GOPRICEOPTIONS_RAW_V1,
            greek_unit_evidence_hash="e" * 64,
        )
        selection = select_vertical_candidate(
            snapshot,
            policy,
            OpportunityDirection.BULLISH,
            4,
            selection_authority,
        )
        assert selection.candidate is not None
        assert tuple(item.symbol for item in selection.candidate.legs) == (LONG, SHORT)
        assert selection.candidate.quantity == QUANTITY
        assert selection.candidate.approved_limit == Decimal("1.81")
        assert selection.candidate.maximum_limit == Decimal("1.84")

        signals = OpportunitySignalAuthority(
            snapshot_source_hash=snapshot.source_hash,
            calculation_source_hash="1" * 64,
            beta=DecimalSignalAuthority(Decimal("1.1"), ENTRY_BOUNDARY, "2" * 64),
            vwap_distance=DecimalSignalAuthority(Decimal("0.02"), ENTRY_BOUNDARY, "3" * 64),
            relative_return=DecimalSignalAuthority(Decimal("0.01"), ENTRY_BOUNDARY, "4" * 64),
            trend=TrendSignalAuthority(3, 0, ENTRY_BOUNDARY, "5" * 64),
            absolute_first_reaction=DecimalSignalAuthority(Decimal("1"), ENTRY_BOUNDARY, "6" * 64),
            trading_halt_state=TradingHaltState.NOT_HALTED,
            trading_status_observed_at=ENTRY_BOUNDARY,
            trading_status_source_hash="7" * 64,
        )
        catalyst = CatalystAuthority(
            opportunity_key=STRUCTURAL_BULLISH_PILOT_ID,
            quality=CatalystQuality.MISSING,
            score=0,
            observed_at=ENTRY_BOUNDARY,
            source_hash="8" * 64,
        )
        account = AccountBudgetAuthority(
            account_role=AccountRole.SUBMISSION,
            account_fingerprint=FINGERPRINT,
            snapshot_book_source_hash=snapshot.account_book.source_hash,
            observed_at=ENTRY_AT,
            baseline_clean=True,
            baseline_source_hash=persisted_baseline.baseline_hash,
            book_fingerprint=snapshot.account_book.source_hash,
            book_source_hash=snapshot.account_book.source_hash,
            clean_equity=Decimal("100000"),
            open_position_count=0,
            open_order_count=0,
            filled_entry_count=0,
            lifetime_approved_risk=Decimal("0"),
            entry_reservation_active=False,
            reserved_approved_risk=Decimal("0"),
            event_already_attempted=False,
            history_source_hash="9" * 64,
        )
        prior = PriorDecisionAuthority(
            opportunity_key=STRUCTURAL_BULLISH_PILOT_ID,
            decision_boundary=ENTRY_BOUNDARY,
            outcome=None,
            observed_at=ENTRY_AT,
            source_hash="a" * 64,
        )
        assembly = assemble_opportunity_input(
            request=request,
            snapshot=snapshot,
            policy=policy,
            requested_maximum_quantity=QUANTITY,
            selection_authority=selection_authority,
            selection=selection,
            signals=signals,
            catalyst=catalyst,
            account=account,
            prior_decision=prior,
        )
        values = assembly.values
        decision = evaluate_opportunity(policy, values)
        assert decision.outcome.value == "ENTRY_APPROVED"
        assert decision.quantity == QUANTITY
        assert decision.approved_max_loss == Decimal("920")
        assert decision.approved_max_loss == (selection.candidate.maximum_limit * QUANTITY * 100)
        assert decision.approved_max_loss <= policy.maximum_position_loss
        observation_spec = OpportunityObservationSpec(
            plan_id=persisted_plan.plan_id,
            baseline_id=persisted_baseline.baseline_id,
            account_fingerprint=FINGERPRINT,
            policy_hash=decision.policy_hash,
            request_hash=snapshot.request_hash,
            snapshot_hash=snapshot.source_hash,
            calendar_hash="b" * 64,
            daily_hash="c" * 64,
            intraday_hash="d" * 64,
            signal_authority_hash=signals.calculation_source_hash,
            halt_hash=signals.trading_status_source_hash,
            catalyst_hash=catalyst.source_hash,
            greek_hash=selection_authority.greek_unit_evidence_hash,
            account_hash=account.snapshot_book_source_hash,
            activity_hash=account.history_source_hash,
            budget_hash="f" * 64,
            prior_decision_hash=prior.source_hash,
            trusted_at=ENTRY_AT,
            evaluated_at=ENTRY_AT,
            account_role=AccountRole.SUBMISSION,
        )
        observation = evidence_repository.append_observation(observation_spec)
        thesis = SQLAlchemyOpportunityThesisRepository(sessions).persist(
            build_frozen_opportunity_thesis(
                OpportunityThesisFactoryInput(
                    plan_spec=plan_spec,
                    plan=persisted_plan,
                    baseline_seal=baseline_seal,
                    baseline=persisted_baseline,
                    observation_spec=observation_spec,
                    observation=observation,
                    request=request,
                    snapshot=snapshot,
                    requested_maximum_quantity=QUANTITY,
                    selection_authority=selection_authority,
                    selection=selection,
                    signals=signals,
                    catalyst=catalyst,
                    account=account,
                    prior_decision=prior,
                    assembly=assembly,
                    decision=decision,
                    signal_calendar_hash=observation_spec.calendar_hash,
                    signal_daily_hash=observation_spec.daily_hash,
                    signal_intraday_hash=observation_spec.intraday_hash,
                    signal_authority_hash=signals.calculation_source_hash,
                    budget_source_hash=observation_spec.budget_hash,
                )
            )
        )
        thesis_version_id = thesis.thesis_version_id
        greek_authority_id = uuid4()
        market_session_id = uuid4()
        market_open_at = ENTRY_BOUNDARY - timedelta(minutes=15)
        market_close_at = ENTRY_BOUNDARY + timedelta(hours=6)
        market_source_hash = "8" * 64
        market_request_hash = "9" * 64
        with sessions.begin() as session:
            session.add(
                AlpacaMarketSessionRow(
                    market_session_id=market_session_id,
                    session_date=ENTRY_BOUNDARY.date(),
                    open_at=market_open_at,
                    close_at=market_close_at,
                    source_hash=market_source_hash,
                    request_hash=market_request_hash,
                    retrieved_at=ENTRY_AT,
                    source_payload={
                        "market_session_id": str(market_session_id),
                        "session_date": ENTRY_BOUNDARY.date().isoformat(),
                        "open_at": market_open_at.isoformat(),
                        "close_at": market_close_at.isoformat(),
                        "source_hash": market_source_hash,
                        "request_hash": market_request_hash,
                        "retrieved_at": ENTRY_AT.isoformat(),
                    },
                    session_hash="0" * 64,
                    created_at=ENTRY_AT,
                )
            )
            session.add(
                GreekAuthorityVersionRow(
                    authority_id=greek_authority_id,
                    version=1,
                    effective_at=ENTRY_BOUNDARY,
                    timestamp_contract_hash="a" * 64,
                    units_contract_hash="b" * 64,
                    authority_payload={},
                    authority_hash="c" * 64,
                    created_at=ENTRY_BOUNDARY,
                )
            )
        entry_inputs = EntryProposalAuthorityInput(
            policy=policy,
            values=values,
            decision=decision,
            thesis_version_id=thesis_version_id,
            thesis_account_role=AccountRole.SUBMISSION,
            thesis_policy_hash=decision.policy_hash,
            thesis_underlying="SPY",
            thesis_frozen_at=ENTRY_BOUNDARY + timedelta(seconds=1),
            account_role=AccountRole.SUBMISSION,
            account_fingerprint=FINGERPRINT,
            valid_from=ENTRY_AT,
            expires_at=ENTRY_AT + timedelta(seconds=30),
            benchmark_symbol="QQQ",
            underlying_source_hash=snapshot.underlying_bar.source_hash,
            benchmark_source_hash=snapshot.benchmark_bar.source_hash,
            completed_bar_source_hash=snapshot.underlying_bar.source_hash,
        )
        built = build_development_entry_proposal(entry_inputs)
        with pytest.raises(ValueError, match="ENTRY_ACCOUNT_BINDING_INVALID"):
            build_development_entry_proposal(
                replace(entry_inputs, account_role=AccountRole.DEVELOPMENT)
            )

        decisions = SQLAlchemyAgentServiceRepository(
            AgentDecisionRepository(
                sessions,
                database_clock=clock,
                server_autonomy_enabled=True,
            ),
            server_autonomy_enabled=True,
        )
        preview_seen = False

        def preview_before_submit(requested_envelope) -> None:
            nonlocal preview_seen
            preview = decisions.submission_order_preview(built.intent.intent_id)
            assert preview.legs == requested_envelope.legs
            assert preview.quantity == QUANTITY
            assert preview.limit_price == Decimal("1.81")
            assert preview.maximum_loss == Decimal("920.00")
            assert requested_envelope.maximum_limit == Decimal("1.84")
            preview_seen = True

        entry_broker = _FilledBroker(Decimal("-905"), preview_before_submit)
        entry_sweeps = _FilledSweepPort(
            clock=clock,
            baseline_at=BASELINE_AT,
            broker=entry_broker,
            starting_cash=Decimal("100000"),
            starting_positions=(),
            known_activities=(_funding(),),
        )
        entry_execution = ExecutionService(
            execution_repository,
            entry_broker,
            entry_sweeps,
            account_role=AccountRole.SUBMISSION,
            account_fingerprint=FINGERPRINT,
        )
        entry_materializer = SQLAlchemyEntryMaterializer(sessions)
        entry_result = asyncio.run(
            AgentRunService(
                account_authority=_Authority(),
                clock=clock,
                calibration=_Calibration(),
                acquisition=_Acquisition(built.acquisition),
                decisions=decisions,
                runtime=_Runtime(entry_execution),
                server_autonomy_enabled=True,
                submission_opportunity_enabled=True,
                entry_materializer=entry_materializer,
            ).run(Actor.SCHEDULER)
        )
        assert preview_seen
        assert entry_broker.envelope == built.envelope
        assert entry_result.terminal_code == "FILLED", (
            preview_seen,
            entry_broker.envelope,
            entry_sweeps.calls,
        )
        assert entry_result.execution_certificate_id is not None
        lifecycle_repository = SQLAlchemyLifecycleRepository(sessions)
        retained = lifecycle_repository.load(_Authority().observe())
        assert retained.account_role is AccountRole.SUBMISSION
        assert retained.thesis.thesis.thesis_code == STRUCTURAL_BULLISH_PILOT_ID
        assert retained.expected_positions[0].symbol == LONG
        assert retained.expected_positions[1].symbol == SHORT
        assert retained.lifecycle_transitions[0].cashflow == Decimal("-905")

        management_cases = (
            (
                TARGET_AT + timedelta(seconds=10),
                Decimal("2.36"),
                "STRUCTURAL_PROFIT_TARGET_CLOSE",
            ),
            (
                TARGET_AT + timedelta(seconds=10),
                Decimal("1.44"),
                "STRUCTURAL_STOP_LIMIT_CLOSE",
            ),
            (
                datetime(2026, 9, 4, 13, 45, tzinfo=UTC),
                Decimal("1.81"),
                "STRUCTURAL_MANDATORY_BOUNDARY_CLOSE",
            ),
        )
        for index, (trusted_at, close_credit, rationale) in enumerate(management_cases, start=1):
            observation = _lifecycle_observation(retained, trusted_at, close_credit)
            sweep = observation.sweep
            assert sweep.activity_pagination.complete
            assert (
                sweep.activity_pagination.visibility_complete_through
                >= sweep.activity_pagination.requested_end
                - sweep.activity_pagination.visibility_horizon
            ), (
                retained.lifecycle_origin_at,
                sweep.activity_pagination.requested_end,
                sweep.activity_pagination.visibility_complete_through,
            )
            assert sweep.first_positions == sweep.final_positions
            assert sweep.first_open_orders == sweep.final_open_orders
            target = DevelopmentLifecycleAcquisition(
                ContextSource(retained),
                ObservationSource(observation),
                _ForbiddenResearch(),
                _ForbiddenClassifier(),
                ManifestSink(),
            )
            acquired = asyncio.run(
                target.acquire(
                    _Authority().observe(),
                    trusted_at,
                    UUID(int=900 + index),
                    actor=Actor.SCHEDULER,
                )
            )
            assessed = evaluate_assessment(acquired.values)
            assert assessed.response.rationale_code == rationale
            assert assessed.response.action.value == "CLOSE"
            assert acquired.proposal is not None
            assert acquired.proposal.intent.envelope.action.value == "CLOSE"

        close_at = TARGET_AT + timedelta(seconds=10)
        close_observation = _lifecycle_observation(retained, close_at, Decimal("2.36"))
        close_acquisition = DevelopmentLifecycleAcquisition(
            lifecycle_repository,
            ObservationSource(close_observation),
            _ForbiddenResearch(),
            _ForbiddenClassifier(),
            lifecycle_repository,
        )
        clock.value = close_at
        entry_state = execution_repository.get_reconciliation_state(AccountRole.SUBMISSION)
        close_preview_seen = False

        def close_preview_before_submit(requested_envelope) -> None:
            nonlocal close_preview_seen
            with sessions() as session:
                close_intent_id = session.scalar(
                    select(ExecutionIntentRow.intent_id).where(
                        ExecutionIntentRow.assessment_certificate_id
                        == requested_envelope.authorization_certificate_id
                    )
                )
            assert isinstance(close_intent_id, UUID)
            preview = decisions.submission_order_preview(close_intent_id)
            assert preview.strategy == "CLOSE_VERTICAL"
            assert preview.reason_codes == ("STRUCTURAL_PROFIT_TARGET_CLOSE",)
            assert preview.legs == requested_envelope.legs
            close_preview_seen = True

        close_broker = _FilledBroker(Decimal("1180"), close_preview_before_submit)
        close_sweeps = _FilledSweepPort(
            clock=clock,
            baseline_at=entry_state.baseline_captured_at,
            broker=close_broker,
            starting_cash=entry_state.expected_cash,
            starting_positions=entry_state.expected_positions,
            known_activities=entry_state.known_activities,
        )
        close_execution = ExecutionService(
            execution_repository,
            close_broker,
            close_sweeps,
            account_role=AccountRole.SUBMISSION,
            account_fingerprint=FINGERPRINT,
        )
        close_result = asyncio.run(
            AgentRunService(
                account_authority=_Authority(),
                clock=clock,
                calibration=_Calibration(),
                acquisition=_LifecycleAcquisition(close_acquisition, close_at),
                decisions=decisions,
                runtime=_Runtime(close_execution),
                server_autonomy_enabled=True,
                submission_opportunity_enabled=True,
                entry_materializer=entry_materializer,
                lifecycle_terminal_materializer=_FailingTerminalMaterializer(),
            ).run(Actor.SCHEDULER)
        )
        assert close_result.decision.code == "CLOSE_RISK_ONLY"
        assert close_result.terminal_code == "LIFECYCLE_FILLED_MATERIALIZATION_FAILED"
        assert close_preview_seen
        assert close_result.execution_certificate_id is not None
        assert close_broker.envelope.action.value == "CLOSE"
        with sessions() as session:
            position = session.scalar(select(ManagedLifecyclePositionRow))
            assert position is not None and position.closed_at is None
        pending = decisions.pending_submission_lifecycle_intents(_Authority().observe())
        assert len(pending) == 1

        clock.value = close_at + timedelta(minutes=5)
        recovered = asyncio.run(
            AgentRunService(
                account_authority=_Authority(),
                clock=clock,
                calibration=_Calibration(),
                acquisition=_ForbiddenAcquisition(),
                decisions=decisions,
                runtime=_Runtime(close_execution),
                server_autonomy_enabled=True,
                submission_opportunity_enabled=True,
                entry_materializer=entry_materializer,
                lifecycle_terminal_materializer=SQLAlchemyLifecycleTerminalMaterializer(sessions),
            ).run(Actor.SCHEDULER)
        )
        assert recovered.terminal_code == "LIFECYCLE_RECOVERY_FILLED"
        assert recovered.execution_certificate_id is None
        assert close_sweeps.calls == 2
        with sessions() as session:
            position = session.scalar(select(ManagedLifecyclePositionRow))
            assert position is not None and position.closed_at is not None
            assert (
                session.scalar(select(func.count()).select_from(ManagedPositionTransitionRow)) == 2
            )
            assert (
                execution_repository.get_reconciliation_state(
                    AccountRole.SUBMISSION
                ).expected_positions
                == ()
            )

        with (
            pytest.raises(
                DBAPIError,
                match="DEVELOPMENT_OPPORTUNITY_EVIDENCE_IMMUTABLE",
            ),
            sessions.begin() as session,
        ):
            session.execute(
                text(
                    f'UPDATE "{schema}".development_opportunity_plans '
                    "SET plan_hash=repeat('f',64) WHERE plan_id=:plan_id"
                ),
                {"plan_id": persisted_plan.plan_id},
            )
        with sessions() as session:
            assert (
                session.get(
                    DevelopmentOpportunityPlanRow,
                    persisted_plan.plan_id,
                ).plan_hash
                == persisted_plan.plan_hash
            )
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()

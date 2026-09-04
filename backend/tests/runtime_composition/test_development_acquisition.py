from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from uuid import UUID

import pytest

from backend.app.contracts.v1 import (
    AccountRole,
    DataQuality,
    EvidenceClassification,
    EvidenceRelation,
    EvidenceTier,
    GreekExposure,
    PositionIntent,
    SourceCluster,
    ThesisCreateRequest,
    ThesisResponse,
)
from backend.app.execution import (
    AccountObservation,
    ActivityItem,
    ActivityPaginationEvidence,
    ActivityType,
    Actor,
    ExecutionAction,
    InventoryItem,
    InventoryKind,
    OrderLegIntent,
    SweepObservation,
)
from backend.app.lifecycle import LifecycleLaunchAuthority
from backend.app.lifecycle.structural_pilot import (
    STRUCTURAL_MANDATORY_BOUNDARY_CLOSE,
    structural_pilot_lifecycle,
)
from backend.app.policy import ExecutionDecision, ThesisStatus, VolatilityView, evaluate_assessment
from backend.app.policy.opportunity import (
    STRUCTURAL_BEARISH_OTM_PILOT_ID,
    STRUCTURAL_BULLISH_OTM_PILOT_ID,
)
from backend.app.services import (
    AcquisitionFailure,
    AlpacaMarketSession,
    AtmIvObservation,
    DevelopmentLifecycleAcquisition,
    GreekAuthorityEvidence,
    LifecycleBoundaryObservation,
    LifecycleOptionObservation,
    LifecycleProviderObservation,
    LifecycleRollObservation,
    ObservedPaperAccountAuthority,
    PriceConfirmationPoint,
    RetainedLifecycleContext,
    RetainedLifecycleTransition,
    RetainedOptionPosition,
    UnderlyingMarketObservation,
)
from backend.app.services.acquisition import lifecycle_liquidity_authority_hash

NOW = datetime(2026, 8, 31, 15, tzinfo=UTC)
BASELINE = NOW - timedelta(days=5)
FINGERPRINT = "a" * 64
POLICY_HASH = "c" * 64
TICK_ID = UUID("00000000-0000-0000-0000-000000000200")
LONG = "NVDA260918C00170000"
SHORT = "NVDA260918C00180000"
SESSION_ID = UUID("00000000-0000-0000-0000-000000000401")


class SourceFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Source:
    def __init__(self, value=None, error: str | None = None) -> None:
        self.value = value
        self.error = error
        self.calls = 0

    def _result(self):
        self.calls += 1
        if self.error is not None:
            raise SourceFailure(self.error)
        return self.value


class ContextSource(Source):
    def load(self, _authority):
        return self._result()


class ObservationSource(Source):
    def observe(self, _context, _trusted_at):
        return self._result()


class ResearchSource(Source):
    async def research(self, _context, _trusted_at):
        return self._result()


class Classifier(Source):
    def classify(self, _thesis, _clusters):
        return self._result()


class ManifestSink(Source):
    def persist(self, **record):
        self.calls += 1
        if self.error is not None:
            raise SourceFailure(self.error)
        self.value = record


def authority(role: AccountRole = AccountRole.DEVELOPMENT) -> ObservedPaperAccountAuthority:
    return ObservedPaperAccountAuthority(role, FINGERPRINT, True, False)


def retained_positions(
    long_symbol: str = LONG,
    short_symbol: str = SHORT,
    *,
    multiplier: int = 100,
) -> tuple[RetainedOptionPosition, RetainedOptionPosition]:
    return (
        RetainedOptionPosition(long_symbol, Decimal("1"), multiplier),
        RetainedOptionPosition(short_symbol, Decimal("-1"), multiplier),
    )


def fingerprint(positions: tuple[RetainedOptionPosition, ...]) -> str:
    material = [
        {
            "kind": "OPTION",
            "symbol": item.symbol,
            "signed_quantity": str(item.signed_quantity),
            "multiplier": item.multiplier,
        }
        for item in positions
    ]
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def thesis() -> ThesisResponse:
    return ThesisResponse(
        thesis_id=UUID("00000000-0000-0000-0000-000000000101"),
        version=1,
        frozen=True,
        thesis_hash="d" * 64,
        thesis=ThesisCreateRequest(
            underlying="NVDA",
            thesis_code="CATALYST_CONTINUATION",
            invalidation_codes=("GUIDANCE_REVERSED",),
            intended_exposure=GreekExposure(
                delta=Decimal("30"),
                gamma=Decimal("2"),
                theta_per_day=Decimal("-3"),
                vega_per_iv_point=Decimal("5"),
            ),
            source_policy_hash=POLICY_HASH,
        ),
    )


def context(**changes: object) -> RetainedLifecycleContext:
    positions = changes.pop("expected_positions", retained_positions())
    values: dict[str, object] = {
        "thesis_version_id": UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
        "account_role": AccountRole.DEVELOPMENT,
        "account_fingerprint": FINGERPRINT,
        "policy_hash": POLICY_HASH,
        "thesis": thesis(),
        "thesis_frozen_at": BASELINE - timedelta(days=2),
        "lifecycle_origin_at": BASELINE - timedelta(days=1),
        "lifecycle_transitions": (
            RetainedLifecycleTransition(
                action="ENTRY",
                occurred_at=BASELINE - timedelta(days=1),
                market_session_id=UUID("00000000-0000-0000-0000-000000000400"),
                cashflow=Decimal("-500"),
                activity_hashes=(),
            ),
        ),
        "target_at": NOW + timedelta(days=5),
        "position_fingerprint": fingerprint(positions),
        "expected_positions": positions,
        "account_expected_positions": positions,
        "account_activity_hashes": (),
        "account_lifecycle_origin_at": BASELINE - timedelta(days=1),
        "delta_low": Decimal("20"),
        "delta_high": Decimal("40"),
        "vega_low": Decimal("3"),
        "vega_high": Decimal("8"),
        "maximum_daily_theta": Decimal("5"),
        "minimum_dte": 14,
        "maximum_dte": 35,
        "maximum_relative_spread": Decimal("0.25"),
        "liquidity_authority_hash": lifecycle_liquidity_authority_hash(
            POLICY_HASH,
            Decimal("0.25"),
        ),
        "volatility_view": VolatilityView.LONG,
        "entry_atm_iv": Decimal("0.40"),
        "approved_max_loss": Decimal("500"),
        "portfolio_risk_cap": Decimal("1000"),
        "greek_authority": GreekAuthorityEvidence(
            authority_id=UUID("00000000-0000-0000-0000-000000000301"),
            version=1,
            effective_at=BASELINE - timedelta(days=2),
            timestamp_contract_hash="8" * 64,
            units_source_hash="9" * 64,
        ),
        "managed_position_id": UUID("00000000-0000-0000-0000-000000000501"),
        "current_snapshot_id": UUID("00000000-0000-0000-0000-000000000502"),
        "launch_authority": LifecycleLaunchAuthority(
            beta60=Decimal("1.25"),
            benchmark_symbol="QQQ",
            entry_boundary_at=BASELINE - timedelta(days=3),
            entry_policy_hash=POLICY_HASH,
            underlying_source_hash="1" * 64,
            benchmark_source_hash="2" * 64,
            completed_bar_source_hash="3" * 64,
        ),
    }
    values.update(changes)
    if "account_activity_hashes" not in changes:
        values["account_activity_hashes"] = tuple(
            sorted(
                {
                    value
                    for transition in values["lifecycle_transitions"]
                    for value in transition.activity_hashes
                }
            )
        )
    return RetainedLifecycleContext(**values)


@pytest.mark.parametrize(
    ("strategy_id", "long_symbol", "short_symbol"),
    (
        (
            STRUCTURAL_BULLISH_OTM_PILOT_ID,
            "SPY261009C00750000",
            "SPY261009C00754000",
        ),
        (
            STRUCTURAL_BEARISH_OTM_PILOT_ID,
            "SPY261009P00750000",
            "SPY261009P00746000",
        ),
    ),
)
def test_registered_structural_lifecycle_matches_exact_vertical_geometry(
    strategy_id: str,
    long_symbol: str,
    short_symbol: str,
) -> None:
    positions = retained_positions(long_symbol, short_symbol)
    pilot_thesis = thesis().model_copy(
        update={
            "thesis": thesis().thesis.model_copy(
                update={"underlying": "SPY", "thesis_code": strategy_id}
            )
        }
    )
    target_at = NOW + timedelta(days=1)
    retained = context(
        account_role=AccountRole.SUBMISSION,
        thesis=pilot_thesis,
        target_at=target_at,
        expected_positions=positions,
        position_fingerprint=fingerprint(positions),
        approved_max_loss=Decimal("225"),
    )

    lifecycle = structural_pilot_lifecycle(retained)

    assert lifecycle is not None
    assert (
        lifecycle.close_reason(
            retained,
            executable_value=Decimal("500"),
            trusted_at=target_at,
        )
        == STRUCTURAL_MANDATORY_BOUNDARY_CLOSE
    )


def account(observed_at: datetime, **changes: object) -> AccountObservation:
    values: dict[str, object] = {
        "role": AccountRole.DEVELOPMENT,
        "account_fingerprint": FINGERPRINT,
        "paper": True,
        "status": "ACTIVE",
        "account_blocked": False,
        "trading_blocked": False,
        "options_trading_blocked": False,
        "equity": Decimal("100000"),
        "buying_power": Decimal("400000"),
        "cash": Decimal("99500"),
        "observed_at": observed_at,
        "time_quality": "RETRIEVAL_TIME_ONLY",
    }
    values.update(changes)
    return AccountObservation(**values)


def inventory(positions=None) -> tuple[InventoryItem, ...]:
    positions = positions or retained_positions()
    return tuple(
        InventoryItem(InventoryKind.OPTION, item.symbol, item.signed_quantity, item.multiplier)
        for item in positions
    )


def sweep(positions=None, **changes: object) -> SweepObservation:
    first_at = NOW - timedelta(seconds=4)
    activity_at = NOW - timedelta(seconds=3)
    final_at = NOW - timedelta(seconds=2)
    positions = inventory(positions)
    funding = ActivityItem(
        activity_id_hash="f" * 64,
        activity_type=ActivityType.INITIAL_FUNDING,
        occurred_at=BASELINE - timedelta(days=1),
        symbol=None,
        signed_quantity=Decimal("100000"),
    )
    values: dict[str, object] = {
        "retrieval_started_at": NOW - timedelta(seconds=5),
        "retrieval_completed_at": NOW - timedelta(seconds=1),
        "activity_pagination": ActivityPaginationEvidence(
            requested_start=BASELINE - timedelta(days=1),
            requested_end=first_at,
            retrieved_through=first_at,
            established_at=activity_at,
            page_count=1,
            terminal_page_seen=True,
            visibility_complete_through=first_at - timedelta(hours=24),
            visibility_horizon=timedelta(hours=24),
        ),
        "first_account": account(first_at),
        "final_account": account(final_at),
        "first_positions": positions,
        "final_positions": positions,
        "first_open_orders": (),
        "final_open_orders": (),
        "activities": (funding,),
        "positions_complete": True,
        "orders_complete": True,
    }
    values.update(changes)
    return SweepObservation(**values)


def option(
    symbol: str,
    quantity: str,
    *,
    bid: str,
    ask: str,
    delta: str,
    gamma: str,
    theta_value: str,
    vega: str,
) -> LifecycleOptionObservation:
    return LifecycleOptionObservation(
        symbol=symbol,
        signed_quantity=Decimal(quantity),
        multiplier=100,
        active=True,
        tradable=True,
        feed="indicative",
        bid_price=Decimal(bid),
        ask_price=Decimal(ask),
        delta=Decimal(delta),
        gamma=Decimal(gamma),
        theta_per_day=Decimal(theta_value),
        vega_per_iv_point=Decimal(vega),
        quote_observed_at=NOW - timedelta(seconds=3),
        greek_observed_at=NOW - timedelta(seconds=3),
        retrieved_at=NOW - timedelta(seconds=2),
        greek_authority_id=UUID("00000000-0000-0000-0000-000000000301"),
        greek_timestamp_source_hash="8" * 64,
        greek_units_source_hash="9" * 64,
        source_hash=hashlib.sha256(f"quote:{symbol}".encode()).hexdigest(),
    )


def options(positions=None) -> tuple[LifecycleOptionObservation, LifecycleOptionObservation]:
    positions = positions or retained_positions()
    return (
        option(
            positions[0].symbol,
            str(positions[0].signed_quantity),
            bid="10",
            ask="10.20",
            delta="0.60",
            gamma="0.04",
            theta_value="-0.05",
            vega="0.12",
        ),
        option(
            positions[1].symbol,
            str(positions[1].signed_quantity),
            bid="5",
            ask="5.20",
            delta="0.30",
            gamma="0.02",
            theta_value="-0.02",
            vega="0.07",
        ),
    )


def boundaries() -> LifecycleBoundaryObservation:
    return LifecycleBoundaryObservation(
        market_session=AlpacaMarketSession(
            market_session_id=SESSION_ID,
            session_date=NOW.date(),
            open_at=NOW - timedelta(hours=2),
            close_at=NOW + timedelta(hours=5),
            source_hash="6" * 64,
            request_hash="a" * 64,
            retrieved_at=NOW - timedelta(seconds=2),
        ),
        observed_at=NOW - timedelta(seconds=3),
        source_hash="8" * 64,
        price_confirmation=(
            PriceConfirmationPoint(
                NOW - timedelta(seconds=110),
                Decimal("1"),
                Decimal("1"),
                "4" * 64,
                "b" * 64,
                "c" * 64,
            ),
            PriceConfirmationPoint(
                NOW - timedelta(seconds=60),
                Decimal("1"),
                Decimal("1"),
                "5" * 64,
                "5" * 64,
                "d" * 64,
            ),
        ),
        short_call_close_at=NOW + timedelta(days=1),
        weekend_close_at=NOW + timedelta(days=1),
        contest_end_at=NOW + timedelta(days=2),
    )


def observation(**changes: object) -> LifecycleProviderObservation:
    values: dict[str, object] = {
        "sweep": sweep(),
        "underlying": UnderlyingMarketObservation(
            underlying="NVDA",
            bid_price=Decimal("180"),
            ask_price=Decimal("180.10"),
            quote_observed_at=NOW - timedelta(seconds=3),
            quote_retrieved_at=NOW - timedelta(seconds=2),
            quote_source_hash="3" * 64,
            completed_bar_at=NOW - timedelta(seconds=60),
            completed_bar_source_hash="5" * 64,
            request_hash="e" * 64,
            benchmark_symbol="QQQ",
            benchmark_completed_bar_at=NOW - timedelta(seconds=60),
            benchmark_completed_bar_source_hash="d" * 64,
        ),
        "options": options(),
        "atm_iv": AtmIvObservation(
            "NVDA",
            Decimal("0.40"),
            "indicative",
            NOW - timedelta(seconds=3),
            NOW - timedelta(seconds=2),
            "7" * 64,
            "f" * 64,
            "0" * 64,
            "1" * 64,
        ),
        "boundaries": boundaries(),
    }
    values.update(changes)
    return LifecycleProviderObservation(**values)


def cluster(**changes: object) -> SourceCluster:
    values: dict[str, object] = {
        "cluster_id": "cluster-1",
        "source_ids": ("source-1",),
        "headline": "Issuer kept its outlook unchanged.",
        "observed_at": NOW - timedelta(minutes=1),
        "source_tier": EvidenceTier.PRIMARY,
    }
    values.update(changes)
    return SourceCluster(**values)


def classification(**changes: object) -> EvidenceClassification:
    values: dict[str, object] = {
        "cluster_id": "cluster-1",
        "source_ids": ("source-1",),
        "event_code": "GUIDANCE",
        "relation": EvidenceRelation.SUPPORTS,
        "materiality": 2,
        "relevance": Decimal("0.90"),
        "confidence": Decimal("0.90"),
        "source_tier": EvidenceTier.PRIMARY,
    }
    values.update(changes)
    return EvidenceClassification(**values)


def acquisition(**changes: object):
    sources = {
        "retained": ContextSource(context()),
        "observed": ObservationSource(observation()),
        "research": ResearchSource((cluster(),)),
        "classifier": Classifier((classification(),)),
        "manifests": ManifestSink(),
    }
    sources.update(changes)
    target = DevelopmentLifecycleAcquisition(
        sources["retained"],
        sources["observed"],
        sources["research"],
        sources["classifier"],
        sources["manifests"],
    )
    return target, sources


def run(
    target,
    role: AccountRole = AccountRole.DEVELOPMENT,
    tick_id: UUID = TICK_ID,
    *,
    actor: object = Actor.SCHEDULER,
):
    return asyncio.run(target.acquire(authority(role), NOW, tick_id, actor=actor))


def test_invalid_actor_stops_before_lifecycle_sources() -> None:
    target, sources = acquisition()

    with pytest.raises(AcquisitionFailure, match="ACTOR_INVALID"):
        run(target, actor="SCHEDULER")

    assert tuple(source.calls for source in sources.values()) == (0, 0, 0, 0, 0)


def test_complete_authoritative_vertical_becomes_no_proposal_policy_input() -> None:
    target, sources = acquisition()
    result = run(target)
    assert result.proposal is None
    assert result.values.quality is DataQuality.COMPLETE
    assert result.values.actual_exposure == GreekExposure(
        delta=Decimal("30"),
        gamma=Decimal("2"),
        theta_per_day=Decimal("-3"),
        vega_per_iv_point=Decimal("5"),
    )
    assert result.values.scores is not None
    assert result.values.scores.horizon_fraction == Decimal(6) / Decimal(11)
    assert evaluate_assessment(result.values).execution_decision is ExecutionDecision.HOLD_CERTIFIED
    persisted = sources["manifests"].value
    assert sources["manifests"].calls == 1
    assert persisted["manifest_id"] == result.values.acquisition_manifest_id
    assert persisted["manifest_hash"] == result.values.acquisition_manifest_hash
    assert persisted["trusted_at"] == NOW


def test_cross_role_context_rejects_before_provider_sources() -> None:
    target, sources = acquisition()
    with pytest.raises(AcquisitionFailure, match="RETAINED_CONTEXT_INVALID"):
        run(target, AccountRole.SUBMISSION)
    assert tuple(source.calls for source in sources.values()) == (1, 0, 0, 0, 0)


def test_manifest_failure_is_typed_and_returns_no_assessment() -> None:
    target, sources = acquisition(manifests=ManifestSink(error="STORE_UNAVAILABLE"))
    with pytest.raises(AcquisitionFailure, match="MANIFEST_STORE_UNAVAILABLE"):
        run(target)
    assert sources["manifests"].calls == 1


def test_tick_identity_is_unique_and_proposal_stays_none() -> None:
    first, _ = acquisition()
    second, _ = acquisition()
    next_tick = UUID("00000000-0000-0000-0000-000000000203")
    one = run(first)
    two = run(second, tick_id=next_tick)
    assert one.values.assessment_id != two.values.assessment_id
    assert one.proposal is two.proposal is None


@pytest.mark.parametrize(
    "positions",
    (
        retained_positions(short_symbol="NVDA260918P00180000"),
        retained_positions(short_symbol="NVDA260925C00180000"),
        retained_positions(short_symbol="NVDA260918C00170000"),
        retained_positions(long_symbol="NVDA991399C00170000"),
        retained_positions(multiplier=10),
        tuple(reversed(retained_positions())),
    ),
)
def test_non_vertical_or_invalid_occ_context_is_typed_no_action(positions) -> None:
    target, _ = acquisition(retained=ContextSource(context(expected_positions=positions)))
    with pytest.raises(AcquisitionFailure, match="RETAINED_CONTEXT_INVALID"):
        run(target)


def test_launch_authority_must_match_the_frozen_policy_before_observation() -> None:
    observed = ObservationSource(observation())
    changed = replace(context().launch_authority, entry_policy_hash="e" * 64)
    target, _ = acquisition(
        retained=ContextSource(context(launch_authority=changed)),
        observed=observed,
    )
    with pytest.raises(AcquisitionFailure, match="RETAINED_CONTEXT_INVALID"):
        run(target)
    assert observed.calls == 0


def test_market_component_sources_must_join_the_boundary_evidence() -> None:
    changed = replace(
        observation().underlying,
        benchmark_completed_bar_source_hash="2" * 64,
    )
    target, _ = acquisition(observed=ObservationSource(observation(underlying=changed)))
    with pytest.raises(AcquisitionFailure, match="LIFECYCLE_MARKET_TIMESTAMP_INVALID"):
        run(target)


@pytest.mark.parametrize(
    ("changed", "code"),
    (
        ({"greek_timestamp_source_hash": ""}, "OPTION_EVIDENCE_INVALID"),
        ({"greek_units_source_hash": ""}, "OPTION_EVIDENCE_INVALID"),
        ({"greek_observed_at": None}, "OPTION_EVIDENCE_INVALID"),
        ({"active": False}, "OPTION_EVIDENCE_INVALID"),
        ({"bid_price": Decimal("0")}, "OPTION_EVIDENCE_INVALID"),
    ),
)
def test_unverified_greek_or_quote_evidence_is_typed_no_action(changed, code) -> None:
    changed_options = (replace(options()[0], **changed), options()[1])
    target, _ = acquisition(observed=ObservationSource(observation(options=changed_options)))
    with pytest.raises(AcquisitionFailure, match=code):
        run(target)


def test_greek_timestamp_contract_must_match_versioned_authority() -> None:
    changed_options = (
        replace(options()[0], greek_timestamp_source_hash="1" * 64),
        options()[1],
    )
    target, _ = acquisition(observed=ObservationSource(observation(options=changed_options)))
    with pytest.raises(AcquisitionFailure, match="OPTION_EVIDENCE_INVALID"):
        run(target)


def test_activity_visibility_must_cover_the_provider_horizon_at_assessment() -> None:
    stale_pagination = replace(
        sweep().activity_pagination,
        visibility_complete_through=NOW - timedelta(days=2),
    )
    target, _ = acquisition(
        observed=ObservationSource(
            observation(sweep=replace(sweep(), activity_pagination=stale_pagination))
        )
    )
    with pytest.raises(AcquisitionFailure, match="LIFECYCLE_ACCOUNT_EVIDENCE_INCOMPLETE"):
        run(target)


def test_unexpected_underlying_inventory_blocks_before_research() -> None:
    unexpected = (
        InventoryItem(InventoryKind.EQUITY, "NVDA", Decimal("100"), 1),
        *inventory(),
    )
    research = ResearchSource((cluster(),))
    target, _ = acquisition(
        observed=ObservationSource(
            observation(sweep=sweep(first_positions=unexpected, final_positions=unexpected))
        ),
        research=research,
    )
    with pytest.raises(AcquisitionFailure, match="UNEXPECTED_ACCOUNT_INVENTORY"):
        run(target)
    assert research.calls == 0


def test_assignment_activity_blocks_before_policy() -> None:
    assignment = ActivityItem(
        activity_id_hash="e" * 64,
        activity_type=ActivityType.OPASN,
        occurred_at=NOW - timedelta(minutes=1),
        symbol=SHORT,
        signed_quantity=Decimal("1"),
    )
    target, _ = acquisition(
        observed=ObservationSource(observation(sweep=sweep(activities=(assignment,))))
    )
    with pytest.raises(AcquisitionFailure, match="ACTIVITY_LINEAGE_INCOMPLETE"):
        run(target)


def test_unstable_account_material_blocks_before_policy() -> None:
    unstable = sweep(final_account=account(NOW - timedelta(seconds=2), cash=Decimal("99499")))
    target, _ = acquisition(observed=ObservationSource(observation(sweep=unstable)))
    with pytest.raises(AcquisitionFailure, match="LIFECYCLE_ACCOUNT_EVIDENCE_UNSTABLE"):
        run(target)


def test_invalidation_requires_known_frozen_id_and_contradiction() -> None:
    unbound = classification(invalidates=True, invalidation_condition_id=None)
    target, _ = acquisition(classifier=Classifier((unbound,)))
    with pytest.raises(AcquisitionFailure, match="CLASSIFICATION_SOURCE_BINDING_MISMATCH"):
        run(target)

    unknown = classification(
        relation=EvidenceRelation.CONTRADICTS,
        invalidates=True,
        invalidation_condition_id="UNKNOWN",
    )
    target, _ = acquisition(classifier=Classifier((unknown,)))
    with pytest.raises(AcquisitionFailure, match="CLASSIFICATION_SOURCE_BINDING_MISMATCH"):
        run(target)


def test_bound_invalidation_is_derived_and_closes() -> None:
    bound = classification(
        relation=EvidenceRelation.CONTRADICTS,
        invalidates=True,
        invalidation_condition_id="GUIDANCE_REVERSED",
    )
    target, _ = acquisition(classifier=Classifier((bound,)))
    result = run(target)
    assert result.values.thesis_status is ThesisStatus.BROKEN
    assert (
        evaluate_assessment(result.values).execution_decision is ExecutionDecision.CLOSE_RISK_ONLY
    )
    assert result.proposal is not None
    authorization = result.proposal.authorization
    intent = result.proposal.intent
    assert authorization.action is ExecutionAction.CLOSE
    assert authorization.assessment_id == result.values.assessment_id
    assert authorization.thesis_version_id == result.thesis_version_id
    assert authorization.position_fingerprint == context().position_fingerprint
    assert authorization.expected_after_exposure is None
    assert intent.envelope.legs == (
        OrderLegIntent(LONG, PositionIntent.SELL_TO_CLOSE, 1),
        OrderLegIntent(SHORT, PositionIntent.BUY_TO_CLOSE, 1),
    )
    assert intent.envelope.quantity == 1
    assert intent.envelope.minimum_limit == Decimal("-5.00")
    assert intent.envelope.maximum_limit == Decimal("-4.80")


def test_close_limit_rounding_never_weakens_midpoint_or_natural_bounds() -> None:
    bound = classification(
        relation=EvidenceRelation.CONTRADICTS,
        invalidates=True,
        invalidation_condition_id="GUIDANCE_REVERSED",
    )
    current_options = (
        replace(options()[0], bid_price=Decimal("9.801"), ask_price=Decimal("9.817")),
        replace(options()[1], bid_price=Decimal("4.999"), ask_price=Decimal("5.009")),
    )
    target, _ = acquisition(
        observed=ObservationSource(observation(options=current_options)),
        classifier=Classifier((bound,)),
    )

    result = run(target)

    assert result.proposal is not None
    assert result.proposal.intent.envelope.minimum_limit == Decimal("-4.81")
    assert result.proposal.intent.envelope.maximum_limit == Decimal("-4.79")


def test_source_ids_must_be_globally_unique() -> None:
    clusters = (cluster(), cluster(cluster_id="cluster-2", source_ids=("source-1",)))
    target, _ = acquisition(research=ResearchSource(clusters), classifier=Classifier(()))
    with pytest.raises(AcquisitionFailure, match="RESEARCH_CLUSTER_SET_INVALID"):
        run(target)


def test_research_cluster_count_is_hard_bounded() -> None:
    clusters = tuple(
        cluster(cluster_id=f"cluster-{index}", source_ids=(f"source-{index}",))
        for index in range(13)
    )
    target, _ = acquisition(research=ResearchSource(clusters), classifier=Classifier(()))
    with pytest.raises(AcquisitionFailure, match="RESEARCH_CLUSTER_SET_INVALID"):
        run(target)


def test_retained_transition_count_is_hard_bounded() -> None:
    transitions = (
        context().lifecycle_transitions[0],
        *(
            RetainedLifecycleTransition(
                action="ROLL",
                occurred_at=BASELINE + timedelta(minutes=index),
                market_session_id=UUID(int=1000 + index),
                cashflow=Decimal("0"),
                activity_hashes=(),
            )
            for index in range(1, 65)
        ),
    )
    target, _ = acquisition(retained=ContextSource(context(lifecycle_transitions=transitions)))
    with pytest.raises(AcquisitionFailure, match="RETAINED_CONTEXT_INVALID"):
        run(target)


def test_classification_event_code_is_bounded() -> None:
    target, _ = acquisition(classifier=Classifier((classification(event_code="ACTION"),)))
    with pytest.raises(AcquisitionFailure, match="CLASSIFICATION_SOURCE_BINDING_MISMATCH"):
        run(target)


@pytest.mark.parametrize(
    "changed",
    (
        {
            "underlying": replace(
                observation().underlying,
                quote_observed_at=datetime(2026, 8, 31, 14, 59),
            )
        },
        {
            "underlying": replace(
                observation().underlying,
                completed_bar_at="not-a-time",
            )
        },
    ),
)
def test_malformed_provider_time_is_typed_no_action(changed) -> None:
    target, _ = acquisition(observed=ObservationSource(observation(**changed)))
    with pytest.raises(AcquisitionFailure):
        run(target)


def test_roll_summary_is_derived_from_exact_replacement_vertical() -> None:
    replacement = retained_positions(
        long_symbol="NVDA261002C00170000",
        short_symbol="NVDA261002C00180000",
    )
    roll = LifecycleRollObservation(
        positions=replacement,
        options=options(replacement),
    )
    target, _ = acquisition(observed=ObservationSource(observation(roll=roll)))
    result = run(target)
    assert result.values.roll_candidate is not None
    assert result.values.roll_candidate.covered_verticals
    assert result.values.roll_candidate.within_loss_budget is False


def test_approved_roll_binds_replacement_exposure_and_four_leg_intent() -> None:
    replacement = retained_positions(
        long_symbol="NVDA261002C00170000",
        short_symbol="NVDA261002C00180000",
    )
    current_options = (
        replace(
            options()[0],
            delta=Decimal("0.90"),
            theta_per_day=Decimal("-1"),
            vega_per_iv_point=Decimal("0.50"),
        ),
        replace(
            options()[1],
            delta=Decimal("0.05"),
            theta_per_day=Decimal("-0.01"),
            vega_per_iv_point=Decimal("0.01"),
        ),
    )
    replacement_options = (
        replace(options(replacement)[0], bid_price=Decimal("3.80"), ask_price=Decimal("4")),
        replace(options(replacement)[1], bid_price=Decimal("1"), ask_price=Decimal("1.20")),
    )
    clusters = (cluster(), cluster(cluster_id="cluster-2", source_ids=("source-2",)))
    classifications = (
        classification(),
        classification(
            cluster_id="cluster-2",
            source_ids=("source-2",),
            relation=EvidenceRelation.CONTRADICTS,
        ),
    )
    target, _ = acquisition(
        retained=ContextSource(context(target_at=NOW + timedelta(days=2))),
        observed=ObservationSource(
            observation(
                options=current_options,
                roll=LifecycleRollObservation(replacement, replacement_options),
            )
        ),
        research=ResearchSource(clusters),
        classifier=Classifier(classifications),
    )

    result = run(target)
    policy = evaluate_assessment(result.values)

    assert policy.execution_decision is ExecutionDecision.ROLL_APPROVED
    assert result.proposal is not None
    authorization = result.proposal.authorization
    envelope = result.proposal.intent.envelope
    expected_exposure = GreekExposure(
        delta=Decimal("30"),
        gamma=Decimal("2"),
        theta_per_day=Decimal("-3"),
        vega_per_iv_point=Decimal("5"),
    )
    assert authorization.expected_after_exposure == expected_exposure
    eligible = next(item for item in policy.response.alternatives if item.eligible)
    assert eligible.expected_exposure == expected_exposure
    assert envelope.action is ExecutionAction.ROLL
    assert envelope.legs == (
        OrderLegIntent(LONG, PositionIntent.SELL_TO_CLOSE, 1),
        OrderLegIntent(SHORT, PositionIntent.BUY_TO_CLOSE, 1),
        OrderLegIntent(replacement[0].symbol, PositionIntent.BUY_TO_OPEN, 1),
        OrderLegIntent(replacement[1].symbol, PositionIntent.SELL_TO_OPEN, 1),
    )
    assert envelope.minimum_limit == Decimal("-2.20")
    assert envelope.maximum_limit == Decimal("-1.80")


def test_roll_replacement_is_part_of_the_same_five_second_snapshot() -> None:
    replacement = retained_positions(
        long_symbol="NVDA261002C00170000",
        short_symbol="NVDA261002C00180000",
    )
    stale = tuple(
        replace(
            item,
            quote_observed_at=NOW - timedelta(seconds=29),
            greek_observed_at=NOW - timedelta(seconds=29),
            retrieved_at=NOW - timedelta(seconds=28),
        )
        for item in options(replacement)
    )
    target, _ = acquisition(
        observed=ObservationSource(observation(roll=LifecycleRollObservation(replacement, stale)))
    )
    with pytest.raises(AcquisitionFailure, match="MARKET_EVIDENCE_NOT_SYNCHRONIZED"):
        run(target)


def test_roll_loss_budget_uses_lower_portfolio_cap() -> None:
    replacement = retained_positions(
        long_symbol="NVDA261002C00170000",
        short_symbol="NVDA261002C00180000",
    )
    target, _ = acquisition(
        retained=ContextSource(
            context(approved_max_loss=Decimal("900"), portfolio_risk_cap=Decimal("500"))
        ),
        observed=ObservationSource(
            observation(roll=LifecycleRollObservation(replacement, options(replacement)))
        ),
    )
    result = run(target)
    assert result.values.roll_candidate is not None
    assert result.values.roll_candidate.within_loss_budget is False


def test_roll_count_is_derived_from_retained_transition_lineage() -> None:
    replacement = retained_positions(
        long_symbol="NVDA261002C00170000",
        short_symbol="NVDA261002C00180000",
    )
    prior_roll = RetainedLifecycleTransition(
        action="ROLL",
        occurred_at=NOW - timedelta(hours=16),
        market_session_id=SESSION_ID,
        cashflow=Decimal("10"),
        activity_hashes=("e" * 64,),
    )
    target, _ = acquisition(
        retained=ContextSource(
            context(lifecycle_transitions=(*context().lifecycle_transitions, prior_roll))
        ),
        observed=ObservationSource(
            observation(
                sweep=replace(
                    sweep(),
                    activities=(
                        ActivityItem(
                            activity_id_hash="e" * 64,
                            activity_type=ActivityType.OPTRD,
                            occurred_at=NOW - timedelta(hours=1),
                            symbol=SHORT,
                            signed_quantity=Decimal("1"),
                        ),
                        *sweep().activities,
                    ),
                ),
                roll=LifecycleRollObservation(replacement, options(replacement)),
            )
        ),
    )
    result = run(target)
    assert result.values.roll_candidate is not None
    assert result.values.roll_candidate.no_prior_roll_today is False


def test_roll_count_uses_authoritative_market_session_not_utc_date() -> None:
    replacement = retained_positions(
        long_symbol="NVDA261002C00170000",
        short_symbol="NVDA261002C00180000",
    )
    prior_roll = RetainedLifecycleTransition(
        action="ROLL",
        occurred_at=NOW - timedelta(hours=1),
        market_session_id=UUID("00000000-0000-0000-0000-000000000499"),
        cashflow=Decimal("10"),
        activity_hashes=("e" * 64,),
    )
    target, _ = acquisition(
        retained=ContextSource(
            context(lifecycle_transitions=(*context().lifecycle_transitions, prior_roll))
        ),
        observed=ObservationSource(
            observation(
                sweep=replace(
                    sweep(),
                    activities=(
                        ActivityItem(
                            activity_id_hash="e" * 64,
                            activity_type=ActivityType.OPTRD,
                            occurred_at=NOW - timedelta(hours=1),
                            symbol=SHORT,
                            signed_quantity=Decimal("1"),
                        ),
                        *sweep().activities,
                    ),
                ),
                roll=LifecycleRollObservation(replacement, options(replacement)),
            )
        ),
    )
    result = run(target)
    assert result.values.roll_candidate is not None
    assert result.values.roll_candidate.no_prior_roll_today is True


def test_arbitrary_greek_authority_hashes_do_not_prove_units() -> None:
    changed_options = tuple(replace(item, greek_units_source_hash="1" * 64) for item in options())
    target, _ = acquisition(observed=ObservationSource(observation(options=changed_options)))
    with pytest.raises(AcquisitionFailure, match="OPTION_EVIDENCE_INVALID"):
        run(target)


def test_market_evidence_requires_a_bounded_cross_source_sync_window() -> None:
    changed_options = (
        replace(
            options()[0],
            quote_observed_at=NOW - timedelta(seconds=29),
            greek_observed_at=NOW - timedelta(seconds=29),
            retrieved_at=NOW - timedelta(seconds=28),
        ),
        options()[1],
    )
    target, _ = acquisition(observed=ObservationSource(observation(options=changed_options)))
    with pytest.raises(AcquisitionFailure, match="MARKET_EVIDENCE_NOT_SYNCHRONIZED"):
        run(target)


def test_activity_window_must_begin_at_lifecycle_origin() -> None:
    changed = sweep()
    pagination = replace(
        changed.activity_pagination,
        requested_start=NOW - timedelta(days=2),
        visibility_complete_through=NOW - timedelta(days=1, seconds=3),
    )
    changed = replace(changed, activity_pagination=pagination)
    target, _ = acquisition(observed=ObservationSource(observation(sweep=changed)))
    with pytest.raises(AcquisitionFailure, match="ACTIVITY_LINEAGE_INCOMPLETE"):
        run(target)


def test_wide_roll_package_is_rejected() -> None:
    replacement = retained_positions(
        long_symbol="NVDA261002C00170000",
        short_symbol="NVDA261002C00180000",
    )
    replacement_options = (
        replace(options(replacement)[0], bid_price=Decimal("1"), ask_price=Decimal("5")),
        replace(options(replacement)[1], bid_price=Decimal("0.10"), ask_price=Decimal("4.10")),
    )
    roll = LifecycleRollObservation(
        positions=replacement,
        options=replacement_options,
    )
    target, _ = acquisition(observed=ObservationSource(observation(roll=roll)))
    result = run(target)
    assert result.values.roll_candidate is None
    assert result.proposal is None


def test_wide_replacement_leg_is_rejected_even_when_package_is_within_bound() -> None:
    replacement = retained_positions(
        long_symbol="NVDA261002C00170000",
        short_symbol="NVDA261002C00180000",
    )
    replacement_options = (
        replace(options(replacement)[0], bid_price=Decimal("6"), ask_price=Decimal("10")),
        replace(options(replacement)[1], bid_price=Decimal("5"), ask_price=Decimal("5.01")),
    )
    target, _ = acquisition(
        observed=ObservationSource(
            observation(roll=LifecycleRollObservation(replacement, replacement_options))
        )
    )

    result = run(target)

    assert result.values.roll_candidate is None
    assert result.proposal is None


def test_wide_current_leg_is_rejected_even_when_package_is_within_bound() -> None:
    replacement = retained_positions(
        long_symbol="NVDA261002C00170000",
        short_symbol="NVDA261002C00180000",
    )
    current_options = (
        replace(options()[0], bid_price=Decimal("6"), ask_price=Decimal("10")),
        replace(options()[1], bid_price=Decimal("5"), ask_price=Decimal("5.01")),
    )
    replacement_options = (
        replace(options(replacement)[0], bid_price=Decimal("10"), ask_price=Decimal("10.01")),
        replace(options(replacement)[1], bid_price=Decimal("5"), ask_price=Decimal("5.01")),
    )
    target, _ = acquisition(
        observed=ObservationSource(
            observation(
                options=current_options,
                roll=LifecycleRollObservation(replacement, replacement_options),
            )
        )
    )

    result = run(target)

    all_options = (*current_options, *replacement_options)
    package_width = sum(item.ask_price - item.bid_price for item in all_options)
    package_midpoint = sum((item.ask_price + item.bid_price) / Decimal(2) for item in all_options)
    assert package_width / package_midpoint < Decimal("0.25")
    assert (current_options[0].ask_price - current_options[0].bid_price) / (
        (current_options[0].ask_price + current_options[0].bid_price) / Decimal(2)
    ) > Decimal("0.25")
    assert result.values.roll_candidate is None


def test_roll_liquidity_exact_policy_boundary_passes_and_excess_fails() -> None:
    replacement = retained_positions(
        long_symbol="NVDA261002C00170000",
        short_symbol="NVDA261002C00180000",
    )
    replacement_options = (
        replace(options(replacement)[0], bid_price=Decimal("10.00"), ask_price=Decimal("10.01")),
        replace(options(replacement)[1], bid_price=Decimal("5.00"), ask_price=Decimal("5.01")),
    )
    roll = LifecycleRollObservation(replacement, replacement_options)
    broad, _ = acquisition(observed=ObservationSource(observation(roll=roll)))
    broad_candidate = run(broad).values.roll_candidate
    assert broad_candidate is not None
    boundary = max(
        broad_candidate.relative_spread,
        *(
            (
                (item.ask_price - item.bid_price) / ((item.ask_price + item.bid_price) / Decimal(2))
            ).quantize(Decimal("0.0000000001"), rounding=ROUND_CEILING)
            for item in (*options(), *replacement_options)
        ),
    )

    exact_context = context(
        maximum_relative_spread=boundary,
        liquidity_authority_hash=lifecycle_liquidity_authority_hash(POLICY_HASH, boundary),
    )
    exact, _ = acquisition(
        retained=ContextSource(exact_context),
        observed=ObservationSource(observation(roll=roll)),
    )
    assert run(exact).values.roll_candidate is not None

    tighter = boundary - Decimal("0.000000001")
    failed, _ = acquisition(
        retained=ContextSource(
            context(
                maximum_relative_spread=tighter,
                liquidity_authority_hash=lifecycle_liquidity_authority_hash(
                    POLICY_HASH,
                    tighter,
                ),
            )
        ),
        observed=ObservationSource(observation(roll=roll)),
    )
    assert run(failed).values.roll_candidate is None


def test_roll_liquidity_policy_substitution_fails_closed() -> None:
    target, _ = acquisition(
        retained=ContextSource(context(maximum_relative_spread=Decimal("0.20")))
    )

    with pytest.raises(AcquisitionFailure, match="RETAINED_CONTEXT_INVALID"):
        run(target)


@pytest.mark.parametrize(
    ("short_bid", "expected"),
    (("1.20", Decimal("500")), ("1.10", None)),
)
def test_roll_incremental_debit_has_independent_exact_budget_bound(
    short_bid: str,
    expected: Decimal | None,
) -> None:
    replacement = retained_positions(
        long_symbol="NVDA261002C00170000",
        short_symbol="NVDA261002C00180000",
    )
    replacement_options = (
        replace(options(replacement)[0], bid_price=Decimal("10.90"), ask_price=Decimal("11")),
        replace(
            options(replacement)[1],
            bid_price=Decimal(short_bid),
            ask_price=Decimal(short_bid) + Decimal("0.10"),
        ),
    )
    target, _ = acquisition(
        observed=ObservationSource(
            observation(
                roll=LifecycleRollObservation(replacement, replacement_options),
            )
        )
    )

    candidate = run(target).values.roll_candidate

    if expected is None:
        assert candidate is None
    else:
        assert candidate is not None
        assert candidate.incremental_debit == expected
        assert candidate.maximum_incremental_debit == Decimal("500")


def test_roll_selection_skips_illiquid_earliest_expiry() -> None:
    early = retained_positions(
        long_symbol="NVDA260925C00170000",
        short_symbol="NVDA260925C00180000",
    )
    later = retained_positions(
        long_symbol="NVDA261002C00170000",
        short_symbol="NVDA261002C00180000",
    )
    illiquid = LifecycleRollObservation(
        early,
        (
            replace(options(early)[0], bid_price=Decimal("6"), ask_price=Decimal("10")),
            replace(options(early)[1], bid_price=Decimal("5"), ask_price=Decimal("5.01")),
        ),
    )
    eligible = LifecycleRollObservation(later, options(later))
    target, _ = acquisition(
        observed=ObservationSource(observation(roll_candidates=(illiquid, eligible)))
    )

    result = run(target)

    assert result.values.roll_candidate is not None
    assert result.values.roll_candidate.expiry_extension_days == 14


def test_roll_selection_is_deterministic_by_debit_then_quoted_spread() -> None:
    replacement = retained_positions(
        long_symbol="NVDA261002C00170000",
        short_symbol="NVDA261002C00180000",
    )
    expensive = LifecycleRollObservation(replacement, options(replacement))
    low_debit_wide = LifecycleRollObservation(
        replacement,
        (
            replace(options(replacement)[0], bid_price=Decimal("9.50"), ask_price=Decimal("10")),
            replace(options(replacement)[1], bid_price=Decimal("5"), ask_price=Decimal("5.40")),
        ),
    )
    low_debit_narrow = LifecycleRollObservation(
        replacement,
        (
            replace(options(replacement)[0], bid_price=Decimal("9.99"), ask_price=Decimal("10")),
            replace(options(replacement)[1], bid_price=Decimal("5"), ask_price=Decimal("5.01")),
        ),
    )

    selected = []
    for candidates in (
        (expensive, low_debit_wide, low_debit_narrow),
        (low_debit_narrow, low_debit_wide, expensive),
    ):
        target, _ = acquisition(observed=ObservationSource(observation(roll_candidates=candidates)))
        candidate = run(target).values.roll_candidate
        assert candidate is not None
        selected.append((candidate.incremental_debit, candidate.relative_spread))

    assert selected[0] == selected[1]
    assert selected[0][0] == Decimal("20")
    assert selected[0][1] == Decimal("0.0139072848")


def test_assessment_binds_stable_exact_acquisition_manifest() -> None:
    first, _ = acquisition()
    second, _ = acquisition()
    one = run(first)
    two = run(second)
    assert one.values.acquisition_manifest_id == two.values.acquisition_manifest_id
    assert len(one.values.acquisition_manifest_hash) == 64


@pytest.mark.parametrize(
    ("stage", "error", "expected"),
    (
        ("retained", "THESIS_STORAGE_OFFLINE", "CONTEXT_THESIS_STORAGE_OFFLINE"),
        ("observed", "ALPACA_TIMEOUT", "OBSERVATION_ALPACA_TIMEOUT"),
        ("research", "MCP_TIMEOUT", "RESEARCH_MCP_TIMEOUT"),
        ("classifier", "MODEL_QUOTA", "CLASSIFICATION_MODEL_QUOTA"),
    ),
)
def test_source_failures_are_stage_qualified(stage, error, expected) -> None:
    source_type = {
        "retained": ContextSource,
        "observed": ObservationSource,
        "research": ResearchSource,
        "classifier": Classifier,
    }[stage]
    target, _ = acquisition(**{stage: source_type(error=error)})
    with pytest.raises(AcquisitionFailure, match=expected):
        run(target)

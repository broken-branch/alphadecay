from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from backend.app.alpaca.opportunity_runtime import (
    OpportunityCatalystRuntimeAdapter,
    OpportunityRuntimeAdapterError,
)
from backend.app.contracts.v1 import (
    EvidenceClassification,
    EvidenceRelation,
    EvidenceTier,
    SourceCluster,
)
from backend.app.policy.opportunity import (
    CatalystQuality,
    OpportunityPolicy,
    opportunity_policy_hash,
)
from backend.app.services.opportunity_catalyst import (
    BoundedOpportunityCatalystAuthority,
    CatalystAuthorityError,
    CatalystEvidencePlan,
    CatalystResearchBundle,
    CatalystSourceEvidence,
    catalyst_evidence_plan_digest,
    catalyst_research_bundle_digest,
    catalyst_source_evidence_digest,
)

BOUNDARY = datetime(2026, 8, 31, 15, tzinfo=UTC)
TRUSTED_AT = BOUNDARY + timedelta(minutes=1)


def _policy() -> OpportunityPolicy:
    return OpportunityPolicy(
        version="test-v1",
        opportunity_key="ACME_EARNINGS",
        underlying="ACME",
        selected_decision_boundary=BOUNDARY,
        last_entry_boundary=BOUNDARY + timedelta(minutes=15),
        maximum_decision_delay=timedelta(minutes=5),
        maximum_underlying_age=timedelta(minutes=2),
        maximum_catalyst_age=timedelta(hours=2),
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


def _plan(policy: OpportunityPolicy) -> CatalystEvidencePlan:
    return CatalystEvidencePlan(
        opportunity_key=policy.opportunity_key,
        underlying=policy.underlying,
        plan_hash="a" * 64,
        policy_hash=opportunity_policy_hash(policy),
        thesis_code="POST_EVENT_CONTINUATION",
        allowed_event_codes=("RESULTS", "GUIDANCE"),
        invalidation_codes=("GUIDANCE_REVERSED",),
        evidence_window_start=BOUNDARY - timedelta(days=1),
        evidence_window_end=BOUNDARY,
        frozen_at=BOUNDARY - timedelta(days=2),
    )


def _source() -> CatalystSourceEvidence:
    value = CatalystSourceEvidence(
        source_id="issuer-results",
        source_kind="MCP_NEWS",
        request_hash="b" * 64,
        result_hash="c" * 64,
        observed_at=BOUNDARY - timedelta(hours=1),
        retrieved_at=TRUSTED_AT,
        source_hash="",
    )
    return replace(value, source_hash=catalyst_source_evidence_digest(value))


def _bundle(plan: CatalystEvidencePlan) -> CatalystResearchBundle:
    source = _source()
    value = CatalystResearchBundle(
        opportunity_key=plan.opportunity_key,
        plan_hash=plan.plan_hash,
        policy_hash=plan.policy_hash,
        criteria_hash=catalyst_evidence_plan_digest(plan),
        clusters=(
            SourceCluster(
                cluster_id="news:issuer-results",
                source_ids=(source.source_id,),
                headline="Issuer reports results and maintains guidance",
                observed_at=source.observed_at,
                source_tier=EvidenceTier.PRIMARY,
            ),
        ),
        sources=(source,),
        retrieved_at=TRUSTED_AT,
        source_hash="",
    )
    return replace(value, source_hash=catalyst_research_bundle_digest(value))


def _rehash_bundle(value: CatalystResearchBundle) -> CatalystResearchBundle:
    return replace(value, source_hash=catalyst_research_bundle_digest(value))


def _classification() -> EvidenceClassification:
    return EvidenceClassification(
        cluster_id="news:issuer-results",
        source_ids=("issuer-results",),
        event_code="RESULTS",
        relation=EvidenceRelation.SUPPORTS,
        materiality=3,
        relevance=Decimal("0.9"),
        confidence=Decimal("0.8"),
        source_tier=EvidenceTier.PRIMARY,
    )


def _two_source_bundle(plan: CatalystEvidencePlan) -> CatalystResearchBundle:
    first = _source()
    second = CatalystSourceEvidence(
        source_id="wire-guidance",
        source_kind="MCP_NEWS",
        request_hash="d" * 64,
        result_hash="e" * 64,
        observed_at=BOUNDARY - timedelta(minutes=30),
        retrieved_at=TRUSTED_AT,
        source_hash="",
    )
    second = replace(second, source_hash=catalyst_source_evidence_digest(second))
    value = CatalystResearchBundle(
        opportunity_key=plan.opportunity_key,
        plan_hash=plan.plan_hash,
        policy_hash=plan.policy_hash,
        criteria_hash=catalyst_evidence_plan_digest(plan),
        clusters=(
            _bundle(plan).clusters[0],
            SourceCluster(
                cluster_id="news:wire-guidance",
                source_ids=(second.source_id,),
                headline="Independent report describes a guidance change",
                observed_at=second.observed_at,
                source_tier=EvidenceTier.SECONDARY,
            ),
        ),
        sources=(first, second),
        retrieved_at=TRUSTED_AT,
        source_hash="",
    )
    return _rehash_bundle(value)


class Research:
    def __init__(self, bundle: CatalystResearchBundle) -> None:
        self.bundle = bundle

    async def research(
        self, plan: CatalystEvidencePlan, trusted_at: datetime
    ) -> CatalystResearchBundle:
        assert plan == _plan(_policy())
        assert trusted_at == TRUSTED_AT
        return self.bundle


class Classifier:
    def classify(
        self,
        plan: CatalystEvidencePlan,
        clusters: tuple[SourceCluster, ...],
    ) -> tuple[EvidenceClassification, ...]:
        assert plan == _plan(_policy())
        assert clusters == _bundle(plan).clusters
        return (_classification(),)


class FixedClassifier:
    def __init__(self, values: tuple[EvidenceClassification, ...]) -> None:
        self.values = values

    def classify(
        self,
        _plan: CatalystEvidencePlan,
        _clusters: tuple[SourceCluster, ...],
    ) -> tuple[EvidenceClassification, ...]:
        return self.values


class CountingClassifier:
    def __init__(self) -> None:
        self.calls = 0

    def classify(self, *_args: object) -> tuple[EvidenceClassification, ...]:
        self.calls += 1
        return ()


def test_produces_plan_bound_clear_authority_without_direction_output() -> None:
    policy = _policy()
    plan = _plan(policy)

    result = asyncio.run(
        BoundedOpportunityCatalystAuthority(Research(_bundle(plan)), Classifier()).produce(
            plan=plan, policy=policy, trusted_at=TRUSTED_AT
        )
    )

    assert result.authority.opportunity_key == "ACME_EARNINGS"
    assert result.authority.quality is CatalystQuality.CLEAR
    assert result.authority.score == 72
    assert result.authority.observed_at == _bundle(plan).clusters[0].observed_at
    assert result.plan_hash == "a" * 64
    assert result.policy_hash == opportunity_policy_hash(policy)
    assert result.criteria_hash == catalyst_evidence_plan_digest(plan)
    assert result.research_source_hash == _bundle(plan).source_hash
    assert len(result.classification_hash) == 64
    assert result.authority.source_hash == result.authority_hash
    assert not hasattr(result, "direction")


def test_runtime_adapter_preserves_the_bounded_catalyst_authority() -> None:
    policy = _policy()
    plan = _plan(policy)
    target = BoundedOpportunityCatalystAuthority(Research(_bundle(plan)), Classifier())

    result = asyncio.run(
        OpportunityCatalystRuntimeAdapter(target).produce(
            plan=plan,
            policy=policy,
            trusted_at=TRUSTED_AT,
        )
    )

    assert result.authority.source_hash == result.authority_hash
    assert result.plan_hash == plan.plan_hash
    assert result.policy_hash == opportunity_policy_hash(policy)


def test_runtime_adapter_rejects_tampered_catalyst_authority() -> None:
    policy = _policy()
    plan = _plan(policy)
    valid = asyncio.run(
        BoundedOpportunityCatalystAuthority(Research(_bundle(plan)), Classifier()).produce(
            plan=plan,
            policy=policy,
            trusted_at=TRUSTED_AT,
        )
    )

    class TamperedCatalystTarget:
        async def produce(self, **_kwargs):
            return replace(valid, authority_hash="f" * 64)

    with pytest.raises(
        OpportunityRuntimeAdapterError,
        match="OPPORTUNITY_CATALYST_AUTHORITY_INVALID",
    ):
        asyncio.run(
            OpportunityCatalystRuntimeAdapter(TamperedCatalystTarget()).produce(
                plan=plan,
                policy=policy,
                trusted_at=TRUSTED_AT,
            )
        )


class ForbiddenResearch:
    async def research(self, *_args: object) -> CatalystResearchBundle:
        raise AssertionError("research must not run")


class ForbiddenClassifier:
    def classify(self, *_args: object) -> tuple[EvidenceClassification, ...]:
        raise AssertionError("classification must not run")


def test_rejects_unfrozen_or_policy_mismatched_plan_before_research() -> None:
    policy = _policy()
    service = BoundedOpportunityCatalystAuthority(ForbiddenResearch(), ForbiddenClassifier())
    invalid = (
        replace(_plan(policy), frozen_at=BOUNDARY + timedelta(microseconds=1)),
        replace(_plan(policy), policy_hash="f" * 64),
        replace(_plan(policy), opportunity_key="OTHER"),
        replace(_plan(policy), thesis_code="not-a-code"),
        replace(_plan(policy), allowed_event_codes=()),
        replace(_plan(policy), invalidation_codes=()),
        replace(_plan(policy), plan_hash=None),  # type: ignore[arg-type]
    )

    for plan in invalid:
        try:
            asyncio.run(service.produce(plan=plan, policy=policy, trusted_at=TRUSTED_AT))
        except CatalystAuthorityError as error:
            assert error.code == "CATALYST_PLAN_INVALID"
        else:
            raise AssertionError("invalid plan was accepted")


def test_complete_empty_research_returns_missing_without_model_call() -> None:
    policy = _policy()
    plan = _plan(policy)
    bundle = CatalystResearchBundle(
        opportunity_key=plan.opportunity_key,
        plan_hash=plan.plan_hash,
        policy_hash=plan.policy_hash,
        criteria_hash=catalyst_evidence_plan_digest(plan),
        clusters=(),
        sources=(),
        retrieved_at=TRUSTED_AT,
        source_hash="",
    )
    bundle = replace(bundle, source_hash=catalyst_research_bundle_digest(bundle))
    classifier = CountingClassifier()

    result = asyncio.run(
        BoundedOpportunityCatalystAuthority(Research(bundle), classifier).produce(
            plan=plan, policy=policy, trusted_at=TRUSTED_AT
        )
    )

    assert result.authority.quality is CatalystQuality.MISSING
    assert result.authority.score == 0
    assert classifier.calls == 0


def test_rejects_stale_malformed_or_unbound_research_before_classification() -> None:
    policy = _policy()
    plan = _plan(policy)
    valid = _bundle(plan)
    source = valid.sources[0]
    cluster = valid.clusters[0]
    malformed_source = replace(source, source_hash="f" * 64)
    malformed_source_id = replace(source, source_id=None)  # type: ignore[arg-type]
    duplicate_source = replace(source, source_id="other-source")
    duplicate_source = replace(
        duplicate_source,
        source_hash=catalyst_source_evidence_digest(duplicate_source),
    )
    invalid = (
        replace(valid, source_hash="f" * 64),
        _rehash_bundle(replace(valid, criteria_hash="f" * 64)),
        _rehash_bundle(replace(valid, retrieved_at=BOUNDARY - timedelta(hours=3))),
        _rehash_bundle(replace(valid, sources=(malformed_source,))),
        _rehash_bundle(replace(valid, sources=(malformed_source_id,))),
        _rehash_bundle(
            replace(
                valid,
                sources=(
                    replace(
                        source,
                        observed_at=plan.evidence_window_start - timedelta(microseconds=1),
                        source_hash="",
                    ),
                ),
            )
        ),
        _rehash_bundle(replace(valid, sources=(source, duplicate_source))),
        _rehash_bundle(
            replace(
                valid,
                clusters=(cluster.model_copy(update={"source_ids": ("unknown-source",)}),),
            )
        ),
    )
    service = BoundedOpportunityCatalystAuthority(ForbiddenResearch(), ForbiddenClassifier())

    for bundle in invalid:
        if bundle.sources and bundle.sources[0].source_hash == "":
            first = bundle.sources[0]
            first = replace(first, source_hash=catalyst_source_evidence_digest(first))
            bundle = _rehash_bundle(replace(bundle, sources=(first,)))
        service = BoundedOpportunityCatalystAuthority(Research(bundle), ForbiddenClassifier())
        try:
            asyncio.run(service.produce(plan=plan, policy=policy, trusted_at=TRUSTED_AT))
        except CatalystAuthorityError as error:
            assert error.code == "CATALYST_RESEARCH_INVALID"
        else:
            raise AssertionError("invalid research was accepted")


def test_rejects_fresh_retrieval_of_stale_source_and_duplicate_result() -> None:
    policy = _policy()
    plan = _plan(policy)
    valid = _bundle(plan)
    stale_source = replace(
        valid.sources[0],
        observed_at=TRUSTED_AT - policy.maximum_catalyst_age - timedelta(microseconds=1),
        source_hash="",
    )
    stale_source = replace(
        stale_source,
        source_hash=catalyst_source_evidence_digest(stale_source),
    )
    stale_cluster = valid.clusters[0].model_copy(
        update={"observed_at": stale_source.observed_at}
    )
    stale_bundle = _rehash_bundle(
        replace(valid, sources=(stale_source,), clusters=(stale_cluster,))
    )

    duplicate_bundle = _two_source_bundle(plan)
    duplicate_source = replace(
        duplicate_bundle.sources[1],
        result_hash=duplicate_bundle.sources[0].result_hash,
        source_hash="",
    )
    duplicate_source = replace(
        duplicate_source,
        source_hash=catalyst_source_evidence_digest(duplicate_source),
    )
    duplicate_bundle = _rehash_bundle(
        replace(
            duplicate_bundle,
            sources=(duplicate_bundle.sources[0], duplicate_source),
        )
    )

    for bundle in (stale_bundle, duplicate_bundle):
        try:
            asyncio.run(
                BoundedOpportunityCatalystAuthority(
                    Research(bundle), ForbiddenClassifier()
                ).produce(plan=plan, policy=policy, trusted_at=TRUSTED_AT)
            )
        except CatalystAuthorityError as error:
            assert error.code == "CATALYST_RESEARCH_INVALID"
        else:
            raise AssertionError("stale or duplicated research was accepted")


def test_rejects_classification_that_omits_or_rewrites_plan_bound_evidence() -> None:
    policy = _policy()
    plan = _plan(policy)
    bundle = _bundle(plan)
    valid = _classification()
    invalid = (
        (),
        (valid, valid),
        (valid.model_copy(update={"event_code": "PRODUCT"}),),
        (valid.model_copy(update={"source_ids": ("unknown-source",)}),),
        (valid.model_copy(update={"source_tier": EvidenceTier.SECONDARY}),),
        (
            valid.model_copy(
                update={
                    "relation": EvidenceRelation.SUPPORTS,
                    "invalidates": True,
                    "invalidation_condition_id": "GUIDANCE_REVERSED",
                }
            ),
        ),
        (
            valid.model_copy(
                update={
                    "relation": EvidenceRelation.CONTRADICTS,
                    "invalidates": True,
                    "invalidation_condition_id": "UNPLANNED_INVALIDATION",
                }
            ),
        ),
    )

    for classifications in invalid:
        try:
            asyncio.run(
                BoundedOpportunityCatalystAuthority(
                    Research(bundle), FixedClassifier(classifications)
                ).produce(plan=plan, policy=policy, trusted_at=TRUSTED_AT)
            )
        except CatalystAuthorityError as error:
            assert error.code == "CATALYST_CLASSIFICATION_INVALID"
        else:
            raise AssertionError("invalid classification was accepted")


def test_primary_planned_invalidation_is_authoritative_contradiction() -> None:
    policy = _policy()
    plan = _plan(policy)
    contradiction = _classification().model_copy(
        update={
            "event_code": "GUIDANCE",
            "relation": EvidenceRelation.CONTRADICTS,
            "invalidates": True,
            "invalidation_condition_id": "GUIDANCE_REVERSED",
        }
    )

    result = asyncio.run(
        BoundedOpportunityCatalystAuthority(
            Research(_bundle(plan)), FixedClassifier((contradiction,))
        ).produce(plan=plan, policy=policy, trusted_at=TRUSTED_AT)
    )

    assert result.authority.quality is CatalystQuality.AUTHORITATIVE_CONTRADICTION
    assert result.authority.score == 0


def test_low_confidence_primary_invalidation_still_fires_frozen_gate() -> None:
    policy = _policy()
    plan = _plan(policy)
    contradiction = _classification().model_copy(
        update={
            "event_code": "GUIDANCE",
            "relation": EvidenceRelation.CONTRADICTS,
            "relevance": Decimal("0.1"),
            "confidence": Decimal("0.1"),
            "invalidates": True,
            "invalidation_condition_id": "GUIDANCE_REVERSED",
        }
    )

    result = asyncio.run(
        BoundedOpportunityCatalystAuthority(
            Research(_bundle(plan)), FixedClassifier((contradiction,))
        ).produce(plan=plan, policy=policy, trusted_at=TRUSTED_AT)
    )

    assert result.authority.quality is CatalystQuality.AUTHORITATIVE_CONTRADICTION
    assert result.authority.score == 0


def test_conflicting_non_authoritative_evidence_fails_closed_as_unresolved() -> None:
    policy = _policy()
    plan = _plan(policy)
    bundle = _two_source_bundle(plan)
    contradiction = EvidenceClassification(
        cluster_id="news:wire-guidance",
        source_ids=("wire-guidance",),
        event_code="GUIDANCE",
        relation=EvidenceRelation.CONTRADICTS,
        materiality=2,
        relevance=Decimal("0.9"),
        confidence=Decimal("0.9"),
        source_tier=EvidenceTier.SECONDARY,
    )

    result = asyncio.run(
        BoundedOpportunityCatalystAuthority(
            Research(bundle), FixedClassifier((_classification(), contradiction))
        ).produce(plan=plan, policy=policy, trusted_at=TRUSTED_AT)
    )

    assert result.authority.quality is CatalystQuality.UNRESOLVED_RISK
    assert result.authority.score == 0


def test_two_independent_original_reports_verify_a_planned_invalidation() -> None:
    policy = _policy()
    plan = _plan(policy)
    bundle = _two_source_bundle(plan)
    clusters = tuple(
        cluster.model_copy(
            update={
                "source_tier": EvidenceTier.ORIGINAL_REPORTING,
                "independent_reporting_group": f"group-{index}",
            }
        )
        for index, cluster in enumerate(bundle.clusters, start=1)
    )
    bundle = _rehash_bundle(replace(bundle, clusters=clusters))
    classifications = tuple(
        EvidenceClassification(
            cluster_id=cluster.cluster_id,
            source_ids=cluster.source_ids,
            event_code="GUIDANCE",
            relation=EvidenceRelation.CONTRADICTS,
            materiality=2,
            relevance=Decimal("0.8"),
            confidence=Decimal("0.9"),
            source_tier=cluster.source_tier,
            independent_reporting_group=cluster.independent_reporting_group,
            invalidates=True,
            invalidation_condition_id="GUIDANCE_REVERSED",
        )
        for cluster in clusters
    )

    result = asyncio.run(
        BoundedOpportunityCatalystAuthority(
            Research(bundle), FixedClassifier(classifications)
        ).produce(plan=plan, policy=policy, trusted_at=TRUSTED_AT)
    )

    assert result.authority.quality is CatalystQuality.AUTHORITATIVE_CONTRADICTION
    assert result.authority.score == 0


def test_neutral_evidence_cannot_become_a_clear_catalyst() -> None:
    policy = _policy()
    plan = _plan(policy)
    neutral = _classification().model_copy(update={"relation": EvidenceRelation.NEUTRAL})

    result = asyncio.run(
        BoundedOpportunityCatalystAuthority(
            Research(_bundle(plan)), FixedClassifier((neutral,))
        ).produce(plan=plan, policy=policy, trusted_at=TRUSTED_AT)
    )

    assert result.authority.quality is CatalystQuality.UNRESOLVED_RISK
    assert result.authority.score == 0


def test_clear_score_applies_application_owned_source_tier_weight() -> None:
    policy = _policy()
    plan = _plan(policy)
    bundle = _bundle(plan)
    secondary_cluster = bundle.clusters[0].model_copy(
        update={"source_tier": EvidenceTier.SECONDARY}
    )
    bundle = _rehash_bundle(replace(bundle, clusters=(secondary_cluster,)))
    secondary = _classification().model_copy(
        update={"source_tier": EvidenceTier.SECONDARY}
    )

    result = asyncio.run(
        BoundedOpportunityCatalystAuthority(
            Research(bundle), FixedClassifier((secondary,))
        ).produce(plan=plan, policy=policy, trusted_at=TRUSTED_AT)
    )

    assert result.authority.quality is CatalystQuality.CLEAR
    assert result.authority.score == 36


def test_rejects_constructed_classifier_enum_substitution() -> None:
    policy = _policy()
    plan = _plan(policy)
    malformed = _classification().model_copy()
    object.__setattr__(malformed, "relation", "SUPPORTS")

    try:
        asyncio.run(
            BoundedOpportunityCatalystAuthority(
                Research(_bundle(plan)), FixedClassifier((malformed,))
            ).produce(plan=plan, policy=policy, trusted_at=TRUSTED_AT)
        )
    except CatalystAuthorityError as error:
        assert error.code == "CATALYST_CLASSIFICATION_INVALID"
    else:
        raise AssertionError("classifier enum substitution was accepted")


class FailedResearch:
    async def research(self, *_args: object) -> CatalystResearchBundle:
        raise TimeoutError("provider details must not escape")


class FailedClassifier:
    def classify(self, *_args: object) -> tuple[EvidenceClassification, ...]:
        raise RuntimeError("model details must not escape")


def test_provider_and_classifier_failure_have_stable_fail_closed_codes() -> None:
    policy = _policy()
    plan = _plan(policy)
    cases = (
        (
            BoundedOpportunityCatalystAuthority(FailedResearch(), ForbiddenClassifier()),
            "CATALYST_RESEARCH_UNAVAILABLE",
        ),
        (
            BoundedOpportunityCatalystAuthority(Research(_bundle(plan)), FailedClassifier()),
            "CATALYST_CLASSIFICATION_UNAVAILABLE",
        ),
    )

    for service, expected in cases:
        try:
            asyncio.run(service.produce(plan=plan, policy=policy, trusted_at=TRUSTED_AT))
        except CatalystAuthorityError as error:
            assert error.code == expected
            assert str(error) == expected
        else:
            raise AssertionError("provider failure did not fail closed")

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import Enum
from typing import Protocol

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
from backend.app.services.opportunity_input import CatalystAuthority

_RETRIEVAL_SKEW_TOLERANCE = timedelta(seconds=30)


_HASH = re.compile(r"^[0-9a-f]{64}$")
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_UNDERLYING = re.compile(r"^[A-Z]{1,6}$")
_SOURCE_WEIGHTS = {
    EvidenceTier.PRIMARY: Decimal("1.0"),
    EvidenceTier.ORIGINAL_REPORTING: Decimal("0.8"),
    EvidenceTier.SECONDARY: Decimal("0.5"),
}


class CatalystAuthorityError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CatalystEvidencePlan:
    opportunity_key: str
    underlying: str
    plan_hash: str
    policy_hash: str
    thesis_code: str
    allowed_event_codes: tuple[str, ...]
    invalidation_codes: tuple[str, ...]
    evidence_window_start: datetime
    evidence_window_end: datetime
    frozen_at: datetime


@dataclass(frozen=True)
class CatalystSourceEvidence:
    source_id: str
    source_kind: str
    request_hash: str
    result_hash: str
    observed_at: datetime
    retrieved_at: datetime
    source_hash: str


@dataclass(frozen=True)
class CatalystResearchBundle:
    opportunity_key: str
    plan_hash: str
    policy_hash: str
    criteria_hash: str
    clusters: tuple[SourceCluster, ...]
    sources: tuple[CatalystSourceEvidence, ...]
    retrieved_at: datetime
    source_hash: str


@dataclass(frozen=True)
class CatalystAuthorityResult:
    authority: CatalystAuthority
    plan_hash: str
    policy_hash: str
    criteria_hash: str
    research_source_hash: str
    classification_hash: str
    authority_hash: str


class CatalystResearchPort(Protocol):
    async def research(
        self, plan: CatalystEvidencePlan, trusted_at: datetime
    ) -> CatalystResearchBundle: ...


class CatalystClassifierPort(Protocol):
    def classify(
        self,
        plan: CatalystEvidencePlan,
        clusters: tuple[SourceCluster, ...],
    ) -> tuple[EvidenceClassification, ...]: ...


class BoundedOpportunityCatalystAuthority:
    def __init__(
        self,
        research: CatalystResearchPort,
        classifier: CatalystClassifierPort,
    ) -> None:
        self._research = research
        self._classifier = classifier

    async def produce(
        self,
        *,
        plan: CatalystEvidencePlan,
        policy: OpportunityPolicy,
        trusted_at: datetime,
    ) -> CatalystAuthorityResult:
        _validate_plan(plan, policy, trusted_at)
        try:
            bundle = await self._research.research(plan, trusted_at)
        except Exception as error:
            raise CatalystAuthorityError("CATALYST_RESEARCH_UNAVAILABLE") from error
        _validate_bundle(bundle, plan, policy, trusted_at)
        if not bundle.clusters:
            return _result(
                plan=plan,
                policy=policy,
                bundle=bundle,
                classifications=(),
                quality=CatalystQuality.MISSING,
                score=0,
            )
        try:
            classifications = self._classifier.classify(plan, bundle.clusters)
        except Exception as error:
            raise CatalystAuthorityError("CATALYST_CLASSIFICATION_UNAVAILABLE") from error
        _validate_classifications(classifications, bundle, plan)
        quality, score = _assess_classifications(
            tuple(item for item in classifications if item.event_code in plan.allowed_event_codes)
        )
        return _result(
            plan=plan,
            policy=policy,
            bundle=bundle,
            classifications=classifications,
            quality=quality,
            score=score,
        )


def catalyst_source_evidence_digest(value: CatalystSourceEvidence) -> str:
    return _hash(
        "alphadecay.opportunity.catalyst-source.v1",
        replace(value, source_hash=""),
    )


def catalyst_evidence_plan_digest(value: CatalystEvidencePlan) -> str:
    return _hash("alphadecay.opportunity.catalyst-plan.v1", value)


def catalyst_research_bundle_digest(value: CatalystResearchBundle) -> str:
    return _hash(
        "alphadecay.opportunity.catalyst-research.v1",
        replace(value, source_hash=""),
    )


def _validate_plan(
    plan: CatalystEvidencePlan,
    policy: OpportunityPolicy,
    trusted_at: datetime,
) -> None:
    if (
        type(plan) is not CatalystEvidencePlan
        or type(policy) is not OpportunityPolicy
        or not _matches(_KEY, plan.opportunity_key)
        or not _matches(_UNDERLYING, plan.underlying)
        or not _matches(_HASH, plan.plan_hash)
        or not _matches(_HASH, plan.policy_hash)
        or not _matches(_CODE, plan.thesis_code)
        or plan.opportunity_key != policy.opportunity_key
        or plan.underlying != policy.underlying
        or plan.policy_hash != opportunity_policy_hash(policy)
        or type(plan.allowed_event_codes) is not tuple
        or not 1 <= len(plan.allowed_event_codes) <= 12
        or len(set(plan.allowed_event_codes)) != len(plan.allowed_event_codes)
        or any(not _matches(_CODE, code) for code in plan.allowed_event_codes)
        or type(plan.invalidation_codes) is not tuple
        or not 1 <= len(plan.invalidation_codes) <= 16
        or len(set(plan.invalidation_codes)) != len(plan.invalidation_codes)
        or any(not _matches(_CODE, code) for code in plan.invalidation_codes)
        or not all(
            _is_utc(value)
            for value in (
                plan.evidence_window_start,
                plan.evidence_window_end,
                plan.frozen_at,
                trusted_at,
            )
        )
        or not plan.frozen_at
        <= plan.evidence_window_start
        <= plan.evidence_window_end
        <= policy.selected_decision_boundary
        or not policy.selected_decision_boundary
        <= trusted_at
        <= policy.selected_decision_boundary + policy.maximum_decision_delay
    ):
        raise CatalystAuthorityError("CATALYST_PLAN_INVALID")


def _validate_bundle(
    bundle: CatalystResearchBundle,
    plan: CatalystEvidencePlan,
    policy: OpportunityPolicy,
    trusted_at: datetime,
) -> None:
    if (
        type(bundle) is not CatalystResearchBundle
        or bundle.opportunity_key != plan.opportunity_key
        or bundle.plan_hash != plan.plan_hash
        or bundle.policy_hash != plan.policy_hash
        or bundle.criteria_hash != catalyst_evidence_plan_digest(plan)
        or not _matches(_HASH, bundle.criteria_hash)
        or type(bundle.clusters) is not tuple
        or type(bundle.sources) is not tuple
        or len(bundle.clusters) > 12
        or len(bundle.sources) > 48
        or not _is_utc(bundle.retrieved_at)
        or bundle.retrieved_at - trusted_at > _RETRIEVAL_SKEW_TOLERANCE
        or trusted_at - bundle.retrieved_at > policy.maximum_catalyst_age
        or not _matches(_HASH, bundle.source_hash)
        or bool(bundle.clusters) != bool(bundle.sources)
    ):
        raise CatalystAuthorityError("CATALYST_RESEARCH_INVALID")
    source_ids: set[str] = set()
    result_hashes: set[str] = set()
    sources_by_id: dict[str, CatalystSourceEvidence] = {}
    for source in bundle.sources:
        if (
            type(source) is not CatalystSourceEvidence
            or not _matches(_KEY, source.source_id)
            or not _matches(_CODE, source.source_kind)
            or source.source_id in source_ids
            or any(
                not _matches(_HASH, value)
                for value in (source.request_hash, source.result_hash, source.source_hash)
            )
            or source.result_hash in result_hashes
            or not _is_utc(source.observed_at)
            or not _is_utc(source.retrieved_at)
            or not plan.evidence_window_start <= source.observed_at <= plan.evidence_window_end
            or not source.observed_at <= source.retrieved_at <= bundle.retrieved_at
            or trusted_at - source.observed_at > policy.maximum_catalyst_age
            or source.source_hash != catalyst_source_evidence_digest(source)
        ):
            raise CatalystAuthorityError("CATALYST_RESEARCH_INVALID")
        source_ids.add(source.source_id)
        result_hashes.add(source.result_hash)
        sources_by_id[source.source_id] = source
    if bundle.sources and bundle.retrieved_at != max(
        source.retrieved_at for source in bundle.sources
    ):
        raise CatalystAuthorityError("CATALYST_RESEARCH_INVALID")
    cluster_ids: set[str] = set()
    clustered_sources: set[str] = set()
    for cluster in bundle.clusters:
        if (
            type(cluster) is not SourceCluster
            or not _matches(_KEY, cluster.cluster_id)
            or cluster.cluster_id in cluster_ids
            or not cluster.source_ids
            or len(set(cluster.source_ids)) != len(cluster.source_ids)
            or any(not _matches(_KEY, source_id) for source_id in cluster.source_ids)
            or any(source_id not in sources_by_id for source_id in cluster.source_ids)
            or any(source_id in clustered_sources for source_id in cluster.source_ids)
            or not _is_utc(cluster.observed_at)
            or cluster.observed_at
            != max(sources_by_id[source_id].observed_at for source_id in cluster.source_ids)
            or not cluster.headline
            or len(cluster.headline) > 512
            or any(ord(character) < 32 and character != "\t" for character in cluster.headline)
            or type(cluster.source_tier) is not EvidenceTier
            or (
                cluster.independent_reporting_group is not None
                and not _matches(_KEY, cluster.independent_reporting_group)
            )
        ):
            raise CatalystAuthorityError("CATALYST_RESEARCH_INVALID")
        cluster_ids.add(cluster.cluster_id)
        clustered_sources.update(cluster.source_ids)
    if clustered_sources != source_ids:
        raise CatalystAuthorityError("CATALYST_RESEARCH_INVALID")
    if bundle.source_hash != catalyst_research_bundle_digest(bundle):
        raise CatalystAuthorityError("CATALYST_RESEARCH_INVALID")


def _result(
    *,
    plan: CatalystEvidencePlan,
    policy: OpportunityPolicy,
    bundle: CatalystResearchBundle,
    classifications: tuple[EvidenceClassification, ...],
    quality: CatalystQuality,
    score: int,
) -> CatalystAuthorityResult:
    observed_at = _authority_observed_at(bundle)
    classification_hash = _hash(
        "alphadecay.opportunity.catalyst-classification.v1", classifications
    )
    authority_material = {
        "plan_hash": plan.plan_hash,
        "policy_hash": opportunity_policy_hash(policy),
        "criteria_hash": catalyst_evidence_plan_digest(plan),
        "research_source_hash": bundle.source_hash,
        "classification_hash": classification_hash,
        "quality": quality,
        "score": score,
        "observed_at": observed_at,
    }
    authority_hash = _hash("alphadecay.opportunity.catalyst-authority.v1", authority_material)
    authority = CatalystAuthority(
        opportunity_key=plan.opportunity_key,
        quality=quality,
        score=score,
        observed_at=observed_at,
        source_hash=authority_hash,
    )
    return CatalystAuthorityResult(
        authority=authority,
        plan_hash=plan.plan_hash,
        policy_hash=plan.policy_hash,
        criteria_hash=catalyst_evidence_plan_digest(plan),
        research_source_hash=bundle.source_hash,
        classification_hash=classification_hash,
        authority_hash=authority_hash,
    )


def _validate_classifications(
    classifications: tuple[EvidenceClassification, ...],
    bundle: CatalystResearchBundle,
    plan: CatalystEvidencePlan,
) -> None:
    if type(classifications) is not tuple or len(classifications) != len(bundle.clusters):
        raise CatalystAuthorityError("CATALYST_CLASSIFICATION_INVALID")
    clusters = {cluster.cluster_id: cluster for cluster in bundle.clusters}
    seen: set[str] = set()
    for classification in classifications:
        if type(classification) is not EvidenceClassification:
            raise CatalystAuthorityError("CATALYST_CLASSIFICATION_INVALID")
        cluster = clusters.get(classification.cluster_id)
        invalidation = classification.invalidation_condition_id
        if (
            cluster is None
            or classification.cluster_id in seen
            or classification.source_ids != cluster.source_ids
            or type(classification.relation) is not EvidenceRelation
            or classification.source_tier is not cluster.source_tier
            or classification.independent_reporting_group != cluster.independent_reporting_group
            or not _matches(_CODE, classification.event_code)
            or type(classification.materiality) is not int
            or type(classification.relevance) is not Decimal
            or type(classification.confidence) is not Decimal
            or not classification.relevance.is_finite()
            or not classification.confidence.is_finite()
            or not 1 <= classification.materiality <= 3
            or not Decimal(0) <= classification.relevance <= Decimal(1)
            or not Decimal(0) <= classification.confidence <= Decimal(1)
            or type(classification.invalidates) is not bool
            or classification.invalidates is not (invalidation is not None)
            or (invalidation is not None and invalidation not in plan.invalidation_codes)
            or (
                invalidation is not None
                and classification.relation is not EvidenceRelation.CONTRADICTS
            )
        ):
            raise CatalystAuthorityError("CATALYST_CLASSIFICATION_INVALID")
        seen.add(classification.cluster_id)
    if seen != set(clusters):
        raise CatalystAuthorityError("CATALYST_CLASSIFICATION_INVALID")


def _assess_classifications(
    classifications: tuple[EvidenceClassification, ...],
) -> tuple[CatalystQuality, int]:
    invalidations = tuple(item for item in classifications if item.invalidates)
    primary_invalidation = any(item.source_tier is EvidenceTier.PRIMARY for item in invalidations)
    reporting_groups = {
        item.independent_reporting_group
        for item in invalidations
        if item.source_tier is EvidenceTier.ORIGINAL_REPORTING
        and item.independent_reporting_group is not None
    }
    if primary_invalidation or len(reporting_groups) >= 2:
        return CatalystQuality.AUTHORITATIVE_CONTRADICTION, 0
    qualifying = tuple(
        item
        for item in classifications
        if item.relevance >= Decimal("0.60") and item.confidence >= Decimal("0.60")
    )
    supports = tuple(item for item in qualifying if item.relation is EvidenceRelation.SUPPORTS)
    contradictions = tuple(
        item for item in qualifying if item.relation is EvidenceRelation.CONTRADICTS
    )
    if contradictions or not supports:
        return CatalystQuality.UNRESOLVED_RISK, 0
    score = max(
        int(
            (
                Decimal(item.materiality)
                / Decimal(3)
                * item.relevance
                * item.confidence
                * _SOURCE_WEIGHTS[item.source_tier]
                * Decimal(100)
            ).to_integral_value(rounding=ROUND_DOWN)
        )
        for item in supports
    )
    return CatalystQuality.CLEAR, score


def _is_utc(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() == timedelta(0)
    )


def _authority_observed_at(bundle: CatalystResearchBundle) -> datetime:
    if not bundle.clusters:
        return bundle.retrieved_at
    return max(cluster.observed_at for cluster in bundle.clusters)


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def _hash(domain: str, value: object) -> str:
    payload = json.dumps(
        {"domain": domain, "value": _canonical(value)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _canonical(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        fixed = format(value, "f")
        if "." in fixed:
            fixed = fixed.rstrip("0").rstrip(".")
        return "0" if fixed in {"-0", "+0"} else fixed
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if hasattr(value, "model_dump"):
        return _canonical(value.model_dump(mode="python"))
    if hasattr(value, "__dataclass_fields__"):
        return _canonical(asdict(value))
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_canonical(item) for item in value]
    return value

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Protocol

from backend.app.contracts.v1 import EvidenceClassification, SourceCluster
from backend.app.evidence.classifier import EvidenceClassificationContext
from backend.app.lifecycle.research import (
    LifecycleResearchError,
    LifecycleResearchSource,
    normalize_mcp_news,
    validate_mcp_research_result,
)
from backend.app.services.opportunity_catalyst import (
    CatalystClassifierPort,
    CatalystEvidencePlan,
    CatalystResearchBundle,
    CatalystResearchPort,
    CatalystSourceEvidence,
    catalyst_evidence_plan_digest,
    catalyst_research_bundle_digest,
    catalyst_source_evidence_digest,
)

_HASH = re.compile(r"^[0-9a-f]{64}$")
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_MAX_NEWS = 12


class OpportunityCatalystAdapterError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RetainedMCPResearchPort(Protocol):
    async def call(self, tool_name: str, arguments: Mapping[str, object]) -> object: ...


class StructuredEvidenceClassifierPort(Protocol):
    def classify_context(
        self,
        context: EvidenceClassificationContext,
        clusters: tuple[SourceCluster, ...],
    ) -> tuple[EvidenceClassification, ...]: ...


@dataclass(frozen=True)
class CatalystClassifierBinding:
    plan_hash: str
    criteria_hash: str
    context: EvidenceClassificationContext
    source_hash: str


class RetainedMCPNewsCatalystResearch(CatalystResearchPort):
    """Read one bounded, plan-selected news window from an already retained MCP session."""

    def __init__(self, runtime: RetainedMCPResearchPort) -> None:
        self._runtime = runtime

    async def research(
        self,
        plan: CatalystEvidencePlan,
        trusted_at: datetime,
    ) -> CatalystResearchBundle:
        arguments = {
            "symbols": [plan.underlying],
            "start": _timestamp(plan.evidence_window_start),
            "end": _timestamp(plan.evidence_window_end),
            "limit": _MAX_NEWS,
        }
        try:
            raw = await self._runtime.call("get_news", arguments)
            result = validate_mcp_research_result(raw, "get_news", arguments)
            if result.audit.completed_at - trusted_at > timedelta(seconds=30):
                raise OpportunityCatalystAdapterError("RESEARCH_AFTER_TRUSTED_TIME")
            clusters, records = normalize_mcp_news(
                result,
                plan.evidence_window_start,
                plan.evidence_window_end,
            )
        except LifecycleResearchError as error:
            raise OpportunityCatalystAdapterError(error.code) from error

        if len(records) > _MAX_NEWS or any(
            _KEY.fullmatch(record.logical_source_id) is None for record in records
        ):
            raise OpportunityCatalystAdapterError("RESULT_LIMIT_OR_IDENTITY_INVALID")
        sources = tuple(_catalyst_source(record) for record in records)
        bundle = CatalystResearchBundle(
            opportunity_key=plan.opportunity_key,
            plan_hash=plan.plan_hash,
            policy_hash=plan.policy_hash,
            criteria_hash=catalyst_evidence_plan_digest(plan),
            clusters=clusters,
            sources=sources,
            retrieved_at=result.audit.completed_at,
            source_hash="",
        )
        return replace(bundle, source_hash=catalyst_research_bundle_digest(bundle))


class BoundOpportunityCatalystClassifier(CatalystClassifierPort):
    """Adapt a structured evidence classifier to one exact frozen catalyst plan."""

    def __init__(
        self,
        target: StructuredEvidenceClassifierPort,
        binding: CatalystClassifierBinding,
    ) -> None:
        self._target = target
        self._binding = binding

    def classify(
        self,
        plan: CatalystEvidencePlan,
        clusters: tuple[SourceCluster, ...],
    ) -> tuple[EvidenceClassification, ...]:
        _validate_classifier_binding(self._binding, plan)
        return self._target.classify_context(self._binding.context, clusters)


def bind_catalyst_classification_context(
    plan: CatalystEvidencePlan,
) -> CatalystClassifierBinding:
    criteria_hash = catalyst_evidence_plan_digest(plan)
    context = _classification_context(plan, criteria_hash)
    binding = CatalystClassifierBinding(
        plan_hash=plan.plan_hash,
        criteria_hash=criteria_hash,
        context=context,
        source_hash="",
    )
    return replace(binding, source_hash=catalyst_classifier_binding_digest(binding))


def catalyst_classifier_binding_digest(binding: CatalystClassifierBinding) -> str:
    return _hash(
        "alphadecay.opportunity.catalyst-classifier-binding.v1",
        replace(binding, source_hash=""),
    )


def _validate_classifier_binding(
    binding: CatalystClassifierBinding,
    plan: CatalystEvidencePlan,
) -> None:
    if type(binding) is not CatalystClassifierBinding or type(plan) is not CatalystEvidencePlan:
        raise OpportunityCatalystAdapterError("CLASSIFIER_BINDING_INVALID")
    criteria_hash = catalyst_evidence_plan_digest(plan)
    expected_context = _classification_context(plan, criteria_hash)
    if (
        binding.plan_hash != plan.plan_hash
        or binding.criteria_hash != criteria_hash
        or not _HASH.fullmatch(binding.plan_hash)
        or not _HASH.fullmatch(binding.criteria_hash)
        or not _HASH.fullmatch(binding.source_hash)
        or binding.source_hash != catalyst_classifier_binding_digest(binding)
        or binding.context != expected_context
    ):
        raise OpportunityCatalystAdapterError("CLASSIFIER_BINDING_INVALID")


def _classification_context(
    plan: CatalystEvidencePlan,
    criteria_hash: str,
) -> EvidenceClassificationContext:
    context_hash = _hash(
        "alphadecay.opportunity.catalyst-classification-context.v1",
        {
            "plan_hash": plan.plan_hash,
            "criteria_hash": criteria_hash,
            "policy_hash": plan.policy_hash,
        },
    )
    return EvidenceClassificationContext(
        context_hash=context_hash,
        version=1,
        underlying=plan.underlying,
        thesis_code=plan.thesis_code,
        invalidation_condition_ids=plan.invalidation_codes,
    )


def _catalyst_source(record: LifecycleResearchSource) -> CatalystSourceEvidence:
    source = CatalystSourceEvidence(
        source_id=record.logical_source_id,
        source_kind=record.source_kind,
        request_hash=record.request_hash,
        result_hash=record.result_hash,
        observed_at=record.observed_at,
        retrieved_at=record.retrieved_at,
        source_hash="",
    )
    return replace(source, source_hash=catalyst_source_evidence_digest(source))


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


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

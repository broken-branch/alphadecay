from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from backend.app.alpaca.mcp import MCPResearchAudit, MCPResearchResult
from backend.app.alpaca.opportunity_catalyst import (
    BoundOpportunityCatalystClassifier,
    CatalystClassifierBinding,
    OpportunityCatalystAdapterError,
    RetainedMCPNewsCatalystResearch,
    bind_catalyst_classification_context,
    catalyst_classifier_binding_digest,
)
from backend.app.contracts.v1 import (
    EvidenceClassification,
    EvidenceRelation,
    EvidenceTier,
    SourceCluster,
)
from backend.app.evidence.classifier import EvidenceClassificationContext
from backend.app.services.opportunity_catalyst import (
    CatalystEvidencePlan,
    catalyst_evidence_plan_digest,
    catalyst_research_bundle_digest,
    catalyst_source_evidence_digest,
)

START = datetime(2026, 8, 31, 13, 30, tzinfo=UTC)
END = datetime(2026, 8, 31, 15, 0, tzinfo=UTC)
TRUSTED = END + timedelta(minutes=1)


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def plan() -> CatalystEvidencePlan:
    return CatalystEvidencePlan(
        opportunity_key="ACME_EVENT",
        underlying="ACME",
        plan_hash="a" * 64,
        policy_hash="b" * 64,
        thesis_code="POST_EVENT_CONTINUATION",
        allowed_event_codes=("RESULTS", "GUIDANCE"),
        invalidation_codes=("GUIDANCE_REVERSED",),
        evidence_window_start=START,
        evidence_window_end=END,
        frozen_at=START - timedelta(days=1),
    )


def payload() -> dict[str, object]:
    return {
        "news": [
            {
                "id": "issuer-results",
                "headline": "Issuer reports results and maintains guidance",
                "published_at": "2026-08-31T14:00:00Z",
                "source_tier": "PRIMARY",
                "independent_reporting_group": None,
            }
        ]
    }


def result(
    data: object | None = None,
    *,
    completed_at: datetime = TRUSTED,
    argument_hash: str | None = None,
) -> MCPResearchResult:
    arguments = {
        "symbols": ["ACME"],
        "start": "2026-08-31T13:30:00Z",
        "end": "2026-08-31T15:00:00Z",
        "limit": 12,
    }
    body = payload() if data is None else data
    return MCPResearchResult(
        tool_name="get_news",
        data=body,
        audit=MCPResearchAudit(
            tool_name="get_news",
            argument_hash=argument_hash or hashlib.sha256(canonical(arguments)).hexdigest(),
            started_at=completed_at - timedelta(seconds=1),
            completed_at=completed_at,
            result_summary_hash=hashlib.sha256(canonical(body)).hexdigest(),
            quality="COMPLETE",
        ),
    )


class MCP:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.entered = 0
        self.closed = 0

    async def call(self, tool_name: str, arguments: dict[str, object]) -> object:
        self.calls.append((tool_name, arguments))
        return self.response

    async def __aenter__(self) -> MCP:
        self.entered += 1
        return self

    async def aclose(self) -> None:
        self.closed += 1


def test_research_uses_one_exact_plan_bound_call_without_owning_runtime() -> None:
    mcp = MCP(result())
    bundle = asyncio.run(RetainedMCPNewsCatalystResearch(mcp).research(plan(), TRUSTED))

    assert mcp.calls == [
        (
            "get_news",
            {
                "symbols": ["ACME"],
                "start": "2026-08-31T13:30:00Z",
                "end": "2026-08-31T15:00:00Z",
                "limit": 12,
            },
        )
    ]
    assert mcp.entered == 0
    assert mcp.closed == 0
    assert bundle.criteria_hash == catalyst_evidence_plan_digest(plan())
    assert bundle.source_hash == catalyst_research_bundle_digest(bundle)
    assert len(bundle.clusters) == len(bundle.sources) == 1
    assert bundle.clusters[0].source_tier is EvidenceTier.PRIMARY
    assert bundle.sources[0].source_kind == "MCP_NEWS"
    assert bundle.sources[0].source_hash == catalyst_source_evidence_digest(bundle.sources[0])
    assert bundle.sources[0].request_hash == result().audit.argument_hash
    assert bundle.sources[0].result_hash != result().audit.result_summary_hash


def test_empty_research_is_complete_and_bound_without_fabricated_sources() -> None:
    mcp = MCP(result({"news": []}))
    bundle = asyncio.run(RetainedMCPNewsCatalystResearch(mcp).research(plan(), TRUSTED))

    assert bundle.clusters == ()
    assert bundle.sources == ()
    assert bundle.retrieved_at == TRUSTED
    assert bundle.source_hash == catalyst_research_bundle_digest(bundle)


def test_research_rejects_more_results_than_the_requested_limit() -> None:
    news = [
        {
            "id": f"item-{index:02d}",
            "headline": f"Bounded item {index}",
            "published_at": "2026-08-31T14:00:00Z",
            "source_tier": "SECONDARY",
            "independent_reporting_group": f"wire-{index:02d}",
        }
        for index in range(13)
    ]

    with pytest.raises(
        OpportunityCatalystAdapterError,
        match="RESULT_LIMIT_EXCEEDED",
    ):
        asyncio.run(
            RetainedMCPNewsCatalystResearch(MCP(result({"news": news}))).research(plan(), TRUSTED)
        )


@pytest.mark.parametrize(
    "response,trusted,code",
    [
        (result(argument_hash="c" * 64), TRUSTED, "AUDIT_INVALID"),
        (
            result(
                {
                    "news": [
                        {
                            **payload()["news"][0],
                            "source_tier": "UNKNOWN",
                        }
                    ]
                }
            ),
            TRUSTED,
            "RESULT_SCHEMA_INVALID",
        ),
        (
            result(completed_at=TRUSTED + timedelta(seconds=31)),
            TRUSTED,
            "RESEARCH_AFTER_TRUSTED_TIME",
        ),
    ],
)
def test_research_fails_closed_on_unbound_audit_tier_or_time(
    response: MCPResearchResult,
    trusted: datetime,
    code: str,
) -> None:
    with pytest.raises(OpportunityCatalystAdapterError, match=code):
        asyncio.run(RetainedMCPNewsCatalystResearch(MCP(response)).research(plan(), trusted))


def binding(**changes: object) -> CatalystClassifierBinding:
    value = bind_catalyst_classification_context(plan())
    value = replace(value, **changes)
    return replace(value, source_hash=catalyst_classifier_binding_digest(value))


class Classifier:
    def __init__(self) -> None:
        self.calls: list[tuple[EvidenceClassificationContext, tuple[SourceCluster, ...]]] = []

    def classify_context(
        self,
        context: EvidenceClassificationContext,
        clusters: tuple[SourceCluster, ...],
    ) -> tuple[EvidenceClassification, ...]:
        self.calls.append((context, clusters))
        cluster = clusters[0]
        return (
            EvidenceClassification(
                cluster_id=cluster.cluster_id,
                source_ids=cluster.source_ids,
                event_code="RESULTS",
                relation=EvidenceRelation.SUPPORTS,
                materiality=3,
                relevance=Decimal("0.9"),
                confidence=Decimal("0.8"),
                source_tier=cluster.source_tier,
            ),
        )


def cluster() -> SourceCluster:
    return SourceCluster(
        cluster_id="news:issuer-results",
        source_ids=("issuer-results",),
        headline="Issuer reports results and maintains guidance",
        observed_at=datetime(2026, 8, 31, 14, tzinfo=UTC),
        source_tier=EvidenceTier.PRIMARY,
    )


def test_classifier_uses_exact_pre_entry_context_without_fabricating_thesis() -> None:
    target = Classifier()
    values = (cluster(),)
    expected = bind_catalyst_classification_context(plan())

    classified = BoundOpportunityCatalystClassifier(target, expected).classify(plan(), values)

    assert classified[0].event_code == "RESULTS"
    assert target.calls == [(expected.context, values)]
    assert expected.context.context_hash not in {plan().plan_hash, expected.criteria_hash}
    assert expected.context.underlying == plan().underlying
    assert expected.context.thesis_code == plan().thesis_code
    assert expected.context.invalidation_condition_ids == plan().invalidation_codes


@pytest.mark.parametrize(
    "invalid_binding",
    [
        binding(plan_hash="e" * 64),
        binding(criteria_hash="e" * 64),
        binding(
            context=replace(
                bind_catalyst_classification_context(plan()).context,
                underlying="OTHER",
            )
        ),
    ],
)
def test_classifier_rejects_mismatched_binding_before_model_call(
    invalid_binding: CatalystClassifierBinding,
) -> None:
    target = Classifier()

    with pytest.raises(OpportunityCatalystAdapterError, match="CLASSIFIER_BINDING_INVALID"):
        BoundOpportunityCatalystClassifier(target, invalid_binding).classify(plan(), (cluster(),))

    assert target.calls == []

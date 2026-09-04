from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from backend.app.alpaca.mcp import MCPResearchAudit, MCPResearchResult
from backend.app.contracts.v1 import AccountRole, EvidenceTier
from backend.app.lifecycle.research import (
    BoundedLifecycleResearch,
    LifecycleResearchError,
    LifecycleResearchSource,
    normalize_mcp_news,
)

NOW = datetime(2026, 8, 29, 16, tzinfo=UTC)
START = datetime(2026, 8, 1, 14, 30, tzinfo=UTC)


@dataclass
class FakeMCP:
    results: list[object]
    entered: int = 0

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def __aenter__(self) -> FakeMCP:
        self.entered += 1
        return self

    async def call(self, tool_name: str, arguments: dict[str, object]) -> object:
        self.calls.append((tool_name, arguments))
        selected = self.results.pop(0)
        if not isinstance(selected, MCPResearchResult):
            return selected
        encoded_arguments = json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()
        encoded_result = json.dumps(selected.data, sort_keys=True, separators=(",", ":")).encode()
        return replace(
            selected,
            audit=replace(
                selected.audit,
                argument_hash=hashlib.sha256(encoded_arguments).hexdigest(),
                result_summary_hash=hashlib.sha256(encoded_result).hexdigest(),
            ),
        )


@dataclass
class FakeEvidence:
    batches: list[tuple[object, tuple[LifecycleResearchSource, ...], datetime]]

    def persist_research_sources(
        self,
        context: object,
        records: tuple[LifecycleResearchSource, ...],
        trusted_at: datetime,
    ) -> None:
        self.batches.append((context, records, trusted_at))


def result(tool: str, data: object, index: int) -> MCPResearchResult:
    return MCPResearchResult(
        tool_name=tool,
        data=data,
        audit=MCPResearchAudit(
            tool_name=tool,
            argument_hash=f"{index:064x}",
            started_at=NOW,
            completed_at=NOW,
            result_summary_hash=f"{index + 10:064x}",
            quality="COMPLETE",
        ),
    )


def context(*, role: AccountRole = AccountRole.DEVELOPMENT, short_call: bool = False) -> object:
    right = "C" if short_call else "P"
    quantity = -1 if short_call else 1
    return SimpleNamespace(
        account_role=role,
        lifecycle_origin_at=START,
        target_at=datetime(2026, 9, 19, tzinfo=UTC),
        thesis=SimpleNamespace(thesis=SimpleNamespace(underlying="PANW")),
        expected_positions=(
            SimpleNamespace(symbol=f"PANW260918{right}00300000", signed_quantity=quantity),
            SimpleNamespace(symbol="PANW260918P00290000", signed_quantity=1),
        ),
    )


def news() -> dict[str, object]:
    return {
        "news": [
            {
                "id": "news-1",
                "headline": "PANW files its quarterly report.",
                "published_at": "2026-08-20T15:00:00Z",
                "source_tier": "PRIMARY",
                "independent_reporting_group": None,
            }
        ]
    }


def actions(*, include_dividend: bool = False) -> dict[str, object]:
    items: list[dict[str, object]] = []
    if include_dividend:
        items.append(
            {
                "id": "action-1",
                "type": "CASH_DIVIDEND",
                "announced_at": "2026-08-21T12:00:00Z",
                "ex_date": "2026-09-04",
            }
        )
    return {"corporate_actions": items}


def detail() -> dict[str, object]:
    return {
        "corporate_action": {
            "id": "action-1",
            "type": "CASH_DIVIDEND",
            "headline": "PANW declares a cash dividend.",
            "announced_at": "2026-08-21T12:00:00Z",
            "ex_date": "2026-09-04",
            "source_tier": "PRIMARY",
            "independent_reporting_group": None,
        }
    }


def run(adapter: BoundedLifecycleResearch, value: object = None):
    return asyncio.run(adapter.research(value or context(), NOW))


def test_adjusted_contract_stops_before_mcp_session_or_evidence_write() -> None:
    value = context()
    value.expected_positions = (
        SimpleNamespace(symbol="PANW1260918P00300000", signed_quantity=1),
        SimpleNamespace(symbol="PANW1260918P00290000", signed_quantity=-1),
    )
    mcp = FakeMCP([])
    evidence = FakeEvidence([])

    with pytest.raises(LifecycleResearchError) as raised:
        run(BoundedLifecycleResearch(mcp, evidence), value)

    assert raised.value.code == "NON_STANDARD_CONTRACT_UNSUPPORTED"
    assert mcp.entered == 0
    assert mcp.calls == []
    assert evidence.batches == []


def test_research_opens_one_session_and_returns_canonical_sanitized_clusters() -> None:
    mcp = FakeMCP(
        [
            result("get_news", news(), 1),
            result("get_corporate_actions", actions(), 2),
            result("get_news", {"news": []}, 3),
            result("get_corporate_actions", actions(), 4),
        ]
    )
    evidence = FakeEvidence([])
    adapter = BoundedLifecycleResearch(mcp, evidence)

    first = run(adapter)
    second = run(adapter)

    assert first[0].headline == "PANW files its quarterly report."
    assert first[0].source_ids == ("news-1",)
    assert first[0].source_tier is EvidenceTier.PRIMARY
    assert second == ()
    assert mcp.entered == 1
    assert [call[0] for call in mcp.calls] == [
        "get_news",
        "get_corporate_actions",
        "get_news",
        "get_corporate_actions",
    ]
    assert len(evidence.batches) == 2
    assert evidence.batches[0][0].account_role is AccountRole.DEVELOPMENT
    first_records = evidence.batches[0][1]
    assert len(first_records) == 1
    assert first_records[0].logical_source_id == "news-1"
    assert first_records[0].source_kind == "MCP_NEWS"
    assert first_records[0].request_hash != f"{1:064x}"
    assert first_records[0].result_hash != f"{11:064x}"
    assert first_records[0].normalized_payload == {
        "headline": "PANW files its quarterly report.",
        "id": "news-1",
        "independent_reporting_group": None,
        "published_at": "2026-08-20T15:00:00Z",
        "source_tier": "PRIMARY",
    }
    assert first_records[0].observed_at == datetime(2026, 8, 20, 15, tzinfo=UTC)
    assert first_records[0].retrieved_at == NOW
    assert len(first_records[0].source_hash) == 64


def test_submission_rejects_before_opening_the_session() -> None:
    mcp = FakeMCP([])
    adapter = BoundedLifecycleResearch(mcp, FakeEvidence([]))

    with pytest.raises(LifecycleResearchError, match="DEVELOPMENT_ONLY"):
        run(adapter, context(role=AccountRole.SUBMISSION))

    assert mcp.entered == 0
    assert mcp.calls == []


def test_short_call_requires_one_detailed_ex_dividend_record() -> None:
    mcp = FakeMCP(
        [
            result("get_news", news(), 1),
            result("get_corporate_actions", actions(include_dividend=True), 2),
            result("get_corporate_action_announcement", detail(), 3),
        ]
    )
    evidence = FakeEvidence([])
    adapter = BoundedLifecycleResearch(mcp, evidence)

    clusters = run(adapter, context(short_call=True))

    assert [item.source_ids for item in clusters] == [("news-1",), ("action-1",)]
    assert mcp.calls[-1] == (
        "get_corporate_action_announcement",
        {"announcement_id": "action-1"},
    )
    records = evidence.batches[0][1]
    assert [item.logical_source_id for item in records] == ["news-1", "action-1"]
    action = records[1]
    assert action.source_kind == "MCP_CORPORATE_ACTION"
    assert action.normalized_payload["headline"] == "PANW declares a cash dividend."


def test_failed_research_does_not_persist_unbindable_call_records() -> None:
    evidence = FakeEvidence([])
    adapter = BoundedLifecycleResearch(
        FakeMCP(
            [
                result("get_news", news(), 1),
                result("get_corporate_actions", actions(include_dividend=True), 2),
                result(
                    "get_corporate_action_announcement",
                    {"corporate_action": {**detail()["corporate_action"], "id": "wrong"}},
                    3,
                ),
            ]
        ),
        evidence,
    )

    with pytest.raises(LifecycleResearchError, match="EX_DIVIDEND_EVIDENCE_AMBIGUOUS"):
        run(adapter, context(short_call=True))

    assert evidence.batches == []


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"corporate_actions": []}, "EX_DIVIDEND_EVIDENCE_MISSING"),
        (
            {
                "corporate_actions": [
                    {
                        "id": "a",
                        "type": "CASH_DIVIDEND",
                        "announced_at": "2026-08-21T12:00:00Z",
                        "ex_date": "2026-09-04",
                    },
                    {
                        "id": "b",
                        "type": "CASH_DIVIDEND",
                        "announced_at": "2026-08-21T12:00:00Z",
                        "ex_date": "2026-09-05",
                    },
                ]
            },
            "EX_DIVIDEND_EVIDENCE_AMBIGUOUS",
        ),
    ],
)
def test_short_call_fails_closed_on_missing_or_ambiguous_dividend(
    payload: object, code: str
) -> None:
    adapter = BoundedLifecycleResearch(
        FakeMCP([result("get_news", news(), 1), result("get_corporate_actions", payload, 2)]),
        FakeEvidence([]),
    )

    with pytest.raises(LifecycleResearchError, match=code):
        run(adapter, context(short_call=True))


def test_unknown_schema_and_noncanonical_order_fail_closed() -> None:
    malformed = {"news": [news()["news"][0], {**news()["news"][0], "id": "news-0"}]}
    adapter = BoundedLifecycleResearch(
        FakeMCP([result("get_news", malformed, 1), result("get_corporate_actions", actions(), 2)]),
        FakeEvidence([]),
    )

    with pytest.raises(LifecycleResearchError, match="RESULT_ORDER_INVALID"):
        run(adapter)


def test_result_limits_and_sensitive_fields_fail_closed() -> None:
    oversized = {"news": [{**news()["news"][0], "id": f"news-{index:02d}"} for index in range(13)]}
    adapter = BoundedLifecycleResearch(
        FakeMCP([result("get_news", oversized, 1), result("get_corporate_actions", actions(), 2)]),
        FakeEvidence([]),
    )
    with pytest.raises(LifecycleResearchError, match="RESULT_LIMIT_EXCEEDED"):
        run(adapter)

    sensitive = {"news": [{**news()["news"][0], "account_id": "forbidden"}]}
    adapter = BoundedLifecycleResearch(
        FakeMCP([result("get_news", sensitive, 1), result("get_corporate_actions", actions(), 2)]),
        FakeEvidence([]),
    )
    with pytest.raises(LifecycleResearchError, match="RESULT_SCHEMA_INVALID"):
        run(adapter)


def test_detail_calls_are_bounded_and_only_use_returned_action_ids() -> None:
    summaries = [
        {
            "id": f"action-{index}",
            "type": "SPLIT",
            "announced_at": f"2026-08-2{index}T12:00:00Z",
            "ex_date": "2026-09-04",
        }
        for index in range(1, 6)
    ]
    mcp = FakeMCP(
        [
            result("get_news", {"news": []}, 1),
            result("get_corporate_actions", {"corporate_actions": summaries}, 2),
        ]
    )
    adapter = BoundedLifecycleResearch(mcp, FakeEvidence([]))

    with pytest.raises(LifecycleResearchError, match="CALL_LIMIT_EXCEEDED"):
        run(adapter)

    assert len(mcp.calls) == 2


def test_cross_kind_duplicate_source_ids_and_unsupported_tiers_are_rejected() -> None:
    duplicate = actions(include_dividend=True)
    duplicate_items = duplicate["corporate_actions"]
    assert isinstance(duplicate_items, list)
    duplicate_items[0]["id"] = "news-1"
    adapter = BoundedLifecycleResearch(
        FakeMCP([result("get_news", news(), 1), result("get_corporate_actions", duplicate, 2)]),
        FakeEvidence([]),
    )
    with pytest.raises(LifecycleResearchError, match="SOURCE_IDS_INVALID"):
        run(adapter)

    invalid_tier = news()
    invalid_items = invalid_tier["news"]
    assert isinstance(invalid_items, list)
    invalid_items[0]["source_tier"] = "BLOG"
    adapter = BoundedLifecycleResearch(
        FakeMCP(
            [
                result("get_news", invalid_tier, 1),
                result("get_corporate_actions", actions(), 2),
            ]
        ),
        FakeEvidence([]),
    )
    with pytest.raises(LifecycleResearchError, match="RESULT_SCHEMA_INVALID"):
        run(adapter)


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"headline": "x" * 513}, "RESULT_SCHEMA_INVALID"),
        ({"published_at": "2026-08-30T15:00:00Z"}, "RESULT_OUT_OF_WINDOW"),
    ],
)
def test_news_text_and_window_are_bounded(change: dict[str, object], code: str) -> None:
    payload = news()
    items = payload["news"]
    assert isinstance(items, list)
    items[0].update(change)
    adapter = BoundedLifecycleResearch(
        FakeMCP(
            [
                result("get_news", payload, 1),
                result("get_corporate_actions", actions(), 2),
            ]
        ),
        FakeEvidence([]),
    )

    with pytest.raises(LifecycleResearchError, match=code):
        run(adapter)


def test_raw_news_rejects_a_timezone_less_created_at() -> None:
    data = {
        "_alpaca_mcp_security": "untrusted",
        "data": {
            "news": [
                {
                    "id": "news-1",
                    "headline": "PANW files its quarterly report.",
                    "created_at": "2026-08-20T15:00:00",
                    "source": "wire",
                }
            ],
            "next_page_token": None,
        },
    }

    with pytest.raises(LifecycleResearchError, match="RESULT_SCHEMA_INVALID"):
        normalize_mcp_news(result("get_news", data, 1), START, NOW)


def test_corporate_action_detail_must_rejoin_the_selected_summary() -> None:
    changed_detail = detail()
    record = changed_detail["corporate_action"]
    assert isinstance(record, dict)
    record["id"] = "unselected-action"
    adapter = BoundedLifecycleResearch(
        FakeMCP(
            [
                result("get_news", news(), 1),
                result("get_corporate_actions", actions(include_dividend=True), 2),
                result("get_corporate_action_announcement", changed_detail, 3),
            ]
        ),
        FakeEvidence([]),
    )

    with pytest.raises(LifecycleResearchError, match="EX_DIVIDEND_EVIDENCE_AMBIGUOUS"):
        run(adapter, context(short_call=True))

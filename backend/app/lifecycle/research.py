from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from backend.app.alpaca.mcp import MCPResearchResult
from backend.app.contracts.v1 import AccountRole, EvidenceTier, SourceCluster
from backend.app.domain.option_contract_symbol import (
    NON_STANDARD_CONTRACT_UNSUPPORTED,
    OptionContractSymbolError,
    parse_standard_option_contract_symbol,
)

if TYPE_CHECKING:
    from backend.app.runtime.composition import RuntimeMCPResearch
    from backend.app.services.acquisition import RetainedLifecycleContext

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_MAX_CALLS = 6
_MAX_RESULTS = 24
_MAX_CLUSTERS = 12
_MAX_SOURCE_IDS = 48
_MAX_TEXT = 512
_MAX_AUDIT_DURATION = timedelta(seconds=30)
_SUPPORTED_ACTION_TYPES = frozenset(
    {"CASH_DIVIDEND", "STOCK_DIVIDEND", "SPLIT", "MERGER", "SPINOFF"}
)


class LifecycleResearchError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class LifecycleResearchSource:
    logical_source_id: str
    source_kind: str
    request_hash: str
    result_hash: str
    normalized_payload: dict[str, object]
    observed_at: datetime
    retrieved_at: datetime
    source_hash: str


class LifecycleResearchEvidencePort(Protocol):
    def persist_research_sources(
        self,
        context: RetainedLifecycleContext,
        records: tuple[LifecycleResearchSource, ...],
        trusted_at: datetime,
    ) -> None: ...


class BoundedLifecycleResearch:
    def __init__(
        self,
        runtime: RuntimeMCPResearch,
        evidence: LifecycleResearchEvidencePort,
    ) -> None:
        self._runtime = runtime
        self._evidence = evidence
        self._entered = False
        self._lock = asyncio.Lock()

    async def research(
        self,
        context: RetainedLifecycleContext,
        trusted_at: datetime,
    ) -> tuple[SourceCluster, ...]:
        async with self._lock:
            return await self._research(context, trusted_at)

    async def _research(
        self,
        context: RetainedLifecycleContext,
        trusted_at: datetime,
    ) -> tuple[SourceCluster, ...]:
        underlying, expiry = _research_authority(context, trusted_at)
        if not self._entered:
            await self._runtime.__aenter__()
            self._entered = True

        calls = 0
        news_result = await self._call(
            "get_news",
            {
                "symbols": [underlying],
                "start": _timestamp(context.lifecycle_origin_at),
                "end": _timestamp(trusted_at),
                "limit": _MAX_RESULTS,
            },
        )
        calls += 1
        news_items = _news_items(news_result.data, context.lifecycle_origin_at, trusted_at)

        action_result = await self._call(
            "get_corporate_actions",
            {
                "symbols": [underlying],
                "start": context.lifecycle_origin_at.date().isoformat(),
                "end": expiry.isoformat(),
                "limit": _MAX_RESULTS,
            },
        )
        calls += 1
        action_items = _action_items(
            action_result.data,
            context.lifecycle_origin_at,
            trusted_at,
            expiry,
        )

        source_ids = [item["id"] for item in [*news_items, *action_items]]
        if len(source_ids) != len(set(source_ids)) or len(source_ids) > _MAX_SOURCE_IDS:
            raise LifecycleResearchError("SOURCE_IDS_INVALID")
        clusters = [_news_cluster(item) for item in news_items]
        sources = [
            _research_source("MCP_NEWS", news_result, item, "published_at") for item in news_items
        ]
        _validate_dividend_evidence(context, action_items)

        if len(action_items) > _MAX_CALLS - calls:
            raise LifecycleResearchError("CALL_LIMIT_EXCEEDED")
        for summary in action_items:
            detail_result = await self._call(
                "get_corporate_action_announcement",
                {"announcement_id": summary["id"]},
            )
            calls += 1
            detail_item = _action_detail(
                detail_result.data,
                summary,
                context.lifecycle_origin_at,
                trusted_at,
                expiry,
            )
            clusters.append(_action_cluster(detail_item))
            sources.append(
                _research_source(
                    "MCP_CORPORATE_ACTION",
                    detail_result,
                    detail_item,
                    "announced_at",
                )
            )

        _validate_cluster_set(clusters)
        ordered_clusters = tuple(
            sorted(clusters, key=lambda item: (item.observed_at, item.cluster_id))
        )
        ordered_sources = tuple(
            sorted(sources, key=lambda item: (item.observed_at, item.logical_source_id))
        )
        cluster_source_ids = {
            source_id for item in ordered_clusters for source_id in item.source_ids
        }
        if {item.logical_source_id for item in ordered_sources} != cluster_source_ids:
            raise LifecycleResearchError("SOURCE_BINDING_INVALID")
        try:
            self._evidence.persist_research_sources(context, ordered_sources, trusted_at)
        except Exception as error:
            raise LifecycleResearchError("EVIDENCE_PERSIST_FAILED") from error
        return ordered_clusters

    async def _call(self, tool: str, arguments: Mapping[str, object]) -> MCPResearchResult:
        raw = await self._runtime.call(tool, arguments)
        return validate_mcp_research_result(raw, tool, arguments)


def validate_mcp_research_result(
    raw: object,
    tool: str,
    arguments: Mapping[str, object],
) -> MCPResearchResult:
    """Validate the retained MCP result and its exact request/result audit binding."""
    if not isinstance(raw, MCPResearchResult) or raw.tool_name != tool:
        raise LifecycleResearchError("RESULT_SCHEMA_INVALID")
    audit = raw.audit
    if (
        audit.tool_name != tool
        or audit.quality != "COMPLETE"
        or not _HASH.fullmatch(audit.argument_hash)
        or not _HASH.fullmatch(audit.result_summary_hash)
        or not _is_utc(audit.started_at)
        or not _is_utc(audit.completed_at)
        or audit.completed_at < audit.started_at
        or audit.completed_at - audit.started_at > _MAX_AUDIT_DURATION
        or audit.argument_hash != hashlib.sha256(_canonical_json(arguments)).hexdigest()
        or audit.result_summary_hash != hashlib.sha256(_canonical_json(raw.data)).hexdigest()
    ):
        raise LifecycleResearchError("AUDIT_INVALID")
    return raw


def normalize_mcp_news(
    result: MCPResearchResult,
    start: datetime,
    end: datetime,
) -> tuple[tuple[SourceCluster, ...], tuple[LifecycleResearchSource, ...]]:
    """Normalize bounded MCP news using the lifecycle source identity contract."""
    items = _news_items(result.data, start, end)
    clusters = [_news_cluster(item) for item in items]
    sources = [_research_source("MCP_NEWS", result, item, "published_at") for item in items]
    _validate_cluster_set(clusters)
    ordered_clusters = tuple(sorted(clusters, key=lambda item: (item.observed_at, item.cluster_id)))
    ordered_sources = tuple(
        sorted(sources, key=lambda item: (item.observed_at, item.logical_source_id))
    )
    return ordered_clusters, ordered_sources


def _research_authority(
    context: RetainedLifecycleContext,
    trusted_at: datetime,
) -> tuple[str, date]:
    if context.account_role is not AccountRole.DEVELOPMENT:
        raise LifecycleResearchError("DEVELOPMENT_ONLY")
    if (
        not _is_utc(trusted_at)
        or not _is_utc(context.lifecycle_origin_at)
        or not _is_utc(context.target_at)
        or context.lifecycle_origin_at > trusted_at
        or trusted_at > context.target_at
    ):
        raise LifecycleResearchError("WINDOW_INVALID")
    return _position_identity(context)


def _position_identity(context: RetainedLifecycleContext) -> tuple[str, date]:
    if len(context.expected_positions) != 2:
        raise LifecycleResearchError("POSITION_INVALID")
    underlying = context.thesis.thesis.underlying
    try:
        contracts = [
            parse_standard_option_contract_symbol(
                item.symbol,
                underlying_symbol=underlying,
            )
            for item in context.expected_positions
        ]
    except OptionContractSymbolError as error:
        if error.code == NON_STANDARD_CONTRACT_UNSUPPORTED:
            raise LifecycleResearchError(error.code) from error
        raise LifecycleResearchError("POSITION_INVALID") from error
    expiries = {contract.expiration_date for contract in contracts}
    if len(expiries) != 1:
        raise LifecycleResearchError("POSITION_INVALID")
    return underlying, expiries.pop()


def _has_short_call(context: RetainedLifecycleContext) -> bool:
    for item in context.expected_positions:
        try:
            contract = parse_standard_option_contract_symbol(item.symbol)
        except OptionContractSymbolError as error:
            if error.code == NON_STANDARD_CONTRACT_UNSUPPORTED:
                raise LifecycleResearchError(error.code) from error
            raise LifecycleResearchError("POSITION_INVALID") from error
        if contract.right == "C" and item.signed_quantity < 0:
            return True
    return False


def _validate_dividend_evidence(
    context: RetainedLifecycleContext,
    action_items: list[dict[str, object]],
) -> None:
    if not _has_short_call(context):
        return
    dividends = [item for item in action_items if item["type"] == "CASH_DIVIDEND"]
    if not dividends:
        raise LifecycleResearchError("EX_DIVIDEND_EVIDENCE_MISSING")
    if len(dividends) != 1:
        raise LifecycleResearchError("EX_DIVIDEND_EVIDENCE_AMBIGUOUS")


def _news_items(data: object, start: datetime, end: datetime) -> list[dict[str, object]]:
    if not isinstance(data, dict) or set(data) != {"news"} or not isinstance(data["news"], list):
        raise LifecycleResearchError("RESULT_SCHEMA_INVALID")
    if len(data["news"]) > _MAX_RESULTS:
        raise LifecycleResearchError("RESULT_LIMIT_EXCEEDED")
    items = [_news_item(item, start, end) for item in data["news"]]
    _canonical_order(items, "published_at")
    return items


def _news_item(raw: object, start: datetime, end: datetime) -> dict[str, object]:
    expected = {
        "id",
        "headline",
        "published_at",
        "source_tier",
        "independent_reporting_group",
    }
    if not isinstance(raw, dict) or set(raw) != expected:
        raise LifecycleResearchError("RESULT_SCHEMA_INVALID")
    identifier = _identifier(raw["id"])
    headline = _text(raw["headline"])
    observed = _datetime(raw["published_at"])
    tier = _tier(raw["source_tier"])
    group = _optional_identifier(raw["independent_reporting_group"])
    if not start <= observed <= end:
        raise LifecycleResearchError("RESULT_OUT_OF_WINDOW")
    return {
        "id": identifier,
        "headline": headline,
        "published_at": observed,
        "source_tier": tier,
        "independent_reporting_group": group,
    }


def _action_items(
    data: object,
    start: datetime,
    end: datetime,
    expiry: date,
) -> list[dict[str, object]]:
    if (
        not isinstance(data, dict)
        or set(data) != {"corporate_actions"}
        or not isinstance(data["corporate_actions"], list)
    ):
        raise LifecycleResearchError("RESULT_SCHEMA_INVALID")
    if len(data["corporate_actions"]) > _MAX_RESULTS:
        raise LifecycleResearchError("RESULT_LIMIT_EXCEEDED")
    items = [_action_summary(item, start, end, expiry) for item in data["corporate_actions"]]
    _canonical_order(items, "announced_at")
    return items


def _action_summary(raw: object, start: datetime, end: datetime, expiry: date) -> dict[str, object]:
    expected = {"id", "type", "announced_at", "ex_date"}
    if (
        not isinstance(raw, dict)
        or set(raw) != expected
        or raw["type"] not in _SUPPORTED_ACTION_TYPES
    ):
        raise LifecycleResearchError("RESULT_SCHEMA_INVALID")
    observed = _datetime(raw["announced_at"])
    ex_date = _date(raw["ex_date"])
    if not start <= observed <= end or not observed.date() <= ex_date <= expiry:
        raise LifecycleResearchError("RESULT_OUT_OF_WINDOW")
    return {
        "id": _identifier(raw["id"]),
        "type": raw["type"],
        "announced_at": observed,
        "ex_date": ex_date,
    }


def _action_detail(
    data: object,
    summary: dict[str, object],
    start: datetime,
    end: datetime,
    expiry: date,
) -> dict[str, object]:
    if not isinstance(data, dict) or set(data) != {"corporate_action"}:
        raise LifecycleResearchError("RESULT_SCHEMA_INVALID")
    raw = data["corporate_action"]
    expected = {
        "id",
        "type",
        "headline",
        "announced_at",
        "ex_date",
        "source_tier",
        "independent_reporting_group",
    }
    if not isinstance(raw, dict) or set(raw) != expected:
        raise LifecycleResearchError("RESULT_SCHEMA_INVALID")
    normalized = _action_summary(
        {key: raw[key] for key in ("id", "type", "announced_at", "ex_date")},
        start,
        end,
        expiry,
    )
    if normalized != summary:
        raise LifecycleResearchError("EX_DIVIDEND_EVIDENCE_AMBIGUOUS")
    return {
        **normalized,
        "headline": _text(raw["headline"]),
        "source_tier": _tier(raw["source_tier"]),
        "independent_reporting_group": _optional_identifier(raw["independent_reporting_group"]),
    }


def _news_cluster(item: dict[str, object]) -> SourceCluster:
    return _cluster("news", item, "published_at")


def _action_cluster(item: dict[str, object]) -> SourceCluster:
    return _cluster("corporate-action", item, "announced_at")


def _research_source(
    kind: str,
    result: MCPResearchResult,
    item: dict[str, object],
    observed_key: str,
) -> LifecycleResearchSource:
    payload = json.loads(_canonical_json(item))
    result_hash = hashlib.sha256(_canonical_json(payload)).hexdigest()
    source_material = {
        "logical_source_id": item["id"],
        "source_kind": kind,
        "request_hash": result.audit.argument_hash,
        "result_hash": result_hash,
        "normalized_payload": payload,
        "observed_at": item[observed_key],
        "retrieved_at": result.audit.completed_at,
    }
    return LifecycleResearchSource(
        logical_source_id=str(item["id"]),
        source_kind=kind,
        request_hash=result.audit.argument_hash,
        result_hash=result_hash,
        normalized_payload=payload,
        observed_at=item[observed_key],
        retrieved_at=result.audit.completed_at,
        source_hash=hashlib.sha256(_canonical_json(source_material)).hexdigest(),
    )


def _cluster(kind: str, item: dict[str, object], observed_key: str) -> SourceCluster:
    source_id = str(item["id"])
    material = _canonical_json({"kind": kind, "source_id": source_id})
    return SourceCluster(
        cluster_id=f"{kind}:{hashlib.sha256(material).hexdigest()}",
        source_ids=(source_id,),
        headline=str(item["headline"]),
        observed_at=item[observed_key],
        source_tier=item["source_tier"],
        independent_reporting_group=item["independent_reporting_group"],
    )


def _validate_cluster_set(clusters: list[SourceCluster]) -> None:
    source_ids = [source_id for cluster in clusters for source_id in cluster.source_ids]
    if (
        len(clusters) > _MAX_CLUSTERS
        or len(source_ids) > _MAX_SOURCE_IDS
        or len(source_ids) != len(set(source_ids))
    ):
        raise LifecycleResearchError("RESULT_LIMIT_EXCEEDED")


def _canonical_order(items: list[dict[str, object]], timestamp_key: str) -> None:
    expected = sorted(items, key=lambda item: (item[timestamp_key], item["id"]))
    if items != expected or len({item["id"] for item in items}) != len(items):
        raise LifecycleResearchError("RESULT_ORDER_INVALID")


def _identifier(value: object) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise LifecycleResearchError("RESULT_SCHEMA_INVALID")
    return value


def _optional_identifier(value: object) -> str | None:
    return None if value is None else _identifier(value)


def _text(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > _MAX_TEXT
        or any(ord(character) < 32 and character not in "\t" for character in value)
    ):
        raise LifecycleResearchError("RESULT_SCHEMA_INVALID")
    return " ".join(value.split())


def _tier(value: object) -> EvidenceTier:
    if type(value) is not str:
        raise LifecycleResearchError("RESULT_SCHEMA_INVALID")
    try:
        return EvidenceTier(value)
    except (TypeError, ValueError) as error:
        raise LifecycleResearchError("RESULT_SCHEMA_INVALID") from error


def _datetime(value: object) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise LifecycleResearchError("RESULT_SCHEMA_INVALID")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise LifecycleResearchError("RESULT_SCHEMA_INVALID") from error
    if not _is_utc(parsed) or _timestamp(parsed) != value:
        raise LifecycleResearchError("RESULT_SCHEMA_INVALID")
    return parsed


def _date(value: object) -> date:
    if type(value) is not str:
        raise LifecycleResearchError("RESULT_SCHEMA_INVALID")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise LifecycleResearchError("RESULT_SCHEMA_INVALID") from error
    if parsed.isoformat() != value:
        raise LifecycleResearchError("RESULT_SCHEMA_INVALID")
    return parsed


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _is_utc(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() == timedelta(0)
    )


def _canonical_json(value: object) -> bytes:
    def encode(item: object) -> object:
        if isinstance(item, datetime):
            return _timestamp(item)
        if isinstance(item, date):
            return item.isoformat()
        if isinstance(item, EvidenceTier):
            return item.value
        raise TypeError

    try:
        return json.dumps(
            value,
            default=encode,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as error:
        raise LifecycleResearchError("RESULT_SCHEMA_INVALID") from error

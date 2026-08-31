from __future__ import annotations

from datetime import datetime
from typing import Protocol

from backend.app.contracts.v1 import (
    AccountResponse,
    EvidenceClassification,
    OptionLeg,
    PositionListResponse,
    SourceCluster,
    ThesisResponse,
)


class TradingReadPort(Protocol):
    def get_account(self) -> AccountResponse: ...
    def list_positions(self) -> PositionListResponse: ...
    def list_open_orders(self) -> tuple[dict[str, object], ...]: ...


class MarketDataPort(Protocol):
    def get_option_legs(self, symbols: tuple[str, ...]) -> tuple[OptionLeg, ...]: ...


class AccountActivityPort(Protocol):
    def list_activities(self, since: datetime) -> tuple[dict[str, object], ...]: ...


class MCPResearchPort(Protocol):
    def research(self, source_ids: tuple[str, ...]) -> tuple[SourceCluster, ...]: ...


class EvidenceClassifierPort(Protocol):
    def classify(
        self, thesis: ThesisResponse, clusters: tuple[SourceCluster, ...]
    ) -> tuple[EvidenceClassification, ...]: ...


class ExecutionBrokerPort(Protocol):
    def submit(self, intent_digest: str, request: dict[str, object]) -> dict[str, object]: ...
    def reconcile(self, client_order_id: str) -> dict[str, object]: ...


class RepositoryPort(Protocol):
    def add(self, entity: object) -> object: ...
    def get(self, entity_type: str, entity_id: str) -> object | None: ...


class ClockPort(Protocol):
    def now(self) -> datetime: ...

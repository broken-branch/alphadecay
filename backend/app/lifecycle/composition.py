from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from backend.app.services.acquisition import (
    DevelopmentLifecycleAcquisition,
    LifecycleClassifierPort,
)

from .observation import (
    AlpacaLifecycleObservationAdapter,
    LifecycleAccountCollector,
    LifecycleMarketCollector,
)
from .repository import SQLAlchemyLifecycleRepository
from .research import BoundedLifecycleResearch

if TYPE_CHECKING:
    from backend.app.runtime.composition import RuntimeMCPResearch


@dataclass(frozen=True)
class LifecycleAdapterComposition:
    acquisition: DevelopmentLifecycleAcquisition
    repository: SQLAlchemyLifecycleRepository


def build_lifecycle_adapters(
    *,
    repository: SQLAlchemyLifecycleRepository,
    accounts: LifecycleAccountCollector,
    markets: LifecycleMarketCollector,
    mcp_research: RuntimeMCPResearch,
    classifier: LifecycleClassifierPort,
) -> LifecycleAdapterComposition:
    observations = AlpacaLifecycleObservationAdapter(accounts, markets, repository)
    research = BoundedLifecycleResearch(mcp_research, repository)
    acquisition = DevelopmentLifecycleAcquisition(
        repository,
        observations,
        research,
        classifier,
        repository,
    )
    return LifecycleAdapterComposition(acquisition, repository)

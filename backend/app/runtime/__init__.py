"""Paper-only runtime composition."""

from .composition import (
    PAPER_TRADING_ENDPOINT,
    BoundProviderResource,
    ProviderBinding,
    RuntimeAccountConfig,
    RuntimeAccountSweep,
    RuntimeAccountView,
    RuntimeComposition,
    RuntimeCompositionError,
    RuntimeDependencies,
    RuntimeEvidenceClassifier,
    RuntimeMCPResearch,
    RuntimeProviderBundle,
    RuntimeResource,
    build_runtime,
)
from .production import (
    ProductionAgent,
    ProductionOpportunityWiring,
    RuntimeAccountAuthority,
    SettingsCalibrationBinding,
    build_production_agent,
    build_production_opportunity_wiring,
)
from .providers import (
    OpportunityHaltAuthority,
    ProductionResources,
    build_production_opportunity_halt_resource,
    build_production_resources,
)

__all__ = [
    "BoundProviderResource",
    "PAPER_TRADING_ENDPOINT",
    "ProviderBinding",
    "OpportunityHaltAuthority",
    "ProductionResources",
    "ProductionAgent",
    "ProductionOpportunityWiring",
    "RuntimeAccountConfig",
    "RuntimeAccountSweep",
    "RuntimeAccountView",
    "RuntimeComposition",
    "RuntimeCompositionError",
    "RuntimeDependencies",
    "RuntimeEvidenceClassifier",
    "RuntimeMCPResearch",
    "RuntimeProviderBundle",
    "RuntimeResource",
    "RuntimeAccountAuthority",
    "SettingsCalibrationBinding",
    "build_runtime",
    "build_production_opportunity_halt_resource",
    "build_production_resources",
    "build_production_agent",
    "build_production_opportunity_wiring",
]

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from backend.app.contracts.v1.models import ContractModel, Money, Percent


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("text cannot be blank")
    return value


BriefContent = Annotated[
    str,
    Field(min_length=20, max_length=20_000),
    AfterValidator(_nonblank),
]
MarketScope = Annotated[str, Field(min_length=1, max_length=120), AfterValidator(_nonblank)]
Horizon = Annotated[str, Field(min_length=1, max_length=240), AfterValidator(_nonblank)]
EvidenceItem = Annotated[str, Field(min_length=1, max_length=1_000), AfterValidator(_nonblank)]
Note = Annotated[str, Field(min_length=1, max_length=4_000), AfterValidator(_nonblank)]
ProtocolRule = Annotated[str, Field(min_length=1, max_length=2_000), AfterValidator(_nonblank)]


class BriefSourceKind(StrEnum):
    PASTED_TEXT = "PASTED_TEXT"
    TEXT_FILE = "TEXT_FILE"
    MARKDOWN_FILE = "MARKDOWN_FILE"


class StrategyDirection(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    UNSURE = "UNSURE"


class BriefModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StrategyBriefSource(BriefModel):
    kind: BriefSourceKind
    content: BriefContent
    filename: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def source_matches_kind(self) -> StrategyBriefSource:
        if self.kind is BriefSourceKind.PASTED_TEXT:
            if self.filename is not None:
                raise ValueError("pasted text cannot have a filename")
            return self
        if self.filename is None:
            raise ValueError("file source requires a filename")
        suffix = self.filename.lower()
        if self.kind is BriefSourceKind.TEXT_FILE and not suffix.endswith(".txt"):
            raise ValueError("text brief must use a .txt filename")
        if self.kind is BriefSourceKind.MARKDOWN_FILE and not suffix.endswith((".md", ".markdown")):
            raise ValueError("Markdown brief must use a .md or .markdown filename")
        return self


class StrategyRiskBudget(BriefModel):
    max_loss_dollars: Money | None = Field(default=None, gt=Decimal("0"))
    max_account_percent: Percent | None = Field(
        default=None,
        gt=Decimal("0"),
        le=Decimal("100"),
    )

    @model_validator(mode="after")
    def at_least_one_limit(self) -> StrategyRiskBudget:
        if self.max_loss_dollars is None and self.max_account_percent is None:
            raise ValueError("risk budget requires a dollar or account percentage limit")
        return self


class StrategyBriefRequest(BriefModel):
    source: StrategyBriefSource
    market_scope: MarketScope | None = None
    direction: StrategyDirection | None = None
    horizon: Horizon | None = None
    evidence: tuple[EvidenceItem, ...] = Field(default=(), max_length=12)
    invalidation: tuple[EvidenceItem, ...] = Field(default=(), max_length=12)
    risk_budget: StrategyRiskBudget | None = None
    notes: Note | None = None


class ProtocolAssumption(StrEnum):
    USER_BRIEF_UNVERIFIED = "USER_BRIEF_UNVERIFIED"
    OPTIONS_ONLY = "OPTIONS_ONLY"
    PAPER_ONLY = "PAPER_ONLY"
    DEFINED_RISK_ONLY = "DEFINED_RISK_ONLY"


class ProtocolQuestion(StrEnum):
    MARKET_SCOPE_REQUIRED = "MARKET_SCOPE_REQUIRED"
    DIRECTION_REQUIRED = "DIRECTION_REQUIRED"
    HORIZON_REQUIRED = "HORIZON_REQUIRED"
    EVIDENCE_REQUIRED = "EVIDENCE_REQUIRED"
    INVALIDATION_REQUIRED = "INVALIDATION_REQUIRED"
    RISK_BUDGET_REQUIRED = "RISK_BUDGET_REQUIRED"
    DIRECTION_REVIEW_REQUIRED = "DIRECTION_REVIEW_REQUIRED"


class CandidateStructureFamily(StrEnum):
    BULL_CALL_DEBIT_SPREAD = "BULL_CALL_DEBIT_SPREAD"
    BEAR_PUT_DEBIT_SPREAD = "BEAR_PUT_DEBIT_SPREAD"
    IRON_CONDOR = "IRON_CONDOR"


class EvidenceCheck(StrEnum):
    VERIFY_THESIS_CLAIMS = "VERIFY_THESIS_CLAIMS"
    CHECK_MARKET_DATA_RECENCY = "CHECK_MARKET_DATA_RECENCY"
    CHECK_OPTION_LIQUIDITY = "CHECK_OPTION_LIQUIDITY"
    CHECK_INVALIDATION_STATE = "CHECK_INVALIDATION_STATE"


class ExitRequirement(StrEnum):
    PROFIT_EXIT_REQUIRED = "PROFIT_EXIT_REQUIRED"
    LOSS_EXIT_REQUIRED = "LOSS_EXIT_REQUIRED"
    TIME_EXIT_REQUIRED = "TIME_EXIT_REQUIRED"


class PromotionRequirement(StrEnum):
    MODEL_CURATION_REQUIRED = "MODEL_CURATION_REQUIRED"
    EVIDENCE_REVIEW_REQUIRED = "EVIDENCE_REVIEW_REQUIRED"
    RISK_REVIEW_REQUIRED = "RISK_REVIEW_REQUIRED"
    OWNER_REVIEW_REQUIRED = "OWNER_REVIEW_REQUIRED"


class StructureConstraints(BriefModel):
    options_required: Literal[True] = True
    defined_risk_required: Literal[True] = True
    naked_short_options_allowed: Literal[False] = False
    direction: StrategyDirection | None
    candidate_families: tuple[CandidateStructureFamily, ...]


class EvidencePlan(BriefModel):
    submitted_evidence: tuple[str, ...]
    required_checks: tuple[EvidenceCheck, ...]


class RiskRules(BriefModel):
    budget: StrategyRiskBudget | None
    loss_must_be_bounded: Literal[True] = True
    size_must_fit_budget: Literal[True] = True


class ExitRules(BriefModel):
    invalidation: tuple[str, ...]
    required_before_promotion: tuple[ExitRequirement, ...]


class StrategyProtocolDraftResponse(ContractModel):
    status: Literal["DRAFT_REVIEW_REQUIRED"] = "DRAFT_REVIEW_REQUIRED"
    curation_status: Literal["NOT_CURATED"] = "NOT_CURATED"
    automation_state: Literal["OFF"] = "OFF"
    execution_eligible: Literal[False] = False
    intake: StrategyBriefRequest
    assumptions: tuple[ProtocolAssumption, ...]
    questions: tuple[ProtocolQuestion, ...]
    required_before_promotion: tuple[PromotionRequirement, ...]
    structure_constraints: StructureConstraints
    evidence_plan: EvidencePlan
    risk_rules: RiskRules
    exit_rules: ExitRules


class CuratedStructure(StrEnum):
    BULL_CALL_DEBIT_SPREAD = "BULL_CALL_DEBIT_SPREAD"
    BEAR_PUT_DEBIT_SPREAD = "BEAR_PUT_DEBIT_SPREAD"
    IRON_CONDOR = "IRON_CONDOR"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class CurationReadiness(StrEnum):
    READY = "READY"
    NEEDS_INPUT = "NEEDS_INPUT"
    CONFLICT_REVIEW = "CONFLICT_REVIEW"


class CurationConfidence(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class CurationBlockingQuestion(StrEnum):
    MARKET_SCOPE_REQUIRED = "MARKET_SCOPE_REQUIRED"
    DIRECTION_REVIEW_REQUIRED = "DIRECTION_REVIEW_REQUIRED"
    HORIZON_REQUIRED = "HORIZON_REQUIRED"
    EVIDENCE_REQUIRED = "EVIDENCE_REQUIRED"
    RISK_BUDGET_REQUIRED = "RISK_BUDGET_REQUIRED"
    ENTRY_RULE_REQUIRED = "ENTRY_RULE_REQUIRED"
    NO_TRADE_RULE_REQUIRED = "NO_TRADE_RULE_REQUIRED"
    PROFIT_EXIT_REQUIRED = "PROFIT_EXIT_REQUIRED"
    LOSS_EXIT_REQUIRED = "LOSS_EXIT_REQUIRED"
    TIME_EXIT_REQUIRED = "TIME_EXIT_REQUIRED"
    INVALIDATION_REQUIRED = "INVALIDATION_REQUIRED"
    STRUCTURE_REVIEW_REQUIRED = "STRUCTURE_REVIEW_REQUIRED"


class StrategyProtocolFields(BriefModel):
    entry_rule: ProtocolRule | None = None
    no_trade_rule: ProtocolRule | None = None
    profit_exit_rule: ProtocolRule | None = None
    loss_exit_rule: ProtocolRule | None = None
    time_exit_rule: ProtocolRule | None = None
    invalidation_rules: tuple[ProtocolRule, ...] = Field(default=(), max_length=12)


class StrategyCurationRequest(BriefModel):
    brief: StrategyBriefRequest
    protocol_fields: StrategyProtocolFields = Field(default_factory=StrategyProtocolFields)


class StrategyCurationClassifications(BriefModel):
    direction: StrategyDirection
    structure: CuratedStructure
    clarity: CurationReadiness
    evidence: CurationReadiness
    risk: CurationReadiness
    exit: CurationReadiness
    confidence: CurationConfidence


class SupportingEvidenceExcerpt(BriefModel):
    evidence_id: str = Field(pattern=r"^evidence-(?:[1-9]|1[0-2])$")
    excerpt: EvidenceItem


class StrategyCurationResponse(ContractModel):
    status: Literal["CURATED_REVIEW_REQUIRED"] = "CURATED_REVIEW_REQUIRED"
    curation_status: Literal["MODEL_CURATED"] = "MODEL_CURATED"
    automation_state: Literal["OFF"] = "OFF"
    execution_eligible: Literal[False] = False
    paper_trading_only: Literal[True] = True
    options_required: Literal[True] = True
    defined_risk_required: Literal[True] = True
    intake: StrategyBriefRequest
    protocol_fields: StrategyProtocolFields
    classifications: StrategyCurationClassifications
    blocking_questions: tuple[CurationBlockingQuestion, ...] = Field(max_length=12)
    supporting_evidence: tuple[SupportingEvidenceExcerpt, ...] = Field(max_length=12)

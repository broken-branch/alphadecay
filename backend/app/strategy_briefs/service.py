from __future__ import annotations

from backend.app.strategy_briefs.models import (
    CandidateStructureFamily,
    EvidenceCheck,
    EvidencePlan,
    ExitRequirement,
    ExitRules,
    PromotionRequirement,
    ProtocolAssumption,
    ProtocolQuestion,
    RiskRules,
    StrategyBriefRequest,
    StrategyDirection,
    StrategyProtocolDraftResponse,
    StructureConstraints,
)

_ASSUMPTIONS = (
    ProtocolAssumption.USER_BRIEF_UNVERIFIED,
    ProtocolAssumption.OPTIONS_ONLY,
    ProtocolAssumption.PAPER_ONLY,
    ProtocolAssumption.DEFINED_RISK_ONLY,
)
_EVIDENCE_CHECKS = (
    EvidenceCheck.VERIFY_THESIS_CLAIMS,
    EvidenceCheck.CHECK_MARKET_DATA_RECENCY,
    EvidenceCheck.CHECK_OPTION_LIQUIDITY,
    EvidenceCheck.CHECK_INVALIDATION_STATE,
)
_EXIT_REQUIREMENTS = (
    ExitRequirement.PROFIT_EXIT_REQUIRED,
    ExitRequirement.LOSS_EXIT_REQUIRED,
    ExitRequirement.TIME_EXIT_REQUIRED,
)
_PROMOTION_REQUIREMENTS = (
    PromotionRequirement.MODEL_CURATION_REQUIRED,
    PromotionRequirement.EVIDENCE_REVIEW_REQUIRED,
    PromotionRequirement.RISK_REVIEW_REQUIRED,
    PromotionRequirement.OWNER_REVIEW_REQUIRED,
)
_FAMILIES_BY_DIRECTION = {
    StrategyDirection.BULLISH: (CandidateStructureFamily.BULL_CALL_DEBIT_SPREAD,),
    StrategyDirection.BEARISH: (CandidateStructureFamily.BEAR_PUT_DEBIT_SPREAD,),
    StrategyDirection.NEUTRAL: (CandidateStructureFamily.IRON_CONDOR,),
    StrategyDirection.UNSURE: (),
}


def draft_strategy_protocol(brief: StrategyBriefRequest) -> StrategyProtocolDraftResponse:
    questions: list[ProtocolQuestion] = []
    if brief.market_scope is None:
        questions.append(ProtocolQuestion.MARKET_SCOPE_REQUIRED)
    if brief.direction is None:
        questions.append(ProtocolQuestion.DIRECTION_REQUIRED)
    if brief.horizon is None:
        questions.append(ProtocolQuestion.HORIZON_REQUIRED)
    if not brief.evidence:
        questions.append(ProtocolQuestion.EVIDENCE_REQUIRED)
    if not brief.invalidation:
        questions.append(ProtocolQuestion.INVALIDATION_REQUIRED)
    if brief.risk_budget is None:
        questions.append(ProtocolQuestion.RISK_BUDGET_REQUIRED)
    if brief.direction is StrategyDirection.UNSURE:
        questions.append(ProtocolQuestion.DIRECTION_REVIEW_REQUIRED)

    families = _FAMILIES_BY_DIRECTION.get(brief.direction, ())
    return StrategyProtocolDraftResponse(
        intake=brief,
        assumptions=_ASSUMPTIONS,
        questions=tuple(questions),
        required_before_promotion=_PROMOTION_REQUIREMENTS,
        structure_constraints=StructureConstraints(
            direction=brief.direction,
            candidate_families=families,
        ),
        evidence_plan=EvidencePlan(
            submitted_evidence=brief.evidence,
            required_checks=_EVIDENCE_CHECKS,
        ),
        risk_rules=RiskRules(budget=brief.risk_budget),
        exit_rules=ExitRules(
            invalidation=brief.invalidation,
            required_before_promotion=_EXIT_REQUIREMENTS,
        ),
    )

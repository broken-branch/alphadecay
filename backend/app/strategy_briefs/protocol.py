from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from backend.app.contracts.v1.models import ExactDecimal, Money, Percent, UtcDateTime
from backend.app.order_limits import MAX_STRUCTURAL_OPTION_QUANTITY
from backend.app.strategy_briefs.models import (
    CuratedStructure,
    CurationReadiness,
    ProtocolRule,
    StrategyCurationResponse,
    StrategyDirection,
    StrategyRiskBudget,
)

_SYMBOL = re.compile(r"^[A-Z]{1,6}$")
_HASH_PREFIX = "alphadecay.reviewed-executable-protocol"


class ProtocolCompilationBlocked(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProtocolEvaluationBlocked(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProtocolMetric(StrEnum):
    UNDERLYING_LAST_PRICE = "UNDERLYING_LAST_PRICE"
    UNDERLYING_SESSION_CLOSE = "UNDERLYING_SESSION_CLOSE"
    UNDERLYING_SESSION_VWAP = "UNDERLYING_SESSION_VWAP"
    UNDERLYING_SMA_20 = "UNDERLYING_SMA_20"
    UNDERLYING_SMA_50 = "UNDERLYING_SMA_50"
    UNDERLYING_RETURN_PERCENT = "UNDERLYING_RETURN_PERCENT"
    OPTION_RELATIVE_SPREAD_PERCENT = "OPTION_RELATIVE_SPREAD_PERCENT"
    OPTION_QUOTE_AGE_SECONDS = "OPTION_QUOTE_AGE_SECONDS"
    DAYS_TO_EXPIRY = "DAYS_TO_EXPIRY"
    POSITION_RETURN_ON_MAX_RISK_PERCENT = "POSITION_RETURN_ON_MAX_RISK_PERCENT"
    TRADING_SESSIONS_HELD = "TRADING_SESSIONS_HELD"
    MINUTES_TO_SESSION_CLOSE = "MINUTES_TO_SESSION_CLOSE"


class ProtocolFact(StrEnum):
    PAPER_ACCOUNT_CONFIRMED = "PAPER_ACCOUNT_CONFIRMED"
    BASELINE_CLEAN = "BASELINE_CLEAN"
    ACCOUNT_FLAT = "ACCOUNT_FLAT"
    NO_OPEN_ORDER = "NO_OPEN_ORDER"
    BUYING_POWER_SUFFICIENT = "BUYING_POWER_SUFFICIENT"
    NO_PRIOR_ENTRY_ATTEMPT = "NO_PRIOR_ENTRY_ATTEMPT"
    MARKET_OPEN = "MARKET_OPEN"
    TRADING_NOT_HALTED = "TRADING_NOT_HALTED"
    MARKET_DATA_COMPLETE = "MARKET_DATA_COMPLETE"
    OPTION_QUOTES_FRESH = "OPTION_QUOTES_FRESH"
    OPTION_QUOTES_SYNCHRONIZED = "OPTION_QUOTES_SYNCHRONIZED"
    OPTION_LIQUIDITY_ACCEPTABLE = "OPTION_LIQUIDITY_ACCEPTABLE"
    RISK_WITHIN_REVIEWED_BUDGET = "RISK_WITHIN_REVIEWED_BUDGET"


class ComparisonOperator(StrEnum):
    LESS_THAN = "LESS_THAN"
    LESS_THAN_OR_EQUAL = "LESS_THAN_OR_EQUAL"
    EQUAL = "EQUAL"
    GREATER_THAN_OR_EQUAL = "GREATER_THAN_OR_EQUAL"
    GREATER_THAN = "GREATER_THAN"


class RuleMatchMode(StrEnum):
    ALL = "ALL"
    ANY = "ANY"


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")


class MetricOperand(ProtocolModel):
    kind: Literal["METRIC"] = "METRIC"
    metric: ProtocolMetric


class ConstantOperand(ProtocolModel):
    kind: Literal["CONSTANT"] = "CONSTANT"
    value: ExactDecimal


ProtocolOperand = Annotated[
    MetricOperand | ConstantOperand,
    Field(discriminator="kind"),
]


class ProtocolNumericPredicate(ProtocolModel):
    kind: Literal["NUMERIC"] = "NUMERIC"
    left: MetricOperand
    operator: ComparisonOperator
    right: ProtocolOperand


class ProtocolFactPredicate(ProtocolModel):
    kind: Literal["FACT"] = "FACT"
    fact: ProtocolFact
    expected: bool


ProtocolPredicate = Annotated[
    ProtocolNumericPredicate | ProtocolFactPredicate,
    Field(discriminator="kind"),
]


class MachineRule(ProtocolModel):
    source_text: ProtocolRule
    mapping_state: Literal["FULLY_MAPPED"]
    match: RuleMatchMode = RuleMatchMode.ALL
    predicates: tuple[ProtocolPredicate, ...] = Field(min_length=1, max_length=12)


class ReviewedProtocolRules(ProtocolModel):
    entry_rule: MachineRule
    no_trade_rule: MachineRule
    profit_exit_rule: MachineRule
    loss_exit_rule: MachineRule
    time_exit_rule: MachineRule
    invalidation_rules: tuple[MachineRule, ...] = Field(min_length=1, max_length=12)


class ProtocolSchedule(ProtocolModel):
    event_session: date
    pre_event_session: date
    reaction_session: date
    signal_session: date
    daily_start_session: date
    evidence_window_start: UtcDateTime
    evidence_window_end: UtcDateTime
    entry_window_start: UtcDateTime
    decision_boundary: UtcDateTime
    entry_window_end: UtcDateTime

    @model_validator(mode="after")
    def windows_are_ordered(self) -> ProtocolSchedule:
        if not (
            self.daily_start_session
            <= self.pre_event_session
            <= self.event_session
            <= self.reaction_session
            <= self.signal_session
        ):
            raise ValueError("protocol sessions must be chronological")
        if not self.evidence_window_start < self.evidence_window_end:
            raise ValueError("evidence window must be increasing")
        if self.evidence_window_end > self.decision_boundary:
            raise ValueError("evidence window must close by the decision boundary")
        if not self.entry_window_start <= self.decision_boundary < self.entry_window_end:
            raise ValueError("decision boundary must be inside the entry window")
        if (
            self.signal_session != self.decision_boundary.date()
            or self.entry_window_start.date() != self.signal_session
            or self.entry_window_end.date() != self.signal_session
            or self.decision_boundary.second != 0
            or self.decision_boundary.microsecond != 0
            or self.decision_boundary.minute % 5 != 0
        ):
            raise ValueError(
                "decision boundary must bind the signal session and a five-minute mark"
            )
        return self


class DebitVerticalSelection(ProtocolModel):
    minimum_expiry: date
    maximum_expiry: date
    minimum_dte: int = Field(ge=1, le=730)
    target_dte: int = Field(ge=1, le=730)
    maximum_dte: int = Field(ge=1, le=730)
    minimum_strike: Money = Field(gt=Decimal("0"))
    maximum_strike: Money = Field(gt=Decimal("0"))
    width_dollars: Money = Field(gt=Decimal("0"))
    quantity: int = Field(ge=1, le=MAX_STRUCTURAL_OPTION_QUANTITY)
    maximum_debit_per_share: Money = Field(gt=Decimal("0"))
    maximum_loss_dollars: Money = Field(gt=Decimal("0"))
    maximum_contracts_considered: int = Field(ge=1, le=128)

    @model_validator(mode="after")
    def bounds_are_consistent(self) -> DebitVerticalSelection:
        if self.minimum_expiry > self.maximum_expiry:
            raise ValueError("expiry window must be increasing")
        if (self.maximum_expiry - self.minimum_expiry).days > 45:
            raise ValueError("expiry window is too wide")
        if not self.minimum_dte <= self.target_dte <= self.maximum_dte:
            raise ValueError("target DTE must be inside the DTE window")
        if self.minimum_strike >= self.maximum_strike:
            raise ValueError("strike window must be increasing")
        if self.maximum_strike - self.minimum_strike > Decimal("1000"):
            raise ValueError("strike window is too wide")
        if self.maximum_debit_per_share >= self.width_dollars:
            raise ValueError("debit must be below vertical width")
        expected_loss = self.maximum_debit_per_share * self.quantity * Decimal("100")
        if self.maximum_loss_dollars != expected_loss:
            raise ValueError("maximum loss must equal the bounded debit exposure")
        return self


class ProtocolMarketQuality(ProtocolModel):
    maximum_underlying_age_seconds: int = Field(ge=1, le=3_600)
    maximum_option_quote_age_seconds: int = Field(ge=1, le=120)
    maximum_leg_quote_skew_seconds: int = Field(ge=1, le=30)
    maximum_relative_spread_percent: Percent = Field(gt=Decimal("0"), le=Decimal("100"))
    minimum_leg_bid_size: int = Field(ge=1, le=1_000_000)
    minimum_leg_ask_size: int = Field(ge=1, le=1_000_000)


class ReviewedProtocolDefinition(ProtocolModel):
    opportunity_key: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    definition_version: int = Field(ge=1, le=1_000_000)
    benchmark_symbol: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,9}$")
    allowed_event_codes: tuple[str, ...] = Field(min_length=1, max_length=12)
    thesis_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    invalidation_codes: tuple[str, ...] = Field(min_length=1, max_length=12)
    schedule: ProtocolSchedule
    selection: DebitVerticalSelection
    market_quality: ProtocolMarketQuality
    maximum_account_risk_percent: Percent | None = Field(
        default=None,
        gt=Decimal("0"),
        le=Decimal("100"),
    )

    @model_validator(mode="after")
    def codes_are_unique(self) -> ReviewedProtocolDefinition:
        code = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
        if (
            len(set(self.allowed_event_codes)) != len(self.allowed_event_codes)
            or len(set(self.invalidation_codes)) != len(self.invalidation_codes)
            or any(code.fullmatch(item) is None for item in self.allowed_event_codes)
            or any(code.fullmatch(item) is None for item in self.invalidation_codes)
        ):
            raise ValueError("protocol codes must be unique bounded codes")
        return self


class ReviewedExecutableProtocolRequest(ProtocolModel):
    review_state: Literal["REVIEWED"] = "REVIEWED"
    curation: StrategyCurationResponse
    definition: ReviewedProtocolDefinition | None = None
    rules: ReviewedProtocolRules


class CompiledMachineRule(ProtocolModel):
    source_rule_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    match: RuleMatchMode
    predicates: tuple[ProtocolPredicate, ...] = Field(min_length=1, max_length=12)


class CompiledProtocolRules(ProtocolModel):
    entry_rule: CompiledMachineRule
    no_trade_rule: CompiledMachineRule
    profit_exit_rule: CompiledMachineRule
    loss_exit_rule: CompiledMachineRule
    time_exit_rule: CompiledMachineRule
    invalidation_rules: tuple[CompiledMachineRule, ...] = Field(min_length=1, max_length=12)


class CompiledStrategyProtocol(ProtocolModel):
    review_state: Literal["REVIEWED"] = "REVIEWED"
    compile_status: Literal["COMPILABLE"] = "COMPILABLE"
    arm_state: Literal["NOT_ARMED"] = "NOT_ARMED"
    automation_state: Literal["OFF"] = "OFF"
    execution_eligible: Literal[False] = False
    paper_trading_only: Literal[True] = True
    options_required: Literal[True] = True
    defined_risk_required: Literal[True] = True
    recipe: Literal["TWO_LEG_DEBIT_VERTICAL"] = "TWO_LEG_DEBIT_VERTICAL"
    leg_count: Literal[2] = 2
    net_premium: Literal["DEBIT"] = "DEBIT"
    symbol: str = Field(pattern=r"^[A-Z]{1,6}$")
    direction: StrategyDirection
    structure: CuratedStructure
    risk_budget: StrategyRiskBudget
    definition: ReviewedProtocolDefinition
    rules: CompiledProtocolRules
    mandatory_safety_facts: tuple[ProtocolFact, ...]
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    compiler_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProtocolObservationSet(ProtocolModel):
    numeric_values: dict[ProtocolMetric, ExactDecimal]
    facts: dict[ProtocolFact, bool]


class ProtocolEvaluation(ProtocolModel):
    status: Literal["EVALUATED"] = "EVALUATED"
    arm_state: Literal["NOT_ARMED"] = "NOT_ARMED"
    execution_eligible: Literal[False] = False
    protocol_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    safety_gates_passed: bool
    entry_rule_matched: bool
    no_trade_rule_matched: bool
    profit_exit_rule_matched: bool
    loss_exit_rule_matched: bool
    time_exit_rule_matched: bool
    invalidation_rule_matches: tuple[bool, ...]


_MANDATORY_SAFETY_FACTS = (
    ProtocolFact.PAPER_ACCOUNT_CONFIRMED,
    ProtocolFact.BASELINE_CLEAN,
    ProtocolFact.ACCOUNT_FLAT,
    ProtocolFact.NO_OPEN_ORDER,
    ProtocolFact.BUYING_POWER_SUFFICIENT,
    ProtocolFact.NO_PRIOR_ENTRY_ATTEMPT,
    ProtocolFact.MARKET_OPEN,
    ProtocolFact.TRADING_NOT_HALTED,
    ProtocolFact.MARKET_DATA_COMPLETE,
    ProtocolFact.OPTION_QUOTES_FRESH,
    ProtocolFact.OPTION_QUOTES_SYNCHRONIZED,
    ProtocolFact.OPTION_LIQUIDITY_ACCEPTABLE,
    ProtocolFact.RISK_WITHIN_REVIEWED_BUDGET,
)
_STRUCTURE_BY_DIRECTION = {
    StrategyDirection.BULLISH: CuratedStructure.BULL_CALL_DEBIT_SPREAD,
    StrategyDirection.BEARISH: CuratedStructure.BEAR_PUT_DEBIT_SPREAD,
    StrategyDirection.NEUTRAL: CuratedStructure.IRON_CONDOR,
}


def compile_reviewed_protocol(
    request: ReviewedExecutableProtocolRequest,
) -> CompiledStrategyProtocol:
    if type(request) is not ReviewedExecutableProtocolRequest:
        raise ProtocolCompilationBlocked("PROTOCOL_REQUEST_INVALID")
    try:
        request = ReviewedExecutableProtocolRequest.model_validate(
            request.model_dump(mode="python")
        )
    except ValidationError as exc:
        raise ProtocolCompilationBlocked("PROTOCOL_REQUEST_INVALID") from exc
    curation = request.curation
    classifications = curation.classifications
    if curation.blocking_questions or any(
        readiness is not CurationReadiness.READY
        for readiness in (
            classifications.clarity,
            classifications.evidence,
            classifications.risk,
            classifications.exit,
        )
    ):
        raise ProtocolCompilationBlocked("PROTOCOL_REVIEW_INCOMPLETE")
    if (
        classifications.direction is StrategyDirection.UNSURE
        or classifications.structure is CuratedStructure.REVIEW_REQUIRED
    ):
        raise ProtocolCompilationBlocked("PROTOCOL_DIRECTION_UNRESOLVED")
    if classifications.structure is CuratedStructure.IRON_CONDOR:
        raise ProtocolCompilationBlocked("PROTOCOL_RECIPE_UNSUPPORTED")
    expected_structure = _STRUCTURE_BY_DIRECTION.get(classifications.direction)
    if classifications.structure is not expected_structure:
        raise ProtocolCompilationBlocked("PROTOCOL_DIRECTION_STRUCTURE_CONFLICT")
    if curation.intake.direction is not classifications.direction:
        raise ProtocolCompilationBlocked("PROTOCOL_DIRECTION_BINDING_MISMATCH")

    symbol = curation.intake.market_scope
    if symbol is None or _SYMBOL.fullmatch(symbol) is None:
        raise ProtocolCompilationBlocked("PROTOCOL_SINGLE_SYMBOL_REQUIRED")
    if curation.intake.risk_budget is None:
        raise ProtocolCompilationBlocked("PROTOCOL_RISK_BUDGET_REQUIRED")
    if request.definition is None:
        raise ProtocolCompilationBlocked("PROTOCOL_DEFINITION_REQUIRED")
    _validate_evidence_binding(curation)
    _validate_rule_binding(curation, request.rules)
    _validate_definition(curation, request.definition, request.rules)

    compiled_rules = _compile_rules(request.rules)
    source_hash = _canonical_hash(f"{_HASH_PREFIX}.source.v1", curation)
    definition_material = {
        "symbol": symbol,
        "direction": classifications.direction,
        "structure": classifications.structure,
        "risk_budget": curation.intake.risk_budget,
        "definition": request.definition,
        "rules": compiled_rules,
        "mandatory_safety_facts": _MANDATORY_SAFETY_FACTS,
    }
    definition_hash = _canonical_hash(f"{_HASH_PREFIX}.definition.v1", definition_material)
    compiler_hash = _compiler_hash()

    material = {
        "review_state": request.review_state,
        "compile_status": "COMPILABLE",
        "arm_state": "NOT_ARMED",
        "automation_state": "OFF",
        "execution_eligible": False,
        "paper_trading_only": True,
        "options_required": True,
        "defined_risk_required": True,
        "recipe": "TWO_LEG_DEBIT_VERTICAL",
        "leg_count": 2,
        "net_premium": "DEBIT",
        "symbol": symbol,
        "direction": classifications.direction,
        "structure": classifications.structure,
        "risk_budget": curation.intake.risk_budget,
        "definition": request.definition,
        "rules": compiled_rules,
        "mandatory_safety_facts": _MANDATORY_SAFETY_FACTS,
        "source_hash": source_hash,
        "compiler_hash": compiler_hash,
        "definition_hash": definition_hash,
    }
    return CompiledStrategyProtocol(
        **material,
        protocol_hash=_canonical_hash(f"{_HASH_PREFIX}.compiled.v1", material),
    )


def evaluate_compiled_protocol(
    protocol: CompiledStrategyProtocol,
    observations: ProtocolObservationSet,
) -> ProtocolEvaluation:
    if (
        type(protocol) is not CompiledStrategyProtocol
        or type(observations) is not ProtocolObservationSet
    ):
        raise ProtocolEvaluationBlocked("PROTOCOL_EVALUATION_INPUT_INVALID")
    try:
        protocol = CompiledStrategyProtocol.model_validate(protocol.model_dump(mode="python"))
        observations = ProtocolObservationSet.model_validate(observations.model_dump(mode="python"))
    except ValidationError as exc:
        raise ProtocolEvaluationBlocked("PROTOCOL_EVALUATION_INPUT_INVALID") from exc
    if protocol.protocol_hash != _compiled_protocol_hash(protocol):
        raise ProtocolEvaluationBlocked("PROTOCOL_HASH_MISMATCH")
    required_metrics, required_facts = _required_observations(protocol)
    if not required_metrics.issubset(observations.numeric_values) or not required_facts.issubset(
        observations.facts
    ):
        raise ProtocolEvaluationBlocked("PROTOCOL_OBSERVATION_INCOMPLETE")
    rules = protocol.rules
    return ProtocolEvaluation(
        protocol_hash=str(protocol.protocol_hash),
        safety_gates_passed=all(
            observations.facts[fact] for fact in protocol.mandatory_safety_facts
        ),
        entry_rule_matched=_matches(rules.entry_rule, observations),
        no_trade_rule_matched=_matches(rules.no_trade_rule, observations),
        profit_exit_rule_matched=_matches(rules.profit_exit_rule, observations),
        loss_exit_rule_matched=_matches(rules.loss_exit_rule, observations),
        time_exit_rule_matched=_matches(rules.time_exit_rule, observations),
        invalidation_rule_matches=tuple(
            _matches(rule, observations) for rule in rules.invalidation_rules
        ),
    )


def verify_compiled_protocol(protocol: CompiledStrategyProtocol) -> bool:
    try:
        validated = CompiledStrategyProtocol.model_validate(protocol.model_dump(mode="python"))
    except ValidationError:
        return False
    return validated.protocol_hash == _compiled_protocol_hash(validated)


def _validate_evidence_binding(curation: StrategyCurationResponse) -> None:
    evidence_by_id = {
        f"evidence-{index}": excerpt
        for index, excerpt in enumerate(curation.intake.evidence, start=1)
    }
    supplied_ids = [item.evidence_id for item in curation.supporting_evidence]
    if not supplied_ids or len(set(supplied_ids)) != len(supplied_ids):
        raise ProtocolCompilationBlocked("PROTOCOL_EVIDENCE_BINDING_INVALID")
    if any(
        evidence_by_id.get(item.evidence_id) != item.excerpt
        for item in curation.supporting_evidence
    ):
        raise ProtocolCompilationBlocked("PROTOCOL_EVIDENCE_BINDING_INVALID")


def _validate_rule_binding(
    curation: StrategyCurationResponse,
    rules: ReviewedProtocolRules,
) -> None:
    fields = curation.protocol_fields
    expected = (
        fields.entry_rule,
        fields.no_trade_rule,
        fields.profit_exit_rule,
        fields.loss_exit_rule,
        fields.time_exit_rule,
    )
    actual = (
        rules.entry_rule.source_text,
        rules.no_trade_rule.source_text,
        rules.profit_exit_rule.source_text,
        rules.loss_exit_rule.source_text,
        rules.time_exit_rule.source_text,
    )
    if any(item is None for item in expected) or actual != expected:
        raise ProtocolCompilationBlocked("PROTOCOL_RULE_BINDING_MISMATCH")
    invalidation = tuple(rule.source_text for rule in rules.invalidation_rules)
    if invalidation != fields.invalidation_rules:
        raise ProtocolCompilationBlocked("PROTOCOL_RULE_BINDING_MISMATCH")


def _validate_definition(
    curation: StrategyCurationResponse,
    definition: ReviewedProtocolDefinition,
    rules: ReviewedProtocolRules,
) -> None:
    risk = curation.intake.risk_budget
    if risk is None or risk.max_loss_dollars is None:
        raise ProtocolCompilationBlocked("PROTOCOL_DOLLAR_RISK_REQUIRED")
    if definition.benchmark_symbol == curation.intake.market_scope:
        raise ProtocolCompilationBlocked("PROTOCOL_BENCHMARK_INVALID")
    if definition.selection.maximum_loss_dollars != risk.max_loss_dollars:
        raise ProtocolCompilationBlocked("PROTOCOL_RISK_BINDING_MISMATCH")
    if definition.maximum_account_risk_percent != risk.max_account_percent:
        raise ProtocolCompilationBlocked("PROTOCOL_RISK_BINDING_MISMATCH")
    minimum_dte = (definition.selection.minimum_expiry - definition.schedule.signal_session).days
    maximum_dte = (definition.selection.maximum_expiry - definition.schedule.signal_session).days
    if (
        definition.selection.minimum_expiry < definition.schedule.decision_boundary.date()
        or minimum_dte != definition.selection.minimum_dte
        or maximum_dte != definition.selection.maximum_dte
    ):
        raise ProtocolCompilationBlocked("PROTOCOL_EXPIRY_DTE_MISMATCH")
    if len(definition.invalidation_codes) != len(rules.invalidation_rules):
        raise ProtocolCompilationBlocked("PROTOCOL_INVALIDATION_MAPPING_INCOMPLETE")


def _compile_rules(rules: ReviewedProtocolRules) -> CompiledProtocolRules:
    def compile_rule(rule: MachineRule) -> CompiledMachineRule:
        return CompiledMachineRule(
            source_rule_hash=_canonical_hash(f"{_HASH_PREFIX}.rule-source.v1", rule.source_text),
            match=rule.match,
            predicates=rule.predicates,
        )

    return CompiledProtocolRules(
        entry_rule=compile_rule(rules.entry_rule),
        no_trade_rule=compile_rule(rules.no_trade_rule),
        profit_exit_rule=compile_rule(rules.profit_exit_rule),
        loss_exit_rule=compile_rule(rules.loss_exit_rule),
        time_exit_rule=compile_rule(rules.time_exit_rule),
        invalidation_rules=tuple(compile_rule(rule) for rule in rules.invalidation_rules),
    )


def _required_observations(
    protocol: CompiledStrategyProtocol,
) -> tuple[set[ProtocolMetric], set[ProtocolFact]]:
    metrics: set[ProtocolMetric] = set()
    facts = set(protocol.mandatory_safety_facts)
    rules = protocol.rules
    for rule in (
        rules.entry_rule,
        rules.no_trade_rule,
        rules.profit_exit_rule,
        rules.loss_exit_rule,
        rules.time_exit_rule,
        *rules.invalidation_rules,
    ):
        for predicate in rule.predicates:
            if isinstance(predicate, ProtocolFactPredicate):
                facts.add(predicate.fact)
                continue
            metrics.add(predicate.left.metric)
            if isinstance(predicate.right, MetricOperand):
                metrics.add(predicate.right.metric)
    return metrics, facts


def _matches(rule: CompiledMachineRule, observations: ProtocolObservationSet) -> bool:
    matches = tuple(_predicate_matches(item, observations) for item in rule.predicates)
    return all(matches) if rule.match is RuleMatchMode.ALL else any(matches)


def _predicate_matches(
    predicate: ProtocolPredicate,
    observations: ProtocolObservationSet,
) -> bool:
    if isinstance(predicate, ProtocolFactPredicate):
        return observations.facts[predicate.fact] is predicate.expected
    left = observations.numeric_values[predicate.left.metric]
    right = (
        observations.numeric_values[predicate.right.metric]
        if isinstance(predicate.right, MetricOperand)
        else predicate.right.value
    )
    if predicate.operator is ComparisonOperator.LESS_THAN:
        return left < right
    if predicate.operator is ComparisonOperator.LESS_THAN_OR_EQUAL:
        return left <= right
    if predicate.operator is ComparisonOperator.EQUAL:
        return left == right
    if predicate.operator is ComparisonOperator.GREATER_THAN_OR_EQUAL:
        return left >= right
    return left > right


def _compiled_protocol_hash(protocol: CompiledStrategyProtocol) -> str:
    material = protocol.model_dump(mode="json", exclude={"protocol_hash"})
    return _canonical_hash(f"{_HASH_PREFIX}.compiled.v1", material)


def _compiler_hash() -> str:
    return _canonical_hash(
        f"{_HASH_PREFIX}.compiler.v1",
        {
            "compiler": "REVIEWED_DEBIT_VERTICAL_V1",
            "recipe": "TWO_LEG_DEBIT_VERTICAL",
            "structures": (
                CuratedStructure.BULL_CALL_DEBIT_SPREAD,
                CuratedStructure.BEAR_PUT_DEBIT_SPREAD,
            ),
            "metrics": tuple(ProtocolMetric),
            "facts": tuple(ProtocolFact),
            "operators": tuple(ComparisonOperator),
            "mandatory_safety_facts": _MANDATORY_SAFETY_FACTS,
            "definition_schema": ReviewedProtocolDefinition.model_json_schema(),
            "rule_schema": ReviewedProtocolRules.model_json_schema(),
            "compiled_schema": CompiledStrategyProtocol.model_json_schema(),
        },
    )


def _canonical_hash(domain: str, value: object) -> str:
    value = value.model_dump(mode="json") if isinstance(value, BaseModel) else _json_value(value)
    payload = json.dumps(
        {"domain": domain, "value": value},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _json_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Decimal):
        fixed = format(value, "f")
        if "." in fixed:
            fixed = fixed.rstrip("0").rstrip(".")
        return "0" if fixed in {"-0", "+0"} else fixed
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("PROTOCOL_VALUE_NOT_CANONICAL")
        return value
    if value is None or isinstance(value, str | int | bool):
        return value
    raise ValueError("PROTOCOL_VALUE_NOT_CANONICAL")

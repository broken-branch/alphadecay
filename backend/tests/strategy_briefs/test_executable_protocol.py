from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from backend.app.strategy_briefs.decision import (
    PositionPhase,
    ProtocolDecisionBlocked,
    ProtocolDecisionClassification,
    ProtocolDecisionReason,
    classify_protocol_decision,
)
from backend.app.strategy_briefs.models import (
    StrategyBriefRequest,
    StrategyCurationClassifications,
    StrategyCurationResponse,
    StrategyDirection,
    StrategyProtocolFields,
    SupportingEvidenceExcerpt,
)
from backend.app.strategy_briefs.protocol import (
    ComparisonOperator,
    ConstantOperand,
    DebitVerticalSelection,
    MachineRule,
    MetricOperand,
    ProtocolCompilationBlocked,
    ProtocolEvaluation,
    ProtocolEvaluationBlocked,
    ProtocolFact,
    ProtocolFactPredicate,
    ProtocolMarketQuality,
    ProtocolMetric,
    ProtocolNumericPredicate,
    ProtocolObservationSet,
    ProtocolSchedule,
    ReviewedExecutableProtocolRequest,
    ReviewedProtocolDefinition,
    ReviewedProtocolRules,
    compile_reviewed_protocol,
    evaluate_compiled_protocol,
)


def curation(**overrides: object) -> StrategyCurationResponse:
    values: dict[str, object] = {
        "intake": StrategyBriefRequest.model_validate(
            {
                "source": {
                    "kind": "PASTED_TEXT",
                    "content": (
                        "I think SPY can rise over the next month as earnings breadth improves."
                    ),
                },
                "market_scope": "SPY",
                "direction": "BULLISH",
                "horizon": "Two to six weeks",
                "evidence": ("SPY remains above its 50-day moving average.",),
                "invalidation": ("SPY closes below its 50-day moving average.",),
                "risk_budget": {"max_loss_dollars": "240"},
            }
        ),
        "protocol_fields": StrategyProtocolFields(
            entry_rule="Enter when SPY closes above its 50-day moving average.",
            no_trade_rule="Do not enter when required market data is incomplete.",
            profit_exit_rule="Close at a 50 percent return on maximum risk.",
            loss_exit_rule="Close at a 25 percent loss on maximum risk.",
            time_exit_rule="Close after 10 trading sessions.",
            invalidation_rules=("SPY closes below its 50-day moving average.",),
        ),
        "classifications": StrategyCurationClassifications(
            direction="BULLISH",
            structure="BULL_CALL_DEBIT_SPREAD",
            clarity="READY",
            evidence="READY",
            risk="READY",
            exit="READY",
            confidence="HIGH",
        ),
        "blocking_questions": (),
        "supporting_evidence": (
            SupportingEvidenceExcerpt(
                evidence_id="evidence-1",
                excerpt="SPY remains above its 50-day moving average.",
            ),
        ),
    }
    values.update(overrides)
    return StrategyCurationResponse(**values)


def numeric_rule(
    source_text: str,
    metric: ProtocolMetric,
    value: str,
    operator: ComparisonOperator = ComparisonOperator.GREATER_THAN_OR_EQUAL,
) -> MachineRule:
    return MachineRule(
        source_text=source_text,
        mapping_state="FULLY_MAPPED",
        predicates=(
            ProtocolNumericPredicate(
                left=MetricOperand(metric=metric),
                operator=operator,
                right=ConstantOperand(value=Decimal(value)),
            ),
        ),
    )


def request(curated: StrategyCurationResponse | None = None) -> ReviewedExecutableProtocolRequest:
    curated = curated or curation()
    fields = curated.protocol_fields
    return ReviewedExecutableProtocolRequest(
        curation=curated,
        definition=ReviewedProtocolDefinition(
            opportunity_key="SPY_REVIEWED_EXPERIMENT",
            definition_version=1,
            benchmark_symbol="QQQ",
            allowed_event_codes=("USER_THESIS",),
            thesis_code="BULLISH_TREND_CONFIRMATION",
            invalidation_codes=("SMA_50_LOST",),
            schedule=ProtocolSchedule(
                event_session=date(2026, 9, 1),
                pre_event_session=date(2026, 8, 31),
                reaction_session=date(2026, 9, 1),
                signal_session=date(2026, 9, 2),
                daily_start_session=date(2026, 6, 1),
                evidence_window_start=datetime(2026, 9, 1, 13, 30, tzinfo=UTC),
                evidence_window_end=datetime(2026, 9, 2, 13, 45, tzinfo=UTC),
                entry_window_start=datetime(2026, 9, 2, 13, 45, tzinfo=UTC),
                decision_boundary=datetime(2026, 9, 2, 13, 50, tzinfo=UTC),
                entry_window_end=datetime(2026, 9, 2, 14, 15, tzinfo=UTC),
            ),
            selection=DebitVerticalSelection(
                minimum_expiry=date(2026, 10, 2),
                maximum_expiry=date(2026, 10, 17),
                minimum_dte=30,
                target_dte=38,
                maximum_dte=45,
                minimum_strike="400",
                maximum_strike="800",
                width_dollars="4",
                quantity=1,
                maximum_debit_per_share="2.40",
                maximum_loss_dollars="240",
                maximum_contracts_considered=64,
            ),
            market_quality=ProtocolMarketQuality(
                maximum_underlying_age_seconds=300,
                maximum_option_quote_age_seconds=20,
                maximum_leg_quote_skew_seconds=3,
                maximum_relative_spread_percent="5",
                minimum_leg_bid_size=1,
                minimum_leg_ask_size=1,
            ),
        ),
        rules=ReviewedProtocolRules(
            entry_rule=MachineRule(
                source_text=fields.entry_rule,
                mapping_state="FULLY_MAPPED",
                predicates=(
                    ProtocolNumericPredicate(
                        left=MetricOperand(metric=ProtocolMetric.UNDERLYING_SESSION_CLOSE),
                        operator=ComparisonOperator.GREATER_THAN,
                        right=MetricOperand(metric=ProtocolMetric.UNDERLYING_SMA_50),
                    ),
                ),
            ),
            no_trade_rule=MachineRule(
                source_text=fields.no_trade_rule,
                mapping_state="FULLY_MAPPED",
                predicates=(
                    ProtocolFactPredicate(
                        fact=ProtocolFact.MARKET_DATA_COMPLETE,
                        expected=False,
                    ),
                ),
            ),
            profit_exit_rule=numeric_rule(
                fields.profit_exit_rule,
                ProtocolMetric.POSITION_RETURN_ON_MAX_RISK_PERCENT,
                "50",
            ),
            loss_exit_rule=numeric_rule(
                fields.loss_exit_rule,
                ProtocolMetric.POSITION_RETURN_ON_MAX_RISK_PERCENT,
                "-25",
                ComparisonOperator.LESS_THAN_OR_EQUAL,
            ),
            time_exit_rule=numeric_rule(
                fields.time_exit_rule,
                ProtocolMetric.TRADING_SESSIONS_HELD,
                "10",
            ),
            invalidation_rules=(
                MachineRule(
                    source_text=fields.invalidation_rules[0],
                    mapping_state="FULLY_MAPPED",
                    predicates=(
                        ProtocolNumericPredicate(
                            left=MetricOperand(metric=ProtocolMetric.UNDERLYING_SESSION_CLOSE),
                            operator=ComparisonOperator.LESS_THAN,
                            right=MetricOperand(metric=ProtocolMetric.UNDERLYING_SMA_50),
                        ),
                    ),
                ),
            ),
        ),
    )


def test_reviewed_rules_compile_to_a_deterministic_never_armed_contract() -> None:
    compiled = compile_reviewed_protocol(request())
    repeated = compile_reviewed_protocol(request())

    assert compiled == repeated
    assert compiled.review_state == "REVIEWED"
    assert compiled.compile_status == "COMPILABLE"
    assert compiled.arm_state == "NOT_ARMED"
    assert compiled.automation_state == "OFF"
    assert compiled.execution_eligible is False
    assert compiled.paper_trading_only is True
    assert compiled.options_required is True
    assert compiled.defined_risk_required is True
    assert compiled.recipe == "TWO_LEG_DEBIT_VERTICAL"
    assert compiled.leg_count == 2
    assert compiled.net_premium == "DEBIT"
    assert compiled.symbol == "SPY"
    assert compiled.direction.value == "BULLISH"
    assert compiled.structure.value == "BULL_CALL_DEBIT_SPREAD"
    assert compiled.risk_budget.max_loss_dollars == Decimal("240")
    assert all(
        len(value) == 64
        for value in (
            compiled.source_hash,
            compiled.compiler_hash,
            compiled.definition_hash,
            compiled.protocol_hash,
        )
    )
    assert not hasattr(compiled.rules.entry_rule, "source_text")
    assert compiled.mandatory_safety_facts == (
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


def test_evaluation_matches_rules_but_cannot_emit_trade_authority() -> None:
    compiled = compile_reviewed_protocol(request())
    observations = ProtocolObservationSet(
        numeric_values={
            ProtocolMetric.UNDERLYING_SESSION_CLOSE: Decimal("590"),
            ProtocolMetric.UNDERLYING_SMA_50: Decimal("580"),
            ProtocolMetric.POSITION_RETURN_ON_MAX_RISK_PERCENT: Decimal("52"),
            ProtocolMetric.TRADING_SESSIONS_HELD: Decimal("4"),
        },
        facts={fact: True for fact in compiled.mandatory_safety_facts},
    )

    result = evaluate_compiled_protocol(compiled, observations)

    assert result.status == "EVALUATED"
    assert result.protocol_hash == compiled.protocol_hash
    assert result.safety_gates_passed is True
    assert result.entry_rule_matched is True
    assert result.no_trade_rule_matched is False
    assert result.profit_exit_rule_matched is True
    assert result.loss_exit_rule_matched is False
    assert result.time_exit_rule_matched is False
    assert result.invalidation_rule_matches == (False,)
    assert result.arm_state == "NOT_ARMED"
    assert result.execution_eligible is False


def test_missing_observation_fails_closed_instead_of_assuming_a_value() -> None:
    compiled = compile_reviewed_protocol(request())
    observations = ProtocolObservationSet(
        numeric_values={},
        facts={fact: True for fact in compiled.mandatory_safety_facts},
    )

    with pytest.raises(
        ProtocolEvaluationBlocked, match="PROTOCOL_OBSERVATION_INCOMPLETE"
    ) as caught:
        evaluate_compiled_protocol(compiled, observations)

    assert caught.value.code == "PROTOCOL_OBSERVATION_INCOMPLETE"


@pytest.mark.parametrize(
    ("curated", "code"),
    (
        (
            curation(blocking_questions=("ENTRY_RULE_REQUIRED",)),
            "PROTOCOL_REVIEW_INCOMPLETE",
        ),
        (
            curation(
                classifications=StrategyCurationClassifications(
                    direction="BULLISH",
                    structure="BULL_CALL_DEBIT_SPREAD",
                    clarity="READY",
                    evidence="NEEDS_INPUT",
                    risk="READY",
                    exit="READY",
                    confidence="LOW",
                )
            ),
            "PROTOCOL_REVIEW_INCOMPLETE",
        ),
        (
            curation(
                classifications=StrategyCurationClassifications(
                    direction="UNSURE",
                    structure="REVIEW_REQUIRED",
                    clarity="READY",
                    evidence="READY",
                    risk="READY",
                    exit="READY",
                    confidence="LOW",
                )
            ),
            "PROTOCOL_DIRECTION_UNRESOLVED",
        ),
    ),
)
def test_unresolved_curation_fails_closed(curated: StrategyCurationResponse, code: str) -> None:
    with pytest.raises(ProtocolCompilationBlocked, match=code) as caught:
        compile_reviewed_protocol(request(curated))

    assert caught.value.code == code


def test_typed_rules_must_bind_to_the_exact_reviewed_text() -> None:
    protocol_request = request()
    changed = protocol_request.rules.model_copy(
        update={
            "entry_rule": protocol_request.rules.entry_rule.model_copy(
                update={"source_text": "A machine silently changed this rule."}
            )
        }
    )

    with pytest.raises(
        ProtocolCompilationBlocked, match="PROTOCOL_RULE_BINDING_MISMATCH"
    ) as caught:
        compile_reviewed_protocol(protocol_request.model_copy(update={"rules": changed}))

    assert caught.value.code == "PROTOCOL_RULE_BINDING_MISMATCH"


def test_definition_is_required_and_risk_and_expiry_values_must_bind() -> None:
    protocol_request = request()

    with pytest.raises(ProtocolCompilationBlocked, match="PROTOCOL_DEFINITION_REQUIRED") as missing:
        compile_reviewed_protocol(protocol_request.model_copy(update={"definition": None}))
    assert missing.value.code == "PROTOCOL_DEFINITION_REQUIRED"

    selection = protocol_request.definition.selection.model_copy(
        update={
            "maximum_debit_per_share": Decimal("2"),
            "maximum_loss_dollars": Decimal("200"),
        }
    )
    changed_definition = protocol_request.definition.model_copy(update={"selection": selection})
    with pytest.raises(ProtocolCompilationBlocked, match="PROTOCOL_RISK_BINDING_MISMATCH") as risk:
        compile_reviewed_protocol(
            protocol_request.model_copy(update={"definition": changed_definition})
        )
    assert risk.value.code == "PROTOCOL_RISK_BINDING_MISMATCH"

    selection = protocol_request.definition.selection.model_copy(update={"minimum_dte": 29})
    changed_definition = protocol_request.definition.model_copy(update={"selection": selection})
    with pytest.raises(ProtocolCompilationBlocked, match="PROTOCOL_EXPIRY_DTE_MISMATCH") as expiry:
        compile_reviewed_protocol(
            protocol_request.model_copy(update={"definition": changed_definition})
        )
    assert expiry.value.code == "PROTOCOL_EXPIRY_DTE_MISMATCH"


def test_neutral_iron_condor_is_not_supported_by_the_first_compiler() -> None:
    neutral_brief = curation().intake.model_copy(update={"direction": StrategyDirection.NEUTRAL})
    neutral = curation(
        intake=neutral_brief,
        classifications=StrategyCurationClassifications(
            direction="NEUTRAL",
            structure="IRON_CONDOR",
            clarity="READY",
            evidence="READY",
            risk="READY",
            exit="READY",
            confidence="HIGH",
        ),
    )

    with pytest.raises(ProtocolCompilationBlocked, match="PROTOCOL_RECIPE_UNSUPPORTED") as caught:
        compile_reviewed_protocol(request(neutral))

    assert caught.value.code == "PROTOCOL_RECIPE_UNSUPPORTED"


def test_rule_shape_is_closed_and_requires_at_least_one_predicate() -> None:
    with pytest.raises(ValidationError):
        MachineRule.model_validate(
            {
                "source_text": "A reviewed rule.",
                "predicates": [],
                "generated_explanation": "free-form model prose is forbidden",
            }
        )


def test_schedule_binds_evidence_and_entry_to_the_decision_session() -> None:
    schedule = request().definition.schedule
    late_evidence = schedule.model_copy(
        update={"evidence_window_end": datetime(2026, 9, 2, 13, 55, tzinfo=UTC)}
    )
    next_day_entry = schedule.model_copy(
        update={"entry_window_end": datetime(2026, 9, 3, 14, 15, tzinfo=UTC)}
    )

    with pytest.raises(ValidationError, match="evidence window must close"):
        ProtocolSchedule.model_validate(late_evidence.model_dump())
    with pytest.raises(ValidationError, match="decision boundary must bind"):
        ProtocolSchedule.model_validate(next_day_entry.model_dump())


def protocol_evaluation(compiled, **updates: object):
    values: dict[str, object] = {
        "protocol_hash": compiled.protocol_hash,
        "safety_gates_passed": True,
        "entry_rule_matched": False,
        "no_trade_rule_matched": False,
        "profit_exit_rule_matched": False,
        "loss_exit_rule_matched": False,
        "time_exit_rule_matched": False,
        "invalidation_rule_matches": (False,),
    }
    values.update(updates)
    return ProtocolEvaluation(**values)


def test_flat_protocol_precedence_stands_aside_before_entry() -> None:
    compiled = compile_reviewed_protocol(request())
    evaluation = protocol_evaluation(
        compiled,
        entry_rule_matched=True,
        no_trade_rule_matched=True,
        invalidation_rule_matches=(True,),
    )

    decision = classify_protocol_decision(compiled, evaluation, PositionPhase.FLAT)

    assert decision.classification is ProtocolDecisionClassification.STAND_ASIDE
    assert decision.reason_codes == (
        ProtocolDecisionReason.INVALIDATION_RULE_MATCHED,
        ProtocolDecisionReason.NO_TRADE_RULE_MATCHED,
    )
    assert decision.matched_invalidation_rule_numbers == (1,)


@pytest.mark.parametrize(
    ("entry_matched", "classification", "reason"),
    (
        (
            True,
            ProtocolDecisionClassification.ENTRY_CANDIDATE,
            ProtocolDecisionReason.ENTRY_RULE_MATCHED,
        ),
        (
            False,
            ProtocolDecisionClassification.STAND_ASIDE,
            ProtocolDecisionReason.ENTRY_RULE_NOT_MATCHED,
        ),
    ),
)
def test_flat_protocol_classifies_entry_or_stands_aside(
    entry_matched: bool,
    classification: ProtocolDecisionClassification,
    reason: ProtocolDecisionReason,
) -> None:
    compiled = compile_reviewed_protocol(request())

    decision = classify_protocol_decision(
        compiled,
        protocol_evaluation(compiled, entry_rule_matched=entry_matched),
        PositionPhase.FLAT,
    )

    assert decision.classification is classification
    assert decision.reason_codes == (reason,)
    assert decision.authority_state == "NON_AUTHORITATIVE"
    assert decision.arm_state == "NOT_ARMED"
    assert decision.automation_state == "OFF"
    assert decision.execution_eligible is False
    assert not hasattr(decision, "action")
    assert not hasattr(decision, "order")
    assert not hasattr(decision, "permit")


def test_open_protocol_preserves_all_simultaneous_exit_reasons_in_precedence_order() -> None:
    compiled = compile_reviewed_protocol(request())
    evaluation = protocol_evaluation(
        compiled,
        invalidation_rule_matches=(True,),
        loss_exit_rule_matched=True,
        time_exit_rule_matched=True,
        profit_exit_rule_matched=True,
    )

    decision = classify_protocol_decision(compiled, evaluation, PositionPhase.OPEN)

    assert decision.classification is ProtocolDecisionClassification.CLOSE_CANDIDATE
    assert decision.reason_codes == (
        ProtocolDecisionReason.INVALIDATION_RULE_MATCHED,
        ProtocolDecisionReason.LOSS_EXIT_RULE_MATCHED,
        ProtocolDecisionReason.TIME_EXIT_RULE_MATCHED,
        ProtocolDecisionReason.PROFIT_EXIT_RULE_MATCHED,
    )
    assert decision.matched_invalidation_rule_numbers == (1,)


@pytest.mark.parametrize("safety_gates_passed", (True, False))
def test_open_protocol_holds_without_an_exit_match(
    safety_gates_passed: bool,
) -> None:
    compiled = compile_reviewed_protocol(request())

    decision = classify_protocol_decision(
        compiled,
        protocol_evaluation(compiled, safety_gates_passed=safety_gates_passed),
        PositionPhase.OPEN,
    )

    assert decision.classification is ProtocolDecisionClassification.HOLD
    assert decision.reason_codes == (ProtocolDecisionReason.NO_EXIT_RULE_MATCHED,)


def test_failed_entry_safety_gate_blocks_flat_entry() -> None:
    compiled = compile_reviewed_protocol(request())
    evaluation = protocol_evaluation(
        compiled,
        safety_gates_passed=False,
        entry_rule_matched=True,
        invalidation_rule_matches=(True,),
        loss_exit_rule_matched=True,
    )

    decision = classify_protocol_decision(compiled, evaluation, PositionPhase.FLAT)

    assert decision.classification is ProtocolDecisionClassification.BLOCKED
    assert decision.reason_codes == (ProtocolDecisionReason.MANDATORY_SAFETY_GATE_FAILED,)
    assert decision.matched_invalidation_rule_numbers == ()


def test_failed_entry_safety_gate_does_not_suppress_open_position_close_reasons() -> None:
    compiled = compile_reviewed_protocol(request())
    evaluation = protocol_evaluation(
        compiled,
        safety_gates_passed=False,
        invalidation_rule_matches=(True,),
        loss_exit_rule_matched=True,
        time_exit_rule_matched=True,
        profit_exit_rule_matched=True,
    )

    decision = classify_protocol_decision(compiled, evaluation, PositionPhase.OPEN)

    assert decision.classification is ProtocolDecisionClassification.CLOSE_CANDIDATE
    assert decision.reason_codes == (
        ProtocolDecisionReason.INVALIDATION_RULE_MATCHED,
        ProtocolDecisionReason.LOSS_EXIT_RULE_MATCHED,
        ProtocolDecisionReason.TIME_EXIT_RULE_MATCHED,
        ProtocolDecisionReason.PROFIT_EXIT_RULE_MATCHED,
    )
    assert decision.matched_invalidation_rule_numbers == (1,)
    assert decision.authority_state == "NON_AUTHORITATIVE"
    assert decision.arm_state == "NOT_ARMED"
    assert decision.automation_state == "OFF"
    assert decision.execution_eligible is False


@pytest.mark.parametrize(
    ("protocol_change", "evaluation_change", "code"),
    (
        ({"protocol_hash": "a" * 64}, {}, "PROTOCOL_HASH_MISMATCH"),
        ({}, {"protocol_hash": "b" * 64}, "PROTOCOL_EVALUATION_MISMATCH"),
        (
            {},
            {"invalidation_rule_matches": (False, False)},
            "PROTOCOL_EVALUATION_MISMATCH",
        ),
    ),
)
def test_protocol_or_evaluation_mismatch_fails_closed(
    protocol_change: dict[str, object],
    evaluation_change: dict[str, object],
    code: str,
) -> None:
    compiled = compile_reviewed_protocol(request())
    changed_protocol = compiled.model_copy(update=protocol_change)
    evaluation = protocol_evaluation(compiled).model_copy(update=evaluation_change)

    with pytest.raises(ProtocolDecisionBlocked, match=code) as caught:
        classify_protocol_decision(changed_protocol, evaluation, PositionPhase.FLAT)

    assert caught.value.code == code

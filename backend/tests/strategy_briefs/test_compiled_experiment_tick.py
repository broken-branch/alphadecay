from datetime import UTC, datetime
from decimal import Decimal

import pytest

from backend.app.strategy_briefs.decision import (
    PositionPhase,
    ProtocolDecisionClassification,
    ProtocolDecisionReason,
)
from backend.app.strategy_briefs.protocol import (
    ComparisonOperator,
    ProtocolFact,
    ProtocolMetric,
    compile_reviewed_protocol,
)
from backend.app.strategy_briefs.tick import (
    ProtocolTickBlocked,
    run_compiled_experiment_tick,
)
from backend.tests.strategy_briefs.test_executable_protocol import numeric_rule, request
from backend.tests.strategy_briefs.test_protocol_observations import evidence


def protocol_with_rules(**updates):
    base = request()
    return compile_reviewed_protocol(
        base.model_copy(update={"rules": base.rules.model_copy(update=updates)})
    )


def test_tick_returns_qualifying_entry_candidate() -> None:
    protocol = compile_reviewed_protocol(request())

    tick = run_compiled_experiment_tick(
        protocol, evidence(protocol, PositionPhase.FLAT), PositionPhase.FLAT
    )

    assert tick.decision.classification is ProtocolDecisionClassification.ENTRY_CANDIDATE
    assert tick.decision.reason_codes == (ProtocolDecisionReason.ENTRY_RULE_MATCHED,)
    assert tick.authority_state == "NON_AUTHORITATIVE"
    assert tick.arm_state == "NOT_ARMED"
    assert tick.automation_state == "OFF"
    assert tick.execution_eligible is False


def test_tick_stands_aside_when_no_trade_rule_matches() -> None:
    base = request()
    protocol = protocol_with_rules(
        no_trade_rule=numeric_rule(
            base.curation.protocol_fields.no_trade_rule,
            ProtocolMetric.UNDERLYING_SESSION_CLOSE,
            "400",
        )
    )

    tick = run_compiled_experiment_tick(
        protocol, evidence(protocol, PositionPhase.FLAT), PositionPhase.FLAT
    )

    assert tick.decision.classification is ProtocolDecisionClassification.STAND_ASIDE
    assert tick.decision.reason_codes == (ProtocolDecisionReason.NO_TRADE_RULE_MATCHED,)


def test_tick_blocks_flat_entry_when_mandatory_safety_fails() -> None:
    protocol = compile_reviewed_protocol(request())
    acquired = evidence(protocol, PositionPhase.FLAT)
    facts = {**acquired.fact_values, ProtocolFact.PAPER_ACCOUNT_CONFIRMED: False}
    acquired = acquired.model_copy(update={"fact_values": facts})

    tick = run_compiled_experiment_tick(protocol, acquired, PositionPhase.FLAT)

    assert tick.decision.classification is ProtocolDecisionClassification.BLOCKED


def test_tick_holds_open_position_without_exit_match() -> None:
    protocol = compile_reviewed_protocol(request())
    acquired = evidence(protocol, PositionPhase.OPEN)
    numeric = {
        **acquired.numeric_values,
        ProtocolMetric.POSITION_RETURN_ON_MAX_RISK_PERCENT: Decimal("0"),
        ProtocolMetric.TRADING_SESSIONS_HELD: Decimal("3"),
    }

    tick = run_compiled_experiment_tick(
        protocol,
        acquired.model_copy(update={"numeric_values": numeric}),
        PositionPhase.OPEN,
    )

    assert tick.decision.classification is ProtocolDecisionClassification.HOLD


def test_tick_preserves_all_simultaneous_open_close_reasons() -> None:
    base = request()
    protocol = protocol_with_rules(
        loss_exit_rule=numeric_rule(
            base.curation.protocol_fields.loss_exit_rule,
            ProtocolMetric.POSITION_RETURN_ON_MAX_RISK_PERCENT,
            "100",
            ComparisonOperator.LESS_THAN_OR_EQUAL,
        )
    )
    acquired = evidence(protocol, PositionPhase.OPEN)
    numeric = {
        **acquired.numeric_values,
        ProtocolMetric.UNDERLYING_SESSION_CLOSE: Decimal("480"),
        ProtocolMetric.POSITION_RETURN_ON_MAX_RISK_PERCENT: Decimal("60"),
        ProtocolMetric.TRADING_SESSIONS_HELD: Decimal("10"),
    }

    tick = run_compiled_experiment_tick(
        protocol,
        acquired.model_copy(update={"numeric_values": numeric}),
        PositionPhase.OPEN,
    )

    assert tick.decision.reason_codes == (
        ProtocolDecisionReason.INVALIDATION_RULE_MATCHED,
        ProtocolDecisionReason.LOSS_EXIT_RULE_MATCHED,
        ProtocolDecisionReason.TIME_EXIT_RULE_MATCHED,
        ProtocolDecisionReason.PROFIT_EXIT_RULE_MATCHED,
    )


@pytest.mark.parametrize(
    ("change", "code"),
    (
        ({"protocol_source_hash": "a" * 64}, "PROTOCOL_SOURCE_MISMATCH"),
        ({"protocol_hash": "b" * 64}, "PROTOCOL_SOURCE_MISMATCH"),
        ({"observed_at": datetime(2026, 9, 2, 0, tzinfo=UTC)}, "PROTOCOL_SESSION_MISMATCH"),
        ({"numeric_values": {}}, "PROTOCOL_METRIC_MISSING"),
    ),
)
def test_tick_preserves_stage_failure_codes(change: dict[str, object], code: str) -> None:
    protocol = compile_reviewed_protocol(request())
    acquired = evidence(protocol, PositionPhase.FLAT).model_copy(update=change)

    with pytest.raises(ProtocolTickBlocked, match=code) as caught:
        run_compiled_experiment_tick(protocol, acquired, PositionPhase.FLAT)

    assert caught.value.code == code

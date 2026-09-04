from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from backend.app.strategy_briefs.decision import PositionPhase
from backend.app.strategy_briefs.observations import (
    AcquiredProtocolEvidence,
    ProtocolObservationBlocked,
    build_protocol_observations,
)
from backend.app.strategy_briefs.protocol import (
    ComparisonOperator,
    ProtocolFact,
    ProtocolMetric,
    compile_reviewed_protocol,
    evaluate_compiled_protocol,
)
from backend.tests.strategy_briefs.test_executable_protocol import numeric_rule, request

NOW = datetime(2026, 9, 2, 14, tzinfo=UTC)
SOURCE = "e" * 64


def compiled_protocol():
    return compile_reviewed_protocol(request())


def evidence(protocol, phase: PositionPhase, **updates: object) -> AcquiredProtocolEvidence:
    numeric = {
        ProtocolMetric.UNDERLYING_SESSION_CLOSE: Decimal("500"),
        ProtocolMetric.UNDERLYING_SMA_50: Decimal("490"),
        ProtocolMetric.POSITION_RETURN_ON_MAX_RISK_PERCENT: Decimal("-30"),
        ProtocolMetric.TRADING_SESSIONS_HELD: Decimal("3"),
        ProtocolMetric.OPTION_QUOTE_AGE_SECONDS: Decimal("1"),
    }
    facts = {fact: True for fact in protocol.mandatory_safety_facts}
    facts[ProtocolFact.MARKET_DATA_COMPLETE] = True
    values: dict[str, object] = {
        "protocol_hash": protocol.protocol_hash,
        "protocol_source_hash": protocol.source_hash,
        "symbol": protocol.symbol,
        "observed_at": NOW,
        "session_date": NOW.date(),
        "numeric_values": numeric,
        "fact_values": facts,
        "metric_source_hashes": {metric: SOURCE for metric in numeric},
        "fact_source_hashes": {fact: SOURCE for fact in facts},
    }
    values.update(updates)
    return AcquiredProtocolEvidence(**values)


def test_complete_flat_observations_include_mandatory_entry_facts() -> None:
    protocol = compiled_protocol()
    bundle = build_protocol_observations(
        protocol,
        evidence(protocol, PositionPhase.FLAT),
        PositionPhase.FLAT,
    )

    assert set(protocol.mandatory_safety_facts).issubset(bundle.observations.facts)
    evaluation = evaluate_compiled_protocol(protocol, bundle.observations)
    assert evaluation.protocol_hash == protocol.protocol_hash
    assert bundle.phase_ignored_entry_facts == ()
    assert bundle.execution_eligible is False


def test_complete_open_observations_ignore_entry_only_facts() -> None:
    protocol = compiled_protocol()
    supplied = evidence(protocol, PositionPhase.OPEN)
    facts = {
        key: value
        for key, value in supplied.fact_values.items()
        if key
        not in {
            ProtocolFact.ACCOUNT_FLAT,
            ProtocolFact.NO_OPEN_ORDER,
            ProtocolFact.BUYING_POWER_SUFFICIENT,
            ProtocolFact.NO_PRIOR_ENTRY_ATTEMPT,
        }
    }
    opened = supplied.model_copy(
        update={
            "fact_values": facts,
            "fact_source_hashes": {key: SOURCE for key in facts},
        }
    )

    bundle = build_protocol_observations(protocol, opened, PositionPhase.OPEN)

    assert ProtocolFact.ACCOUNT_FLAT in bundle.phase_ignored_entry_facts
    assert bundle.observations.facts[ProtocolFact.ACCOUNT_FLAT] is False
    assert evaluate_compiled_protocol(protocol, bundle.observations).safety_gates_passed is False
    assert bundle.arm_state == "NOT_ARMED"


@pytest.mark.parametrize(
    ("change", "code"),
    (
        ({"numeric_values": {}}, "PROTOCOL_METRIC_MISSING"),
        ({"fact_values": {}}, "PROTOCOL_FACT_MISSING"),
        ({"symbol": "QQQ"}, "PROTOCOL_SYMBOL_MISMATCH"),
        ({"protocol_hash": "a" * 64}, "PROTOCOL_SOURCE_MISMATCH"),
    ),
)
def test_missing_or_mismatched_evidence_fails_closed(change: dict[str, object], code: str) -> None:
    protocol = compiled_protocol()
    changed = evidence(protocol, PositionPhase.FLAT).model_copy(update=change)

    with pytest.raises(ProtocolObservationBlocked, match=code):
        build_protocol_observations(protocol, changed, PositionPhase.FLAT)


@pytest.mark.parametrize(
    "observed_at",
    (
        datetime(2026, 9, 2, 0, tzinfo=UTC),
        datetime(2026, 9, 2, 14, 15, tzinfo=UTC),
        datetime(2026, 9, 2, 15, tzinfo=UTC),
    ),
)
def test_flat_observation_must_be_inside_exact_entry_window(
    observed_at: datetime,
) -> None:
    protocol = compiled_protocol()
    acquired = evidence(protocol, PositionPhase.FLAT).model_copy(
        update={"observed_at": observed_at, "session_date": observed_at.date()}
    )

    with pytest.raises(ProtocolObservationBlocked, match="PROTOCOL_SESSION_MISMATCH"):
        build_protocol_observations(protocol, acquired, PositionPhase.FLAT)


@pytest.mark.parametrize(
    ("quotes", "code"),
    (
        ((NOW - timedelta(seconds=21),), "PROTOCOL_QUOTE_STALE"),
        ((NOW - timedelta(seconds=1), NOW - timedelta(seconds=5)), "PROTOCOL_QUOTE_SKEWED"),
    ),
)
def test_stale_or_skewed_quotes_fail_closed(quotes: tuple[datetime, ...], code: str) -> None:
    base = request()
    rules = base.rules.model_copy(
        update={
            "entry_rule": numeric_rule(
                base.curation.protocol_fields.entry_rule,
                ProtocolMetric.OPTION_QUOTE_AGE_SECONDS,
                "20",
                ComparisonOperator.LESS_THAN_OR_EQUAL,
            )
        }
    )
    protocol = compile_reviewed_protocol(base.model_copy(update={"rules": rules}))
    acquired = evidence(protocol, PositionPhase.FLAT, option_quote_observed_at=quotes)

    with pytest.raises(ProtocolObservationBlocked, match=code):
        build_protocol_observations(protocol, acquired, PositionPhase.FLAT)

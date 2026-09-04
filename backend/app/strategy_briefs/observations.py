from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, ValidationError

from .decision import PositionPhase
from .protocol import (
    CompiledMachineRule,
    CompiledStrategyProtocol,
    ProtocolFact,
    ProtocolFactPredicate,
    ProtocolMetric,
    ProtocolModel,
    ProtocolNumericPredicate,
    ProtocolObservationSet,
    verify_compiled_protocol,
)

_ENTRY_ONLY_FACTS = frozenset(
    {
        ProtocolFact.ACCOUNT_FLAT,
        ProtocolFact.NO_OPEN_ORDER,
        ProtocolFact.BUYING_POWER_SUFFICIENT,
        ProtocolFact.NO_PRIOR_ENTRY_ATTEMPT,
    }
)


class ProtocolObservationBlocked(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AcquiredProtocolEvidence(ProtocolModel):
    protocol_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    symbol: str = Field(pattern=r"^[A-Z]{1,6}$")
    observed_at: datetime
    session_date: date
    numeric_values: dict[ProtocolMetric, Decimal]
    fact_values: dict[ProtocolFact, bool]
    metric_source_hashes: dict[ProtocolMetric, str]
    fact_source_hashes: dict[ProtocolFact, str]
    option_quote_observed_at: tuple[datetime, ...] = Field(default=(), max_length=4)


class ProtocolObservationBundle(ProtocolModel):
    status: Literal["OBSERVATIONS_READY"] = "OBSERVATIONS_READY"
    authority_state: Literal["NON_AUTHORITATIVE"] = "NON_AUTHORITATIVE"
    arm_state: Literal["NOT_ARMED"] = "NOT_ARMED"
    automation_state: Literal["OFF"] = "OFF"
    execution_eligible: Literal[False] = False
    protocol_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    position_phase: PositionPhase
    observations: ProtocolObservationSet
    required_metrics: tuple[ProtocolMetric, ...]
    required_facts: tuple[ProtocolFact, ...]
    phase_ignored_entry_facts: tuple[ProtocolFact, ...]
    metric_source_hashes: dict[ProtocolMetric, str]
    fact_source_hashes: dict[ProtocolFact, str]
    source_hashes: tuple[str, ...]


def build_protocol_observations(
    protocol: CompiledStrategyProtocol,
    evidence: AcquiredProtocolEvidence,
    position_phase: PositionPhase,
) -> ProtocolObservationBundle:
    if (
        type(protocol) is not CompiledStrategyProtocol
        or type(evidence) is not AcquiredProtocolEvidence
        or type(position_phase) is not PositionPhase
    ):
        raise ProtocolObservationBlocked("PROTOCOL_OBSERVATION_INPUT_INVALID")
    try:
        protocol = CompiledStrategyProtocol.model_validate(protocol.model_dump(mode="python"))
        evidence = AcquiredProtocolEvidence.model_validate(evidence.model_dump(mode="python"))
    except ValidationError as exc:
        raise ProtocolObservationBlocked("PROTOCOL_OBSERVATION_INPUT_INVALID") from exc
    if not verify_compiled_protocol(protocol):
        raise ProtocolObservationBlocked("PROTOCOL_HASH_MISMATCH")
    if (
        evidence.protocol_hash != protocol.protocol_hash
        or evidence.protocol_source_hash != protocol.source_hash
    ):
        raise ProtocolObservationBlocked("PROTOCOL_SOURCE_MISMATCH")
    if evidence.symbol != protocol.symbol:
        raise ProtocolObservationBlocked("PROTOCOL_SYMBOL_MISMATCH")
    observed_at = _utc(evidence.observed_at)
    schedule = protocol.definition.schedule
    if (
        evidence.session_date != observed_at.date()
        or (
            position_phase is PositionPhase.FLAT
            and (
                evidence.session_date != schedule.signal_session
                or not schedule.entry_window_start <= observed_at < schedule.entry_window_end
            )
        )
        or (
            position_phase is PositionPhase.OPEN and evidence.session_date < schedule.signal_session
        )
    ):
        raise ProtocolObservationBlocked("PROTOCOL_SESSION_MISMATCH")

    required_metrics, predicate_facts = _required_predicates(protocol)
    ignored = (
        tuple(sorted(_ENTRY_ONLY_FACTS, key=str)) if position_phase is PositionPhase.OPEN else ()
    )
    required_facts = set(predicate_facts)
    if position_phase is PositionPhase.FLAT:
        required_facts.update(protocol.mandatory_safety_facts)
    else:
        required_facts.update(set(protocol.mandatory_safety_facts) - _ENTRY_ONLY_FACTS)
    required_metric_tuple = tuple(sorted(required_metrics, key=str))
    required_fact_tuple = tuple(sorted(required_facts, key=str))
    if not required_metrics.issubset(evidence.numeric_values):
        raise ProtocolObservationBlocked("PROTOCOL_METRIC_MISSING")
    if not required_facts.issubset(evidence.fact_values):
        raise ProtocolObservationBlocked("PROTOCOL_FACT_MISSING")
    if not required_metrics.issubset(evidence.metric_source_hashes) or not required_facts.issubset(
        evidence.fact_source_hashes
    ):
        raise ProtocolObservationBlocked("PROTOCOL_PROVENANCE_MISSING")
    values = {metric: evidence.numeric_values[metric] for metric in required_metric_tuple}
    if any(not value.is_finite() for value in values.values()):
        raise ProtocolObservationBlocked("PROTOCOL_NUMERIC_INVALID")
    facts = {fact: evidence.fact_values[fact] for fact in required_fact_tuple}
    for fact in ignored:
        facts.setdefault(fact, False)
    _validate_quotes(protocol, evidence, observed_at, required_metrics)
    source_hashes = tuple(
        sorted(
            {
                *(evidence.metric_source_hashes[item] for item in required_metric_tuple),
                *(evidence.fact_source_hashes[item] for item in required_fact_tuple),
            }
        )
    )
    if any(
        len(item) != 64 or any(char not in "0123456789abcdef" for char in item)
        for item in source_hashes
    ):
        raise ProtocolObservationBlocked("PROTOCOL_PROVENANCE_INVALID")
    return ProtocolObservationBundle(
        protocol_hash=protocol.protocol_hash,
        position_phase=position_phase,
        observations=ProtocolObservationSet(numeric_values=values, facts=facts),
        required_metrics=required_metric_tuple,
        required_facts=required_fact_tuple,
        phase_ignored_entry_facts=ignored,
        metric_source_hashes={
            item: evidence.metric_source_hashes[item] for item in required_metric_tuple
        },
        fact_source_hashes={
            item: evidence.fact_source_hashes[item] for item in required_fact_tuple
        },
        source_hashes=source_hashes,
    )


def _required_predicates(
    protocol: CompiledStrategyProtocol,
) -> tuple[set[ProtocolMetric], set[ProtocolFact]]:
    rules: tuple[CompiledMachineRule, ...] = (
        protocol.rules.entry_rule,
        protocol.rules.no_trade_rule,
        protocol.rules.loss_exit_rule,
        protocol.rules.time_exit_rule,
        protocol.rules.profit_exit_rule,
        *protocol.rules.invalidation_rules,
    )
    metrics: set[ProtocolMetric] = set()
    facts: set[ProtocolFact] = set()
    for rule in rules:
        for predicate in rule.predicates:
            if isinstance(predicate, ProtocolNumericPredicate):
                metrics.add(predicate.left.metric)
                if predicate.right.kind == "METRIC":
                    metrics.add(predicate.right.metric)
            elif isinstance(predicate, ProtocolFactPredicate):
                facts.add(predicate.fact)
    return metrics, facts


def _validate_quotes(
    protocol: CompiledStrategyProtocol,
    evidence: AcquiredProtocolEvidence,
    observed_at: datetime,
    metrics: set[ProtocolMetric],
) -> None:
    quote_metrics = {
        ProtocolMetric.OPTION_QUOTE_AGE_SECONDS,
        ProtocolMetric.OPTION_RELATIVE_SPREAD_PERCENT,
    }
    if not metrics.intersection(quote_metrics):
        return
    if not evidence.option_quote_observed_at:
        raise ProtocolObservationBlocked("PROTOCOL_QUOTE_EVIDENCE_MISSING")
    quotes = tuple(_utc(value) for value in evidence.option_quote_observed_at)
    maximum_age = protocol.definition.market_quality.maximum_option_quote_age_seconds
    if any(
        value > observed_at or (observed_at - value).total_seconds() > maximum_age
        for value in quotes
    ):
        raise ProtocolObservationBlocked("PROTOCOL_QUOTE_STALE")
    skew = (max(quotes) - min(quotes)).total_seconds()
    if skew > protocol.definition.market_quality.maximum_leg_quote_skew_seconds:
        raise ProtocolObservationBlocked("PROTOCOL_QUOTE_SKEWED")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProtocolObservationBlocked("PROTOCOL_TIMESTAMP_INVALID")
    return value.astimezone(UTC)

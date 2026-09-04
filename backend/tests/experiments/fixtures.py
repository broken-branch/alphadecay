from __future__ import annotations

from datetime import UTC, date, datetime

from backend.app.experiments.models import (
    CompileExperimentRequest,
    ExperimentAuthorizationRequest,
    ReviewedExperimentCreateRequest,
)


def reviewed_request() -> ReviewedExperimentCreateRequest:
    thesis = {
        "source": {
            "kind": "PASTED_TEXT",
            "content": "SPY may continue higher while breadth remains constructive.",
        },
        "market_scope": "SPY",
        "direction": "BULLISH",
        "horizon": "two to six weeks",
        "evidence": ["Breadth has improved."],
        "invalidation": ["Breadth breaks down."],
        "risk_budget": {"max_loss_dollars": "240"},
    }
    protocol = {
        "entry_rule": "Enter only after the reviewed signal.",
        "no_trade_rule": "Do not enter when evidence is stale.",
        "profit_exit_rule": "Take profit at the reviewed target.",
        "loss_exit_rule": "Exit at the reviewed loss limit.",
        "time_exit_rule": "Exit before expiration week.",
        "invalidation_rules": ["Breadth breaks down."],
    }
    return ReviewedExperimentCreateRequest.model_validate(
        {
            "original_thesis": thesis,
            "reviewed_protocol": protocol,
            "curation": {
                "intake": thesis,
                "protocol_fields": protocol,
                "classifications": {
                    "direction": "BULLISH",
                    "structure": "BULL_CALL_DEBIT_SPREAD",
                    "clarity": "READY",
                    "evidence": "READY",
                    "risk": "READY",
                    "exit": "READY",
                    "confidence": "HIGH",
                },
                "blocking_questions": [],
                "supporting_evidence": [
                    {"evidence_id": "evidence-1", "excerpt": "Breadth has improved."}
                ],
            },
        }
    )


def compile_request(source_definition_hash: str) -> CompileExperimentRequest:
    reviewed = reviewed_request()

    def numeric_rule(source_text: str, metric: str, operator: str, value: str):
        return {
            "source_text": source_text,
            "mapping_state": "FULLY_MAPPED",
            "predicates": [
                {
                    "kind": "NUMERIC",
                    "left": {"kind": "METRIC", "metric": metric},
                    "operator": operator,
                    "right": {"kind": "CONSTANT", "value": value},
                }
            ],
        }

    protocol = reviewed.reviewed_protocol
    return CompileExperimentRequest.model_validate(
        {
            "source_definition_hash": source_definition_hash,
            "definition": {
                "opportunity_key": "SPY_REVIEWED_EXPERIMENT",
                "definition_version": 1,
                "benchmark_symbol": "QQQ",
                "allowed_event_codes": ["USER_THESIS"],
                "thesis_code": "BULLISH_CONFIRMATION",
                "invalidation_codes": ["BREADTH_BREAKDOWN"],
                "schedule": {
                    "event_session": date(2026, 9, 1),
                    "pre_event_session": date(2026, 8, 31),
                    "reaction_session": date(2026, 9, 1),
                    "signal_session": date(2026, 9, 2),
                    "daily_start_session": date(2026, 6, 1),
                    "evidence_window_start": datetime(2026, 9, 1, 13, 30, tzinfo=UTC),
                    "evidence_window_end": datetime(2026, 9, 2, 13, 45, tzinfo=UTC),
                    "entry_window_start": datetime(2026, 9, 2, 13, 45, tzinfo=UTC),
                    "decision_boundary": datetime(2026, 9, 2, 13, 50, tzinfo=UTC),
                    "entry_window_end": datetime(2026, 9, 2, 14, 15, tzinfo=UTC),
                },
                "selection": {
                    "minimum_expiry": date(2026, 10, 2),
                    "maximum_expiry": date(2026, 10, 17),
                    "minimum_dte": 30,
                    "target_dte": 38,
                    "maximum_dte": 45,
                    "minimum_strike": "400",
                    "maximum_strike": "800",
                    "width_dollars": "4",
                    "quantity": 1,
                    "maximum_debit_per_share": "2.40",
                    "maximum_loss_dollars": "240",
                    "maximum_contracts_considered": 64,
                },
                "market_quality": {
                    "maximum_underlying_age_seconds": 300,
                    "maximum_option_quote_age_seconds": 20,
                    "maximum_leg_quote_skew_seconds": 3,
                    "maximum_relative_spread_percent": "5",
                    "minimum_leg_bid_size": 1,
                    "minimum_leg_ask_size": 1,
                },
            },
            "rules": {
                "entry_rule": numeric_rule(
                    protocol.entry_rule,
                    "UNDERLYING_SESSION_CLOSE",
                    "GREATER_THAN",
                    "0",
                ),
                "no_trade_rule": {
                    "source_text": protocol.no_trade_rule,
                    "mapping_state": "FULLY_MAPPED",
                    "predicates": [
                        {"kind": "FACT", "fact": "MARKET_DATA_COMPLETE", "expected": False}
                    ],
                },
                "profit_exit_rule": numeric_rule(
                    protocol.profit_exit_rule,
                    "POSITION_RETURN_ON_MAX_RISK_PERCENT",
                    "GREATER_THAN_OR_EQUAL",
                    "50",
                ),
                "loss_exit_rule": numeric_rule(
                    protocol.loss_exit_rule,
                    "POSITION_RETURN_ON_MAX_RISK_PERCENT",
                    "LESS_THAN_OR_EQUAL",
                    "-25",
                ),
                "time_exit_rule": numeric_rule(
                    protocol.time_exit_rule,
                    "TRADING_SESSIONS_HELD",
                    "GREATER_THAN_OR_EQUAL",
                    "10",
                ),
                "invalidation_rules": [
                    numeric_rule(
                        protocol.invalidation_rules[0],
                        "UNDERLYING_RETURN_PERCENT",
                        "LESS_THAN",
                        "-2",
                    )
                ],
            },
        }
    )


def authorization_request(
    source_definition_hash: str,
    protocol_hash: str,
    expected_revision: int,
) -> ExperimentAuthorizationRequest:
    return ExperimentAuthorizationRequest(
        source_definition_hash=source_definition_hash,
        protocol_hash=protocol_hash,
        expected_revision=expected_revision,
    )

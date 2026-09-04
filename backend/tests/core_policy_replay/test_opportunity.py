from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from backend.app.contracts.v1 import AccountRole, DataQuality, OptionRight, PositionIntent
from backend.app.order_limits import MAX_STRUCTURAL_OPTION_QUANTITY
from backend.app.policy import (
    AccountOpportunityState,
    CatalystQuality,
    InstrumentKind,
    OpportunityInput,
    OpportunityOutcome,
    OpportunityPolicy,
    OpportunityReason,
    OptionFeed,
    OptionLeg,
    VerticalCandidate,
    VerticalStrategy,
    evaluate_opportunity,
)
from backend.app.policy.opportunity import TradingHaltState, opportunity_policy_hash

BOUNDARY = datetime(2037, 4, 15, 16, 0, tzinfo=UTC)


def policy(**changes: object) -> OpportunityPolicy:
    values: dict[str, object] = {
        "version": "opportunity-v1",
        "opportunity_key": "ACME_EVENT",
        "underlying": "ACME",
        "selected_decision_boundary": BOUNDARY,
        "last_entry_boundary": BOUNDARY + timedelta(days=2),
        "maximum_decision_delay": timedelta(seconds=43),
        "maximum_underlying_age": timedelta(seconds=43),
        "maximum_catalyst_age": timedelta(minutes=17),
        "maximum_option_quote_age": timedelta(seconds=43),
        "maximum_leg_quote_skew": timedelta(seconds=29),
        "minimum_vwap_distance": Decimal("0.0037"),
        "maximum_vwap_distance": Decimal("0.031"),
        "minimum_relative_return": Decimal("0.0065"),
        "minimum_beta": Decimal("0"),
        "maximum_beta": Decimal("4"),
        "required_trend_hits": 3,
        "maximum_first_reaction": Decimal("0.19"),
        "minimum_catalyst_score": 17,
        "minimum_candidate_score": 73,
        "minimum_dte": 19,
        "maximum_dte": 41,
        "maximum_relative_spread": Decimal("0.07"),
        "minimum_debit_width_fraction": Decimal("0.17"),
        "maximum_debit_width_fraction": Decimal("0.63"),
        "minimum_credit_width_fraction": Decimal("0.18"),
        "maximum_position_loss": Decimal("960"),
        "maximum_equity_risk_fraction": Decimal("0.0096"),
        "maximum_lifetime_entries": 4,
        "maximum_lifetime_risk": Decimal("2400"),
        "equity_floor": Decimal("54321"),
        "maximum_quantity": 9,
    }
    values.update(changes)
    return OpportunityPolicy(**values)


def leg(
    *,
    symbol: str,
    strike: str,
    intent: PositionIntent,
    bid: str,
    ask: str,
    right: OptionRight = OptionRight.CALL,
    quote_at: datetime = BOUNDARY,
) -> OptionLeg:
    return OptionLeg(
        instrument_kind=InstrumentKind.OPTION,
        symbol=symbol,
        underlying="ACME",
        right=right,
        strike=Decimal(strike),
        expiry=date(2037, 5, 8),
        intent=intent,
        ratio=1,
        multiplier=100,
        active=True,
        tradable=True,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=10,
        ask_size=12,
        quote_at=quote_at,
        feed=OptionFeed.INDICATIVE_MODIFIED,
        greeks_complete=True,
        greek_units_verified=True,
    )


def candidate(**changes: object) -> VerticalCandidate:
    values: dict[str, object] = {
        "strategy": VerticalStrategy.BULL_CALL_DEBIT,
        "legs": (
            leg(
                symbol="ACME370508C00100000",
                strike="100",
                intent=PositionIntent.BUY_TO_OPEN,
                bid="2.00",
                ask="2.10",
            ),
            leg(
                symbol="ACME370508C00105000",
                strike="105",
                intent=PositionIntent.SELL_TO_OPEN,
                bid="0.90",
                ask="0.94",
            ),
        ),
        "quantity": 4,
        "dte": 23,
        "approved_limit": Decimal("1.20"),
        "candidate_score": 82,
        "selection_rank": 1,
        "buying_power_sufficient": True,
    }
    values.update(changes)
    return VerticalCandidate(**values)


def bear_put_candidate(**changes: object) -> VerticalCandidate:
    values: dict[str, object] = {
        "strategy": VerticalStrategy.BEAR_PUT_DEBIT,
        "legs": (
            leg(
                symbol="ACME370508P00100000",
                strike="100",
                intent=PositionIntent.BUY_TO_OPEN,
                bid="2.00",
                ask="2.10",
                right=OptionRight.PUT,
            ),
            leg(
                symbol="ACME370508P00095000",
                strike="95",
                intent=PositionIntent.SELL_TO_OPEN,
                bid="0.90",
                ask="0.94",
                right=OptionRight.PUT,
            ),
        ),
        "quantity": 4,
        "dte": 23,
        "approved_limit": Decimal("1.20"),
        "candidate_score": 82,
        "selection_rank": 1,
        "buying_power_sufficient": True,
    }
    values.update(changes)
    return VerticalCandidate(**values)


def bull_put_credit_candidate(**changes: object) -> VerticalCandidate:
    values: dict[str, object] = {
        "strategy": VerticalStrategy.BULL_PUT_CREDIT,
        "legs": (
            leg(
                symbol="ACME370508P00095000",
                strike="95",
                intent=PositionIntent.BUY_TO_OPEN,
                bid="0.58",
                ask="0.60",
                right=OptionRight.PUT,
            ),
            leg(
                symbol="ACME370508P00100000",
                strike="100",
                intent=PositionIntent.SELL_TO_OPEN,
                bid="1.60",
                ask="1.64",
                right=OptionRight.PUT,
            ),
        ),
        "quantity": 2,
        "dte": 23,
        "approved_limit": Decimal("1.00"),
        "candidate_score": 82,
        "selection_rank": 1,
        "buying_power_sufficient": True,
    }
    values.update(changes)
    return VerticalCandidate(**values)


def account(**changes: object) -> AccountOpportunityState:
    values: dict[str, object] = {
        "account_role": AccountRole.SUBMISSION,
        "book_fingerprint": "a" * 64,
        "baseline_clean": True,
        "clean_equity": Decimal("100000"),
        "open_position_count": 0,
        "open_order_count": 0,
        "filled_entry_count": 0,
        "lifetime_approved_risk": Decimal("0"),
        "entry_reservation_active": False,
        "reserved_approved_risk": Decimal("0"),
        "event_already_attempted": False,
    }
    values.update(changes)
    return AccountOpportunityState(**values)


def opportunity_input(**changes: object) -> OpportunityInput:
    values: dict[str, object] = {
        "opportunity_key": "ACME_EVENT",
        "underlying": "ACME",
        "observed_decision_boundary": BOUNDARY,
        "evaluated_at": BOUNDARY + timedelta(seconds=5),
        "completed_bar_at": BOUNDARY,
        "decision_boundary_complete": True,
        "prior_decision_outcome": None,
        "data_quality": DataQuality.COMPLETE,
        "market_open": True,
        "trading_halted": TradingHaltState.NOT_HALTED,
        "underlying_observed_at": BOUNDARY,
        "catalyst_observed_at": BOUNDARY,
        "catalyst_quality": CatalystQuality.CLEAR,
        "catalyst_score": 25,
        "vwap_distance": Decimal("0.019"),
        "relative_return": Decimal("0.011"),
        "beta": Decimal("1.7"),
        "bull_trend_hits": 3,
        "bear_trend_hits": 0,
        "absolute_first_reaction": Decimal("0.13"),
        "candidate": candidate(),
        "account": account(),
    }
    values.update(changes)
    return OpportunityInput(**values)


def test_eligible_bull_call_vertical_returns_immutable_entry_decision() -> None:
    result = evaluate_opportunity(policy(), opportunity_input())

    assert result.outcome == OpportunityOutcome.ENTRY_APPROVED
    assert result.reason_codes == (OpportunityReason.ENTRY_APPROVED,)
    assert result.direction.value == "BULLISH"
    assert result.strategy == VerticalStrategy.BULL_CALL_DEBIT
    assert result.quantity == 4
    assert result.approved_max_loss == Decimal("480.00")
    assert len(result.input_hash) == len(result.policy_hash) == len(result.result_hash) == 64


def test_mirrored_bearish_confirmation_approves_only_bearish_vertical() -> None:
    result = evaluate_opportunity(
        policy(),
        opportunity_input(
            vwap_distance=Decimal("-0.019"),
            relative_return=Decimal("-0.011"),
            bull_trend_hits=0,
            bear_trend_hits=3,
            candidate=bear_put_candidate(),
        ),
    )

    assert result.outcome == OpportunityOutcome.ENTRY_APPROVED
    assert result.direction.value == "BEARISH"
    assert result.strategy == VerticalStrategy.BEAR_PUT_DEBIT


def test_defined_risk_credit_vertical_uses_width_less_credit_for_max_loss() -> None:
    result = evaluate_opportunity(
        policy(), opportunity_input(candidate=bull_put_credit_candidate())
    )

    assert result.outcome == OpportunityOutcome.ENTRY_APPROVED
    assert result.approved_max_loss == Decimal("800.00")


def test_risk_and_lifetime_caps_accept_exact_equality() -> None:
    exact_risk_candidate = candidate(
        legs=(
            replace(candidate().legs[0], bid=Decimal("3.40"), ask=Decimal("3.50")),
            replace(candidate().legs[1], bid=Decimal("1.00"), ask=Decimal("1.04")),
        ),
        approved_limit=Decimal("2.40"),
    )
    result = evaluate_opportunity(
        policy(),
        opportunity_input(
            candidate=exact_risk_candidate,
            account=account(
                filled_entry_count=3,
                lifetime_approved_risk=Decimal("1440"),
            ),
        ),
    )

    assert result.outcome == OpportunityOutcome.ENTRY_APPROVED
    assert result.approved_max_loss == Decimal("960.00")


def test_freshness_limits_accept_exact_equality() -> None:
    result = evaluate_opportunity(
        policy(), opportunity_input(evaluated_at=BOUNDARY + timedelta(seconds=43))
    )

    assert result.outcome == OpportunityOutcome.ENTRY_APPROVED


def test_invalid_prospective_policy_is_rejected_before_evaluation() -> None:
    with pytest.raises(ValueError, match="INVALID_OPPORTUNITY_POLICY"):
        policy(
            minimum_vwap_distance=Decimal("0.04"),
            maximum_vwap_distance=Decimal("0.031"),
        )
    with pytest.raises(ValueError, match="INVALID_OPPORTUNITY_POLICY"):
        policy(maximum_quantity=MAX_STRUCTURAL_OPTION_QUANTITY + 1)


def test_decision_record_hashes_are_canonical_literals_and_record_is_frozen() -> None:
    result = evaluate_opportunity(policy(), opportunity_input())

    assert result.input_hash == "04c78755822264778159e0c1a0e78b7aaa195a139a1fa4f7dddb228353b0db7e"
    assert result.policy_hash == "e914c54ec2b5d7f2b61167530375569c8f2a78d5b2b2363bcfab41f64de08a0e"
    assert opportunity_policy_hash(policy()) == result.policy_hash
    assert (
        result.candidate_hash == "f248eb3c07e05a8e6633b67dfc8303f1d049e6450f72b183bfae3dcf00e74a5e"
    )
    assert result.result_hash == "401f2563675aa00d77bd25b7c9502380eccc5b85ffc6b29a8a0750842f15454a"
    with pytest.raises(FrozenInstanceError):
        result.quantity = 5


def test_direction_confirmation_rejects_vwap_distance_at_strict_minimum() -> None:
    result = evaluate_opportunity(policy(), opportunity_input(vwap_distance=Decimal("0.0037")))

    assert result.outcome == OpportunityOutcome.NO_TRADE
    assert result.reason_codes == (OpportunityReason.DIRECTION_NOT_CONFIRMED,)
    assert result.direction is None
    assert result.quantity is None


def test_confirmed_direction_cannot_be_replaced_by_opposite_vertical() -> None:
    result = evaluate_opportunity(policy(), opportunity_input(candidate=bear_put_candidate()))

    assert result.outcome == OpportunityOutcome.NO_TRADE
    assert result.reason_codes == (OpportunityReason.VERTICAL_DIRECTION_MISMATCH,)
    assert result.direction.value == "BULLISH"


def test_unselected_boundary_is_not_a_fallback() -> None:
    result = evaluate_opportunity(
        policy(),
        opportunity_input(
            observed_decision_boundary=BOUNDARY + timedelta(hours=1),
            completed_bar_at=BOUNDARY + timedelta(hours=1),
            evaluated_at=BOUNDARY + timedelta(hours=1, seconds=5),
            underlying_observed_at=BOUNDARY + timedelta(hours=1),
            catalyst_observed_at=BOUNDARY + timedelta(hours=1),
            candidate=candidate(
                legs=tuple(
                    replace(item, quote_at=BOUNDARY + timedelta(hours=1))
                    for item in candidate().legs
                )
            ),
        ),
    )

    assert result.outcome == OpportunityOutcome.NO_TRADE
    assert result.reason_codes == (OpportunityReason.DECISION_BOUNDARY_MISMATCH,)


def test_prior_no_trade_decision_is_binding_and_cannot_be_retried() -> None:
    result = evaluate_opportunity(
        policy(),
        opportunity_input(prior_decision_outcome=OpportunityOutcome.NO_TRADE),
    )

    assert result.outcome == OpportunityOutcome.NO_TRADE
    assert result.reason_codes == (OpportunityReason.PRIOR_DECISION_BINDING,)
    assert result.input_hash == "5fb3d5289cae87cf5ed95d1077ff4cb2b6125196f46b78ea33a1e0610a56e6a9"
    assert result.result_hash == "4fdc59a0b248a68af1740c03c11f6af5809c4e4d93bec3a773d34d9c21c658f3"


def test_incomplete_decision_bar_cannot_authorize_entry() -> None:
    result = evaluate_opportunity(policy(), opportunity_input(decision_boundary_complete=False))

    assert result.outcome == OpportunityOutcome.NO_TRADE
    assert result.reason_codes == (OpportunityReason.DECISION_BOUNDARY_INCOMPLETE,)


def test_decision_boundary_after_freshness_limit_is_stale() -> None:
    result = evaluate_opportunity(
        policy(), opportunity_input(evaluated_at=BOUNDARY + timedelta(seconds=43, microseconds=1))
    )

    assert result.outcome == OpportunityOutcome.NO_TRADE
    assert result.reason_codes == (OpportunityReason.DECISION_BOUNDARY_STALE,)


def test_decision_cannot_run_before_completed_boundary() -> None:
    result = evaluate_opportunity(
        policy(), opportunity_input(evaluated_at=BOUNDARY - timedelta(microseconds=1))
    )

    assert result.outcome == OpportunityOutcome.NO_TRADE
    assert result.reason_codes == (OpportunityReason.DECISION_BOUNDARY_NOT_REACHED,)


def test_incomplete_normalized_market_data_is_no_trade() -> None:
    result = evaluate_opportunity(policy(), opportunity_input(data_quality=DataQuality.MISSING))

    assert result.outcome == OpportunityOutcome.NO_TRADE
    assert result.reason_codes == (OpportunityReason.DATA_QUALITY_INCOMPLETE,)


def test_authoritative_catalyst_contradiction_cannot_become_bearish_signal() -> None:
    result = evaluate_opportunity(
        policy(),
        opportunity_input(
            catalyst_quality=CatalystQuality.AUTHORITATIVE_CONTRADICTION,
            vwap_distance=Decimal("-0.019"),
            relative_return=Decimal("-0.011"),
            bull_trend_hits=0,
            bear_trend_hits=3,
            candidate=bear_put_candidate(),
        ),
    )

    assert result.outcome == OpportunityOutcome.NO_TRADE
    assert result.reason_codes == (OpportunityReason.CATALYST_CONTRADICTED,)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"market_open": False}, OpportunityReason.MARKET_CLOSED),
        ({"trading_halted": TradingHaltState.HALTED}, OpportunityReason.TRADING_HALTED),
        (
            {"trading_halted": TradingHaltState.UNKNOWN},
            OpportunityReason.TRADING_HALT_STATUS_UNKNOWN,
        ),
        (
            {"underlying_observed_at": BOUNDARY - timedelta(seconds=39)},
            OpportunityReason.UNDERLYING_DATA_STALE,
        ),
        (
            {"catalyst_observed_at": BOUNDARY - timedelta(minutes=17)},
            OpportunityReason.CATALYST_DATA_STALE,
        ),
        ({"catalyst_quality": CatalystQuality.MISSING}, OpportunityReason.CATALYST_DATA_MISSING),
        ({"catalyst_score": 16}, OpportunityReason.CATALYST_SCORE_BELOW_MINIMUM),
        ({"beta": Decimal("0")}, OpportunityReason.BETA_OUT_OF_BOUNDS),
        (
            {"absolute_first_reaction": Decimal("0.190000001")},
            OpportunityReason.FIRST_REACTION_OUT_OF_SUPPORT,
        ),
        (
            {"absolute_first_reaction": Decimal("-0.01")},
            OpportunityReason.NORMALIZED_INPUT_INVALID,
        ),
    ],
)
def test_market_and_catalyst_quality_fail_closed(
    changes: dict[str, object], reason: OpportunityReason
) -> None:
    result = evaluate_opportunity(policy(), opportunity_input(**changes))

    assert result.outcome == OpportunityOutcome.NO_TRADE
    assert result.reason_codes == (reason,)


def test_missing_option_candidate_is_an_explicit_no_trade() -> None:
    result = evaluate_opportunity(policy(), opportunity_input(candidate=None))

    assert result.outcome == OpportunityOutcome.NO_TRADE
    assert result.reason_codes == (OpportunityReason.OPTION_CANDIDATE_MISSING,)


def _candidate_with_leg(index: int, **changes: object) -> VerticalCandidate:
    selected = candidate()
    legs = list(selected.legs)
    legs[index] = replace(legs[index], **changes)
    return replace(selected, legs=tuple(legs))


@pytest.mark.parametrize(
    ("policy_changes", "selected", "reason"),
    [
        (
            {},
            _candidate_with_leg(0, instrument_kind=InstrumentKind.EQUITY),
            OpportunityReason.OPTION_ONLY_REQUIRED,
        ),
        ({}, candidate(legs=(candidate().legs[0],)), OpportunityReason.VERTICAL_STRUCTURE_INVALID),
        (
            {},
            _candidate_with_leg(0, feed=OptionFeed.OPRA),
            OpportunityReason.OPTION_FEED_NOT_INDICATIVE,
        ),
        (
            {},
            _candidate_with_leg(0, quote_at=BOUNDARY - timedelta(seconds=39)),
            OpportunityReason.OPTION_QUOTE_STALE,
        ),
        (
            {"maximum_option_quote_age": timedelta(seconds=60)},
            _candidate_with_leg(0, quote_at=BOUNDARY - timedelta(seconds=30)),
            OpportunityReason.OPTION_QUOTES_UNSYNCHRONIZED,
        ),
        (
            {},
            _candidate_with_leg(0, bid=Decimal("2.20"), ask=Decimal("2.10")),
            OpportunityReason.OPTION_QUOTE_INVALID,
        ),
        (
            {},
            _candidate_with_leg(0, bid=Decimal("1.00"), ask=Decimal("2.00")),
            OpportunityReason.OPTION_QUOTE_TOO_WIDE,
        ),
        (
            {},
            _candidate_with_leg(0, greeks_complete=False),
            OpportunityReason.OPTION_GREEKS_MISSING,
        ),
        ({}, candidate(dte=18), OpportunityReason.OPTION_DTE_OUT_OF_RANGE),
        ({}, candidate(dte=24), OpportunityReason.OPTION_DTE_MISMATCH),
        (
            {},
            _candidate_with_leg(0, active=False),
            OpportunityReason.OPTION_CONTRACT_INELIGIBLE,
        ),
        (
            {},
            _candidate_with_leg(0, expiry=date(2037, 5, 15)),
            OpportunityReason.VERTICAL_STRUCTURE_INVALID,
        ),
        ({}, candidate(approved_limit=Decimal("3.16")), OpportunityReason.OPTION_PAYOFF_INVALID),
        ({}, candidate(approved_limit=Decimal("1.21")), OpportunityReason.OPTION_PAYOFF_INVALID),
        ({}, candidate(candidate_score=72), OpportunityReason.CANDIDATE_SCORE_BELOW_MINIMUM),
        ({}, candidate(candidate_score=101), OpportunityReason.OPTION_CANDIDATE_INVALID),
        ({}, candidate(selection_rank=2), OpportunityReason.CANDIDATE_FALLBACK_FORBIDDEN),
        ({}, candidate(buying_power_sufficient=False), OpportunityReason.BUYING_POWER_INSUFFICIENT),
        ({}, candidate(quantity=10), OpportunityReason.QUANTITY_OUT_OF_BOUNDS),
    ],
)
def test_option_structure_and_quote_quality_fail_closed(
    policy_changes: dict[str, object],
    selected: VerticalCandidate,
    reason: OpportunityReason,
) -> None:
    result = evaluate_opportunity(policy(**policy_changes), opportunity_input(candidate=selected))

    assert result.outcome == OpportunityOutcome.NO_TRADE
    assert result.reason_codes == (reason,)


@pytest.mark.parametrize(
    ("policy_changes", "input_changes", "reason"),
    [
        (
            {},
            {"account": account(account_role=AccountRole.REPLAY)},
            OpportunityReason.ACCOUNT_ROLE_NOT_EXECUTABLE,
        ),
        (
            {},
            {"account": account(clean_equity=Decimal("54321"))},
            OpportunityReason.EQUITY_FLOOR_REACHED,
        ),
        ({}, {"account": account(open_order_count=1)}, OpportunityReason.OPEN_ORDER_EXISTS),
        (
            {},
            {"account": account(filled_entry_count=4)},
            OpportunityReason.LIFETIME_ENTRY_LIMIT_REACHED,
        ),
        (
            {},
            {"account": account(lifetime_approved_risk=Decimal("2000"))},
            OpportunityReason.LIFETIME_RISK_LIMIT_REACHED,
        ),
        (
            {},
            {
                "account": account(
                    entry_reservation_active=True,
                    reserved_approved_risk=Decimal("400"),
                )
            },
            OpportunityReason.ENTRY_RESERVATION_ACTIVE,
        ),
        (
            {},
            {"account": account(book_fingerprint="not-a-hash")},
            OpportunityReason.BOOK_FINGERPRINT_INVALID,
        ),
        (
            {},
            {"account": account(open_position_count=-1)},
            OpportunityReason.ACCOUNT_STATE_INVALID,
        ),
        (
            {},
            {"candidate": bull_put_credit_candidate(quantity=3)},
            OpportunityReason.POSITION_RISK_LIMIT_EXCEEDED,
        ),
        (
            {"last_entry_boundary": BOUNDARY - timedelta(microseconds=1)},
            {},
            OpportunityReason.ENTRY_WINDOW_CLOSED,
        ),
        ({}, {"opportunity_key": "OTHER_EVENT"}, OpportunityReason.POLICY_SCOPE_MISMATCH),
    ],
)
def test_account_budget_and_entry_window_gates_fail_closed(
    policy_changes: dict[str, object],
    input_changes: dict[str, object],
    reason: OpportunityReason,
) -> None:
    result = evaluate_opportunity(policy(**policy_changes), opportunity_input(**input_changes))

    assert result.outcome == OpportunityOutcome.NO_TRADE
    assert result.reason_codes == (reason,)

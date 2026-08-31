import pytest
from alpaca.trading.enums import OrderStatus

from backend.app.execution.order_status import (
    KNOWN_BROKER_ORDER_STATES,
    LOOKUP_ONLY_BROKER_ORDER_STATES,
    MUTATION_ELIGIBLE_BROKER_ORDER_STATES,
    TERMINAL_BROKER_ORDER_STATES,
    BrokerOrderPhase,
    broker_lookup_policy,
    broker_order_phase,
    broker_state_matches_fill,
    normalize_broker_order_state,
)


def test_order_state_taxonomy_covers_the_pinned_alpaca_sdk() -> None:
    sdk_states = {status.value.upper() for status in OrderStatus}

    assert sdk_states == KNOWN_BROKER_ORDER_STATES


def test_order_state_phases_are_disjoint_and_complete() -> None:
    assert not MUTATION_ELIGIBLE_BROKER_ORDER_STATES & LOOKUP_ONLY_BROKER_ORDER_STATES
    assert not MUTATION_ELIGIBLE_BROKER_ORDER_STATES & TERMINAL_BROKER_ORDER_STATES
    assert not LOOKUP_ONLY_BROKER_ORDER_STATES & TERMINAL_BROKER_ORDER_STATES
    assert (
        MUTATION_ELIGIBLE_BROKER_ORDER_STATES
        | LOOKUP_ONLY_BROKER_ORDER_STATES
        | TERMINAL_BROKER_ORDER_STATES
    ) == KNOWN_BROKER_ORDER_STATES


def test_pending_replace_is_lookup_only_and_can_retain_a_partial_fill() -> None:
    assert broker_order_phase("pending_replace") is BrokerOrderPhase.LOOKUP_ONLY
    assert broker_state_matches_fill("PENDING_REPLACE", 1, 2) is True
    assert broker_state_matches_fill("PENDING_REPLACE", 2, 2) is False


@pytest.mark.parametrize("state", ["CANCELED", "REPLACED"])
def test_canceled_and_replaced_orders_cannot_claim_a_full_fill(state: str) -> None:
    assert broker_state_matches_fill(state, 0, 2) is True
    assert broker_state_matches_fill(state, 1, 2) is True
    assert broker_state_matches_fill(state, 2, 2) is False


def test_expired_order_can_retain_a_partial_fill() -> None:
    assert broker_state_matches_fill("EXPIRED", 1, 2) is True
    assert broker_state_matches_fill("EXPIRED", 2, 2) is False


def test_calculated_order_can_report_a_completed_cumulative_fill() -> None:
    assert broker_order_phase("CALCULATED") is BrokerOrderPhase.LOOKUP_ONLY
    assert broker_state_matches_fill("CALCULATED", 2, 2) is True


def test_stopped_order_requires_zero_fill() -> None:
    assert broker_state_matches_fill("STOPPED", 0, 2) is True
    assert broker_state_matches_fill("STOPPED", 1, 2) is False


def test_done_for_day_uses_a_session_scale_observation_window() -> None:
    assert broker_lookup_policy("DONE_FOR_DAY").cadence.total_seconds() == 1800
    assert broker_lookup_policy("DONE_FOR_DAY").deadline.total_seconds() == 129600


@pytest.mark.parametrize(
    "state",
    [
        "DONE_FOR_DAY",
        "PENDING_CANCEL",
        "PENDING_REVIEW",
        "ACCEPTED_FOR_BIDDING",
        "STOPPED",
        "SUSPENDED",
        "HELD",
    ],
)
def test_rare_nonterminal_states_are_lookup_only(state: str) -> None:
    assert broker_order_phase(state) is BrokerOrderPhase.LOOKUP_ONLY


def test_unknown_broker_state_fails_closed() -> None:
    with pytest.raises(ValueError, match="BROKER_ORDER_STATE_UNKNOWN"):
        normalize_broker_order_state("settled_by_magic")

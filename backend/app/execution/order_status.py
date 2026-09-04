from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum


class BrokerOrderPhase(StrEnum):
    MUTATION_ELIGIBLE = "MUTATION_ELIGIBLE"
    LOOKUP_ONLY = "LOOKUP_ONLY"
    TERMINAL = "TERMINAL"


MUTATION_ELIGIBLE_BROKER_ORDER_STATES = frozenset(
    {
        "NEW",
        "PARTIALLY_FILLED",
        "ACCEPTED",
        "PENDING_NEW",
    }
)
LOOKUP_ONLY_BROKER_ORDER_STATES = frozenset(
    {
        "DONE_FOR_DAY",
        "PENDING_CANCEL",
        "PENDING_REPLACE",
        "PENDING_REVIEW",
        "ACCEPTED_FOR_BIDDING",
        "STOPPED",
        "SUSPENDED",
        "CALCULATED",
        "HELD",
    }
)
TERMINAL_BROKER_ORDER_STATES = frozenset(
    {
        "FILLED",
        "CANCELED",
        "EXPIRED",
        "REPLACED",
        "REJECTED",
    }
)
PENDING_BROKER_ORDER_STATES = (
    MUTATION_ELIGIBLE_BROKER_ORDER_STATES | LOOKUP_ONLY_BROKER_ORDER_STATES
)
KNOWN_BROKER_ORDER_STATES = PENDING_BROKER_ORDER_STATES | TERMINAL_BROKER_ORDER_STATES
FINALIZABLE_BROKER_ORDER_STATES = TERMINAL_BROKER_ORDER_STATES | {"CALCULATED"}

_ZERO_FILL_PENDING_STATES = frozenset(
    {
        "NEW",
        "PENDING_REVIEW",
        "ACCEPTED",
        "PENDING_NEW",
        "ACCEPTED_FOR_BIDDING",
        "HELD",
        "STOPPED",
    }
)


@dataclass(frozen=True)
class BrokerLookupPolicy:
    cadence: timedelta
    deadline: timedelta


_DEFAULT_LOOKUP_POLICY = BrokerLookupPolicy(timedelta(seconds=30), timedelta(minutes=10))
_LOOKUP_POLICIES = {
    "DONE_FOR_DAY": BrokerLookupPolicy(timedelta(minutes=30), timedelta(hours=36)),
    "CALCULATED": BrokerLookupPolicy(timedelta(minutes=1), timedelta(minutes=30)),
}


def broker_lookup_policy(value: str) -> BrokerLookupPolicy:
    state = normalize_broker_order_state(value)
    if state not in PENDING_BROKER_ORDER_STATES:
        raise ValueError("BROKER_LOOKUP_POLICY_STATE_INVALID")
    return _LOOKUP_POLICIES.get(state, _DEFAULT_LOOKUP_POLICY)


def normalize_broker_order_state(value: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("BROKER_ORDER_STATE_INVALID")
    state = value.upper()
    if state not in KNOWN_BROKER_ORDER_STATES:
        raise ValueError("BROKER_ORDER_STATE_UNKNOWN")
    return state


def broker_order_phase(value: str) -> BrokerOrderPhase:
    state = normalize_broker_order_state(value)
    if state in MUTATION_ELIGIBLE_BROKER_ORDER_STATES:
        return BrokerOrderPhase.MUTATION_ELIGIBLE
    if state in LOOKUP_ONLY_BROKER_ORDER_STATES:
        return BrokerOrderPhase.LOOKUP_ONLY
    return BrokerOrderPhase.TERMINAL


def broker_state_matches_fill(state: str, filled_quantity: int, quantity: int) -> bool:
    state = normalize_broker_order_state(state)
    if not 0 <= filled_quantity <= quantity:
        return False
    if state in _ZERO_FILL_PENDING_STATES or state == "REJECTED":
        return filled_quantity == 0
    if state == "PARTIALLY_FILLED":
        return 0 < filled_quantity < quantity
    if state == "FILLED":
        return filled_quantity == quantity
    if state == "CALCULATED":
        return True
    if state in PENDING_BROKER_ORDER_STATES or state in {
        "CANCELED",
        "EXPIRED",
        "REPLACED",
    }:
        return filled_quantity < quantity
    return False

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from math import isfinite

from backend.app.contracts.v1 import GreekExposure


class VerticalKind(StrEnum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class ExposureBlocked(ValueError):
    pass


@dataclass(frozen=True)
class PayoffResult:
    cumulative_max_loss: Decimal
    liquidation_pnl: Decimal
    remaining_worst_loss: Decimal
    remaining_best_reward: Decimal


@dataclass(frozen=True)
class GreekLeg:
    contracts: int
    is_long: bool
    delta: Decimal | None
    gamma: Decimal | None
    theta_per_day: Decimal | None
    vega_per_iv_point: Decimal | None
    multiplier: int = 100
    units_verified: bool = True


def calculate_vertical_payoff(
    *,
    kind: VerticalKind,
    width: Decimal,
    quantity: int,
    cumulative_cashflow: Decimal,
    close_cashflow_now: Decimal,
) -> PayoffResult:
    if width <= 0 or quantity <= 0:
        raise ExposureBlocked("INVALID_VERTICAL_DIMENSIONS")

    settlement = width * quantity * 100
    if kind == VerticalKind.DEBIT:
        worst_settlement = Decimal(0)
        best_settlement = settlement
    else:
        worst_settlement = -settlement
        best_settlement = Decimal(0)

    return PayoffResult(
        cumulative_max_loss=max(Decimal(0), -(cumulative_cashflow + worst_settlement)),
        liquidation_pnl=cumulative_cashflow + close_cashflow_now,
        remaining_worst_loss=max(Decimal(0), close_cashflow_now - worst_settlement),
        remaining_best_reward=max(Decimal(0), best_settlement - close_cashflow_now),
    )


def fill_cashflow(
    *, price: Decimal, contracts: int, is_buy: bool, multiplier: int = 100
) -> Decimal:
    if price < 0 or contracts <= 0 or multiplier != 100:
        raise ExposureBlocked("INVALID_FILL")
    cashflow = price * contracts * multiplier
    return -cashflow if is_buy else cashflow


def normalized_limit_cashflow(provider_limit_price: Decimal, multiplier: int = 100) -> Decimal:
    if multiplier != 100:
        raise ExposureBlocked("UNSUPPORTED_MULTIPLIER")
    return -provider_limit_price * multiplier


def aggregate_greeks(legs: tuple[GreekLeg, ...]) -> GreekExposure:
    if len(legs) != 2:
        raise ExposureBlocked("INCOMPLETE_VERTICAL")

    totals = [Decimal(0), Decimal(0), Decimal(0), Decimal(0)]
    for leg in legs:
        values = (leg.delta, leg.gamma, leg.theta_per_day, leg.vega_per_iv_point)
        if (
            leg.contracts <= 0
            or leg.multiplier != 100
            or not leg.units_verified
            or any(value is None or not isfinite(value) for value in values)
        ):
            raise ExposureBlocked("INVALID_GREEK_INPUT")
        sign = 1 if leg.is_long else -1
        for index, value in enumerate(values):
            totals[index] += value * sign * leg.contracts * leg.multiplier

    return GreekExposure(
        delta=totals[0],
        gamma=totals[1],
        theta_per_day=totals[2],
        vega_per_iv_point=totals[3],
    )


def reconcile_vertical_inventory(
    expected_option_symbols: tuple[str, str],
    actual_option_symbols: tuple[str, ...],
    underlying_quantity: int,
) -> str:
    if set(expected_option_symbols) != set(actual_option_symbols) or underlying_quantity != 0:
        return "ASSIGNMENT_SUSPECTED"
    return "COMPLETE"

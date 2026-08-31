from decimal import Decimal

import pytest

from backend.app.domain.exposure import (
    ExposureBlocked,
    GreekLeg,
    VerticalKind,
    aggregate_greeks,
    calculate_vertical_payoff,
    fill_cashflow,
    normalized_limit_cashflow,
    reconcile_vertical_inventory,
)


def test_debit_vertical_reports_signed_payoff_and_remaining_values() -> None:
    result = calculate_vertical_payoff(
        kind=VerticalKind.DEBIT,
        width=Decimal("5"),
        quantity=2,
        cumulative_cashflow=Decimal("-320"),
        close_cashflow_now=Decimal("460"),
    )

    assert result.cumulative_max_loss == Decimal("320")
    assert result.liquidation_pnl == Decimal("140")
    assert result.remaining_worst_loss == Decimal("460")
    assert result.remaining_best_reward == Decimal("540")


def test_credit_vertical_reports_defined_loss_and_reward() -> None:
    result = calculate_vertical_payoff(
        kind=VerticalKind.CREDIT,
        width=Decimal("5"),
        quantity=1,
        cumulative_cashflow=Decimal("140"),
        close_cashflow_now=Decimal("-70"),
    )

    assert result.cumulative_max_loss == Decimal("360")
    assert result.liquidation_pnl == Decimal("70")
    assert result.remaining_worst_loss == Decimal("430")
    assert result.remaining_best_reward == Decimal("70")


def test_roll_cashflow_carries_realized_result_into_new_maximum_loss() -> None:
    result = calculate_vertical_payoff(
        kind=VerticalKind.DEBIT,
        width=Decimal("10"),
        quantity=1,
        cumulative_cashflow=Decimal("-475"),
        close_cashflow_now=Decimal("620"),
    )

    assert result.cumulative_max_loss == Decimal("475")
    assert result.liquidation_pnl == Decimal("145")
    assert result.remaining_worst_loss == Decimal("620")
    assert result.remaining_best_reward == Decimal("380")


def test_fill_and_provider_limit_signs_use_account_cashflow() -> None:
    assert fill_cashflow(price=Decimal("1.25"), contracts=2, is_buy=True) == Decimal("-250")
    assert fill_cashflow(price=Decimal("1.25"), contracts=2, is_buy=False) == Decimal("250")
    assert normalized_limit_cashflow(Decimal("1.25")) == Decimal("-125.00")
    assert normalized_limit_cashflow(Decimal("-1.25")) == Decimal("125.00")


def test_greeks_are_signed_and_scaled_by_contract_multiplier() -> None:
    exposure = aggregate_greeks(
        (
            GreekLeg(2, True, Decimal("0.60"), Decimal("0.04"), Decimal("-0.08"), Decimal("0.12")),
            GreekLeg(2, False, Decimal("0.35"), Decimal("0.03"), Decimal("-0.05"), Decimal("0.09")),
        )
    )

    assert exposure.delta == Decimal("50.00")
    assert exposure.gamma == Decimal("2.00")
    assert exposure.theta_per_day == Decimal("-6.00")
    assert exposure.vega_per_iv_point == Decimal("6.00")


def test_missing_leg_or_unexpected_underlying_inventory_blocks_as_assignment_suspected() -> None:
    assert reconcile_vertical_inventory(("CALL-100", "CALL-105"), ("CALL-100",), 0) == (
        "ASSIGNMENT_SUSPECTED"
    )
    assert reconcile_vertical_inventory(
        ("CALL-100", "CALL-105"), ("CALL-100", "CALL-105"), 100
    ) == ("ASSIGNMENT_SUSPECTED")


@pytest.mark.parametrize(
    "leg,code",
    [
        (
            GreekLeg(1, True, None, Decimal("0.1"), Decimal("-0.1"), Decimal("0.1")),
            "INVALID_GREEK_INPUT",
        ),
        (
            GreekLeg(1, True, Decimal("NaN"), Decimal("0.1"), Decimal("-0.1"), Decimal("0.1")),
            "INVALID_GREEK_INPUT",
        ),
        (
            GreekLeg(
                1,
                True,
                Decimal("0.1"),
                Decimal("0.1"),
                Decimal("-0.1"),
                Decimal("0.1"),
                units_verified=False,
            ),
            "INVALID_GREEK_INPUT",
        ),
    ],
)
def test_missing_nonfinite_or_unverified_greeks_block(leg: GreekLeg, code: str) -> None:
    with pytest.raises(ExposureBlocked, match=code):
        aggregate_greeks(
            (
                leg,
                GreekLeg(
                    1, False, Decimal("0.05"), Decimal("0.01"), Decimal("-0.01"), Decimal("0.02")
                ),
            )
        )


def test_incomplete_vertical_blocks_exposure() -> None:
    with pytest.raises(ExposureBlocked, match="INCOMPLETE_VERTICAL"):
        aggregate_greeks(
            (GreekLeg(1, True, Decimal("0.1"), Decimal("0.1"), Decimal("-0.1"), Decimal("0.1")),)
        )


def test_unknown_multiplier_blocks_instead_of_guessing() -> None:
    with pytest.raises(ExposureBlocked, match="UNSUPPORTED_MULTIPLIER"):
        normalized_limit_cashflow(Decimal("1.00"), multiplier=10)

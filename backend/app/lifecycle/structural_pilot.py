from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from backend.app.contracts.v1 import AccountRole
from backend.app.domain.option_contract_symbol import (
    OptionContractSymbolError,
    parse_standard_option_contract_symbol,
)
from backend.app.policy.opportunity import (
    STRUCTURAL_BULLISH_PILOT,
    STRUCTURAL_BULLISH_PILOT_ID,
    OpportunityDirection,
    structural_pilot_profile,
)

if TYPE_CHECKING:
    from backend.app.services.acquisition import RetainedLifecycleContext


STRUCTURAL_BULLISH_BETA_PILOT_ID = STRUCTURAL_BULLISH_PILOT_ID
STRUCTURAL_PROFIT_TARGET_CLOSE = "STRUCTURAL_PROFIT_TARGET_CLOSE"
STRUCTURAL_STOP_LIMIT_CLOSE = "STRUCTURAL_STOP_LIMIT_CLOSE"
STRUCTURAL_MANDATORY_BOUNDARY_CLOSE = "STRUCTURAL_MANDATORY_BOUNDARY_CLOSE"
STRUCTURAL_CLOSE_REASONS = frozenset(
    {
        STRUCTURAL_PROFIT_TARGET_CLOSE,
        STRUCTURAL_STOP_LIMIT_CLOSE,
        STRUCTURAL_MANDATORY_BOUNDARY_CLOSE,
    }
)
STRUCTURAL_MANDATORY_CLOSE_AT = datetime(2026, 9, 4, 13, 45, tzinfo=UTC)
STRUCTURAL_RECONCILIATION_DUE_AT = datetime(2026, 9, 4, 13, 55, tzinfo=UTC)


@dataclass(frozen=True)
class StructuralBullishBetaLifecycleContract:
    strategy_id: str = STRUCTURAL_BULLISH_BETA_PILOT_ID
    profit_value_ratio: Decimal = Decimal("1.30")
    stop_value_ratio: Decimal = Decimal("0.80")
    mandatory_close_at: datetime = STRUCTURAL_MANDATORY_CLOSE_AT
    reconciliation_due_at: datetime = STRUCTURAL_RECONCILIATION_DUE_AT

    def __post_init__(self) -> None:
        if (
            self.strategy_id != STRUCTURAL_BULLISH_BETA_PILOT_ID
            or self.profit_value_ratio != Decimal("1.30")
            or self.stop_value_ratio != Decimal("0.80")
            or self.mandatory_close_at != STRUCTURAL_MANDATORY_CLOSE_AT
            or self.reconciliation_due_at != STRUCTURAL_RECONCILIATION_DUE_AT
        ):
            raise ValueError("STRUCTURAL_LIFECYCLE_CONTRACT_INVALID")

    def applies(self, context: RetainedLifecycleContext) -> bool:
        try:
            if len(context.expected_positions) != 2:
                return False
            lower, upper = context.expected_positions
            quantity = Decimal(STRUCTURAL_BULLISH_PILOT.quantity)
            lower_contract = parse_standard_option_contract_symbol(lower.symbol)
            upper_contract = parse_standard_option_contract_symbol(upper.symbol)
            return bool(
                context.account_role is AccountRole.SUBMISSION
                and context.thesis.thesis.underlying == "SPY"
                and context.thesis.thesis.thesis_code == self.strategy_id
                and Decimal(0) < lower.signed_quantity <= quantity
                and upper.signed_quantity == -lower.signed_quantity
                and context.approved_max_loss
                <= lower.signed_quantity * STRUCTURAL_BULLISH_PILOT.maximum_debit * 100
                and lower.multiplier == upper.multiplier == 100
                and lower_contract.root_symbol == upper_contract.root_symbol == "SPY"
                and lower_contract.expiration_date == upper_contract.expiration_date
                and lower_contract.right == upper_contract.right == "C"
                and lower_contract.strike_price < upper_contract.strike_price
                and len(context.lifecycle_transitions) == 1
                and context.lifecycle_transitions[0].action == "ENTRY"
            )
        except (AttributeError, OptionContractSymbolError, TypeError):
            return False

    def close_reason(
        self,
        context: RetainedLifecycleContext,
        *,
        executable_value: Decimal,
        trusted_at: datetime,
    ) -> str | None:
        if not self.applies(context):
            return None
        if (
            trusted_at.tzinfo is None
            or trusted_at.utcoffset() != timedelta(0)
            or not isinstance(executable_value, Decimal)
            or not executable_value.is_finite()
            or len(context.lifecycle_transitions) != 1
            or context.lifecycle_transitions[0].action != "ENTRY"
            or context.lifecycle_transitions[0].cashflow >= 0
        ):
            raise ValueError("STRUCTURAL_LIFECYCLE_STATE_INVALID")
        entry_debit = -context.lifecycle_transitions[0].cashflow
        if trusted_at >= self.mandatory_close_at:
            return STRUCTURAL_MANDATORY_BOUNDARY_CLOSE
        if executable_value >= entry_debit * self.profit_value_ratio:
            return STRUCTURAL_PROFIT_TARGET_CLOSE
        if executable_value <= entry_debit * self.stop_value_ratio:
            return STRUCTURAL_STOP_LIMIT_CLOSE
        return None


STRUCTURAL_BULLISH_BETA_LIFECYCLE = StructuralBullishBetaLifecycleContract()


@dataclass(frozen=True)
class RegisteredStructuralPilotLifecycleContract:
    strategy_id: str
    profit_value_ratio: Decimal = Decimal("1.30")
    stop_value_ratio: Decimal = Decimal("0.80")

    def __post_init__(self) -> None:
        if (
            self.strategy_id == STRUCTURAL_BULLISH_BETA_PILOT_ID
            or structural_pilot_profile(self.strategy_id) is None
            or self.profit_value_ratio != Decimal("1.30")
            or self.stop_value_ratio != Decimal("0.80")
        ):
            raise ValueError("STRUCTURAL_LIFECYCLE_CONTRACT_INVALID")

    def applies(self, context: RetainedLifecycleContext) -> bool:
        profile = structural_pilot_profile(self.strategy_id)
        if profile is None:
            return False
        try:
            if len(context.expected_positions) != 2:
                return False
            bought = next(
                position for position in context.expected_positions if position.signed_quantity > 0
            )
            sold = next(
                position for position in context.expected_positions if position.signed_quantity < 0
            )
            bought_contract = parse_standard_option_contract_symbol(bought.symbol)
            sold_contract = parse_standard_option_contract_symbol(sold.symbol)
            expected_right = "C" if profile.direction is OpportunityDirection.BULLISH else "P"
            expected_sold_strike = (
                bought_contract.strike_price + profile.width
                if profile.direction is OpportunityDirection.BULLISH
                else bought_contract.strike_price - profile.width
            )
            return bool(
                context.account_role is AccountRole.SUBMISSION
                and context.thesis.thesis.underlying == "SPY"
                and context.thesis.thesis.thesis_code == self.strategy_id
                and Decimal(0) < bought.signed_quantity <= Decimal(profile.quantity)
                and sold.signed_quantity == -bought.signed_quantity
                and context.approved_max_loss
                <= bought.signed_quantity * profile.maximum_debit * 100
                and bought.multiplier == sold.multiplier == 100
                and bought_contract.root_symbol == sold_contract.root_symbol == "SPY"
                and bought_contract.expiration_date == sold_contract.expiration_date
                and bought_contract.right == sold_contract.right == expected_right
                and sold_contract.strike_price == expected_sold_strike
                and len(context.lifecycle_transitions) == 1
                and context.lifecycle_transitions[0].action == "ENTRY"
            )
        except (AttributeError, OptionContractSymbolError, StopIteration, TypeError):
            return False

    def close_reason(
        self,
        context: RetainedLifecycleContext,
        *,
        executable_value: Decimal,
        trusted_at: datetime,
    ) -> str | None:
        if not self.applies(context):
            return None
        if (
            trusted_at.tzinfo is None
            or trusted_at.utcoffset() != timedelta(0)
            or not isinstance(executable_value, Decimal)
            or not executable_value.is_finite()
            or context.target_at.tzinfo is None
            or context.target_at.utcoffset() != timedelta(0)
            or len(context.lifecycle_transitions) != 1
            or context.lifecycle_transitions[0].action != "ENTRY"
            or context.lifecycle_transitions[0].cashflow >= 0
        ):
            raise ValueError("STRUCTURAL_LIFECYCLE_STATE_INVALID")
        entry_debit = -context.lifecycle_transitions[0].cashflow
        if trusted_at >= context.target_at:
            return STRUCTURAL_MANDATORY_BOUNDARY_CLOSE
        if executable_value >= entry_debit * self.profit_value_ratio:
            return STRUCTURAL_PROFIT_TARGET_CLOSE
        if executable_value <= entry_debit * self.stop_value_ratio:
            return STRUCTURAL_STOP_LIMIT_CLOSE
        return None


def structural_pilot_lifecycle(
    context: RetainedLifecycleContext,
) -> StructuralBullishBetaLifecycleContract | RegisteredStructuralPilotLifecycleContract | None:
    thesis_code = context.thesis.thesis.thesis_code
    if thesis_code == STRUCTURAL_BULLISH_BETA_PILOT_ID:
        contract = STRUCTURAL_BULLISH_BETA_LIFECYCLE
    elif structural_pilot_profile(thesis_code) is not None:
        contract = RegisteredStructuralPilotLifecycleContract(thesis_code)
    else:
        return None
    return contract if contract.applies(context) else None

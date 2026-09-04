from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

MAX_STRUCTURAL_OPTION_QUANTITY = 100
MAX_STRUCTURAL_APPROVED_RISK = Decimal("100000")
MAX_STRUCTURAL_LIFETIME_ENTRIES = 1000
MAX_STRUCTURAL_LIFETIME_RISK = Decimal("100000")

STRUCTURAL_PILOT_PER_CONTRACT_RISK = Decimal("225")
SUBMISSION_STRUCTURAL_OPTION_QUANTITY = 5
SUBMISSION_STRUCTURAL_APPROVED_RISK = (
    STRUCTURAL_PILOT_PER_CONTRACT_RISK * SUBMISSION_STRUCTURAL_OPTION_QUANTITY
)
SUBMISSION_STRUCTURAL_LIFETIME_ENTRIES = 10
SUBMISSION_STRUCTURAL_LIFETIME_RISK = (
    SUBMISSION_STRUCTURAL_APPROVED_RISK * SUBMISSION_STRUCTURAL_LIFETIME_ENTRIES
)

_HASH = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class EntryBudgetLimits:
    policy_hash: str
    equity_floor: Decimal
    maximum_lifetime_entries: int
    maximum_lifetime_risk: Decimal
    maximum_position_loss: Decimal
    maximum_entry_quantity: int

    def __post_init__(self) -> None:
        decimal_values = (
            self.equity_floor,
            self.maximum_lifetime_risk,
            self.maximum_position_loss,
        )
        if (
            _HASH.fullmatch(self.policy_hash) is None
            or any(
                not isinstance(value, Decimal) or not value.is_finite() for value in decimal_values
            )
            or self.equity_floor < 0
            or not 1 <= self.maximum_lifetime_entries <= MAX_STRUCTURAL_LIFETIME_ENTRIES
            or not Decimal(0) < self.maximum_position_loss <= MAX_STRUCTURAL_APPROVED_RISK
            or not self.maximum_position_loss
            <= self.maximum_lifetime_risk
            <= MAX_STRUCTURAL_LIFETIME_RISK
            or not 1 <= self.maximum_entry_quantity <= MAX_STRUCTURAL_OPTION_QUANTITY
        ):
            raise ValueError("ENTRY_BUDGET_LIMITS_INVALID")

    def validate_entry(
        self,
        *,
        policy_hash: str,
        equity: Decimal,
        entries_used: int,
        lifetime_risk: Decimal,
        proposed_risk: Decimal,
        proposed_quantity: int,
    ) -> None:
        if policy_hash != self.policy_hash:
            raise ValueError("ENTRY_POLICY_AUTHORITY_MISMATCH")
        if equity <= self.equity_floor:
            raise ValueError("ENTRY_EQUITY_FLOOR")
        if entries_used >= self.maximum_lifetime_entries:
            raise ValueError("ENTRY_COUNT_EXHAUSTED")
        if proposed_risk > self.maximum_position_loss:
            raise ValueError("ENTRY_POSITION_RISK_EXHAUSTED")
        if lifetime_risk + proposed_risk > self.maximum_lifetime_risk:
            raise ValueError("ENTRY_RISK_EXHAUSTED")
        if not 1 <= proposed_quantity <= self.maximum_entry_quantity:
            raise ValueError("ENTRY_QUANTITY_EXHAUSTED")

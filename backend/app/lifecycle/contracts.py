from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

_HASH = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class LifecycleLaunchAuthority:
    beta60: Decimal
    benchmark_symbol: str
    entry_boundary_at: datetime
    entry_policy_hash: str
    underlying_source_hash: str
    benchmark_source_hash: str
    completed_bar_source_hash: str

    def __post_init__(self) -> None:
        valid = (
            isinstance(self.beta60, Decimal)
            and self.beta60.is_finite()
            and Decimal(0) < self.beta60 <= Decimal(3)
            and self.benchmark_symbol == "QQQ"
            and isinstance(self.entry_boundary_at, datetime)
            and self.entry_boundary_at.tzinfo is not None
            and self.entry_boundary_at.utcoffset() == timedelta(0)
            and all(
                _HASH.fullmatch(value)
                for value in (
                    self.entry_policy_hash,
                    self.underlying_source_hash,
                    self.benchmark_source_hash,
                    self.completed_bar_source_hash,
                )
            )
        )
        if not valid:
            raise ValueError("LIFECYCLE_LAUNCH_AUTHORITY_INVALID")

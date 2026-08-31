from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from decimal import Decimal

from backend.app.contracts.v1.models import canonical_decimal

_OPTION_SYMBOL = re.compile(r"^[A-Z0-9]{1,64}$")


def option_position_fingerprint(
    positions: Sequence[tuple[str, Decimal, int]],
) -> str:
    if len(positions) > 64:
        raise ValueError("MANAGED_POSITION_FINGERPRINT_INVENTORY_INVALID")
    for symbol, signed_quantity, multiplier in positions:
        if (
            not isinstance(symbol, str)
            or _OPTION_SYMBOL.fullmatch(symbol) is None
            or not isinstance(signed_quantity, Decimal)
            or not signed_quantity.is_finite()
            or signed_quantity == 0
            or signed_quantity != signed_quantity.to_integral_value()
            or type(multiplier) is not int
            or multiplier != 100
        ):
            raise ValueError("MANAGED_POSITION_FINGERPRINT_INVENTORY_INVALID")
    material = [
        {
            "kind": "OPTION",
            "symbol": symbol,
            "signed_quantity": canonical_decimal(signed_quantity),
            "multiplier": multiplier,
        }
        for symbol, signed_quantity, multiplier in positions
    ]
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

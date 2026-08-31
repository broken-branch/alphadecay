from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

NON_STANDARD_CONTRACT_UNSUPPORTED = "NON_STANDARD_CONTRACT_UNSUPPORTED"
OPTION_CONTRACT_SYMBOL_MALFORMED = "OPTION_CONTRACT_SYMBOL_MALFORMED"

_SYMBOL = re.compile(
    r"^(?P<root>[A-Z]{1,6}|[A-Z]{1,5}[0-9])"
    r"(?P<expiration>\d{6})(?P<right>[CP])(?P<strike>\d{8})$"
)


class OptionContractSymbolError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class OptionContractSymbol:
    symbol: str
    root_symbol: str
    expiration_date: date
    right: Literal["C", "P"]
    strike_price: Decimal
    is_adjusted: bool

    def require_standard(self, underlying_symbol: str | None = None) -> OptionContractSymbol:
        if self.is_adjusted:
            raise OptionContractSymbolError(NON_STANDARD_CONTRACT_UNSUPPORTED)
        if underlying_symbol is not None and self.root_symbol != underlying_symbol:
            raise OptionContractSymbolError(OPTION_CONTRACT_SYMBOL_MALFORMED)
        return self


def parse_option_contract_symbol(symbol: object) -> OptionContractSymbol:
    if not isinstance(symbol, str):
        raise OptionContractSymbolError(OPTION_CONTRACT_SYMBOL_MALFORMED)
    match = _SYMBOL.fullmatch(symbol)
    if match is None:
        raise OptionContractSymbolError(OPTION_CONTRACT_SYMBOL_MALFORMED)
    try:
        expiration = datetime.strptime(match.group("expiration"), "%y%m%d").date()
    except ValueError as error:
        raise OptionContractSymbolError(OPTION_CONTRACT_SYMBOL_MALFORMED) from error
    root = match.group("root")
    return OptionContractSymbol(
        symbol=symbol,
        root_symbol=root,
        expiration_date=expiration,
        right=match.group("right"),
        strike_price=Decimal(match.group("strike")) / Decimal(1000),
        is_adjusted=root[-1].isdigit(),
    )


def parse_standard_option_contract_symbol(
    symbol: object,
    *,
    underlying_symbol: str | None = None,
) -> OptionContractSymbol:
    return parse_option_contract_symbol(symbol).require_standard(underlying_symbol)

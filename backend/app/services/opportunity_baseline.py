from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol
from uuid import UUID

from backend.app.alpaca.execution_evidence import baseline_account_fingerprint
from backend.app.persistence.opportunity_evidence import (
    OpportunityBaselineSeal,
    OpportunityPlanSpec,
    opportunity_plan_identity,
)
from backend.app.services.opportunity_bootstrap import (
    OpportunityBootstrapInput,
    development_opportunity_bootstrap_payload,
)

_MAX_ITEMS = 10_000
_MAX_ACTIVITY_PAGES = 100
_ACTIVITY_PAGE_SIZE = 100
_MAX_STRING = 4_096
_NUMBER = re.compile(r"^[+-]?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


class OpportunityBaselineCollectionError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ActivityPage:
    items: tuple[dict[str, object], ...]


class OpportunityBaselineProvider(Protocol):
    def get_account(self) -> object: ...

    def get_all_positions(self) -> object: ...

    def get_open_orders(self, *, limit: int) -> object: ...

    def get_activity_page(
        self,
        *,
        after: date,
        until: datetime,
        page_token: str | None,
        page_size: int,
    ) -> ActivityPage: ...


def collect_development_opportunity_bootstrap(
    plan: OpportunityPlanSpec,
    provider: OpportunityBaselineProvider,
    *,
    captured_at: datetime,
) -> dict[str, object]:
    captured_at = _utc(captured_at)
    if type(plan) is not OpportunityPlanSpec or captured_at < plan.frozen_at:
        raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_AUTHORITY_INVALID")

    account, account_id, account_fingerprint = _capture_account(provider, plan)
    created_at = _provider_datetime(account.get("created_at"))
    if created_at > captured_at:
        raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_CHRONOLOGY_INVALID")
    normalized_account = _normalize_account(account)

    positions = _collection(_read(provider.get_all_positions), "POSITION")
    orders = _collection(_read(lambda: provider.get_open_orders(limit=500)), "ORDER")
    if len(orders) >= 500:
        raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_ORDERS_INCOMPLETE")
    normalized_positions = _normalize_positions(positions)
    normalized_orders = _normalize_orders(orders)

    activity, page_count = _collect_activity(
        provider,
        account_id=account_id,
        account_fingerprint=account_fingerprint,
        created_at=created_at,
        captured_at=captured_at,
    )
    final_orders = _collection(_read(lambda: provider.get_open_orders(limit=500)), "ORDER")
    if len(final_orders) >= 500:
        raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_ORDERS_INCOMPLETE")
    final_positions = _collection(_read(provider.get_all_positions), "POSITION")
    final_account, final_account_id, final_fingerprint = _capture_account(provider, plan)
    final_activity, final_page_count = _collect_activity(
        provider,
        account_id=account_id,
        account_fingerprint=account_fingerprint,
        created_at=created_at,
        captured_at=captured_at,
    )
    if (
        _normalize_orders(final_orders) != normalized_orders
        or _normalize_positions(final_positions) != normalized_positions
        or _normalize_account(final_account) != normalized_account
        or final_account_id != account_id
        or final_fingerprint != account_fingerprint
    ):
        raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_BOOK_CHANGED")
    if final_activity != activity or final_page_count != page_count:
        raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_HISTORY_CHANGED")
    if normalized_positions or normalized_orders:
        raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_BOOK_NOT_CLEAN")

    account_hash = _hash("alphadecay.opportunity-baseline.account-source.v1", normalized_account)
    positions_hash = _hash(
        "alphadecay.opportunity-baseline.positions-source.v1", normalized_positions
    )
    orders_hash = _hash("alphadecay.opportunity-baseline.orders-source.v1", normalized_orders)
    activity_hash = _hash(
        "alphadecay.opportunity-baseline.activity-source.v1",
        {
            "after": (created_at - timedelta(days=1)).date().isoformat(),
            "until": captured_at.isoformat(),
            "page_count": page_count,
            "terminal_page_seen": True,
            "items": activity,
        },
    )
    book_hash = _hash(
        "alphadecay.opportunity-baseline.book.v1",
        {
            "account_fingerprint": account_fingerprint,
            "account_source_hash": account_hash,
            "positions_source_hash": positions_hash,
            "orders_source_hash": orders_hash,
        },
    )
    history_hash = _hash(
        "alphadecay.opportunity-baseline.history.v1",
        {
            "account_fingerprint": account_fingerprint,
            "activity_source_hash": activity_hash,
            "activity_count": len(activity),
        },
    )
    plan_id, _ = opportunity_plan_identity(plan)
    seal = OpportunityBaselineSeal(
        plan_id=plan_id,
        account_fingerprint=account_fingerprint,
        account_source_hash=account_hash,
        positions_manifest=normalized_positions,
        positions_source_hash=positions_hash,
        orders_manifest=normalized_orders,
        orders_source_hash=orders_hash,
        activity_manifest=activity,
        activity_source_hash=activity_hash,
        book_hash=book_hash,
        history_hash=history_hash,
        captured_at=captured_at,
    )
    return development_opportunity_bootstrap_payload(OpportunityBootstrapInput(plan, seal))


def _capture_account(
    provider: OpportunityBaselineProvider, plan: OpportunityPlanSpec
) -> tuple[dict[str, object], UUID, str]:
    account = _record(_read(provider.get_account))
    account_reference = _uuid(account.get("id"), "OPPORTUNITY_BASELINE_ACCOUNT_INVALID")
    account_fingerprint = baseline_account_fingerprint(account_reference)
    if account_fingerprint != plan.request_contract.expected_account_fingerprint:
        raise OpportunityBaselineCollectionError("DEVELOPMENT_ACCOUNT_MISMATCH")
    return account, account_reference, account_fingerprint


def _collect_activity(
    provider: OpportunityBaselineProvider,
    *,
    account_id: UUID,
    account_fingerprint: str,
    created_at: datetime,
    captured_at: datetime,
) -> tuple[tuple[dict[str, object], ...], int]:
    after = (created_at - timedelta(days=1)).date()
    page_token: str | None = None
    seen_tokens: set[str] = set()
    seen_ids: set[str] = set()
    result: list[dict[str, object]] = []
    last_event_day: date | None = None
    last_exact_by_day: dict[date, datetime] = {}
    for page_index in range(_MAX_ACTIVITY_PAGES):
        try:
            page = provider.get_activity_page(
                after=after,
                until=captured_at,
                page_token=page_token,
                page_size=_ACTIVITY_PAGE_SIZE,
            )
        except OpportunityBaselineCollectionError:
            raise
        except (AttributeError, OSError, TypeError, ValueError):
            raise OpportunityBaselineCollectionError(
                "OPPORTUNITY_BASELINE_PROVIDER_READ_FAILED"
            ) from None
        if type(page) is not ActivityPage or type(page.items) is not tuple:
            raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_ACTIVITY_INVALID")
        if len(page.items) > _ACTIVITY_PAGE_SIZE:
            raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_ACTIVITY_INVALID")
        for value in page.items:
            row = _record(value)
            activity_reference = _activity_id(row.get("id"))
            if activity_reference in seen_ids:
                raise OpportunityBaselineCollectionError(
                    "OPPORTUNITY_BASELINE_ACTIVITY_NONMONOTONIC"
                )
            observed_account = row.get("account_id")
            if observed_account not in {None, ""} and _uuid(
                observed_account, "OPPORTUNITY_BASELINE_ACTIVITY_INVALID"
            ) != account_id:
                raise OpportunityBaselineCollectionError("DEVELOPMENT_ACCOUNT_MISMATCH")
            event_day, exact_time = _activity_time(row)
            if (
                event_day < created_at.date()
                or event_day > captured_at.date()
                or exact_time is not None
                and (exact_time < created_at or exact_time > captured_at)
            ):
                raise OpportunityBaselineCollectionError(
                    "OPPORTUNITY_BASELINE_CHRONOLOGY_INVALID"
                )
            if last_event_day is not None and event_day < last_event_day:
                raise OpportunityBaselineCollectionError(
                    "OPPORTUNITY_BASELINE_ACTIVITY_NONMONOTONIC"
                )
            if exact_time is not None:
                previous = last_exact_by_day.get(event_day)
                if previous is not None and exact_time < previous:
                    raise OpportunityBaselineCollectionError(
                        "OPPORTUNITY_BASELINE_ACTIVITY_NONMONOTONIC"
                    )
                last_exact_by_day[event_day] = exact_time
            last_event_day = event_day
            seen_ids.add(activity_reference)
            result.append(_normalize_activity(row, account_fingerprint))
            if len(result) > _MAX_ITEMS:
                raise OpportunityBaselineCollectionError(
                    "OPPORTUNITY_BASELINE_ACTIVITY_INCOMPLETE"
                )
        if len(page.items) < _ACTIVITY_PAGE_SIZE:
            result.sort(key=_canonical)
            return tuple(result), page_index + 1
        next_cursor = _activity_id(page.items[-1].get("id"))
        if next_cursor == page_token or next_cursor in seen_tokens:
            raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_ACTIVITY_INCOMPLETE")
        seen_tokens.add(next_cursor)
        page_token = next_cursor
    raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_ACTIVITY_INCOMPLETE")


def _read(call: Callable[[], object]) -> object:
    try:
        return call()
    except OpportunityBaselineCollectionError:
        raise
    except (AttributeError, OSError, TypeError, ValueError):
        raise OpportunityBaselineCollectionError(
            "OPPORTUNITY_BASELINE_PROVIDER_READ_FAILED"
        ) from None


def _collection(value: object, kind: str) -> tuple[dict[str, object], ...]:
    if type(value) not in {list, tuple} or len(value) > _MAX_ITEMS:
        raise OpportunityBaselineCollectionError(f"OPPORTUNITY_BASELINE_{kind}_INCOMPLETE")
    return tuple(_record(item) for item in value)


def _record(value: object) -> dict[str, object]:
    if type(value) is dict:
        row = value.copy()
    else:
        dump = getattr(value, "model_dump", None)
        if not callable(dump):
            raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_PROVIDER_RECORD_INVALID")
        try:
            row = dump(mode="json")
        except (TypeError, ValueError):
            raise OpportunityBaselineCollectionError(
                "OPPORTUNITY_BASELINE_PROVIDER_RECORD_INVALID"
            ) from None
    if type(row) is not dict or len(row) > 256 or any(type(key) is not str for key in row):
        raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_PROVIDER_RECORD_INVALID")
    return row


def _normalize_account(row: Mapping[str, object]) -> dict[str, object]:
    blocked = (
        "trading_blocked",
        "transfers_blocked",
        "account_blocked",
        "trade_suspended_by_user",
    )
    if _string(row.get("status"), required=True) != "ACTIVE" or any(
        _boolean(row.get(key)) for key in blocked
    ):
        raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_ACCOUNT_BLOCKED")
    return {
        "status": "ACTIVE",
        "currency": _string(row.get("currency")),
        "created_at": _provider_datetime(row.get("created_at")).isoformat(),
        "account_number_hash": hashlib.sha256(
            _string(row.get("account_number")).encode()
        ).hexdigest(),
        **{
            key: _number(row.get(key), required=key == "equity")
            for key in (
                "equity",
                "cash",
                "last_equity",
                "portfolio_value",
                "buying_power",
                "options_buying_power",
                "options_approved_level",
                "options_trading_level",
                "pending_transfer_in",
                "pending_transfer_out",
            )
        },
        **{key: _boolean(row.get(key)) for key in blocked},
    }


def _normalize_positions(rows: Sequence[Mapping[str, object]]) -> tuple[dict[str, object], ...]:
    items = []
    for row in rows:
        items.append(
            {
                "asset_id": _string(row.get("asset_id"), required=True),
                "symbol": _string(row.get("symbol"), required=True),
                "asset_class": _string(row.get("asset_class"), required=True),
                "side": _string(row.get("side"), required=True),
                "qty": _number(row.get("qty"), required=True),
            }
        )
    return _unique_sorted(items, "asset_id")


def _normalize_orders(rows: Sequence[Mapping[str, object]]) -> tuple[dict[str, object], ...]:
    items = []
    for row in rows:
        source_client_reference = _string(row.get("client_order_id"))
        items.append(
            {
                "id": _string(row.get("id"), required=True),
                "client_order_id": source_client_reference,
                "symbol": _string(row.get("symbol")),
                "status": _string(row.get("status"), required=True),
                "order_class": _string(row.get("order_class")),
                "side": _string(row.get("side")),
                "qty": _number(row.get("qty")),
                "filled_qty": _number(row.get("filled_qty")),
            }
        )
    return _unique_sorted(items, "id")


def _normalize_activity(row: Mapping[str, object], account_fingerprint: str) -> dict[str, object]:
    source_order_reference = _string(row.get("order_id"))
    source_client_reference = _string(row.get("client_order_id"))
    return {
        "id": _activity_id(row.get("id")),
        "account_fingerprint": account_fingerprint,
        "activity_type": _string(row.get("activity_type"), required=True),
        "transaction_time": _optional_datetime(row.get("transaction_time")),
        "date": _optional_date(row.get("date")),
        "symbol": _string(row.get("symbol")),
        "side": _string(row.get("side")),
        "order_id": source_order_reference,
        "client_order_id": source_client_reference,
        "description": _string(row.get("description")),
        "qty": _number(row.get("qty")),
        "price": _number(row.get("price")),
        "net_amount": _number(row.get("net_amount")),
    }


def _unique_sorted(items: list[dict[str, object]], key: str) -> tuple[dict[str, object], ...]:
    identities = [_string(item[key], required=True) for item in items]
    if len(set(identities)) != len(identities):
        raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_PROVIDER_DUPLICATE_ITEM")
    return tuple(sorted(items, key=lambda item: _canonical(item)))


def _activity_time(row: Mapping[str, object]) -> tuple[date, datetime | None]:
    transaction_time = row.get("transaction_time")
    if transaction_time not in {None, ""}:
        exact_time = _provider_datetime(transaction_time)
        activity_date = row.get("date")
        if activity_date not in {None, ""} and _activity_date(activity_date) != exact_time.date():
            raise OpportunityBaselineCollectionError(
                "OPPORTUNITY_BASELINE_CHRONOLOGY_INVALID"
            )
        return exact_time.date(), exact_time
    value = row.get("date")
    return _activity_date(value), None


def _activity_id(value: object) -> str:
    if type(value) is not str:
        raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_ACTIVITY_INVALID")
    try:
        return _string(value, required=True)
    except OpportunityBaselineCollectionError:
        raise OpportunityBaselineCollectionError(
            "OPPORTUNITY_BASELINE_ACTIVITY_INVALID"
        ) from None


def _activity_date(value: object) -> date:
    try:
        parsed = value if type(value) is date else date.fromisoformat(_string(value, required=True))
    except ValueError:
        raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_ACTIVITY_INVALID") from None
    return parsed


def _provider_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif type(value) is str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise OpportunityBaselineCollectionError(
                "OPPORTUNITY_BASELINE_CHRONOLOGY_INVALID"
            ) from None
    else:
        raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_CHRONOLOGY_INVALID")
    return _utc(parsed)


def _optional_datetime(value: object) -> str:
    return "" if value in {None, ""} else _provider_datetime(value).isoformat()


def _optional_date(value: object) -> str:
    if value in {None, ""}:
        return ""
    try:
        return (value if type(value) is date else date.fromisoformat(_string(value))).isoformat()
    except ValueError:
        raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_ACTIVITY_INVALID") from None


def _utc(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_CHRONOLOGY_INVALID")
    return value.astimezone(UTC)


def _uuid(value: object, code: str) -> UUID:
    try:
        return value if type(value) is UUID else UUID(_string(value, required=True))
    except ValueError:
        raise OpportunityBaselineCollectionError(code) from None


def _string(value: object, *, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if type(value) not in {str, int}:
        raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_PROVIDER_RECORD_INVALID")
    result = str(value)
    if (required and not result) or len(result) > _MAX_STRING:
        raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_PROVIDER_RECORD_INVALID")
    return result


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_PROVIDER_RECORD_INVALID")
    return value


def _number(value: object, *, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if type(value) not in {str, int, Decimal} or type(value) is bool:
        raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_PROVIDER_RECORD_INVALID")
    encoded = str(value)
    if len(encoded) > 72 or _NUMBER.fullmatch(encoded) is None:
        raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_PROVIDER_RECORD_INVALID")
    try:
        number = Decimal(encoded)
    except InvalidOperation:
        raise OpportunityBaselineCollectionError(
            "OPPORTUNITY_BASELINE_PROVIDER_RECORD_INVALID"
        ) from None
    if not number.is_finite():
        raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_PROVIDER_RECORD_INVALID")
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"", "-0", "+0"} else rendered


def _hash(domain: str, value: object) -> str:
    return hashlib.sha256(domain.encode() + b"\0" + _canonical(value)).hexdigest()


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError):
        raise OpportunityBaselineCollectionError(
            "OPPORTUNITY_BASELINE_PROVIDER_RECORD_INVALID"
        ) from None

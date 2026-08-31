from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from datetime import date as CalendarDate
from decimal import Decimal, InvalidOperation
from typing import Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict

from backend.app.execution import ActivityItem, ActivityPaginationEvidence, ActivityType

_PAPER_HOST = "paper-api.alpaca.markets"
_ACTIVITY_PATH = "/v2/account/activities"
_OPTION_ACTIVITY_TYPES = {"OPASN", "OPTRD", "OPEXC", "OPEXP", "OPXRC"}
_OPTION_DOMAIN_TYPES = {
    ActivityType.OPASN,
    ActivityType.OPTRD,
    ActivityType.OPEXC,
    ActivityType.OPEXP,
    ActivityType.OPXRC,
}
_CASH_ACTIVITY_TYPES = {
    "CSD": ActivityType.DEPOSIT,
    "CSW": ActivityType.WITHDRAWAL,
    "ACATC": ActivityType.TRANSFER,
    "ACATS": ActivityType.TRANSFER,
    "FOPT": ActivityType.TRANSFER,
    "JNL": ActivityType.JOURNAL,
    "JNLC": ActivityType.JOURNAL,
    "JNLS": ActivityType.JOURNAL,
}
_CORPORATE_ACTION_TYPES = {
    "MA",
    "NC",
    "REORG",
    "SPLIT",
    "SPINOFF",
    "STOCK_SPLIT",
    "STOCK_SPINOFF",
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ActivityReadError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class InitialFundingContext:
    captured_at: datetime
    equity: Decimal
    account_fingerprint: str
    activity_id_hash: str

    def __post_init__(self) -> None:
        if (
            self.captured_at.tzinfo is None
            or self.captured_at.utcoffset() is None
            or not isinstance(self.equity, Decimal)
            or not self.equity.is_finite()
            or self.equity <= 0
            or len(self.account_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in self.account_fingerprint)
            or len(self.activity_id_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.activity_id_hash)
        ):
            raise ValueError("INITIAL_FUNDING_CONTEXT_INVALID")


class _ActivityPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str
    activity_type: str
    transaction_time: datetime | None = None
    date: CalendarDate | None = None
    net_amount: Decimal | None = None
    symbol: str | None = None
    qty: Decimal | None = None
    price: Decimal | None = None
    side: Literal["buy", "sell"] | None = None
    order_id: str | None = None
    client_order_id: str | None = None


class AccountActivitiesAdapter:
    def __init__(
        self,
        client: httpx.Client,
        *,
        base_url: str,
        api_key: str,
        secret_key: str,
        page_size: int = 100,
        max_pages: int = 100,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        parsed = urlparse(base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != _PAPER_HOST
            or parsed.port is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
        ):
            raise ActivityReadError("ACTIVITY_HOST_FORBIDDEN")
        if not 1 <= page_size <= 100:
            raise ActivityReadError("ACTIVITY_PAGE_SIZE_INVALID")
        if not 1 <= max_pages <= 100:
            raise ActivityReadError("ACTIVITY_PAGE_LIMIT_INVALID")
        self._client = client
        self._url = f"https://{_PAPER_HOST}{_ACTIVITY_PATH}"
        self._headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": secret_key,
            "Accept": "application/json",
        }
        self._page_size = page_size
        self._max_pages = max_pages
        self._clock = clock

    def list_activities(self, since: datetime) -> tuple[dict[str, object], ...]:
        _require_aware(since, "ACTIVITY_SINCE_TIMEZONE_MISSING")
        until = self._clock()
        _require_aware(until, "ACTIVITY_TIMEZONE_MISSING")
        payloads, _ = self._list_payloads(since=since, until=until)
        return tuple(self._legacy_payload(item) for item in payloads)

    def collect(
        self,
        *,
        since: datetime,
        until: datetime,
        provider_to_client: Mapping[str, str],
        initial_funding: InitialFundingContext | None = None,
        observed_account_fingerprint: str | None = None,
    ) -> tuple[tuple[ActivityItem, ...], ActivityPaginationEvidence]:
        _require_aware(since, "ACTIVITY_TIMEZONE_MISSING")
        _require_aware(until, "ACTIVITY_TIMEZONE_MISSING")
        if since > until:
            raise ActivityReadError("ACTIVITY_WINDOW_INVALID")
        if until - since < timedelta(hours=24):
            raise ActivityReadError("ACTIVITY_VISIBILITY_HORIZON_INCOMPLETE")
        if initial_funding is not None and (
            observed_account_fingerprint != initial_funding.account_fingerprint
        ):
            raise ActivityReadError("INITIAL_FUNDING_ACCOUNT_MISMATCH")
        payloads, page_count = self._list_payloads(since=since, until=until)
        established_at = self._clock()
        _require_aware(established_at, "ACTIVITY_TIMEZONE_MISSING")
        if established_at < until:
            raise ActivityReadError("ACTIVITY_WINDOW_INVALID")
        items = tuple(
            self._activity_item(
                payload,
                provider_to_client=provider_to_client,
                initial_funding=initial_funding,
            )
            for payload in payloads
        )
        canonical = tuple(sorted(items, key=lambda item: item.activity_id_hash))
        return canonical, ActivityPaginationEvidence(
            requested_start=since.astimezone(UTC),
            requested_end=until.astimezone(UTC),
            retrieved_through=until.astimezone(UTC),
            established_at=established_at.astimezone(UTC),
            page_count=page_count,
            terminal_page_seen=True,
            visibility_complete_through=(until - timedelta(hours=24)).astimezone(UTC),
            visibility_horizon=timedelta(hours=24),
        )

    def _list_payloads(
        self, *, since: datetime, until: datetime
    ) -> tuple[tuple[_ActivityPayload, ...], int]:
        collected: list[_ActivityPayload] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()
        seen_ids: set[str] = set()
        last_day: CalendarDate | None = None
        last_exact_by_day: dict[CalendarDate, datetime] = {}
        for page_index in range(self._max_pages):
            payloads = self._get_page(since=since, until=until, page_token=page_token)
            for payload in payloads:
                event_day, exact_time = _provider_time(payload)
                if not payload.id.strip() or payload.id in seen_ids:
                    raise ActivityReadError("ACTIVITY_PAGINATION_NONMONOTONIC")
                if last_day is not None and event_day < last_day:
                    raise ActivityReadError("ACTIVITY_PAGINATION_NONMONOTONIC")
                if exact_time is not None:
                    previous = last_exact_by_day.get(event_day)
                    if previous is not None and exact_time < previous:
                        raise ActivityReadError("ACTIVITY_PAGINATION_NONMONOTONIC")
                    last_exact_by_day[event_day] = exact_time
                seen_ids.add(payload.id)
                last_day = event_day
                if _in_window(payload, since, until):
                    collected.append(payload)
            if len(payloads) < self._page_size:
                return tuple(collected), page_index + 1
            next_token = payloads[-1].id
            if not next_token or next_token in seen_tokens:
                raise ActivityReadError("ACTIVITY_PAGINATION_LOOP")
            seen_tokens.add(next_token)
            page_token = next_token
        raise ActivityReadError("ACTIVITY_PAGE_LIMIT_EXCEEDED")

    def _get_page(
        self, *, since: datetime, until: datetime, page_token: str | None
    ) -> tuple[_ActivityPayload, ...]:
        params = {
            "after": (since - timedelta(days=1)).date().isoformat(),
            "until": until.isoformat(),
            "direction": "asc",
            "page_size": str(self._page_size),
        }
        if page_token is not None:
            params["page_token"] = page_token
        try:
            response = self._client.get(
                self._url,
                params=params,
                headers=self._headers,
                follow_redirects=False,
            )
        except httpx.TimeoutException as exc:
            raise ActivityReadError("ACTIVITY_TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise ActivityReadError("ACTIVITY_PROVIDER_ERROR") from exc
        if response.status_code != 200:
            raise ActivityReadError(f"ACTIVITY_HTTP_{response.status_code}")
        try:
            body = response.json()
            if not isinstance(body, list):
                raise TypeError
            return tuple(_ActivityPayload.model_validate(item) for item in body)
        except (TypeError, ValueError) as exc:
            raise ActivityReadError("ACTIVITY_SCHEMA_INVALID") from exc

    @staticmethod
    def _legacy_payload(payload: _ActivityPayload) -> dict[str, object]:
        occurred_at, time_quality = _normalized_time(payload)
        return {
            "activity_id": payload.id,
            "activity_type": _normalized_type(payload, None).value,
            "transaction_time": occurred_at.isoformat().replace("+00:00", "Z"),
            "time_quality": time_quality,
            "symbol": payload.symbol,
            "quantity": str(payload.qty) if payload.qty is not None else None,
            "net_amount": str(payload.net_amount) if payload.net_amount is not None else None,
            "price": str(payload.price) if payload.price is not None else None,
            "side": payload.side,
            "provider_order_id": payload.order_id,
            "client_order_id": payload.client_order_id,
        }

    @staticmethod
    def _activity_item(
        payload: _ActivityPayload,
        *,
        provider_to_client: Mapping[str, str],
        initial_funding: InitialFundingContext | None,
    ) -> ActivityItem:
        activity_type = _normalized_type(payload, initial_funding)
        occurred_at, time_quality = _normalized_time(payload)
        provider_order_id = payload.order_id
        client_order_id = payload.client_order_id
        if provider_order_id is not None:
            mapped = provider_to_client.get(provider_order_id)
            if client_order_id is not None and mapped is not None and client_order_id != mapped:
                raise ActivityReadError("ACTIVITY_ORDER_LINEAGE_MISMATCH")
            client_order_id = client_order_id or mapped
        if activity_type == ActivityType.FILL and (
            provider_order_id is None or client_order_id is None
        ):
            raise ActivityReadError("ACTIVITY_ORDER_LINEAGE_INCOMPLETE")
        try:
            return ActivityItem(
                activity_id_hash=_activity_id_hash(payload.id),
                activity_type=activity_type,
                occurred_at=occurred_at,
                symbol=payload.symbol,
                signed_quantity=_signed_activity_quantity(payload, activity_type),
                provider_order_id=provider_order_id,
                client_order_id=client_order_id,
                time_quality=time_quality,
                provider_activity_type=payload.activity_type,
            )
        except ValueError as exc:
            raise ActivityReadError("ACTIVITY_SCHEMA_INVALID") from exc


class LifecycleAccountActivitiesAdapter:
    def __init__(
        self,
        reader: AccountActivitiesAdapter,
        *,
        expected_account_fingerprint: str,
    ) -> None:
        if not _is_hash(expected_account_fingerprint):
            raise ActivityReadError("ACTIVITY_ACCOUNT_FINGERPRINT_INVALID")
        self._reader = reader
        self._expected_account_fingerprint = expected_account_fingerprint

    def collect(
        self,
        *,
        since: datetime,
        until: datetime,
        provider_to_client: Mapping[str, str],
        initial_funding: InitialFundingContext,
        observed_account_fingerprint: str,
    ) -> tuple[tuple[ActivityItem, ...], ActivityPaginationEvidence]:
        self._require_account(observed_account_fingerprint)
        return self._reader.collect(
            since=since,
            until=until,
            provider_to_client=provider_to_client,
            initial_funding=initial_funding,
            observed_account_fingerprint=observed_account_fingerprint,
        )

    def collect_lifecycle(
        self,
        *,
        since: datetime,
        until: datetime,
        observed_account_fingerprint: str,
        known_activity_hashes: tuple[str, ...],
    ) -> tuple[tuple[ActivityItem, ...], ActivityPaginationEvidence]:
        self._require_account(observed_account_fingerprint)
        if (
            known_activity_hashes != tuple(sorted(known_activity_hashes))
            or len(set(known_activity_hashes)) != len(known_activity_hashes)
            or any(not _is_hash(value) for value in known_activity_hashes)
        ):
            raise ActivityReadError("ACTIVITY_KNOWN_HISTORY_INVALID")
        activities, pagination = self._reader.collect(
            since=since,
            until=until,
            provider_to_client={},
        )
        observed_hashes = tuple(
            sorted(
                activity.activity_id_hash
                for activity in activities
                if activity.activity_type is not ActivityType.INITIAL_FUNDING
            )
        )
        if observed_hashes != known_activity_hashes:
            raise ActivityReadError("ACTIVITY_KNOWN_HISTORY_MISMATCH")
        return activities, pagination

    def _require_account(self, observed_account_fingerprint: str) -> None:
        if observed_account_fingerprint != self._expected_account_fingerprint:
            raise ActivityReadError("ACTIVITY_ACCOUNT_FINGERPRINT_MISMATCH")


def _provider_time(payload: _ActivityPayload) -> tuple[CalendarDate, datetime | None]:
    if payload.activity_type == "FILL":
        if (
            payload.transaction_time is None
            or payload.transaction_time.tzinfo is None
            or payload.transaction_time.utcoffset() is None
            or payload.date is not None
        ):
            raise ActivityReadError("ACTIVITY_SCHEMA_INVALID")
        value = payload.transaction_time.astimezone(UTC)
        return value.date(), value
    if payload.date is None or payload.transaction_time is not None:
        raise ActivityReadError("ACTIVITY_SCHEMA_INVALID")
    return payload.date, None


def _normalized_time(payload: _ActivityPayload) -> tuple[datetime, str]:
    event_day, exact_time = _provider_time(payload)
    if exact_time is not None:
        return exact_time, "EXACT_TRANSACTION_TIME"
    return datetime.combine(event_day, time.min, tzinfo=UTC), "DATE_ONLY"


def _in_window(payload: _ActivityPayload, since: datetime, until: datetime) -> bool:
    event_day, exact_time = _provider_time(payload)
    if exact_time is not None:
        if exact_time > until.astimezone(UTC):
            raise ActivityReadError("ACTIVITY_WINDOW_MISMATCH")
        return exact_time >= since.astimezone(UTC)
    if event_day > until.astimezone(UTC).date():
        raise ActivityReadError("ACTIVITY_WINDOW_MISMATCH")
    return event_day >= since.astimezone(UTC).date()


def _normalized_type(
    payload: _ActivityPayload, initial_funding: InitialFundingContext | None
) -> ActivityType:
    raw_type = payload.activity_type.strip().upper()
    if not raw_type or raw_type != payload.activity_type:
        raise ActivityReadError("ACTIVITY_SCHEMA_INVALID")
    if raw_type == "FILL":
        return ActivityType.FILL
    if raw_type in _OPTION_ACTIVITY_TYPES:
        return ActivityType(raw_type)
    if raw_type == "JNLC" and initial_funding is not None:
        occurred_at, _ = _normalized_time(payload)
        if (
            occurred_at.date() <= initial_funding.captured_at.astimezone(UTC).date()
            and payload.net_amount == initial_funding.equity
            and _activity_id_hash(payload.id) == initial_funding.activity_id_hash
        ):
            return ActivityType.INITIAL_FUNDING
    if raw_type in _CASH_ACTIVITY_TYPES:
        return _CASH_ACTIVITY_TYPES[raw_type]
    if raw_type.startswith("DIV"):
        return ActivityType.DIVIDEND
    if "FEE" in raw_type:
        return ActivityType.FEE
    if "INT" in raw_type:
        return ActivityType.INTEREST
    if raw_type in _CORPORATE_ACTION_TYPES:
        return ActivityType.CORPORATE_ACTION
    return ActivityType.UNKNOWN_CASH


def _signed_activity_quantity(
    payload: _ActivityPayload, activity_type: ActivityType
) -> Decimal | None:
    raw = (
        payload.qty
        if activity_type in {ActivityType.FILL, *_OPTION_DOMAIN_TYPES}
        else payload.net_amount
    )
    if raw is None:
        if activity_type in {
            ActivityType.FILL,
            *_OPTION_DOMAIN_TYPES,
            ActivityType.INITIAL_FUNDING,
        }:
            raise ActivityReadError("ACTIVITY_SCHEMA_INVALID")
        return None
    try:
        quantity = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ActivityReadError("ACTIVITY_SCHEMA_INVALID") from exc
    if not quantity.is_finite() or quantity == 0:
        raise ActivityReadError("ACTIVITY_SCHEMA_INVALID")
    if activity_type == ActivityType.FILL:
        if (
            payload.side not in {"buy", "sell"}
            or quantity < 0
            or payload.symbol is None
            or payload.price is None
            or not payload.price.is_finite()
            or payload.price <= 0
        ):
            raise ActivityReadError("ACTIVITY_SCHEMA_INVALID")
        return -quantity if payload.side == "sell" else quantity
    if payload.side is not None:
        if quantity < 0:
            raise ActivityReadError("ACTIVITY_SCHEMA_INVALID")
        return -quantity if payload.side == "sell" else quantity
    return quantity


def _require_aware(value: datetime, code: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ActivityReadError(code)


def _activity_id_hash(activity_id: str) -> str:
    return hashlib.sha256(activity_id.encode()).hexdigest()


def _is_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )

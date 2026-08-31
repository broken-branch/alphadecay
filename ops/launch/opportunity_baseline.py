from __future__ import annotations

import argparse
import json
import os
import stat
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol

import httpx
from alpaca.common.enums import Sort
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest
from requests import ConnectionError as RequestsConnectionError
from requests import Timeout as RequestsTimeout

from backend.app.persistence.opportunity_evidence import opportunity_baseline_identity
from backend.app.services.opportunity_baseline import (
    ActivityPage,
    OpportunityBaselineCollectionError,
    collect_development_opportunity_bootstrap,
)
from backend.app.services.opportunity_bootstrap import (
    OpportunityBootstrapError,
    parse_development_opportunity_bootstrap,
    parse_development_opportunity_plan,
)

_PAPER_ENDPOINT = "https://paper-api.alpaca.markets"
_ACTIVITY_URL = f"{_PAPER_ENDPOINT}/v2/account/activities"
_INPUT_LIMIT = 1024 * 1024


class TradingReadClient(Protocol):
    def get_account(self) -> object: ...

    def get_all_positions(self) -> object: ...

    def get_orders(self, filter: GetOrdersRequest | None = None) -> object: ...


class ActivityHttpClient(Protocol):
    def get(self, url: str, **kwargs: object) -> object: ...


@dataclass(frozen=True)
class _Credentials:
    api_key: str
    secret_key: str


class AlpacaOpportunityBaselineProvider:
    def __init__(
        self,
        trading: TradingReadClient,
        activity_http: ActivityHttpClient,
        credentials: _Credentials,
    ) -> None:
        self._trading = trading
        self._activity_http = activity_http
        self._headers = {
            "APCA-API-KEY-ID": credentials.api_key,
            "APCA-API-SECRET-KEY": credentials.secret_key,
            "Accept": "application/json",
        }

    def get_account(self) -> object:
        return self._trading_read(self._trading.get_account)

    def get_all_positions(self) -> object:
        return self._trading_read(self._trading.get_all_positions)

    def get_open_orders(self, *, limit: int) -> object:
        if limit != 500:
            raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_ORDERS_INCOMPLETE")
        return self._trading_read(
            lambda: self._trading.get_orders(
                GetOrdersRequest(
                    status=QueryOrderStatus.OPEN,
                    limit=limit,
                    direction=Sort.ASC,
                    nested=True,
                )
            )
        )

    def get_activity_page(
        self,
        *,
        after: date,
        until: datetime,
        page_token: str | None,
        page_size: int,
    ) -> ActivityPage:
        params = {
            "after": after.isoformat(),
            "until": until.isoformat(),
            "direction": "asc",
            "page_size": str(page_size),
        }
        if page_token is not None:
            params["page_token"] = page_token
        try:
            response = self._activity_http.get(
                _ACTIVITY_URL,
                params=params,
                headers=self._headers,
                follow_redirects=False,
            )
        except (httpx.HTTPError, OSError, TypeError, ValueError):
            raise OpportunityBaselineCollectionError(
                "OPPORTUNITY_BASELINE_PROVIDER_READ_FAILED"
            ) from None
        if getattr(response, "history", ()) or getattr(response, "status_code", None) != 200:
            raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_ACTIVITY_INVALID")
        try:
            payload = response.json()
        except (TypeError, ValueError):
            raise OpportunityBaselineCollectionError(
                "OPPORTUNITY_BASELINE_ACTIVITY_INVALID"
            ) from None
        if type(payload) is not list or any(type(item) is not dict for item in payload):
            raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_ACTIVITY_INVALID")
        return ActivityPage(tuple(payload))

    @staticmethod
    def _trading_read(call: Callable[[], object]) -> object:
        try:
            return call()
        except (APIError, RequestsConnectionError, RequestsTimeout, OSError, TypeError, ValueError):
            raise OpportunityBaselineCollectionError(
                "OPPORTUNITY_BASELINE_PROVIDER_READ_FAILED"
            ) from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture one complete read-only DEVELOPMENT opportunity baseline"
    )
    parser.add_argument("--plan-file", required=True, type=Path)
    parser.add_argument("--credentials-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    trading_factory: Callable[..., TradingReadClient] = TradingClient,
    http_factory: Callable[..., ActivityHttpClient] = httpx.Client,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    trading: object | None = None
    activity_http: object | None = None
    try:
        plan_payload = json.loads(_read_private_file(args.plan_file).decode("utf-8"))
        if isinstance(plan_payload, dict) and set(plan_payload) == {"account_role", "plan"}:
            if plan_payload["account_role"] != "DEVELOPMENT":
                raise OpportunityBootstrapError("OPPORTUNITY_BOOTSTRAP_DEVELOPMENT_ONLY")
            plan_payload = plan_payload["plan"]
        plan = parse_development_opportunity_plan(plan_payload)
        credentials = _parse_credentials(
            json.loads(_read_private_file(args.credentials_file).decode("utf-8"))
        )
        captured_at = clock()
        trading = trading_factory(
            api_key=credentials.api_key,
            secret_key=credentials.secret_key,
            paper=True,
            raw_data=False,
            url_override=_PAPER_ENDPOINT,
        )
        activity_http = http_factory(
            timeout=httpx.Timeout(10.0), follow_redirects=False, trust_env=False
        )
        provider = AlpacaOpportunityBaselineProvider(trading, activity_http, credentials)
        payload = collect_development_opportunity_bootstrap(
            plan,
            provider,
            captured_at=captured_at,
        )
        parse_development_opportunity_bootstrap(payload)
        output_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        _write_private_file(args.output, output_bytes)
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        OSError,
        OpportunityBaselineCollectionError,
        OpportunityBootstrapError,
        APIError,
        RequestsConnectionError,
        RequestsTimeout,
        httpx.HTTPError,
    ) as error:
        parser.error(getattr(error, "code", "OPPORTUNITY_BASELINE_INPUT_INVALID"))
    finally:
        _close(activity_http)
        _close(trading)

    result = parse_development_opportunity_bootstrap(payload)
    baseline_id, _baseline_hash = opportunity_baseline_identity(result.baseline)
    print(
        json.dumps(
            {
                "mode": "READ_ONLY_CAPTURE",
                "baseline_id": str(baseline_id),
                "plan_id": str(result.baseline.plan_id),
                "output_written": True,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def _parse_credentials(value: object) -> _Credentials:
    if type(value) is not dict or set(value) != {"api_key", "secret_key"}:
        raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_CREDENTIALS_INVALID")
    api_key = value.get("api_key")
    secret_key = value.get("secret_key")
    if (
        type(api_key) is not str
        or type(secret_key) is not str
        or not api_key
        or not secret_key
        or len(api_key) > 512
        or len(secret_key) > 512
        or any(not 33 <= ord(character) <= 126 for character in api_key + secret_key)
    ):
        raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_CREDENTIALS_INVALID")
    return _Credentials(api_key, secret_key)


def _read_private_file(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_PRIVATE_FILE_INVALID")
        chunks = bytearray()
        while True:
            chunk = os.read(descriptor, min(64 * 1024, _INPUT_LIMIT + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > _INPUT_LIMIT:
                raise OpportunityBaselineCollectionError(
                    "OPPORTUNITY_BASELINE_PRIVATE_FILE_INVALID"
                )
        return bytes(chunks)
    finally:
        os.close(descriptor)


def _write_private_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_OUTPUT_INVALID")
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OpportunityBaselineCollectionError("OPPORTUNITY_BASELINE_OUTPUT_INVALID")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _close(resource: object | None) -> None:
    if resource is None:
        return
    close = getattr(resource, "close", None)
    if callable(close):
        with suppress(OSError, RuntimeError):
            close()
        return
    session = getattr(resource, "_session", None)
    session_close = getattr(session, "close", None)
    if callable(session_close):
        with suppress(OSError, RuntimeError):
            session_close()


if __name__ == "__main__":
    raise SystemExit(main())

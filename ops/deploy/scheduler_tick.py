from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from http.client import HTTPResponse
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

_EXPECTED_PATH = "/api/internal/scheduler/tick"
_MAX_RESPONSE_BYTES = 4096


class SchedulerTickError(RuntimeError):
    pass


@dataclass(frozen=True, repr=False)
class SchedulerConfig:
    url: str
    token: str

    def __repr__(self) -> str:
        return f"SchedulerConfig(url={self.url!r}, token=<redacted>)"


class ResponsePort(Protocol):
    def __enter__(self) -> HTTPResponse: ...
    def __exit__(self, *args: object) -> None: ...


class OpenerPort(Protocol):
    def open(self, request: Request, *, timeout: float) -> ResponsePort: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def load_config(environment: Mapping[str, str]) -> SchedulerConfig:
    url = environment.get("ALPHADECAY_SCHEDULER_URL", "")
    token = environment.get("ALPHADECAY_SCHEDULER_TOKEN", "")
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError:
        raise SchedulerTickError("SCHEDULER_URL_INVALID") from None
    if (
        not url.isascii()
        or any(character.isspace() or not character.isprintable() for character in url)
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != _EXPECTED_PATH
        or parsed.query
        or parsed.fragment
    ):
        raise SchedulerTickError("SCHEDULER_URL_INVALID")
    if (
        not 32 <= len(token) <= 256
        or not token.isascii()
        or any(character.isspace() or not character.isprintable() for character in token)
    ):
        raise SchedulerTickError("SCHEDULER_TOKEN_INVALID")
    return SchedulerConfig(url=url, token=token)


def send_tick(config: SchedulerConfig, opener: OpenerPort | None = None) -> str:
    request = Request(
        config.url,
        data=b"",
        method="POST",
        headers={
            "Authorization": f"Bearer {config.token}",
            "Accept": "application/json",
            "Content-Type": "application/octet-stream",
        },
    )
    client = opener if opener is not None else build_opener(_NoRedirect())
    try:
        with client.open(request, timeout=90.0) as response:
            if response.status != 200:
                raise SchedulerTickError("SCHEDULER_RESPONSE_REJECTED")
            payload_bytes = response.read(_MAX_RESPONSE_BYTES + 1)
    except SchedulerTickError:
        raise
    except (HTTPError, URLError, OSError, TimeoutError):
        raise SchedulerTickError("SCHEDULER_REQUEST_FAILED") from None
    if len(payload_bytes) > _MAX_RESPONSE_BYTES:
        raise SchedulerTickError("SCHEDULER_RESPONSE_TOO_LARGE")
    try:
        payload = json.loads(payload_bytes)
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "tick_id",
            "accepted",
            "code",
        }:
            raise ValueError
        UUID(payload["tick_id"])
    except (TypeError, ValueError):
        raise SchedulerTickError("SCHEDULER_RESPONSE_INVALID") from None
    if (
        payload["schema_version"] != "v1"
        or payload["accepted"] is not True
        or not isinstance(payload["code"], str)
        or not payload["code"]
        or len(payload["code"]) > 128
        or any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
            for character in payload["code"]
        )
    ):
        raise SchedulerTickError("SCHEDULER_RESPONSE_INVALID")
    return payload["code"]


def main() -> int:
    try:
        code = send_tick(load_config(os.environ))
    except SchedulerTickError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"scheduler tick accepted: {code}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

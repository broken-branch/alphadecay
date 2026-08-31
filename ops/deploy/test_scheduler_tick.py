from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from ops.deploy.scheduler_tick import (
    SchedulerConfig,
    SchedulerTickError,
    load_config,
    send_tick,
)

URL = "https://alphadecay.example/api/internal/scheduler/tick"
TOKEN = "t" * 32


@dataclass
class FakeResponse:
    body: bytes
    status: int = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        return self.body[:size]


class FakeOpener:
    def __init__(self, response: FakeResponse | Exception) -> None:
        self.response = response
        self.request: Request | None = None
        self.timeout: float | None = None

    def open(self, request: Request, *, timeout: float):
        self.request = request
        self.timeout = timeout
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def response_body(**changes: object) -> bytes:
    payload: dict[str, object] = {
        "schema_version": "v1",
        "tick_id": "00000000-0000-0000-0000-000000000901",
        "accepted": True,
        "code": "CALIBRATION_BINDING_NO_TRADE",
    }
    payload.update(changes)
    return json.dumps(payload).encode()


def test_load_config_accepts_only_exact_https_tick_url_and_bounded_token() -> None:
    assert load_config(
        {"ALPHADECAY_SCHEDULER_URL": URL, "ALPHADECAY_SCHEDULER_TOKEN": TOKEN}
    ) == SchedulerConfig(URL, TOKEN)

    invalid_urls = (
        "http://alphadecay.example/api/internal/scheduler/tick",
        "https://user@alphadecay.example/api/internal/scheduler/tick",
        "https://alphadecay.example/api/internal/scheduler/tick/",
        "https://alphadecay.example/api/internal/scheduler/tick?symbol=NVDA",
        "https://alphadecay.example/api/owner/runs",
        "https://alpha\ndecay.example/api/internal/scheduler/tick",
        "https://alphadécay.example/api/internal/scheduler/tick",
        "https://[invalid/api/internal/scheduler/tick",
        "https://alphadecay.example:invalid/api/internal/scheduler/tick",
    )
    for invalid_url in invalid_urls:
        with pytest.raises(SchedulerTickError, match="SCHEDULER_URL_INVALID"):
            load_config(
                {
                    "ALPHADECAY_SCHEDULER_URL": invalid_url,
                    "ALPHADECAY_SCHEDULER_TOKEN": TOKEN,
                }
            )
    for invalid_token in ("", "short", "t" * 257, "t" * 31 + "\n"):
        with pytest.raises(SchedulerTickError, match="SCHEDULER_TOKEN_INVALID"):
            load_config(
                {
                    "ALPHADECAY_SCHEDULER_URL": URL,
                    "ALPHADECAY_SCHEDULER_TOKEN": invalid_token,
                }
            )


@pytest.mark.parametrize(
    "environment",
    (
        {},
        {"ALPHADECAY_SCHEDULER_TOKEN": TOKEN},
        {"ALPHADECAY_SCHEDULER_URL": URL},
    ),
)
def test_load_config_fails_closed_when_url_or_token_is_absent(
    environment: dict[str, str],
) -> None:
    with pytest.raises(SchedulerTickError, match="SCHEDULER_(URL|TOKEN)_INVALID"):
        load_config(environment)


def test_send_tick_posts_empty_selector_free_request_and_returns_code() -> None:
    opener = FakeOpener(FakeResponse(response_body()))

    code = send_tick(SchedulerConfig(URL, TOKEN), opener)

    assert code == "CALIBRATION_BINDING_NO_TRADE"
    assert opener.timeout == 90.0
    assert opener.request is not None
    assert opener.request.get_method() == "POST"
    assert opener.request.data == b""
    assert opener.request.get_header("Authorization") == f"Bearer {TOKEN}"
    assert opener.request.full_url == URL
    assert set(opener.request.header_items()) == {
        ("Authorization", f"Bearer {TOKEN}"),
        ("Accept", "application/json"),
        ("Content-type", "application/octet-stream"),
    }


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        response_body(schema_version="v2"),
        response_body(accepted=False),
        response_body(tick_id="not-a-uuid"),
        response_body(code="free form"),
        response_body(extra="unexpected"),
        b"x" * 4097,
    ],
)
def test_send_tick_rejects_invalid_or_unbounded_response(body: bytes) -> None:
    expected = "TOO_LARGE" if len(body) > 4096 else "INVALID"
    with pytest.raises(SchedulerTickError, match=f"SCHEDULER_RESPONSE_{expected}"):
        send_tick(SchedulerConfig(URL, TOKEN), FakeOpener(FakeResponse(body)))


def test_send_tick_canonicalizes_http_failure_without_response_body() -> None:
    error = HTTPError(URL, 307, "redirect", {}, None)
    with pytest.raises(SchedulerTickError, match="SCHEDULER_REQUEST_FAILED") as captured:
        send_tick(SchedulerConfig(URL, TOKEN), FakeOpener(error))
    assert TOKEN not in str(captured.value)

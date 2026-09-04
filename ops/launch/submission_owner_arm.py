from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from pathlib import Path

import httpx

from backend.app.api.auth import CSRF_COOKIE, SESSION_COOKIE, SESSION_MAX_AGE_SECONDS
from backend.app.contracts.v1 import AccountRole
from ops.launch.submission_runtime import SubmissionRuntimeError, load_config

_BASE_URL = "http://127.0.0.1:8000"
_RESPONSE_LIMIT = 64 * 1024
_STATUS = {
    "schema_version": "v1",
    "role": AccountRole.SUBMISSION.value,
    "server_enabled": True,
    "account_enabled": True,
    "effective": True,
}


class SubmissionOwnerArmError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class _BoundResponse:
    status_code: int
    headers: httpx.Headers
    payload: bytes
    oversized: bool


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview or arm durable SUBMISSION autonomy through the owner product API"
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the fixed-loopback owner session and autonomy calls",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config, autonomous=True)
        if args.apply:
            _arm(config.environment, transport=transport)
            mode = "DURABLE_AUTONOMY_ARMED"
        else:
            mode = "NO_NETWORK_PREVIEW"
    except Exception as error:
        code = (
            error.code
            if isinstance(error, SubmissionOwnerArmError | SubmissionRuntimeError)
            else "SUBMISSION_OWNER_ARM_FAILED"
        )
        parser.error(code)
    print(
        json.dumps(
            {
                "mode": mode,
                "account_role": AccountRole.SUBMISSION.value,
                "fixed_loopback": True,
                "selector_free": True,
                "effective": args.apply,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def _arm(
    environment: dict[str, str],
    *,
    transport: httpx.BaseTransport | None = None,
) -> None:
    if (
        environment["APP_ACCOUNT_ROLE"] != AccountRole.SUBMISSION.value
        or environment["APP_AUTONOMOUS_ENABLED"] != "true"
        or environment["APP_SUBMISSION_OPPORTUNITY_ENABLED"] != "true"
        or environment["APP_RUNTIME_CONFIG_REQUIRED"] != "true"
        or environment["ALPACA_PAPER_TRADE"] != "true"
        or environment["ALPACA_API_ENDPOINT"] != "https://paper-api.alpaca.markets"
    ):
        raise SubmissionOwnerArmError("SUBMISSION_OWNER_ARM_AUTHORITY_INVALID")

    origin = environment["APP_ALLOWED_ORIGIN"]
    access_code = environment["APP_OWNER_ACCESS_CODE"]
    try:
        with httpx.Client(
            base_url=_BASE_URL,
            timeout=10.0,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            login = _request(
                client,
                "POST",
                "/api/session",
                headers={"Origin": origin},
                json_payload={"access_code": access_code},
            )
            if login.status_code != 200:
                raise SubmissionOwnerArmError("SUBMISSION_OWNER_SESSION_FAILED")
            headers = _session_headers(login, origin=origin)
            operation_error: Exception | None = None
            try:
                login_payload = _response_json(login)
                if (
                    set(login_payload) != {"schema_version", "authenticated", "expires_at"}
                    or login_payload["schema_version"] != "v1"
                    or login_payload["authenticated"] is not True
                    or type(login_payload["expires_at"]) is not str
                ):
                    raise SubmissionOwnerArmError("SUBMISSION_OWNER_SESSION_FAILED")
                armed = _request(
                    client,
                    "POST",
                    "/api/owner/autonomy/enable",
                    headers=headers,
                    content=b"",
                )
                _verify_status(armed)
                observed = _request(
                    client,
                    "GET",
                    "/api/owner/autonomy",
                    headers=headers,
                )
                _verify_status(observed)
            except Exception as error:
                operation_error = error

            cleanup_error: Exception | None = None
            try:
                logout = _request(
                    client,
                    "DELETE",
                    "/api/session",
                    headers=headers,
                )
                if logout.status_code != 200 or _response_json(logout) != {
                    "schema_version": "v1",
                    "authenticated": False,
                    "expires_at": None,
                }:
                    raise SubmissionOwnerArmError("SUBMISSION_OWNER_SESSION_CLEANUP_FAILED")
            except Exception as error:
                cleanup_error = error
            if cleanup_error is not None:
                if isinstance(cleanup_error, SubmissionOwnerArmError):
                    raise cleanup_error
                raise SubmissionOwnerArmError("SUBMISSION_OWNER_SESSION_CLEANUP_FAILED") from None
            if operation_error is not None:
                if isinstance(operation_error, SubmissionOwnerArmError):
                    raise operation_error
                raise SubmissionOwnerArmError("SUBMISSION_OWNER_ARM_FAILED") from None
    except SubmissionOwnerArmError:
        raise
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        raise SubmissionOwnerArmError("SUBMISSION_OWNER_ARM_FAILED") from None


def _verify_status(response: _BoundResponse) -> None:
    if response.status_code != 200 or _response_json(response) != _STATUS:
        raise SubmissionOwnerArmError("SUBMISSION_OWNER_ARM_NOT_EFFECTIVE")


def _request(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    json_payload: object | None = None,
    content: bytes | None = None,
) -> _BoundResponse:
    if json_payload is not None and content is not None:
        raise SubmissionOwnerArmError("SUBMISSION_OWNER_ARM_FAILED")
    kwargs: dict[str, object] = {"headers": headers}
    if json_payload is not None:
        kwargs["json"] = json_payload
    elif content is not None:
        kwargs["content"] = content
    request = client.build_request(method, path, **kwargs)
    response = client.send(request, stream=True)
    try:
        payload = bytearray()
        oversized = False
        for chunk in response.iter_bytes(chunk_size=16 * 1024):
            payload.extend(chunk)
            if len(payload) > _RESPONSE_LIMIT:
                oversized = True
                break
        return _BoundResponse(
            status_code=response.status_code,
            headers=httpx.Headers(response.headers),
            payload=bytes(payload),
            oversized=oversized,
        )
    finally:
        response.close()


def _session_headers(response: _BoundResponse, *, origin: str) -> dict[str, str]:
    raw_cookies = response.headers.get_list("set-cookie")
    if len(raw_cookies) != 2:
        raise SubmissionOwnerArmError("SUBMISSION_OWNER_SESSION_FAILED")
    tokens: dict[str, str] = {}
    for raw in raw_cookies:
        parsed = SimpleCookie()
        try:
            parsed.load(raw)
        except CookieError:
            raise SubmissionOwnerArmError("SUBMISSION_OWNER_SESSION_FAILED") from None
        if len(parsed) != 1:
            raise SubmissionOwnerArmError("SUBMISSION_OWNER_SESSION_FAILED")
        name, morsel = next(iter(parsed.items()))
        token = morsel.value
        if (
            name not in {SESSION_COOKIE, CSRF_COOKIE}
            or name in tokens
            or not token
            or len(token) > 4096
            or not token.isascii()
            or morsel["path"] != "/"
            or not morsel["secure"]
            or morsel["samesite"].casefold() != "strict"
            or morsel["max-age"] != str(SESSION_MAX_AGE_SECONDS)
            or morsel["domain"]
            or bool(morsel["httponly"]) != (name == SESSION_COOKIE)
        ):
            raise SubmissionOwnerArmError("SUBMISSION_OWNER_SESSION_FAILED")
        tokens[name] = token
    if set(tokens) != {SESSION_COOKIE, CSRF_COOKIE}:
        raise SubmissionOwnerArmError("SUBMISSION_OWNER_SESSION_FAILED")
    csrf = tokens[CSRF_COOKIE]
    return {
        "Origin": origin,
        "X-CSRF-Token": csrf,
        "Cookie": (f"{SESSION_COOKIE}={tokens[SESSION_COOKIE]}; {CSRF_COOKIE}={csrf}"),
    }


def _response_json(response: _BoundResponse) -> dict[str, object]:
    if response.oversized:
        raise SubmissionOwnerArmError("SUBMISSION_OWNER_RESPONSE_INVALID")
    try:
        value = json.loads(
            response.payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise SubmissionOwnerArmError("SUBMISSION_OWNER_RESPONSE_INVALID") from None
    if type(value) is not dict:
        raise SubmissionOwnerArmError("SUBMISSION_OWNER_RESPONSE_INVALID")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


if __name__ == "__main__":
    raise SystemExit(main())

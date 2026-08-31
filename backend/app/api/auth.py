from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import Lock
from urllib.parse import urlsplit

SESSION_COOKIE = "__Host-alphadecay_session"
CSRF_COOKIE = "__Host-alphadecay_csrf"
SESSION_MAX_AGE_SECONDS = 900
SESSION_TTL = timedelta(seconds=SESSION_MAX_AGE_SECONDS)
LOGIN_WINDOW = timedelta(minutes=5)
LOGIN_ATTEMPT_LIMIT = 5


class SessionAuthError(ValueError):
    pass


class SchedulerAuthError(ValueError):
    pass


class SchedulerAuthenticator:
    def __init__(self, token: str) -> None:
        if len(token.encode()) < 32:
            raise ValueError("scheduler token is too short")
        self._token = token.encode()

    def verify(self, supplied: str | None) -> None:
        candidate = supplied.encode() if supplied is not None else b""
        if not hmac.compare_digest(candidate, self._token):
            raise SchedulerAuthError("SCHEDULER_AUTHENTICATION_FAILED")


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(encoded: str) -> bytes:
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(encoded + padding)


class OwnerSessionManager:
    def __init__(
        self,
        *,
        access_code: str,
        signing_secret: str,
        allowed_origin: str,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if len(access_code) < 16:
            raise ValueError("owner access code is too short")
        if len(signing_secret.encode()) < 32:
            raise ValueError("session signing secret is too short")
        self._access_code = access_code.encode()
        self._signing_key = signing_secret.encode()
        self._allowed_origin_bytes = allowed_origin.encode()
        self._now = now
        self._failed_attempts: deque[datetime] = deque(maxlen=LOGIN_ATTEMPT_LIMIT)
        self._active_session_id: str | None = None
        self._lock = Lock()

    def require_origin(self, origin: str | None) -> None:
        supplied = origin.encode() if origin is not None else b""
        if not hmac.compare_digest(supplied, self._allowed_origin_bytes):
            raise SessionAuthError("ORIGIN_REJECTED")

    def require_same_origin_referer(self, referer: str | None) -> None:
        try:
            parsed = urlsplit(referer or "")
            supplied_origin = f"{parsed.scheme}://{parsed.netloc}"
        except ValueError:
            raise SessionAuthError("ORIGIN_REJECTED") from None
        if (
            parsed.username
            or parsed.password
            or parsed.scheme != "https"
            or not hmac.compare_digest(supplied_origin.encode(), self._allowed_origin_bytes)
        ):
            raise SessionAuthError("ORIGIN_REJECTED")

    def create(self, access_code: str) -> tuple[str, str, datetime]:
        with self._lock:
            now = self._now()
            cutoff = now - LOGIN_WINDOW
            while self._failed_attempts and self._failed_attempts[0] <= cutoff:
                self._failed_attempts.popleft()
            if len(self._failed_attempts) >= LOGIN_ATTEMPT_LIMIT:
                raise SessionAuthError("AUTHENTICATION_RATE_LIMITED")
            if not hmac.compare_digest(access_code.encode(), self._access_code):
                self._failed_attempts.append(now)
                raise SessionAuthError("AUTHENTICATION_FAILED")

            self._failed_attempts.clear()
            expires_at = now + SESSION_TTL
            csrf = secrets.token_urlsafe(32)
            session_id = secrets.token_urlsafe(24)
            payload = json.dumps(
                {
                    "csrf_hash": hashlib.sha256(csrf.encode()).hexdigest(),
                    "expires_at": int(expires_at.timestamp()),
                    "session_id": session_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            encoded_payload = _encode(payload)
            signature = _encode(hmac.digest(self._signing_key, encoded_payload.encode(), "sha256"))
            self._active_session_id = session_id
            return f"{encoded_payload}.{signature}", csrf, expires_at

    def verify(self, token: str | None, csrf: str | None) -> None:
        with self._lock:
            self._verify_locked(token, csrf)

    def revoke(self, token: str | None, csrf: str | None) -> None:
        with self._lock:
            self._verify_locked(token, csrf)
            self._active_session_id = None

    def _verify_locked(self, token: str | None, csrf: str | None) -> None:
        if token is None:
            raise SessionAuthError("SESSION_REQUIRED")
        try:
            encoded_payload, supplied_signature = token.split(".", maxsplit=1)
            expected_signature = _encode(
                hmac.digest(self._signing_key, encoded_payload.encode(), "sha256")
            )
            if not hmac.compare_digest(supplied_signature, expected_signature):
                raise SessionAuthError("SESSION_INVALID")
            payload = json.loads(_decode(encoded_payload))
            expires_at = datetime.fromtimestamp(payload["expires_at"], tz=UTC)
            csrf_hash = payload["csrf_hash"]
            session_id = payload["session_id"]
            if not isinstance(csrf_hash, str) or not isinstance(session_id, str):
                raise SessionAuthError("SESSION_INVALID")
        except SessionAuthError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise SessionAuthError("SESSION_INVALID") from error
        if self._now() >= expires_at:
            if hmac.compare_digest(session_id, self._active_session_id or ""):
                self._active_session_id = None
            raise SessionAuthError("SESSION_EXPIRED")
        if not hmac.compare_digest(session_id, self._active_session_id or ""):
            raise SessionAuthError("SESSION_INVALID")
        supplied_csrf_hash = hashlib.sha256(csrf.encode()).hexdigest() if csrf else ""
        if not hmac.compare_digest(supplied_csrf_hash, csrf_hash):
            raise SessionAuthError("CSRF_REJECTED")

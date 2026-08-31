import pytest

from backend.app.api.auth import SchedulerAuthenticator, SchedulerAuthError


def test_scheduler_token_requires_sufficient_entropy_budget() -> None:
    with pytest.raises(ValueError, match="scheduler token is too short"):
        SchedulerAuthenticator("too-short")


def test_scheduler_authenticator_accepts_only_exact_token() -> None:
    authenticator = SchedulerAuthenticator("t" * 32)

    authenticator.verify("t" * 32)
    for supplied in (None, "", "t" * 31, "t" * 33, "u" * 32):
        with pytest.raises(SchedulerAuthError, match="SCHEDULER_AUTHENTICATION_FAILED"):
            authenticator.verify(supplied)

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest

from backend.app.contracts.v1 import AccountRole
from backend.app.execution import Actor
from backend.app.persistence.opportunity_authority import OpportunityAuthorityError
from backend.app.services.acquisition import (
    AcquisitionFailure,
    AcquisitionKind,
    ObservedPaperAccountAuthority,
)
from backend.app.services.development_acquisition import (
    DevelopmentAcquisitionRouter,
    DevelopmentRoute,
    DevelopmentRouteAuthority,
)

NOW = datetime(2026, 8, 31, 15, 0, tzinfo=UTC)
TICK_ID = UUID("00000000-0000-0000-0000-000000000901")
POSITION_ID = UUID("00000000-0000-0000-0000-000000000902")
FINGERPRINT = "a" * 64
POSITION_FINGERPRINT = "b" * 64
AUTHORITY_HASH = "c" * 64


def authority(role: AccountRole = AccountRole.DEVELOPMENT):
    return ObservedPaperAccountAuthority(
        role=role,
        account_fingerprint=FINGERPRINT,
        paper=True,
        persistent_autonomy_enabled=True,
    )


class Routes:
    def __init__(self, value=None, error: Exception | None = None) -> None:
        self.value = value or empty_route()
        self.error = error
        self.calls: list[str] = []

    def load_development_route(
        self,
        *,
        account_role=AccountRole.DEVELOPMENT,
        expected_account_fingerprint,
    ):
        self.calls.append(expected_account_fingerprint)
        if self.error is not None:
            raise self.error
        return (
            replace(self.value, account_role=account_role)
            if isinstance(self.value, DevelopmentRouteAuthority)
            else self.value
        )


class Acquisition:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[object, ...]] = []

    async def acquire(self, account, trusted_at, tick_id, *, actor):
        self.calls.append((account, trusted_at, tick_id, actor))
        return self.result


def empty_route(*, fingerprint: str = FINGERPRINT) -> DevelopmentRouteAuthority:
    return DevelopmentRouteAuthority(
        fingerprint,
        DevelopmentRoute.EMPTY,
        0,
        None,
        None,
        AUTHORITY_HASH,
    )


def managed_route(
    *,
    fingerprint: str = FINGERPRINT,
    managed_position_id: UUID = POSITION_ID,
    position_fingerprint: str = POSITION_FINGERPRINT,
) -> DevelopmentRouteAuthority:
    return DevelopmentRouteAuthority(
        fingerprint,
        DevelopmentRoute.MANAGED_POSITION,
        1,
        managed_position_id,
        position_fingerprint,
        AUTHORITY_HASH,
    )


def ambiguous_route() -> DevelopmentRouteAuthority:
    return DevelopmentRouteAuthority(
        FINGERPRINT,
        DevelopmentRoute.AMBIGUOUS,
        2,
        None,
        None,
        AUTHORITY_HASH,
    )


def router(value=None, *, route_error: Exception | None = None):
    routes = Routes(value, route_error)
    opportunity = Acquisition(object())
    lifecycle = Acquisition(object())
    return (
        DevelopmentAcquisitionRouter(routes, opportunity, lifecycle),
        routes,
        opportunity,
        lifecycle,
    )


def acquire(target, *, actor=Actor.SCHEDULER, account=None):
    return asyncio.run(
        target.acquire(
            account or authority(),
            NOW,
            TICK_ID,
            actor=actor,
        )
    )


def assert_failure(target, code: str, kind: AcquisitionKind, **kwargs) -> None:
    with pytest.raises(AcquisitionFailure) as caught:
        acquire(target, **kwargs)
    assert caught.value.code == code
    assert caught.value.kind is kind


def test_submission_routes_empty_exact_role_to_opportunity() -> None:
    target, routes, opportunity, lifecycle = router()

    result = acquire(target, account=authority(AccountRole.SUBMISSION))

    assert result is opportunity.result
    assert routes.calls == [FINGERPRINT]
    assert opportunity.calls == [
        (authority(AccountRole.SUBMISSION), NOW, TICK_ID, Actor.SCHEDULER)
    ]
    assert lifecycle.calls == []


def test_empty_durable_inventory_routes_scheduler_to_opportunity() -> None:
    target, routes, opportunity, lifecycle = router()

    result = acquire(target, actor=Actor.SCHEDULER)

    assert result is opportunity.result
    assert routes.calls == [FINGERPRINT]
    assert opportunity.calls == [(authority(), NOW, TICK_ID, Actor.SCHEDULER)]
    assert lifecycle.calls == []


def test_empty_durable_inventory_rejects_owner_before_either_acquisition() -> None:
    target, routes, opportunity, lifecycle = router()

    assert_failure(
        target,
        "OPPORTUNITY_ACQUISITION_SCHEDULER_REQUIRED",
        AcquisitionKind.LIFECYCLE,
        actor=Actor.OWNER,
    )

    assert routes.calls == [FINGERPRINT]
    assert opportunity.calls == []
    assert lifecycle.calls == []


def test_owner_safe_stop_does_not_consume_same_boundary_scheduler_success() -> None:
    target, routes, opportunity, lifecycle = router()

    assert_failure(
        target,
        "OPPORTUNITY_ACQUISITION_SCHEDULER_REQUIRED",
        AcquisitionKind.LIFECYCLE,
        actor=Actor.OWNER,
    )
    result = acquire(target, actor=Actor.SCHEDULER)

    assert result is opportunity.result
    assert routes.calls == [FINGERPRINT, FINGERPRINT]
    assert opportunity.calls == [(authority(), NOW, TICK_ID, Actor.SCHEDULER)]
    assert lifecycle.calls == []


@pytest.mark.parametrize("actor", [Actor.OWNER, Actor.SCHEDULER])
def test_one_matching_managed_position_routes_to_lifecycle(actor: Actor) -> None:
    target, routes, opportunity, lifecycle = router(managed_route())

    result = acquire(target, actor=actor)

    assert result is lifecycle.result
    assert routes.calls == [FINGERPRINT]
    assert opportunity.calls == []
    assert lifecycle.calls == [(authority(), NOW, TICK_ID, actor)]


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (managed_route(fingerprint="d" * 64), "MANAGED_POSITION_AUTHORITY_MISMATCH"),
        (ambiguous_route(), "ACTIVE_MANAGED_POSITION_NOT_UNIQUE"),
    ],
)
def test_ambiguous_or_mismatched_position_authority_fails_before_acquisition(
    value,
    code: str,
) -> None:
    target, routes, opportunity, lifecycle = router(value)

    assert_failure(target, code, AcquisitionKind.LIFECYCLE)

    assert routes.calls == [FINGERPRINT]
    assert opportunity.calls == []
    assert lifecycle.calls == []


def test_unavailable_position_authority_fails_before_acquisition() -> None:
    target, routes, opportunity, lifecycle = router(
        route_error=RuntimeError("database unavailable")
    )

    assert_failure(
        target,
        "MANAGED_POSITION_AUTHORITY_UNAVAILABLE",
        AcquisitionKind.LIFECYCLE,
    )

    assert routes.calls == [FINGERPRINT]
    assert opportunity.calls == []
    assert lifecycle.calls == []


@pytest.mark.parametrize(
    "error",
    [
        ValueError("invalid durable state"),
        OpportunityAuthorityError("MANAGED_POSITION_SNAPSHOT_MISSING"),
    ],
)
def test_repository_failures_are_normalized_before_acquisition(error: Exception) -> None:
    target, routes, opportunity, lifecycle = router()
    routes.error = error

    assert_failure(
        target,
        "MANAGED_POSITION_AUTHORITY_UNAVAILABLE",
        AcquisitionKind.LIFECYCLE,
    )

    assert routes.calls == [FINGERPRINT]
    assert opportunity.calls == []
    assert lifecycle.calls == []


@pytest.mark.parametrize(
    "value",
    [
        object(),
        DevelopmentRouteAuthority(
            FINGERPRINT,
            DevelopmentRoute.EMPTY,
            1,
            POSITION_ID,
            POSITION_FINGERPRINT,
            AUTHORITY_HASH,
        ),
        DevelopmentRouteAuthority(
            FINGERPRINT,
            DevelopmentRoute.MANAGED_POSITION,
            1,
            POSITION_ID,
            "not-a-hash",
            AUTHORITY_HASH,
        ),
        DevelopmentRouteAuthority(
            FINGERPRINT,
            DevelopmentRoute.EMPTY,
            0,
            None,
            None,
            cast(str, 3),
        ),
    ],
)
def test_invalid_position_authority_result_fails_before_acquisition(value) -> None:
    target, _, opportunity, lifecycle = router(value)

    assert_failure(
        target,
        "MANAGED_POSITION_AUTHORITY_INVALID",
        AcquisitionKind.LIFECYCLE,
    )

    assert opportunity.calls == []
    assert lifecycle.calls == []

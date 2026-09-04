from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from backend.app.contracts.v1 import AccountRole
from backend.app.execution import Actor

from .acquisition import (
    AcquisitionFailure,
    AcquisitionKind,
    AgentAcquisitionPort,
    DecisionAcquisition,
    ObservedPaperAccountAuthority,
)


def _is_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


class DevelopmentRoute(StrEnum):
    EMPTY = "EMPTY"
    MANAGED_POSITION = "MANAGED_POSITION"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class DevelopmentRouteAuthority:
    account_fingerprint: str
    route: DevelopmentRoute
    active_position_count: int
    managed_position_id: UUID | None
    position_fingerprint: str | None
    authority_hash: str
    account_role: AccountRole = AccountRole.DEVELOPMENT


class DevelopmentRouteAuthorityPort(Protocol):
    def load_development_route(
        self,
        *,
        account_role: AccountRole = AccountRole.DEVELOPMENT,
        expected_account_fingerprint: str,
    ) -> DevelopmentRouteAuthority:
        """Load the durable DEVELOPMENT route for the expected paper account."""
        ...


_ENTRY_WINDOW_INACTIVE_CODES = frozenset(
    {
        "OPPORTUNITY_ENTRY_WINDOW_CLOSED",
        "OPPORTUNITY_PLAN_AUTHORITY_INVALID",
        "OPPORTUNITY_PERSISTENCE_REQUIRED",
    }
)


class DevelopmentAcquisitionRouter:
    def __init__(
        self,
        routes: DevelopmentRouteAuthorityPort,
        opportunity: AgentAcquisitionPort,
        lifecycle: AgentAcquisitionPort,
    ) -> None:
        self._routes = routes
        self._opportunity = opportunity
        self._lifecycle = lifecycle

    async def acquire(
        self,
        authority: ObservedPaperAccountAuthority,
        trusted_at: datetime,
        tick_id: UUID,
        *,
        actor: Actor,
    ) -> DecisionAcquisition:
        if authority.role not in (AccountRole.DEVELOPMENT, AccountRole.SUBMISSION):
            raise AcquisitionFailure(
                AcquisitionKind.LIFECYCLE,
                "ACQUISITION_ACCOUNT_ROLE_INVALID",
            )
        if (
            trusted_at.tzinfo is None
            or trusted_at.utcoffset() != timedelta(0)
            or not isinstance(tick_id, UUID)
            or not isinstance(actor, Actor)
        ):
            raise AcquisitionFailure(
                AcquisitionKind.LIFECYCLE,
                "DEVELOPMENT_ACQUISITION_AUTHORITY_INVALID",
            )
        try:
            route = (
                self._routes.load_development_route(
                    expected_account_fingerprint=authority.account_fingerprint
                )
                if authority.role is AccountRole.DEVELOPMENT
                else self._routes.load_development_route(
                    account_role=authority.role,
                    expected_account_fingerprint=authority.account_fingerprint,
                )
            )
        except Exception as error:
            raise AcquisitionFailure(
                AcquisitionKind.LIFECYCLE,
                "MANAGED_POSITION_AUTHORITY_UNAVAILABLE",
            ) from error
        if not isinstance(route, DevelopmentRouteAuthority):
            raise AcquisitionFailure(
                AcquisitionKind.LIFECYCLE,
                "MANAGED_POSITION_AUTHORITY_INVALID",
            )
        if (
            route.account_role is not authority.role
            or route.account_fingerprint != authority.account_fingerprint
        ):
            raise AcquisitionFailure(
                AcquisitionKind.LIFECYCLE,
                "MANAGED_POSITION_AUTHORITY_MISMATCH",
            )
        if not _valid_route_authority(route):
            raise AcquisitionFailure(
                AcquisitionKind.LIFECYCLE,
                "MANAGED_POSITION_AUTHORITY_INVALID",
            )
        positions_open = route.route in (
            DevelopmentRoute.MANAGED_POSITION,
            DevelopmentRoute.AMBIGUOUS,
        )
        if positions_open and actor is not Actor.SCHEDULER:
            return await self._lifecycle.acquire(
                authority,
                trusted_at,
                tick_id,
                actor=actor,
            )
        if actor is not Actor.SCHEDULER:
            raise AcquisitionFailure(
                AcquisitionKind.LIFECYCLE,
                "OPPORTUNITY_ACQUISITION_SCHEDULER_REQUIRED",
            )
        # Entries take priority while an entry window is open, even with managed positions
        # on the book; once the window has closed, ticks assess the open positions. Each
        # managed position keeps its own lifecycle context, so several may be open at once.
        try:
            return await self._opportunity.acquire(
                authority,
                trusted_at,
                tick_id,
                actor=actor,
            )
        except AcquisitionFailure as error:
            if not positions_open or error.code not in _ENTRY_WINDOW_INACTIVE_CODES:
                raise
        return await self._lifecycle.acquire(
            authority,
            trusted_at,
            tick_id,
            actor=actor,
        )


def _valid_route_authority(authority: DevelopmentRouteAuthority) -> bool:
    if (
        not _is_hash(authority.account_fingerprint)
        or not _is_hash(authority.authority_hash)
        or authority.account_role not in (AccountRole.DEVELOPMENT, AccountRole.SUBMISSION)
        or type(authority.active_position_count) is not int
        or authority.active_position_count < 0
    ):
        return False
    if authority.route is DevelopmentRoute.EMPTY:
        return (
            authority.active_position_count == 0
            and authority.managed_position_id is None
            and authority.position_fingerprint is None
        )
    if authority.route is DevelopmentRoute.MANAGED_POSITION:
        return (
            authority.active_position_count == 1
            and isinstance(authority.managed_position_id, UUID)
            and isinstance(authority.position_fingerprint, str)
            and _is_hash(authority.position_fingerprint)
        )
    if authority.route is DevelopmentRoute.AMBIGUOUS:
        return (
            authority.active_position_count > 1
            and authority.managed_position_id is None
            and authority.position_fingerprint is None
        )
    return False

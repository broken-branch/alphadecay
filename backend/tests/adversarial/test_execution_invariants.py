from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from backend.app.contracts.v1 import AccountRole, PositionIntent
from backend.app.execution import (
    Actor,
    BrokerResult,
    EntryApprovalAuthorization,
    ExecutionAction,
    ExecutionBlocked,
    OrderEnvelope,
    OrderLegIntent,
    Reconciliation,
    order_envelope_hash,
)
from backend.app.persistence import InMemoryExecutionRepository
from backend.app.services import ExecutionService

NOW = datetime(2026, 8, 28, 15, 0, tzinfo=UTC)
FIRST_INTENT_ID = UUID("00000000-0000-0000-0000-000000000501")
SECOND_INTENT_ID = UUID("00000000-0000-0000-0000-000000000502")
AUTHORIZATION_ID = UUID("00000000-0000-0000-0000-000000000503")


def _execution_service(repository, broker, preflight) -> ExecutionService:
    return ExecutionService(
        repository,
        broker,
        preflight,
        account_role=AccountRole.SUBMISSION,
        account_fingerprint="submission-fingerprint",
    )


def envelope() -> OrderEnvelope:
    return OrderEnvelope(
        action=ExecutionAction.ENTRY,
        authorization_certificate_id=AUTHORIZATION_ID,
        policy_hash="policy-v0.1",
        account_fingerprint="submission-fingerprint",
        position_or_book_fingerprint="book-fingerprint",
        legs=(
            OrderLegIntent("DEMO260918C00100000", PositionIntent.BUY_TO_OPEN, 1),
            OrderLegIntent("DEMO260918C00105000", PositionIntent.SELL_TO_OPEN, 1),
        ),
        quantity=1,
        minimum_limit=Decimal("1.00"),
        maximum_limit=Decimal("1.50"),
        approved_max_loss=Decimal("700"),
        event_key="DEMO-2026-08-28",
        trading_day=date(2026, 8, 28),
    )


def repository() -> InMemoryExecutionRepository:
    repo = InMemoryExecutionRepository(clock=lambda: NOW)
    repo.register_account(
        role=AccountRole.SUBMISSION,
        fingerprint="submission-fingerprint",
        equity=Decimal("100000"),
        autonomous_enabled=True,
    )
    repo.set_autonomous_enabled(AccountRole.SUBMISSION, True, actor=Actor.OWNER)
    repo.capture_baseline(
        role=AccountRole.SUBMISSION,
        fingerprint="submission-fingerprint",
        equity=Decimal("100000"),
        captured_at=NOW,
        positions_hash="positions",
        orders_hash="orders",
        activities_hash="activities",
    )
    return repo


@pytest.mark.parametrize(
    "changed_envelope",
    [
        replace(envelope(), approved_max_loss=Decimal("800")),
        replace(envelope(), event_key="DEMO-OTHER-2026-08-28"),
        replace(envelope(), trading_day=date(2026, 8, 29)),
    ],
)
def test_digest_deduplication_never_reuses_a_different_envelope(
    changed_envelope: OrderEnvelope,
) -> None:
    repo = repository()
    original = repo.approve_intent(FIRST_INTENT_ID, AccountRole.SUBMISSION, envelope())

    with pytest.raises(ExecutionBlocked, match="INTENT_DIGEST_COLLISION"):
        repo.approve_intent(SECOND_INTENT_ID, AccountRole.SUBMISSION, changed_envelope)

    assert repo.get_intent(original.intent_id).envelope == envelope()


class LookupFillBroker:
    def submit(self, request: OrderEnvelope, client_id: str) -> BrokerResult:
        return BrokerResult("provider-order", "NEW", 0, request.quantity)

    def lookup(self, client_id: str) -> BrokerResult:
        return BrokerResult("provider-order", "FILLED", 1, 1)

    def replace(self, provider_order_id: str, client_id: str, limit: Decimal) -> BrokerResult:
        raise AssertionError("a discovered fill must not be replaced")

    def cancel(self, provider_order_id: str) -> BrokerResult:
        raise AssertionError("a complete fill must not be canceled")

    def reconcile(self, client_id: str) -> Reconciliation:
        return Reconciliation(
            terminal=True,
            remainder_absent=True,
            matches_expected=True,
            assignment_suspected=False,
            actual_exposure=None,
        )


class StablePreflight:
    def current_fingerprint(self, action: ExecutionAction) -> str:
        return "book-fingerprint"


def test_retired_service_cannot_reach_pre_replace_broker_path() -> None:
    repo = repository()
    order = envelope()
    repo.add_entry_approval(
        EntryApprovalAuthorization(
            approval_id=AUTHORIZATION_ID,
            thesis_version_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
            account_role=AccountRole.SUBMISSION,
            policy_hash=order.policy_hash,
            book_fingerprint=order.position_or_book_fingerprint,
            envelope_hash=order_envelope_hash(order),
            approved_max_loss=order.approved_max_loss,
            quantity=order.quantity,
            valid_from=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(minutes=1),
        )
    )
    repo.approve_intent(FIRST_INTENT_ID, AccountRole.SUBMISSION, order)

    with pytest.raises(ExecutionBlocked, match="BROKER_WRITE_AUTHORITY_NOT_INTEGRATED"):
        _execution_service(repo, LookupFillBroker(), StablePreflight()).execute(
            FIRST_INTENT_ID, Actor.SCHEDULER, NOW
        )

    assert repo.attempts_for(FIRST_INTENT_ID) == ()

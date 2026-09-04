from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from backend.app.contracts.v1 import AccountRole, GreekExposure, PositionIntent
from backend.app.execution import (
    Actor,
    AssessmentCertificate,
    BrokerResult,
    EntryApprovalAuthorization,
    ExecutionAction,
    ExecutionBlocked,
    ExecutionCertificate,
    OrderEnvelope,
    OrderLegIntent,
    Reconciliation,
    attempt_request_hash,
    client_order_id,
    intent_digest,
    order_envelope_hash,
)
from backend.app.execution.models import IntentState, OrderAttempt
from backend.app.order_limits import MAX_STRUCTURAL_OPTION_QUANTITY, EntryBudgetLimits
from backend.app.persistence import InMemoryExecutionRepository
from backend.app.services import ExecutionService

NOW = datetime(2026, 9, 3, 16, 0, tzinfo=UTC)
INTENT_ID = UUID("00000000-0000-0000-0000-000000000201")
AUTH_ID = UUID("00000000-0000-0000-0000-000000000202")
POLICY_HASH = "a" * 64
ENTRY_LIMITS = EntryBudgetLimits(
    policy_hash=POLICY_HASH,
    equity_floor=Decimal("99000"),
    maximum_lifetime_entries=3,
    maximum_lifetime_risk=Decimal("1500"),
    maximum_position_loss=Decimal("800"),
    maximum_entry_quantity=4,
)


def _claim(
    repo: InMemoryExecutionRepository,
    intent_id: UUID,
    actor: Actor,
    *,
    now: datetime,
):
    return repo.claim_intent(
        intent_id,
        actor,
        now=now,
        account_role=AccountRole.SUBMISSION,
        account_fingerprint="account-fingerprint",
    )


def _execution_service(repository, broker, preflight) -> ExecutionService:
    return ExecutionService(
        repository,
        broker,
        preflight,
        account_role=AccountRole.SUBMISSION,
        account_fingerprint="account-fingerprint",
    )


def envelope(action: ExecutionAction = ExecutionAction.ENTRY) -> OrderEnvelope:
    return OrderEnvelope(
        action=action,
        authorization_certificate_id=AUTH_ID,
        policy_hash=POLICY_HASH,
        account_fingerprint="account-fingerprint",
        position_or_book_fingerprint="book-fingerprint",
        legs=(
            OrderLegIntent("DEMO260918C00100000", PositionIntent.BUY_TO_OPEN, 1),
            OrderLegIntent("DEMO260918C00105000", PositionIntent.SELL_TO_OPEN, 1),
        ),
        quantity=2,
        minimum_limit=Decimal("1.00"),
        maximum_limit=Decimal("1.50"),
        approved_max_loss=Decimal("700"),
        event_key="PANW-2026-09-03",
        trading_day=date(2026, 9, 3),
    )


class FakeBroker:
    def __init__(self, results: list[BrokerResult | Exception]) -> None:
        self.results = results
        self.calls: list[tuple[str, str]] = []

    def submit(self, request: OrderEnvelope, client_id: str) -> BrokerResult:
        self.calls.append(("submit", client_id))
        return self._next()

    def lookup(self, client_id: str) -> BrokerResult | None:
        self.calls.append(("lookup", client_id))
        result = self._next()
        return result

    def replace(self, provider_order_id: str, client_id: str, limit: Decimal) -> BrokerResult:
        self.calls.append(("replace", client_id))
        return self._next()

    def cancel(self, provider_order_id: str) -> BrokerResult:
        self.calls.append(("cancel", provider_order_id))
        return self._next()

    def reconcile(self, client_id: str) -> Reconciliation:
        self.calls.append(("reconcile", client_id))
        result = self._next()
        assert isinstance(result, Reconciliation)
        return result

    def _next(self):
        value = self.results.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class FakePreflight:
    def __init__(self, fingerprint: str = "book-fingerprint") -> None:
        self.fingerprint = fingerprint

    def current_fingerprint(self, action: ExecutionAction) -> str:
        return self.fingerprint


def result(state: str, *, filled: int = 0) -> BrokerResult:
    return BrokerResult("broker-order", state, filled, 2)


def reconciliation(
    *,
    matches: bool = True,
    remainder_absent: bool = True,
    assignment_suspected: bool = False,
) -> Reconciliation:
    return Reconciliation(
        terminal=True,
        remainder_absent=remainder_absent,
        matches_expected=matches,
        assignment_suspected=assignment_suspected,
        actual_exposure=GreekExposure(delta=50, gamma=2, theta_per_day=-4, vega_per_iv_point=4),
    )


def entry_approval(order: OrderEnvelope | None = None) -> EntryApprovalAuthorization:
    order = order or envelope()
    return EntryApprovalAuthorization(
        approval_id=order.authorization_certificate_id,
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


def assessment_authorization(order: OrderEnvelope) -> AssessmentCertificate:
    return AssessmentCertificate(
        certificate_id=order.authorization_certificate_id,
        assessment_id=UUID("00000000-0000-0000-0000-000000000204"),
        thesis_version_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
        account_role=AccountRole.SUBMISSION,
        action=order.action,
        position_fingerprint=order.position_or_book_fingerprint,
        envelope_hash=order_envelope_hash(order),
        approved_max_loss=order.approved_max_loss,
        quantity=order.quantity,
        expected_after_exposure=None,
        policy_hash=order.policy_hash,
        created_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=1),
    )


def lifecycle_envelope(action: ExecutionAction = ExecutionAction.CLOSE) -> OrderEnvelope:
    return replace(
        envelope(action),
        authorization_certificate_id=UUID("00000000-0000-0000-0000-000000000207"),
    )


def repository(
    *,
    register_authorization: bool = True,
    entry_limits: EntryBudgetLimits = ENTRY_LIMITS,
) -> InMemoryExecutionRepository:
    repo = InMemoryExecutionRepository(clock=lambda: NOW, entry_limits=entry_limits)
    repo.register_account(
        role=AccountRole.SUBMISSION,
        fingerprint="account-fingerprint",
        equity=Decimal("100000"),
        autonomous_enabled=True,
    )
    repo.capture_baseline(
        role=AccountRole.SUBMISSION,
        fingerprint="account-fingerprint",
        equity=Decimal("100000"),
        captured_at=NOW,
        positions_hash="positions",
        orders_hash="orders",
        activities_hash="activities",
    )
    repo.set_autonomous_enabled(AccountRole.SUBMISSION, True, actor=Actor.OWNER)
    if register_authorization:
        repo.add_entry_approval(entry_approval())
    return repo


def terminal_attempt(*, state: str = "FILLED", filled: int = 2) -> OrderAttempt:
    order = envelope()
    identifier = client_order_id(order.trading_day, order.action, intent_digest(order), 0)
    return OrderAttempt(
        INTENT_ID,
        0,
        identifier,
        attempt_request_hash(intent_digest(order), 0, identifier, order.minimum_limit, None),
        state,
        provider_order_id="broker-order",
        filled_quantity=filled,
        quantity=order.quantity,
        fill_cash_flow=Decimal("-120") * filled if filled else None,
    )


def certificate_candidate(*, status: str = "FILLED") -> ExecutionCertificate:
    order = envelope()
    return ExecutionCertificate(
        certificate_id=uuid5(NAMESPACE_URL, f"alphadecay:execution:{intent_digest(order)}"),
        intent_id=INTENT_ID,
        entry_approval_id=AUTH_ID,
        assessment_certificate_id=None,
        execution_status=status,
        attempt_ids=(terminal_attempt().client_order_id,),
        actual_exposure=reconciliation().actual_exposure,
        reconciliation_checks=(
            "TERMINAL",
            "REMAINDER_ABSENT",
            "WHOLE_ACCOUNT_RECONCILED",
        ),
        created_at=NOW - timedelta(days=1),
    )


def test_in_memory_execution_rejects_replay_role() -> None:
    repo = InMemoryExecutionRepository(clock=lambda: NOW)

    with pytest.raises(ExecutionBlocked, match="REPLAY_EXECUTION_FORBIDDEN"):
        repo.register_account(
            role=AccountRole.REPLAY,
            fingerprint="replay-fingerprint",
            equity=Decimal("100000"),
            autonomous_enabled=False,
        )


def test_in_memory_accounts_require_unique_fingerprints_across_roles() -> None:
    repo = InMemoryExecutionRepository(clock=lambda: NOW)
    repo.register_account(
        role=AccountRole.SUBMISSION,
        fingerprint="shared-fingerprint",
        equity=Decimal("100000"),
        autonomous_enabled=False,
    )

    with pytest.raises(ExecutionBlocked, match="ACCOUNT_FINGERPRINT_ALREADY_REGISTERED"):
        repo.register_account(
            role=AccountRole.DEVELOPMENT,
            fingerprint="shared-fingerprint",
            equity=Decimal("100000"),
            autonomous_enabled=False,
        )


def test_claim_uses_trusted_clock_instead_of_caller_time() -> None:
    repo = repository(register_authorization=False)
    repo.add_entry_approval(replace(entry_approval(), expires_at=NOW))
    repo.approve_intent(INTENT_ID, AccountRole.SUBMISSION, envelope())

    with pytest.raises(ExecutionBlocked, match="AUTHORIZATION_EXPIRED"):
        _claim(
            repo,
            INTENT_ID,
            Actor.SCHEDULER,
            now=NOW - timedelta(seconds=30),
        )


def test_attempt_mutations_require_claimed_intent_and_exact_immutable_ordinal() -> None:
    repo = repository()
    repo.approve_intent(INTENT_ID, AccountRole.SUBMISSION, envelope())
    attempt = terminal_attempt(state="SUBMITTING", filled=0)

    with pytest.raises(ExecutionBlocked, match="INTENT_NOT_CLAIMED"):
        repo.add_attempt(attempt)

    _claim(repo, INTENT_ID, Actor.SCHEDULER, now=NOW)
    repo.add_attempt(attempt)
    with pytest.raises(ExecutionBlocked, match="ATTEMPT_IMMUTABLE_FIELDS_MISMATCH"):
        repo.replace_attempt(replace(attempt, request_hash="tampered"))


def test_attempt_observations_never_lose_fill_or_rewrite_broker_identity() -> None:
    repo = repository()
    repo.approve_intent(INTENT_ID, AccountRole.SUBMISSION, envelope())
    _claim(repo, INTENT_ID, Actor.SCHEDULER, now=NOW)
    attempt = terminal_attempt(state="SUBMITTING", filled=0)
    attempt = replace(attempt, provider_order_id=None, quantity=0)
    repo.add_attempt(attempt)
    observed = replace(
        attempt,
        state="PARTIALLY_FILLED",
        provider_order_id="broker-order",
        filled_quantity=1,
        quantity=2,
        fill_cash_flow=Decimal("-120"),
    )
    repo.replace_attempt(observed)

    stale_updates = (
        (replace(observed, filled_quantity=0), "ATTEMPT_FILL_REGRESSION"),
        (replace(observed, provider_order_id="different-order"), "ATTEMPT_PROVIDER_ID_MISMATCH"),
        (replace(observed, quantity=1), "ATTEMPT_QUANTITY_MISMATCH"),
    )
    for stale, code in stale_updates:
        with pytest.raises(ExecutionBlocked, match=code):
            repo.replace_attempt(stale)

    terminal = replace(observed, state="CANCELED")
    repo.replace_attempt(terminal)
    with pytest.raises(ExecutionBlocked, match="ATTEMPT_TERMINAL_STATE_REGRESSION"):
        repo.replace_attempt(replace(terminal, state="NEW"))
    with pytest.raises(ExecutionBlocked, match="ATTEMPT_STATE_FILL_INVALID"):
        repo.replace_attempt(replace(terminal, filled_quantity=2, fill_cash_flow=Decimal("-240")))
    assert repo.attempts_for(INTENT_ID)[0].filled_quantity == 1


def test_concurrent_attempt_observations_preserve_the_highest_fill() -> None:
    repo = repository()
    repo.approve_intent(INTENT_ID, AccountRole.SUBMISSION, envelope())
    _claim(repo, INTENT_ID, Actor.SCHEDULER, now=NOW)
    attempt = replace(terminal_attempt(state="PARTIALLY_FILLED", filled=1), quantity=2)
    repo.add_attempt(attempt)
    barrier = Barrier(2)

    def observe(filled_quantity: int) -> str:
        barrier.wait()
        try:
            state = "FILLED" if filled_quantity == attempt.quantity else attempt.state
            repo.replace_attempt(
                replace(
                    attempt,
                    state=state,
                    filled_quantity=filled_quantity,
                    fill_cash_flow=(Decimal("-120") * filled_quantity if filled_quantity else None),
                )
            )
        except ExecutionBlocked as error:
            return str(error)
        return "UPDATED"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(observe, (0, 2)))

    assert "ATTEMPT_FILL_REGRESSION" in outcomes
    assert repo.attempts_for(INTENT_ID)[0].filled_quantity == 2


def test_finalize_execution_is_atomic_and_validates_reconciled_lineage() -> None:
    repo = repository()
    repo.approve_intent(INTENT_ID, AccountRole.SUBMISSION, envelope())
    _claim(repo, INTENT_ID, Actor.SCHEDULER, now=NOW)
    repo.add_attempt(terminal_attempt())

    with pytest.raises(ExecutionBlocked, match="CERTIFICATE_ID_MISMATCH"):
        repo.finalize_execution(
            replace(certificate_candidate(), certificate_id=UUID(int=99)),
            reconciliation(matches=False),
            "FILLED",
        )

    assert repo.get_intent(INTENT_ID).state == IntentState.CLAIMED
    assert repo.get_entry_budget(AccountRole.SUBMISSION).entries_used == 0
    assert repo.get_execution_lock(AccountRole.SUBMISSION).locked is False
    certificate = repo.finalize_execution(certificate_candidate(), reconciliation(), "FILLED")
    assert certificate.created_at == NOW
    assert repo.get_intent(INTENT_ID).state == IntentState.TERMINAL
    assert repo.get_entry_budget(AccountRole.SUBMISSION).entries_used == 1


@pytest.mark.parametrize(
    ("state", "status"),
    [
        ("CANCELED", "PARTIAL_CANCELED_RECONCILED"),
        ("EXPIRED", "PARTIAL_EXPIRED_RECONCILED"),
        ("REPLACED", "PARTIAL_REPLACED_RECONCILED"),
    ],
)
def test_repository_finalizes_supported_partial_terminal_outcomes(
    state: str,
    status: str,
) -> None:
    repo = repository()
    repo.approve_intent(INTENT_ID, AccountRole.SUBMISSION, envelope())
    _claim(repo, INTENT_ID, Actor.SCHEDULER, now=NOW)
    repo.add_attempt(terminal_attempt(state=state, filled=1))

    certificate = repo.finalize_execution(
        certificate_candidate(status=status),
        reconciliation(),
        status,
    )

    assert certificate.execution_status == status
    assert repo.get_entry_budget(AccountRole.SUBMISSION).entries_used == 1


@pytest.mark.parametrize("state", ["CANCELED", "EXPIRED", "REPLACED"])
def test_repository_rejects_impossible_full_fill_terminal_outcomes(state: str) -> None:
    repo = repository()
    repo.approve_intent(INTENT_ID, AccountRole.SUBMISSION, envelope())
    _claim(repo, INTENT_ID, Actor.SCHEDULER, now=NOW)
    repo.add_attempt(terminal_attempt(state=state, filled=2))

    with pytest.raises(ExecutionBlocked, match="ATTEMPT_STATE_FILL_INVALID"):
        repo.finalize_execution(
            certificate_candidate(status="FILLED"),
            reconciliation(),
            "FILLED",
        )


def test_repository_treats_full_calculated_fill_as_filled_pending_settlement() -> None:
    repo = repository()
    repo.approve_intent(INTENT_ID, AccountRole.SUBMISSION, envelope())
    _claim(repo, INTENT_ID, Actor.SCHEDULER, now=NOW)
    repo.add_attempt(terminal_attempt(state="CALCULATED", filled=2))

    certificate = repo.finalize_execution(
        certificate_candidate(status="FILLED"),
        reconciliation(),
        "FILLED",
    )

    assert certificate.execution_status == "FILLED"


def test_digest_and_client_ids_are_stable_bounded_and_attempt_specific() -> None:
    digest = intent_digest(envelope())

    assert digest == "6484bdad825346cd4e17878ecae26b6404f229ffa0357b652892f22bb72f341c"
    assert client_order_id(date(2026, 9, 3), ExecutionAction.ENTRY, digest, 0) == (
        "ad-20260903-e-6484bdad825346cd4e17878e-a0"
    )
    assert len(client_order_id(date(2026, 9, 3), ExecutionAction.ROLL, digest, 3)) <= 64

    initial_id = client_order_id(date(2026, 9, 3), ExecutionAction.ENTRY, digest, 0)
    initial_hash = attempt_request_hash(digest, 0, initial_id, Decimal("1.00"), None)
    assert len(initial_hash) == 64
    assert initial_hash == attempt_request_hash(digest, 0, initial_id, Decimal("1.00"), None)
    assert initial_hash != attempt_request_hash(
        digest,
        1,
        client_order_id(date(2026, 9, 3), ExecutionAction.ENTRY, digest, 1),
        Decimal("1.00"),
        initial_id,
    )


def test_order_envelope_uses_a_generic_structural_quantity_boundary() -> None:
    assert replace(envelope(), quantity=MAX_STRUCTURAL_OPTION_QUANTITY).quantity == (
        MAX_STRUCTURAL_OPTION_QUANTITY
    )
    with pytest.raises(ValueError, match="ORDER_STRUCTURE_OUT_OF_BOUNDS"):
        replace(envelope(), quantity=MAX_STRUCTURAL_OPTION_QUANTITY + 1)


def test_scheduler_claim_reserves_risk_and_finalization_consumes_it_once() -> None:
    repo = repository()
    intent = repo.approve_intent(INTENT_ID, AccountRole.SUBMISSION, envelope())

    _claim(repo, intent.intent_id, Actor.SCHEDULER, now=NOW)
    repo.add_attempt(terminal_attempt())
    repo.finalize_execution(certificate_candidate(), reconciliation(), "FILLED")
    with pytest.raises(ExecutionBlocked, match="CERTIFICATE_IMMUTABLE"):
        repo.finalize_execution(certificate_candidate(), reconciliation(), "FILLED")

    budget = repo.get_entry_budget(AccountRole.SUBMISSION)
    assert budget.reserved_intent_id is None
    assert budget.entries_used == 1
    assert budget.gross_approved_risk == Decimal("700")


@pytest.mark.parametrize(
    ("limits", "code"),
    [
        (replace(ENTRY_LIMITS, policy_hash="b" * 64), "ENTRY_POLICY_AUTHORITY_MISMATCH"),
        (replace(ENTRY_LIMITS, maximum_entry_quantity=1), "ENTRY_QUANTITY_EXHAUSTED"),
        (
            replace(ENTRY_LIMITS, maximum_position_loss=Decimal("600")),
            "ENTRY_POSITION_RISK_EXHAUSTED",
        ),
    ],
)
def test_memory_entry_claim_enforces_the_injected_policy_limits(
    limits: EntryBudgetLimits,
    code: str,
) -> None:
    repo = repository(entry_limits=limits)
    repo.approve_intent(INTENT_ID, AccountRole.SUBMISSION, envelope())

    with pytest.raises(ExecutionBlocked, match=code):
        _claim(repo, INTENT_ID, Actor.SCHEDULER, now=NOW)


def test_entry_claim_fails_closed_without_limit_authority() -> None:
    repo = InMemoryExecutionRepository(clock=lambda: NOW)
    repo.register_account(
        role=AccountRole.DEVELOPMENT,
        fingerprint="account-fingerprint",
        equity=Decimal("100000"),
        autonomous_enabled=True,
    )
    repo.add_entry_approval(replace(entry_approval(), account_role=AccountRole.DEVELOPMENT))
    repo.set_autonomous_enabled(AccountRole.DEVELOPMENT, True, actor=Actor.OWNER)
    repo.approve_intent(INTENT_ID, AccountRole.DEVELOPMENT, envelope())

    with pytest.raises(ExecutionBlocked, match="ENTRY_LIMITS_REQUIRED"):
        repo.claim_intent(
            INTENT_ID,
            Actor.SCHEDULER,
            now=NOW,
            account_role=AccountRole.DEVELOPMENT,
            account_fingerprint="account-fingerprint",
        )


def test_zero_fill_releases_reservation_but_blocks_event_day() -> None:
    repo = repository()
    approved = repo.approve_intent(INTENT_ID, AccountRole.SUBMISSION, envelope())
    _claim(repo, approved.intent_id, Actor.SCHEDULER, now=NOW)
    repo.add_attempt(terminal_attempt(state="CANCELED", filled=0))
    repo.finalize_execution(certificate_candidate(status="UNFILLED"), reconciliation(), "UNFILLED")

    budget = repo.get_entry_budget(AccountRole.SUBMISSION)
    assert budget.entries_used == 0
    assert budget.gross_approved_risk == 0
    with pytest.raises(ExecutionBlocked, match="EVENT_DAY_ALREADY_USED"):
        repo.approve_intent(
            UUID("00000000-0000-0000-0000-000000000203"),
            AccountRole.SUBMISSION,
            replace(envelope(), policy_hash="b" * 64),
        )


def test_event_day_lock_applies_to_entries_but_not_lifecycle_actions() -> None:
    repo = repository()
    entry = repo.approve_intent(INTENT_ID, AccountRole.SUBMISSION, envelope())
    _claim(repo, entry.intent_id, Actor.SCHEDULER, now=NOW)
    repo.add_attempt(terminal_attempt(state="CANCELED", filled=0))
    repo.finalize_execution(certificate_candidate(status="UNFILLED"), reconciliation(), "UNFILLED")

    lifecycle_order = lifecycle_envelope()
    repo.add_assessment_certificate(assessment_authorization(lifecycle_order))
    lifecycle = repo.approve_intent(
        UUID("00000000-0000-0000-0000-000000000205"),
        AccountRole.SUBMISSION,
        lifecycle_order,
    )

    assert _claim(repo, lifecycle.intent_id, Actor.OWNER, now=NOW).state.value == "CLAIMED"


def test_lifecycle_close_can_reconcile_above_the_private_entry_quantity_limit() -> None:
    repo = repository()
    order = replace(
        lifecycle_envelope(),
        quantity=ENTRY_LIMITS.maximum_entry_quantity + 1,
    )
    repo.add_assessment_certificate(assessment_authorization(order))
    intent = repo.approve_intent(
        UUID("00000000-0000-0000-0000-000000000206"),
        AccountRole.SUBMISSION,
        order,
    )

    assert _claim(repo, intent.intent_id, Actor.OWNER, now=NOW).state is IntentState.CLAIMED


def test_only_one_entry_intent_can_be_active_per_account() -> None:
    repo = repository()
    repo.approve_intent(INTENT_ID, AccountRole.SUBMISSION, envelope())

    with pytest.raises(ExecutionBlocked, match="ENTRY_INTENT_ACTIVE"):
        repo.approve_intent(
            UUID("00000000-0000-0000-0000-000000000206"),
            AccountRole.SUBMISSION,
            replace(
                envelope(),
                event_key="PANW-2026-09-04",
                trading_day=date(2026, 9, 4),
                policy_hash="b" * 64,
            ),
        )


def test_account_execution_lease_allows_only_one_claimed_intent_across_actions() -> None:
    repo = repository(register_authorization=False)
    first_order = lifecycle_envelope()
    second_order = replace(
        first_order,
        action=ExecutionAction.ROLL,
        authorization_certificate_id=UUID("00000000-0000-0000-0000-000000000208"),
        legs=first_order.legs
        + (
            OrderLegIntent("DEMO261016C00105000", PositionIntent.BUY_TO_OPEN, 1),
            OrderLegIntent("DEMO261016C00110000", PositionIntent.SELL_TO_OPEN, 1),
        ),
        event_key="PANW-2026-09-04",
        trading_day=date(2026, 9, 4),
        market_session_id=UUID("00000000-0000-0000-0000-000000000209"),
        quoted_relative_spread=Decimal("0.05"),
        maximum_relative_spread=Decimal("0.25"),
        incremental_debit=Decimal("100"),
        maximum_incremental_debit=Decimal("500"),
    )
    repo.add_assessment_certificate(assessment_authorization(first_order))
    repo.add_assessment_certificate(
        replace(
            assessment_authorization(second_order),
            assessment_id=UUID("00000000-0000-0000-0000-000000000210"),
        )
    )
    first = repo.approve_intent(INTENT_ID, AccountRole.SUBMISSION, first_order)
    second = repo.approve_intent(
        UUID("00000000-0000-0000-0000-000000000211"),
        AccountRole.SUBMISSION,
        second_order,
    )

    _claim(repo, first.intent_id, Actor.OWNER, now=NOW)
    with pytest.raises(ExecutionBlocked, match="ACCOUNT_EXECUTION_LEASE_ACTIVE"):
        _claim(repo, second.intent_id, Actor.OWNER, now=NOW)

    repo.release_unsubmitted_claim(first.intent_id)
    assert _claim(repo, second.intent_id, Actor.OWNER, now=NOW).state == IntentState.CLAIMED


def test_owner_cannot_claim_entry_and_disabled_scheduler_cannot_claim() -> None:
    repo = repository()
    approved = repo.approve_intent(INTENT_ID, AccountRole.SUBMISSION, envelope())
    with pytest.raises(ExecutionBlocked, match="OWNER_ENTRY_FORBIDDEN"):
        _claim(repo, approved.intent_id, Actor.OWNER, now=NOW)

    repo.set_autonomous_enabled(AccountRole.SUBMISSION, False, actor=Actor.OWNER)
    with pytest.raises(ExecutionBlocked, match="AUTONOMOUS_DISABLED"):
        _claim(repo, approved.intent_id, Actor.SCHEDULER, now=NOW)


def test_account_refresh_cannot_reenable_autonomy() -> None:
    repo = repository()
    approved = repo.approve_intent(INTENT_ID, AccountRole.SUBMISSION, envelope())
    repo.set_autonomous_enabled(AccountRole.SUBMISSION, False, actor=Actor.OWNER)

    repo.register_account(
        role=AccountRole.SUBMISSION,
        fingerprint="account-fingerprint",
        equity=Decimal("100000"),
        autonomous_enabled=True,
    )

    with pytest.raises(ExecutionBlocked, match="AUTONOMOUS_DISABLED"):
        _claim(repo, approved.intent_id, Actor.SCHEDULER, now=NOW)


def test_submission_entry_requires_clean_baseline_and_per_position_risk_cap() -> None:
    repo = InMemoryExecutionRepository(clock=lambda: NOW, entry_limits=ENTRY_LIMITS)
    repo.register_account(
        role=AccountRole.SUBMISSION,
        fingerprint="account-fingerprint",
        equity=Decimal("100000"),
        autonomous_enabled=True,
    )
    repo.add_entry_approval(entry_approval())
    repo.set_autonomous_enabled(AccountRole.SUBMISSION, True, actor=Actor.OWNER)
    approved = repo.approve_intent(INTENT_ID, AccountRole.SUBMISSION, envelope())
    with pytest.raises(ExecutionBlocked, match="SUBMISSION_BASELINE_REQUIRED"):
        _claim(repo, approved.intent_id, Actor.SCHEDULER, now=NOW)

    oversized = replace(envelope(), approved_max_loss=Decimal("801"))
    oversized_repo = InMemoryExecutionRepository(clock=lambda: NOW, entry_limits=ENTRY_LIMITS)
    oversized_repo.register_account(
        role=AccountRole.DEVELOPMENT,
        fingerprint="account-fingerprint",
        equity=Decimal("100000"),
        autonomous_enabled=True,
    )
    oversized_repo.add_entry_approval(
        replace(
            entry_approval(oversized),
            account_role=AccountRole.DEVELOPMENT,
        )
    )
    oversized_repo.set_autonomous_enabled(AccountRole.DEVELOPMENT, True, actor=Actor.OWNER)
    oversized_repo.approve_intent(INTENT_ID, AccountRole.DEVELOPMENT, oversized)
    with pytest.raises(ExecutionBlocked, match="ENTRY_POSITION_RISK_EXHAUSTED"):
        oversized_repo.claim_intent(
            INTENT_ID,
            Actor.SCHEDULER,
            now=NOW,
            account_role=AccountRole.DEVELOPMENT,
            account_fingerprint="account-fingerprint",
        )


def test_retired_execution_service_fails_before_any_fixture_write() -> None:
    repo = repository()
    repo.approve_intent(INTENT_ID, AccountRole.SUBMISSION, envelope())
    broker = FakeBroker([])

    with pytest.raises(ExecutionBlocked, match="BROKER_WRITE_AUTHORITY_NOT_INTEGRATED"):
        _execution_service(repo, broker, FakePreflight()).execute(INTENT_ID, Actor.SCHEDULER, NOW)

    assert repo.get_intent(INTENT_ID).state == IntentState.APPROVED
    assert repo.attempts_for(INTENT_ID) == ()
    assert broker.calls == []


def test_baseline_is_immutable_and_contamination_suppresses_return() -> None:
    repo = repository()
    with pytest.raises(ExecutionBlocked, match="BASELINE_ALREADY_CAPTURED"):
        repo.capture_baseline(
            role=AccountRole.SUBMISSION,
            fingerprint="account-fingerprint",
            equity=Decimal("100000"),
            captured_at=NOW,
            positions_hash="other",
            orders_hash="orders",
            activities_hash="activities",
        )

    repo.observe_account_adjustment(AccountRole.SUBMISSION, "TRANSFER")
    assert repo.normalized_return(AccountRole.SUBMISSION, Decimal("101000")) is None


def test_second_claim_and_certificate_overwrite_are_rejected() -> None:
    repo = repository()
    with pytest.raises(ExecutionBlocked, match="AUTHORIZATION_IMMUTABLE"):
        repo.add_entry_approval(entry_approval())
    approved = repo.approve_intent(INTENT_ID, AccountRole.SUBMISSION, envelope())
    _claim(repo, approved.intent_id, Actor.SCHEDULER, now=NOW)
    with pytest.raises(ExecutionBlocked, match="INTENT_ALREADY_CLAIMED"):
        _claim(repo, approved.intent_id, Actor.SCHEDULER, now=NOW)

    certificate = assessment_authorization(
        replace(
            envelope(ExecutionAction.CLOSE),
            authorization_certificate_id=UUID("00000000-0000-0000-0000-000000000207"),
        )
    )
    repo.add_assessment_certificate(certificate)
    with pytest.raises(ExecutionBlocked, match="CERTIFICATE_IMMUTABLE"):
        repo.add_assessment_certificate(certificate)
    assert repo.get_assessment_certificate(certificate.certificate_id) == certificate

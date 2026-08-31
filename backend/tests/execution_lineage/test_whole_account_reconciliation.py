import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from inspect import signature
from uuid import UUID

import pytest

from backend.app.contracts.v1 import AccountRole, PositionIntent
from backend.app.execution import (
    AccountObservation,
    AccountReconciliationState,
    ActivityItem,
    ActivityPaginationEvidence,
    ActivityType,
    InventoryItem,
    InventoryKind,
    OpenOrderItem,
    OpenOrderLeg,
    ReconciliationBlockCode,
    ReconciliationPurpose,
    SweepObservation,
)
from backend.app.execution.reconciliation import (
    ReconciliationExpectation,
    WholeAccountReconciliation,
)
from backend.app.order_limits import MAX_STRUCTURAL_OPTION_QUANTITY

NOW = datetime(2026, 8, 28, 19, 15, tzinfo=UTC)
BASELINE = datetime(2026, 8, 27, 15, 0, tzinfo=UTC)


def test_default_activity_provenance_preserves_legacy_state_hash_material() -> None:
    funding = ActivityItem(
        activity_id_hash="b" * 64,
        activity_type=ActivityType.INITIAL_FUNDING,
        occurred_at=BASELINE - timedelta(minutes=1),
        symbol=None,
        signed_quantity=Decimal("100000"),
    )
    state = AccountReconciliationState._from_repository_state(
        account_role=AccountRole.SUBMISSION,
        account_fingerprint="a" * 64,
        baseline_captured_at=BASELINE,
        accepted_at=NOW,
        expected_cash=Decimal("100000"),
        expected_positions=(),
        expected_open_orders=(),
        known_activities=(funding,),
        activity_complete_through=BASELINE,
    )
    legacy_material = {
        "domain": "alphadecay.account-reconciliation-state.v1",
        "account_role": "SUBMISSION",
        "account_fingerprint": "a" * 64,
        "baseline_captured_at": "2026-08-27T15:00:00Z",
        "accepted_at": "2026-08-28T19:15:00Z",
        "expected_cash": "100000",
        "expected_positions": [],
        "expected_open_orders": [],
        "known_activities": [
            {
                "activity_id_hash": "b" * 64,
                "activity_type": "INITIAL_FUNDING",
                "occurred_at": "2026-08-27T14:59:00Z",
                "symbol": None,
                "signed_quantity": "100000",
                "provider_order_id": None,
                "client_order_id": None,
            }
        ],
        "activity_complete_through": "2026-08-27T15:00:00Z",
    }
    expected = hashlib.sha256(
        json.dumps(legacy_material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    assert state.state_hash == expected


def account(*, observed_at: datetime = NOW - timedelta(seconds=2)) -> AccountObservation:
    return AccountObservation(
        role=AccountRole.SUBMISSION,
        account_fingerprint="a" * 64,
        paper=True,
        status="ACTIVE",
        account_blocked=False,
        trading_blocked=False,
        options_trading_blocked=False,
        equity=Decimal("100100"),
        buying_power=Decimal("99000"),
        cash=Decimal("100000"),
        observed_at=observed_at,
        time_quality="RETRIEVAL_TIME_ONLY",
    )


def positions() -> tuple[InventoryItem, ...]:
    return (
        InventoryItem(
            kind=InventoryKind.OPTION,
            symbol="DEMO260918C00100000",
            signed_quantity=Decimal("1"),
            multiplier=100,
        ),
        InventoryItem(
            kind=InventoryKind.OPTION,
            symbol="DEMO260918C00110000",
            signed_quantity=Decimal("-1"),
            multiplier=100,
        ),
    )


def orders() -> tuple[OpenOrderItem, ...]:
    return (
        OpenOrderItem(
            provider_order_id="provider-1",
            client_order_id="client-1",
            state="NEW",
            quantity=1,
            filled_quantity=0,
            replaces_client_order_id=None,
            replaced_by_client_order_id=None,
            order_class="MLEG",
            legs=(
                OpenOrderLeg("DEMO260918C00100000", PositionIntent.SELL_TO_CLOSE, 1),
                OpenOrderLeg("DEMO260918C00110000", PositionIntent.BUY_TO_CLOSE, 1),
            ),
        ),
    )


def pagination(**changes: object) -> ActivityPaginationEvidence:
    values: dict[str, object] = {
        "requested_start": BASELINE,
        "requested_end": NOW - timedelta(seconds=2),
        "retrieved_through": NOW - timedelta(seconds=2),
        "established_at": NOW - timedelta(seconds=1),
        "page_count": 1,
        "terminal_page_seen": True,
        "visibility_complete_through": BASELINE,
        "visibility_horizon": timedelta(hours=24),
    }
    values.update(changes)
    return ActivityPaginationEvidence(**values)


def sweep(**changes: object) -> SweepObservation:
    values: dict[str, object] = {
        "retrieval_started_at": NOW - timedelta(seconds=5),
        "retrieval_completed_at": NOW - timedelta(seconds=1),
        "activity_pagination": pagination(),
        "first_account": account(),
        "final_account": account(observed_at=NOW - timedelta(seconds=1)),
        "first_positions": positions(),
        "final_positions": positions(),
        "first_open_orders": orders(),
        "final_open_orders": orders(),
        "activities": (),
        "positions_complete": True,
        "orders_complete": True,
    }
    values.update(changes)
    return SweepObservation(**values)


def expectation(**changes: object) -> ReconciliationExpectation:
    values: dict[str, object] = {
        "purpose": ReconciliationPurpose.CANCEL,
        "account_role": AccountRole.SUBMISSION,
        "account_fingerprint": "a" * 64,
        "expected_cash": Decimal("100000"),
        "baseline_captured_at": BASELINE,
        "expected_positions": positions(),
        "expected_open_orders": orders(),
        "known_activities": (),
        "resolved_activity_hashes": (),
        "required_activity_window_start": BASELINE,
        "required_activity_complete_through": BASELINE,
        "intent_id": UUID("00000000-0000-0000-0000-000000000601"),
        "intent_digest": "b" * 64,
        "attempt_ordinal": 0,
        "request_hash": "c" * 64,
    }
    values.update(changes)
    return ReconciliationExpectation._from_repository_state(**values)


def test_stable_complete_sweep_is_safe_and_hash_is_deterministic() -> None:
    first = WholeAccountReconciliation.evaluate(sweep(), expectation(), accepted_at=NOW)
    second = WholeAccountReconciliation.evaluate(sweep(), expectation(), accepted_at=NOW)

    assert first.safe is True
    assert first.block_codes == ()
    assert first.reconciliation_hash == second.reconciliation_hash
    assert first.reconciliation_id == second.reconciliation_id
    assert len(first.reconciliation_hash) == 64
    assert len(first.positions_manifest_hash) == 64
    assert len(first.orders_manifest_hash) == 64
    assert len(first.activities_manifest_hash) == 64


@pytest.mark.parametrize(
    ("changes", "expected_code"),
    [
        (
            {
                "retrieval_started_at": NOW - timedelta(seconds=20),
                "first_account": account(observed_at=NOW - timedelta(seconds=18)),
                "final_account": account(observed_at=NOW - timedelta(seconds=16)),
                "activity_pagination": pagination(
                    requested_end=NOW - timedelta(seconds=18),
                    retrieved_through=NOW - timedelta(seconds=18),
                    established_at=NOW - timedelta(seconds=17),
                ),
            },
            ReconciliationBlockCode.STALE_OBSERVATION,
        ),
        ({"orders_complete": False}, ReconciliationBlockCode.INCOMPLETE_SWEEP),
        (
            {"activity_pagination": pagination(terminal_page_seen=False)},
            ReconciliationBlockCode.ACTIVITY_WATERMARK_UNKNOWN,
        ),
        ({"final_positions": ()}, ReconciliationBlockCode.UNSTABLE_SWEEP),
    ],
)
def test_stale_incomplete_or_unstable_sweep_blocks(
    changes: dict[str, object], expected_code: ReconciliationBlockCode
) -> None:
    result = WholeAccountReconciliation.evaluate(sweep(**changes), expectation(), accepted_at=NOW)

    assert result.safe is False
    assert expected_code in result.block_codes


def test_wrong_account_and_foreign_order_block() -> None:
    foreign = orders() + (
        OpenOrderItem(
            provider_order_id="provider-foreign",
            client_order_id="client-foreign",
            state="NEW",
            quantity=1,
            filled_quantity=0,
            replaces_client_order_id=None,
            replaced_by_client_order_id=None,
            order_class="MLEG",
            legs=(
                OpenOrderLeg("OTHER260918P00090000", PositionIntent.BUY_TO_CLOSE, 1),
                OpenOrderLeg("OTHER260918P00080000", PositionIntent.SELL_TO_CLOSE, 1),
            ),
        ),
    )
    result = WholeAccountReconciliation.evaluate(
        sweep(first_open_orders=foreign, final_open_orders=foreign),
        expectation(account_fingerprint="f" * 64),
        accepted_at=NOW,
    )

    assert result.safe is False
    assert ReconciliationBlockCode.ACCOUNT_MISMATCH in result.block_codes
    assert ReconciliationBlockCode.UNEXPECTED_OPEN_ORDER in result.block_codes


def test_unexpected_inventory_blocks_even_without_assignment_activity() -> None:
    unexpected = (
        InventoryItem(
            kind=InventoryKind.EQUITY,
            symbol="DEMO",
            signed_quantity=Decimal("100"),
            multiplier=1,
        ),
    ) + positions()
    result = WholeAccountReconciliation.evaluate(
        sweep(first_positions=unexpected, final_positions=unexpected),
        expectation(),
        accepted_at=NOW,
    )

    assert result.safe is False
    assert ReconciliationBlockCode.UNEXPECTED_INVENTORY in result.block_codes
    assert ReconciliationBlockCode.ASSIGNMENT_SUSPECTED in result.block_codes


def test_assignment_or_cash_adjustment_activity_blocks() -> None:
    activities = (
        ActivityItem(
            activity_id_hash="d" * 64,
            activity_type=ActivityType.OPASN,
            occurred_at=NOW - timedelta(minutes=1),
            symbol="DEMO260918C00100000",
            signed_quantity=Decimal("-1"),
        ),
        ActivityItem(
            activity_id_hash="e" * 64,
            activity_type=ActivityType.JOURNAL,
            occurred_at=NOW - timedelta(minutes=2),
            symbol=None,
            signed_quantity=None,
        ),
    )
    result = WholeAccountReconciliation.evaluate(
        sweep(activities=activities), expectation(), accepted_at=NOW
    )

    assert result.safe is False
    assert ReconciliationBlockCode.ASSIGNMENT_SUSPECTED in result.block_codes
    assert ReconciliationBlockCode.ACCOUNT_ADJUSTMENT in result.block_codes


def test_resolved_known_adjustment_does_not_permanently_block_later_sweeps() -> None:
    journal = ActivityItem(
        activity_id_hash="f" * 64,
        activity_type=ActivityType.JOURNAL,
        occurred_at=NOW - timedelta(days=1),
        symbol=None,
        signed_quantity=Decimal("100000"),
        time_quality="DATE_ONLY",
        provider_activity_type="JNLC",
    )

    unresolved = WholeAccountReconciliation.evaluate(
        sweep(activities=(journal,)),
        expectation(known_activities=(journal,)),
        accepted_at=NOW,
    )
    resolved = WholeAccountReconciliation.evaluate(
        sweep(activities=(journal,)),
        expectation(
            known_activities=(journal,),
            resolved_activity_hashes=(journal.activity_id_hash,),
        ),
        accepted_at=NOW,
    )

    assert ReconciliationBlockCode.ACCOUNT_ADJUSTMENT in unresolved.block_codes
    assert ReconciliationBlockCode.ACCOUNT_ADJUSTMENT not in resolved.block_codes
    assert resolved.safe is True


def test_reconciliation_rejects_noncanonical_or_future_evidence() -> None:
    with pytest.raises(ValueError, match="INVENTORY_NOT_CANONICAL"):
        sweep(first_positions=tuple(reversed(positions())))

    result = WholeAccountReconciliation.evaluate(
        sweep(
            retrieval_completed_at=NOW + timedelta(seconds=1),
            final_account=account(observed_at=NOW + timedelta(seconds=1)),
        ),
        expectation(),
        accepted_at=NOW,
    )

    assert ReconciliationBlockCode.FUTURE_OBSERVATION in result.block_codes


def test_future_activity_blocks_and_unknown_activity_type_is_rejected() -> None:
    future = ActivityItem(
        activity_id_hash="d" * 64,
        activity_type=ActivityType.FILL,
        occurred_at=NOW + timedelta(seconds=1),
        symbol="DEMO260918C00100000",
        signed_quantity=Decimal("1"),
    )

    result = WholeAccountReconciliation.evaluate(
        sweep(activities=(future,)), expectation(), accepted_at=NOW
    )

    assert ReconciliationBlockCode.FUTURE_OBSERVATION in result.block_codes
    with pytest.raises(ValueError, match="ACTIVITY_TYPE_INVALID"):
        ActivityItem(
            activity_id_hash="e" * 64,
            activity_type="SURPRISE",  # type: ignore[arg-type]
            occurred_at=NOW,
            symbol=None,
            signed_quantity=None,
        )


def test_unreconciled_fill_activity_blocks_even_when_book_is_stable() -> None:
    fill = ActivityItem(
        activity_id_hash="d" * 64,
        activity_type=ActivityType.FILL,
        occurred_at=NOW - timedelta(minutes=1),
        symbol="DEMO260918C00100000",
        signed_quantity=Decimal("1"),
    )

    result = WholeAccountReconciliation.evaluate(
        sweep(activities=(fill,)), expectation(), accepted_at=NOW
    )

    assert ReconciliationBlockCode.UNEXPECTED_ACTIVITY in result.block_codes


def test_known_assignment_cannot_disappear_or_change_material() -> None:
    assignment = ActivityItem(
        activity_id_hash="d" * 64,
        activity_type=ActivityType.OPASN,
        occurred_at=NOW - timedelta(minutes=1),
        symbol="DEMO260918C00100000",
        signed_quantity=Decimal("1"),
    )
    changed = replace(assignment, signed_quantity=Decimal("2"))

    missing_result = WholeAccountReconciliation.evaluate(
        sweep(), expectation(known_activities=(assignment,)), accepted_at=NOW
    )
    changed_result = WholeAccountReconciliation.evaluate(
        sweep(activities=(changed,)),
        expectation(known_activities=(assignment,)),
        accepted_at=NOW,
    )

    assert ReconciliationBlockCode.KNOWN_ACTIVITY_MISSING in missing_result.block_codes
    assert ReconciliationBlockCode.KNOWN_ACTIVITY_MISSING in changed_result.block_codes
    assert missing_result.safe is False
    assert changed_result.safe is False


def test_resolved_assignment_requires_present_exact_evidence() -> None:
    assignment = ActivityItem(
        activity_id_hash="d" * 64,
        activity_type=ActivityType.OPASN,
        occurred_at=NOW - timedelta(minutes=1),
        symbol="DEMO260918C00100000",
        signed_quantity=Decimal("1"),
    )
    result = WholeAccountReconciliation.evaluate(
        sweep(activities=(assignment,)),
        expectation(
            known_activities=(assignment,),
            resolved_activity_hashes=(assignment.activity_id_hash,),
        ),
        accepted_at=NOW,
    )

    assert result.safe is True
    assert ReconciliationBlockCode.ASSIGNMENT_SUSPECTED not in result.block_codes


def test_activity_watermark_must_meet_repository_bound_and_retrieval_time() -> None:
    required = BASELINE + timedelta(hours=1)
    stale = WholeAccountReconciliation.evaluate(
        sweep(),
        expectation(required_activity_complete_through=required),
        accepted_at=NOW,
    )
    truncated = WholeAccountReconciliation.evaluate(
        sweep(activity_pagination=pagination(terminal_page_seen=False)),
        expectation(),
        accepted_at=NOW,
    )

    assert ReconciliationBlockCode.ACTIVITY_WATERMARK_UNKNOWN in stale.block_codes
    assert ReconciliationBlockCode.ACTIVITY_WATERMARK_UNKNOWN in truncated.block_codes


def test_activity_query_must_reach_the_first_account_bookend() -> None:
    with pytest.raises(ValueError, match="SWEEP_CHRONOLOGY_INVALID"):
        sweep(
            activity_pagination=pagination(
                requested_end=BASELINE,
                retrieved_through=BASELINE,
            )
        )


def test_activity_visibility_horizon_is_derived_from_times() -> None:
    with pytest.raises(ValueError, match="ACTIVITY_PAGINATION_INVALID"):
        pagination(
            visibility_complete_through=NOW - timedelta(seconds=2),
        )


def test_duplicate_provider_order_identity_is_rejected() -> None:
    duplicate = orders() + (
        OpenOrderItem(
            provider_order_id="provider-1",
            client_order_id="client-2",
            state="NEW",
            quantity=1,
            filled_quantity=0,
            replaces_client_order_id=None,
            replaced_by_client_order_id=None,
            order_class="MLEG",
            legs=(
                OpenOrderLeg("OTHER260918P00090000", PositionIntent.BUY_TO_CLOSE, 1),
                OpenOrderLeg("OTHER260918P00080000", PositionIntent.SELL_TO_CLOSE, 1),
            ),
        ),
    )

    with pytest.raises(ValueError, match="OPEN_ORDER_PROVIDER_ID_DUPLICATE"):
        sweep(first_open_orders=duplicate, final_open_orders=duplicate)


def test_changed_order_leg_intent_blocks_with_same_order_and_symbols() -> None:
    changed = (
        replace(
            orders()[0],
            legs=(
                OpenOrderLeg("DEMO260918C00100000", PositionIntent.BUY_TO_CLOSE, 1),
                OpenOrderLeg("DEMO260918C00110000", PositionIntent.SELL_TO_CLOSE, 1),
            ),
        ),
    )

    result = WholeAccountReconciliation.evaluate(
        sweep(first_open_orders=changed, final_open_orders=changed),
        expectation(),
        accepted_at=NOW,
    )

    assert ReconciliationBlockCode.UNEXPECTED_OPEN_ORDER in result.block_codes


def test_equal_decimal_representations_have_one_canonical_hash() -> None:
    alternate_positions = tuple(
        replace(item, signed_quantity=Decimal(f"{item.signed_quantity}.0")) for item in positions()
    )
    alternate_account = replace(
        account(), equity=Decimal("100100.0"), buying_power=Decimal("99000.00")
    )
    alternate_final_account = replace(
        account(observed_at=NOW - timedelta(seconds=1)),
        equity=Decimal("100100.00"),
        buying_power=Decimal("99000.0"),
    )
    first = WholeAccountReconciliation.evaluate(sweep(), expectation(), accepted_at=NOW)
    second = WholeAccountReconciliation.evaluate(
        sweep(
            first_account=alternate_account,
            final_account=alternate_final_account,
            first_positions=alternate_positions,
            final_positions=alternate_positions,
        ),
        expectation(expected_positions=alternate_positions),
        accepted_at=NOW,
    )

    assert first.reconciliation_hash == second.reconciliation_hash
    assert first.reconciliation_id == second.reconciliation_id


def test_mark_changes_do_not_make_an_unchanged_book_unstable() -> None:
    result = WholeAccountReconciliation.evaluate(
        sweep(
            first_account=replace(account(), equity=Decimal("100050")),
            final_account=replace(
                account(observed_at=NOW - timedelta(seconds=1)),
                equity=Decimal("100150"),
                buying_power=Decimal("98850"),
            ),
        ),
        expectation(),
        accepted_at=NOW,
    )

    assert result.safe is True
    assert ReconciliationBlockCode.UNSTABLE_SWEEP not in result.block_codes


def test_unexpected_cash_change_blocks_even_when_book_is_stable() -> None:
    changed = Decimal("100010")
    result = WholeAccountReconciliation.evaluate(
        sweep(
            first_account=replace(account(), cash=changed),
            final_account=replace(account(observed_at=NOW - timedelta(seconds=1)), cash=changed),
        ),
        expectation(),
        accepted_at=NOW,
    )

    assert ReconciliationBlockCode.ACCOUNT_ADJUSTMENT in result.block_codes
    assert result.safe is False


def test_sweep_requires_observations_in_retrieval_order() -> None:
    with pytest.raises(ValueError, match="SWEEP_CHRONOLOGY_INVALID"):
        sweep(
            first_account=account(observed_at=NOW - timedelta(seconds=1)),
            final_account=account(observed_at=NOW - timedelta(seconds=2)),
        )


def test_observed_order_accepts_the_generic_structural_quantity_boundary() -> None:
    observed = replace(
        orders()[0],
        quantity=MAX_STRUCTURAL_OPTION_QUANTITY,
        filled_quantity=0,
    )

    assert observed.quantity == MAX_STRUCTURAL_OPTION_QUANTITY


@pytest.mark.parametrize(
    "factory",
    [
        lambda: replace(account(), paper=1),  # type: ignore[arg-type]
        lambda: InventoryItem(
            kind="OPTION",  # type: ignore[arg-type]
            symbol="DEMO260918C00100000",
            signed_quantity=Decimal("1"),
            multiplier=100,
        ),
        lambda: InventoryItem(
            kind=InventoryKind.OPTION,
            symbol="DEMO260918C00100000",
            signed_quantity=Decimal("NaN"),
            multiplier=100,
        ),
        lambda: InventoryItem(
            kind=InventoryKind.OPTION,
            symbol="DEMO260918C00100000",
            signed_quantity=Decimal("1"),
            multiplier=1,
        ),
        lambda: replace(orders()[0], provider_order_id="bad id"),
        lambda: replace(orders()[0], quantity=MAX_STRUCTURAL_OPTION_QUANTITY + 1),
        lambda: OpenOrderLeg("DEMO260918C00100000", PositionIntent.SELL_TO_CLOSE, 7),
        lambda: ReconciliationExpectation._from_repository_state(
            purpose=ReconciliationPurpose.CANCEL,
            account_role=AccountRole.SUBMISSION,
            account_fingerprint="a" * 64,
            expected_cash=Decimal("100000"),
            baseline_captured_at=BASELINE,
            expected_positions=positions(),
            expected_open_orders=orders(),
            known_activities=(),
            resolved_activity_hashes=(),
            required_activity_window_start=BASELINE,
            required_activity_complete_through=BASELINE,
            intent_id=UUID("00000000-0000-0000-0000-000000000601"),
            intent_digest="b" * 64,
            attempt_ordinal=True,  # type: ignore[arg-type]
            request_hash="c" * 64,
        ),
    ],
)
def test_typed_observations_reject_runtime_type_holes(factory: object) -> None:
    with pytest.raises(ValueError):
        factory()  # type: ignore[operator]


def test_caller_cannot_construct_a_safe_verdict() -> None:
    assert "safe" not in ReconciliationExpectation.__dataclass_fields__
    assert "block_codes" not in ReconciliationExpectation.__dataclass_fields__
    assert "assignment_suspected" not in ReconciliationExpectation.__dataclass_fields__
    assert tuple(signature(WholeAccountReconciliation).parameters) == ()
    assert tuple(signature(ReconciliationExpectation).parameters) == ()

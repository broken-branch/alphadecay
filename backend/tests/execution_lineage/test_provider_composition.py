import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from alpaca.data.enums import OptionsFeed
from alpaca.data.models import OptionsSnapshot
from alpaca.trading.enums import (
    AccountStatus,
    AssetClass,
    AssetExchange,
    AssetStatus,
    ContractType,
    ExerciseStyle,
    OrderClass,
    OrderStatus,
    PositionSide,
    TimeInForce,
)
from alpaca.trading.enums import (
    PositionIntent as AlpacaPositionIntent,
)
from alpaca.trading.models import (
    OptionContract,
    OptionContractsResponse,
    Order,
    Position,
    TradeAccount,
)

from backend.app.alpaca.activities import (
    AccountActivitiesAdapter,
    ActivityReadError,
    InitialFundingContext,
    LifecycleAccountActivitiesAdapter,
)
from backend.app.alpaca.execution_evidence import (
    AlpacaExecutionReadCollector,
    AlpacaOptionContractCollector,
    AlpacaWholeAccountSweepPort,
    ExecutionEvidenceError,
    IndicativeGreekCollector,
    baseline_account_fingerprint,
)
from backend.app.contracts.v1 import AccountRole, PositionIntent
from backend.app.execution import (
    ActivityItem,
    ActivityPaginationEvidence,
    ActivityType,
    InventoryItem,
    InventoryKind,
    ReconciliationBlockCode,
    ReconciliationPurpose,
)
from backend.app.execution.models import OrderAttempt
from backend.app.execution.reconciliation import (
    ReconciliationExpectation,
    WholeAccountReconciliation,
)

NOW = datetime(2026, 8, 28, 15, 30, 20, tzinfo=UTC)
CALL = "NVDA260918C00230000"
SHORT_CALL = "NVDA260918C00240000"
ACCOUNT_FINGERPRINT = "3d53b6bfe05370b45df327123656e54260ce56e1fafd1881853cebae6ead8458"
INITIAL_FUNDING_ID = "initial-funding"
INITIAL_FUNDING_HASH = hashlib.sha256(INITIAL_FUNDING_ID.encode()).hexdigest()


class InstalledModelTradingClient:
    def __init__(self) -> None:
        self.account_value = account_model()
        self.position_values = [
            position_model(CALL, "2", PositionSide.LONG),
            position_model(SHORT_CALL, "-2", PositionSide.SHORT),
            position_model("NVDA", "100", PositionSide.LONG, AssetClass.US_EQUITY),
        ]
        self.order_values = [order_model()]
        self.linked_orders: dict[str, Order] = {}

    def get_account(self) -> TradeAccount:
        return self.account_value

    def get_all_positions(self) -> list[Position]:
        return self.position_values

    def get_orders(self, filter: object = None) -> list[Order]:
        assert filter is not None
        assert filter.status.value == "open"
        assert filter.nested is True
        assert filter.limit == 500
        return self.order_values

    def get_order_by_id(self, order_id: object, filter: object = None) -> Order:
        assert filter is None
        return self.linked_orders[str(order_id)]


def test_installed_trading_models_project_complete_execution_evidence() -> None:
    collector = AlpacaExecutionReadCollector(
        InstalledModelTradingClient(),
        account_role=AccountRole.SUBMISSION,
        expected_account_fingerprint=ACCOUNT_FINGERPRINT,
        paper=True,
        clock=lambda: NOW,
    )

    account = collector.account()
    positions = collector.positions()
    orders = collector.open_orders()

    assert account.equity == 100000
    assert account.cash == 100000
    assert account.options_trading_blocked is False
    assert account.account_fingerprint == ACCOUNT_FINGERPRINT
    assert [
        (item.kind, item.symbol, item.signed_quantity, item.multiplier) for item in positions
    ] == [
        (InventoryKind.EQUITY, "NVDA", 100, 1),
        (InventoryKind.OPTION, CALL, 2, 100),
        (InventoryKind.OPTION, SHORT_CALL, -2, 100),
    ]
    assert orders[0].client_order_id == "approved-a0"
    assert [leg.intent for leg in orders[0].legs] == [
        PositionIntent.BUY_TO_OPEN,
        PositionIntent.SELL_TO_OPEN,
    ]


def test_account_fingerprint_uses_sealed_baseline_compatible_uuid_bytes() -> None:
    assert baseline_account_fingerprint(UUID(int=1)) == ACCOUNT_FINGERPRINT


def test_account_identity_cannot_be_caller_stamped() -> None:
    with pytest.raises(ExecutionEvidenceError, match="ACCOUNT_FINGERPRINT_MISMATCH"):
        AlpacaExecutionReadCollector(
            InstalledModelTradingClient(),
            account_role=AccountRole.SUBMISSION,
            expected_account_fingerprint="a" * 64,
            paper=True,
            clock=lambda: NOW,
        ).account()


def test_all_activity_collection_preserves_date_only_baseline_funding() -> None:
    requests: list[httpx.Request] = []
    baseline_at = datetime(2026, 8, 27, 15, 0, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "id": "initial-funding",
                    "activity_type": "JNLC",
                    "date": "2026-08-27",
                    "net_amount": "100000",
                }
            ],
        )

    adapter = AccountActivitiesAdapter(
        httpx.Client(transport=httpx.MockTransport(handler)),
        base_url="https://paper-api.alpaca.markets",
        api_key="fixture-key",
        secret_key="fixture-secret",
        clock=lambda: NOW,
    )

    activities, _ = adapter.collect(
        since=baseline_at,
        until=NOW,
        provider_to_client={},
        initial_funding=InitialFundingContext(
            captured_at=baseline_at,
            equity=Decimal("100000"),
            account_fingerprint=ACCOUNT_FINGERPRINT,
            activity_id_hash=INITIAL_FUNDING_HASH,
        ),
        observed_account_fingerprint=ACCOUNT_FINGERPRINT,
    )

    assert activities[0].activity_type == ActivityType.INITIAL_FUNDING
    assert activities[0].occurred_at == datetime(2026, 8, 27, tzinfo=UTC)
    assert activities[0].time_quality == "DATE_ONLY"
    assert activities[0].signed_quantity == 100000
    assert "activity_types" not in requests[0].url.params
    assert requests[0].url.params["after"] == "2026-08-26"


def test_baseline_funding_classification_requires_matching_account_identity() -> None:
    baseline_at = NOW - timedelta(days=1)
    adapter = AccountActivitiesAdapter(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json=[
                        {
                            "id": "initial-funding",
                            "activity_type": "JNLC",
                            "date": baseline_at.date().isoformat(),
                            "net_amount": "100000",
                        }
                    ],
                )
            )
        ),
        base_url="https://paper-api.alpaca.markets",
        api_key="fixture-key",
        secret_key="fixture-secret",
        clock=lambda: NOW,
    )

    with pytest.raises(ActivityReadError, match="INITIAL_FUNDING_ACCOUNT_MISMATCH"):
        adapter.collect(
            since=baseline_at,
            until=NOW,
            provider_to_client={},
            initial_funding=InitialFundingContext(
                captured_at=baseline_at,
                equity=Decimal("100000"),
                account_fingerprint=ACCOUNT_FINGERPRINT,
                activity_id_hash=INITIAL_FUNDING_HASH,
            ),
            observed_account_fingerprint="a" * 64,
        )


def test_lifecycle_activity_source_preserves_window_pagination_and_known_history() -> None:
    requests: list[httpx.Request] = []
    activity_id = "lifecycle-option-trade"
    activity_hash = hashlib.sha256(activity_id.encode()).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "id": activity_id,
                    "activity_type": "OPTRD",
                    "date": "2026-08-28",
                    "symbol": CALL,
                    "qty": "1",
                }
            ],
        )

    source = LifecycleAccountActivitiesAdapter(
        AccountActivitiesAdapter(
            httpx.Client(transport=httpx.MockTransport(handler)),
            base_url="https://paper-api.alpaca.markets",
            api_key="fixture-key",
            secret_key="fixture-secret",
            clock=lambda: NOW,
        ),
        expected_account_fingerprint=ACCOUNT_FINGERPRINT,
    )
    since = NOW - timedelta(days=1)

    activities, pagination = source.collect_lifecycle(
        since=since,
        until=NOW,
        observed_account_fingerprint=ACCOUNT_FINGERPRINT,
        known_activity_hashes=(activity_hash,),
    )

    assert tuple(item.activity_id_hash for item in activities) == (activity_hash,)
    assert pagination.requested_start == since
    assert pagination.requested_end == NOW
    assert pagination.terminal_page_seen is True
    assert requests[0].url.params["until"] == NOW.isoformat()


def test_lifecycle_activity_source_rejects_account_or_history_substitution() -> None:
    requests = 0
    activity_id = "unexpected-option-trade"

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            json=[
                {
                    "id": activity_id,
                    "activity_type": "OPTRD",
                    "date": "2026-08-28",
                    "symbol": CALL,
                    "qty": "1",
                }
            ],
        )

    source = LifecycleAccountActivitiesAdapter(
        AccountActivitiesAdapter(
            httpx.Client(transport=httpx.MockTransport(handler)),
            base_url="https://paper-api.alpaca.markets",
            api_key="fixture-key",
            secret_key="fixture-secret",
            clock=lambda: NOW,
        ),
        expected_account_fingerprint=ACCOUNT_FINGERPRINT,
    )

    with pytest.raises(ActivityReadError, match="ACTIVITY_ACCOUNT_FINGERPRINT_MISMATCH"):
        source.collect_lifecycle(
            since=NOW - timedelta(days=1),
            until=NOW,
            observed_account_fingerprint="a" * 64,
            known_activity_hashes=(),
        )
    assert requests == 0

    with pytest.raises(ActivityReadError, match="ACTIVITY_KNOWN_HISTORY_MISMATCH"):
        source.collect_lifecycle(
            since=NOW - timedelta(days=1),
            until=NOW,
            observed_account_fingerprint=ACCOUNT_FINGERPRINT,
            known_activity_hashes=(),
        )
    assert requests == 1


def test_same_day_same_amount_journal_does_not_impersonate_sealed_funding() -> None:
    baseline_at = NOW - timedelta(days=1)
    adapter = AccountActivitiesAdapter(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json=[
                        {
                            "id": "different-journal",
                            "activity_type": "JNLC",
                            "date": baseline_at.date().isoformat(),
                            "net_amount": "100000",
                        }
                    ],
                )
            )
        ),
        base_url="https://paper-api.alpaca.markets",
        api_key="fixture-key",
        secret_key="fixture-secret",
        clock=lambda: NOW,
    )

    activities, _ = adapter.collect(
        since=baseline_at,
        until=NOW,
        provider_to_client={},
        initial_funding=InitialFundingContext(
            captured_at=baseline_at,
            equity=Decimal("100000"),
            account_fingerprint=ACCOUNT_FINGERPRINT,
            activity_id_hash=INITIAL_FUNDING_HASH,
        ),
        observed_account_fingerprint=ACCOUNT_FINGERPRINT,
    )

    assert activities[0].activity_type == ActivityType.JOURNAL


@pytest.mark.parametrize(
    ("provider_type", "expected_type"),
    [
        ("JNLC", ActivityType.JOURNAL),
        ("CSD", ActivityType.DEPOSIT),
        ("CSW", ActivityType.WITHDRAWAL),
        ("ACATS", ActivityType.TRANSFER),
        ("DIV", ActivityType.DIVIDEND),
        ("CFEE", ActivityType.FEE),
        ("INT", ActivityType.INTEREST),
        ("REORG", ActivityType.CORPORATE_ACTION),
        ("NEW_PROVIDER_CODE", ActivityType.UNKNOWN_CASH),
    ],
)
def test_nontrade_adjustments_are_not_filtered_or_silently_ignored(
    provider_type: str, expected_type: ActivityType
) -> None:
    payload = {
        "id": f"activity-{provider_type}",
        "activity_type": provider_type,
        "date": "2026-08-28",
        "net_amount": "1.25",
    }
    adapter = AccountActivitiesAdapter(
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[payload]))),
        base_url="https://paper-api.alpaca.markets",
        api_key="fixture-key",
        secret_key="fixture-secret",
        clock=lambda: NOW,
    )

    activities, _ = adapter.collect(
        since=NOW - timedelta(days=1),
        until=NOW,
        provider_to_client={},
    )

    assert activities[0].activity_type == expected_type
    assert activities[0].provider_activity_type == provider_type
    assert activities[0].time_quality == "DATE_ONLY"


def test_only_fill_trade_payload_requires_order_lineage() -> None:
    payloads = [
        {
            "id": "fill-1",
            "activity_type": "FILL",
            "transaction_time": NOW.replace(second=10).isoformat(),
            "symbol": CALL,
            "qty": "1",
            "price": "8.25",
            "side": "buy",
            "order_id": "provider-order",
        },
        {
            "id": "option-trade-1",
            "activity_type": "OPTRD",
            "date": "2026-08-28",
            "symbol": CALL,
            "qty": "1",
            "net_amount": "8.25",
        },
    ]
    adapter = AccountActivitiesAdapter(
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payloads))),
        base_url="https://paper-api.alpaca.markets",
        api_key="fixture-key",
        secret_key="fixture-secret",
        clock=lambda: NOW,
    )

    activities, _ = adapter.collect(
        since=NOW - timedelta(days=1),
        until=NOW,
        provider_to_client={"provider-order": "approved-a0"},
    )

    by_type = {item.activity_type: item for item in activities}
    assert by_type[ActivityType.FILL].client_order_id == "approved-a0"
    assert by_type[ActivityType.OPTRD].client_order_id is None


def test_nontrade_option_activity_rejects_unknown_side_sign() -> None:
    payload = {
        "id": "option-trade-1",
        "activity_type": "OPTRD",
        "date": "2026-08-28",
        "symbol": CALL,
        "qty": "1",
        "side": "unknown",
    }
    adapter = AccountActivitiesAdapter(
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[payload]))),
        base_url="https://paper-api.alpaca.markets",
        api_key="fixture-key",
        secret_key="fixture-secret",
        clock=lambda: NOW,
    )

    with pytest.raises(ActivityReadError, match="ACTIVITY_SCHEMA_INVALID"):
        adapter.collect(
            since=NOW - timedelta(days=1),
            until=NOW,
            provider_to_client={},
        )


def test_position_with_non_occ_option_symbol_fails_closed() -> None:
    client = InstalledModelTradingClient()
    client.position_values = [position_model("NOT-OCC", "1", PositionSide.LONG)]
    collector = AlpacaExecutionReadCollector(
        client,
        account_role=AccountRole.SUBMISSION,
        expected_account_fingerprint=ACCOUNT_FINGERPRINT,
        paper=True,
        clock=lambda: NOW,
    )

    with pytest.raises(ExecutionEvidenceError, match="OPTION_POSITION_METADATA_INVALID"):
        collector.positions()


def test_position_with_adjusted_occ_root_has_stable_unsupported_reason() -> None:
    client = InstalledModelTradingClient()
    client.position_values = [position_model("NVDA1260918C00230000", "1", PositionSide.LONG)]
    collector = AlpacaExecutionReadCollector(
        client,
        account_role=AccountRole.SUBMISSION,
        expected_account_fingerprint=ACCOUNT_FINGERPRINT,
        paper=True,
        clock=lambda: NOW,
    )

    with pytest.raises(ExecutionEvidenceError) as raised:
        collector.positions()

    assert raised.value.code == "NON_STANDARD_CONTRACT_UNSUPPORTED"


def test_execution_read_collector_rejects_nonpaper_composition() -> None:
    with pytest.raises(ExecutionEvidenceError, match="PAPER_TRADING_REQUIRED"):
        AlpacaExecutionReadCollector(
            InstalledModelTradingClient(),
            account_role=AccountRole.SUBMISSION,
            expected_account_fingerprint=ACCOUNT_FINGERPRINT,
            paper=False,
            clock=lambda: NOW,
        )


def test_open_order_resolves_replacement_lineage_by_provider_identity() -> None:
    client = InstalledModelTradingClient()
    predecessor = order_model().model_copy(
        update={
            "id": UUID(int=6),
            "client_order_id": "approved-a0",
            "replaced_by": UUID(int=7),
        }
    )
    current = order_model().model_copy(
        update={
            "id": UUID(int=7),
            "client_order_id": "approved-a1",
            "replaces": predecessor.id,
        }
    )
    client.order_values = [current]
    client.linked_orders = {str(predecessor.id): predecessor}
    collector = AlpacaExecutionReadCollector(
        client,
        account_role=AccountRole.SUBMISSION,
        expected_account_fingerprint=ACCOUNT_FINGERPRINT,
        paper=True,
        clock=lambda: NOW,
    )

    orders = collector.open_orders()

    assert orders[0].replaces_client_order_id == "approved-a0"


def test_open_order_collection_fails_when_maximum_sdk_window_is_saturated() -> None:
    client = InstalledModelTradingClient()
    client.order_values = [
        order_model().model_copy(
            update={"id": UUID(int=index + 100), "client_order_id": f"approved-{index}"}
        )
        for index in range(500)
    ]
    collector = AlpacaExecutionReadCollector(
        client,
        account_role=AccountRole.SUBMISSION,
        expected_account_fingerprint=ACCOUNT_FINGERPRINT,
        paper=True,
        clock=lambda: NOW,
    )

    with pytest.raises(ExecutionEvidenceError, match="OPEN_ORDER_WINDOW_SATURATED"):
        collector.open_orders()


def test_open_order_replacement_links_must_be_reciprocal() -> None:
    client = InstalledModelTradingClient()
    predecessor = order_model().model_copy(
        update={"id": UUID(int=6), "client_order_id": "approved-a0"}
    )
    current = order_model().model_copy(
        update={"id": UUID(int=7), "client_order_id": "approved-a1", "replaces": predecessor.id}
    )
    client.order_values = [current]
    client.linked_orders = {str(predecessor.id): predecessor}
    collector = AlpacaExecutionReadCollector(
        client,
        account_role=AccountRole.SUBMISSION,
        expected_account_fingerprint=ACCOUNT_FINGERPRINT,
        paper=True,
        clock=lambda: NOW,
    )

    with pytest.raises(ExecutionEvidenceError, match="ORDER_REPLACEMENT_LINEAGE_INVALID"):
        collector.open_orders()


class InstalledSnapshotClient:
    def __init__(self, snapshot: OptionsSnapshot | None = None) -> None:
        self.request = None
        self.snapshot = snapshot or snapshot_model(CALL)

    def get_option_snapshot(self, request_params: object) -> dict[str, OptionsSnapshot]:
        self.request = request_params
        return {CALL: self.snapshot}


class InstalledContractSource:
    def __init__(self, contract: OptionContract | None = None) -> None:
        self.contract = contract or contract_model(CALL)

    def contracts_for(self, symbols: tuple[str, ...]) -> dict[str, OptionContract]:
        return {CALL: self.contract}


class InstalledContractClient:
    def __init__(self) -> None:
        self.requests = []

    def get_option_contracts(self, request):
        self.requests.append(request)
        return OptionContractsResponse(
            option_contracts=[contract_model(CALL)], next_page_token=None
        )


def test_installed_option_contract_response_is_collected_by_exact_occ_root() -> None:
    client = InstalledContractClient()
    source = AlpacaOptionContractCollector(client)

    result = source.contracts_for((CALL,))

    assert result[CALL].size == "100"
    assert client.requests[0].root_symbol == "NVDA"
    assert client.requests[0].limit == 1000


def test_installed_snapshot_models_emit_indicative_hashed_greek_evidence() -> None:
    snapshots = InstalledSnapshotClient()
    collector = IndicativeGreekCollector(
        snapshots,
        InstalledContractSource(),
        clock=lambda: NOW,
    )

    result = collector.collect((InventoryItem(InventoryKind.OPTION, CALL, Decimal("2"), 100),))

    assert snapshots.request.feed == OptionsFeed.INDICATIVE
    assert snapshots.request.symbol_or_symbols == [CALL]
    assert result[0].feed == "indicative"
    assert result[0].source_timestamp == NOW.replace(second=10)
    assert len(result[0].source_hash) == 64


def test_greek_collector_rejects_contract_metadata_that_disagrees_with_occ_symbol() -> None:
    contract = contract_model(CALL).model_copy(update={"strike_price": 231.0})
    collector = IndicativeGreekCollector(
        InstalledSnapshotClient(), InstalledContractSource(contract), clock=lambda: NOW
    )

    with pytest.raises(ExecutionEvidenceError, match="OPTION_CONTRACT_METADATA_INVALID"):
        collector.collect((InventoryItem(InventoryKind.OPTION, CALL, Decimal("2"), 100),))


@pytest.mark.parametrize(
    "contract_change",
    [
        {"tradable": False},
        {"status": AssetStatus.INACTIVE},
        {"root_symbol": "OTHER"},
        {"underlying_symbol": "OTHER"},
    ],
)
def test_greek_collector_requires_active_tradable_exact_occ_contract(
    contract_change: dict[str, object],
) -> None:
    collector = IndicativeGreekCollector(
        InstalledSnapshotClient(),
        InstalledContractSource(contract_model(CALL).model_copy(update=contract_change)),
        clock=lambda: NOW,
    )

    with pytest.raises(ExecutionEvidenceError, match="OPTION_CONTRACT_METADATA_INVALID"):
        collector.collect((InventoryItem(InventoryKind.OPTION, CALL, Decimal("2"), 100),))


@pytest.mark.parametrize(
    ("quote_changes", "greek_changes"),
    [
        ({"bp": 0}, {}),
        ({"bs": 0}, {}),
        ({"bp": 8.5, "ap": 8.4}, {}),
        ({}, {"delta": 1.1}),
        ({"t": NOW - timedelta(seconds=31)}, {}),
    ],
)
def test_greek_collector_rejects_nonactionable_or_stale_evidence(
    quote_changes: dict[str, object], greek_changes: dict[str, object]
) -> None:
    collector = IndicativeGreekCollector(
        InstalledSnapshotClient(
            snapshot_model(CALL, quote_changes=quote_changes, greek_changes=greek_changes)
        ),
        InstalledContractSource(),
        clock=lambda: NOW,
    )

    with pytest.raises(ExecutionEvidenceError, match="OPTION_GREEK_EVIDENCE_INVALID"):
        collector.collect((InventoryItem(InventoryKind.OPTION, CALL, Decimal("2"), 100),))


class CapturingActivitySource:
    def __init__(self) -> None:
        self.until = None
        self.provider_to_client = None
        self.initial_funding = None
        self.observed_account_fingerprint = None

    def collect(
        self,
        *,
        since,
        until,
        provider_to_client,
        initial_funding,
        observed_account_fingerprint,
    ):
        self.until = until
        self.provider_to_client = provider_to_client
        self.initial_funding = initial_funding
        self.observed_account_fingerprint = observed_account_fingerprint
        established_at = until.replace(microsecond=500_000)
        funding = ActivityItem(
            activity_id_hash=INITIAL_FUNDING_HASH,
            activity_type=ActivityType.INITIAL_FUNDING,
            occurred_at=since,
            symbol=None,
            signed_quantity=Decimal("100000"),
        )
        pagination = ActivityPaginationEvidence(
            requested_start=since,
            requested_end=until,
            retrieved_through=until,
            established_at=established_at,
            page_count=1,
            terminal_page_seen=True,
            visibility_complete_through=since,
            visibility_horizon=timedelta(hours=24),
        )
        return (funding,), pagination


def reconciliation_expectation() -> ReconciliationExpectation:
    baseline_at = NOW - timedelta(days=2)
    funding = ActivityItem(
        activity_id_hash=INITIAL_FUNDING_HASH,
        activity_type=ActivityType.INITIAL_FUNDING,
        occurred_at=datetime.combine(baseline_at.date(), datetime.min.time(), tzinfo=UTC),
        symbol=None,
        signed_quantity=Decimal("100000"),
        time_quality="DATE_ONLY",
        provider_activity_type="JNLC",
    )
    return ReconciliationExpectation._from_repository_state(
        purpose=ReconciliationPurpose.SUBMIT,
        account_role=AccountRole.SUBMISSION,
        account_fingerprint=ACCOUNT_FINGERPRINT,
        expected_cash=Decimal("100000"),
        baseline_captured_at=baseline_at,
        expected_positions=(),
        expected_open_orders=(),
        known_activities=(funding,),
        resolved_activity_hashes=(),
        required_activity_window_start=baseline_at,
        required_activity_complete_through=baseline_at,
        intent_id=UUID(int=10),
        intent_digest="c" * 64,
        attempt_ordinal=0,
        request_hash="d" * 64,
    )


class InstalledAttemptLineage:
    def attempts_for(self, intent_id: UUID) -> tuple[OrderAttempt, ...]:
        return (
            OrderAttempt(
                intent_id=intent_id,
                ordinal=0,
                client_order_id="approved-a0",
                request_hash="d" * 64,
                state="FILLED",
                provider_order_id="filled-provider-order",
                filled_quantity=2,
                quantity=2,
            ),
        )


def test_composed_sweep_uses_first_account_boundary_and_contains_greek_retrieval() -> None:
    client = InstalledModelTradingClient()
    client.position_values = [position_model(CALL, "2", PositionSide.LONG)]
    times = iter(
        [
            NOW.replace(microsecond=0),
            NOW.replace(microsecond=100_000),
            NOW.replace(microsecond=600_000),
            NOW.replace(microsecond=700_000),
            NOW.replace(microsecond=800_000),
        ]
    )
    clock = times.__next__
    trading = AlpacaExecutionReadCollector(
        client,
        account_role=AccountRole.SUBMISSION,
        expected_account_fingerprint=ACCOUNT_FINGERPRINT,
        paper=True,
        clock=clock,
    )
    activities = CapturingActivitySource()
    greeks = IndicativeGreekCollector(
        InstalledSnapshotClient(), InstalledContractSource(), clock=clock
    )
    sweep_port = AlpacaWholeAccountSweepPort(
        trading,
        activities,
        greeks,
        InstalledAttemptLineage(),
        clock=clock,
    )
    expectation = reconciliation_expectation()

    evidence = sweep_port.collect(expectation)

    assert activities.until == evidence.sweep.first_account.observed_at
    assert activities.provider_to_client["filled-provider-order"] == "approved-a0"
    assert activities.initial_funding.activity_id_hash == INITIAL_FUNDING_HASH
    assert activities.observed_account_fingerprint == ACCOUNT_FINGERPRINT
    assert evidence.position_greeks[0].retrieved_at < evidence.sweep.retrieval_completed_at
    assert evidence.position_greeks[0].retrieved_at < evidence.sweep.final_account.observed_at


@pytest.mark.parametrize(
    ("activity_ids", "expected_safe"),
    [
        ((INITIAL_FUNDING_ID,), True),
        ((INITIAL_FUNDING_ID, "different-journal"), False),
    ],
)
def test_real_activity_adapter_composition_binds_sealed_initial_funding_identity(
    activity_ids: tuple[str, ...], expected_safe: bool
) -> None:
    expectation = reconciliation_expectation()
    activity_date = expectation.baseline_captured_at.date().isoformat()
    adapter = AccountActivitiesAdapter(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json=[
                        {
                            "id": activity_id,
                            "activity_type": "JNLC",
                            "date": activity_date,
                            "net_amount": "100000",
                        }
                        for activity_id in activity_ids
                    ],
                )
            )
        ),
        base_url="https://paper-api.alpaca.markets",
        api_key="fixture-key",
        secret_key="fixture-secret",
        clock=lambda: NOW,
    )
    client = InstalledModelTradingClient()
    client.position_values = []
    client.order_values = []
    sweep_port = AlpacaWholeAccountSweepPort(
        AlpacaExecutionReadCollector(
            client,
            account_role=AccountRole.SUBMISSION,
            expected_account_fingerprint=ACCOUNT_FINGERPRINT,
            paper=True,
            clock=lambda: NOW,
        ),
        adapter,
        IndicativeGreekCollector(
            InstalledSnapshotClient(), InstalledContractSource(), clock=lambda: NOW
        ),
        InstalledAttemptLineage(),
        clock=lambda: NOW,
    )

    evidence = sweep_port.collect(expectation)
    result = WholeAccountReconciliation.evaluate(evidence.sweep, expectation, accepted_at=NOW)
    activities_by_hash = {
        activity.activity_id_hash: activity for activity in evidence.sweep.activities
    }

    assert activities_by_hash[INITIAL_FUNDING_HASH].activity_type == ActivityType.INITIAL_FUNDING
    assert result.safe is expected_safe
    if not expected_safe:
        different_hash = hashlib.sha256(b"different-journal").hexdigest()
        assert activities_by_hash[different_hash].activity_type == ActivityType.JOURNAL
        assert ReconciliationBlockCode.ACCOUNT_ADJUSTMENT in result.block_codes


def account_model() -> TradeAccount:
    return TradeAccount(
        id=UUID(int=1),
        account_number="paper-fixture",
        status=AccountStatus.ACTIVE,
        equity="100000",
        buying_power="200000",
        cash="100000",
        account_blocked=False,
        trading_blocked=False,
        transfers_blocked=False,
        trade_suspended_by_user=False,
        options_trading_level=3,
    )


def position_model(
    symbol: str,
    quantity: str,
    side: PositionSide,
    asset_class: AssetClass = AssetClass.US_OPTION,
) -> Position:
    return Position(
        asset_id=UUID(int=2),
        symbol=symbol,
        exchange=AssetExchange.NASDAQ,
        asset_class=asset_class,
        avg_entry_price="1",
        qty=quantity,
        side=side,
        cost_basis="100",
    )


def order_model() -> Order:
    legs = [
        order_leg(CALL, AlpacaPositionIntent.BUY_TO_OPEN, 3),
        order_leg(SHORT_CALL, AlpacaPositionIntent.SELL_TO_OPEN, 4),
    ]
    return Order(
        id=UUID(int=5),
        client_order_id="approved-a0",
        created_at=NOW,
        updated_at=NOW,
        submitted_at=NOW,
        order_class=OrderClass.MLEG,
        time_in_force=TimeInForce.DAY,
        status=OrderStatus.NEW,
        extended_hours=False,
        qty="2",
        filled_qty="0",
        legs=legs,
    )


def order_leg(symbol: str, intent: AlpacaPositionIntent, identifier: int) -> Order:
    return Order(
        id=UUID(int=identifier),
        client_order_id=f"leg-{identifier}",
        created_at=NOW,
        updated_at=NOW,
        submitted_at=NOW,
        order_class=OrderClass.SIMPLE,
        time_in_force=TimeInForce.DAY,
        status=OrderStatus.NEW,
        extended_hours=False,
        symbol=symbol,
        qty="2",
        filled_qty="0",
        position_intent=intent,
        ratio_qty="1",
    )


def snapshot_model(
    symbol: str,
    *,
    quote_changes: dict[str, object] | None = None,
    greek_changes: dict[str, object] | None = None,
) -> OptionsSnapshot:
    quote = {
        "t": NOW.replace(second=10),
        "bp": 8.2,
        "ap": 8.4,
        "bs": 4,
        "as": 7,
    }
    quote.update(quote_changes or {})
    greeks = {
        "delta": 0.55,
        "gamma": 0.031,
        "rho": 0.1,
        "theta": -0.18,
        "vega": 0.42,
    }
    greeks.update(greek_changes or {})
    return OptionsSnapshot(
        symbol,
        {
            "latestQuote": quote,
            "impliedVolatility": 0.4,
            "greeks": greeks,
        },
    )


def contract_model(symbol: str) -> OptionContract:
    return OptionContract(
        id="contract-fixture",
        symbol=symbol,
        name="fixture",
        status=AssetStatus.ACTIVE,
        tradable=True,
        expiration_date=date(2026, 9, 18),
        root_symbol="NVDA",
        underlying_symbol="NVDA",
        underlying_asset_id=UUID(int=9),
        type=ContractType.CALL,
        style=ExerciseStyle.AMERICAN,
        strike_price=230.0,
        size="100",
    )

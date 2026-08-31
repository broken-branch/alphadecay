from __future__ import annotations

import asyncio
import inspect
import threading
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from importlib.metadata import version

import pytest
from alpaca.data.enums import DataFeed
from alpaca.data.live.stock import StockDataStream
from alpaca.data.models import Trade, TradingStatus
from msgpack import Timestamp

from backend.app.alpaca.opportunity_halt_stream import (
    ALPACA_TRADING_STATUS_CODEBOOK_VERSION,
    AlpacaOpportunityHaltStreamAdapter,
    OpportunityHaltStreamError,
    PinnedAlpacaStockDataStream,
    alpaca_trading_status_codebook,
)
from backend.app.alpaca.opportunity_runtime import (
    OpportunityHaltRuntimeAdapter,
    OpportunityRuntimeAdapterError,
)
from backend.app.policy.opportunity import TradingHaltState
from backend.app.services.opportunity_halt_authority import (
    HaltAuthorityConfig,
    HaltAuthorityState,
    HaltCodeMeaning,
    HaltSequenceAuthority,
    HaltStatusCodebook,
    halt_authority_config_digest,
    halt_status_codebook_digest,
)

OPEN = datetime(2026, 8, 28, 13, 30, tzinfo=UTC)
CLOSE = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)


class Clock:
    def __init__(self, value: datetime = OPEN + timedelta(minutes=30)) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class FakeStream:
    def __init__(self) -> None:
        self.supervisor = None
        self.trade_handler = None
        self.status_handler = None
        self.trade_symbols = None
        self.status_symbols = None
        self.run_calls = 0
        self.stop_calls = 0

    def bind_supervisor(self, supervisor) -> None:
        self.supervisor = supervisor

    def subscribe_trades(self, handler, *symbols: str) -> None:
        self.trade_handler = handler
        self.trade_symbols = symbols

    def subscribe_trading_statuses(self, handler, *symbols: str) -> None:
        self.status_handler = handler
        self.status_symbols = symbols

    def run(self) -> None:
        self.run_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1


class FakeThread:
    def __init__(self, *, target, name: str, daemon: bool) -> None:
        self.target = target
        self.name = name
        self.daemon = daemon
        self.started = False
        self.joined = None
        self.alive = False

    def start(self) -> None:
        self.started = True
        self.alive = True

    def join(self, timeout=None) -> None:
        self.joined = timeout
        self.alive = False

    def is_alive(self) -> bool:
        return self.alive


class SupervisorRecorder:
    def __init__(self) -> None:
        self.events = []

    def connection_starting(self) -> None:
        self.events.append(("start", None))

    def subscription_message(self, message: dict) -> None:
        self.events.append(("subscription", message))

    def stream_error(self) -> None:
        self.events.append(("error", None))

    def unexpected_message(self) -> None:
        self.events.append(("unexpected", None))

    def stream_closed(self) -> None:
        self.events.append(("closed", None))


class BlockingFakeStream(FakeStream):
    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()

    def run(self) -> None:
        self.run_calls += 1
        self.supervisor.connection_starting()
        self.supervisor.subscription_message(
            {"T": "subscription", "trades": ["NVDA"], "statuses": ["NVDA"]}
        )
        self.release.wait(2)

    def stop(self) -> None:
        super().stop()
        self.release.set()


class NoAckBlockingFakeStream(BlockingFakeStream):
    def run(self) -> None:
        self.run_calls += 1
        self.supervisor.connection_starting()
        self.release.wait(2)


class FailingThread(FakeThread):
    def start(self) -> None:
        raise RuntimeError("thread unavailable")


class StubbornThread(FakeThread):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.join_calls = 0

    def join(self, timeout=None) -> None:
        self.joined = timeout
        self.join_calls += 1
        if self.join_calls > 1:
            self.alive = False


def _codebook() -> HaltStatusCodebook:
    value = HaltStatusCodebook(
        version="verified-test-codebook",
        feed="IEX",
        sdk_version="0.44.0",
        mappings=(("H", HaltCodeMeaning.HALT), ("T", HaltCodeMeaning.RESUME)),
        source_hash="",
    )
    return replace(value, source_hash=halt_status_codebook_digest(value))


def test_official_alpaca_codebook_covers_documented_cta_and_utp_states() -> None:
    codebook = alpaca_trading_status_codebook()

    assert codebook.version == ALPACA_TRADING_STATUS_CODEBOOK_VERSION
    assert codebook.feed == "IEX"
    assert codebook.sdk_version == "0.44.0"
    assert codebook.mappings == (
        ("2", HaltCodeMeaning.HALT),
        ("3", HaltCodeMeaning.RESUME),
        ("H", HaltCodeMeaning.HALT),
        ("P", HaltCodeMeaning.HALT),
        ("Q", HaltCodeMeaning.RESUME),
        ("T", HaltCodeMeaning.RESUME),
    )
    assert codebook.source_hash == halt_status_codebook_digest(codebook)


def test_subscription_ack_accepts_trade_derived_correction_channels() -> None:
    adapter, stream, clock, _ = _adapter()

    stream.supervisor.connection_starting()
    clock.value += timedelta(milliseconds=1)
    stream.supervisor.subscription_message(
        {
            "T": "subscription",
            "trades": ["NVDA"],
            "statuses": ["NVDA"],
            "corrections": ["NVDA"],
            "cancelErrors": ["NVDA"],
        }
    )

    assert adapter.snapshot().acknowledged_at is not None


def _config(
    *, codebook_hash: str | None = None, symbol: str = "NVDA"
) -> HaltAuthorityConfig:
    value = HaltAuthorityConfig(
        symbol=symbol,
        feed="IEX",
        sdk_version="0.44.0",
        session_date=date(2026, 8, 28),
        session_open_at=OPEN,
        session_close_at=CLOSE,
        maximum_trade_age=timedelta(seconds=15),
        sequence_authority=HaltSequenceAuthority.ADAPTER_RECEIVE_ORDER_V1,
        codebook_hash=codebook_hash or "0" * 64,
        source_hash="",
    )
    return replace(value, source_hash=halt_authority_config_digest(value))


def _adapter(*, codebook: HaltStatusCodebook | None = None, epochs=("epoch-1", "epoch-2")):
    stream = FakeStream()
    clock = Clock()
    values = iter(epochs)
    threads = []

    def thread_factory(**kwargs):
        thread = FakeThread(**kwargs)
        threads.append(thread)
        return thread

    adapter = AlpacaOpportunityHaltStreamAdapter(
        stream,
        config=_config(codebook_hash=codebook.source_hash if codebook else None),
        codebook=codebook,
        clock=clock,
        epoch_factory=lambda: next(values),
        thread_factory=thread_factory,
    )
    return adapter, stream, clock, threads


def _ack(adapter, stream, clock) -> None:
    stream.supervisor.connection_starting()
    clock.value += timedelta(milliseconds=1)
    stream.supervisor.subscription_message(
        {"T": "subscription", "trades": ["NVDA"], "statuses": ["NVDA"]}
    )


def _trade(at: datetime, trade_id: int = 1) -> Trade:
    return Trade(
        "NVDA",
        {
            "S": "NVDA",
            "i": trade_id,
            "x": "V",
            "p": 100.0,
            "s": 1,
            "t": at.isoformat(),
            "c": ["@"],
            "z": "C",
        },
    )


def test_runtime_adapter_reads_the_retained_stream_without_owning_it() -> None:
    target, _, clock, _ = _adapter()

    result = OpportunityHaltRuntimeAdapter(target, symbol="NVDA").read(
        symbol="NVDA",
        trusted_at=clock.value,
    )

    assert result.symbol == "NVDA"
    assert result.observed_at == clock.value
    assert result.source_hash == target.snapshot(read_at=clock.value).source_hash


def test_runtime_adapter_rejects_symbol_substitution_and_tampering() -> None:
    target, _, clock, _ = _adapter()
    runtime = OpportunityHaltRuntimeAdapter(target, symbol="NVDA")

    with pytest.raises(
        OpportunityRuntimeAdapterError,
        match="OPPORTUNITY_HALT_BINDING_INVALID",
    ):
        runtime.read(symbol="AMD", trusted_at=clock.value)

    snapshot = target.snapshot(read_at=clock.value)

    class TamperedHaltTarget:
        def snapshot(self, *, read_at=None):
            return replace(snapshot, observed_at=read_at, source_hash="f" * 64)

    with pytest.raises(
        OpportunityRuntimeAdapterError,
        match="OPPORTUNITY_HALT_AUTHORITY_INVALID",
    ):
        OpportunityHaltRuntimeAdapter(TamperedHaltTarget(), symbol="NVDA").read(
            symbol="NVDA",
            trusted_at=clock.value,
        )


def _status(at: datetime, code: str) -> TradingStatus:
    return TradingStatus(
        "NVDA",
        {
            "S": "NVDA",
            "sc": code,
            "sm": "status",
            "rc": "T1",
            "rm": "reason",
            "t": at.isoformat(),
            "z": "C",
        },
    )


def test_binds_only_one_underlying_to_trades_and_statuses() -> None:
    adapter, stream, _, _ = _adapter()

    assert adapter.snapshot().trading_halt_state is TradingHaltState.UNKNOWN
    assert stream.trade_symbols == ("NVDA",)
    assert stream.status_symbols == ("NVDA",)
    assert stream.trade_handler is not None
    assert stream.status_handler is not None


def test_exact_ack_is_sequence_one_and_trade_confirms_open() -> None:
    adapter, stream, clock, _ = _adapter()
    _ack(adapter, stream, clock)
    clock.value += timedelta(seconds=1)

    asyncio.run(stream.trade_handler(_trade(clock.value)))
    snapshot = adapter.snapshot(read_at=clock.value)

    assert snapshot.epoch == "epoch-1"
    assert snapshot.last_sequence == 2
    assert snapshot.last_trade_id == "1"
    assert snapshot.state is HaltAuthorityState.OPEN_CONFIRMED


@pytest.mark.parametrize(
    "message",
    [
        {"T": "subscription", "trades": ["NVDA"]},
        {"T": "subscription", "trades": ["NVDA"], "statuses": ["*"]},
        {"T": "subscription", "trades": ["NVDA"], "statuses": ["NVDA"], "bars": ["NVDA"]},
        {"T": "subscription", "trades": ["NVDA"], "statuses": ["NVDA"], "extra": []},
    ],
)
def test_nonexact_subscription_ack_never_establishes_continuity(message: dict) -> None:
    adapter, stream, clock, _ = _adapter()
    stream.supervisor.connection_starting()
    clock.value += timedelta(milliseconds=1)

    stream.supervisor.subscription_message(message)

    snapshot = adapter.snapshot(read_at=clock.value)
    assert snapshot.acknowledged_at is None
    assert snapshot.trading_halt_state is TradingHaltState.UNKNOWN


def test_reconnect_rotates_epoch_and_invalidates_open_before_new_ack() -> None:
    adapter, stream, clock, _ = _adapter()
    _ack(adapter, stream, clock)
    clock.value += timedelta(seconds=1)
    asyncio.run(stream.trade_handler(_trade(clock.value)))

    clock.value += timedelta(seconds=1)
    stream.supervisor.connection_starting()
    snapshot = adapter.snapshot(read_at=clock.value)

    assert snapshot.trading_halt_state is TradingHaltState.UNKNOWN
    assert snapshot.acknowledged_at is None
    clock.value += timedelta(milliseconds=1)
    stream.supervisor.subscription_message(
        {"T": "subscription", "trades": ["NVDA"], "statuses": ["NVDA"]}
    )
    assert adapter.snapshot(read_at=clock.value).epoch == "epoch-2"


@pytest.mark.parametrize("failure", ["stream_error", "unexpected_message", "stream_closed"])
def test_stream_failure_boundaries_invalidate_open(failure: str) -> None:
    adapter, stream, clock, _ = _adapter()
    _ack(adapter, stream, clock)
    clock.value += timedelta(seconds=1)
    asyncio.run(stream.trade_handler(_trade(clock.value)))
    clock.value += timedelta(seconds=1)

    getattr(stream.supervisor, failure)()

    assert adapter.snapshot(read_at=clock.value).trading_halt_state is TradingHaltState.UNKNOWN


def test_default_missing_codebook_never_interprets_status_code() -> None:
    adapter, stream, clock, _ = _adapter()
    _ack(adapter, stream, clock)
    clock.value += timedelta(seconds=1)

    asyncio.run(stream.status_handler(_status(clock.value, "H")))

    assert adapter.snapshot(read_at=clock.value).trading_halt_state is TradingHaltState.UNKNOWN


def test_explicit_hash_bound_codebook_can_latch_halt() -> None:
    codebook = _codebook()
    adapter, stream, clock, _ = _adapter(codebook=codebook)
    _ack(adapter, stream, clock)
    clock.value += timedelta(seconds=1)

    asyncio.run(stream.status_handler(_status(clock.value, "H")))

    assert adapter.snapshot(read_at=clock.value).trading_halt_state is TradingHaltState.HALTED


def test_malformed_or_duplicate_trade_identity_fails_closed() -> None:
    adapter, stream, clock, _ = _adapter()
    _ack(adapter, stream, clock)
    clock.value += timedelta(seconds=1)
    asyncio.run(stream.trade_handler(_trade(clock.value)))
    clock.value += timedelta(seconds=1)

    asyncio.run(stream.trade_handler(_trade(clock.value)))

    assert adapter.snapshot(read_at=clock.value).trading_halt_state is TradingHaltState.UNKNOWN


def test_nonconsecutive_duplicate_trade_identity_fails_closed() -> None:
    adapter, stream, clock, _ = _adapter()
    _ack(adapter, stream, clock)
    for trade_id in (1, 2, 1):
        clock.value += timedelta(seconds=1)
        asyncio.run(stream.trade_handler(_trade(clock.value, trade_id)))

    assert adapter.snapshot(read_at=clock.value).trading_halt_state is TradingHaltState.UNKNOWN


def test_replayed_trade_identity_after_reconnect_cannot_confirm_open() -> None:
    adapter, stream, clock, _ = _adapter()
    _ack(adapter, stream, clock)
    clock.value += timedelta(seconds=1)
    asyncio.run(stream.trade_handler(_trade(clock.value, 7)))
    clock.value += timedelta(seconds=1)
    stream.supervisor.connection_starting()
    clock.value += timedelta(milliseconds=1)
    stream.supervisor.subscription_message(
        {"T": "subscription", "trades": ["NVDA"], "statuses": ["NVDA"]}
    )
    clock.value += timedelta(seconds=1)

    asyncio.run(stream.trade_handler(_trade(clock.value, 7)))

    assert adapter.snapshot(read_at=clock.value).trading_halt_state is TradingHaltState.UNKNOWN


def test_invalid_typed_timestamp_is_contained_and_fails_closed() -> None:
    adapter, stream, clock, _ = _adapter()
    _ack(adapter, stream, clock)
    clock.value += timedelta(seconds=1)
    asyncio.run(stream.trade_handler(_trade(clock.value)))
    invalid = _trade(clock.value + timedelta(seconds=1), 2)
    invalid.timestamp = invalid.timestamp.replace(tzinfo=None)
    clock.value += timedelta(seconds=1)

    asyncio.run(stream.trade_handler(invalid))

    assert adapter.snapshot(read_at=clock.value).trading_halt_state is TradingHaltState.UNKNOWN


def test_adapter_owns_one_joined_thread_and_stop_is_idempotent() -> None:
    adapter, stream, clock, threads = _adapter()
    _ack(adapter, stream, clock)

    adapter.start()
    adapter.start()
    adapter.stop()
    adapter.stop()

    assert len(threads) == 1
    assert threads[0].name == "alphadecay-halt-NVDA"
    assert threads[0].daemon is True
    assert threads[0].joined == 5.0
    assert stream.stop_calls == 1


def test_adapter_stop_itself_invalidates_continuity() -> None:
    adapter, stream, clock, _ = _adapter()
    _ack(adapter, stream, clock)
    clock.value += timedelta(seconds=1)
    asyncio.run(stream.trade_handler(_trade(clock.value)))
    adapter.start()
    clock.value += timedelta(seconds=1)

    adapter.stop()

    assert adapter.snapshot(read_at=clock.value).trading_halt_state is TradingHaltState.UNKNOWN


def test_start_requires_exact_subscription_ack_and_cleans_up_timeout() -> None:
    stream = NoAckBlockingFakeStream()
    clock = Clock()
    adapter = AlpacaOpportunityHaltStreamAdapter(
        stream,
        config=_config(),
        codebook=None,
        clock=clock,
        epoch_factory=lambda: "epoch-no-ack",
        join_timeout=0.05,
    )

    with pytest.raises(OpportunityHaltStreamError, match="HALT_STREAM_START_TIMEOUT"):
        adapter.start()

    assert stream.stop_calls == 1
    with pytest.raises(OpportunityHaltStreamError, match="HALT_STREAM_CLOSED"):
        adapter.start()


def test_thread_start_failure_is_terminal_and_does_not_call_stream() -> None:
    stream = FakeStream()
    adapter = AlpacaOpportunityHaltStreamAdapter(
        stream,
        config=_config(),
        codebook=None,
        clock=Clock(),
        epoch_factory=lambda: "epoch-thread-failure",
        thread_factory=lambda **kwargs: FailingThread(**kwargs),
    )

    with pytest.raises(OpportunityHaltStreamError, match="HALT_STREAM_START_FAILED"):
        adapter.start()

    assert stream.run_calls == 0
    assert stream.stop_calls == 0


def test_join_timeout_stays_stopping_until_a_later_join_succeeds() -> None:
    stream = FakeStream()
    threads = []

    def thread_factory(**kwargs):
        thread = StubbornThread(**kwargs)
        threads.append(thread)
        return thread

    adapter = AlpacaOpportunityHaltStreamAdapter(
        stream,
        config=_config(),
        codebook=None,
        clock=Clock(),
        epoch_factory=lambda: "epoch-stubborn-thread",
        thread_factory=thread_factory,
        join_timeout=0.05,
    )
    stream.supervisor.connection_starting()
    stream.supervisor.subscription_message(
        {"T": "subscription", "trades": ["NVDA"], "statuses": ["NVDA"]}
    )
    adapter.start()

    with pytest.raises(OpportunityHaltStreamError, match="HALT_STREAM_JOIN_TIMEOUT"):
        adapter.stop()
    with pytest.raises(OpportunityHaltStreamError, match="HALT_STREAM_STOPPING"):
        adapter.start()
    adapter.stop()

    assert threads[0].join_calls == 2
    assert stream.stop_calls == 2


def test_concurrent_stop_joins_owned_thread_once() -> None:
    stream = BlockingFakeStream()
    adapter = AlpacaOpportunityHaltStreamAdapter(
        stream,
        config=_config(),
        codebook=None,
        clock=Clock(),
        epoch_factory=lambda: "epoch-concurrent-stop",
        join_timeout=1,
    )
    adapter.start()
    failures = []

    def stop_adapter() -> None:
        try:
            adapter.stop()
        except Exception as exc:  # pragma: no cover - asserted empty below
            failures.append(exc)

    callers = [threading.Thread(target=stop_adapter) for _ in range(2)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(2)

    assert failures == []
    assert stream.stop_calls == 1


def test_real_owned_thread_runs_public_stream_and_joins_without_network() -> None:
    stream = BlockingFakeStream()
    clock = Clock()
    adapter = AlpacaOpportunityHaltStreamAdapter(
        stream,
        config=_config(),
        codebook=None,
        clock=clock,
        epoch_factory=lambda: "epoch-real-thread",
        join_timeout=1,
    )

    adapter.start()
    adapter.stop()

    assert stream.run_calls == 1
    assert stream.stop_calls == 1


def test_duplicate_epoch_is_rejected() -> None:
    adapter, stream, clock, _ = _adapter(epochs=("same", "same"))
    _ack(adapter, stream, clock)
    clock.value += timedelta(seconds=1)

    with pytest.raises(OpportunityHaltStreamError, match="HALT_STREAM_EPOCH_INVALID"):
        stream.supervisor.connection_starting()

    assert adapter.snapshot(read_at=clock.value).trading_halt_state is TradingHaltState.UNKNOWN


def test_sdk_boundary_pins_methods_and_typed_model_fields() -> None:
    assert version("alpaca-py") == "0.44.0"
    assert str(inspect.signature(StockDataStream._start_ws)) == "(self) -> None"
    assert str(inspect.signature(StockDataStream._dispatch)) == "(self, msg: Dict) -> None"
    assert str(inspect.signature(StockDataStream.close)) == "(self) -> None"
    assert tuple(Trade.model_fields) == (
        "symbol",
        "timestamp",
        "exchange",
        "price",
        "size",
        "id",
        "conditions",
        "tape",
    )
    assert tuple(TradingStatus.model_fields) == (
        "symbol",
        "timestamp",
        "status_code",
        "status_message",
        "reason_code",
        "reason_message",
        "tape",
    )


def test_pinned_private_hooks_supervise_start_dispatch_and_close(monkeypatch) -> None:
    recorder = SupervisorRecorder()
    stream = object.__new__(PinnedAlpacaStockDataStream)
    stream._halt_supervisor = recorder
    parent_calls = []

    async def start(_self) -> None:
        parent_calls.append("start")

    async def dispatch(_self, message: dict) -> None:
        parent_calls.append(("dispatch", message))

    async def close(_self) -> None:
        parent_calls.append("close")

    monkeypatch.setattr(StockDataStream, "_start_ws", start)
    monkeypatch.setattr(StockDataStream, "_dispatch", dispatch)
    monkeypatch.setattr(StockDataStream, "close", close)
    subscription = {"T": "subscription", "trades": ["NVDA"], "statuses": ["NVDA"]}

    asyncio.run(stream._start_ws())
    asyncio.run(stream._dispatch(subscription))
    asyncio.run(stream._dispatch({"T": "error", "code": 500, "msg": "failed"}))
    asyncio.run(stream._dispatch({"T": "q", "S": "NVDA"}))
    asyncio.run(stream.close())

    assert recorder.events == [
        ("start", None),
        ("subscription", subscription),
        ("error", None),
        ("unexpected", None),
        ("closed", None),
    ]
    assert parent_calls == [
        "start",
        ("dispatch", subscription),
        ("dispatch", {"T": "error", "code": 500, "msg": "failed"}),
        ("dispatch", {"T": "q", "S": "NVDA"}),
        "close",
    ]


def test_pinned_sdk_dispatch_normalizes_exact_trade_and_status_types() -> None:
    stream = PinnedAlpacaStockDataStream("key", "secret")
    recorder = SupervisorRecorder()
    stream.bind_supervisor(recorder)
    received = []

    async def capture(value) -> None:
        received.append(value)

    stream.subscribe_trades(capture, "NVDA")
    stream.subscribe_trading_statuses(capture, "NVDA")
    asyncio.run(
        stream._dispatch(
            {
                "T": "t",
                "S": "NVDA",
                "i": 11,
                "x": "V",
                "p": 100.0,
                "s": 1,
                "t": Timestamp(1_787_922_000, 0),
                "c": ["@"],
                "z": "C",
            }
        )
    )
    asyncio.run(
        stream._dispatch(
            {
                "T": "s",
                "S": "NVDA",
                "sc": "H",
                "sm": "halted",
                "rc": "T1",
                "rm": "reason",
                "t": Timestamp(1_787_922_001, 0),
                "z": "C",
            }
        )
    )

    assert [type(value) for value in received] == [Trade, TradingStatus]
    assert received[0].id == 11
    assert received[0].timestamp.tzinfo is UTC
    assert received[1].status_code == "H"
    assert received[1].timestamp.tzinfo is UTC
    assert recorder.events == []


def test_pinned_start_failure_marks_error(monkeypatch) -> None:
    recorder = SupervisorRecorder()
    stream = object.__new__(PinnedAlpacaStockDataStream)
    stream._halt_supervisor = recorder

    async def fail(_self) -> None:
        raise RuntimeError("failed")

    monkeypatch.setattr(StockDataStream, "_start_ws", fail)

    with pytest.raises(RuntimeError, match="failed"):
        asyncio.run(stream._start_ws())
    assert recorder.events == [("start", None), ("error", None)]


def test_pinned_stream_rejects_raw_or_non_iex_before_connection() -> None:
    with pytest.raises(OpportunityHaltStreamError, match="HALT_STREAM_SDK_CONFIG_INVALID"):
        PinnedAlpacaStockDataStream("key", "secret", raw_data=True)
    with pytest.raises(OpportunityHaltStreamError, match="HALT_STREAM_SDK_CONFIG_INVALID"):
        PinnedAlpacaStockDataStream("key", "secret", raw_data=0)
    with pytest.raises(OpportunityHaltStreamError, match="HALT_STREAM_SDK_CONFIG_INVALID"):
        PinnedAlpacaStockDataStream("key", "secret", feed=DataFeed.SIP)


def test_pinned_stream_rejects_an_unexpected_installed_sdk(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.app.alpaca.opportunity_halt_stream.package_version",
        lambda _: "0.45.0",
    )

    with pytest.raises(OpportunityHaltStreamError, match="HALT_STREAM_SDK_CONFIG_INVALID"):
        PinnedAlpacaStockDataStream("key", "secret")


@pytest.mark.parametrize("symbol", ["*", "NVDA,QQQ", "NV DA"])
def test_adapter_rejects_non_single_symbol_scope(symbol: str) -> None:
    with pytest.raises(OpportunityHaltStreamError, match="HALT_STREAM_CONFIG_INVALID"):
        AlpacaOpportunityHaltStreamAdapter(
            FakeStream(),
            config=_config(symbol=symbol),
            codebook=None,
            clock=Clock(),
        )

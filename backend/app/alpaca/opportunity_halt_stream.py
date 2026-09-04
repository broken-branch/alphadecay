from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from typing import Protocol

from alpaca.data.enums import DataFeed
from alpaca.data.live.stock import StockDataStream
from alpaca.data.models import Trade, TradingStatus

from backend.app.services.opportunity_halt_authority import (
    HaltAuthorityConfig,
    HaltAuthorityEvent,
    HaltAuthorityEventKind,
    HaltAuthoritySnapshot,
    HaltAuthorityState,
    HaltCodeMeaning,
    HaltStatusCodebook,
    halt_authority_event_digest,
    halt_status_codebook_digest,
    initial_halt_authority,
    read_halt_authority,
    reduce_halt_authority,
)

_LOGGER = logging.getLogger(__name__)

PINNED_ALPACA_PY_VERSION = "0.44.0"
PINNED_STOCK_FEED = "IEX"
ALPACA_TRADING_STATUS_CODEBOOK_VERSION = (
    "https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data@2026-08-30"
)
_MARKET_TYPES = {"t", "s"}
_SUBSCRIPTION_CHANNELS = {
    "trades",
    "quotes",
    "orderbooks",
    "bars",
    "updatedBars",
    "dailyBars",
    "statuses",
    "lulds",
    "news",
    "corrections",
    "cancelErrors",
}


class OpportunityHaltStreamError(RuntimeError):
    pass


def alpaca_trading_status_codebook() -> HaltStatusCodebook:
    value = HaltStatusCodebook(
        version=ALPACA_TRADING_STATUS_CODEBOOK_VERSION,
        feed=PINNED_STOCK_FEED,
        sdk_version=PINNED_ALPACA_PY_VERSION,
        mappings=(
            ("2", HaltCodeMeaning.HALT),
            ("3", HaltCodeMeaning.RESUME),
            ("H", HaltCodeMeaning.HALT),
            ("P", HaltCodeMeaning.HALT),
            ("Q", HaltCodeMeaning.RESUME),
            ("T", HaltCodeMeaning.RESUME),
        ),
        source_hash="",
    )
    return replace(value, source_hash=halt_status_codebook_digest(value))


class HaltStreamSupervisor(Protocol):
    def connection_starting(self) -> None: ...

    def subscription_message(self, message: dict) -> None: ...

    def stream_error(self) -> None: ...

    def unexpected_message(self) -> None: ...

    def stream_closed(self) -> None: ...


class HaltStockDataStream(Protocol):
    def bind_supervisor(self, supervisor: HaltStreamSupervisor) -> None: ...

    def subscribe_trades(self, handler: Callable[..., object], *symbols: str) -> None: ...

    def subscribe_trading_statuses(self, handler: Callable[..., object], *symbols: str) -> None: ...

    def run(self) -> None: ...

    def stop(self) -> None: ...


class PinnedAlpacaStockDataStream(StockDataStream):
    """alpaca-py 0.44.0 stream with explicit lifecycle supervision.

    The SDK exposes no public connect, reconnect, control-frame, or close callbacks.
    These three overrides are therefore a deliberately pinned compatibility boundary.
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        raw_data: bool = False,
        feed: DataFeed = DataFeed.IEX,
        websocket_params: dict | None = None,
        url_override: str | None = None,
        data_timeout: float | None = None,
    ) -> None:
        try:
            installed_version = package_version("alpaca-py")
        except PackageNotFoundError:
            installed_version = None
        if (
            installed_version != PINNED_ALPACA_PY_VERSION
            or raw_data is not False
            or feed is not DataFeed.IEX
        ):
            raise OpportunityHaltStreamError("HALT_STREAM_SDK_CONFIG_INVALID")
        super().__init__(
            api_key=api_key,
            secret_key=secret_key,
            raw_data=raw_data,
            feed=feed,
            websocket_params=websocket_params,
            url_override=url_override,
            data_timeout=data_timeout,
        )
        self._halt_supervisor: HaltStreamSupervisor | None = None

    def bind_supervisor(self, supervisor: HaltStreamSupervisor) -> None:
        if self._halt_supervisor is not None or supervisor is None:
            raise OpportunityHaltStreamError("HALT_STREAM_SUPERVISOR_INVALID")
        self._halt_supervisor = supervisor

    async def _start_ws(self) -> None:
        supervisor = self._required_supervisor()
        supervisor.connection_starting()
        try:
            await super()._start_ws()
        except Exception:
            supervisor.stream_error()
            raise

    async def _dispatch(self, msg: dict) -> None:
        supervisor = self._required_supervisor()
        message_type = msg.get("T") if type(msg) is dict else None
        if message_type == "subscription":
            supervisor.subscription_message(msg)
        elif message_type == "error":
            supervisor.stream_error()
        elif message_type not in _MARKET_TYPES:
            supervisor.unexpected_message()
        await super()._dispatch(msg)

    async def close(self) -> None:
        supervisor = self._required_supervisor()
        try:
            await super().close()
        finally:
            supervisor.stream_closed()

    def _required_supervisor(self) -> HaltStreamSupervisor:
        if self._halt_supervisor is None:
            raise OpportunityHaltStreamError("HALT_STREAM_SUPERVISOR_MISSING")
        return self._halt_supervisor


class _OwnedThread(Protocol):
    def start(self) -> None: ...

    def join(self, timeout: float | None = None) -> None: ...

    def is_alive(self) -> bool: ...


ThreadFactory = Callable[..., _OwnedThread]


class _RecoveryTimer(Protocol):
    daemon: bool

    def start(self) -> None: ...

    def cancel(self) -> None: ...


RecoveryTimerFactory = Callable[[float, Callable[[], None]], _RecoveryTimer]


class AlpacaOpportunityHaltStreamAdapter:
    """Reduce one supervised IEX trade/status subscription into halt authority."""

    def __init__(
        self,
        stream: HaltStockDataStream,
        *,
        config: HaltAuthorityConfig,
        codebook: HaltStatusCodebook | None,
        clock: Callable[[], datetime],
        epoch_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        thread_factory: ThreadFactory = threading.Thread,
        join_timeout: float = 5.0,
        resubscribe_backoff: float = 1.0,
        recovery_timer_factory: RecoveryTimerFactory = threading.Timer,
    ) -> None:
        if (
            config.feed != PINNED_STOCK_FEED
            or config.sdk_version != PINNED_ALPACA_PY_VERSION
            or not _is_single_symbol(config.symbol)
            or not callable(clock)
            or not callable(epoch_factory)
            or not callable(thread_factory)
            or type(join_timeout) not in {int, float}
            or not 0 < float(join_timeout) <= 30
            or type(resubscribe_backoff) not in {int, float}
            or not 0 < float(resubscribe_backoff) <= 30
            or not callable(recovery_timer_factory)
        ):
            raise OpportunityHaltStreamError("HALT_STREAM_CONFIG_INVALID")
        self._stream = stream
        self._config = config
        self._codebook = codebook
        self._clock = clock
        self._epoch_factory = epoch_factory
        self._thread_factory = thread_factory
        self._join_timeout = float(join_timeout)
        self._resubscribe_backoff = timedelta(seconds=float(resubscribe_backoff))
        self._recovery_timer_factory = recovery_timer_factory
        self._lock = threading.RLock()
        self._snapshot = initial_halt_authority(config=config, read_at=self._now())
        self._epoch: str | None = None
        self._sequence = 0
        self._seen_epochs: set[str] = set()
        self._seen_trade_ids: set[str] = set()
        self._thread: _OwnedThread | None = None
        self._thread_started = False
        self._stream_ready = threading.Event()
        self._startup_resolved = threading.Event()
        self._startup_error: str | None = None
        self._stop_lock = threading.Lock()
        self._stopping = False
        self._closed = False
        self._recovery_due_at: datetime | None = None
        self._recovery_timer: _RecoveryTimer | None = None

        try:
            stream.bind_supervisor(self)
            stream.subscribe_trades(self._on_trade, config.symbol)
            stream.subscribe_trading_statuses(self._on_status, config.symbol)
        except Exception as exc:
            raise OpportunityHaltStreamError("HALT_STREAM_BIND_FAILED") from exc

    def start(self) -> None:
        with self._stop_lock:
            with self._lock:
                if self._closed:
                    raise OpportunityHaltStreamError("HALT_STREAM_CLOSED")
                if self._stopping:
                    raise OpportunityHaltStreamError("HALT_STREAM_STOPPING")
                should_start = self._thread is None
                if should_start:
                    self._thread = self._thread_factory(
                        target=self._run_stream,
                        name=f"alphadecay-halt-{self._config.symbol}",
                        daemon=True,
                    )
                thread = self._thread
            if thread is None:
                raise OpportunityHaltStreamError("HALT_STREAM_THREAD_DEAD")
            if should_start:
                try:
                    thread.start()
                except Exception as exc:
                    with self._lock:
                        self._startup_error = "HALT_STREAM_START_FAILED"
                        self._startup_resolved.set()
                        self._closed = True
                    raise OpportunityHaltStreamError("HALT_STREAM_START_FAILED") from exc
                with self._lock:
                    self._thread_started = True
        if not self._startup_resolved.wait(self._join_timeout):
            self._abort_startup()
            raise OpportunityHaltStreamError("HALT_STREAM_START_TIMEOUT")
        with self._lock:
            startup_error = self._startup_error
            ready = self._stream_ready.is_set()
            alive = thread.is_alive()
        if startup_error is not None or not ready or not alive:
            self._abort_startup()
            raise OpportunityHaltStreamError(startup_error or "HALT_STREAM_THREAD_DEAD")

    def stop(self) -> None:
        with self._stop_lock:
            with self._lock:
                if self._closed:
                    return
                self._stopping = True
                self._cancel_recovery()
                thread = self._thread
            try:
                if thread is not None and self._thread_started:
                    stop_error: Exception | None = None
                    try:
                        self._stream.stop()
                    except Exception as exc:
                        stop_error = exc
                    thread.join(self._join_timeout)
                    if thread.is_alive():
                        with self._lock:
                            self._safe_failure(HaltAuthorityEventKind.THREAD_DEAD)
                        raise OpportunityHaltStreamError("HALT_STREAM_JOIN_TIMEOUT")
                    with self._lock:
                        self._safe_failure(HaltAuthorityEventKind.THREAD_DEAD)
                    if stop_error is not None:
                        raise OpportunityHaltStreamError("HALT_STREAM_STOP_FAILED") from stop_error
            finally:
                with self._lock:
                    still_alive = bool(
                        thread is not None and self._thread_started and thread.is_alive()
                    )
                    self._closed = not still_alive
                    self._startup_resolved.set()

    close = stop

    def snapshot(self, *, read_at: datetime | None = None) -> HaltAuthoritySnapshot:
        request_resubscribe = False
        with self._lock:
            now = self._now()
            requested_at = now if read_at is None else _strict_utc(read_at)
            updated = read_halt_authority(
                previous=self._snapshot,
                config=self._config,
                read_at=max(requested_at, now),
            )
            self._set_snapshot(updated, trigger="READ")
            thread_alive = bool(
                self._thread is not None and self._thread_started and self._thread.is_alive()
            )
            if (
                self._recovery_due_at is not None
                and now >= self._recovery_due_at
                and thread_alive
                and not self._closed
                and not self._stopping
            ):
                self._begin_resubscribe()
                request_resubscribe = True
        if request_resubscribe:
            try:
                self._stream.subscribe_trades(self._on_trade, self._config.symbol)
            except Exception:
                with self._lock:
                    _LOGGER.exception("halt authority resubscribe request failed")
                    self._arm_recovery(self._now())
        with self._lock:
            return self._snapshot

    def connection_starting(self) -> None:
        with self._lock:
            if self._closed or self._stopping:
                return
            self._cancel_recovery()
            if self._has_continuity():
                self._emit(HaltAuthorityEventKind.RECONNECT, event_at=self._now())
            try:
                epoch = self._epoch_factory()
            except Exception as exc:
                self._resolve_startup_failure("HALT_STREAM_EPOCH_INVALID")
                raise OpportunityHaltStreamError("HALT_STREAM_EPOCH_INVALID") from exc
            if (
                type(epoch) is not str
                or not epoch
                or epoch.strip() != epoch
                or epoch in self._seen_epochs
            ):
                self._failure(HaltAuthorityEventKind.GAP)
                raise OpportunityHaltStreamError("HALT_STREAM_EPOCH_INVALID")
            self._seen_epochs.add(epoch)
            self._epoch = epoch
            self._sequence = 0
            self._arm_recovery(self._now())

    def subscription_message(self, message: dict) -> None:
        with self._lock:
            if self._closed or self._stopping:
                return
            if not self._exact_subscription_ack(message):
                self._failure(HaltAuthorityEventKind.MALFORMED)
                self._arm_recovery(self._now())
                self._resolve_startup_failure("HALT_STREAM_SUBSCRIPTION_INVALID")
                return
            if self._epoch is None or self._sequence != 0:
                self._failure(HaltAuthorityEventKind.GAP)
                self._arm_recovery(self._now())
                self._resolve_startup_failure("HALT_STREAM_SUBSCRIPTION_INVALID")
                return
            self._emit(HaltAuthorityEventKind.EPOCH_ACK, event_at=self._now())
            self._stream_ready.set()
            self._startup_resolved.set()

    def stream_error(self) -> None:
        with self._lock:
            self._safe_failure(HaltAuthorityEventKind.ERROR)
            self._resolve_startup_failure("HALT_STREAM_START_FAILED")

    def unexpected_message(self) -> None:
        with self._lock:
            self._safe_failure(HaltAuthorityEventKind.GAP)
            self._resolve_startup_failure("HALT_STREAM_START_FAILED")

    def stream_closed(self) -> None:
        with self._lock:
            self._safe_failure(HaltAuthorityEventKind.THREAD_DEAD)
            self._resolve_startup_failure("HALT_STREAM_THREAD_DEAD")

    async def _on_trade(self, value: Trade | dict) -> None:
        with self._lock:
            if self._closed or self._stopping:
                return
            received_at: datetime | None = None
            try:
                received_at = self._now()
                if (
                    type(value) is not Trade
                    or value.symbol != self._config.symbol
                    or type(value.id) is not int
                    or value.id < 0
                    or type(value.timestamp) is not datetime
                ):
                    self._failure(HaltAuthorityEventKind.MALFORMED, at=received_at)
                    return
                trade_id = str(value.id)
                if trade_id in self._seen_trade_ids:
                    self._failure(HaltAuthorityEventKind.MALFORMED, at=received_at)
                    return
                acknowledged_at = self._snapshot.acknowledged_at
                if (
                    acknowledged_at is not None
                    and self._snapshot.last_trade_at is None
                    and _strict_utc(value.timestamp) <= acknowledged_at
                ):
                    # A trade that printed before this epoch was acknowledged can arrive
                    # after the acknowledgment. It is not evidence for the epoch, and
                    # feeding it to the reducer would fail the authority closed for the
                    # rest of the process, so it is skipped rather than reduced.
                    self._seen_trade_ids.add(trade_id)
                    return
                self._emit(
                    HaltAuthorityEventKind.TRADE,
                    event_at=_strict_utc(value.timestamp),
                    received_at=received_at,
                    trade_id=trade_id,
                )
                self._seen_trade_ids.add(trade_id)
            except Exception:
                self._contain_callback_failure(received_at)

    async def _on_status(self, value: TradingStatus | dict) -> None:
        with self._lock:
            if self._closed or self._stopping:
                return
            received_at: datetime | None = None
            try:
                received_at = self._now()
                if (
                    type(value) is not TradingStatus
                    or value.symbol != self._config.symbol
                    or type(value.status_code) is not str
                    or not value.status_code
                    or value.status_code.strip() != value.status_code
                    or type(value.timestamp) is not datetime
                ):
                    self._failure(HaltAuthorityEventKind.MALFORMED, at=received_at)
                    return
                self._emit(
                    HaltAuthorityEventKind.TRADING_STATUS,
                    event_at=_strict_utc(value.timestamp),
                    received_at=received_at,
                    status_code=value.status_code,
                )
            except Exception:
                self._contain_callback_failure(received_at)

    def _run_stream(self) -> None:
        try:
            self._stream.run()
        except Exception:
            with self._lock:
                self._safe_failure(HaltAuthorityEventKind.ERROR)
        finally:
            with self._lock:
                self._safe_failure(HaltAuthorityEventKind.THREAD_DEAD)
                self._resolve_startup_failure("HALT_STREAM_THREAD_DEAD")
                self._startup_resolved.set()

    def _abort_startup(self) -> None:
        with suppress(OpportunityHaltStreamError):
            self.stop()

    def _resolve_startup_failure(self, code: str) -> None:
        if not self._stream_ready.is_set() and self._startup_error is None:
            self._startup_error = code
            self._startup_resolved.set()

    def _contain_callback_failure(self, received_at: object) -> None:
        at = received_at if type(received_at) is datetime else None
        self._safe_failure(HaltAuthorityEventKind.MALFORMED, at=at)

    def _safe_failure(self, kind: HaltAuthorityEventKind, *, at: datetime | None = None) -> None:
        with suppress(Exception):
            self._failure(kind, at=at)

    def _failure(self, kind: HaltAuthorityEventKind, *, at: datetime | None = None) -> None:
        if self._has_continuity():
            self._emit(kind, event_at=at or self._now())

    def _has_continuity(self) -> bool:
        return (
            self._epoch is not None
            and self._sequence > 0
            and self._snapshot.acknowledged_at is not None
            and self._snapshot.epoch == self._epoch
        )

    def _emit(
        self,
        kind: HaltAuthorityEventKind,
        *,
        event_at: datetime,
        received_at: datetime | None = None,
        status_code: str | None = None,
        trade_id: str | None = None,
    ) -> None:
        if self._epoch is None:
            return
        received = received_at or self._now()
        self._sequence += 1
        event = HaltAuthorityEvent(
            kind=kind,
            symbol=self._config.symbol,
            epoch=self._epoch,
            sequence=self._sequence,
            event_at=event_at,
            received_at=received,
            status_code=status_code,
            trade_id=trade_id,
            source_hash="",
        )
        event = replace(event, source_hash=halt_authority_event_digest(event))
        previous = self._snapshot
        updated = reduce_halt_authority(
            previous=previous,
            event=event,
            config=self._config,
            codebook=self._codebook,
            read_at=received,
        )
        self._set_snapshot(updated, trigger=kind.value)

    def _set_snapshot(self, updated: HaltAuthoritySnapshot, *, trigger: str) -> None:
        previous = self._snapshot
        self._snapshot = updated
        state_changed = previous.state is not updated.state
        acknowledgment_changed = (previous.acknowledged_at is None) != (
            updated.acknowledged_at is None
        )
        epoch_changed = previous.epoch != updated.epoch
        if state_changed or acknowledgment_changed or epoch_changed:
            log = (
                _LOGGER.warning
                if previous.acknowledged_at is not None and updated.acknowledged_at is None
                else _LOGGER.info
            )
            log(
                "halt authority transition: trigger=%s state=%s->%s "
                "acknowledged=%s->%s epoch_changed=%s transitions=%s",
                trigger,
                previous.state.value,
                updated.state.value,
                previous.acknowledged_at is not None,
                updated.acknowledged_at is not None,
                epoch_changed,
                updated.transition_count,
            )
        if updated.acknowledged_at is not None and (
            previous.acknowledged_at is None or updated.state is HaltAuthorityState.OPEN_CONFIRMED
        ):
            self._cancel_recovery()
        elif (
            previous.state is HaltAuthorityState.OPEN_CONFIRMED
            and updated.state is HaltAuthorityState.UNKNOWN
        ) or (previous.acknowledged_at is not None and updated.acknowledged_at is None):
            self._arm_recovery(updated.observed_at)

    def _begin_resubscribe(self) -> None:
        self.connection_starting()
        _LOGGER.info(
            "halt authority resubscribe requested: state=%s thread_alive=true",
            self._snapshot.state.value,
        )

    def _arm_recovery(self, observed_at: datetime) -> None:
        if not self._config.session_open_at <= observed_at < self._config.session_close_at:
            self._cancel_recovery()
            return
        self._recovery_due_at = observed_at + self._resubscribe_backoff
        if (
            self._recovery_timer is not None
            or self._thread is None
            or not self._thread_started
            or not self._thread.is_alive()
            or self._closed
            or self._stopping
        ):
            return
        timer = self._recovery_timer_factory(
            self._resubscribe_backoff.total_seconds(),
            self._recover_live_stream,
        )
        timer.daemon = True
        self._recovery_timer = timer
        timer.start()

    def _cancel_recovery(self) -> None:
        timer = self._recovery_timer
        self._recovery_timer = None
        self._recovery_due_at = None
        if timer is not None:
            timer.cancel()

    def _recover_live_stream(self) -> None:
        request_resubscribe = False
        with self._lock:
            self._recovery_timer = None
            if (
                self._thread is not None
                and self._thread_started
                and self._thread.is_alive()
                and not self._closed
                and not self._stopping
            ):
                self._begin_resubscribe()
                request_resubscribe = True
        if request_resubscribe:
            try:
                self._stream.subscribe_trades(self._on_trade, self._config.symbol)
            except Exception:
                with self._lock:
                    _LOGGER.exception("halt authority resubscribe request failed")
                    self._arm_recovery(self._now())

    def _exact_subscription_ack(self, message: dict) -> bool:
        if type(message) is not dict or message.get("T") != "subscription":
            return False
        if set(message) - ({"T"} | _SUBSCRIPTION_CHANNELS):
            return False
        if message.get("trades") != [self._config.symbol]:
            return False
        if message.get("statuses") != [self._config.symbol]:
            return False
        if message.get("corrections", [self._config.symbol]) != [self._config.symbol]:
            return False
        if message.get("cancelErrors", [self._config.symbol]) != [self._config.symbol]:
            return False
        return all(
            key in {"trades", "statuses", "corrections", "cancelErrors"}
            or message.get(key, []) == []
            for key in _SUBSCRIPTION_CHANNELS
        )

    def _now(self) -> datetime:
        return _strict_utc(self._clock())


def _strict_utc(value: datetime) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise OpportunityHaltStreamError("HALT_STREAM_TIME_INVALID")
    return value.astimezone(UTC)


def _is_single_symbol(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and value != "*"
        and "," not in value
        and all(not character.isspace() for character in value)
    )

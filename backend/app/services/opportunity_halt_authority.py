from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from enum import Enum, StrEnum

from backend.app.policy.opportunity import TradingHaltState

_HASH = re.compile(r"[0-9a-f]{64}")


class HaltAuthorityError(ValueError):
    pass


class HaltAuthorityState(StrEnum):
    UNKNOWN = "UNKNOWN"
    OPEN_CONFIRMED = "OPEN_CONFIRMED"
    HALTED_LATCHED = "HALTED_LATCHED"
    RESUME_PENDING = "RESUME_PENDING"


class HaltCodeMeaning(StrEnum):
    HALT = "HALT"
    RESUME = "RESUME"


class HaltSequenceAuthority(StrEnum):
    """The sequence is assigned by the adapter after each accepted stream message."""

    ADAPTER_RECEIVE_ORDER_V1 = "ADAPTER_RECEIVE_ORDER_V1"


class HaltAuthorityEventKind(StrEnum):
    EPOCH_ACK = "EPOCH_ACK"
    TRADE = "TRADE"
    TRADING_STATUS = "TRADING_STATUS"
    QUOTE = "QUOTE"
    BAR = "BAR"
    TRADABLE = "TRADABLE"
    RECONNECT = "RECONNECT"
    GAP = "GAP"
    ERROR = "ERROR"
    MALFORMED = "MALFORMED"
    THREAD_DEAD = "THREAD_DEAD"


@dataclass(frozen=True)
class HaltStatusCodebook:
    version: str
    feed: str
    sdk_version: str
    mappings: tuple[tuple[str, HaltCodeMeaning], ...]
    source_hash: str


@dataclass(frozen=True)
class HaltAuthorityConfig:
    symbol: str
    feed: str
    sdk_version: str
    session_date: date
    session_open_at: datetime
    session_close_at: datetime
    maximum_trade_age: timedelta
    sequence_authority: HaltSequenceAuthority
    codebook_hash: str
    source_hash: str


@dataclass(frozen=True)
class HaltAuthorityEvent:
    """One adapter envelope; epoch and sequence are not provider-supplied fields."""

    kind: HaltAuthorityEventKind
    symbol: str
    epoch: str
    sequence: int
    event_at: datetime
    received_at: datetime
    status_code: str | None
    trade_id: str | None
    source_hash: str


@dataclass(frozen=True)
class HaltAuthoritySnapshot:
    symbol: str
    feed: str
    sdk_version: str
    sequence_authority: HaltSequenceAuthority
    codebook_hash: str
    config_hash: str
    session_date: date
    session_open_at: datetime
    session_close_at: datetime
    epoch: str | None
    acknowledged_at: datetime | None
    state: HaltAuthorityState
    last_sequence: int
    last_event_at: datetime | None
    last_event_hash: str | None
    decisive_event_hash: str | None
    last_rejected_event_hash: str | None
    last_trade_at: datetime | None
    last_trade_id: str | None
    last_trade_hash: str | None
    halt_latched_at: datetime | None
    halt_event_hash: str | None
    transition_count: int
    observed_at: datetime
    source_hash: str

    @property
    def trading_halt_state(self) -> TradingHaltState:
        if self.state is HaltAuthorityState.OPEN_CONFIRMED:
            return TradingHaltState.NOT_HALTED
        if self.state in {
            HaltAuthorityState.HALTED_LATCHED,
            HaltAuthorityState.RESUME_PENDING,
        }:
            return TradingHaltState.HALTED
        return TradingHaltState.UNKNOWN

    @property
    def trading_status_observed_at(self) -> datetime:
        return self.observed_at

    @property
    def trading_status_source_hash(self) -> str:
        return self.source_hash


def halt_status_codebook_digest(value: HaltStatusCodebook) -> str:
    return _canonical_hash(
        "alphadecay.opportunity.halt-codebook.v1",
        {
            "version": value.version,
            "feed": value.feed,
            "sdk_version": value.sdk_version,
            "mappings": value.mappings,
        },
    )


def halt_authority_config_digest(value: HaltAuthorityConfig) -> str:
    return _canonical_hash(
        "alphadecay.opportunity.halt-config.v1",
        {
            "symbol": value.symbol,
            "feed": value.feed,
            "sdk_version": value.sdk_version,
            "session_date": value.session_date,
            "session_open_at": value.session_open_at,
            "session_close_at": value.session_close_at,
            "maximum_trade_age": value.maximum_trade_age,
            "sequence_authority": value.sequence_authority,
            "codebook_hash": value.codebook_hash,
        },
    )


def halt_authority_event_digest(value: HaltAuthorityEvent) -> str:
    return _canonical_hash(
        "alphadecay.opportunity.halt-event.v1",
        {
            "kind": value.kind,
            "symbol": value.symbol,
            "epoch": value.epoch,
            "sequence": value.sequence,
            "event_at": value.event_at,
            "received_at": value.received_at,
            "status_code": value.status_code,
            "trade_id": value.trade_id,
        },
    )


def halt_authority_snapshot_digest(value: HaltAuthoritySnapshot) -> str:
    return _canonical_hash(
        "alphadecay.opportunity.halt-authority.v1",
        {
            "symbol": value.symbol,
            "feed": value.feed,
            "sdk_version": value.sdk_version,
            "sequence_authority": value.sequence_authority,
            "codebook_hash": value.codebook_hash,
            "config_hash": value.config_hash,
            "session_date": value.session_date,
            "session_open_at": value.session_open_at,
            "session_close_at": value.session_close_at,
            "epoch": value.epoch,
            "acknowledged_at": value.acknowledged_at,
            "state": value.state,
            "last_sequence": value.last_sequence,
            "last_event_at": value.last_event_at,
            "last_event_hash": value.last_event_hash,
            "decisive_event_hash": value.decisive_event_hash,
            "last_rejected_event_hash": value.last_rejected_event_hash,
            "last_trade_at": value.last_trade_at,
            "last_trade_id": value.last_trade_id,
            "last_trade_hash": value.last_trade_hash,
            "halt_latched_at": value.halt_latched_at,
            "halt_event_hash": value.halt_event_hash,
            "transition_count": value.transition_count,
            "observed_at": value.observed_at,
        },
    )


def initial_halt_authority(
    *, config: HaltAuthorityConfig, read_at: datetime
) -> HaltAuthoritySnapshot:
    _validate_config(config)
    observed_at = _utc(read_at, "HALT_READ_TIME_INVALID")
    value = HaltAuthoritySnapshot(
        symbol=config.symbol,
        feed=config.feed,
        sdk_version=config.sdk_version,
        sequence_authority=config.sequence_authority,
        codebook_hash=config.codebook_hash,
        config_hash=config.source_hash,
        session_date=config.session_date,
        session_open_at=config.session_open_at,
        session_close_at=config.session_close_at,
        epoch=None,
        acknowledged_at=None,
        state=HaltAuthorityState.UNKNOWN,
        last_sequence=0,
        last_event_at=None,
        last_event_hash=None,
        decisive_event_hash=None,
        last_rejected_event_hash=None,
        last_trade_at=None,
        last_trade_id=None,
        last_trade_hash=None,
        halt_latched_at=None,
        halt_event_hash=None,
        transition_count=0,
        observed_at=observed_at,
        source_hash="",
    )
    return _with_digest(value)


def reduce_halt_authority(
    *,
    previous: HaltAuthoritySnapshot,
    event: HaltAuthorityEvent,
    config: HaltAuthorityConfig,
    codebook: HaltStatusCodebook | None,
    read_at: datetime,
) -> HaltAuthoritySnapshot:
    _validate_config(config)
    _validate_previous(previous, config)
    observed_at = _utc(read_at, "HALT_READ_TIME_INVALID")
    _validate_read_time(previous, observed_at)
    if not _inside_session(config, observed_at):
        return _session_closed(previous, observed_at)
    if not _event_valid(event, config, observed_at):
        return _fail_closed(previous, observed_at)

    if event.kind is HaltAuthorityEventKind.EPOCH_ACK:
        return _acknowledge_epoch(previous, event, observed_at)
    if previous.epoch is None or previous.acknowledged_at is None:
        return _fail_closed(previous, observed_at, event.source_hash)
    if event.epoch != previous.epoch or event.sequence != previous.last_sequence + 1:
        return _fail_closed(previous, observed_at, event.source_hash)
    if previous.last_event_at is not None and event.event_at <= previous.last_event_at:
        return _fail_closed(previous, observed_at, event.source_hash)

    if event.kind in {
        HaltAuthorityEventKind.RECONNECT,
        HaltAuthorityEventKind.GAP,
        HaltAuthorityEventKind.ERROR,
        HaltAuthorityEventKind.MALFORMED,
        HaltAuthorityEventKind.THREAD_DEAD,
    }:
        return _failure_event(previous, event, observed_at)
    if event.kind is HaltAuthorityEventKind.TRADING_STATUS:
        return _status_event(previous, event, config, codebook, observed_at)
    if event.kind is HaltAuthorityEventKind.TRADE:
        return _trade_event(previous, event, config, observed_at)
    return _passive_event(previous, event, config, observed_at)


def read_halt_authority(
    *, previous: HaltAuthoritySnapshot, config: HaltAuthorityConfig, read_at: datetime
) -> HaltAuthoritySnapshot:
    _validate_config(config)
    _validate_previous(previous, config)
    observed_at = _utc(read_at, "HALT_READ_TIME_INVALID")
    _validate_read_time(previous, observed_at)
    if not _inside_session(config, observed_at):
        return _session_closed(previous, observed_at)
    if previous.state in {
        HaltAuthorityState.HALTED_LATCHED,
        HaltAuthorityState.RESUME_PENDING,
    }:
        return _replace_snapshot(previous, observed_at=observed_at)
    if (
        previous.state is not HaltAuthorityState.OPEN_CONFIRMED
        or previous.last_trade_at is None
        or observed_at - previous.last_trade_at > config.maximum_trade_age
        or observed_at < previous.last_trade_at
    ):
        return _transition(previous, HaltAuthorityState.UNKNOWN, observed_at=observed_at)
    return _replace_snapshot(previous, observed_at=observed_at)


def _acknowledge_epoch(
    previous: HaltAuthoritySnapshot,
    event: HaltAuthorityEvent,
    observed_at: datetime,
) -> HaltAuthoritySnapshot:
    if (
        previous.acknowledged_at is not None
        or not event.epoch
        or event.sequence != 1
        or (previous.epoch is not None and event.epoch == previous.epoch)
        or (
            previous.last_event_at is not None
            and event.event_at <= previous.last_event_at
        )
        or event.status_code is not None
        or event.trade_id is not None
    ):
        return _fail_closed(previous, observed_at, event.source_hash)
    state = (
        previous.state
        if previous.state
        in {HaltAuthorityState.HALTED_LATCHED, HaltAuthorityState.RESUME_PENDING}
        else HaltAuthorityState.UNKNOWN
    )
    return _transition(
        previous,
        state,
        epoch=event.epoch,
        acknowledged_at=event.event_at,
        last_sequence=event.sequence,
        last_event_at=event.event_at,
        last_event_hash=event.source_hash,
        decisive_event_hash=event.source_hash,
        last_trade_at=None,
        last_trade_id=None,
        last_trade_hash=None,
        observed_at=observed_at,
    )


def _failure_event(
    previous: HaltAuthoritySnapshot,
    event: HaltAuthorityEvent,
    observed_at: datetime,
) -> HaltAuthoritySnapshot:
    state = (
        previous.state
        if previous.state
        in {HaltAuthorityState.HALTED_LATCHED, HaltAuthorityState.RESUME_PENDING}
        else HaltAuthorityState.UNKNOWN
    )
    return _transition(
        previous,
        state,
        acknowledged_at=None,
        last_sequence=0,
        last_event_at=event.event_at,
        last_event_hash=event.source_hash,
        decisive_event_hash=event.source_hash,
        observed_at=observed_at,
    )


def _status_event(
    previous: HaltAuthoritySnapshot,
    event: HaltAuthorityEvent,
    config: HaltAuthorityConfig,
    codebook: HaltStatusCodebook | None,
    observed_at: datetime,
) -> HaltAuthoritySnapshot:
    if not _codebook_valid(codebook, config) or not event.status_code or event.trade_id is not None:
        return _fail_closed(previous, observed_at, event.source_hash)
    mappings = dict(codebook.mappings)
    meaning = mappings.get(event.status_code)
    if meaning is HaltCodeMeaning.HALT:
        state = HaltAuthorityState.HALTED_LATCHED
    elif meaning is HaltCodeMeaning.RESUME and previous.state in {
        HaltAuthorityState.HALTED_LATCHED,
        HaltAuthorityState.RESUME_PENDING,
    }:
        state = HaltAuthorityState.RESUME_PENDING
    elif meaning is HaltCodeMeaning.RESUME:
        state = HaltAuthorityState.UNKNOWN
    else:
        return _fail_closed(previous, observed_at, event.source_hash)
    changes: dict[str, object] = {}
    if meaning is HaltCodeMeaning.HALT:
        changes = {
            "halt_latched_at": event.event_at,
            "halt_event_hash": event.source_hash,
        }
    return _transition(
        previous,
        state,
        last_sequence=event.sequence,
        last_event_at=event.event_at,
        last_event_hash=event.source_hash,
        decisive_event_hash=event.source_hash,
        observed_at=observed_at,
        **changes,
    )


def _trade_event(
    previous: HaltAuthoritySnapshot,
    event: HaltAuthorityEvent,
    config: HaltAuthorityConfig,
    observed_at: datetime,
) -> HaltAuthoritySnapshot:
    valid_trade = (
        _nonblank_token(event.trade_id)
        and event.trade_id != previous.last_trade_id
        and event.status_code is None
        and previous.acknowledged_at is not None
        and event.event_at > previous.acknowledged_at
        and timedelta(0) <= observed_at - event.event_at <= config.maximum_trade_age
    )
    if not valid_trade:
        return _fail_closed(previous, observed_at, event.source_hash)
    state = previous.state
    if state in {HaltAuthorityState.UNKNOWN, HaltAuthorityState.RESUME_PENDING}:
        state = HaltAuthorityState.OPEN_CONFIRMED
    elif state is HaltAuthorityState.HALTED_LATCHED:
        state = HaltAuthorityState.HALTED_LATCHED
    changes: dict[str, object] = {}
    if state is HaltAuthorityState.OPEN_CONFIRMED:
        changes = {"halt_latched_at": None, "halt_event_hash": None}
    return _transition(
        previous,
        state,
        last_sequence=event.sequence,
        last_event_at=event.event_at,
        last_event_hash=event.source_hash,
        decisive_event_hash=event.source_hash,
        last_trade_at=event.event_at,
        last_trade_id=event.trade_id,
        last_trade_hash=event.source_hash,
        observed_at=observed_at,
        **changes,
    )


def _passive_event(
    previous: HaltAuthoritySnapshot,
    event: HaltAuthorityEvent,
    config: HaltAuthorityConfig,
    observed_at: datetime,
) -> HaltAuthoritySnapshot:
    if event.status_code is not None or event.trade_id is not None:
        return _fail_closed(previous, observed_at, event.source_hash)
    state = previous.state
    if (
        state is HaltAuthorityState.OPEN_CONFIRMED
        and (
            previous.last_trade_at is None
            or observed_at - previous.last_trade_at > config.maximum_trade_age
            or observed_at < previous.last_trade_at
        )
    ):
        state = HaltAuthorityState.UNKNOWN
    return _replace_snapshot(
        previous,
        state=state,
        transition_count=(
            previous.transition_count + 1
            if state is not previous.state
            else previous.transition_count
        ),
        last_sequence=event.sequence,
        last_event_at=event.event_at,
        last_event_hash=event.source_hash,
        observed_at=observed_at,
    )


def _fail_closed(
    previous: HaltAuthoritySnapshot,
    observed_at: datetime,
    rejected_event_hash: str | None = None,
) -> HaltAuthoritySnapshot:
    state = (
        previous.state
        if previous.state
        in {HaltAuthorityState.HALTED_LATCHED, HaltAuthorityState.RESUME_PENDING}
        else HaltAuthorityState.UNKNOWN
    )
    changes: dict[str, object] = {}
    if rejected_event_hash is not None:
        changes["last_rejected_event_hash"] = rejected_event_hash
    return _transition(
        previous,
        state,
        acknowledged_at=None,
        last_sequence=0,
        observed_at=observed_at,
        **changes,
    )


def _session_closed(
    previous: HaltAuthoritySnapshot, observed_at: datetime
) -> HaltAuthoritySnapshot:
    return _transition(
        previous,
        HaltAuthorityState.UNKNOWN,
        acknowledged_at=None,
        last_sequence=0,
        halt_latched_at=None,
        halt_event_hash=None,
        observed_at=observed_at,
    )


def _transition(
    previous: HaltAuthoritySnapshot,
    state: HaltAuthorityState,
    **changes: object,
) -> HaltAuthoritySnapshot:
    if state is not previous.state:
        changes["transition_count"] = previous.transition_count + 1
    return _replace_snapshot(previous, state=state, **changes)


def _replace_snapshot(
    previous: HaltAuthoritySnapshot, **changes: object
) -> HaltAuthoritySnapshot:
    value = replace(previous, source_hash="", **changes)
    return _with_digest(value)


def _with_digest(value: HaltAuthoritySnapshot) -> HaltAuthoritySnapshot:
    return replace(value, source_hash=halt_authority_snapshot_digest(value))


def _validate_config(config: HaltAuthorityConfig) -> None:
    try:
        valid = (
            type(config) is HaltAuthorityConfig
            and all(
                _nonblank_token(value)
                for value in (config.symbol, config.feed, config.sdk_version)
            )
            and type(config.session_date) is date
            and type(config.sequence_authority) is HaltSequenceAuthority
            and _is_hash(config.codebook_hash)
            and type(config.maximum_trade_age) is timedelta
            and config.maximum_trade_age > timedelta(0)
            and config.maximum_trade_age <= timedelta(minutes=1)
            and _utc(config.session_open_at, "HALT_CONFIG_INVALID") < _utc(
                config.session_close_at, "HALT_CONFIG_INVALID"
            )
            and config.session_open_at.date() == config.session_close_at.date()
            and config.session_date == config.session_open_at.date()
            and config.source_hash == halt_authority_config_digest(config)
        )
    except (AttributeError, HaltAuthorityError, TypeError, ValueError):
        raise HaltAuthorityError("HALT_CONFIG_INVALID") from None
    if not valid:
        raise HaltAuthorityError("HALT_CONFIG_INVALID")


def _validate_previous(previous: HaltAuthoritySnapshot, config: HaltAuthorityConfig) -> None:
    try:
        cross_scope_valid = (
            type(previous) is HaltAuthoritySnapshot
            and previous.source_hash == halt_authority_snapshot_digest(previous)
            and previous.config_hash == config.source_hash
            and previous.symbol == config.symbol
            and previous.feed == config.feed
            and previous.sdk_version == config.sdk_version
            and previous.sequence_authority is config.sequence_authority
            and previous.codebook_hash == config.codebook_hash
            and previous.session_date == config.session_date
            and previous.session_open_at == config.session_open_at
            and previous.session_close_at == config.session_close_at
        )
        observed_at = _utc(previous.observed_at, "HALT_PREVIOUS_INVALID")
        acknowledged_at = _optional_utc(previous.acknowledged_at)
        last_event_at = _optional_utc(previous.last_event_at)
        last_trade_at = _optional_utc(previous.last_trade_at)
        halt_latched_at = _optional_utc(previous.halt_latched_at)
        active_epoch_valid = (
            acknowledged_at is None
            and previous.last_sequence == 0
        ) or (
            bool(previous.epoch)
            and acknowledged_at is not None
            and previous.last_sequence >= 1
            and last_event_at is not None
            and acknowledged_at <= last_event_at
        )
        hash_pairs_valid = (
            (last_trade_at is None)
            == (previous.last_trade_hash is None)
            == (previous.last_trade_id is None)
            and (halt_latched_at is None) == (previous.halt_event_hash is None)
            and (last_event_at is None) == (previous.last_event_hash is None)
            and _optional_hash(previous.last_event_hash)
            and _optional_hash(previous.decisive_event_hash)
            and _optional_hash(previous.last_rejected_event_hash)
            and _optional_hash(previous.last_trade_hash)
            and _optional_hash(previous.halt_event_hash)
            and (
                previous.last_trade_id is None
                or _nonblank_token(previous.last_trade_id)
            )
        )
        chronology_valid = (
            (last_event_at is None or last_event_at <= observed_at)
            and (last_trade_at is None or last_trade_at <= observed_at)
            and (halt_latched_at is None or halt_latched_at <= observed_at)
            and (
                last_event_at is None
                or last_trade_at is None
                or last_trade_at <= last_event_at
            )
            and (
                last_event_at is None
                or halt_latched_at is None
                or halt_latched_at <= last_event_at
            )
            and (
                acknowledged_at is None
                or last_trade_at is None
                or acknowledged_at < last_trade_at
            )
        )
        state_valid = (
            previous.state is HaltAuthorityState.UNKNOWN
            or (
                previous.state is HaltAuthorityState.OPEN_CONFIRMED
                and acknowledged_at is not None
                and last_trade_at is not None
                and halt_latched_at is None
            )
            or (
                previous.state
                in {
                    HaltAuthorityState.HALTED_LATCHED,
                    HaltAuthorityState.RESUME_PENDING,
                }
                and halt_latched_at is not None
                and (
                    previous.state is not HaltAuthorityState.RESUME_PENDING
                    or (
                        last_event_at is not None
                        and halt_latched_at < last_event_at
                    )
                )
            )
        )
        scalar_valid = (
            type(previous.state) is HaltAuthorityState
            and type(previous.last_sequence) is int
            and previous.last_sequence >= 0
            and type(previous.transition_count) is int
            and previous.transition_count >= 0
        )
    except (AttributeError, HaltAuthorityError, TypeError, ValueError):
        raise HaltAuthorityError("HALT_PREVIOUS_INVALID") from None
    if not all(
        (
            cross_scope_valid,
            active_epoch_valid,
            hash_pairs_valid,
            chronology_valid,
            state_valid,
            scalar_valid,
        )
    ):
        raise HaltAuthorityError("HALT_PREVIOUS_INVALID")


def _validate_read_time(previous: HaltAuthoritySnapshot, observed_at: datetime) -> None:
    if observed_at < previous.observed_at:
        raise HaltAuthorityError("HALT_READ_TIME_REGRESSION")


def _codebook_valid(
    codebook: HaltStatusCodebook | None, config: HaltAuthorityConfig
) -> bool:
    if type(codebook) is not HaltStatusCodebook:
        return False
    try:
        pairs_valid = all(type(item) is tuple and len(item) == 2 for item in codebook.mappings)
        if not pairs_valid:
            return False
        codes = [code for code, _ in codebook.mappings]
        meanings_valid = all(
            type(code) is str and bool(code) and type(meaning) is HaltCodeMeaning
            for code, meaning in codebook.mappings
        )
        structurally_valid = (
            bool(codebook.version)
            and bool(codebook.feed)
            and bool(codebook.sdk_version)
            and bool(codebook.mappings)
            and len(codes) == len(set(codes))
            and meanings_valid
            and codebook.source_hash == halt_status_codebook_digest(codebook)
        )
    except (TypeError, ValueError):
        return False
    if not structurally_valid:
        return False
    valid = (
        codebook.feed == config.feed
        and codebook.sdk_version == config.sdk_version
        and codebook.source_hash == config.codebook_hash
    )
    return valid


def _event_valid(
    event: HaltAuthorityEvent, config: HaltAuthorityConfig, observed_at: datetime
) -> bool:
    try:
        if type(event) is not HaltAuthorityEvent:
            return False
        event_at = _utc(event.event_at, "HALT_EVENT_INVALID")
        received_at = _utc(event.received_at, "HALT_EVENT_INVALID")
        digest_matches = event.source_hash == halt_authority_event_digest(event)
    except (AttributeError, HaltAuthorityError, TypeError, ValueError):
        return False
    return (
        type(event.kind) is HaltAuthorityEventKind
        and event.symbol == config.symbol
        and _nonblank_token(event.epoch)
        and type(event.sequence) is int
        and event.sequence > 0
        and config.session_open_at <= event_at < config.session_close_at
        and event_at <= received_at <= observed_at
        and digest_matches
    )


def _inside_session(config: HaltAuthorityConfig, observed_at: datetime) -> bool:
    return config.session_open_at <= observed_at < config.session_close_at


def _utc(value: datetime, code: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise HaltAuthorityError(code)
    return value.astimezone(UTC)


def _is_hash(value: str) -> bool:
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _optional_hash(value: str | None) -> bool:
    return value is None or _is_hash(value)


def _optional_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _utc(value, "HALT_PREVIOUS_INVALID")


def _nonblank_token(value: str | None) -> bool:
    return type(value) is str and bool(value) and value.strip() == value


def _canonical_hash(domain: str, value: object) -> str:
    payload = json.dumps(
        {"domain": domain, "value": _canonical_value(value)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _canonical_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _utc(value, "HALT_TIME_INVALID").isoformat(timespec="microseconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value // timedelta(microseconds=1)
    if hasattr(value, "__dataclass_fields__"):
        return {key: _canonical_value(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_canonical_value(item) for item in value]
    return value

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from backend.app.policy.opportunity import TradingHaltState
from backend.app.services.opportunity_halt_authority import (
    HaltAuthorityConfig,
    HaltAuthorityEvent,
    HaltAuthorityEventKind,
    HaltAuthorityState,
    HaltCodeMeaning,
    HaltSequenceAuthority,
    HaltStatusCodebook,
    halt_authority_config_digest,
    halt_authority_event_digest,
    halt_authority_snapshot_digest,
    halt_status_codebook_digest,
    initial_halt_authority,
    read_halt_authority,
    reduce_halt_authority,
)

OPEN = datetime(2026, 8, 28, 13, 30, tzinfo=UTC)
CLOSE = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
ACK = datetime(2026, 8, 28, 14, 0, tzinfo=UTC)


def _codebook() -> HaltStatusCodebook:
    value = HaltStatusCodebook(
        version="alpaca-sip-v1",
        feed="SIP",
        sdk_version="0.44.0",
        mappings=(("H", HaltCodeMeaning.HALT), ("T", HaltCodeMeaning.RESUME)),
        source_hash="",
    )
    return replace(value, source_hash=halt_status_codebook_digest(value))


def _config() -> HaltAuthorityConfig:
    codebook = _codebook()
    value = HaltAuthorityConfig(
        symbol="NVDA",
        feed="SIP",
        sdk_version="0.44.0",
        session_date=date(2026, 8, 28),
        session_open_at=OPEN,
        session_close_at=CLOSE,
        maximum_trade_age=timedelta(seconds=15),
        sequence_authority=HaltSequenceAuthority.ADAPTER_RECEIVE_ORDER_V1,
        codebook_hash=codebook.source_hash,
        source_hash="",
    )
    return replace(value, source_hash=halt_authority_config_digest(value))


def _event(
    kind: HaltAuthorityEventKind,
    *,
    sequence: int,
    at: datetime,
    epoch: str = "epoch-1",
    status_code: str | None = None,
    trade_id: str | None = None,
) -> HaltAuthorityEvent:
    value = HaltAuthorityEvent(
        kind=kind,
        symbol="NVDA",
        epoch=epoch,
        sequence=sequence,
        event_at=at,
        received_at=at + timedelta(milliseconds=20),
        status_code=status_code,
        trade_id=trade_id,
        source_hash="",
    )
    return replace(value, source_hash=halt_authority_event_digest(value))


def _initial():
    return initial_halt_authority(config=_config(), read_at=ACK - timedelta(seconds=1))


def _ack(previous=None):
    previous = previous or _initial()
    event = _event(HaltAuthorityEventKind.EPOCH_ACK, sequence=1, at=ACK)
    return reduce_halt_authority(
        previous=previous,
        event=event,
        config=_config(),
        codebook=_codebook(),
        read_at=event.received_at,
    )


def test_startup_and_ack_without_trade_are_unknown() -> None:
    initial = _initial()
    acknowledged = _ack(initial)

    assert initial.state is HaltAuthorityState.UNKNOWN
    assert initial.trading_halt_state is TradingHaltState.UNKNOWN
    assert acknowledged.state is HaltAuthorityState.UNKNOWN
    assert acknowledged.acknowledged_at == ACK
    assert acknowledged.trading_halt_state is TradingHaltState.UNKNOWN


def test_ack_is_only_valid_as_first_adapter_event_of_an_unacknowledged_epoch() -> None:
    acknowledged = _ack()
    repeated = _event(
        HaltAuthorityEventKind.EPOCH_ACK,
        sequence=1,
        at=ACK + timedelta(seconds=1),
    )
    wrong_first_sequence = _event(
        HaltAuthorityEventKind.EPOCH_ACK,
        sequence=2,
        at=ACK,
    )

    repeated_result = _reduce(acknowledged, repeated)
    sequence_result = _reduce(_initial(), wrong_first_sequence)

    assert repeated_result.state is HaltAuthorityState.UNKNOWN
    assert repeated_result.acknowledged_at is None
    assert sequence_result.state is HaltAuthorityState.UNKNOWN
    assert sequence_result.epoch is None


def test_fresh_actual_trade_after_ack_confirms_open() -> None:
    acknowledged = _ack()
    trade = _event(
        HaltAuthorityEventKind.TRADE,
        sequence=2,
        at=ACK + timedelta(seconds=1),
        trade_id="trade-1",
    )
    result = reduce_halt_authority(
        previous=acknowledged,
        event=trade,
        config=_config(),
        codebook=_codebook(),
        read_at=trade.received_at,
    )

    assert result.state is HaltAuthorityState.OPEN_CONFIRMED
    assert result.trading_halt_state is TradingHaltState.NOT_HALTED
    assert result.last_trade_at == trade.event_at
    assert result.decisive_event_hash == trade.source_hash
    assert result.source_hash == halt_authority_snapshot_digest(result)
    assert result.trading_status_observed_at == result.observed_at
    assert result.trading_status_source_hash == result.source_hash


def test_reused_trade_identity_cannot_refresh_open_authority() -> None:
    state = _open_state()
    duplicate = _event(
        HaltAuthorityEventKind.TRADE,
        sequence=state.last_sequence + 1,
        at=state.last_trade_at + timedelta(seconds=1),  # type: ignore[operator]
        trade_id="trade-1",
    )

    result = _reduce(state, duplicate)

    assert result.state is HaltAuthorityState.UNKNOWN
    assert result.acknowledged_at is None


@pytest.mark.parametrize(
    "kind",
    [
        HaltAuthorityEventKind.QUOTE,
        HaltAuthorityEventKind.BAR,
        HaltAuthorityEventKind.TRADABLE,
    ],
)
def test_quotes_bars_and_tradable_never_confirm_open(kind: HaltAuthorityEventKind) -> None:
    acknowledged = _ack()
    event = _event(kind, sequence=2, at=ACK + timedelta(seconds=1))

    result = reduce_halt_authority(
        previous=acknowledged,
        event=event,
        config=_config(),
        codebook=_codebook(),
        read_at=event.received_at,
    )

    assert result.state is HaltAuthorityState.UNKNOWN
    assert result.trading_halt_state is TradingHaltState.UNKNOWN


def test_passive_message_kind_changes_authority_hash() -> None:
    acknowledged = _ack()
    quote = _event(
        HaltAuthorityEventKind.QUOTE,
        sequence=2,
        at=ACK + timedelta(seconds=1),
    )
    bar = replace(quote, kind=HaltAuthorityEventKind.BAR, source_hash="")
    bar = replace(bar, source_hash=halt_authority_event_digest(bar))

    quote_result = _reduce(acknowledged, quote)
    bar_result = _reduce(acknowledged, bar)

    assert quote_result.last_event_hash == quote.source_hash
    assert bar_result.last_event_hash == bar.source_hash
    assert quote_result.source_hash != bar_result.source_hash


def test_halt_latches_and_resume_requires_a_later_trade() -> None:
    open_state = _open_state()
    halt = _event(
        HaltAuthorityEventKind.TRADING_STATUS,
        sequence=3,
        at=ACK + timedelta(seconds=2),
        status_code="H",
    )
    halted = _reduce(open_state, halt)
    resume = _event(
        HaltAuthorityEventKind.TRADING_STATUS,
        sequence=4,
        at=ACK + timedelta(seconds=3),
        status_code="T",
    )
    pending = _reduce(halted, resume)
    trade = _event(
        HaltAuthorityEventKind.TRADE,
        sequence=5,
        at=ACK + timedelta(seconds=4),
        trade_id="trade-2",
    )
    reopened = _reduce(pending, trade)

    assert halted.state is HaltAuthorityState.HALTED_LATCHED
    assert halted.trading_halt_state is TradingHaltState.HALTED
    assert pending.state is HaltAuthorityState.RESUME_PENDING
    assert pending.trading_halt_state is TradingHaltState.HALTED
    assert reopened.state is HaltAuthorityState.OPEN_CONFIRMED
    assert reopened.trading_halt_state is TradingHaltState.NOT_HALTED


def test_halt_from_unknown_is_still_authoritative() -> None:
    acknowledged = _ack()
    halt = _event(
        HaltAuthorityEventKind.TRADING_STATUS,
        sequence=2,
        at=ACK + timedelta(seconds=1),
        status_code="H",
    )
    assert _reduce(acknowledged, halt).state is HaltAuthorityState.HALTED_LATCHED


@pytest.mark.parametrize(
    "kind",
    [
        HaltAuthorityEventKind.RECONNECT,
        HaltAuthorityEventKind.GAP,
        HaltAuthorityEventKind.ERROR,
        HaltAuthorityEventKind.MALFORMED,
        HaltAuthorityEventKind.THREAD_DEAD,
    ],
)
def test_failure_boundaries_fail_closed_and_do_not_clear_a_halt(
    kind: HaltAuthorityEventKind,
) -> None:
    event = _event(kind, sequence=6, at=ACK + timedelta(seconds=5))

    assert _reduce(_open_state(), event).state is HaltAuthorityState.UNKNOWN
    assert _reduce(_halted_state(), event).state is HaltAuthorityState.HALTED_LATCHED


def test_unknown_status_code_or_missing_codebook_fails_closed() -> None:
    event = _event(
        HaltAuthorityEventKind.TRADING_STATUS,
        sequence=3,
        at=ACK + timedelta(seconds=2),
        status_code="NOT-IN-CODEBOOK",
    )
    assert _reduce(_open_state(), event).state is HaltAuthorityState.UNKNOWN

    known = replace(event, status_code="H", source_hash="")
    known = replace(known, source_hash=halt_authority_event_digest(known))
    result = reduce_halt_authority(
        previous=_open_state(),
        event=known,
        config=_config(),
        codebook=None,
        read_at=known.received_at,
    )
    assert result.state is HaltAuthorityState.UNKNOWN

    malformed = replace(
        _codebook(),
        mappings=(("H", HaltCodeMeaning.HALT), ("H", HaltCodeMeaning.RESUME)),
    )
    malformed_event = replace(
        known,
        sequence=3,
        event_at=ACK + timedelta(seconds=2),
        received_at=ACK + timedelta(seconds=2, milliseconds=20),
        source_hash="",
    )
    malformed_event = replace(
        malformed_event,
        source_hash=halt_authority_event_digest(malformed_event),
    )
    malformed_result = reduce_halt_authority(
        previous=_open_state(),
        event=malformed_event,
        config=_config(),
        codebook=malformed,
        read_at=ACK + timedelta(seconds=3),
    )
    assert malformed_result.state is HaltAuthorityState.UNKNOWN


def test_out_of_order_or_wrong_epoch_fails_closed_without_clearing_halt() -> None:
    open_state = _open_state()
    stale = _event(
        HaltAuthorityEventKind.TRADE,
        sequence=open_state.last_sequence,
        at=ACK + timedelta(seconds=3),
        trade_id="stale",
    )
    wrong_epoch = _event(
        HaltAuthorityEventKind.TRADE,
        sequence=10,
        at=ACK + timedelta(seconds=3),
        epoch="epoch-2",
        trade_id="wrong-epoch",
    )

    assert _reduce(open_state, stale).state is HaltAuthorityState.UNKNOWN
    assert _reduce(open_state, wrong_epoch).state is HaltAuthorityState.UNKNOWN
    assert _reduce(_halted_state(), stale).state is HaltAuthorityState.HALTED_LATCHED


def test_failed_epoch_requires_a_new_epoch_identity_before_recovery() -> None:
    failed = _reduce(
        _open_state(),
        _event(
            HaltAuthorityEventKind.GAP,
            sequence=3,
            at=ACK + timedelta(seconds=2),
        ),
    )
    reused_epoch_ack = _event(
        HaltAuthorityEventKind.EPOCH_ACK,
        sequence=1,
        at=ACK + timedelta(seconds=3),
    )
    new_epoch_ack = _event(
        HaltAuthorityEventKind.EPOCH_ACK,
        sequence=1,
        at=ACK + timedelta(seconds=4),
        epoch="epoch-2",
    )

    rejected = _reduce(failed, reused_epoch_ack)
    accepted = _reduce(rejected, new_epoch_ack)
    fresh_trade = _event(
        HaltAuthorityEventKind.TRADE,
        sequence=2,
        at=ACK + timedelta(seconds=5),
        epoch="epoch-2",
        trade_id="trade-2",
    )
    reopened = _reduce(accepted, fresh_trade)

    assert rejected.acknowledged_at is None
    assert accepted.epoch == "epoch-2"
    assert accepted.acknowledged_at == new_epoch_ack.event_at
    assert accepted.last_trade_at is None
    assert reopened.state is HaltAuthorityState.OPEN_CONFIRMED


def test_sequence_gap_fails_closed() -> None:
    state = _open_state()
    skipped = _event(
        HaltAuthorityEventKind.TRADE,
        sequence=state.last_sequence + 2,
        at=ACK + timedelta(seconds=3),
        trade_id="gap",
    )
    assert _reduce(state, skipped).state is HaltAuthorityState.UNKNOWN

    next_trade = _event(
        HaltAuthorityEventKind.TRADE,
        sequence=state.last_sequence + 1,
        at=ACK + timedelta(seconds=4),
        trade_id="not-recovered",
    )
    assert _reduce(_reduce(state, skipped), next_trade).state is HaltAuthorityState.UNKNOWN


def test_passive_messages_cannot_keep_an_old_trade_safe() -> None:
    state = _open_state()
    quote = _event(
        HaltAuthorityEventKind.QUOTE,
        sequence=state.last_sequence + 1,
        at=state.last_trade_at + timedelta(seconds=16),  # type: ignore[operator]
    )
    result = reduce_halt_authority(
        previous=state,
        event=quote,
        config=_config(),
        codebook=_codebook(),
        read_at=quote.received_at,
    )
    assert result.state is HaltAuthorityState.UNKNOWN
    assert result.trading_halt_state is TradingHaltState.UNKNOWN


def test_trade_must_be_post_ack_fresh_and_well_formed() -> None:
    acknowledged = _ack()
    before_ack = _event(
        HaltAuthorityEventKind.TRADE,
        sequence=2,
        at=ACK - timedelta(microseconds=1),
        trade_id="old",
    )
    before_ack = replace(
        before_ack,
        received_at=ACK + timedelta(milliseconds=20),
        source_hash="",
    )
    before_ack = replace(
        before_ack,
        source_hash=halt_authority_event_digest(before_ack),
    )
    stale = _event(
        HaltAuthorityEventKind.TRADE,
        sequence=2,
        at=ACK + timedelta(seconds=1),
        trade_id="stale",
    )
    missing_id = _event(
        HaltAuthorityEventKind.TRADE,
        sequence=2,
        at=ACK + timedelta(seconds=1),
    )

    assert _reduce(acknowledged, before_ack).state is HaltAuthorityState.UNKNOWN
    assert reduce_halt_authority(
        previous=acknowledged,
        event=stale,
        config=_config(),
        codebook=_codebook(),
        read_at=stale.event_at + timedelta(seconds=16),
    ).state is HaltAuthorityState.UNKNOWN
    assert _reduce(acknowledged, missing_id).state is HaltAuthorityState.UNKNOWN


def test_read_goes_unknown_when_trade_is_stale_or_session_rolls() -> None:
    open_state = _open_state()

    stale = read_halt_authority(
        previous=open_state,
        config=_config(),
        read_at=open_state.last_trade_at + timedelta(seconds=16),  # type: ignore[operator]
    )
    rollover = read_halt_authority(
        previous=open_state,
        config=_config(),
        read_at=datetime(2026, 8, 29, 14, 0, tzinfo=UTC),
    )

    assert stale.state is HaltAuthorityState.UNKNOWN
    assert rollover.state is HaltAuthorityState.UNKNOWN
    assert stale.source_hash == halt_authority_snapshot_digest(stale)


def test_session_close_is_exclusive_for_reads_and_events() -> None:
    open_state = _open_state()
    at_close = read_halt_authority(
        previous=open_state,
        config=_config(),
        read_at=CLOSE,
    )
    late_trade = _event(
        HaltAuthorityEventKind.TRADE,
        sequence=open_state.last_sequence + 1,
        at=CLOSE - timedelta(milliseconds=1),
        trade_id="closing-trade",
    )
    late_trade = replace(late_trade, received_at=CLOSE, source_hash="")
    late_trade = replace(late_trade, source_hash=halt_authority_event_digest(late_trade))
    reduced = reduce_halt_authority(
        previous=open_state,
        event=late_trade,
        config=_config(),
        codebook=_codebook(),
        read_at=CLOSE,
    )

    assert at_close.state is HaltAuthorityState.UNKNOWN
    assert at_close.acknowledged_at is None
    assert reduced.state is HaltAuthorityState.UNKNOWN
    assert reduced.acknowledged_at is None


def test_read_time_cannot_regress() -> None:
    state = _open_state()
    with pytest.raises(ValueError, match="HALT_READ_TIME_REGRESSION"):
        read_halt_authority(
            previous=state,
            config=_config(),
            read_at=state.observed_at - timedelta(microseconds=1),
        )


def test_self_hashed_but_semantically_impossible_open_snapshot_is_rejected() -> None:
    state = _open_state()
    impossible = replace(
        state,
        last_trade_at=None,
        last_trade_hash=None,
        source_hash="",
    )
    impossible = replace(
        impossible,
        source_hash=halt_authority_snapshot_digest(impossible),
    )

    with pytest.raises(ValueError, match="HALT_PREVIOUS_INVALID"):
        read_halt_authority(
            previous=impossible,
            config=_config(),
            read_at=state.observed_at,
        )


def test_self_hashed_but_unproven_halt_snapshot_is_rejected() -> None:
    state = _open_state()
    impossible = replace(
        state,
        state=HaltAuthorityState.HALTED_LATCHED,
        source_hash="",
    )
    impossible = replace(
        impossible,
        source_hash=halt_authority_snapshot_digest(impossible),
    )

    with pytest.raises(ValueError, match="HALT_PREVIOUS_INVALID"):
        read_halt_authority(
            previous=impossible,
            config=_config(),
            read_at=state.observed_at,
        )


def test_halted_read_remains_halted_when_messages_stop() -> None:
    halted = _halted_state()
    result = read_halt_authority(
        previous=halted,
        config=_config(),
        read_at=halted.observed_at + timedelta(minutes=5),
    )
    assert result.state is HaltAuthorityState.HALTED_LATCHED
    assert result.trading_halt_state is TradingHaltState.HALTED

    rollover = read_halt_authority(
        previous=halted,
        config=_config(),
        read_at=datetime(2026, 8, 29, 14, 0, tzinfo=UTC),
    )
    assert rollover.state is HaltAuthorityState.UNKNOWN
    assert rollover.trading_halt_state is TradingHaltState.UNKNOWN


def test_hashes_bind_codebook_epoch_ack_transition_event_trade_and_read_time() -> None:
    state = _open_state()
    variants = (
        replace(state, epoch="other"),
        replace(state, acknowledged_at=state.acknowledged_at + timedelta(microseconds=1)),  # type: ignore[operator]
        replace(state, transition_count=state.transition_count + 1),
        replace(state, last_event_hash="4" * 64),
        replace(state, decisive_event_hash="1" * 64),
        replace(state, last_rejected_event_hash="5" * 64),
        replace(state, last_trade_hash="2" * 64),
        replace(state, observed_at=state.observed_at + timedelta(microseconds=1)),
    )
    assert all(halt_authority_snapshot_digest(item) != state.source_hash for item in variants)

    config = _config()
    changed_codebook = replace(config, codebook_hash="3" * 64)
    assert halt_authority_config_digest(changed_codebook) != config.source_hash
    changed_sequence_authority = replace(
        config,
        sequence_authority="PROVIDER_SEQUENCE",  # type: ignore[arg-type]
    )
    assert halt_authority_config_digest(changed_sequence_authority) != config.source_hash


def test_invalid_config_is_rejected() -> None:
    with pytest.raises(ValueError, match="HALT_CONFIG_INVALID"):
        initial_halt_authority(
            config=replace(_config(), feed="IEX"),
            read_at=ACK + timedelta(seconds=2),
        )

    wrong_date = replace(_config(), session_date=date(2026, 8, 27), source_hash="")
    wrong_date = replace(wrong_date, source_hash=halt_authority_config_digest(wrong_date))
    with pytest.raises(ValueError, match="HALT_CONFIG_INVALID"):
        initial_halt_authority(
            config=wrong_date,
            read_at=ACK + timedelta(seconds=2),
        )

    malformed_age = replace(
        _config(),
        maximum_trade_age="15 seconds",  # type: ignore[arg-type]
        source_hash="",
    )
    malformed_age = replace(
        malformed_age,
        source_hash=halt_authority_config_digest(malformed_age),
    )
    with pytest.raises(ValueError, match="HALT_CONFIG_INVALID"):
        initial_halt_authority(
            config=malformed_age,
            read_at=ACK + timedelta(seconds=2),
        )


def _reduce(previous, event):
    return reduce_halt_authority(
        previous=previous,
        event=event,
        config=_config(),
        codebook=_codebook(),
        read_at=event.received_at,
    )


def _open_state():
    acknowledged = _ack()
    trade = _event(
        HaltAuthorityEventKind.TRADE,
        sequence=2,
        at=ACK + timedelta(seconds=1),
        trade_id="trade-1",
    )
    return _reduce(acknowledged, trade)


def _halted_state():
    halt = _event(
        HaltAuthorityEventKind.TRADING_STATUS,
        sequence=3,
        at=ACK + timedelta(seconds=2),
        status_code="H",
    )
    return _reduce(_open_state(), halt)

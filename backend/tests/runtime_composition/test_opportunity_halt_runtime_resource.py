from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from alpaca.data.enums import DataFeed
from pydantic import SecretStr

from backend.app.config import RuntimeRole, Settings
from backend.app.runtime import (
    RuntimeCompositionError,
    build_production_opportunity_halt_resource,
)
from backend.app.runtime.providers import OpportunityHaltAuthority
from backend.app.services.opportunity_halt_authority import (
    HaltAuthorityConfig,
    HaltAuthoritySnapshot,
    HaltSequenceAuthority,
    halt_authority_config_digest,
)


class _Stream:
    def __init__(self, events: list[str], **kwargs: object) -> None:
        self.events = events
        self.kwargs = kwargs

    def stop(self) -> None:
        self.events.append("stream.stop")


class _Adapter:
    def __init__(
        self,
        stream: _Stream,
        events: list[str],
        *,
        start_error: BaseException | None = None,
        stop_error: BaseException | None = None,
    ) -> None:
        self.stream = stream
        self.events = events
        self.start_error = start_error
        self.stop_error = stop_error

    def start(self) -> None:
        self.events.append("adapter.start")
        if self.start_error is not None:
            raise self.start_error

    def stop(self) -> None:
        self.events.append("adapter.stop")
        if self.stop_error is not None:
            raise self.stop_error

    def snapshot(self, *, read_at: datetime | None = None) -> HaltAuthoritySnapshot:
        raise NotImplementedError


def _settings() -> Settings:
    return Settings(
        app_account_role=RuntimeRole.DEVELOPMENT,
        app_policy_hash=SecretStr("a" * 64),
        app_calibration_hash=SecretStr("b" * 64),
        app_calibration_decision_boundary=datetime(2026, 8, 28, 16, tzinfo=UTC),
        app_calibration_sealed_at=datetime(2026, 8, 28, 16, 1, tzinfo=UTC),
        app_entry_equity_floor=SecretStr("99000"),
        app_maximum_lifetime_entries=SecretStr("3"),
        app_maximum_lifetime_risk=SecretStr("1500"),
        app_maximum_position_loss=SecretStr("900"),
        app_maximum_entry_quantity=SecretStr("4"),
        alpaca_api_endpoint="https://paper-api.alpaca.markets",
        alpaca_api_key=SecretStr("paper-key"),
        alpaca_secret_key=SecretStr("paper-secret"),
        database_url=SecretStr("postgresql://db.invalid/alphadecay"),
        gemini_api_key=SecretStr("model-key"),
        app_owner_access_code=SecretStr("owner-access-code"),
        app_session_secret=SecretStr("s" * 32),
        app_provider_settings_secret=SecretStr("p" * 32),
        app_allowed_origin="https://alphadecay.example",
        scheduler_token=SecretStr("t" * 32),
    )


def _config() -> HaltAuthorityConfig:
    from backend.app.alpaca.opportunity_halt_stream import (
        alpaca_trading_status_codebook,
    )

    value = HaltAuthorityConfig(
        symbol="NVDA",
        feed="IEX",
        sdk_version="0.44.0",
        session_date=date(2026, 8, 31),
        session_open_at=datetime(2026, 8, 31, 13, 30, tzinfo=UTC),
        session_close_at=datetime(2026, 8, 31, 20, tzinfo=UTC),
        maximum_trade_age=timedelta(seconds=15),
        sequence_authority=HaltSequenceAuthority.ADAPTER_RECEIVE_ORDER_V1,
        codebook_hash=alpaca_trading_status_codebook().source_hash,
        source_hash="",
    )
    return replace(value, source_hash=halt_authority_config_digest(value))


def test_factory_constructs_starts_and_owns_one_pinned_stream() -> None:
    events: list[str] = []
    captured: dict[str, object] = {}

    def stream_factory(**kwargs: object) -> _Stream:
        events.append("stream.construct")
        stream = _Stream(events, **kwargs)
        captured["stream"] = stream
        return stream

    def adapter_factory(stream: _Stream, **kwargs: object) -> OpportunityHaltAuthority:
        events.append("adapter.construct")
        captured.update(kwargs)
        adapter = _Adapter(stream, events)
        captured["adapter"] = adapter
        return adapter

    def clock() -> datetime:
        return datetime(2026, 8, 31, 13, 31, tzinfo=UTC)

    resource = build_production_opportunity_halt_resource(
        _settings(),
        config=_config(),
        clock=clock,
        stream_factory=stream_factory,
        adapter_factory=adapter_factory,
    )

    assert events == ["stream.construct", "adapter.construct", "adapter.start"]
    assert resource.value is captured["adapter"]
    stream = captured["stream"]
    assert isinstance(stream, _Stream)
    assert stream.kwargs == {
        "api_key": "paper-key",
        "secret_key": "paper-secret",
        "raw_data": False,
        "feed": DataFeed.IEX,
    }
    assert captured["config"] == _config()
    assert captured["clock"] is clock
    assert captured["codebook"].source_hash == _config().codebook_hash
    assert "paper-key" not in repr(resource)
    assert "paper-secret" not in repr(resource)

    assert resource.close is not None
    resource.close()
    assert events[-1] == "adapter.stop"


def test_adapter_construction_failure_stops_partial_stream() -> None:
    events: list[str] = []

    def stream_factory(**kwargs: object) -> _Stream:
        events.append("stream.construct")
        return _Stream(events, **kwargs)

    def adapter_factory(_stream: _Stream, **_kwargs: object) -> OpportunityHaltAuthority:
        events.append("adapter.construct")
        raise RuntimeError("adapter failed")

    with pytest.raises(RuntimeError, match="adapter failed"):
        build_production_opportunity_halt_resource(
            _settings(),
            config=_config(),
            clock=lambda: datetime(2026, 8, 31, 13, 31, tzinfo=UTC),
            stream_factory=stream_factory,
            adapter_factory=adapter_factory,
        )

    assert events == ["stream.construct", "adapter.construct", "stream.stop"]


def test_start_failure_stops_constructed_adapter_exactly_once() -> None:
    events: list[str] = []

    def stream_factory(**kwargs: object) -> _Stream:
        events.append("stream.construct")
        return _Stream(events, **kwargs)

    def adapter_factory(stream: _Stream, **_kwargs: object) -> OpportunityHaltAuthority:
        events.append("adapter.construct")
        return _Adapter(stream, events, start_error=RuntimeError("start failed"))

    with pytest.raises(RuntimeError, match="start failed"):
        build_production_opportunity_halt_resource(
            _settings(),
            config=_config(),
            clock=lambda: datetime(2026, 8, 31, 13, 31, tzinfo=UTC),
            stream_factory=stream_factory,
            adapter_factory=adapter_factory,
        )

    assert events == [
        "stream.construct",
        "adapter.construct",
        "adapter.start",
        "adapter.stop",
    ]


def test_start_and_cleanup_failures_are_both_reported_after_all_cleanup() -> None:
    events: list[str] = []

    def stream_factory(**kwargs: object) -> _Stream:
        events.append("stream.construct")
        return _Stream(events, **kwargs)

    def adapter_factory(stream: _Stream, **_kwargs: object) -> OpportunityHaltAuthority:
        events.append("adapter.construct")
        return _Adapter(
            stream,
            events,
            start_error=RuntimeError("start failed"),
            stop_error=RuntimeError("stop failed"),
        )

    with pytest.raises(BaseExceptionGroup) as caught:
        build_production_opportunity_halt_resource(
            _settings(),
            config=_config(),
            clock=lambda: datetime(2026, 8, 31, 13, 31, tzinfo=UTC),
            stream_factory=stream_factory,
            adapter_factory=adapter_factory,
        )

    assert str(caught.value).startswith(
        "OPPORTUNITY_HALT_STREAM_STARTUP_AND_CLEANUP_FAILED"
    )
    assert [str(error) for error in caught.value.exceptions] == [
        "start failed",
        "stop failed",
    ]
    assert events[-1:] == ["adapter.stop"]
    assert events.count("adapter.stop") == 1
    assert "stream.stop" not in events


@pytest.mark.parametrize(
    "settings_update",
    [
        {"alpaca_api_endpoint": "https://api.alpaca.markets"},
        {"alpaca_paper_trade": False},
    ],
)
def test_paper_boundary_rejects_before_stream_construction(
    settings_update: dict[str, object],
) -> None:
    calls = 0

    def stream_factory(**_kwargs: object) -> _Stream:
        nonlocal calls
        calls += 1
        raise AssertionError("stream must not be constructed")

    with pytest.raises(RuntimeCompositionError, match="PAPER_TRADING_REQUIRED"):
        build_production_opportunity_halt_resource(
            _settings().model_copy(update=settings_update),
            config=_config(),
            clock=lambda: datetime(2026, 8, 31, 13, 31, tzinfo=UTC),
            stream_factory=stream_factory,
        )

    assert calls == 0


def test_nonofficial_codebook_binding_rejects_before_stream_construction() -> None:
    calls = 0

    def stream_factory(**_kwargs: object) -> _Stream:
        nonlocal calls
        calls += 1
        raise AssertionError("stream must not be constructed")

    config = replace(_config(), codebook_hash="0" * 64, source_hash="")
    config = replace(config, source_hash=halt_authority_config_digest(config))
    with pytest.raises(RuntimeCompositionError, match="HALT_STREAM_CODEBOOK_MISMATCH"):
        build_production_opportunity_halt_resource(
            _settings(),
            config=config,
            clock=lambda: datetime(2026, 8, 31, 13, 31, tzinfo=UTC),
            stream_factory=stream_factory,
        )

    assert calls == 0


def test_invalid_config_rejects_before_stream_construction() -> None:
    calls = 0

    def stream_factory(**_kwargs: object) -> _Stream:
        nonlocal calls
        calls += 1
        raise AssertionError("stream must not be constructed")

    with pytest.raises(ValueError, match="HALT_CONFIG_INVALID"):
        build_production_opportunity_halt_resource(
            _settings(),
            config=replace(_config(), source_hash="0" * 64),
            clock=lambda: datetime(2026, 8, 31, 13, 31, tzinfo=UTC),
            stream_factory=stream_factory,
        )

    assert calls == 0

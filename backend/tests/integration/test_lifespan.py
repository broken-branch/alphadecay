from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from backend.app import main
from backend.app.performance import UnavailablePerformanceProofReader


class Aggregate:
    def __init__(self, *, close_error: BaseException | None = None) -> None:
        self.service = object()
        self.autonomy = object()
        self.provider_settings = object()
        self.strategy_curation = object()
        self.runtime = object()
        self.persistence = SimpleNamespace(
            experiment_registry=object(),
            experiment_performance_reader=object(),
            experiment_window_reader=object(),
            performance_proof_reader=object(),
            performance_repository=object(),
        )
        self.close_error = close_error
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


def settings() -> SimpleNamespace:
    owner_reference = SecretStr("dummy-owner-access-code-long-enough")
    session_reference = SecretStr("dummy-session-secret-placeholder-01")
    scheduler_reference = SecretStr("dummy-scheduler-token-placeholder-01")
    return SimpleNamespace(
        app_owner_access_code=owner_reference,
        app_session_secret=session_reference,
        app_allowed_origin="https://alphadecay.example",
        scheduler_token=scheduler_reference,
    )


def test_lifespan_publishes_complete_agent_then_unpublishes_before_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = FastAPI()
    aggregate = Aggregate()
    observed_during_close: dict[str, bool] = {}

    async def build(*_args, **_kwargs):
        return aggregate

    async def close() -> None:
        observed_during_close.update(
            {
                name: hasattr(target.state, name)
                for name in ("agent_run_service", "scheduler_authenticator", "persistence")
            }
        )
        await Aggregate.aclose(aggregate)

    aggregate.aclose = close
    monkeypatch.setenv("APP_RUNTIME_CONFIG_REQUIRED", "true")
    monkeypatch.setattr(main, "Settings", settings)
    monkeypatch.setattr(main, "build_production_agent", build)

    async def exercise() -> None:
        async with main.lifespan(target):
            assert target.state.production_agent is aggregate
            assert target.state.agent_run_service is aggregate.service
            assert target.state.account_autonomy_service is aggregate.autonomy
            assert target.state.owner_provider_settings_service is aggregate.provider_settings
            assert target.state.strategy_curation_service is aggregate.strategy_curation
            assert target.state.runtime_composition is aggregate.runtime
            assert target.state.persistence is aggregate.persistence
            assert target.state.experiment_registry is aggregate.persistence.experiment_registry
            assert (
                target.state.experiment_window_reader
                is aggregate.persistence.experiment_window_reader
            )
            assert (
                target.state.performance_proof_reader
                is aggregate.persistence.performance_proof_reader
            )
            assert (
                target.state.performance_publisher is aggregate.persistence.performance_repository
            )

    asyncio.run(exercise())

    assert aggregate.close_calls == 1
    assert observed_during_close == {
        "agent_run_service": False,
        "scheduler_authenticator": False,
        "persistence": False,
    }
    assert isinstance(target.state.performance_proof_reader, UnavailablePerformanceProofReader)
    for name in main._RUNTIME_STATE_FIELDS:
        assert not hasattr(target.state, name)


def test_lifespan_closes_agent_when_later_constructor_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = FastAPI()
    aggregate = Aggregate()

    async def build(*_args, **_kwargs):
        return aggregate

    def fail_owner_manager(**_kwargs):
        raise RuntimeError("owner manager failed")

    monkeypatch.setenv("APP_RUNTIME_CONFIG_REQUIRED", "true")
    monkeypatch.setattr(main, "Settings", settings)
    monkeypatch.setattr(main, "build_production_agent", build)
    monkeypatch.setattr(main, "OwnerSessionManager", fail_owner_manager)

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="owner manager failed"):
            async with main.lifespan(target):
                raise AssertionError("lifespan yielded after startup failure")

    asyncio.run(exercise())

    assert aggregate.close_calls == 1
    assert isinstance(target.state.performance_proof_reader, UnavailablePerformanceProofReader)
    for name in main._RUNTIME_STATE_FIELDS:
        assert not hasattr(target.state, name)


def test_lifespan_unpublishes_state_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = FastAPI()
    aggregate = Aggregate(close_error=RuntimeError("cleanup failed"))

    async def build(*_args, **_kwargs):
        return aggregate

    monkeypatch.setenv("APP_RUNTIME_CONFIG_REQUIRED", "true")
    monkeypatch.setattr(main, "Settings", settings)
    monkeypatch.setattr(main, "build_production_agent", build)

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="cleanup failed"):
            async with main.lifespan(target):
                pass

    asyncio.run(exercise())

    assert aggregate.close_calls == 1
    assert isinstance(target.state.performance_proof_reader, UnavailablePerformanceProofReader)
    for name in main._RUNTIME_STATE_FIELDS:
        assert not hasattr(target.state, name)


def test_lifespan_without_runtime_configuration_does_not_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = FastAPI()
    calls = 0

    async def build(*_args, **_kwargs):
        nonlocal calls
        calls += 1

    monkeypatch.delenv("APP_RUNTIME_CONFIG_REQUIRED", raising=False)
    monkeypatch.setattr(main, "build_production_agent", build)

    async def exercise() -> None:
        async with main.lifespan(target):
            pass

    asyncio.run(exercise())
    assert calls == 0

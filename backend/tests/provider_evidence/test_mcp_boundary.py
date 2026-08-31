import asyncio
import json
import operator
import os
import sys
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult

from backend.app.alpaca.mcp import (
    EXPOSED_TOOL_SURFACE,
    RUNTIME_RESEARCH_ALLOWLIST,
    AlpacaMCPResearchClient,
    MCPBoundaryError,
    MCPClientLimits,
    build_launch_spec,
    prepare_research_call,
    validate_tool_surface,
)


def test_pinned_installed_option_schemas_use_plural_symbols_and_explicit_feed() -> None:
    spec = json.loads(
        files("alpaca_mcp_server")
        .joinpath("specs/market-data-api.json")
        .read_text(encoding="utf-8")
    )
    parameters = spec["components"]["parameters"]

    assert parameters["option_symbols"]["name"] == "symbols"
    assert parameters["option_symbols"]["required"] is True
    assert parameters["option_feed"]["name"] == "feed"
    assert "indicative" in parameters["option_feed"]["description"]


class _FakeMCPClient:
    def __init__(self, *, payload: dict[str, object]) -> None:
        self._payload = payload

    async def __aenter__(self) -> "_FakeMCPClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def list_tools(self, *, max_pages: int) -> list[SimpleNamespace]:
        assert max_pages == 1
        quote_schema = {
            "type": "object",
            "properties": {
                "symbols": {"type": "string"},
                "feed": {"type": "string", "enum": ["opra", "indicative"]},
            },
            "required": ["symbols"],
            "additionalProperties": False,
        }
        return [
            SimpleNamespace(
                name=name,
                inputSchema=(
                    quote_schema if name == "get_option_latest_quote" else {"type": "object"}
                ),
            )
            for name in EXPOSED_TOOL_SURFACE
        ]

    async def call_tool_mcp(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        timeout: float,
    ) -> CallToolResult:
        return CallToolResult(content=[], structuredContent=self._payload)


def _clock(*values: datetime):
    remaining = iter(values)
    return lambda: next(remaining)


def _write_mcp_fixture_server(
    directory: Path,
    *,
    tool_payload_source: str,
) -> Path:
    executable = directory / "alpaca-mcp-server"
    tool_names = json.dumps(sorted(EXPOSED_TOOL_SURFACE))
    executable.write_text(
        f"""#!{sys.executable}
import json
import os
import sys

TOOL_NAMES = {tool_names}


def respond(request_id, result):
    sys.stdout.write(json.dumps({{"jsonrpc": "2.0", "id": request_id, "result": result}}))
    sys.stdout.write("\\n")
    sys.stdout.flush()


for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        respond(
            message["id"],
            {{
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {{"tools": {{}}}},
                "serverInfo": {{"name": "fixture", "version": "1"}},
            }},
        )
    elif method == "tools/list":
        respond(
            message["id"],
            {{
                "tools": [
                    {{"name": name, "inputSchema": {{"type": "object"}}}}
                    for name in TOOL_NAMES
                ]
            }},
        )
    elif method == "tools/call":
        payload = {tool_payload_source}
        respond(
            message["id"],
            {{"content": [], "structuredContent": payload, "isError": False}},
        )
"""
    )
    executable.chmod(0o700)
    return executable


@pytest.fixture
def fake_mcp_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_mcp_fixture_server(tmp_path, tool_payload_source="{}")
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")


def test_launch_spec_has_frozen_no_shell_argv_and_isolated_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_mcp_fixture_server(tmp_path, tool_payload_source="{}")
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    spec = build_launch_spec(api_key="fixture-key", secret_key="fixture-secret")

    assert Path(spec.argv[0]).is_absolute()
    assert Path(spec.argv[0]).name == "alpaca-mcp-server"
    assert Path(spec.argv[0]).is_file()
    assert spec.shell is False
    assert spec.environment == {
        "ALPACA_API_KEY": "fixture-key",
        "ALPACA_SECRET_KEY": "fixture-secret",
        "ALPACA_PAPER_TRADE": "true",
        "ALPACA_TOOLSETS": "assets,options-data,corporate-actions,news",
        "LC_ALL": "C.UTF-8",
    }
    assert "fixture-key" not in repr(spec)
    assert "fixture-secret" not in repr(spec)


def test_exact_22_tool_surface_and_narrow_runtime_allowlist_are_frozen() -> None:
    validate_tool_surface(EXPOSED_TOOL_SURFACE)

    assert len(EXPOSED_TOOL_SURFACE) == 22
    assert len(RUNTIME_RESEARCH_ALLOWLIST) == 12
    assert "get_option_latest_trade" not in RUNTIME_RESEARCH_ALLOWLIST
    assert "search_alpaca_docs" not in RUNTIME_RESEARCH_ALLOWLIST
    assert "submit_order" not in EXPOSED_TOOL_SURFACE

    with pytest.raises(MCPBoundaryError, match="MCP_TOOL_SURFACE_MISMATCH"):
        validate_tool_surface(EXPOSED_TOOL_SURFACE | {"submit_order"})


def test_runtime_call_is_application_allowlisted_and_forces_indicative_feed() -> None:
    call = prepare_research_call("get_option_chain", {"underlying_symbol": "NVDA"})

    assert call.arguments == {"underlying_symbol": "NVDA", "feed": "indicative"}
    with pytest.raises(TypeError):
        operator.setitem(call.arguments, "feed", "opra")
    with pytest.raises(MCPBoundaryError, match="MCP_OPTION_FEED_FORBIDDEN"):
        prepare_research_call(
            "get_option_snapshot",
            {"symbol": "NVDA260918C00230000", "feed": "opra"},
        )
    with pytest.raises(MCPBoundaryError, match="MCP_TOOL_NOT_RUNTIME_ALLOWED"):
        prepare_research_call("get_option_latest_trade", {"symbol": "fixture"})


def test_connected_client_returns_bounded_normalized_data_and_sanitized_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_mcp_fixture_server(tmp_path, tool_payload_source="{}")
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    payload = {
        "quotes": {
            "NVDA260918C00230000": {
                "ask_price": "4.20",
                "bid_price": "4.00",
            }
        }
    }
    started_at = datetime(2026, 8, 28, 20, 30, tzinfo=UTC)
    completed_at = datetime(2026, 8, 28, 20, 30, 1, tzinfo=UTC)
    fake = _FakeMCPClient(payload=payload)
    client = AlpacaMCPResearchClient(
        api_key="fixture-key",
        secret_key="fixture-secret",
        limits=MCPClientLimits(),
        client_factory=lambda _spec, _limits: fake,
        now=_clock(started_at, completed_at),
    )

    async def exercise() -> object:
        async with client:
            return await client.call(
                "get_option_latest_quote",
                {"symbols": "NVDA260918C00230000"},
            )

    result = asyncio.run(exercise())

    assert result.tool_name == "get_option_latest_quote"
    assert result.data == payload
    assert result.audit.tool_name == "get_option_latest_quote"
    assert result.audit.argument_hash == (
        "ea2b99e7454a897aa2076f8678eb677065f2ea3564771b706aabbc7c615e09eb"
    )
    assert result.audit.started_at == started_at
    assert result.audit.completed_at == completed_at
    assert result.audit.result_summary_hash == (
        "ec7db8a45911954eb4de4678b43c7a1bb71bb0f5d27f6211ca3be7500d86b6c6"
    )
    assert result.audit.quality == "COMPLETE"
    assert not hasattr(result.audit, "arguments")
    assert not hasattr(result.audit, "result")


def test_connected_client_preserves_the_structured_result_size_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_mcp_fixture_server(tmp_path, tool_payload_source="{}")
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    fake = _FakeMCPClient(payload={"blob": "x" * 200})
    client = AlpacaMCPResearchClient(
        api_key="fixture-key",
        secret_key="fixture-secret",
        limits=MCPClientLimits(max_result_bytes=100),
        client_factory=lambda _spec, _limits: fake,
    )

    async def exercise() -> None:
        async with client:
            with pytest.raises(MCPBoundaryError, match="MCP_RESULT_TOO_LARGE"):
                await client.call("get_clock", {})

    asyncio.run(exercise())


def test_canceled_client_exit_retains_and_reawaits_the_connected_handle(
    fake_mcp_executable: None,
) -> None:
    class BlockingExitClient(_FakeMCPClient):
        def __init__(self) -> None:
            super().__init__(payload={})
            self.exit_started = asyncio.Event()
            self.exit_release = asyncio.Event()
            self.exit_calls = 0
            self.exit_completed = False

        async def __aexit__(self, *_args: object) -> None:
            self.exit_calls += 1
            self.exit_started.set()
            await self.exit_release.wait()
            self.exit_completed = True

    connected = BlockingExitClient()
    client = AlpacaMCPResearchClient(
        api_key="fixture-key",
        secret_key="fixture-secret",
        client_factory=lambda _spec, _limits: connected,
    )

    async def exercise() -> None:
        await client.__aenter__()
        first = asyncio.create_task(client.__aexit__(None, None, None))
        await connected.exit_started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        connected.exit_release.set()
        await client.__aexit__(None, None, None)

    asyncio.run(exercise())

    assert connected.exit_calls == 1
    assert connected.exit_completed is True


def test_transient_client_exit_failure_retries_without_reentering_or_leaking(
    fake_mcp_executable: None,
) -> None:
    class FlakyExitClient(_FakeMCPClient):
        def __init__(self) -> None:
            super().__init__(payload={})
            self.enter_calls = 0
            self.exit_calls = 0
            self.open = False

        async def __aenter__(self) -> "FlakyExitClient":
            self.enter_calls += 1
            self.open = True
            return self

        async def __aexit__(self, *_args: object) -> None:
            self.exit_calls += 1
            if self.exit_calls == 1:
                raise RuntimeError("transient fixture exit failure")
            self.open = False

    connected = FlakyExitClient()
    client = AlpacaMCPResearchClient(
        api_key="fixture-key",
        secret_key="fixture-secret",
        client_factory=lambda _spec, _limits: connected,
    )

    async def exercise() -> None:
        await client.__aenter__()
        with pytest.raises(RuntimeError, match="transient fixture exit failure"):
            await client.__aexit__(None, None, None)
        with pytest.raises(MCPBoundaryError, match="MCP_SESSION_NOT_CONNECTED"):
            await client.call("get_clock", {})
        await client.__aexit__(None, None, None)
        with pytest.raises(MCPBoundaryError, match="MCP_SESSION_ALREADY_CONNECTED"):
            await client.__aenter__()

    asyncio.run(exercise())

    assert connected.enter_calls == 1
    assert connected.exit_calls == 2
    assert connected.open is False


def test_internal_client_exit_cancellation_does_not_poison_retry(
    fake_mcp_executable: None,
) -> None:
    class CancelingExitClient(_FakeMCPClient):
        def __init__(self) -> None:
            super().__init__(payload={})
            self.exit_calls = 0

        async def __aexit__(self, *_args: object) -> None:
            self.exit_calls += 1
            if self.exit_calls == 1:
                raise asyncio.CancelledError

    connected = CancelingExitClient()
    client = AlpacaMCPResearchClient(
        api_key="fixture-key",
        secret_key="fixture-secret",
        client_factory=lambda _spec, _limits: connected,
    )

    async def exercise() -> None:
        await client.__aenter__()
        with pytest.raises(asyncio.CancelledError):
            await client.__aexit__(None, None, None)
        await client.__aexit__(None, None, None)

    asyncio.run(exercise())

    assert connected.exit_calls == 2


def test_concurrent_exit_waiters_share_each_retry_attempt(
    fake_mcp_executable: None,
) -> None:
    class CoordinatedExitClient(_FakeMCPClient):
        def __init__(self) -> None:
            super().__init__(payload={})
            self.exit_started = asyncio.Event()
            self.exit_release = asyncio.Event()
            self.exit_calls = 0

        async def __aexit__(self, *_args: object) -> None:
            self.exit_calls += 1
            if self.exit_calls == 1:
                self.exit_started.set()
                await self.exit_release.wait()
                raise RuntimeError("shared fixture exit failure")

    connected = CoordinatedExitClient()
    client = AlpacaMCPResearchClient(
        api_key="fixture-key",
        secret_key="fixture-secret",
        client_factory=lambda _spec, _limits: connected,
    )

    async def exercise() -> tuple[object, object]:
        await client.__aenter__()
        first = asyncio.create_task(client.__aexit__(None, None, None))
        await connected.exit_started.wait()
        second = asyncio.create_task(client.__aexit__(None, None, None))
        await asyncio.sleep(0)
        connected.exit_release.set()
        failures = await asyncio.gather(first, second, return_exceptions=True)
        await client.__aexit__(None, None, None)
        return failures

    failures = asyncio.run(exercise())

    assert all(
        isinstance(error, RuntimeError) and str(error) == "shared fixture exit failure"
        for error in failures
    )
    assert connected.exit_calls == 2


def test_failed_tool_surface_cleanup_retries_the_entered_session_without_reentry(
    fake_mcp_executable: None,
) -> None:
    class InvalidSurfaceClient(_FakeMCPClient):
        def __init__(self) -> None:
            super().__init__(payload={})
            self.enter_calls = 0
            self.exit_calls = 0
            self.open = False

        async def __aenter__(self) -> "InvalidSurfaceClient":
            self.enter_calls += 1
            self.open = True
            return self

        async def list_tools(self, *, max_pages: int) -> list[SimpleNamespace]:
            assert max_pages == 1
            return []

        async def __aexit__(self, *_args: object) -> None:
            self.exit_calls += 1
            if self.exit_calls == 1:
                raise RuntimeError("transient initialization cleanup failure")
            self.open = False

    connected = InvalidSurfaceClient()
    client = AlpacaMCPResearchClient(
        api_key="fixture-key",
        secret_key="fixture-secret",
        client_factory=lambda _spec, _limits: connected,
    )

    async def exercise() -> None:
        with pytest.raises(MCPBoundaryError, match="MCP_TOOL_SURFACE_MISMATCH"):
            await client.__aenter__()
        with pytest.raises(MCPBoundaryError, match="MCP_SESSION_NOT_CONNECTED"):
            await client.call("get_clock", {})
        await client.__aexit__(None, None, None)
        await client.__aexit__(None, None, None)
        with pytest.raises(MCPBoundaryError, match="MCP_SESSION_ALREADY_CONNECTED"):
            await client.__aenter__()

    asyncio.run(exercise())

    assert connected.enter_calls == 1
    assert connected.exit_calls == 2
    assert connected.open is False


def test_canceled_initialization_cleanup_waiter_keeps_the_shared_retry(
    fake_mcp_executable: None,
) -> None:
    class CancelThenCloseClient(_FakeMCPClient):
        def __init__(self) -> None:
            super().__init__(payload={})
            self.enter_calls = 0
            self.exit_calls = 0
            self.retry_started = asyncio.Event()
            self.retry_release = asyncio.Event()
            self.successful_exits = 0

        async def __aenter__(self) -> "CancelThenCloseClient":
            self.enter_calls += 1
            return self

        async def list_tools(self, *, max_pages: int) -> list[SimpleNamespace]:
            assert max_pages == 1
            return []

        async def __aexit__(self, *_args: object) -> None:
            self.exit_calls += 1
            if self.exit_calls == 1:
                raise asyncio.CancelledError
            self.retry_started.set()
            await self.retry_release.wait()
            self.successful_exits += 1

    connected = CancelThenCloseClient()
    client = AlpacaMCPResearchClient(
        api_key="fixture-key",
        secret_key="fixture-secret",
        client_factory=lambda _spec, _limits: connected,
    )

    async def exercise() -> None:
        with pytest.raises(MCPBoundaryError, match="MCP_TOOL_SURFACE_MISMATCH"):
            await client.__aenter__()
        first = asyncio.create_task(client.__aexit__(None, None, None))
        await connected.retry_started.wait()
        canceled_waiter = asyncio.create_task(client.__aexit__(None, None, None))
        await asyncio.sleep(0)
        canceled_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await canceled_waiter
        connected.retry_release.set()
        await first
        await client.__aexit__(None, None, None)

    asyncio.run(exercise())

    assert connected.enter_calls == 1
    assert connected.exit_calls == 2
    assert connected.successful_exits == 1


def test_stdio_child_receives_only_the_frozen_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_mcp_fixture_server(
        tmp_path,
        tool_payload_source='{"environment": dict(os.environ)}',
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    expected_environment = {
        "ALPACA_API_KEY": "fixture-key",
        "ALPACA_SECRET_KEY": "fixture-secret",
        "ALPACA_PAPER_TRADE": "true",
        "ALPACA_TOOLSETS": "assets,options-data,corporate-actions,news",
        "LC_ALL": "C.UTF-8",
    }
    client = AlpacaMCPResearchClient(
        api_key="fixture-key",
        secret_key="fixture-secret",
    )

    async def exercise() -> object:
        async with client:
            return await client.call("get_clock", {})

    result = asyncio.run(exercise())

    assert result.data == {"environment": expected_environment}


def test_stdio_transport_rejects_an_oversized_frame_before_result_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_mcp_fixture_server(
        tmp_path,
        tool_payload_source='{"blob": "x" * 20_000}',
    )
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    client = AlpacaMCPResearchClient(
        api_key="fixture-key",
        secret_key="fixture-secret",
        limits=MCPClientLimits(max_result_bytes=100, max_frame_bytes=4_096),
    )

    async def exercise() -> None:
        async with client:
            with pytest.raises(MCPBoundaryError, match="MCP_RESPONSE_FRAME_TOO_LARGE"):
                await client.call("get_clock", {})

    asyncio.run(exercise())

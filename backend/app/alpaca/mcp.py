from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from shutil import which
from types import MappingProxyType
from typing import Protocol

import anyio
from mcp import ClientSession, types
from mcp.shared.message import SessionMessage
from mcp.types import CallToolResult
from pydantic import ValidationError

_TOOLSETS = "assets,options-data,corporate-actions,news"
_INDICATIVE_TOOLS = frozenset(
    {"get_option_chain", "get_option_snapshot", "get_option_latest_quote"}
)

EXPOSED_TOOL_SURFACE = frozenset(
    {
        "fetch_alpaca_doc",
        "get_all_assets",
        "get_alpaca_endpoint_docs",
        "get_asset",
        "get_calendar",
        "get_clock",
        "get_corporate_action_announcement",
        "get_corporate_action_announcements",
        "get_corporate_actions",
        "get_news",
        "get_option_bars",
        "get_option_chain",
        "get_option_contract",
        "get_option_contracts",
        "get_option_exchange_codes",
        "get_option_latest_quote",
        "get_option_latest_trade",
        "get_option_snapshot",
        "get_option_trades",
        "list_alpaca_api_endpoints",
        "search_alpaca_api_specs",
        "search_alpaca_docs",
    }
)

RUNTIME_RESEARCH_ALLOWLIST = frozenset(
    {
        "get_asset",
        "get_calendar",
        "get_clock",
        "get_option_contracts",
        "get_option_contract",
        "get_option_chain",
        "get_option_snapshot",
        "get_option_latest_quote",
        "get_news",
        "get_corporate_actions",
        "get_corporate_action_announcements",
        "get_corporate_action_announcement",
    }
)


class MCPBoundaryError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, repr=False)
class MCPLaunchSpec:
    argv: tuple[str, ...]
    environment: Mapping[str, str]
    shell: bool = False

    def __repr__(self) -> str:
        return f"MCPLaunchSpec(argv={self.argv!r}, environment=<redacted>, shell={self.shell!r})"


@dataclass(frozen=True)
class MCPResearchCall:
    tool_name: str
    arguments: Mapping[str, object]


@dataclass(frozen=True)
class MCPClientLimits:
    call_timeout_seconds: float = 10.0
    max_result_bytes: int = 200_000
    max_frame_bytes: int = 400_000
    max_tool_pages: int = 1

    def __post_init__(self) -> None:
        if self.call_timeout_seconds <= 0:
            raise ValueError("MCP_CALL_TIMEOUT_INVALID")
        if self.max_result_bytes <= 0:
            raise ValueError("MCP_RESULT_LIMIT_INVALID")
        if self.max_frame_bytes <= 0:
            raise ValueError("MCP_FRAME_LIMIT_INVALID")
        if self.max_tool_pages != 1:
            raise ValueError("MCP_TOOL_PAGE_LIMIT_INVALID")


@dataclass(frozen=True)
class MCPResearchAudit:
    tool_name: str
    argument_hash: str
    started_at: datetime
    completed_at: datetime
    result_summary_hash: str
    quality: str


@dataclass(frozen=True)
class MCPResearchResult:
    tool_name: str
    data: object
    audit: MCPResearchAudit


@dataclass
class _TransportState:
    error_code: str | None = None


class ConnectedMCPClient(Protocol):
    async def __aenter__(self) -> ConnectedMCPClient: ...

    async def __aexit__(self, *args: object) -> None: ...

    async def list_tools(self, *, max_pages: int) -> list[object]: ...

    async def call_tool_mcp(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        timeout: float,
    ) -> CallToolResult: ...


@asynccontextmanager
async def _bounded_stdio_client(
    spec: MCPLaunchSpec,
    *,
    max_frame_bytes: int,
) -> AsyncIterator[
    tuple[
        anyio.abc.ObjectReceiveStream[SessionMessage | Exception],
        anyio.abc.ObjectSendStream[SessionMessage],
        _TransportState,
    ]
]:
    state = _TransportState()
    read_stream_writer, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](
        0
    )
    write_stream, write_stream_reader = anyio.create_memory_object_stream[SessionMessage](0)
    try:
        process = await anyio.open_process(
            list(spec.argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=dict(spec.environment),
        )
    except OSError:
        await read_stream.aclose()
        await write_stream.aclose()
        await read_stream_writer.aclose()
        await write_stream_reader.aclose()
        raise

    async def fail_transport(code: str) -> None:
        state.error_code = code
        with suppress(anyio.BrokenResourceError, anyio.ClosedResourceError):
            await read_stream_writer.send(MCPBoundaryError(code))
        with suppress(ProcessLookupError):
            process.terminate()

    async def emit_frame(frame: bytearray) -> bool:
        try:
            message = types.JSONRPCMessage.model_validate_json(frame)
        except ValidationError:
            await fail_transport("MCP_RESPONSE_FRAME_INVALID")
            return False
        try:
            await read_stream_writer.send(SessionMessage(message))
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            return False
        return True

    async def stdout_reader() -> None:
        assert process.stdout is not None
        buffer = bytearray()
        try:
            async with read_stream_writer:
                async for chunk in process.stdout:
                    cursor = 0
                    while cursor < len(chunk):
                        newline = chunk.find(b"\n", cursor)
                        segment_end = len(chunk) if newline < 0 else newline
                        segment = memoryview(chunk)[cursor:segment_end]
                        if len(buffer) + len(segment) > max_frame_bytes:
                            await fail_transport("MCP_RESPONSE_FRAME_TOO_LARGE")
                            return
                        buffer.extend(segment)
                        if newline < 0:
                            break
                        if not await emit_frame(buffer):
                            return
                        buffer.clear()
                        cursor = newline + 1
                if buffer:
                    await fail_transport("MCP_RESPONSE_FRAME_INCOMPLETE")
        except anyio.ClosedResourceError:
            pass

    async def stdin_writer() -> None:
        assert process.stdin is not None
        try:
            async with write_stream_reader:
                async for session_message in write_stream_reader:
                    encoded = session_message.message.model_dump_json(
                        by_alias=True,
                        exclude_none=True,
                    ).encode()
                    await process.stdin.send(encoded + b"\n")
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            pass

    async def stop_process() -> None:
        if process.stdin is not None:
            with suppress(anyio.BrokenResourceError, anyio.ClosedResourceError):
                await process.stdin.aclose()
        with anyio.move_on_after(2):
            await process.wait()
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
            with anyio.move_on_after(2):
                await process.wait()
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()

    async with process, anyio.create_task_group() as task_group:
        task_group.start_soon(stdout_reader)
        task_group.start_soon(stdin_writer)
        try:
            yield read_stream, write_stream, state
        finally:
            await stop_process()
            await read_stream.aclose()
            await write_stream.aclose()


class _StdioMCPClient:
    def __init__(self, spec: MCPLaunchSpec, limits: MCPClientLimits) -> None:
        self._spec = spec
        self._limits = limits
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._transport_state: _TransportState | None = None

    async def __aenter__(self) -> _StdioMCPClient:
        stack = AsyncExitStack()
        transport_state: _TransportState | None = None
        try:
            read_stream, write_stream, transport_state = await stack.enter_async_context(
                _bounded_stdio_client(
                    self._spec,
                    max_frame_bytes=self._limits.max_frame_bytes,
                )
            )
            session = await stack.enter_async_context(
                ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=self._limits.call_timeout_seconds),
                )
            )
            await session.initialize()
        except BaseException as error:
            await stack.aclose()
            if transport_state is not None and transport_state.error_code is not None:
                raise MCPBoundaryError(transport_state.error_code) from error
            raise
        self._stack = stack
        self._session = session
        self._transport_state = transport_state
        return self

    async def __aexit__(self, *_args: object) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._session = None
        self._transport_state = None

    async def list_tools(self, *, max_pages: int) -> list[object]:
        if self._session is None:
            raise MCPBoundaryError("MCP_SESSION_NOT_CONNECTED")
        if max_pages != 1:
            raise MCPBoundaryError("MCP_TOOL_PAGE_LIMIT_INVALID")
        try:
            result = await self._session.list_tools()
        except Exception as error:
            if self._transport_state is not None and self._transport_state.error_code is not None:
                raise MCPBoundaryError(self._transport_state.error_code) from error
            raise
        if result.nextCursor is not None:
            raise MCPBoundaryError("MCP_TOOL_SURFACE_PAGINATED")
        return list(result.tools)

    async def call_tool_mcp(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        timeout: float,
    ) -> CallToolResult:
        if self._session is None:
            raise MCPBoundaryError("MCP_SESSION_NOT_CONNECTED")
        try:
            return await self._session.call_tool(
                name,
                arguments,
                read_timeout_seconds=timedelta(seconds=timeout),
            )
        except Exception as error:
            if self._transport_state is not None and self._transport_state.error_code is not None:
                raise MCPBoundaryError(self._transport_state.error_code) from error
            raise


def _stdio_client_factory(spec: MCPLaunchSpec, limits: MCPClientLimits) -> ConnectedMCPClient:
    return _StdioMCPClient(spec, limits)


class AlpacaMCPResearchClient:
    def __init__(
        self,
        *,
        api_key: str,
        secret_key: str,
        limits: MCPClientLimits | None = None,
        client_factory: Callable[
            [MCPLaunchSpec, MCPClientLimits], ConnectedMCPClient
        ] = _stdio_client_factory,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._spec = build_launch_spec(api_key=api_key, secret_key=secret_key)
        self._limits = limits or MCPClientLimits()
        self._client_factory = client_factory
        self._now = now
        self._used = False
        self._closing = False
        self._connected: ConnectedMCPClient | None = None
        self._lifecycle_task: asyncio.Task[None] | None = None
        self._exit_requests: (
            asyncio.Queue[tuple[tuple[object, ...], asyncio.Future[None]]] | None
        ) = None
        self._exit_attempt: asyncio.Future[None] | None = None

    async def __aenter__(self) -> AlpacaMCPResearchClient:
        if self._used or self._connected is not None or self._lifecycle_task is not None:
            raise MCPBoundaryError("MCP_SESSION_ALREADY_CONNECTED")
        self._used = True
        connected = self._client_factory(self._spec, self._limits)
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[None] = loop.create_future()
        exit_requests: asyncio.Queue[tuple[tuple[object, ...], asyncio.Future[None]]] = (
            asyncio.Queue()
        )
        lifecycle = asyncio.create_task(
            self._run_connected_lifecycle(connected, ready, exit_requests)
        )
        self._lifecycle_task = lifecycle
        self._exit_requests = exit_requests
        try:
            await asyncio.shield(ready)
        except BaseException:
            self._closing = True
            with suppress(BaseException):
                await self._close_lifecycle((None, None, None))
            raise
        self._connected = connected
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._lifecycle_task is None:
            return
        self._closing = True
        await self._close_lifecycle(args)

    async def _close_lifecycle(self, args: tuple[object, ...]) -> None:
        task = self._lifecycle_task
        exit_requests = self._exit_requests
        if task is None or exit_requests is None:
            raise MCPBoundaryError("MCP_SESSION_LIFECYCLE_INVALID")
        attempt = self._exit_attempt
        if attempt is None:
            attempt = asyncio.get_running_loop().create_future()
            self._exit_attempt = attempt
            exit_requests.put_nowait((args, attempt))
        try:
            await asyncio.shield(attempt)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            internally_cancelled = current is None or current.cancelling() == 0
            if internally_cancelled and attempt.done() and self._exit_attempt is attempt:
                self._exit_attempt = None
            raise
        except BaseException:
            if self._exit_attempt is attempt:
                self._exit_attempt = None
            raise
        await asyncio.shield(task)
        self._connected = None
        self._lifecycle_task = None
        self._exit_requests = None
        self._exit_attempt = None

    async def _run_connected_lifecycle(
        self,
        connected: ConnectedMCPClient,
        ready: asyncio.Future[None],
        exit_requests: asyncio.Queue[tuple[tuple[object, ...], asyncio.Future[None]]],
    ) -> None:
        entered = False
        try:
            await connected.__aenter__()
            entered = True
            tools = await connected.list_tools(max_pages=self._limits.max_tool_pages)
            validate_tool_surface(str(getattr(tool, "name", "")) for tool in tools)
            ready.set_result(None)
        except BaseException as error:
            if not ready.done():
                ready.set_exception(error)
        while True:
            exit_args, outcome = await exit_requests.get()
            if not entered:
                outcome.set_result(None)
                return
            try:
                await connected.__aexit__(*exit_args)
            except BaseException as error:
                outcome.set_exception(error)
            else:
                outcome.set_result(None)
                return

    async def call(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> MCPResearchResult:
        if self._connected is None or self._closing:
            raise MCPBoundaryError("MCP_SESSION_NOT_CONNECTED")
        call = prepare_research_call(tool_name, arguments)
        materialized_arguments = dict(call.arguments)
        started_at = self._now()
        response = await self._connected.call_tool_mcp(
            call.tool_name,
            materialized_arguments,
            timeout=self._limits.call_timeout_seconds,
        )
        completed_at = self._now()
        if response.isError:
            raise MCPBoundaryError("MCP_TOOL_ERROR")
        data = response.structuredContent
        if data is None:
            raise MCPBoundaryError("MCP_STRUCTURED_RESULT_REQUIRED")
        encoded = _canonical_json(data)
        if len(encoded) > self._limits.max_result_bytes:
            raise MCPBoundaryError("MCP_RESULT_TOO_LARGE")
        return MCPResearchResult(
            tool_name=call.tool_name,
            data=data,
            audit=MCPResearchAudit(
                tool_name=call.tool_name,
                argument_hash=_hash_json(materialized_arguments),
                started_at=started_at,
                completed_at=completed_at,
                result_summary_hash=hashlib.sha256(encoded).hexdigest(),
                quality="COMPLETE",
            ),
        )


def build_launch_spec(*, api_key: str, secret_key: str) -> MCPLaunchSpec:
    if not api_key or not secret_key:
        raise MCPBoundaryError("MCP_CREDENTIAL_MISSING")
    executable = _resolve_mcp_executable()
    return MCPLaunchSpec(
        argv=(str(executable),),
        environment=MappingProxyType(
            {
                "ALPACA_API_KEY": api_key,
                "ALPACA_SECRET_KEY": secret_key,
                "ALPACA_PAPER_TRADE": "true",
                "ALPACA_TOOLSETS": _TOOLSETS,
                "LC_ALL": "C.UTF-8",
            }
        ),
    )


def _resolve_mcp_executable() -> Path:
    sibling = Path(sys.executable).absolute().with_name("alpaca-mcp-server")
    if sibling.exists() or sibling.is_symlink():
        return _validated_executable(sibling)
    installed = which("alpaca-mcp-server")
    if installed is None:
        raise MCPBoundaryError("MCP_EXECUTABLE_MISSING")
    return _validated_executable(Path(installed).absolute())


def _validated_executable(path: Path) -> Path:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise MCPBoundaryError("MCP_EXECUTABLE_INVALID") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or not stat.S_IMODE(metadata.st_mode) & 0o111
        or not os.access(path, os.X_OK)
    ):
        raise MCPBoundaryError("MCP_EXECUTABLE_INVALID")
    return path


def validate_tool_surface(tool_names: Iterable[str]) -> None:
    if frozenset(tool_names) != EXPOSED_TOOL_SURFACE:
        raise MCPBoundaryError("MCP_TOOL_SURFACE_MISMATCH")


def prepare_research_call(tool_name: str, arguments: Mapping[str, object]) -> MCPResearchCall:
    if tool_name not in RUNTIME_RESEARCH_ALLOWLIST:
        raise MCPBoundaryError("MCP_TOOL_NOT_RUNTIME_ALLOWED")
    bounded_arguments = dict(arguments)
    if tool_name in _INDICATIVE_TOOLS:
        supplied_feed = bounded_arguments.get("feed")
        if supplied_feed not in {None, "indicative"}:
            raise MCPBoundaryError("MCP_OPTION_FEED_FORBIDDEN")
        bounded_arguments["feed"] = "indicative"
    return MCPResearchCall(
        tool_name=tool_name,
        arguments=MappingProxyType(bounded_arguments),
    )


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as exc:
        raise MCPBoundaryError("MCP_RESULT_NOT_JSON") from exc


def _hash_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()

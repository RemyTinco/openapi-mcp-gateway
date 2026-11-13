"""The MCP server: one namespaced provider and HTTP client per backend.

Only operations in each backend's filtered document become tools; nothing else
(no generic HTTP tool, resources or prompts) is registered.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
import sys
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

import anyio
import anyio.abc
import fastmcp
from fastmcp import FastMCP
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from openapi_mcp_gateway.config.loader import load_gateway_config
from openapi_mcp_gateway.exceptions import GatewayError, McpInitError
from openapi_mcp_gateway.http.client import GatewayToolError, build_client, current_call
from openapi_mcp_gateway.logging import configure_logging, log_event
from openapi_mcp_gateway.mcp.factory import build_provider
from openapi_mcp_gateway.openapi.operations import path_parameter_names
from openapi_mcp_gateway.pipeline import PreparedBackend, prepare_checked

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Coroutine, Mapping, Sequence
    from pathlib import Path

    import httpx2
    import mcp_types as mt
    from fastmcp.tools import ToolResult

    from openapi_mcp_gateway.config.models import GatewayConfig

logger = logging.getLogger(__name__)

# Library loggers that would otherwise log URLs, headers or bodies at DEBUG/INFO.
_QUIET_LOGGERS = ("httpx2", "httpcore2", "fastmcp", "mcp")


class ToolLoggingMiddleware(Middleware):
    """Logs ``tool.executed`` for every call of a known tool, failed or not."""

    def __init__(self, operations: Mapping[str, tuple[str, str]]) -> None:
        self.operations = operations  # tool name -> (backend, operationId)

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Log the outcome of the call, whatever it is."""
        known = self.operations.get(context.message.name)
        if known is None:
            return await call_next(context)
        backend, operation = known
        call: dict[str, Any] = {}
        token = current_call.set(call)
        error: str | None = None
        start = time.perf_counter()
        try:
            return await call_next(context)
        except asyncio.CancelledError:
            error = "cancelled"
            raise
        except Exception as exc:
            error = exc.payload.get("error") if isinstance(exc, GatewayToolError) else None
            error = error or "tool_failed"
            raise
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000)
            current_call.reset(token)
            fields: dict[str, object] = {
                "backend": backend,
                "operation": operation,
                "method": call.get("method"),
                "status": call.get("status"),
                "duration_ms": duration_ms,
            }
            if error is not None:
                fields["error"] = error
            log_event(logger, logging.INFO, "tool.executed", **fields)


_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
# Stands for "some argument value" in a template segment; no schema literal contains it.
_ANY_VALUE = "\x00"


def _segments(path: str) -> list[str]:
    # "/a/b/", "/a/b" and "//a//b" are the same location: drop empty segments.
    return [segment for segment in path.split("/") if segment]


def _template_matches(template: str, segments: Sequence[str]) -> bool:
    """Whether concrete path ``segments`` are an instance of the OpenAPI ``template``."""
    parts = _segments(template)
    if len(parts) != len(segments):
        return False
    for part, segment in zip(parts, segments, strict=True):
        pattern = ".+".join(re.escape(literal) for literal in _PLACEHOLDER.split(part)[::2])
        if re.fullmatch(pattern, segment, re.DOTALL) is None:
            return False
    return True


def _fill(template: str, values: Mapping[str, str]) -> list[str]:
    """``template``'s segments with each ``{name}`` replaced by its value."""
    return [
        _PLACEHOLDER.sub(lambda m: values.get(m.group(1), _ANY_VALUE), part)
        for part in _segments(template)
    ]


def _path_text(value: object) -> str | None:
    """Return the text FastMCP writes into the URL for a scalar; ``None`` for anything else."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int | float):
        return str(value)
    return None


@dataclass(frozen=True)
class PathRule:
    """What the path arguments of one tool are checked against."""

    backend: str
    template: str  # the tool's own OpenAPI path
    arguments: Mapping[str, str]  # path parameter name -> tool argument name
    other_paths: tuple[str, ...]  # every other path of the unfiltered schema

    def other_path_reached(self, values: Mapping[str, str]) -> bool:
        """Tell whether these values make the request an instance of another schema path.

        ``/items/{id}`` with ``id="export"`` is ``/items/export``, a different and
        possibly hidden operation. A path that the template overlaps whatever the
        values are (``/{collection}/{key}``) is the schema's own ambiguity and is
        not counted, or the tool could never be called.
        """
        whatever = _fill(self.template, {})
        filled = _fill(self.template, values)
        # A value's "/" is sent as %2F; also check the path a backend sees if it decodes it.
        candidates = (filled, _segments("/".join(filled)))
        return any(
            _template_matches(other, candidate)
            for other in self.other_paths
            if not _template_matches(other, whatever)
            for candidate in candidates
        )


class PathArgumentMiddleware(Middleware):
    """Refuses a call whose path arguments would address anything but the tool's own path.

    The exposure filter decides on path *templates*, but a request carries a concrete
    path. Three kinds of value open a gap between the two, and each is refused
    before anything is sent:

    - missing, null, empty or whitespace: ``/items/{id}`` becomes ``/items/``, which
      many backends serve as the parent or collection endpoint;
    - a list or an object: FastMCP joins the items with commas, so an empty one is
      the empty case again;
    - a value that makes the path an instance of another path in the backend's
      schema (``id="export"`` reaching ``/items/export``).
    """

    def __init__(self, rules: Mapping[str, PathRule]) -> None:
        self.rules = rules  # tool name -> rule

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Check the path arguments, then let the call through."""
        rule = self.rules.get(context.message.name)
        if rule is not None:
            arguments = context.message.arguments or {}
            values: dict[str, str] = {}
            for parameter, name in rule.arguments.items():
                value = arguments.get(name)
                if value is None or (isinstance(value, str) and not value.strip()):
                    raise self._refused(
                        rule, f"path parameter {name!r} must not be missing or empty"
                    )
                text = _path_text(value)
                if text is None:
                    raise self._refused(
                        rule, f"path parameter {name!r} must be a string, a number or a boolean"
                    )
                values[parameter] = text
            if rule.other_path_reached(values):
                names = ", ".join(repr(name) for name in rule.arguments.values())
                raise self._refused(
                    rule,
                    f"the value given for {names} would address a different path of this "
                    "backend than the one this tool is for",
                )
        return await call_next(context)

    @staticmethod
    def _refused(rule: PathRule, reason: str) -> GatewayToolError:
        return GatewayToolError(
            {
                "error": "invalid_argument",
                "backend": rule.backend,
                "status": None,
                "message": f"{reason}; nothing was sent",
            }
        )


def _run_coroutine(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run ``coro`` to completion on a private loop, even if a loop is running here."""
    result: dict[str, Any] = {}

    def target() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the calling thread
            result["error"] = exc

    thread = threading.Thread(target=target, name="openapi-mcp-gateway-startup")
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result["value"]


def _check_tool_names(server: FastMCP[Any], expected: set[str]) -> dict[str, dict[str, Any]]:
    """Tool name -> input schema, once the listed names are exactly the expected ones."""
    tools = {tool.name: tool.parameters for tool in _run_coroutine(server.list_tools())}
    listed = set(tools)
    if listed == expected:
        return tools
    problems = []
    if missing := sorted(expected - listed):
        problems.append(f"missing {', '.join(missing)}")
    if unexpected := sorted(listed - expected):
        problems.append(f"unexpected {', '.join(unexpected)}")
    raise McpInitError(
        "FastMCP registered tool names that differ from the configured ones ("
        + "; ".join(problems)
        + "); check this FastMCP version's tool naming"
    )


class _Lifecycle:
    """Owns the backend HTTP clients; closes them once, when the session ends."""

    def __init__(self, clients: Sequence[httpx2.AsyncClient]) -> None:
        self.clients = clients
        self.started = False
        self._closed = False

    @asynccontextmanager
    async def lifespan(self, _server: FastMCP[Any]) -> AsyncIterator[None]:
        """Mark the session started; close the clients when it ends."""
        self.started = True
        try:
            yield
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        for client in self.clients:
            await client.aclose()
        log_event(logger, logging.INFO, "gateway.stopped")


def _quiet_library_loggers() -> None:
    """Cap library loggers at WARNING and route them through the gateway's handler.

    Importing FastMCP gives the ``fastmcp`` logger two Rich handlers and
    ``propagate=False``, which would bypass the gateway's format (breaking
    ``logging.format: json``) and print tracebacks. Remove them and let records
    propagate to the root handler. FastMCP only reconfigures its logger when a
    ``log_level`` is passed to ``run_*`` (``temporary_log_level``); the gateway
    never passes one, and disabling ``log_enabled`` makes any such call a no-op.
    """
    fastmcp.settings.log_enabled = False
    for name in _QUIET_LOGGERS:
        library = logging.getLogger(name)
        library.setLevel(logging.WARNING)
        for handler in list(library.handlers):
            library.removeHandler(handler)
        library.propagate = True


def _other_paths(all_paths: Sequence[str], own: str) -> tuple[str, ...]:
    """Every schema path but ``own``, also in percent-decoded form.

    A schema may spell a literal as ``/items/my%20export``; the argument ``"my export"``
    is sent as those same bytes, so it has to match the decoded spelling.
    """
    others = [t for t in all_paths if t != own]
    return tuple(dict.fromkeys(others + [unquote(t) for t in others]))


def _argument_name(parameter: str, input_schema: Mapping[str, Any]) -> str:
    # FastMCP renames a path parameter to ``<name>__path`` when another argument
    # (a query parameter or body property) has the same name.
    suffixed = f"{parameter}__path"
    return suffixed if suffixed in (input_schema.get("properties") or {}) else parameter


def _assemble(
    cfg: GatewayConfig,
    prepared: Sequence[PreparedBackend],
    env: Mapping[str, str],
    inner_transports: Mapping[str, httpx2.AsyncBaseTransport],
) -> tuple[FastMCP[Any], _Lifecycle]:
    _quiet_library_loggers()
    operations = {
        name: (p.config.name, operation_id)
        for p in prepared
        for operation_id, name in p.tool_names.items()
    }
    path_rules: dict[str, PathRule] = {}
    clients: list[httpx2.AsyncClient] = []
    lifecycle = _Lifecycle(clients)
    try:
        # Inside the try: nothing at start-up may escape as a raw traceback.
        clients.extend(
            build_client(p.config, env, inner_transport=inner_transports.get(p.config.name))
            for p in prepared
        )
        server: FastMCP[Any] = FastMCP(
            cfg.server_name,
            lifespan=lifecycle.lifespan,
            # The first middleware is the outermost: a refused call is still logged.
            middleware=[ToolLoggingMiddleware(operations), PathArgumentMiddleware(path_rules)],
        )
        for backend, client in zip(prepared, clients, strict=True):
            server.add_provider(build_provider(backend, client), namespace=backend.config.name)
            log_event(logger, logging.INFO, "backend.loaded", name=backend.config.name)
        schemas = _check_tool_names(server, set(operations))
        for backend in prepared:
            for op in backend.operations:
                if op.operation_id is None:
                    continue
                name = backend.tool_names[op.operation_id]
                path_rules[name] = PathRule(
                    backend=backend.config.name,
                    template=op.path,
                    arguments={
                        parameter: _argument_name(parameter, schemas[name])
                        for parameter in path_parameter_names(backend.document, op)
                    },
                    other_paths=_other_paths(backend.all_paths, op.path),
                )
    except GatewayError:
        raise
    except Exception as exc:
        msg = f"FastMCP could not build the server: {exc}"
        raise McpInitError(msg) from exc
    log_event(logger, logging.INFO, "tools.registered", count=len(operations))
    return server, lifecycle


def build_server(
    cfg: GatewayConfig,
    prepared: Sequence[PreparedBackend],
    env: Mapping[str, str],
    *,
    inner_transports: Mapping[str, httpx2.AsyncBaseTransport] | None = None,
) -> FastMCP[Any]:
    """Assemble the MCP server; raises ``McpInitError`` if FastMCP cannot represent it."""
    server, _ = _assemble(cfg, prepared, env, inner_transports or {})
    return server


# --- stdio ----------------------------------------------------------------------

_STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


def _leaf(exc: BaseException) -> BaseException:
    """Return the single error inside (nested) exception groups, else ``exc`` itself."""
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    return exc


async def _stop_on_signal(stopped: anyio.Event, *, task_status: anyio.abc.TaskStatus[None]) -> None:
    with anyio.open_signal_receiver(*_STOP_SIGNALS) as signals:
        task_status.started()
        async for signum in signals:
            log_event(logger, logging.INFO, "gateway.stopping", signal=signal.Signals(signum).name)
            stopped.set()
            return


def _exit_now() -> None:
    """Flush what the process has written and end it with exit code 0.

    After a stop signal the event loop cannot be unwound: the MCP stdio transport
    reads stdin in a worker thread, and that blocking read is not cancellable. It
    returns only when the client sends another line or closes stdin, which a client
    that has just sent SIGTERM may never do. Waiting for it would hang the process.
    """
    logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(OSError, ValueError):
            stream.flush()
    os._exit(0)


async def _run_stdio(server: FastMCP[Any], lifecycle: _Lifecycle) -> None:
    stopped = anyio.Event()  # a stop signal arrived
    finished = anyio.Event()  # the stop signal arrived or the transport ended (stdin closed)

    async def run_transport() -> None:
        # run_stdio_async only touches logging when given log_level; keep it unset so
        # the library loggers stay capped at WARNING and on the gateway's handler.
        await server.run_async(transport="stdio", show_banner=False)
        finished.set()

    async def relay_stop() -> None:
        await stopped.wait()
        finished.set()

    try:
        async with anyio.create_task_group() as tg:
            # Handlers go in before ``gateway.ready``: a SIGTERM from then on is a clean stop.
            await tg.start(_stop_on_signal, stopped)
            log_event(logger, logging.INFO, "gateway.ready", transport="stdio")
            tg.start_soon(relay_stop)
            tg.start_soon(run_transport)
            await finished.wait()
            if stopped.is_set():
                with anyio.CancelScope(shield=True):
                    await lifecycle.aclose()
                _exit_now()
            tg.cancel_scope.cancel()  # stdin closed: stop listening for signals
    finally:
        with anyio.CancelScope(shield=True):
            await lifecycle.aclose()  # no-op once the lifespan closed the clients


def serve(config_path: Path, *, env: Mapping[str, str]) -> None:
    """Load, validate and prepare the gateway, then serve MCP on stdio until stopped.

    Config, schema and credential errors are reported together before anything
    starts; a failure building or starting FastMCP is an ``McpInitError``; any
    other failure while serving is a ``GatewayError`` (exit code 1). SIGTERM,
    SIGINT and the end of stdin all stop the server normally.
    """
    cfg = load_gateway_config(config_path)
    configure_logging(cfg.log_level, cfg.log_format)
    log_event(logger, logging.INFO, "gateway.starting")
    prepared = prepare_checked(cfg, env)
    for backend in prepared:
        for warning in backend.warnings:
            log_event(
                logger, logging.WARNING, "backend.warning", name=backend.config.name, detail=warning
            )
    server, lifecycle = _assemble(cfg, prepared, env, {})
    try:
        anyio.run(_run_stdio, server, lifecycle)
    except Exception as exc:  # noqa: BLE001 - every failure gets an exit code
        leaf = _leaf(exc)
        if isinstance(leaf, GatewayError):
            raise leaf from None
        if not lifecycle.started:
            msg = f"FastMCP could not start: {leaf}"
            raise McpInitError(msg) from leaf
        msg = f"unexpected error while serving: {leaf}"
        raise GatewayError(msg) from leaf

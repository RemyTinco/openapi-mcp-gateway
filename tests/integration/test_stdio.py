"""End to end over stdio: the gateway runs as a subprocess, as Claude Code launches it."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

import mcp_types as mt
import pytest
import yaml
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError
from stub_backend import StubBackend, stub_backend
from typer.testing import CliRunner

from openapi_mcp_gateway.cli import app
from openapi_mcp_gateway.mcp import gateway as gateway_module

if TYPE_CHECKING:
    from collections.abc import Iterator

RUNTIME = Path(__file__).parents[1] / "fixtures" / "gateways" / "runtime"
SECRETS = {"CATALOG_API_TOKEN": "app-token", "MESSAGING_API_KEY": "pay-key"}
SERVE = ["-m", "openapi_mcp_gateway", "serve", "--config"]
STARTUP_TIMEOUT = 30.0  # generous: a cold interpreter imports FastMCP
EXIT_TIMEOUT = 5.0


def child_env(secrets: dict[str, str] = SECRETS) -> dict[str, str]:
    """Build the child's whole environment: PATH plus the given secrets, nothing inherited."""
    return {"PATH": os.environ.get("PATH", ""), **secrets}


@pytest.fixture
def stub() -> Iterator[StubBackend]:
    with stub_backend() as backend:
        yield backend


def write_config(
    tmp_path: Path,
    stub: StubBackend,
    *,
    log_level: str | None = None,
    log_format: str | None = None,
) -> Path:
    """Copy the runtime fixture with every backend pointed at the stub."""
    root = tmp_path / "gateway"
    shutil.copytree(RUNTIME, root)
    for backend_yaml in (root / "backends").glob("*/backend.yaml"):
        data = yaml.safe_load(backend_yaml.read_text())
        data["base_url"] = stub.url
        backend_yaml.write_text(yaml.safe_dump(data))
    logging_block = {
        key: value for key, value in (("level", log_level), ("format", log_format)) if value
    }
    if logging_block:
        gateway_yaml = root / "gateway.yaml"
        data = yaml.safe_load(gateway_yaml.read_text())
        data["logging"] = logging_block
        gateway_yaml.write_text(yaml.safe_dump(data))
    return root / "gateway.yaml"


@pytest.fixture
def config(tmp_path: Path, stub: StubBackend) -> Path:
    return write_config(tmp_path, stub)


def stdio_client(config: Path, log_file: Path) -> Client[Any]:
    transport = StdioTransport(
        command=sys.executable,
        args=[*SERVE, str(config)],
        env=child_env(),
        cwd=str(config.parent),
        keep_alive=False,
        log_file=log_file,
    )
    return Client(transport, timeout=STARTUP_TIMEOUT, init_timeout=STARTUP_TIMEOUT)


# --- through fastmcp.Client ----------------------------------------------------


async def test_discover_and_invoke_over_stdio(
    config: Path, stub: StubBackend, tmp_path: Path
) -> None:
    async with stdio_client(config, tmp_path / "stderr.log") as c:
        assert "catalog_items_list" in [t.name for t in await c.list_tools()]
        r = await c.call_tool("catalog_items_retrieve", {"id": 5})
    assert r.structured_content is not None
    assert r.structured_content["path"] == "/items/5"
    assert r.structured_content["headers"]["authorization"] == "Bearer app-token"
    assert stub.paths == ["/items/5"]
    stderr = (tmp_path / "stderr.log").read_text()
    assert "tool.executed backend=catalog operation=items_retrieve" in stderr


async def test_upstream_404_reaches_client_as_tool_error(config: Path, tmp_path: Path) -> None:
    async with stdio_client(config, tmp_path / "stderr.log") as c:
        with pytest.raises(ToolError) as excinfo:
            await c.call_tool("messaging_items_retrieve", {"id": 404})
    payload = json.loads(str(excinfo.value))
    assert payload["error"] == "not_found"
    assert payload["status"] == 404
    assert payload["backend"] == "messaging"


async def test_excluded_operation_is_absent_and_cannot_be_called(
    config: Path, stub: StubBackend, tmp_path: Path
) -> None:
    async with stdio_client(config, tmp_path / "stderr.log") as c:
        names = [t.name for t in await c.list_tools()]
        with pytest.raises(ToolError):
            await c.call_tool("catalog_items_export", {})
    assert "catalog_items_export" not in names
    assert "catalog_items_create" not in names  # read_only hides writes too
    assert stub.requests == []


# --- raw subprocess --------------------------------------------------------------


class GatewayProcess:
    """A ``serve`` subprocess whose stdout and stderr are drained by reader threads."""

    def __init__(self, config: Path, env: dict[str, str]) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, *SERVE, str(config)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=config.parent,
        )
        self.stdout_lines: list[str] = []
        self.stderr_lines: list[str] = []
        self._stdout_queue: queue.Queue[str] = queue.Queue()
        self._stderr_seen = threading.Condition()
        assert self.proc.stdout is not None
        assert self.proc.stderr is not None
        self._readers = [
            threading.Thread(target=self._drain_stdout, args=(self.proc.stdout,), daemon=True),
            threading.Thread(target=self._drain_stderr, args=(self.proc.stderr,), daemon=True),
        ]
        for reader in self._readers:
            reader.start()

    def _drain_stdout(self, stream: IO[str]) -> None:
        for line in stream:
            self.stdout_lines.append(line)
            self._stdout_queue.put(line)

    def _drain_stderr(self, stream: IO[str]) -> None:
        for line in stream:
            with self._stderr_seen:
                self.stderr_lines.append(line)
                self._stderr_seen.notify_all()
        with self._stderr_seen:
            self._stderr_seen.notify_all()

    @property
    def stderr(self) -> str:
        return "".join(self.stderr_lines)

    @property
    def stdout(self) -> str:
        return "".join(self.stdout_lines)

    def wait_for_stderr(self, text: str, timeout: float = STARTUP_TIMEOUT) -> None:
        with self._stderr_seen:
            found = self._stderr_seen.wait_for(
                lambda: text in self.stderr or not self._readers[1].is_alive(), timeout
            )
        assert found, f"{text!r} not on stderr:\n{self.stderr}"
        assert text in self.stderr, f"{text!r} not on stderr:\n{self.stderr}"

    def send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def receive(self, timeout: float = STARTUP_TIMEOUT) -> dict[str, Any]:
        message: dict[str, Any] = json.loads(self._stdout_queue.get(timeout=timeout))
        return message

    def request(self, request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        response = self.receive()
        assert response["id"] == request_id, response
        return response

    def initialize(self) -> None:
        response = self.request(
            1,
            "initialize",
            {
                "protocolVersion": mt.LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        )
        assert "result" in response, response
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def close_stdin(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()

    def wait(self, timeout: float = EXIT_TIMEOUT) -> int:
        code = self.proc.wait(timeout=timeout)
        for reader in self._readers:
            reader.join(timeout=EXIT_TIMEOUT)
        return code

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=EXIT_TIMEOUT)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if stream is not None and not stream.closed:
                with contextlib.suppress(OSError):  # stdin pipe already broken
                    stream.close()
        for reader in self._readers:
            reader.join(timeout=EXIT_TIMEOUT)


@contextmanager
def launch(config: Path, env: dict[str, str] | None = None) -> Iterator[GatewayProcess]:
    process = GatewayProcess(config, child_env() if env is None else env)
    try:
        yield process
    finally:
        process.stop()


def test_stdout_is_protocol_only(tmp_path: Path, stub: StubBackend) -> None:
    config = write_config(tmp_path, stub, log_level="DEBUG")  # most logging there is
    with launch(config) as gw:
        gw.initialize()
        assert "tools" in gw.request(2, "tools/list", {})["result"]
        gw.request(3, "tools/call", {"name": "catalog_items_retrieve", "arguments": {"id": 5}})
        gw.request(4, "tools/call", {"name": "messaging_items_retrieve", "arguments": {"id": 404}})
        gw.close_stdin()
        assert gw.wait() == 0
        stdout, stderr = gw.stdout, gw.stderr
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert len(lines) == 4
    for line in lines:
        assert json.loads(line)["jsonrpc"] == "2.0"
    assert "tool.executed" in stderr  # the logs went to stderr instead
    assert "app-token" not in stderr
    assert "pay-key" not in stderr
    assert "Not found." not in stderr  # no upstream body, even at DEBUG


def test_json_logging_holds_for_failing_calls_while_serving(
    tmp_path: Path, stub: StubBackend
) -> None:
    config = write_config(tmp_path, stub, log_format="json")
    with launch(config) as gw:
        gw.initialize()
        for i, (name, arguments) in enumerate(
            [
                ("messaging_items_retrieve", {"id": 404}),
                ("catalog_items_retrieve", {"id": ".."}),
                ("catalog_items_retrieve", {"id": ""}),
            ],
            start=2,
        ):
            response = gw.request(i, "tools/call", {"name": name, "arguments": arguments})
            assert response["result"]["isError"] is True, response
        gw.close_stdin()
        assert gw.wait() == 0
        stderr = gw.stderr
    records = [json.loads(line) for line in stderr.splitlines() if line.strip()]
    assert all(isinstance(r, dict) for r in records)
    events = [r["event"] for r in records]
    assert events.count("tool.executed") == 3
    assert any(e.startswith("Error calling tool") for e in events), events
    assert "Traceback" not in stderr
    assert stub.paths == ["/items/404"]


def test_startup_log_lines_on_stderr(config: Path) -> None:
    with launch(config) as gw:
        gw.wait_for_stderr("gateway.ready transport=stdio")
        stderr = gw.stderr
    for line in (
        "INFO gateway.starting",
        "INFO backend.loaded name=catalog",
        "INFO backend.loaded name=messaging",
        "INFO tools.registered count=5",
        "INFO gateway.ready transport=stdio",
    ):
        assert line in stderr
    assert stderr.index("gateway.starting") < stderr.index("gateway.ready")


# The transport reads stdin in a thread that cannot be interrupted, so a stop signal must
# end the process without waiting for the client's next line. The pause lets the server
# go idle first: without it the signal races the ``initialized`` notification, and a
# gateway that waits on stdin would still pass.
IDLE_PAUSE = 0.3


@pytest.mark.parametrize("how", ["sigterm", "sigint", "stdin_close"])
@pytest.mark.parametrize("session", ["initialized", "not_initialized"])
def test_exits_cleanly_on_sigterm_and_on_stdin_close(config: Path, how: str, session: str) -> None:
    with launch(config) as gw:
        gw.wait_for_stderr("gateway.ready transport=stdio")
        if session == "initialized":
            gw.initialize()
        time.sleep(IDLE_PAUSE)
        if how == "stdin_close":
            gw.close_stdin()
        else:
            gw.proc.send_signal(signal.SIGTERM if how == "sigterm" else signal.SIGINT)
        assert gw.wait() == 0, gw.stderr
        stdout, stderr = gw.stdout, gw.stderr
    assert "Traceback" not in stderr
    assert "error:" not in stderr
    assert "INFO gateway.stopped" in stderr  # logged once the HTTP clients are closed
    assert stderr.count("INFO gateway.stopped") == 1
    # just the initialize response, when there was one
    assert len(stdout.splitlines()) == (1 if session == "initialized" else 0)


@pytest.mark.parametrize(
    ("breakage", "code"), [("bad_config", 2), ("bad_schema", 3), ("missing_env", 4)]
)
def test_serve_startup_failure_exit_codes(
    tmp_path: Path, stub: StubBackend, breakage: str, code: int
) -> None:
    config = write_config(tmp_path, stub)
    env = child_env()
    if breakage == "bad_config":
        config.write_text("version: 2\n")
    elif breakage == "bad_schema":
        (config.parent / "backends" / "catalog" / "openapi.yaml").write_text("openapi: 9.0.0\n")
    else:
        env = child_env({"MESSAGING_API_KEY": "pay-key"})
    result = subprocess.run(
        [sys.executable, *SERVE, str(config)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        timeout=STARTUP_TIMEOUT,
        check=False,
    )
    assert result.returncode == code, result.stderr
    assert result.stdout == ""
    assert "Traceback" not in result.stderr
    assert any(line.startswith("error: ") for line in result.stderr.splitlines())
    assert "gateway.ready" not in result.stderr


def test_serve_reports_schema_and_credential_errors_together(
    tmp_path: Path, stub: StubBackend
) -> None:
    config = write_config(tmp_path, stub)
    (config.parent / "backends" / "catalog" / "openapi.yaml").write_text("openapi: 9.0.0\n")
    result = subprocess.run(
        [sys.executable, *SERVE, str(config)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=child_env({}),
        timeout=STARTUP_TIMEOUT,
        check=False,
    )
    errors = [line for line in result.stderr.splitlines() if line.startswith("error: ")]
    assert result.returncode == 3
    assert result.stdout == ""
    assert len(errors) == 3  # schema + two missing variables
    assert "CATALOG_API_TOKEN" in result.stderr
    assert "MESSAGING_API_KEY" in result.stderr


# --- in process: failures FastMCP itself raises ------------------------------------


@pytest.fixture
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def serve_in_process(config: Path, *flags: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(app, [*flags, "serve", "--config", str(config)], env=SECRETS)
    return result.exit_code, result.stdout, result.stderr


@pytest.mark.usefixtures("restore_logging")
def test_build_failure_exits_5(config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        msg = "provider exploded"
        raise RuntimeError(msg)

    monkeypatch.setattr(gateway_module, "build_provider", broken)
    code, out, err = serve_in_process(config)
    assert code == 5
    assert out == ""
    assert "error: FastMCP could not build the server: provider exploded" in err
    assert "Traceback" not in err


@pytest.mark.usefixtures("restore_logging")
def test_non_ascii_secret_is_a_credentials_error(config: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["serve", "--config", str(config)],
        env={**SECRETS, "CATALOG_API_TOKEN": "sécret-Ω"},
    )
    assert result.exit_code == 4
    assert result.stdout == ""
    assert "environment variable CATALOG_API_TOKEN" in result.stderr
    assert "sécret" not in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.usefixtures("restore_logging")
def test_client_construction_failure_exits_5(config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        msg = "ascii"
        raise UnicodeEncodeError(msg, "x", 0, 1, "ordinal not in range(128)")

    monkeypatch.setattr(gateway_module, "build_client", broken)
    code, out, err = serve_in_process(config)
    assert code == 5
    assert out == ""
    assert "error: FastMCP could not build the server:" in err
    assert "Traceback" not in err


@pytest.mark.usefixtures("restore_logging")
def test_fastmcp_startup_failure_exits_5(config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def cannot_start(self: FastMCP[Any], **kwargs: Any) -> None:
        msg = "stdio unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(FastMCP, "run_stdio_async", cannot_start)
    code, out, err = serve_in_process(config)
    assert code == 5
    assert out == ""
    assert "error: FastMCP could not start: stdio unavailable" in err
    assert "Traceback" not in err


async def _fail_while_serving(self: FastMCP[Any], **kwargs: Any) -> None:
    async with self._lifespan_manager():
        msg = "session crashed"
        raise RuntimeError(msg)


@pytest.mark.usefixtures("restore_logging")
def test_unexpected_error_while_serving_exits_1(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FastMCP, "run_stdio_async", _fail_while_serving)
    code, out, err = serve_in_process(config)
    errors = [line for line in err.splitlines() if line.startswith("error: ")]
    assert code == 1
    assert out == ""
    assert errors == ["error: unexpected error while serving: session crashed"]
    assert "Traceback" not in err
    assert "INFO gateway.stopped" in err


@pytest.mark.usefixtures("restore_logging")
def test_unexpected_error_traceback_only_with_debug(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FastMCP, "run_stdio_async", _fail_while_serving)
    code, out, err = serve_in_process(config, "--debug")
    assert code == 1
    assert out == ""
    assert "Traceback" in err
    assert "session crashed" in err

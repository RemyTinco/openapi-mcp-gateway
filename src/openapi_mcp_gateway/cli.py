"""Command-line interface; `main()` is the single place mapping errors to exit codes."""

from __future__ import annotations

import functools
import os
import sys
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from openapi_mcp_gateway import __version__
from openapi_mcp_gateway.config.loader import load_gateway_config
from openapi_mcp_gateway.config.validator import validate_config
from openapi_mcp_gateway.exceptions import GatewayError, MultipleErrors
from openapi_mcp_gateway.logging import configure_logging
from openapi_mcp_gateway.pipeline import PreparedBackend, prepare_all, prepare_checked

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from openapi_mcp_gateway.config.models import GatewayConfig

# Nothing FastMCP-related may be imported at module level: the offline commands
# below must stay light and work without the server stack.

ConfigOption = Annotated[
    Path,
    typer.Option("--config", help="Path to gateway.yaml.", show_default="./gateway.yaml"),
]
DEFAULT_CONFIG = Path("gateway.yaml")

app = typer.Typer(
    name="openapi-mcp-gateway",
    help="Turn local OpenAPI files into MCP tools.",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)


class _State:
    debug = False


@app.callback()
def _root(
    *,
    debug: Annotated[bool, typer.Option("--debug", help="Show tracebacks on errors.")] = False,
) -> None:
    _State.debug = debug


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


def _flatten(error: GatewayError) -> list[GatewayError]:
    if isinstance(error, MultipleErrors):
        return [leaf for inner in error.errors for leaf in _flatten(inner)]
    return [error]


def _report(exc: GatewayError) -> None:
    """Print one ``error:`` line per leaf error (traceback too under --debug)."""
    if _State.debug:
        traceback.print_exc()
    for leaf in _flatten(exc) or [exc]:
        sys.stderr.write(f"error: {leaf}\n")


def _handled[**P](command: Callable[P, None]) -> Callable[P, None]:
    """Report a GatewayError and exit with its code, so every entry point agrees."""

    @functools.wraps(command)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            command(*args, **kwargs)
        except GatewayError as exc:
            _report(exc)
            raise typer.Exit(exc.exit_code) from None

    return wrapper


def _load(config: Path) -> GatewayConfig:
    cfg = load_gateway_config(config)
    configure_logging(cfg.log_level, cfg.log_format)
    return cfg


def _prepare(
    config: Path, *, require_tools: bool = True
) -> tuple[GatewayConfig, list[PreparedBackend]]:
    cfg = _load(config)
    validate_config(cfg)
    return cfg, prepare_all(cfg, require_tools=require_tools)


def _table(header: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> str:
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]
    lines = [
        "   ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip()
        for row in [header, *rows]
    ]
    return "\n".join(lines)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


@app.command()
@_handled
def validate(config: ConfigOption = DEFAULT_CONFIG) -> None:
    """Validate configuration, local schemas and required environment variables."""
    prepared = prepare_checked(_load(config), os.environ)

    lines = [
        "OpenAPI MCP Gateway — Validation",
        "",
        "✓ Gateway configuration valid",
        f"✓ {_plural(len(prepared), 'backend')} discovered",
        *(f"✓ {p.config.name}: OpenAPI {p.openapi_version} valid" for p in prepared),
        "✓ No duplicate tool names",
        f"✓ {_plural(sum(len(p.operations) for p in prepared), 'operation')} selected",
        *(f"! {warning}" for p in prepared for warning in p.warnings),
        "",
        "Validation successful.",
    ]
    typer.echo("\n".join(lines))


@app.command("list-backends")
@_handled
def list_backends(config: ConfigOption = DEFAULT_CONFIG) -> None:
    """List discovered backends and how many tools each exposes (0 is not an error)."""
    _, prepared = _prepare(config, require_tools=False)
    rows = [
        (p.config.name, p.config.base_url, p.config.mode, str(len(p.tool_names))) for p in prepared
    ]
    typer.echo(_table(("BACKEND", "BASE_URL", "MODE", "TOOLS"), rows))


@app.command("list-tools")
@_handled
def list_tools(config: ConfigOption = DEFAULT_CONFIG) -> None:
    """Display generated MCP tool names."""
    _, prepared = _prepare(config)
    rows = [(p.config.name, name) for p in prepared for name in p.tool_names.values()]
    typer.echo(_table(("BACKEND", "TOOL"), rows))


@app.command()
@_handled
def serve(config: ConfigOption = DEFAULT_CONFIG) -> None:
    """Start the MCP server on stdio (stdout carries only the protocol)."""
    # Imports FastMCP, which is slow: only the command that needs it pays for it.
    from openapi_mcp_gateway.mcp import gateway  # noqa: PLC0415

    try:
        gateway.serve(config, env=os.environ)
    except KeyboardInterrupt:  # Ctrl-C before the signal handlers are installed
        raise typer.Exit(0) from None


def main() -> None:
    """Run the CLI; a ``GatewayError`` becomes ``error:`` lines and its exit code."""
    try:
        app()
    except GatewayError as exc:
        _report(exc)
        raise SystemExit(exc.exit_code) from None

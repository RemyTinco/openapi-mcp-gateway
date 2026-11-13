from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from openapi_mcp_gateway import cli
from openapi_mcp_gateway.cli import app, main
from openapi_mcp_gateway.exceptions import ConfigError, MultipleErrors, SchemaError

THREE = Path(__file__).parent.parent / "fixtures" / "gateways" / "three-backends" / "gateway.yaml"
ENV = {"CATALOG_API_TOKEN": "tok", "MESSAGING_API_KEY": "key"}


def _run(*args: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    # mix_stderr is gone in current Click; CliRunner.stderr is captured separately.
    r = CliRunner().invoke(app, list(args), env=env)
    return r.exit_code, r.stdout, r.stderr


def _single(tmp_path: Path, write_gateway: Any, schema: str) -> Path:
    path = write_gateway(
        tmp_path,
        {"version": 1},
        {"x": {"name": "x", "schema": "./openapi.json", "base_url": "https://x.example.com"}},
    )
    (tmp_path / "backends" / "x" / "openapi.json").write_text(schema)
    return path


def test_validate_success_output() -> None:
    code, out, _ = _run("validate", "--config", str(THREE), env=ENV)
    assert code == 0
    assert out.splitlines()[:2] == ["OpenAPI MCP Gateway — Validation", ""]
    assert "✓ Gateway configuration valid" in out
    assert "✓ 3 backends discovered" in out
    assert "✓ messaging: OpenAPI 3.1.0 valid" in out
    assert "✓ catalog: OpenAPI 3.0.3 valid" in out
    assert "✓ No duplicate tool names" in out
    assert "✓ 4 operations selected" in out
    assert out.rstrip().endswith("\n\nValidation successful.")


def test_validate_warnings_print_in_stdout(tmp_path: Path, write_gateway: Any) -> None:
    path = _single(
        tmp_path,
        write_gateway,
        json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "t", "version": "1"},
                "paths": {
                    "/a": {"get": {"operationId": "a", "responses": {"200": {"description": "x"}}}}
                },
            }
        ),
    )
    with (tmp_path / "backends" / "x" / "backend.yaml").open("a") as f:
        f.write("exposure:\n  exclude:\n    operations: [ghost]\n")
    code, out, _ = _run("validate", "--config", str(path))
    assert code == 0
    assert "! [x]" in out
    assert "ghost" in out


def test_validate_exit_2_on_bad_config(tmp_path: Path) -> None:
    (tmp_path / "gateway.yaml").write_text("version: 2\n")
    code, _, err = _run("validate", "--config", str(tmp_path / "gateway.yaml"))
    assert code == 2
    assert err.startswith("error:")


def test_validate_exit_3_on_bad_schema(tmp_path: Path, write_gateway: Any) -> None:
    path = _single(tmp_path, write_gateway, '{"openapi": "9.0.0"}')
    code, _, err = _run("validate", "--config", str(path))
    assert code == 3
    assert "[x]" in err
    assert "openapi.json" in err
    assert "openapi" in err


def test_validate_exit_4_on_missing_env() -> None:
    code, _, err = _run("validate", "--config", str(THREE), env={"MESSAGING_API_KEY": "key"})
    assert code == 4
    assert "CATALOG_API_TOKEN" in err
    assert "tok" not in err.replace("CATALOG_API_TOKEN", "")


@pytest.mark.parametrize("value", ["", "   ", "abc\ndef"])
def test_validate_exit_4_on_unusable_env(value: str) -> None:
    code, _, err = _run(
        "validate",
        "--config",
        str(THREE),
        env={"CATALOG_API_TOKEN": value, "MESSAGING_API_KEY": "key"},
    )
    assert code == 4
    assert "CATALOG_API_TOKEN" in err
    assert "abc" not in err


def test_validate_reports_schema_and_credential_errors_together(
    tmp_path: Path, write_gateway: Any
) -> None:
    path = write_gateway(
        tmp_path,
        {"version": 1},
        {
            "x": {
                "name": "x",
                "schema": "./openapi.json",
                "base_url": "https://x.example.com",
                "auth": {"type": "bearer", "token_env": "X_TOKEN_UNSET_FOR_TEST"},
            }
        },
    )
    (tmp_path / "backends" / "x" / "openapi.json").write_text('{"openapi": "9.0.0"}')
    code, _, err = _run("validate", "--config", str(path), env={})
    lines = err.strip().splitlines()
    assert code == 3
    assert len(lines) == 2
    assert all(line.startswith("error: ") for line in lines)
    assert "X_TOKEN_UNSET_FOR_TEST" in err


def test_list_tools_table() -> None:
    code, out, _ = _run("list-tools", "--config", str(THREE))
    lines = out.splitlines()
    assert code == 0
    assert lines[0].split() == ["BACKEND", "TOOL"]
    assert lines[1].split() == ["catalog", "catalog_products_list"]
    assert [line.split()[1] for line in lines[1:]] == [
        "catalog_products_list",
        "catalog_products_retrieve",
        "messaging_messages_list",
        "warehouse_orders_retrieve",
    ]


def test_list_tools_works_without_credentials() -> None:
    code, _, _ = _run("list-tools", "--config", str(THREE), env={})
    assert code == 0


def test_list_backends_table() -> None:
    code, out, _ = _run("list-backends", "--config", str(THREE))
    lines = out.splitlines()
    assert code == 0
    assert lines[0].split() == ["BACKEND", "BASE_URL", "MODE", "TOOLS"]
    assert lines[1].split() == ["catalog", "https://catalog.example.com", "read_only", "2"]
    assert lines[3].split() == ["warehouse", "https://warehouse.example.com", "read_only", "1"]


WRITE_ONLY_SCHEMA = json.dumps(
    {
        "openapi": "3.0.3",
        "info": {"title": "t", "version": "1"},
        "paths": {"/a": {"post": {"operationId": "a", "responses": {"200": {"description": "x"}}}}},
    }
)


def test_list_backends_shows_backends_when_no_tools_are_exposed(
    tmp_path: Path, write_gateway: Any
) -> None:
    path = _single(tmp_path, write_gateway, WRITE_ONLY_SCHEMA)  # read_only exposes nothing
    code, out, err = _run("list-backends", "--config", str(path))
    assert code == 0, err
    lines = out.splitlines()
    assert lines[0].split() == ["BACKEND", "BASE_URL", "MODE", "TOOLS"]
    assert lines[1].split() == ["x", "https://x.example.com", "read_only", "0"]
    assert err == ""


@pytest.mark.parametrize("command", ["validate", "list-tools"])
def test_other_commands_still_fail_when_no_tools_are_exposed(
    tmp_path: Path, write_gateway: Any, command: str
) -> None:
    path = _single(tmp_path, write_gateway, WRITE_ONLY_SCHEMA)
    code, _, err = _run(command, "--config", str(path))
    assert code == 2
    assert "no tools exposed by any backend" in err


def test_commands_default_config_is_cwd_gateway_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(THREE.parent)
    code, out, _ = _run("list-tools")
    assert code == 0
    assert "warehouse_orders_retrieve" in out


def test_no_traceback_without_debug() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "openapi_mcp_gateway",
            "list-tools",
            "--config",
            "/nonexistent.yaml",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert result.stderr.startswith("error:")


@pytest.mark.parametrize("command", ["validate", "list-backends", "list-tools"])
def test_offline_commands_do_not_import_fastmcp(command: str) -> None:
    code = (
        "import sys; from openapi_mcp_gateway.cli import app; "
        "import typer.testing as t; "
        f"r = t.CliRunner().invoke(app, [{command!r}, '--config', {str(THREE)!r}], env={ENV!r}); "
        "assert r.exit_code == 0, r.output; "
        "sys.exit(1 if any(m == 'fastmcp' or m.startswith('fastmcp.') for m in sys.modules) else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False, env=os.environ
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_main_prints_every_error_of_multiple_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom() -> None:
        raise MultipleErrors(
            [
                SchemaError("s", backend="a"),
                MultipleErrors([ConfigError("c1", backend="b"), ConfigError("c2")]),
            ]
        )

    monkeypatch.setattr(cli, "app", boom)
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert capsys.readouterr().err == "error: [a] s\nerror: [b] c1\nerror: c2\n"


def test_debug_flag_does_not_leak_between_invocations() -> None:
    _run("--debug", "version")
    assert cli._State.debug is True
    _run("version")
    assert cli._State.debug is False

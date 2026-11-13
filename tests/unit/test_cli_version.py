import pytest
from typer.testing import CliRunner

from openapi_mcp_gateway import __version__, cli
from openapi_mcp_gateway.cli import app, main
from openapi_mcp_gateway.exceptions import SchemaError


def test_version_command() -> None:
    r = CliRunner().invoke(app, ["version"])
    assert r.exit_code == 0
    assert r.stdout.strip() == __version__


def test_main_maps_gateway_error_to_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom() -> None:
        msg = "broken"
        raise SchemaError(msg, backend="pay")

    monkeypatch.setattr(cli, "app", boom)
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 3
    assert capsys.readouterr().err == "error: [pay] broken\n"

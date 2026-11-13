from __future__ import annotations

import json
import logging

import pytest

from openapi_mcp_gateway.logging import configure_logging, log_event


@pytest.fixture(autouse=True)
def _restore_root_logger() -> object:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def _log(**fields: object) -> logging.Logger:
    logger = logging.getLogger("openapi_mcp_gateway.test")
    log_event(logger, logging.INFO, "backend.loaded", **fields)
    return logger


def test_logs_go_to_stderr_only(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "text")
    _log(name="catalog")
    out, err = capsys.readouterr()
    assert out == ""
    assert err


def test_text_format(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "text")
    _log(name="catalog")
    assert capsys.readouterr().err.strip() == "INFO backend.loaded name=catalog"


def test_text_format_quotes_whitespace(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "text")
    _log(reason="two words", empty="")
    assert capsys.readouterr().err.strip() == 'INFO backend.loaded reason="two words" empty=""'


def test_level_filters(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("WARNING", "text")
    _log(name="x")
    assert capsys.readouterr().err == ""


def test_configure_twice_does_not_duplicate(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "text")
    configure_logging("INFO", "text")
    _log(name="x")
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


def test_sensitive_fields_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "text")
    _log(token="s3cr3t", Authorization="Bearer s3cr3t", password="s3cr3t", ok="fine")
    err = capsys.readouterr().err
    assert "s3cr3t" not in err
    assert "token=***" in err
    assert "Authorization=***" in err
    assert "ok=fine" in err


@pytest.mark.parametrize(
    "name", ["api_key", "api_key_header", "request_body", "x-api-key", "X_Api_Key", "client_secret"]
)
def test_sensitive_substring_names_redacted(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")
    _log(**{name: "s3cr3t"})
    err = capsys.readouterr().err
    assert "s3cr3t" not in err
    assert json.loads(err)[name] == "***"


def test_json_format_is_one_object_per_line(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")
    _log(name="a")
    _log(name="b", token="s3cr3t")
    lines = capsys.readouterr().err.strip().splitlines()
    objs = [json.loads(line) for line in lines]
    assert [o["name"] for o in objs] == ["a", "b"]
    assert objs[0]["level"] == "INFO"
    assert objs[0]["event"] == "backend.loaded"
    assert objs[1]["token"] == "***"


@pytest.mark.parametrize(("level", "shown"), [("INFO", False), ("DEBUG", True)])
@pytest.mark.parametrize("fmt", ["text", "json"])
def test_tracebacks_only_at_debug(
    capsys: pytest.CaptureFixture[str], level: str, shown: bool, fmt: str
) -> None:
    configure_logging(level, fmt)  # type: ignore[arg-type]
    try:
        int("boom")
    except ValueError:
        # FastMCP logs tool failures exactly this way.
        logging.getLogger("fastmcp.test").error("Error calling tool 'x'", exc_info=True)  # noqa: G201
    err = capsys.readouterr().err
    assert ("Traceback" in err) is shown
    if fmt == "json":
        assert len(err.splitlines()) == 1
        assert json.loads(err)["event"] == "Error calling tool 'x'"
    else:
        assert err.splitlines()[0] == "ERROR Error calling tool 'x'"

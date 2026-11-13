from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from openapi_mcp_gateway.config.loader import load_gateway_config
from openapi_mcp_gateway.config.models import AuthApiKey, AuthBearer, AuthNone
from openapi_mcp_gateway.exceptions import ConfigError, MultipleErrors

if TYPE_CHECKING:
    from pathlib import Path

GATEWAY_YAML = """\
version: 1

server:
  name: "Company API Gateway"
  transport: stdio

backends:
  directory: ./backends
  discovery: auto

defaults:
  exposure:
    mode: read_only

  http:
    timeout_seconds: 30
    follow_redirects: false

logging:
  level: INFO
  format: text
"""

BACKEND_YAML = """\
name: catalog

schema: ./openapi.yaml

base_url: https://catalog.example.com

auth:
  type: bearer
  token_env: CATALOG_API_TOKEN

exposure:
  mode: read_only

  exclude:
    operations:
      - products_export
    paths:
      - /api/internal/*
      - /api/admin/*

http:
  timeout_seconds: 20
"""

MIN_BACKEND: dict[str, Any] = {
    "name": "app",
    "schema": "./openapi.yaml",
    "base_url": "https://x.com",
}


def _backend(**extra: Any) -> dict[str, Any]:
    return {**MIN_BACKEND, **extra}


def test_loads_spec_examples(tmp_path: Path) -> None:
    (tmp_path / "gateway.yaml").write_text(GATEWAY_YAML)
    backend_dir = tmp_path / "backends" / "catalog"
    backend_dir.mkdir(parents=True)
    (backend_dir / "backend.yaml").write_text(BACKEND_YAML)

    cfg = load_gateway_config(tmp_path / "gateway.yaml")

    assert cfg.server_name == "Company API Gateway"
    assert (cfg.log_level, cfg.log_format) == ("INFO", "text")
    assert cfg.backends_directory == tmp_path / "backends"
    b = cfg.backends[0]
    assert (b.name, b.base_url, b.mode, b.timeout_seconds) == (
        "catalog",
        "https://catalog.example.com",
        "read_only",
        20,
    )
    assert b.exclude_operations == ("products_export",)
    assert b.exclude_paths == ("/api/internal/*", "/api/admin/*")
    assert b.schema_path == tmp_path / "backends/catalog/openapi.yaml"
    assert b.directory == backend_dir
    assert b.config_path == backend_dir / "backend.yaml"
    assert b.auth == AuthBearer(type="bearer", token_env="CATALOG_API_TOKEN")
    assert (b.follow_redirects, b.max_response_bytes, b.max_connections) == (False, 1048576, 10)


def test_builtin_defaults_for_minimal_files(tmp_path: Path, write_gateway: Any) -> None:
    path = write_gateway(tmp_path, {"version": 1}, {"app": MIN_BACKEND})
    cfg = load_gateway_config(path)
    assert cfg.server_name == "OpenAPI MCP Gateway"
    assert (cfg.log_level, cfg.log_format) == ("INFO", "text")
    b = cfg.backends[0]
    assert b.auth == AuthNone(type="none")
    assert (b.mode, b.operations, b.timeout_seconds) == ("read_only", (), 30)
    assert b.exclude_operations == ()
    assert b.exclude_paths == ()


def test_discovery_is_lexicographic_and_skips_dirs_without_backend_yaml(
    tmp_path: Path, write_gateway: Any
) -> None:
    path = write_gateway(
        tmp_path,
        {"version": 1},
        {
            "warehouse": _backend(name="warehouse"),
            "catalog": _backend(name="catalog"),
            "messaging": _backend(name="messaging"),
        },
    )
    (tmp_path / "backends" / "notes").mkdir()
    (tmp_path / "backends" / "notes" / "readme.txt").write_text("x")
    nested = tmp_path / "backends" / "group" / "deep"
    nested.mkdir(parents=True)
    (nested / "backend.yaml").write_text("name: deep\n")
    (tmp_path / "backends" / "loose.yaml").write_text("x: 1")

    cfg = load_gateway_config(path)
    assert [b.name for b in cfg.backends] == ["catalog", "messaging", "warehouse"]


def test_empty_backends_directory_yields_no_backends(tmp_path: Path, write_gateway: Any) -> None:
    path = write_gateway(tmp_path, {"version": 1}, {})
    (tmp_path / "backends").mkdir()
    assert load_gateway_config(path).backends == ()


def test_missing_backends_directory_is_config_error(tmp_path: Path, write_gateway: Any) -> None:
    path = write_gateway(tmp_path, {"version": 1}, {})
    with pytest.raises(ConfigError) as exc:
        load_gateway_config(path)
    assert exc.value.field == "backends.directory"


def test_missing_gateway_file_is_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_gateway_config(tmp_path / "nope.yaml")


def test_backends_directory_resolves_against_gateway_file(
    tmp_path: Path, write_gateway: Any
) -> None:
    path = write_gateway(
        tmp_path, {"version": 1, "backends": {"directory": "./custom/dir"}}, {"app": MIN_BACKEND}
    )
    cfg = load_gateway_config(path)
    assert cfg.backends_directory == tmp_path / "custom/dir"
    assert cfg.backends[0].schema_path == tmp_path / "custom/dir/app/openapi.yaml"


def test_schema_path_resolves_against_backend_file(tmp_path: Path, write_gateway: Any) -> None:
    path = write_gateway(
        tmp_path, {"version": 1}, {"app": _backend(schema="../shared/openapi.json")}
    )
    assert (
        load_gateway_config(path).backends[0].schema_path
        == tmp_path / "backends/shared/openapi.json"
    )


def test_backend_inherits_default_timeout(tmp_path: Path, write_gateway: Any) -> None:
    path = write_gateway(
        tmp_path,
        {"version": 1, "defaults": {"http": {"timeout_seconds": 45}}},
        {"a": _backend(name="a"), "b": _backend(name="b", http={"timeout_seconds": 5})},
    )
    a, b = load_gateway_config(path).backends
    assert (a.timeout_seconds, b.timeout_seconds) == (45, 5)


def test_extra_http_keys_inherit_and_override(tmp_path: Path, write_gateway: Any) -> None:
    defaults = {"http": {"follow_redirects": True, "max_response_bytes": 10, "max_connections": 3}}
    path = write_gateway(
        tmp_path,
        {"version": 1, "defaults": defaults},
        {
            "a": _backend(name="a"),
            "b": _backend(
                name="b",
                http={"follow_redirects": False, "max_response_bytes": 20, "max_connections": 4},
            ),
        },
    )
    a, b = load_gateway_config(path).backends
    assert (a.follow_redirects, a.max_response_bytes, a.max_connections) == (True, 10, 3)
    assert (b.follow_redirects, b.max_response_bytes, b.max_connections) == (False, 20, 4)


def test_backend_mode_beats_default_mode(tmp_path: Path, write_gateway: Any) -> None:
    path = write_gateway(
        tmp_path,
        {"version": 1, "defaults": {"exposure": {"mode": "allowlist"}}},
        {
            "a": _backend(name="a"),
            "b": _backend(name="b", exposure={"mode": "all"}),
            "c": _backend(name="c", exposure={"mode": "allowlist", "operations": ["x", "y"]}),
        },
    )
    a, b, c = load_gateway_config(path).backends
    assert (a.mode, b.mode, c.mode) == ("allowlist", "all", "allowlist")
    assert c.operations == ("x", "y")


def test_default_mode_all_rejected(tmp_path: Path, write_gateway: Any) -> None:
    path = write_gateway(tmp_path, {"version": 1, "defaults": {"exposure": {"mode": "all"}}}, {})
    with pytest.raises(ConfigError, match=r"defaults\.exposure\.mode"):
        load_gateway_config(path)


def test_unknown_key_rejected_with_field_and_file(tmp_path: Path, write_gateway: Any) -> None:
    backend = {k: v for k, v in MIN_BACKEND.items() if k != "base_url"}
    path = write_gateway(tmp_path, {"version": 1}, {"catalog": {**backend, "base_uri": "x"}})
    with pytest.raises(MultipleErrors) as multi:  # also reports the missing base_url
        load_gateway_config(path)
    e = next(e for e in multi.value.errors if e.field == "base_uri")
    assert e.backend == "catalog"
    assert e.file is not None
    assert e.file.name == "backend.yaml"
    assert "base_uri" in str(e)


def test_single_unknown_key_raises_that_error(tmp_path: Path, write_gateway: Any) -> None:
    path = write_gateway(tmp_path, {"version": 1}, {"catalog": _backend(base_uri="x")})
    with pytest.raises(ConfigError) as exc:
        load_gateway_config(path)
    e = exc.value
    assert e.backend == "catalog"
    assert e.file is not None
    assert e.file.name == "backend.yaml"
    assert "base_uri" in str(e)


@pytest.mark.parametrize(
    "gateway",
    [
        {"version": 2},
        {},
        {"version": 1, "server": {"transport": "http"}},
        {"version": 1, "backends": {"discovery": "manual"}},
        {"version": 1, "logging": {"level": "TRACE"}},
        {"version": 1, "defaults": {"http": {"timeout_seconds": 0}}},
        {"version": 1, "defaults": {"http": {"max_connections": -1}}},
        {"version": 1, "defaults": {"http": {"follow_redirects": "yes"}}},
        {"version": 1, "surprise": True},
    ],
)
def test_invalid_gateway_values_rejected(tmp_path: Path, write_gateway: Any, gateway: Any) -> None:
    path = write_gateway(tmp_path, gateway, {})
    with pytest.raises(ConfigError):
        load_gateway_config(path)


@pytest.mark.parametrize("content", ["", "- a\n- b\n", "name: [unclosed", b"\xff\xfe"])
def test_malformed_yaml_is_config_error(tmp_path: Path, content: str | bytes) -> None:
    (tmp_path / "backends" / "app").mkdir(parents=True)
    (tmp_path / "gateway.yaml").write_text("version: 1\n")
    target = tmp_path / "backends" / "app" / "backend.yaml"
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content)
    with pytest.raises(ConfigError) as exc:
        load_gateway_config(tmp_path / "gateway.yaml")
    assert exc.value.backend == "app"


@pytest.mark.parametrize("content", ["", "- a\n", b"\xff\xfe"])
def test_malformed_gateway_yaml_is_config_error(tmp_path: Path, content: str | bytes) -> None:
    path = tmp_path / "gateway.yaml"
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    with pytest.raises(ConfigError):
        load_gateway_config(path)


def test_missing_required_fields_reported_together(tmp_path: Path, write_gateway: Any) -> None:
    path = write_gateway(tmp_path, {"version": 1}, {"app": {"schema": "./openapi.yaml"}})
    with pytest.raises(MultipleErrors) as exc:
        load_gateway_config(path)
    assert sorted(e.field or "" for e in exc.value.errors) == ["base_url", "name"]
    assert exc.value.exit_code == 2


def test_errors_across_backends_reported_together(tmp_path: Path, write_gateway: Any) -> None:
    path = write_gateway(
        tmp_path,
        {"version": 1},
        {"a": _backend(name="a", bogus=1), "b": _backend(name="b", other=2)},
    )
    with pytest.raises(MultipleErrors) as exc:
        load_gateway_config(path)
    assert [e.backend for e in exc.value.errors] == ["a", "b"]


def test_api_key_header_auth(tmp_path: Path, write_gateway: Any) -> None:
    auth = {"type": "api_key", "location": "header", "name": "X-API-Key", "value_env": "PAY_KEY"}
    path = write_gateway(tmp_path, {"version": 1}, {"app": _backend(auth=auth)})
    assert load_gateway_config(path).backends[0].auth == AuthApiKey(
        type="api_key", location="header", name="X-API-Key", value_env="PAY_KEY"
    )


def test_api_key_query_location_rejected(tmp_path: Path, write_gateway: Any) -> None:
    auth = {"type": "api_key", "location": "query", "name": "k", "value_env": "PAY_KEY"}
    path = write_gateway(tmp_path, {"version": 1}, {"app": _backend(auth=auth)})
    with pytest.raises(ConfigError) as exc:
        load_gateway_config(path)
    assert exc.value.field == "auth.location"


@pytest.mark.parametrize(
    "auth", [{"type": "basic"}, {"type": "bearer"}, {"token_env": "X"}, {"type": "none", "x": 1}]
)
def test_invalid_auth_rejected(tmp_path: Path, write_gateway: Any, auth: Any) -> None:
    path = write_gateway(tmp_path, {"version": 1}, {"app": _backend(auth=auth)})
    with pytest.raises(ConfigError):
        load_gateway_config(path)


def test_empty_auth_block_means_none(tmp_path: Path, write_gateway: Any) -> None:
    path = write_gateway(tmp_path, {"version": 1}, {"app": _backend(auth={})})
    assert load_gateway_config(path).backends[0].auth == AuthNone(type="none")


def test_validation_errors_do_not_echo_input_values(tmp_path: Path, write_gateway: Any) -> None:
    auth = {"type": "bearer", "token_env": "X", "token": "s3cr3t-value"}
    path = write_gateway(tmp_path, {"version": 1}, {"app": _backend(auth=auth)})
    with pytest.raises(ConfigError) as exc:
        load_gateway_config(path)
    assert "s3cr3t-value" not in str(exc.value)


# --- duplicate keys and YAML syntax errors (F1) --------------------------------

DUPLICATE_EXCLUDE = """\
name: app
schema: ./openapi.yaml
base_url: https://x.com
exposure:
  mode: all
  exclude:
    operations: [items_export]
  exclude:
    paths: [/internal/*]
"""


def _write_backend(tmp_path: Path, text: str) -> Path:
    (tmp_path / "backends" / "app").mkdir(parents=True)
    (tmp_path / "gateway.yaml").write_text("version: 1\n")
    (tmp_path / "backends" / "app" / "backend.yaml").write_text(text)
    return tmp_path / "gateway.yaml"


def test_duplicate_key_in_backend_yaml_is_config_error(tmp_path: Path) -> None:
    path = _write_backend(tmp_path, DUPLICATE_EXCLUDE)
    with pytest.raises(ConfigError) as exc:
        load_gateway_config(path)
    assert exc.value.backend == "app"
    assert exc.value.file == tmp_path / "backends" / "app" / "backend.yaml"
    assert "duplicate key 'exclude'" in exc.value.message
    assert "line 8" in exc.value.message


@pytest.mark.parametrize(
    "text",
    [
        "version: 1\nversion: 1\n",  # top level
        "version: 1\nlogging:\n  level: INFO\n  level: DEBUG\n",  # nested
        "version: 1\ndefaults:\n  http: {timeout_seconds: 1, timeout_seconds: 2}\n",  # flow
    ],
)
def test_duplicate_key_in_gateway_yaml_is_config_error(tmp_path: Path, text: str) -> None:
    (tmp_path / "backends").mkdir()
    path = tmp_path / "gateway.yaml"
    path.write_text(text)
    with pytest.raises(ConfigError) as exc:
        load_gateway_config(path)
    assert exc.value.file == path
    assert "duplicate key" in exc.value.message


def test_yaml_merge_keys_are_not_duplicates(tmp_path: Path) -> None:
    # A merge key may be overridden by an explicit key; that is not a duplicate.
    text = (
        "name: app\nschema: ./openapi.yaml\nbase_url: https://x.com\n"
        "http:\n  <<: {timeout_seconds: 5, max_connections: 2}\n  timeout_seconds: 7\n"
    )
    path = _write_backend(tmp_path, text)
    backend = load_gateway_config(path).backends[0]
    assert (backend.timeout_seconds, backend.max_connections) == (7.0, 2)


def test_yaml_syntax_error_does_not_echo_source(tmp_path: Path) -> None:
    text = "name: app\nauth:\n  token: s3cr3t-pasted-value: oops\n"
    path = _write_backend(tmp_path, text)
    with pytest.raises(ConfigError) as exc:
        load_gateway_config(path)
    message = exc.value.message
    assert message.startswith("invalid YAML: ")
    assert "s3cr3t-pasted-value" not in str(exc.value)
    assert "line 3" in message
    assert "column" in message


# --- api_key header names and numeric limits (F5b, F5d) ---------------------------


@pytest.mark.parametrize(
    "name",
    [
        "X API Key",
        "X-Key:",
        "X-Ключ",
        "(key)",
        "a\nb",
        "Host",
        "content-length",
        "Transfer-Encoding",
        "CONNECTION",
    ],
)
def test_api_key_name_must_be_a_usable_header_name(
    tmp_path: Path, write_gateway: Any, name: str
) -> None:
    auth = {"type": "api_key", "location": "header", "name": name, "value_env": "PAY_KEY"}
    path = write_gateway(tmp_path, {"version": 1}, {"app": _backend(auth=auth)})
    with pytest.raises(ConfigError) as exc:
        load_gateway_config(path)
    assert exc.value.field == "auth.name"


@pytest.mark.parametrize("name", ["X-API-Key", "Authorization", "x_key.v2", "!#$%&'*+-.^_`|~"])
def test_api_key_name_accepts_header_tokens(tmp_path: Path, write_gateway: Any, name: str) -> None:
    auth = {"type": "api_key", "location": "header", "name": name, "value_env": "PAY_KEY"}
    path = write_gateway(tmp_path, {"version": 1}, {"app": _backend(auth=auth)})
    assert load_gateway_config(path).backends[0].auth.name == name  # type: ignore[union-attr]


@pytest.mark.parametrize("value", [".inf", "-.inf", ".nan"])
@pytest.mark.parametrize("where", ["backend", "defaults"])
def test_timeout_must_be_finite(tmp_path: Path, write_gateway: Any, value: str, where: str) -> None:
    gateway: dict[str, Any] = {"version": 1}
    backend = _backend()
    if where == "defaults":
        gateway["defaults"] = {"http": {"timeout_seconds": "PLACEHOLDER"}}
    else:
        backend["http"] = {"timeout_seconds": "PLACEHOLDER"}
    path = write_gateway(tmp_path, gateway, {"app": backend})
    target = path if where == "defaults" else tmp_path / "backends" / "app" / "backend.yaml"
    target.write_text(target.read_text().replace("PLACEHOLDER", value))
    with pytest.raises(ConfigError) as exc:
        load_gateway_config(path)
    assert (exc.value.field or "").endswith("timeout_seconds")

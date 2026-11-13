from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from openapi_mcp_gateway.config.models import (
    AuthApiKey,
    AuthBearer,
    AuthNone,
    BackendConfig,
    GatewayConfig,
)
from openapi_mcp_gateway.config.validator import (
    check_credentials,
    required_env_vars,
    secret_from_env,
    validate_config,
)
from openapi_mcp_gateway.exceptions import ConfigError, CredentialsError, MultipleErrors

if TYPE_CHECKING:
    from pathlib import Path


def make_backend(tmp_path: Path, dirname: str = "catalog", **overrides: object) -> BackendConfig:
    directory = tmp_path / "backends" / dirname
    directory.mkdir(parents=True, exist_ok=True)
    schema = directory / "openapi.yaml"
    schema.write_text("openapi: 3.0.0\n")
    backend = BackendConfig(
        name=dirname,
        directory=directory,
        config_path=directory / "backend.yaml",
        schema_path=schema,
        base_url="https://catalog.example.com",
        auth=AuthBearer(type="bearer", token_env="CATALOG_API_TOKEN"),
        mode="read_only",
        operations=(),
        exclude_operations=(),
        exclude_paths=(),
        timeout_seconds=30.0,
        follow_redirects=False,
        max_response_bytes=1048576,
        max_connections=10,
    )
    return replace(backend, **overrides)  # type: ignore[arg-type]


def make_config(tmp_path: Path, *backends: BackendConfig) -> GatewayConfig:
    return GatewayConfig(
        config_path=tmp_path / "gateway.yaml",
        server_name="OpenAPI MCP Gateway",
        backends_directory=tmp_path / "backends",
        log_level="INFO",
        log_format="text",
        backends=backends,
    )


def test_valid_config_passes(tmp_path: Path) -> None:
    validate_config(make_config(tmp_path, make_backend(tmp_path)))


def test_duplicate_backend_names(tmp_path: Path) -> None:
    first = make_backend(tmp_path, "one", name="same")
    second = make_backend(tmp_path, "two", name="same")
    with pytest.raises(ConfigError) as exc:
        validate_config(make_config(tmp_path, first, second))
    assert str(first.directory) in str(exc.value)
    assert str(second.directory) in str(exc.value)
    assert exc.value.backend == "same"


@pytest.mark.parametrize("name", ["Catalog", "1api", "my api", ""])
def test_invalid_backend_name(tmp_path: Path, name: str) -> None:
    cfg = make_config(tmp_path, make_backend(tmp_path, "dirname", name=name))
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert exc.value.field == "name"


@pytest.mark.parametrize("name", ["a", "my-api_2"])
def test_valid_backend_names(tmp_path: Path, name: str) -> None:
    validate_config(make_config(tmp_path, make_backend(tmp_path, "d", name=name)))


@pytest.mark.parametrize(
    "url",
    [
        "ftp://x",
        "catalog.example.com",
        "https://u:p@x.com",
        "https://u@x.com",
        "https://x.com/?a=1",
        "https://x.com?",
        "https://x.com/#f",
        "https://",
        "https://x.com:notaport",
    ],
)
def test_invalid_base_url(tmp_path: Path, url: str) -> None:
    cfg = make_config(tmp_path, make_backend(tmp_path, base_url=url))
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert exc.value.field == "base_url"
    assert "p@" not in str(exc.value)


@pytest.mark.parametrize("url", ["https://x.com/api/v2", "http://localhost:8000", "https://x.com/"])
def test_base_url_with_path_prefix_allowed(tmp_path: Path, url: str) -> None:
    validate_config(make_config(tmp_path, make_backend(tmp_path, base_url=url)))


def test_missing_schema_file(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    backend.schema_path.unlink()
    with pytest.raises(ConfigError) as exc:
        validate_config(make_config(tmp_path, backend))
    assert exc.value.field == "schema"


def test_all_problems_across_backends_collected(tmp_path: Path) -> None:
    bad_url = make_backend(tmp_path, "one", name="one", base_url="ftp://x")
    bad_name = make_backend(tmp_path, "two", name="Two")
    missing = make_backend(tmp_path, "three", name="three")
    missing.schema_path.unlink()
    with pytest.raises(MultipleErrors) as exc:
        validate_config(make_config(tmp_path, bad_url, bad_name, missing))
    assert [e.field for e in exc.value.errors] == ["base_url", "name", "schema"]


def test_required_env_vars(tmp_path: Path) -> None:
    assert required_env_vars(make_backend(tmp_path)) == ("CATALOG_API_TOKEN",)
    api_key = AuthApiKey(type="api_key", location="header", name="X-K", value_env="PAY_KEY")
    assert required_env_vars(make_backend(tmp_path, auth=api_key)) == ("PAY_KEY",)
    assert required_env_vars(make_backend(tmp_path, auth=AuthNone(type="none"))) == ()


def test_secret_from_env_returns_value() -> None:
    assert secret_from_env({"K": "abc def"}, "K") == "abc def"


@pytest.mark.parametrize(
    ("env", "fragment"),
    [
        ({}, "not set"),
        ({"K": ""}, "empty"),
        ({"K": " \t "}, "empty"),
        ({"K": "tok\nen"}, "line break"),
        ({"K": "tok\ren"}, "line break"),
        ({"K": "token\r\nX-Evil: 1"}, "line break"),
    ],
)
def test_secret_from_env_rejects_bad_values(env: dict[str, str], fragment: str) -> None:
    with pytest.raises(CredentialsError) as exc:
        secret_from_env(env, "K")
    assert "K" in str(exc.value)
    assert fragment in str(exc.value)
    for value in env.values():
        if value.strip():
            assert value not in str(exc.value)


def test_check_credentials_ok(tmp_path: Path) -> None:
    check_credentials(make_config(tmp_path, make_backend(tmp_path)), {"CATALOG_API_TOKEN": "t"})


def test_check_credentials_single_failure(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, make_backend(tmp_path))
    with pytest.raises(CredentialsError) as exc:
        check_credentials(cfg, {})
    assert exc.value.backend == "catalog"
    assert "CATALOG_API_TOKEN" in str(exc.value)
    assert exc.value.exit_code == 4


def test_check_credentials_collects_all_failures(tmp_path: Path) -> None:
    api_key = AuthApiKey(type="api_key", location="header", name="X-K", value_env="PAY_KEY")
    cfg = make_config(
        tmp_path,
        make_backend(tmp_path, "a", name="a"),
        make_backend(tmp_path, "b", name="b", auth=api_key),
        make_backend(tmp_path, "c", name="c", auth=AuthNone(type="none")),
    )
    with pytest.raises(MultipleErrors) as exc:
        check_credentials(cfg, {"PAY_KEY": "bad\nvalue"})
    assert [e.backend for e in exc.value.errors] == ["a", "b"]
    assert all(isinstance(e, CredentialsError) for e in exc.value.errors)
    assert "bad" not in str(exc.value)


# --- exposure.operations outside allowlist mode (F3) ---------------------------


@pytest.mark.parametrize("mode", ["read_only", "all"])
def test_operations_list_outside_allowlist_is_config_error(tmp_path: Path, mode: str) -> None:
    backend = make_backend(tmp_path, mode=mode, operations=("items_list",))
    with pytest.raises(ConfigError) as exc:
        validate_config(make_config(tmp_path, backend))
    assert exc.value.field == "exposure.operations"
    assert exc.value.backend == "catalog"
    assert "mode: allowlist" in exc.value.message


def test_operations_list_in_allowlist_mode_passes(tmp_path: Path) -> None:
    backend = make_backend(tmp_path, mode="allowlist", operations=("items_list",))
    validate_config(make_config(tmp_path, backend))


@pytest.mark.parametrize("mode", ["read_only", "allowlist", "all"])
def test_empty_operations_list_passes_in_every_mode(tmp_path: Path, mode: str) -> None:
    validate_config(make_config(tmp_path, make_backend(tmp_path, mode=mode)))


# --- base_url spelling (F5c) -----------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        " https://x.com",
        "https://x.com ",
        "https://x.com/api v2",
        "https://x.com/\tapi",
        "https://x.com\n",
    ],
)
def test_base_url_with_whitespace_rejected(tmp_path: Path, url: str) -> None:
    cfg = make_config(tmp_path, make_backend(tmp_path, base_url=url))
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert exc.value.field == "base_url"
    assert "whitespace" in exc.value.message


@pytest.mark.parametrize("url", ["https://bücher.example", "https://例え.jp:8443/api"])
def test_base_url_with_non_ascii_host_rejected(tmp_path: Path, url: str) -> None:
    cfg = make_config(tmp_path, make_backend(tmp_path, base_url=url))
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert exc.value.field == "base_url"
    assert "punycode" in exc.value.message


def test_base_url_with_punycode_host_allowed(tmp_path: Path) -> None:
    url = "https://xn--bcher-kva.example/api"
    validate_config(make_config(tmp_path, make_backend(tmp_path, base_url=url)))


# --- secrets must be printable ASCII (F5a) -----------------------------------------


@pytest.mark.parametrize(
    "value",
    ["sécret-Ω", "tok\ten", "tok\x00en", "tok\x7fen", " token", "token ", "tök"],
)
def test_secret_from_env_requires_printable_ascii(value: str) -> None:
    with pytest.raises(CredentialsError) as exc:
        secret_from_env({"K": value}, "K")
    assert "environment variable K" in str(exc.value)
    assert value.strip() not in str(exc.value)


def test_secret_from_env_allows_interior_spaces_and_punctuation() -> None:
    value = "Token a!b#c$%&'()*+,-./:;<=>?@[\\]^_`{|}~ z"
    assert secret_from_env({"K": value}, "K") == value

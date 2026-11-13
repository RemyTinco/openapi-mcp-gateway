from __future__ import annotations

import pytest

from openapi_mcp_gateway.config.models import AuthApiKey, AuthBearer, AuthNone
from openapi_mcp_gateway.exceptions import CredentialsError
from openapi_mcp_gateway.http.authentication import auth_headers

bearer = AuthBearer(type="bearer", token_env="CATALOG_API_TOKEN")
api_key = AuthApiKey(
    type="api_key", location="header", name="X-API-Key", value_env="MESSAGING_API_KEY"
)


def test_none() -> None:
    assert auth_headers(AuthNone(type="none"), {}) == {}


def test_none_ignores_environment() -> None:
    assert auth_headers(AuthNone(type="none"), {"CATALOG_API_TOKEN": "t0k"}) == {}


def test_bearer() -> None:
    assert auth_headers(bearer, {"CATALOG_API_TOKEN": "t0k"}) == {"Authorization": "Bearer t0k"}


def test_api_key() -> None:
    assert auth_headers(api_key, {"MESSAGING_API_KEY": "k"}) == {"X-API-Key": "k"}


@pytest.mark.parametrize("env", [{}, {"CATALOG_API_TOKEN": ""}, {"CATALOG_API_TOKEN": "  "}])
def test_missing_secret_names_variable_only(env: dict[str, str]) -> None:
    with pytest.raises(CredentialsError, match="CATALOG_API_TOKEN"):
        auth_headers(bearer, env)


def test_missing_api_key_names_variable() -> None:
    with pytest.raises(CredentialsError, match="MESSAGING_API_KEY"):
        auth_headers(api_key, {})


@pytest.mark.parametrize("value", ["s3cr3t\ninjected", "s3cr3t\r\nX-Evil: 1"])
def test_error_never_contains_value(value: str) -> None:
    with pytest.raises(CredentialsError) as e:
        auth_headers(bearer, {"CATALOG_API_TOKEN": value})
    assert "s3cr3t" not in str(e.value)
    assert "s3cr3t" not in repr(e.value)
    assert "CATALOG_API_TOKEN" in str(e.value)

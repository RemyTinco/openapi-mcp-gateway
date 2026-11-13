from pathlib import Path

from openapi_mcp_gateway.exceptions import (
    ConfigError,
    CredentialsError,
    GatewayError,
    MultipleErrors,
    SchemaError,
)


def test_error_string_includes_context() -> None:
    e = SchemaError(
        "broken $ref", backend="messaging", file=Path("openapi.json"), field="paths./x.get"
    )
    assert str(e) == "[messaging] openapi.json: paths./x.get: broken $ref"
    assert e.exit_code == 3


def test_error_string_omits_absent_parts() -> None:
    assert str(ConfigError("bad")) == "bad"
    assert str(ConfigError("bad", field="timeout")) == "timeout: bad"


def test_multiple_errors_uses_lowest_code() -> None:
    assert MultipleErrors([CredentialsError("a"), ConfigError("b")]).exit_code == 2


def test_multiple_errors_lists_all_messages() -> None:
    err = MultipleErrors([CredentialsError("a"), ConfigError("b")])
    assert str(err) == "a; b"
    assert isinstance(err, GatewayError)

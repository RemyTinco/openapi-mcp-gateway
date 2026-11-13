"""Semantic validation of a resolved configuration, and credential checks."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from openapi_mcp_gateway.config._errors import raise_collected
from openapi_mcp_gateway.config.models import (
    AuthApiKey,
    AuthBearer,
    BackendConfig,
    GatewayConfig,
)
from openapi_mcp_gateway.exceptions import ConfigError, CredentialsError, GatewayError

if TYPE_CHECKING:
    from collections.abc import Mapping

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")


def _base_url_problem(url: str) -> str | None:  # noqa: PLR0911 - one return per rule
    if any(c.isspace() for c in url):
        return "must not contain whitespace"
    try:
        parts = urlsplit(url)
        host = parts.hostname
        _ = parts.port  # raises ValueError for a malformed port
    except ValueError:
        return "is not a valid URL"
    if parts.scheme not in ("http", "https"):
        return "must start with http:// or https://"
    if not host:
        return "must include a host"
    if not host.isascii():
        return "host must be ASCII; write an internationalised domain name in punycode (xn--...)"
    if parts.username is not None or parts.password is not None:
        return "must not contain credentials"
    if parts.query or "?" in url:
        return "must not contain a query string"
    if parts.fragment or "#" in url:
        return "must not contain a fragment"
    return None


def _validate_backend(backend: BackendConfig) -> list[GatewayError]:
    def err(message: str, field: str) -> ConfigError:
        return ConfigError(message, backend=backend.name, file=backend.config_path, field=field)

    errors: list[GatewayError] = []
    if not NAME_PATTERN.fullmatch(backend.name):
        errors.append(err(f"name {backend.name!r} must match {NAME_PATTERN.pattern}", "name"))
    problem = _base_url_problem(backend.base_url)
    if problem is not None:
        errors.append(err(f"base_url {problem}", "base_url"))
    if backend.operations and backend.mode != "allowlist":
        errors.append(
            err(
                f"operations are only used in allowlist mode, but the mode is {backend.mode}; "
                "set mode: allowlist or remove the list",
                "exposure.operations",
            )
        )
    if not backend.schema_path.is_file():
        errors.append(err(f"schema file not found: {backend.schema_path}", "schema"))
    return errors


def validate_config(cfg: GatewayConfig) -> None:
    """Check every backend and report all problems together."""
    errors: list[GatewayError] = []
    first_seen: dict[str, BackendConfig] = {}
    for backend in cfg.backends:
        errors.extend(_validate_backend(backend))
        earlier = first_seen.setdefault(backend.name, backend)
        if earlier is not backend:
            errors.append(
                ConfigError(
                    f"duplicate backend name; used by directories "
                    f"{earlier.directory} and {backend.directory}",
                    backend=backend.name,
                    file=backend.config_path,
                    field="name",
                )
            )
    raise_collected(errors)


def required_env_vars(cfg: BackendConfig) -> tuple[str, ...]:
    """Names of the environment variables the backend's auth needs."""
    match cfg.auth:
        case AuthBearer(token_env=name):
            return (name,)
        case AuthApiKey(value_env=name):
            return (name,)
        case _:
            return ()


def secret_from_env(env: Mapping[str, str], name: str) -> str:
    """Return the secret in ``env[name]``; errors name the variable, never its value."""
    value = env.get(name)
    if value is None:
        msg = f"environment variable {name} is not set"
        raise CredentialsError(msg)
    if not value.strip():
        msg = f"environment variable {name} is empty"
        raise CredentialsError(msg)
    if "\r" in value or "\n" in value:
        msg = f"environment variable {name} contains a line break"
        raise CredentialsError(msg)
    # A header value the HTTP client can always encode: printable ASCII, spaces inside only.
    if value != value.strip(" ") or not all(c == " " or "!" <= c <= "~" for c in value):
        msg = (
            f"environment variable {name} must contain only printable ASCII characters, "
            "with no leading or trailing whitespace"
        )
        raise CredentialsError(msg)
    return value


def check_credentials(cfg: GatewayConfig, env: Mapping[str, str]) -> None:
    """Ensure every required credential is present and usable."""
    errors: list[GatewayError] = []
    for backend in cfg.backends:
        for name in required_env_vars(backend):
            try:
                secret_from_env(env, name)
            except CredentialsError as exc:
                errors.append(
                    CredentialsError(exc.message, backend=backend.name, file=backend.config_path)
                )
    raise_collected(errors)

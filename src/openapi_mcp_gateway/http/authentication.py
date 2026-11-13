"""Turn a backend's auth configuration and the environment into request headers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from openapi_mcp_gateway.config.models import AuthApiKey, AuthBearer, AuthConfig
from openapi_mcp_gateway.config.validator import secret_from_env

if TYPE_CHECKING:
    from collections.abc import Mapping


def auth_headers(auth: AuthConfig, env: Mapping[str, str]) -> dict[str, str]:
    """Headers carrying the backend's credential; raises ``CredentialsError`` if unusable."""
    if isinstance(auth, AuthBearer):
        return {"Authorization": f"Bearer {secret_from_env(env, auth.token_env)}"}
    if isinstance(auth, AuthApiKey):
        return {auth.name: secret_from_env(env, auth.value_env)}
    return {}

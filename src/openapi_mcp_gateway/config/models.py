"""Configuration models.

The ``*File`` pydantic models mirror the YAML files and reject unknown keys.
``GatewayConfig`` and ``BackendConfig`` are the resolved, frozen result of
loading: defaults applied, relative paths made absolute.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    field_validator,
)

if TYPE_CHECKING:
    from pathlib import Path

ExposureMode = Literal["read_only", "allowlist", "all"]
DefaultExposureMode = Literal["read_only", "allowlist"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
LogFormat = Literal["text", "json"]

DEFAULT_SERVER_NAME = "OpenAPI MCP Gateway"
DEFAULT_MODE: ExposureMode = "read_only"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_FOLLOW_REDIRECTS = False
DEFAULT_MAX_RESPONSE_BYTES = 1048576
DEFAULT_MAX_CONNECTIONS = 10

# StrictFloat accepts integers too, but not booleans or strings; inf and nan are refused.
_PositiveFloat = Annotated[StrictFloat, Field(gt=0, allow_inf_nan=False)]
_PositiveInt = Annotated[StrictInt, Field(gt=0)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- authentication (part of the resolved config as well) -------------------


class AuthNone(_Strict):
    """``auth`` block for a backend that needs no credential."""

    type: Literal["none"]


class AuthBearer(_Strict):
    """``auth`` block for a bearer token read from an environment variable."""

    type: Literal["bearer"]
    token_env: str = Field(min_length=1)


# RFC 9110 section 5.6.2 ``token``: the characters a header field name may use.
_HEADER_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
# Headers that describe the connection or the message framing, not the request.
_RESERVED_HEADERS = frozenset({"host", "content-length", "transfer-encoding", "connection"})


class AuthApiKey(_Strict):
    """``auth`` block for an API key sent in a request header."""

    type: Literal["api_key"]
    location: Literal["header"]
    name: str = Field(min_length=1)
    value_env: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _header_name(cls, name: str) -> str:
        if not _HEADER_TOKEN.fullmatch(name):
            msg = "must be an HTTP header name: letters, digits and !#$%&'*+-.^_`|~ only"
            raise ValueError(msg)
        if name.lower() in _RESERVED_HEADERS:
            msg = "must not be Host, Content-Length, Transfer-Encoding or Connection"
            raise ValueError(msg)
        return name


AuthConfig = AuthNone | AuthBearer | AuthApiKey
AuthField = Annotated[AuthConfig, Field(discriminator="type")]


# --- file models ------------------------------------------------------------


class HttpFile(_Strict):
    """``http`` block; unset values fall through to defaults, then built-ins."""

    timeout_seconds: _PositiveFloat | None = None
    follow_redirects: StrictBool | None = None
    max_response_bytes: _PositiveInt | None = None
    max_connections: _PositiveInt | None = None


class ServerFile(_Strict):
    """``server`` block of ``gateway.yaml``."""

    name: str = Field(default=DEFAULT_SERVER_NAME, min_length=1)
    transport: Literal["stdio"] = "stdio"


class BackendsFile(_Strict):
    """``backends`` block of ``gateway.yaml``."""

    directory: str = Field(default="./backends", min_length=1)
    discovery: Literal["auto"] = "auto"


class DefaultExposureFile(_Strict):
    """``defaults.exposure`` block; ``all`` cannot be a default."""

    mode: DefaultExposureMode | None = None


class DefaultsFile(_Strict):
    """``defaults`` block of ``gateway.yaml``."""

    exposure: DefaultExposureFile = DefaultExposureFile()
    http: HttpFile = HttpFile()


class LoggingFile(_Strict):
    """``logging`` block of ``gateway.yaml``."""

    level: LogLevel = "INFO"
    format: LogFormat = "text"


class GatewayFile(_Strict):
    """``gateway.yaml`` as written, before defaults are resolved."""

    version: Literal[1]
    server: ServerFile = ServerFile()
    backends: BackendsFile = BackendsFile()
    defaults: DefaultsFile = DefaultsFile()
    logging: LoggingFile = LoggingFile()


class ExcludeFile(_Strict):
    """``exposure.exclude`` block of ``backend.yaml``."""

    operations: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()


class ExposureFile(_Strict):
    """``exposure`` block of ``backend.yaml``."""

    mode: ExposureMode | None = None
    operations: tuple[str, ...] = ()
    exclude: ExcludeFile = ExcludeFile()


class BackendFile(_Strict):
    """``backend.yaml`` as written, before defaults are resolved."""

    name: str
    schema_: str = Field(alias="schema", min_length=1)
    base_url: str
    auth: AuthField = AuthNone(type="none")
    exposure: ExposureFile = ExposureFile()
    http: HttpFile = HttpFile()


# --- resolved config ----------------------------------------------------------


@dataclass(frozen=True)
class BackendConfig:
    """One backend with every default resolved and every path absolute."""

    name: str
    directory: Path
    config_path: Path
    schema_path: Path
    base_url: str
    auth: AuthConfig
    mode: ExposureMode
    operations: tuple[str, ...]
    exclude_operations: tuple[str, ...]
    exclude_paths: tuple[str, ...]
    timeout_seconds: float
    follow_redirects: bool
    max_response_bytes: int
    max_connections: int


@dataclass(frozen=True)
class GatewayConfig:
    """The whole gateway configuration, resolved: server, logging and backends."""

    config_path: Path
    server_name: str
    backends_directory: Path
    log_level: LogLevel
    log_format: LogFormat
    backends: tuple[BackendConfig, ...]

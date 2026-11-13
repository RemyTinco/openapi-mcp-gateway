"""Load ``gateway.yaml`` and the backends it discovers into resolved config."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from openapi_mcp_gateway.config._errors import raise_collected
from openapi_mcp_gateway.config.models import (
    DEFAULT_FOLLOW_REDIRECTS,
    DEFAULT_MAX_CONNECTIONS,
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_MODE,
    DEFAULT_TIMEOUT_SECONDS,
    BackendConfig,
    BackendFile,
    GatewayConfig,
    GatewayFile,
)
from openapi_mcp_gateway.exceptions import ConfigError, GatewayError

BACKEND_FILE_NAME = "backend.yaml"


def _absolute(path: Path) -> Path:
    """Absolute, normalised path; symlinks are left alone."""
    return Path(os.path.normpath(Path.cwd() / path))


def _field_name(loc: tuple[int | str, ...]) -> str | None:
    parts = [str(p) for p in loc]
    # Discriminated unions put the tag in the location (auth.bearer.token_env).
    if parts[:1] == ["auth"] and parts[2:]:
        del parts[1]
    return ".".join(parts) or None


class _DuplicateKeyError(yaml.YAMLError):
    def __init__(self, key: object, line: int) -> None:
        super().__init__(f"duplicate key {key!r} (line {line})")
        self.key = key
        self.line = line


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe loader that refuses a mapping key written twice, at any depth.

    PyYAML keeps the last value of a repeated key, so a second ``exclude:`` block
    would silently discard the first. YAML merge keys (``<<``) are not duplicates.
    """

    def construct_mapping(  # PyYAML's signature
        self,
        node: yaml.MappingNode,
        deep: bool = False,  # noqa: FBT001, FBT002
    ) -> dict[Any, Any]:
        seen: set[object] = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = self.construct_object(key_node, deep=True)
            try:
                is_duplicate = key in seen
                seen.add(key)
            except TypeError:  # unhashable key; SafeConstructor reports it
                continue
            if is_duplicate:
                raise _DuplicateKeyError(key, key_node.start_mark.line + 1)
        return super().construct_mapping(node, deep=deep)


def _yaml_problem(exc: yaml.YAMLError) -> str:
    """Return the parser's problem and position, without the source snippet PyYAML would add.

    A malformed file may contain a pasted secret, so its text is never echoed.
    """
    if isinstance(exc, _DuplicateKeyError):
        return str(exc)
    if isinstance(exc, yaml.MarkedYAMLError):
        problem = exc.problem or exc.context or "malformed document"
        mark = exc.problem_mark or exc.context_mark
        if mark is not None:
            return f"{problem} (line {mark.line + 1}, column {mark.column + 1})"
        return problem
    return "malformed document"


def _parse[M: BaseModel](model: type[M], path: Path, backend: str | None = None) -> M:
    """Read a YAML mapping and validate it; raises ConfigError / MultipleErrors."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"cannot read file: {exc.__class__.__name__}"
        raise ConfigError(msg, backend=backend, file=path) from exc
    try:
        data: Any = yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 - a SafeLoader subclass
    except _DuplicateKeyError as exc:
        raise ConfigError(_yaml_problem(exc), backend=backend, file=path) from None
    except yaml.YAMLError as exc:
        msg = f"invalid YAML: {_yaml_problem(exc)}"
        raise ConfigError(msg, backend=backend, file=path) from None
    if not isinstance(data, dict):
        msg = "file must contain a YAML mapping"
        raise ConfigError(msg, backend=backend, file=path)
    if "auth" in data and data["auth"] == {}:
        data["auth"] = {"type": "none"}
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        # Messages only, never the offending input, which may be sensitive.
        errors = [
            ConfigError(e["msg"], backend=backend, file=path, field=_field_name(e["loc"]))
            for e in exc.errors()
        ]
        raise_collected(errors)
        msg = "unreachable"
        raise AssertionError(msg) from exc  # pragma: no cover


def _discover(directory: Path) -> list[Path]:
    """Immediate subdirectories holding a backend.yaml, sorted by directory name."""
    children = sorted((p for p in directory.iterdir() if p.is_dir()), key=lambda p: p.name)
    return [c / BACKEND_FILE_NAME for c in children if (c / BACKEND_FILE_NAME).is_file()]


def _resolve_backend(gateway: GatewayFile, raw: BackendFile, config_path: Path) -> BackendConfig:
    directory = config_path.parent
    http, default_http = raw.http, gateway.defaults.http

    def pick[T](own: T | None, inherited: T | None, builtin: T) -> T:
        if own is not None:
            return own
        return inherited if inherited is not None else builtin

    return BackendConfig(
        name=raw.name,
        directory=directory,
        config_path=config_path,
        schema_path=_absolute(directory / raw.schema_),
        base_url=raw.base_url,
        auth=raw.auth,
        mode=pick(raw.exposure.mode, gateway.defaults.exposure.mode, DEFAULT_MODE),
        operations=raw.exposure.operations,
        exclude_operations=raw.exposure.exclude.operations,
        exclude_paths=raw.exposure.exclude.paths,
        timeout_seconds=float(
            pick(http.timeout_seconds, default_http.timeout_seconds, DEFAULT_TIMEOUT_SECONDS)
        ),
        follow_redirects=pick(
            http.follow_redirects, default_http.follow_redirects, DEFAULT_FOLLOW_REDIRECTS
        ),
        max_response_bytes=pick(
            http.max_response_bytes, default_http.max_response_bytes, DEFAULT_MAX_RESPONSE_BYTES
        ),
        max_connections=pick(
            http.max_connections, default_http.max_connections, DEFAULT_MAX_CONNECTIONS
        ),
    )


def load_gateway_config(path: Path) -> GatewayConfig:
    """Load gateway.yaml and every discovered backend; all problems are reported together."""
    config_path = _absolute(path)
    gateway = _parse(GatewayFile, config_path)

    backends_dir = _absolute(config_path.parent / gateway.backends.directory)
    if not backends_dir.is_dir():
        msg = f"backends directory does not exist: {backends_dir}"
        raise ConfigError(
            msg,
            file=config_path,
            field="backends.directory",
        )

    backends: list[BackendConfig] = []
    errors: list[GatewayError] = []
    for backend_path in _discover(backends_dir):
        try:
            raw = _parse(BackendFile, backend_path, backend=backend_path.parent.name)
        except GatewayError as exc:
            errors.append(exc)
            continue
        backends.append(_resolve_backend(gateway, raw, backend_path))
    raise_collected(errors)

    return GatewayConfig(
        config_path=config_path,
        server_name=gateway.server.name,
        backends_directory=backends_dir,
        log_level=gateway.logging.level,
        log_format=gateway.logging.format,
        backends=tuple(backends),
    )

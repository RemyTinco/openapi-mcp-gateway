"""Preparation pipeline: from a resolved configuration to filtered, named operations."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from openapi_mcp_gateway.config._errors import raise_collected
from openapi_mcp_gateway.config.validator import check_credentials, validate_config
from openapi_mcp_gateway.exceptions import ConfigError, GatewayError, SchemaError
from openapi_mcp_gateway.logging import log_event
from openapi_mcp_gateway.openapi.filter import filter_document, select_operations
from openapi_mcp_gateway.openapi.loader import load_document
from openapi_mcp_gateway.openapi.operations import (
    Operation,
    build_tool_names,
    extract_operations,
)
from openapi_mcp_gateway.openapi.validator import validate_document

if TYPE_CHECKING:
    from collections.abc import Mapping

    from openapi_mcp_gateway.config.models import BackendConfig, GatewayConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedBackend:
    """One backend ready to serve: its filtered document and named operations."""

    config: BackendConfig
    openapi_version: str
    document: dict[str, Any]  # filtered
    operations: tuple[Operation, ...]  # selected
    tool_names: dict[str, str]  # operationId -> tool name
    warnings: tuple[str, ...]
    # Every path template of the unfiltered document, hidden ones included.
    all_paths: tuple[str, ...] = ()


def _prepare_backend(cfg: BackendConfig) -> PreparedBackend:
    path = cfg.schema_path
    document = load_document(path, backend=cfg.name)
    version = validate_document(document, backend=cfg.name, path=path)
    operations = extract_operations(document, backend=cfg.name, path=path)
    selection = select_operations(operations, cfg)
    tool_names = build_tool_names(cfg.name, selection.operations, path=path)
    warnings = list(selection.warnings)
    if not selection.operations:
        warnings.append(f"[{cfg.name}] no tools exposed by this backend")
    log_event(
        logger,
        logging.DEBUG,
        "backend.prepared",
        name=cfg.name,
        openapi=version,
        tools=len(tool_names),
    )
    return PreparedBackend(
        config=cfg,
        openapi_version=version,
        document=filter_document(document, selection.operations),
        operations=selection.operations,
        tool_names=tool_names,
        warnings=tuple(warnings),
        all_paths=tuple(t for t in (document.get("paths") or {}) if isinstance(t, str)),
    )


def _name_collisions(prepared: list[PreparedBackend]) -> list[GatewayError]:
    owner: dict[str, PreparedBackend] = {}
    errors: list[GatewayError] = []
    for backend in prepared:
        for name in backend.tool_names.values():
            first = owner.setdefault(name, backend)
            if first is not backend:
                errors.append(
                    SchemaError(
                        f"tool name {name!r} is produced by both backend "
                        f"{first.config.name!r} and backend {backend.config.name!r}; "
                        "rename a backend or an operationId, or exclude one operation",
                        backend=backend.config.name,
                        file=backend.config.schema_path,
                    )
                )
    return errors


def prepare_all(cfg: GatewayConfig, *, require_tools: bool = True) -> list[PreparedBackend]:
    """Prepare every backend, collecting errors from all of them before raising.

    With ``require_tools`` (the default), no tool across all backends is an error.
    """
    prepared: list[PreparedBackend] = []
    errors: list[GatewayError] = []
    for backend in cfg.backends:
        try:
            prepared.append(_prepare_backend(backend))
        except GatewayError as exc:
            errors.append(exc)
    errors.extend(_name_collisions(prepared))
    raise_collected(errors)
    if require_tools and not any(p.tool_names for p in prepared):
        msg = "no tools exposed by any backend"
        raise ConfigError(msg, file=cfg.config_path)
    return prepared


def prepare_checked(cfg: GatewayConfig, env: Mapping[str, str]) -> list[PreparedBackend]:
    """Validate, prepare and check credentials, reporting every error together.

    Config, schema and credential errors are collected; the lowest exit code wins.
    """
    errors: list[GatewayError] = []
    prepared: list[PreparedBackend] = []
    try:
        validate_config(cfg)
        prepared = prepare_all(cfg)
    except GatewayError as exc:
        errors.append(exc)
    try:
        check_credentials(cfg, env)
    except GatewayError as exc:
        errors.append(exc)
    raise_collected(errors)
    return prepared

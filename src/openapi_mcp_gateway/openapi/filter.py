"""Exposure filter: decide which operations a backend exposes and prune the document."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

from openapi_mcp_gateway.exceptions import ConfigError
from openapi_mcp_gateway.openapi.operations import HTTP_METHODS, Operation

if TYPE_CHECKING:
    from collections.abc import Sequence

    from openapi_mcp_gateway.config.models import BackendConfig

_READ_ONLY_METHODS = frozenset({"GET", "HEAD"})


def _segments(path: str) -> list[str]:
    # "/a/b/", "/a/b" and "//a//b" are the same location: drop empty segments.
    return [segment for segment in path.split("/") if segment]


def _segment_matches(pattern: str, segment: str) -> bool:
    # Only '*' is a wildcard. '?' and '[' would be fnmatch syntax, so make them
    # literal; a pattern must never match more than its author wrote.
    return fnmatchcase(segment, pattern.replace("[", "[[]").replace("?", "[?]"))


def path_matches(pattern: str, path: str) -> bool:
    """Segment-aware, case-sensitive glob match against an OpenAPI path template.

    Each pattern segment matches exactly one path segment; ``*`` is the only
    wildcard. A final ``*`` segment matches the prefix itself and everything below.
    """
    pat = _segments(pattern)
    parts = _segments(path)
    if pat and pat[-1] == "*":
        prefix = pat[:-1]
        if len(parts) < len(prefix):
            return False
        return all(_segment_matches(p, s) for p, s in zip(prefix, parts, strict=False))
    if len(pat) != len(parts):
        return False
    return all(_segment_matches(p, s) for p, s in zip(pat, parts, strict=True))


@dataclass(frozen=True)
class Selection:
    """The operations a backend exposes, and warnings about its exclusions."""

    operations: tuple[Operation, ...]
    warnings: tuple[str, ...]


def _check_exclude_paths(cfg: BackendConfig) -> None:
    """Reject patterns that could silently fail to hide what the author meant to hide."""
    for pattern in cfg.exclude_paths:
        if not pattern.startswith("/"):
            rule = "must start with '/'"
        elif "**" in pattern.split("/"):
            rule = "'**' is not supported; use a trailing '/*' to match a prefix and below"
        else:
            continue
        msg = f"invalid exclude path pattern {pattern!r}: {rule}"
        raise ConfigError(
            msg,
            backend=cfg.name,
            file=cfg.config_path,
            field="exposure.exclude.paths",
        )


def select_operations(ops: Sequence[Operation], cfg: BackendConfig) -> Selection:
    """Apply exposure mode, then operation exclusions, then path exclusions."""
    _check_exclude_paths(cfg)
    warnings: list[str] = []
    known_ids = {op.operation_id for op in ops if op.operation_id is not None}

    if cfg.mode == "read_only":
        selected = [op for op in ops if op.method in _READ_ONLY_METHODS]
    elif cfg.mode == "allowlist":
        unknown = [name for name in cfg.operations if name not in known_ids]
        if unknown:
            raise ConfigError(
                "allowlist names operationIds not found in the schema: "
                + ", ".join(repr(n) for n in unknown),
                backend=cfg.name,
                file=cfg.config_path,
                field="exposure.operations",
            )
        wanted = set(cfg.operations)
        selected = [op for op in ops if op.operation_id in wanted]
    else:
        selected = list(ops)

    warnings.extend(
        f"[{cfg.name}] exposure.exclude.operations: {name!r} matches no operation in the schema"
        for name in cfg.exclude_operations
        if name not in known_ids
    )
    warnings.extend(
        f"[{cfg.name}] exposure.exclude.paths: {pattern!r} matches no operation in the schema"
        for pattern in cfg.exclude_paths
        if not any(path_matches(pattern, op.path) for op in ops)
    )
    excluded_ids = set(cfg.exclude_operations)
    selected = [op for op in selected if op.operation_id not in excluded_ids]
    selected = [
        op
        for op in selected
        if not any(path_matches(pattern, op.path) for pattern in cfg.exclude_paths)
    ]
    return Selection(tuple(selected), tuple(warnings))


def filter_document(doc: dict[str, Any], selected: Sequence[Operation]) -> dict[str, Any]:
    """Return a deep copy keeping only selected operations, with ``servers`` removed."""
    keep = {(op.method.lower(), op.path) for op in selected}
    out = copy.deepcopy(doc)
    out.pop("servers", None)
    paths = out.get("paths")
    if not isinstance(paths, dict):
        return out
    pruned: dict[str, Any] = {}
    for template, item in paths.items():
        if not isinstance(item, dict) or "$ref" in item:
            continue
        new_item = {
            key: value
            for key, value in item.items()
            if key != "servers" and (key not in HTTP_METHODS or (key, template) in keep)
        }
        if not any(key in HTTP_METHODS for key in new_item):
            continue
        for method in HTTP_METHODS:
            operation = new_item.get(method)
            if isinstance(operation, dict):
                operation.pop("servers", None)
        pruned[template] = new_item
    out["paths"] = pruned
    return out

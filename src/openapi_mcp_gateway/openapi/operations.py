"""Extract operations from a validated OpenAPI document and compute MCP tool names."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from openapi_mcp_gateway.exceptions import SchemaError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

HTTP_METHODS: tuple[str, ...] = (
    "get",
    "head",
    "post",
    "put",
    "patch",
    "delete",
    "options",
    "trace",
)

# FastMCP truncates component names at 56 characters and silently suffixes
# collisions; MCP clients cap tool names at 64 characters. Reject instead.
MAX_SLUG_LENGTH = 56
MAX_TOOL_NAME_LENGTH = 64

_INVALID_CHARS = re.compile(r"[^A-Za-z0-9_]")
_UNDERSCORE_RUNS = re.compile(r"_+")


@dataclass(frozen=True)
class Operation:
    """One HTTP method on one path template of an OpenAPI document."""

    operation_id: str | None
    method: str  # upper case
    path: str  # OpenAPI template, e.g. "/pets/{id}"


def extract_operations(doc: dict[str, Any], *, backend: str, path: Path) -> list[Operation]:
    """Return operations in document order; a missing operationId yields ``None``."""
    ops: list[Operation] = []
    seen: dict[str, str] = {}
    paths = doc.get("paths") or {}
    for template, item in paths.items():
        # A $ref path item has no operations (filter_document drops it too).
        if not isinstance(item, dict) or "$ref" in item:
            continue
        for method in HTTP_METHODS:
            operation = item.get(method)
            if not isinstance(operation, dict):
                continue
            label = f"{method.upper()} {template}"
            operation_id = operation.get("operationId")
            if operation_id is not None or "operationId" in operation:
                if not isinstance(operation_id, str) or not operation_id:
                    msg = f"{label}: operationId must be a non-empty string"
                    raise SchemaError(
                        msg,
                        backend=backend,
                        file=path,
                    )
                if operation_id in seen:
                    msg = (
                        f"duplicate operationId {operation_id!r}: {seen[operation_id]} and "
                        f"{label}; operationIds must be unique"
                    )
                    raise SchemaError(
                        msg,
                        backend=backend,
                        file=path,
                    )
                seen[operation_id] = label
            ops.append(Operation(operation_id, method.upper(), template))
    return ops


def _resolve(doc: dict[str, Any], node: Any) -> Any:
    """Follow internal ``$ref`` pointers (``#/a/b``); anything unresolvable becomes ``None``."""
    for _ in range(32):  # a reference cycle cannot loop forever
        if not isinstance(node, dict) or "$ref" not in node:
            return node
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return None
        node = doc
        for escaped in ref[2:].split("/"):
            token = escaped.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or token not in node:
                return None
            node = node[token]
    return None


def path_parameter_names(doc: dict[str, Any], op: Operation) -> tuple[str, ...]:
    """Names of the operation's ``in: path`` parameters, path-item level included.

    ``$ref`` parameters are resolved against the document (``components.parameters``).
    """
    item = _resolve(doc, (doc.get("paths") or {}).get(op.path))
    if not isinstance(item, dict):
        return ()
    operation = _resolve(doc, item.get(op.method.lower()))
    declared: list[Any] = []
    for owner in (item, operation):
        if isinstance(owner, dict) and isinstance(owner.get("parameters"), list):
            declared.extend(_resolve(doc, p) for p in owner["parameters"])
    names: list[str] = []
    for parameter in declared:
        if not isinstance(parameter, dict) or parameter.get("in") != "path":
            continue
        name = parameter.get("name")
        if isinstance(name, str) and name not in names:
            names.append(name)
    return tuple(names)


def slug(operation_id: str) -> str:
    """Normalise an operationId to the name FastMCP will register for it.

    Every character outside ASCII ``[A-Za-z0-9_]`` becomes ``_``, runs of ``_``
    collapse to one, and leading/trailing ``_`` are stripped; case and leading
    digits are kept. FastMCP's OpenAPI provider slugifies the names we pass it
    the same way, so for any non-empty result it registers exactly this name.
    """
    return _UNDERSCORE_RUNS.sub("_", _INVALID_CHARS.sub("_", operation_id)).strip("_")


def tool_name(backend: str, operation_id: str) -> str:
    """Return the MCP tool name: ``<backend>_<slug of the operationId>``."""
    return f"{backend}_{slug(operation_id)}"


def build_tool_names(backend: str, ops: Sequence[Operation], *, path: Path) -> dict[str, str]:
    """Map operationId to tool name for exposed operations, rejecting unsafe names."""
    names: dict[str, str] = {}
    owner: dict[str, str] = {}
    for op in ops:
        if op.operation_id is None:
            msg = (
                f"{op.method} {op.path} has no operationId; add one in the schema "
                "or exclude the operation"
            )
            raise SchemaError(
                msg,
                backend=backend,
                file=path,
            )
        operation_id = op.operation_id
        if not slug(operation_id):
            msg = (
                f"operationId {operation_id!r} has no letters, digits or inner underscores "
                "to build a tool name from; exclude the operation or rename its operationId"
            )
            raise SchemaError(
                msg,
                backend=backend,
                file=path,
            )
        if len(slug(operation_id)) > MAX_SLUG_LENGTH:
            msg = (
                f"operationId {operation_id!r} is longer than {MAX_SLUG_LENGTH} characters "
                "after normalisation; exclude the operation or rename its operationId"
            )
            raise SchemaError(
                msg,
                backend=backend,
                file=path,
            )
        name = tool_name(backend, operation_id)
        if len(name) > MAX_TOOL_NAME_LENGTH:
            msg = (
                f"tool name {name!r} for operationId {operation_id!r} is "
                f"{len(name)} characters, over the {MAX_TOOL_NAME_LENGTH} limit; "
                "exclude the operation or rename its operationId"
            )
            raise SchemaError(
                msg,
                backend=backend,
                file=path,
            )
        if name in owner:
            msg = (
                f"operationIds {owner[name]!r} and {operation_id!r} both produce tool "
                f"name {name!r}; exclude one operation or rename its operationId"
            )
            raise SchemaError(
                msg,
                backend=backend,
                file=path,
            )
        owner[name] = operation_id
        names[operation_id] = name
    return names

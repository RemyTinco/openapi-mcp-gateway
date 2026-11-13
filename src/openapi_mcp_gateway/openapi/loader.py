"""Read one local OpenAPI file into a plain ``dict`` with string-only keys."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import yaml

from openapi_mcp_gateway.exceptions import SchemaError

if TYPE_CHECKING:
    from pathlib import Path


class _Loader(yaml.SafeLoader):
    """Safe YAML loader that keeps timestamps as strings, as JSON would."""


_Loader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _key(key: Any) -> str:
    """Render a mapping key the way JSON would."""
    if isinstance(key, str):
        return key
    if isinstance(key, bool):
        return "true" if key else "false"
    if key is None:
        return "null"
    return str(key)


def _join(path: tuple[str, ...]) -> str | None:
    return ".".join(path) or None


def _normalise(
    node: Any,
    path: tuple[str, ...],
    active: set[int],
    done: dict[int, Any],
    fail: Any,
) -> Any:
    """Copy ``node`` converting mapping keys to strings; YAML aliases stay shared."""
    if not isinstance(node, dict | list):
        return node
    ident = id(node)
    if ident in active:
        fail("document contains a recursive YAML alias", _join(path))
    if ident in done:
        return done[ident]
    active.add(ident)
    result: Any
    if isinstance(node, list):
        result = [
            _normalise(item, (*path, str(i)), active, done, fail) for i, item in enumerate(node)
        ]
    else:
        result = {}
        for raw_key, value in node.items():
            key = _key(raw_key)
            if key in result:
                fail(f"duplicate mapping key {key!r} after converting keys to strings", _join(path))
            result[key] = _normalise(value, (*path, key), active, done, fail)
    active.discard(ident)
    done[ident] = result
    return result


def load_document(path: Path, *, backend: str) -> dict[str, Any]:
    """Load ``path`` as JSON, falling back to YAML; the root must be a mapping."""

    def fail(message: str, field: str | None = None) -> Any:
        raise SchemaError(message, backend=backend, file=path, field=field)

    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        fail("file is not valid UTF-8")
    except OSError as error:
        fail(f"cannot read file: {error.strerror or type(error).__name__}")

    try:
        raw = _parse(text, fail)
    except RecursionError:
        fail("document is nested too deeply")

    if not isinstance(raw, dict):
        fail("document root must be a mapping (JSON object or YAML mapping)")
    try:
        doc: dict[str, Any] = _normalise(raw, (), set(), {}, fail)
    except RecursionError:
        fail("document is nested too deeply")
    return doc


def _parse(text: str, fail: Any) -> Any:
    json_error: json.JSONDecodeError
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        json_error = error
    try:
        return yaml.load(text, Loader=_Loader)  # noqa: S506 - a SafeLoader subclass
    except yaml.YAMLError as yaml_error:
        if text.lstrip().startswith(("{", "[")):
            fail(
                f"invalid JSON at line {json_error.lineno}, column {json_error.colno}: "
                f"{json_error.msg}"
            )
        mark = yaml_error.problem_mark if isinstance(yaml_error, yaml.MarkedYAMLError) else None
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        fail(f"invalid YAML{where}")

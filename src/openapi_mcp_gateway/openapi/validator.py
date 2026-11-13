"""Check that a loaded document is a self-contained OpenAPI 3.0.x or 3.1.x document."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from openapi_spec_validator import validate
from openapi_spec_validator.exceptions import OpenAPISpecValidatorError
from openapi_spec_validator.validation.exceptions import (
    DuplicateOperationIDError,
    ExtraParametersError,
    OpenAPIValidationError,
    ParameterDuplicateError,
    UnresolvableParameterError,
)

from openapi_mcp_gateway.exceptions import SchemaError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_SUPPORTED = "only OpenAPI 3.0.x and 3.1.x are supported"
_VERSION = re.compile(r"3\.[01]\.\d+")
_SAFE_TO_ECHO = re.compile(r"[\w.\-]{1,20}")
_MAX_REF_ECHO = 200
_MAX_EXPANDED_NODES = 500_000

_SEMANTIC_MESSAGES: dict[type[OpenAPIValidationError], str] = {
    DuplicateOperationIDError: "duplicate operationId",
    ParameterDuplicateError: "duplicate parameter",
    ExtraParametersError: "path parameter missing from the path template or not declared",
    UnresolvableParameterError: "parameter reference cannot be resolved",
}


def validate_document(doc: dict[str, Any], *, backend: str, path: Path) -> str:
    """Return the ``openapi`` version of ``doc``; raise ``SchemaError`` on the first problem."""

    def fail(message: str, field: str | None) -> SchemaError:
        return SchemaError(message, backend=backend, file=path, field=field)

    version = _check_version(doc, fail)
    _check_size(doc, fail)
    _check_refs(doc, fail)
    try:
        validate(doc)
    except OpenAPIValidationError as error:
        raise fail(_describe(error), _join(error.absolute_path)) from None
    except OpenAPISpecValidatorError as error:
        msg = f"document is not a valid OpenAPI document ({type(error).__name__})"
        raise fail(msg, None) from None
    # The validator must never leak a traceback or document content.
    except Exception as error:  # noqa: BLE001
        msg = f"OpenAPI validation failed ({type(error).__name__})"
        raise fail(msg, None) from None
    return version


def _check_version(doc: dict[str, Any], fail: Any) -> str:
    if "openapi" not in doc:
        if "swagger" in doc:
            msg = f"Swagger {_echo(doc['swagger'])} is not supported; {_SUPPORTED}"
            raise fail(msg, "swagger")
        msg = f"missing required 'openapi' field; {_SUPPORTED}"
        raise fail(msg, "openapi")
    value = doc["openapi"]
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        msg = f"unsupported openapi version {_echo(value)}; {_SUPPORTED}"
        raise fail(msg, "openapi")
    return value


def _echo(value: Any) -> str:
    """Show a version-like value; anything else could be arbitrary document content."""
    text = str(value) if isinstance(value, str | int | float) else ""
    return text if _SAFE_TO_ECHO.fullmatch(text) else "(unrecognised value)"


def _join(parts: Any) -> str | None:
    return ".".join(str(p) for p in parts) or None


def _walk(node: Any) -> Iterator[tuple[tuple[str, ...], dict[str, Any]]]:
    """Yield (path, mapping) for every mapping in the document, depth first."""
    stack: list[tuple[tuple[str, ...], Any]] = [((), node)]
    seen: set[int] = set()  # YAML aliases share containers; visit each once
    while stack:
        path, current = stack.pop()
        if isinstance(current, dict | list):
            if id(current) in seen:
                continue
            seen.add(id(current))
        if isinstance(current, dict):
            yield path, current
            stack.extend(((*path, k), v) for k, v in current.items())
        elif isinstance(current, list):
            stack.extend(((*path, str(i)), v) for i, v in enumerate(current))


def _check_size(doc: dict[str, Any], fail: Any) -> None:
    """Reject documents whose alias-expanded tree is huge (alias bombs)."""
    sizes: dict[int, int] = {}
    stack: list[tuple[Any, bool]] = [(doc, False)]
    while stack:
        node, children_done = stack.pop()
        if not isinstance(node, dict | list) or (id(node) in sizes and not children_done):
            continue
        children = list(node.values()) if isinstance(node, dict) else node
        if not children_done:
            stack.append((node, True))
            stack.extend((c, False) for c in children if isinstance(c, dict | list))
            continue
        total = 1 + sum(sizes[id(c)] if isinstance(c, dict | list) else 1 for c in children)
        sizes[id(node)] = total
        if total > _MAX_EXPANDED_NODES:
            msg = (
                f"document expands to more than {_MAX_EXPANDED_NODES} nodes "
                "(excessive YAML alias use)"
            )
            raise fail(
                msg,
                None,
            )


def _check_refs(doc: dict[str, Any], fail: Any) -> None:
    refs = [
        (path, node["$ref"])
        for path, node in _walk(doc)
        if "$ref" in node and isinstance(node["$ref"], str)
    ]
    for path, ref in refs:
        if not ref.startswith("#"):
            msg = (
                "external $ref is not supported: only references starting with '#' are allowed; "
                "bundle the document into a single file first"
            )
            raise fail(
                msg,
                _join(path),
            )
    for path, ref in refs:
        if not _resolves(doc, ref[1:]):
            msg = f"$ref {ref[:_MAX_REF_ECHO]!r} does not resolve"
            raise fail(msg, _join(path))


def _resolves(doc: Any, fragment: str) -> bool:
    """Follow a JSON pointer (RFC 6901, URI-fragment encoded) from the root."""
    pointer = unquote(fragment)
    if pointer == "":
        return True
    if not pointer.startswith("/"):
        return False
    node = doc
    for escaped in pointer[1:].split("/"):
        token = escaped.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and token in node:
            node = node[token]
        elif (
            isinstance(node, list)
            and token.isascii()
            and token.isdecimal()
            and str(int(token)) == token
            and int(token) < len(node)
        ):
            node = node[int(token)]
        else:
            return False
    return True


def _describe(error: OpenAPIValidationError) -> str:
    """One line naming the violated rule, never the offending value."""
    for kind, message in _SEMANTIC_MESSAGES.items():
        if isinstance(error, kind):
            return message
    rule = error.validator
    expected = error.validator_value
    if rule == "type":
        kinds = expected if isinstance(expected, list) else [expected]
        return f"expected type {' or '.join(str(k) for k in kinds)}"
    if rule == "required" and isinstance(expected, list):
        present = error.instance if isinstance(error.instance, dict) else {}
        missing = [str(name) for name in expected if name not in present]
        return f"missing required property {', '.join(repr(m) for m in missing)}"
    if rule == "additionalProperties":
        return "unexpected additional property"
    if isinstance(rule, str):
        return f"violates schema rule '{rule}'"
    return "document is not a valid OpenAPI document"

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from openapi_mcp_gateway.exceptions import SchemaError
from openapi_mcp_gateway.openapi.loader import load_document
from openapi_mcp_gateway.openapi.operations import (
    HTTP_METHODS,
    Operation,
    build_tool_names,
    extract_operations,
    path_parameter_names,
    slug,
    tool_name,
)

FIX = Path(__file__).parents[2] / "fixtures" / "schemas"
SPEC = Path("spec.yaml")


def _doc(paths: dict[str, Any]) -> dict[str, Any]:
    return {"openapi": "3.0.3", "paths": paths}


def _extract(paths: dict[str, Any]) -> list[Operation]:
    return extract_operations(_doc(paths), backend="b", path=SPEC)


def test_extract_order_and_methods() -> None:
    ops = _extract(
        {
            "/pets": {
                "post": {"operationId": "pets_create"},
                "get": {"operationId": "pets_list"},
            },
            "/pets/{id}": {"get": {"operationId": "pets_read"}},
        }
    )
    assert [(o.method, o.path) for o in ops] == [
        ("GET", "/pets"),
        ("POST", "/pets"),
        ("GET", "/pets/{id}"),
    ]
    assert [o.operation_id for o in ops] == ["pets_list", "pets_create", "pets_read"]


def test_http_methods_constant() -> None:
    assert HTTP_METHODS == ("get", "head", "post", "put", "patch", "delete", "options", "trace")


def test_path_level_keys_are_not_operations() -> None:
    ops = _extract(
        {
            "/pets": {
                "summary": "s",
                "description": "d",
                "servers": [{"url": "http://x"}],
                "parameters": [{"name": "a", "in": "query"}],
                "x-extension": {"get": {}},
                "get": {"operationId": "pets_list"},
            }
        }
    )
    assert [(o.method, o.path) for o in ops] == [("GET", "/pets")]


def test_ref_path_item_has_no_operations() -> None:
    assert _extract({"/x": {"$ref": "#/components/pathItems/X"}}) == []


def test_missing_paths_key() -> None:
    assert extract_operations({"openapi": "3.0.3"}, backend="b", path=SPEC) == []


def test_missing_operation_id_extracts_as_none() -> None:
    ops = _extract({"/pets/{id}": {"get": {}}})
    assert ops == [Operation(operation_id=None, method="GET", path="/pets/{id}")]


@pytest.mark.parametrize("bad", ["", 5, None, ["a"]])
def test_invalid_operation_id_value(bad: object) -> None:
    with pytest.raises(SchemaError, match=r"POST /pets.*operationId") as exc:
        _extract({"/pets": {"post": {"operationId": bad}}})
    assert exc.value.backend == "b"
    assert exc.value.file == SPEC


def test_duplicate_operation_id() -> None:
    with pytest.raises(SchemaError, match=r"pets_list.*GET /pets.*GET /animals"):
        _extract(
            {
                "/pets": {"get": {"operationId": "pets_list"}},
                "/animals": {"get": {"operationId": "pets_list"}},
            }
        )


def test_extract_from_petstore_fixture() -> None:
    doc = load_document(FIX / "petstore-3.0.yaml", backend="b")
    ops = extract_operations(doc, backend="b", path=FIX / "petstore-3.0.yaml")
    assert ops
    assert all(o.method.isupper() and o.path.startswith("/") for o in ops)


def test_tool_name() -> None:
    assert tool_name("catalog", "products_list") == "catalog_products_list"


def test_slug_replaces_invalid_chars() -> None:
    assert slug("pets.list-all v2") == "pets_list_all_v2"


@pytest.mark.parametrize(
    ("operation_id", "expected"),
    [
        ("items--list", "items_list"),
        ("a__b", "a_b"),
        ("_private_op_", "private_op"),
        ("--x--", "x"),
        ("Mixed.Case-Id", "Mixed_Case_Id"),  # case kept, as FastMCP does
        ("v2/items:list", "v2_items_list"),
        ("2fa_verify", "2fa_verify"),  # leading digit kept, as FastMCP does
        ("caf\u00e9_list", "caf_list"),  # non-ASCII is outside [A-Za-z0-9_]
        ("a . b", "a_b"),
    ],
)
def test_slug_collapses_and_strips_underscores(operation_id: str, expected: str) -> None:
    assert slug(operation_id) == expected


@pytest.mark.parametrize("operation_id", ["---", "_", "__", "...", "\u00e9\u00e9"])
def test_empty_slug_rejected(operation_id: str) -> None:
    with pytest.raises(SchemaError) as exc:
        build_tool_names("b", [Operation(operation_id, "GET", "/a")], path=SPEC)
    assert repr(operation_id) in exc.value.message
    assert "exclude" in exc.value.message
    assert "rename" in exc.value.message


def test_names_colliding_after_underscore_collapse() -> None:
    ops = [Operation("pets--list", "GET", "/a"), Operation("pets_list", "GET", "/b")]
    with pytest.raises(SchemaError, match=r"pets--list.*pets_list.*b_pets_list"):
        build_tool_names("b", ops, path=SPEC)


def test_slug_length_limit_applies_after_collapse() -> None:
    op_id = "_" + "a" * 28 + "--" + "a" * 27 + "_"  # 59 raw characters, 56 after slug
    assert len(slug(op_id)) == 56
    assert build_tool_names("b", [Operation(op_id, "GET", "/a")], path=SPEC) == {
        op_id: "b_" + slug(op_id)
    }


def test_hyphenated_backend() -> None:
    assert tool_name("my-api", "x") == "my-api_x"


def test_build_tool_names_maps_ids() -> None:
    ops = [
        Operation("pets.list", "GET", "/pets"),
        Operation("pets_read", "GET", "/pets/{id}"),
    ]
    assert build_tool_names("b", ops, path=SPEC) == {
        "pets.list": "b_pets_list",
        "pets_read": "b_pets_read",
    }


def test_missing_operation_id_names_method_and_path() -> None:
    with pytest.raises(SchemaError, match=r"GET /pets/\{id\}.*operationId") as exc:
        build_tool_names("b", [Operation(None, "GET", "/pets/{id}")], path=SPEC)
    assert exc.value.backend == "b"
    assert exc.value.file == SPEC
    assert "exclude" in exc.value.message


def test_names_colliding_after_slug() -> None:
    ops = [Operation("pets.list", "GET", "/a"), Operation("pets_list", "GET", "/b")]
    with pytest.raises(SchemaError, match=r"pets\.list.*pets_list.*b_pets_list"):
        build_tool_names("b", ops, path=SPEC)


def test_slug_too_long() -> None:
    op_id = "a" * 57
    with pytest.raises(SchemaError, match=op_id) as exc:
        build_tool_names("b", [Operation(op_id, "GET", "/a")], path=SPEC)
    assert "56" in exc.value.message
    assert "rename" in exc.value.message


def test_slug_of_56_chars_accepted() -> None:
    op_id = "a" * 56
    assert build_tool_names("b", [Operation(op_id, "GET", "/a")], path=SPEC) == {
        op_id: "b_" + op_id
    }


def test_full_name_too_long() -> None:
    backend = "b" * 20
    op_id = "a" * 50
    with pytest.raises(SchemaError, match=op_id) as exc:
        build_tool_names(backend, [Operation(op_id, "GET", "/a")], path=SPEC)
    assert "64" in exc.value.message


def test_full_name_of_64_chars_accepted() -> None:
    backend = "b" * 20
    op_id = "a" * 43
    names = build_tool_names(backend, [Operation(op_id, "GET", "/a")], path=SPEC)
    assert len(names[op_id]) == 64


# --- path parameters ----------------------------------------------------------------


def test_path_parameter_names_include_path_item_level_and_refs() -> None:
    doc: dict[str, Any] = {
        "paths": {
            "/orgs/{org}/teams/{team}": {
                "parameters": [{"$ref": "#/components/parameters/Org"}],
                "get": {
                    "parameters": [
                        {"name": "team", "in": "path"},
                        {"name": "q", "in": "query"},
                        {"$ref": "#/components/parameters/Org"},  # repeated: listed once
                    ]
                },
            }
        },
        "components": {"parameters": {"Org": {"name": "org", "in": "path"}}},
    }
    op = Operation("t", "GET", "/orgs/{org}/teams/{team}")
    assert path_parameter_names(doc, op) == ("org", "team")


def test_path_parameter_names_tolerate_unresolvable_and_cyclic_refs() -> None:
    doc: dict[str, Any] = {
        "paths": {
            "/a/{x}": {
                "get": {
                    "parameters": [
                        {"$ref": "#/components/parameters/Missing"},
                        {"$ref": "#/components/parameters/Loop"},
                        {"$ref": "https://example.com/p.yaml"},
                        {"name": "x", "in": "path"},
                    ]
                }
            }
        },
        "components": {"parameters": {"Loop": {"$ref": "#/components/parameters/Loop"}}},
    }
    assert path_parameter_names(doc, Operation("a", "GET", "/a/{x}")) == ("x",)
    assert path_parameter_names(doc, Operation("b", "GET", "/missing")) == ()

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from openapi_mcp_gateway.config.models import BackendConfig, ExposureMode
from openapi_mcp_gateway.exceptions import ConfigError
from openapi_mcp_gateway.openapi.filter import filter_document, path_matches, select_operations
from openapi_mcp_gateway.openapi.operations import (
    HTTP_METHODS,
    Operation,
    extract_operations,
)

SPEC = Path("spec.yaml")


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("/api/internal/*", "/api/internal", True),
        ("/api/internal/*", "/api/internal/", True),
        ("/api/internal/*", "/api/internal/jobs/{id}", True),
        ("/api/internal/*", "/api/internal-tools", False),
        ("/api/internal/*", "/v2/api/internal/x", False),
        ("/api/Internal/*", "/api/internal/x", False),
        ("/api/*/export", "/api/orders/export", True),
        ("/api/*/export", "/api/orders/x/export", False),
        ("/api/users", "/api/users", True),
        ("/api/users", "/api/users/{id}", False),
        ("/api/v*/users", "/api/v2/users", True),
        # trailing slashes are the same location, on either side
        ("/api/internal", "/api/internal/", True),
        ("/api/internal/", "/api/internal", True),
        # literal template segments match themselves
        ("/pets/{id}", "/pets/{id}", True),
        ("/pets/{id}", "/pets/{petId}", False),
        ("/pets/{id}/*", "/pets/{id}/toys", True),
        # '?' and '[...]' are literal, not glob syntax
        ("/pets/?", "/pets/a", False),
        ("/pets/?", "/pets/?", True),
        ("/pets/[ab]", "/pets/a", False),
        ("/pets/[ab]", "/pets/[ab]", True),
        ("/pets/[!a]", "/pets/b", False),
        # '*' matches within one segment only and may be empty/partial
        ("/api/*", "/api", True),
        ("/api/*", "/apix", False),
        ("/api/x*", "/api/xyz/deeper", False),
        ("/api/x*/*", "/api/xyz/deeper", True),
        # repeated slashes do not defeat a pattern
        ("/api/admin/*", "//api/admin", True),
        ("/api/admin/*", "/api//admin/users", True),
        ("/api//admin", "/api/admin", True),
    ],
)
def test_path_matches(pattern: str, path: str, expected: bool) -> None:
    assert path_matches(pattern, path) is expected


def _cfg(
    mode: ExposureMode = "read_only",
    operations: tuple[str, ...] = (),
    exclude_operations: tuple[str, ...] = (),
    exclude_paths: tuple[str, ...] = (),
) -> BackendConfig:
    return BackendConfig(
        name="shop",
        directory=Path(),
        config_path=Path("backend.yaml"),
        schema_path=Path("openapi.yaml"),
        base_url="http://localhost",
        auth=None,  # type: ignore[arg-type]
        mode=mode,
        operations=operations,
        exclude_operations=exclude_operations,
        exclude_paths=exclude_paths,
        timeout_seconds=30.0,
        follow_redirects=False,
        max_response_bytes=1048576,
        max_connections=10,
    )


def _doc() -> dict[str, Any]:
    things: dict[str, Any] = {"parameters": [{"name": "x", "in": "query"}], "summary": "s"}
    for m in HTTP_METHODS:
        things[m] = {"operationId": f"things_{m}", "servers": [{"url": "http://op"}]}
    things["servers"] = [{"url": "http://path"}]
    return {
        "openapi": "3.0.3",
        "info": {"title": "t", "version": "1"},
        "servers": [{"url": "http://root"}],
        "security": [{"k": []}],
        "tags": [{"name": "a"}],
        "paths": {
            "/things": things,
            "/api/admin/users": {"get": {"operationId": "admin_users"}},
            "/reports": {"get": {"operationId": "reports_export"}},
        },
        "components": {"schemas": {"S": {"type": "object"}}},
    }


def _ops() -> list[Operation]:
    return extract_operations(_doc(), backend="shop", path=SPEC)


def test_read_only_selects_get_and_head_only() -> None:
    sel = select_operations(_ops(), _cfg())
    assert {o.method for o in sel.operations} == {"GET", "HEAD"}


def test_allowlist_selects_exactly_listed() -> None:
    sel = select_operations(_ops(), _cfg("allowlist", operations=("things_post", "admin_users")))
    assert {o.operation_id for o in sel.operations} == {"things_post", "admin_users"}
    assert any(o.method == "POST" for o in sel.operations)


def test_allowlist_unknown_id_is_config_error() -> None:
    with pytest.raises(ConfigError) as exc:
        select_operations(_ops(), _cfg("allowlist", operations=("things_get", "nope_op")))
    assert exc.value.field == "exposure.operations"
    assert exc.value.backend == "shop"
    assert exc.value.file == Path("backend.yaml")
    assert "nope_op" in exc.value.message


def test_allowlist_never_selects_operation_without_id() -> None:
    ops = [Operation(None, "GET", "/anon"), Operation("a", "GET", "/a")]
    sel = select_operations(ops, _cfg("allowlist", operations=("a",)))
    assert sel.operations == (ops[1],)


def test_all_selects_every_method() -> None:
    sel = select_operations(_ops(), _cfg("all"))
    assert len(sel.operations) == 10


@pytest.mark.parametrize("mode", ["read_only", "allowlist", "all"])
def test_exclusions_apply_in_every_mode(mode: ExposureMode) -> None:
    # An operations list is only valid in allowlist mode (validate_config rejects it elsewhere).
    allowlist = ("reports_export", "admin_users", "things_get") if mode == "allowlist" else ()
    cfg = _cfg(
        mode,
        operations=allowlist,
        exclude_operations=("reports_export",),
        exclude_paths=("/api/admin/*",),
    )
    sel = select_operations(_ops(), cfg)
    ids = {o.operation_id for o in sel.operations}
    assert "reports_export" not in ids
    assert not any(o.path.startswith("/api/admin") for o in sel.operations)
    assert "things_get" in ids


def test_excluded_unknown_operation_warns() -> None:
    sel = select_operations(_ops(), _cfg(exclude_operations=("no_such_op",)))
    assert len(sel.warnings) == 1
    assert "no_such_op" in sel.warnings[0]
    assert "shop" in sel.warnings[0]


def test_excluding_known_but_unselected_operation_does_not_warn() -> None:
    sel = select_operations(_ops(), _cfg(exclude_operations=("things_post",)))
    assert sel.warnings == ()


def test_operation_without_id_survives_selection_in_read_only() -> None:
    anon = Operation(None, "GET", "/anon")
    sel = select_operations([anon], _cfg())
    assert sel.operations == (anon,)


def test_operation_without_id_survives_in_all() -> None:
    anon = Operation(None, "DELETE", "/anon")
    assert select_operations([anon], _cfg("all")).operations == (anon,)


def test_filter_document_prunes_and_strips_servers() -> None:
    doc = _doc()
    before = copy.deepcopy(doc)
    out = filter_document(doc, [Operation("things_get", "GET", "/things")])
    assert list(out["paths"]) == ["/things"]
    assert list(out["paths"]["/things"]) == ["parameters", "summary", "get"]
    assert "servers" not in out
    assert "servers" not in out["paths"]["/things"]["get"]
    assert "servers" not in out["paths"]["/things"]
    assert "post" in doc["paths"]["/things"]  # input untouched
    assert doc == before
    assert out["components"] == doc["components"]
    assert out["info"] == doc["info"]
    assert out["security"] == doc["security"]
    assert out["tags"] == doc["tags"]
    assert out["openapi"] == "3.0.3"


def test_filter_document_result_is_independent_of_input() -> None:
    doc = _doc()
    out = filter_document(doc, [Operation("things_get", "GET", "/things")])
    out["components"]["schemas"]["S"]["type"] = "string"
    out["paths"]["/things"]["parameters"].append({"name": "y"})
    assert doc["components"]["schemas"]["S"]["type"] == "object"
    assert len(doc["paths"]["/things"]["parameters"]) == 1


def test_filter_document_drops_ref_path_item_and_selects_by_method_and_path() -> None:
    doc = _doc()
    doc["paths"]["/ref"] = {"$ref": "#/components/pathItems/X"}
    out = filter_document(
        doc, [Operation("a", "GET", "/ref"), Operation("things_head", "HEAD", "/things")]
    )
    assert list(out["paths"]) == ["/things"]
    assert [k for k in out["paths"]["/things"] if k in HTTP_METHODS] == ["head"]


@pytest.mark.parametrize(
    ("pattern", "rule"),
    [
        ("api/admin/*", "start with '/'"),
        ("*", "start with '/'"),
        ("/api/admin/**", "'**'"),
        ("/**", "'**'"),
    ],
)
def test_malformed_exclude_path_is_config_error(pattern: str, rule: str) -> None:
    with pytest.raises(ConfigError) as exc:
        select_operations(_ops(), _cfg(exclude_paths=(pattern,)))
    assert exc.value.field == "exposure.exclude.paths"
    assert exc.value.backend == "shop"
    assert exc.value.file == Path("backend.yaml")
    assert repr(pattern) in exc.value.message
    assert rule in exc.value.message


def test_unmatched_exclude_path_warns_against_all_operations() -> None:
    sel = select_operations(_ops(), _cfg(exclude_paths=("/api/admin/*", "/nothing/*", "/things")))
    assert len(sel.warnings) == 1
    assert "'/nothing/*'" in sel.warnings[0]
    assert "shop" in sel.warnings[0]


def test_exclude_path_matching_only_unselected_operation_does_not_warn() -> None:
    # POST /things is not selected in read_only, but the pattern does match the schema.
    ops = [Operation("p", "POST", "/hidden"), Operation("g", "GET", "/g")]
    assert select_operations(ops, _cfg(exclude_paths=("/hidden",))).warnings == ()


def test_ref_path_item_with_inline_methods_agrees_with_filter_document() -> None:
    doc = {
        "openapi": "3.0.3",
        "paths": {
            "/r": {"$ref": "#/x", "get": {"operationId": "r_get"}},
            "/ok": {"get": {"operationId": "ok_get"}},
        },
    }
    ops = extract_operations(doc, backend="shop", path=SPEC)
    assert [o.path for o in ops] == ["/ok"]
    sel = select_operations(ops, _cfg("all"))
    assert list(filter_document(doc, sel.operations)["paths"]) == ["/ok"]

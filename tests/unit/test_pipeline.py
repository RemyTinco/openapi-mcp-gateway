from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from openapi_mcp_gateway.config.loader import load_gateway_config
from openapi_mcp_gateway.exceptions import ConfigError, MultipleErrors, SchemaError
from openapi_mcp_gateway.pipeline import prepare_all

THREE = Path(__file__).parent.parent / "fixtures" / "gateways" / "three-backends"


def _doc(*operations: tuple[str, str, str]) -> dict[str, Any]:
    paths: dict[str, Any] = {}
    for op_id, method, path in operations:
        paths.setdefault(path, {})[method] = {
            "operationId": op_id,
            "responses": {"200": {"description": "OK"}},
        }
    return {"openapi": "3.0.3", "info": {"title": "t", "version": "1"}, "paths": paths}


def _gateway(
    tmp_path: Path,
    write_gateway: Any,
    backends: dict[str, dict[str, Any] | None],
) -> Path:
    """Backends maps name -> OpenAPI document (None for an invalid one)."""
    specs = {
        name: {"name": name, "schema": "./openapi.json", "base_url": f"https://{name}.example.com"}
        for name in backends
    }
    path = write_gateway(tmp_path, {"version": 1}, specs)
    for name, doc in backends.items():
        target = tmp_path / "backends" / name / "openapi.json"
        target.write_text(json.dumps(doc if doc is not None else {"openapi": "9"}))
    return path


def test_prepare_three_backends() -> None:
    prepared = prepare_all(load_gateway_config(THREE / "gateway.yaml"))
    assert [p.config.name for p in prepared] == ["catalog", "messaging", "warehouse"]
    assert [p.openapi_version for p in prepared] == ["3.0.3", "3.1.0", "3.0.3"]
    assert prepared[0].tool_names == {
        "products_list": "catalog_products_list",
        "products_retrieve": "catalog_products_retrieve",
    }


def test_filtered_document_contains_only_selected() -> None:
    catalog = prepare_all(load_gateway_config(THREE / "gateway.yaml"))[0]
    assert "post" not in catalog.document["paths"]["/products"]
    assert [op.operation_id for op in catalog.operations] == [
        "products_list",
        "products_retrieve",
    ]


def test_cross_backend_name_collision(tmp_path: Path, write_gateway: Any) -> None:
    path = _gateway(
        tmp_path,
        write_gateway,
        {"a": _doc(("b_c", "get", "/x")), "a_b": _doc(("c", "get", "/y"))},
    )
    with pytest.raises(SchemaError) as exc:
        prepare_all(load_gateway_config(path))
    assert "a_b_c" in str(exc.value)
    assert "'a'" in str(exc.value)
    assert "'a_b'" in str(exc.value)


def test_empty_backend_warns_but_passes(tmp_path: Path, write_gateway: Any) -> None:
    path = _gateway(
        tmp_path,
        write_gateway,
        {"full": _doc(("l", "get", "/x")), "empty": _doc(("c", "post", "/y"))},
    )
    prepared = prepare_all(load_gateway_config(path))
    empty = next(p for p in prepared if p.config.name == "empty")
    assert empty.tool_names == {}
    assert any("no tools" in w for w in empty.warnings)


def test_zero_tools_overall_fails(tmp_path: Path, write_gateway: Any) -> None:
    path = _gateway(tmp_path, write_gateway, {"only": _doc(("c", "post", "/y"))})
    with pytest.raises(ConfigError, match="no tools exposed by any backend"):
        prepare_all(load_gateway_config(path))


def test_errors_from_two_backends_reported_together(tmp_path: Path, write_gateway: Any) -> None:
    path = _gateway(
        tmp_path,
        write_gateway,
        {"one": None, "two": None, "three": _doc(("l", "get", "/x"))},
    )
    with pytest.raises(MultipleErrors) as exc:
        prepare_all(load_gateway_config(path))
    assert len(exc.value.errors) == 2
    assert {e.backend for e in exc.value.errors} == {"one", "two"}


def test_selection_warnings_are_carried(tmp_path: Path, write_gateway: Any) -> None:
    path = _gateway(tmp_path, write_gateway, {"x": _doc(("l", "get", "/x"))})
    backend_yaml = tmp_path / "backends" / "x" / "backend.yaml"
    backend_yaml.write_text(
        backend_yaml.read_text() + "exposure:\n  exclude:\n    operations: [nope]\n"
    )
    prepared = prepare_all(load_gateway_config(path))
    assert any("nope" in w for w in prepared[0].warnings)

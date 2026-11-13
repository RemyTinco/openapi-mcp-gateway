"""NFR-001: five backends with 200 operations in total start in under three seconds."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from openapi_mcp_gateway.config.loader import load_gateway_config
from openapi_mcp_gateway.mcp.gateway import build_server
from openapi_mcp_gateway.pipeline import prepare_all

if TYPE_CHECKING:
    from pathlib import Path

BACKENDS = 5
OPERATIONS_PER_BACKEND = 40
BUDGET_SECONDS = 3.0


def _document(backend: str) -> dict[str, Any]:
    item = {
        "type": "object",
        "required": ["name"],
        "properties": {
            "id": {"type": "integer"},
            "name": {"type": "string"},
            "status": {"type": "string", "enum": ["a", "b", "c"]},
            "note": {"type": "string", "nullable": True},
        },
    }
    paths: dict[str, Any] = {}
    for n in range(OPERATIONS_PER_BACKEND // 4):
        resource = f"resource{n}"
        paths[f"/{resource}/"] = {
            "get": {
                "operationId": f"{resource}_list",
                "parameters": [
                    {"name": "page", "in": "query", "schema": {"type": "integer"}},
                    {"name": "q", "in": "query", "schema": {"type": "string"}},
                ],
                "responses": {"200": {"description": "ok"}},
            },
            "post": {
                "operationId": f"{resource}_create",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Item"}}
                    },
                },
                "responses": {"201": {"description": "created"}},
            },
        }
        id_param = {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}
        paths[f"/{resource}/{{id}}/"] = {
            "get": {
                "operationId": f"{resource}_retrieve",
                "parameters": [id_param],
                "responses": {"200": {"description": "ok"}},
            },
            "delete": {
                "operationId": f"{resource}_destroy",
                "parameters": [id_param],
                "responses": {"204": {"description": "gone"}},
            },
        }
    return {
        "openapi": "3.0.3",
        "info": {"title": backend, "version": "1"},
        "paths": paths,
        "components": {"schemas": {"Item": item}},
    }


def _write_reference_config(root: Path) -> Path:
    gateway = root / "gateway.yaml"
    gateway.write_text(yaml.safe_dump({"version": 1}), encoding="utf-8")
    for n in range(BACKENDS):
        name = f"backend{n}"
        directory = root / "backends" / name
        directory.mkdir(parents=True)
        (directory / "openapi.json").write_text(json.dumps(_document(name)), encoding="utf-8")
        backend = {
            "name": name,
            "schema": "./openapi.json",
            "base_url": f"https://{name}.example.com",
            "exposure": {"mode": "all"},
        }
        (directory / "backend.yaml").write_text(yaml.safe_dump(backend), encoding="utf-8")
    return gateway


@pytest.mark.perf
def test_startup_under_three_seconds(tmp_path: Path) -> None:
    gateway = _write_reference_config(tmp_path)

    started = time.perf_counter()
    cfg = load_gateway_config(gateway)
    prepared = prepare_all(cfg)
    build_server(cfg, prepared, {})
    elapsed = time.perf_counter() - started

    assert sum(len(p.tool_names) for p in prepared) == BACKENDS * OPERATIONS_PER_BACKEND
    print(f"startup: {elapsed:.3f}s")  # noqa: T201 - the benchmark's result
    assert elapsed < BUDGET_SECONDS

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

WriteGateway = Callable[[Path, dict[str, Any], dict[str, dict[str, Any]]], Path]


def _write_gateway(
    tmp_path: Path, gateway: dict[str, Any], backends: dict[str, dict[str, Any]]
) -> Path:
    """Write gateway.yaml plus one <backends dir>/<name>/backend.yaml per entry."""
    path = tmp_path / "gateway.yaml"
    path.write_text(yaml.safe_dump(gateway), encoding="utf-8")
    directory = gateway.get("backends", {}).get("directory", "./backends")
    for name, backend in backends.items():
        backend_dir = tmp_path / directory / name
        backend_dir.mkdir(parents=True, exist_ok=True)
        (backend_dir / "backend.yaml").write_text(yaml.safe_dump(backend), encoding="utf-8")
    return path


@pytest.fixture
def write_gateway() -> WriteGateway:
    return _write_gateway

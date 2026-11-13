from __future__ import annotations

import json
from pathlib import Path

import pytest

from openapi_mcp_gateway.exceptions import SchemaError
from openapi_mcp_gateway.openapi.loader import load_document

FIX = Path(__file__).parents[2] / "fixtures" / "schemas"


def test_loads_yaml_fixture() -> None:
    doc = load_document(FIX / "petstore-3.0.yaml", backend="b")
    assert doc["openapi"] == "3.0.3"


def test_loads_json_fixture() -> None:
    doc = load_document(FIX / "petstore-3.1.json", backend="b")
    assert doc["openapi"] == "3.1.0"


def test_json_content_in_yaml_extension_loads(tmp_path: Path) -> None:
    path = tmp_path / "spec.yaml"
    path.write_text(json.dumps({"openapi": "3.0.3", "paths": {}}), encoding="utf-8")
    assert load_document(path, backend="b") == {"openapi": "3.0.3", "paths": {}}


def test_yaml_content_in_json_extension_loads(tmp_path: Path) -> None:
    path = tmp_path / "spec.json"
    path.write_text("openapi: 3.0.3\npaths: {}\n", encoding="utf-8")
    assert load_document(path, backend="b")["paths"] == {}


def test_integer_status_keys_normalised() -> None:
    doc = load_document(FIX / "int-status-keys.yaml", backend="b")
    assert set(doc["paths"]["/pets"]["get"]["responses"]) == {"200", "404"}


def test_non_string_keys_normalised_recursively_including_lists(tmp_path: Path) -> None:
    path = tmp_path / "spec.yaml"
    path.write_text("a:\n  - 1: x\n  - true: y\n    null: z\n", encoding="utf-8")
    assert load_document(path, backend="b") == {"a": [{"1": "x"}, {"true": "y", "null": "z"}]}


def test_colliding_normalised_keys_rejected(tmp_path: Path) -> None:
    path = tmp_path / "spec.yaml"
    path.write_text('a:\n  1: x\n  "1": y\n', encoding="utf-8")
    with pytest.raises(SchemaError, match="duplicate") as e:
        load_document(path, backend="b")
    assert e.value.field == "a"


def test_yaml_dates_stay_strings(tmp_path: Path) -> None:
    path = tmp_path / "spec.yaml"
    path.write_text("info:\n  version: 2020-01-01\n", encoding="utf-8")
    assert load_document(path, backend="b")["info"]["version"] == "2020-01-01"


def test_recursive_alias_rejected(tmp_path: Path) -> None:
    path = tmp_path / "spec.yaml"
    path.write_text("a: &x\n  b: *x\n", encoding="utf-8")
    with pytest.raises(SchemaError, match="recursive"):
        load_document(path, backend="b")


@pytest.mark.parametrize("content", ["- a\n- b\n", "just a string\n", "", "42\n"])
def test_non_mapping_root_rejected(tmp_path: Path, content: str) -> None:
    path = tmp_path / "spec.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(SchemaError, match="mapping") as e:
        load_document(path, backend="b")
    assert e.value.file == path
    assert e.value.backend == "b"


def test_unreadable_file(tmp_path: Path) -> None:
    path = tmp_path / "missing.yaml"
    with pytest.raises(SchemaError) as e:
        load_document(path, backend="b")
    assert e.value.file == path
    assert e.value.backend == "b"


def test_not_utf8_file(tmp_path: Path) -> None:
    path = tmp_path / "spec.yaml"
    path.write_bytes(b"openapi: \xff\xfe\n")
    with pytest.raises(SchemaError, match="UTF-8") as e:
        load_document(path, backend="b")
    assert e.value.file == path


def test_invalid_yaml_reports_location_without_content(tmp_path: Path) -> None:
    path = tmp_path / "spec.yaml"
    path.write_text("ok: 1\nSECRET-TOKEN: [unclosed\nnext: 2\n", encoding="utf-8")
    with pytest.raises(SchemaError, match=r"line \d+, column \d+") as e:
        load_document(path, backend="b")
    assert "SECRET-TOKEN" not in str(e.value)
    assert "Traceback" not in str(e.value)


def test_invalid_json_reports_location_without_content(tmp_path: Path) -> None:
    path = tmp_path / "spec.json"
    path.write_text('{"SECRET-TOKEN": [1, }', encoding="utf-8")
    with pytest.raises(SchemaError, match=r"line 1, column \d+") as e:
        load_document(path, backend="b")
    assert "SECRET-TOKEN" not in str(e.value)


def test_flow_style_yaml_loads(tmp_path: Path) -> None:
    path = tmp_path / "spec.yaml"
    path.write_text(
        "{openapi: 3.0.3, info: {title: t, version: '1'}, paths: {}}\n", encoding="utf-8"
    )
    assert load_document(path, backend="b")["info"] == {"title": "t", "version": "1"}

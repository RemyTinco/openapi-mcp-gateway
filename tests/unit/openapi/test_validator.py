from __future__ import annotations

import copy
import time
from pathlib import Path
from typing import Any

import pytest

from openapi_mcp_gateway.exceptions import SchemaError
from openapi_mcp_gateway.openapi.loader import load_document
from openapi_mcp_gateway.openapi.validator import validate_document

FIX = Path(__file__).parents[2] / "fixtures" / "schemas"
PATH = Path("spec.yaml")


def _petstore() -> dict[str, Any]:
    return copy.deepcopy(load_document(FIX / "petstore-3.0.yaml", backend="b"))


def _validate(doc: dict[str, Any]) -> str:
    return validate_document(doc, backend="b", path=PATH)


@pytest.mark.parametrize(
    ("name", "version"), [("petstore-3.0.yaml", "3.0.3"), ("petstore-3.1.json", "3.1.0")]
)
def test_valid_documents(name: str, version: str) -> None:
    p = FIX / name
    assert validate_document(load_document(p, backend="b"), backend="b", path=p) == version


def test_broken_internal_ref() -> None:
    p = FIX / "broken-ref.yaml"
    with pytest.raises(SchemaError, match=r"#/components/schemas/Missing") as e:
        validate_document(load_document(p, backend="b"), backend="b", path=p)
    assert e.value.field is not None
    assert e.value.field.startswith("paths./pets.get")
    assert e.value.file == p
    assert e.value.backend == "b"


def test_pointer_with_escapes_resolves() -> None:
    doc = _petstore()
    doc["components"]["schemas"]["a/b~c"] = {"type": "string"}
    doc["components"]["schemas"]["Pet"]["allOf"][0] = {"$ref": "#/components/schemas/a~1b~0c"}
    assert _validate(doc) == "3.0.3"


def test_pointer_through_list_index() -> None:
    doc = _petstore()
    doc["components"]["schemas"]["Alias"] = {"$ref": "#/components/schemas/Pet/allOf/1"}
    assert _validate(doc) == "3.0.3"


def test_pointer_bad_list_index() -> None:
    doc = _petstore()
    doc["components"]["schemas"]["Alias"] = {"$ref": "#/components/schemas/Pet/allOf/9"}
    with pytest.raises(SchemaError, match="does not resolve") as e:
        _validate(doc)
    assert e.value.field == "components.schemas.Alias"


@pytest.mark.parametrize("ref", ["./other.yaml#/X", "https://example.com/s.json#/X", "other.yaml"])
def test_external_ref_rejected(ref: str) -> None:
    doc = _petstore()
    doc["components"]["schemas"]["Pet"] = {"$ref": ref}
    with pytest.raises(SchemaError, match="bundle") as e:
        _validate(doc)
    assert e.value.field == "components.schemas.Pet"


def test_external_ref_fixture_rejected() -> None:
    p = FIX / "external-ref.yaml"
    with pytest.raises(SchemaError, match="bundle"):
        validate_document(load_document(p, backend="b"), backend="b", path=p)


def test_swagger_2_rejected() -> None:
    p = FIX / "swagger-2.0.yaml"
    with pytest.raises(SchemaError, match=r"2\.0.*3\.0\.x.*3\.1\.x") as e:
        validate_document(load_document(p, backend="b"), backend="b", path=p)
    assert e.value.field == "swagger"


def test_missing_openapi_field() -> None:
    doc = _petstore()
    del doc["openapi"]
    with pytest.raises(SchemaError, match="openapi") as e:
        _validate(doc)
    assert e.value.field == "openapi"


@pytest.mark.parametrize("value", ["3.2.0", "4.0.0", "3", "3.0", 3.0, None, "3.0.3-rc1"])
def test_unsupported_openapi_version(value: Any) -> None:
    doc = _petstore()
    doc["openapi"] = value
    with pytest.raises(SchemaError, match=r"3\.0\.x.*3\.1\.x") as e:
        _validate(doc)
    assert e.value.field == "openapi"


def test_structurally_invalid_document() -> None:
    doc = _petstore()
    doc["paths"] = []
    with pytest.raises(SchemaError) as e:
        _validate(doc)
    assert e.value.field == "paths"
    assert "Traceback" not in str(e.value)
    assert "\n" not in e.value.message


def test_spec_error_message_does_not_embed_document_content() -> None:
    doc = _petstore()
    doc["info"]["title"] = {"SECRET-TOKEN": 1}
    with pytest.raises(SchemaError) as e:
        _validate(doc)
    assert e.value.field == "info.title"
    assert "SECRET-TOKEN" not in str(e.value)


def test_missing_required_property_is_named_in_field_or_message() -> None:
    doc = _petstore()
    del doc["info"]["version"]
    with pytest.raises(SchemaError, match="version") as e:
        _validate(doc)
    assert e.value.field == "info"


def test_3_1_document_uses_3_1_validator() -> None:
    doc = _petstore()
    doc["openapi"] = "3.1.0"
    doc["components"]["schemas"]["NewPet"]["properties"]["tag"] = {"type": ["string", "null"]}
    assert _validate(doc) == "3.1.0"
    doc["openapi"] = "3.0.3"
    with pytest.raises(SchemaError):
        _validate(doc)


def test_error_carries_backend_and_file() -> None:
    doc = _petstore()
    doc["paths"] = []
    with pytest.raises(SchemaError) as e:
        _validate(doc)
    assert e.value.backend == "b"
    assert e.value.file == PATH


def test_alias_bomb_rejected_quickly(tmp_path: Path) -> None:
    lines = ["l0: &l0 [x, x, x, x, x, x, x, x, x, x]"]
    for i in range(1, 15):
        refs = ", ".join([f"*l{i - 1}"] * 10)
        lines.append(f"l{i}: &l{i} [{refs}]")
    path = tmp_path / "bomb.yaml"
    path.write_text(
        "openapi: 3.0.3\ninfo: {title: t, version: '1'}\npaths: {}\nx-bomb:\n"
        + "\n".join("  " + line for line in lines)
        + "\n",
        encoding="utf-8",
    )
    doc = load_document(path, backend="b")
    started = time.monotonic()
    with pytest.raises(SchemaError, match="alias"):
        validate_document(doc, backend="b", path=path)
    assert time.monotonic() - started < 1.0


def test_shared_alias_with_bad_ref_still_reported(tmp_path: Path) -> None:
    path = tmp_path / "spec.yaml"
    path.write_text(
        "openapi: 3.0.3\ninfo: {title: t, version: '1'}\npaths: {}\n"
        "components:\n  schemas:\n    A: &a {$ref: '#/nope'}\n    B: *a\n",
        encoding="utf-8",
    )
    with pytest.raises(SchemaError, match="does not resolve"):
        validate_document(load_document(path, backend="b"), backend="b", path=path)

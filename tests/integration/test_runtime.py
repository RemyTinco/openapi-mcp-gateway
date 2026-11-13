"""In-memory MCP runtime: FastMCP providers per backend, routed through the guarded client."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
import yaml
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from openapi_mcp_gateway.config.loader import load_gateway_config
from openapi_mcp_gateway.config.validator import validate_config
from openapi_mcp_gateway.exceptions import CredentialsError, McpInitError
from openapi_mcp_gateway.http.client import current_call
from openapi_mcp_gateway.logging import configure_logging
from openapi_mcp_gateway.mcp import gateway as gateway_module
from openapi_mcp_gateway.mcp.gateway import ToolLoggingMiddleware, build_server
from openapi_mcp_gateway.pipeline import PreparedBackend, prepare_all

if TYPE_CHECKING:
    from openapi_mcp_gateway.config.models import GatewayConfig

RUNTIME = Path(__file__).parents[1] / "fixtures" / "gateways" / "runtime"
ENV = {"CATALOG_API_TOKEN": "app-token", "MESSAGING_API_KEY": "pay-key"}

Handler = Callable[[httpx2.Request], httpx2.Response]


def echo(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, json={"path": request.url.path})


class RecordingTransport(httpx2.MockTransport):
    """Inner transport that records requests and whether it was closed."""

    def __init__(self, respond: Handler = echo) -> None:
        self.seen: list[httpx2.Request] = []
        self.closed = False

        def handler(request: httpx2.Request) -> httpx2.Response:
            self.seen.append(request)
            return respond(request)

        super().__init__(handler)

    async def aclose(self) -> None:
        self.closed = True


def load(path: Path = RUNTIME) -> tuple[GatewayConfig, list[PreparedBackend]]:
    cfg = load_gateway_config(path / "gateway.yaml")
    validate_config(cfg)
    return cfg, prepare_all(cfg)


@dataclass
class Runtime:
    client: Client[Any]
    server: FastMCP[Any]
    transports: dict[str, RecordingTransport] = field(default_factory=dict)

    @property
    def app_seen(self) -> list[httpx2.Request]:
        return self.transports["catalog"].seen

    @property
    def pay_seen(self) -> list[httpx2.Request]:
        return self.transports["messaging"].seen


@asynccontextmanager
async def running(
    *,
    app: Handler = echo,
    pay: Handler = echo,
    path: Path = RUNTIME,
    prepared: list[PreparedBackend] | None = None,
    env: Mapping[str, str] = ENV,
) -> AsyncIterator[Runtime]:
    cfg, default_prepared = load(path)
    prepared = default_prepared if prepared is None else prepared
    respond = {"catalog": app, "messaging": pay}
    transports = {
        p.config.name: RecordingTransport(respond.get(p.config.name, echo)) for p in prepared
    }
    server = build_server(cfg, prepared, env, inner_transports=transports)
    async with Client(server) as client:
        yield Runtime(client, server, transports)


async def tool(name: str) -> Any:
    async with running() as rt:
        tools = {t.name: t for t in await rt.client.list_tools()}
    return tools[name]


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def copy_fixture(tmp_path: Path) -> Path:
    target = tmp_path / "gateway"
    shutil.copytree(RUNTIME, target)
    return target


# --- tool definitions ---------------------------------------------------------


async def test_tools_match_pipeline_names() -> None:
    async with running() as rt:
        assert sorted(t.name for t in await rt.client.list_tools()) == [
            "catalog_items_list",
            "catalog_items_retrieve",
            "messaging_items_create",
            "messaging_items_list",
            "messaging_items_retrieve",
        ]


async def test_server_exposes_only_tools() -> None:  # SEC-003: nothing beyond the documents
    async with running() as rt:
        assert await rt.client.list_resources() == []
        assert await rt.client.list_resource_templates() == []
        assert await rt.client.list_prompts() == []


async def test_input_schema_preserves_required_and_enum() -> None:
    listing = (await tool("catalog_items_list")).input_schema
    assert listing["properties"]["status"]["enum"] == ["open", "closed"]
    assert listing["properties"]["limit"]["maximum"] == 100
    assert "id" in (await tool("catalog_items_retrieve")).input_schema["required"]
    create = (await tool("messaging_items_create")).input_schema
    assert "name" in json.dumps(create)


async def test_description_preserved() -> None:
    description = (await tool("catalog_items_retrieve")).description
    assert "Retrieve one catalog item by id." in description


async def test_no_secret_in_tool_definitions() -> None:
    async with running() as rt:
        tools = await rt.client.list_tools()
    dumped = json.dumps([t.model_dump() for t in tools], default=str)
    assert "app-token" not in dumped
    assert "pay-key" not in dumped


async def test_hyphenated_backend_name_is_namespaced_verbatim(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    (root / "backends" / "messaging").rename(root / "backends" / "my-api")
    backend_yaml = root / "backends" / "my-api" / "backend.yaml"
    data = yaml.safe_load(backend_yaml.read_text())
    data["name"] = "my-api"
    backend_yaml.write_text(yaml.safe_dump(data))
    _, prepared = load(root)
    async with running(path=root) as rt:
        names = sorted(t.name for t in await rt.client.list_tools())
        await rt.client.call_tool("my-api_items_retrieve", {"id": 3})
        seen = rt.transports["my-api"].seen
    assert names == sorted(n for p in prepared for n in p.tool_names.values())
    assert "my-api_items_list" in names
    assert str(seen[0].url) == "https://messaging.example.com/items/3"


# --- request construction -------------------------------------------------------


async def test_routes_to_own_backend_with_own_credentials() -> None:
    async with running() as rt:
        await rt.client.call_tool("catalog_items_retrieve", {"id": 7})
        await rt.client.call_tool("messaging_items_retrieve", {"id": 9})
    app, pay = rt.app_seen, rt.pay_seen
    assert str(app[0].url) == "https://catalog.example.com/items/7"
    assert app[0].headers["authorization"] == "Bearer app-token"
    assert "x-api-key" not in app[0].headers
    assert str(pay[0].url) == "https://messaging.example.com/items/9"
    assert pay[0].headers["x-api-key"] == "pay-key"
    assert "authorization" not in pay[0].headers


async def test_tool_result_returned() -> None:
    async with running() as rt:
        result = await rt.client.call_tool("messaging_items_retrieve", {"id": 9})
    assert result.structured_content == {"path": "/items/9"}


async def test_schema_servers_never_used() -> None:
    async with running() as rt:
        await rt.client.call_tool("catalog_items_list", {})
        await rt.client.call_tool("catalog_items_retrieve", {"id": "1"})
        await rt.client.call_tool("messaging_items_list", {})
        await rt.client.call_tool("messaging_items_create", {"name": "x"})
    requests = rt.app_seen + rt.pay_seen
    assert len(requests) == 4
    assert all(r.url.host != "evil.example.com" for r in requests)
    assert all("level" not in r.url.path for r in requests)


async def test_query_parameters_serialised() -> None:
    async with running() as rt:
        await rt.client.call_tool("catalog_items_list", {"status": "open", "limit": 5})
    url = rt.app_seen[0].url
    assert url.path == "/items"
    assert dict(url.params) == {"status": "open", "limit": "5"}


async def test_path_and_query_parameters_together() -> None:
    async with running() as rt:
        await rt.client.call_tool("messaging_items_retrieve", {"id": 4, "expand": True})
    url = rt.pay_seen[0].url
    assert url.path == "/items/4"
    assert dict(url.params) == {"expand": "true"}


async def test_json_body_forwarded() -> None:
    async with running() as rt:
        await rt.client.call_tool("messaging_items_create", {"name": "x", "amount": 3})
    request = rt.pay_seen[0]
    assert request.method == "POST"
    assert request.url.path == "/items"
    assert request.headers["content-type"].startswith("application/json")
    assert json.loads(request.content) == {"name": "x", "amount": 3}


async def test_base_url_path_prefix_kept() -> None:
    _, prepared = load()
    prefixed = [
        replace(p, config=replace(p.config, base_url="https://catalog.example.com/api/v2"))
        if p.config.name == "catalog"
        else p
        for p in prepared
    ]
    async with running(prepared=prefixed) as rt:
        await rt.client.call_tool("catalog_items_retrieve", {"id": "7"})
        await rt.client.call_tool("catalog_items_list", {"status": "closed"})
    assert str(rt.app_seen[0].url) == "https://catalog.example.com/api/v2/items/7"
    assert str(rt.app_seen[1].url) == "https://catalog.example.com/api/v2/items?status=closed"


async def test_path_argument_cannot_escape_template() -> None:  # Review Focus 2
    async with running() as rt:
        with suppress(ToolError):
            await rt.client.call_tool("catalog_items_retrieve", {"id": "../admin?x=1"})
    # either rejected and nothing sent, or sent as exactly one encoded segment
    if rt.app_seen:
        raw_path = rt.app_seen[0].url.raw_path
        assert raw_path.count(b"/") == 2, raw_path
        assert raw_path.startswith(b"/items/"), raw_path
        assert rt.app_seen[0].url.query == b""


def add_path_templates(root: Path) -> None:
    """Mid-path, end-of-path and path-item-level (``$ref``) path parameters."""
    schema = root / "backends" / "catalog" / "openapi.yaml"
    document = yaml.safe_load(schema.read_text())
    ok = {"200": {"description": "OK"}}
    string = {"type": "string"}
    document["paths"]["/users/{id}/profile"] = {
        "get": {
            "operationId": "user_profile",
            "parameters": [
                {"name": "id", "in": "path", "required": True, "schema": string},
                {"name": "note", "in": "query", "schema": string},
            ],
            "responses": ok,
        }
    }
    document["paths"]["/orgs/{org}/teams/{team}"] = {
        "parameters": [{"$ref": "#/components/parameters/Org"}],
        "get": {
            "operationId": "team_retrieve",
            "parameters": [{"name": "team", "in": "path", "required": True, "schema": string}],
            "responses": ok,
        },
    }
    document.setdefault("components", {})["parameters"] = {
        "Org": {"name": "org", "in": "path", "required": True, "schema": string}
    }
    schema.write_text(yaml.safe_dump(document))


@pytest.mark.parametrize(
    ("tool", "arguments", "parameter"),
    [
        ("catalog_items_retrieve", {"id": ""}, "id"),  # end of path: /items/
        ("catalog_items_retrieve", {"id": "   "}, "id"),
        ("catalog_items_retrieve", {"id": None}, "id"),
        ("catalog_items_retrieve", {"expand": True}, "id"),  # missing
        ("catalog_user_profile", {"id": "", "note": "NOTE-MARKER"}, "id"),  # /users//profile
        ("catalog_user_profile", {"id": "\t\n", "note": "NOTE-MARKER"}, "id"),
        ("catalog_team_retrieve", {"org": "", "team": "a"}, "org"),  # path-item level $ref
        ("catalog_team_retrieve", {"org": "a", "team": " "}, "team"),
    ],
)
async def test_empty_path_argument_rejected_before_sending(
    tmp_path: Path, tool: str, arguments: dict[str, Any], parameter: str
) -> None:
    root = copy_fixture(tmp_path)
    add_path_templates(root)
    async with running(path=root) as rt:
        with pytest.raises(ToolError) as e:
            await rt.client.call_tool(tool, arguments)
    assert rt.app_seen == []
    payload = json.loads(str(e.value))
    assert payload == {
        "error": "invalid_argument",
        "backend": "catalog",
        "status": None,
        "message": payload["message"],
    }
    assert repr(parameter) in payload["message"]
    assert "NOTE-MARKER" not in str(e.value)


@pytest.mark.parametrize("value", [[], {}, [""], [[]], ["a", "b"], {"a": 1}])
async def test_non_scalar_path_argument_rejected_before_sending(value: Any) -> None:
    # FastMCP joins lists and objects with commas; an empty one becomes ``/items/``.
    async with running() as rt:
        with pytest.raises(ToolError) as e:
            await rt.client.call_tool("catalog_items_retrieve", {"id": value})
    assert rt.app_seen == []
    payload = json.loads(str(e.value))
    assert payload["error"] == "invalid_argument"
    assert "'id'" in payload["message"]


def add_sibling_routes(root: Path) -> None:
    """Templates whose concrete form can equal another path of the same schema."""
    schema = root / "backends" / "catalog" / "openapi.yaml"
    document = yaml.safe_load(schema.read_text())
    ok = {"200": {"description": "OK"}}

    def get(operation_id: str, *names: str) -> dict[str, Any]:
        parameters = [
            {"name": n, "in": "path", "required": True, "schema": {"type": "string"}} for n in names
        ]
        return {"get": {"operationId": operation_id, "parameters": parameters, "responses": ok}}

    document["paths"]["/items/true"] = get("items_true")
    document["paths"]["/items/export/"] = get("items_export_slash")
    document["paths"]["/items/{id}/audit/log"] = get("item_audit_log", "id")
    document["paths"]["/items/{id}/{view}"] = get("item_view", "id", "view")
    document["paths"]["/files/{name}.json"] = get("file_json", "name")
    document["paths"]["/files/{name}"] = get("file_raw", "name")
    document["paths"]["/{collection}/{key}"] = get("any_retrieve", "collection", "key")
    document["paths"]["/items/my%20export"] = get("items_my_export")
    document["paths"]["/reports/latest"] = {"post": get("reports_latest")["get"]}
    document["paths"]["/reports/{id}"] = get("report_retrieve", "id")
    schema.write_text(yaml.safe_dump(document))
    backend = root / "backends" / "catalog" / "backend.yaml"
    config = yaml.safe_load(backend.read_text())
    config["exposure"]["exclude"]["operations"] += [
        "items_true",
        "items_export_slash",
        "items_my_export",
    ]
    backend.write_text(yaml.safe_dump(config))


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("catalog_items_retrieve", {"id": "export"}),  # excluded /items/export
        ("catalog_items_retrieve", {"id": True}),  # sent as /items/true
        ("catalog_items_retrieve", {"id": "my export"}),  # schema spells it my%20export
        ("catalog_item_view", {"id": "1", "view": "audit/log"}),  # if a backend decodes %2F
        ("catalog_file_raw", {"name": "a.json"}),  # partial-segment template
        ("catalog_any_retrieve", {"collection": "items", "key": "7"}),  # /items/{id}
        ("catalog_any_retrieve", {"collection": "items", "key": "export"}),
        ("catalog_report_retrieve", {"id": "latest"}),  # path only has a hidden POST
    ],
)
async def test_path_argument_matching_another_schema_path_rejected(
    tmp_path: Path, tool: str, arguments: dict[str, Any]
) -> None:
    root = copy_fixture(tmp_path)
    add_sibling_routes(root)
    async with running(path=root) as rt:
        with pytest.raises(ToolError) as e:
            await rt.client.call_tool(tool, arguments)
    assert rt.app_seen == []
    payload = json.loads(str(e.value))
    assert payload == {
        "error": "invalid_argument",
        "backend": "catalog",
        "status": None,
        "message": payload["message"],
    }
    assert "different path" in payload["message"]
    assert "export" not in payload["message"]  # hidden paths are not described


async def test_path_arguments_not_matching_another_path_still_sent(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    add_sibling_routes(root)
    async with running(path=root) as rt:
        await rt.client.call_tool("catalog_items_retrieve", {"id": "exports"})
        await rt.client.call_tool("catalog_items_retrieve", {"id": "Export"})
        await rt.client.call_tool("catalog_items_retrieve", {"id": False})
        await rt.client.call_tool("catalog_items_retrieve", {"id": 1.5})
        await rt.client.call_tool("catalog_file_raw", {"name": "a.txt"})
        await rt.client.call_tool("catalog_file_json", {"name": "a"})
        # /items/{id} and /{collection}/{key} overlap whatever the arguments are: that
        # ambiguity is the schema's, so neither tool is blocked by the other.
        await rt.client.call_tool("catalog_any_retrieve", {"collection": "users", "key": "7"})
        await rt.client.call_tool("catalog_item_view", {"id": "export", "view": "x"})
    assert [r.url.raw_path for r in rt.app_seen] == [
        b"/items/exports",
        b"/items/Export",
        b"/items/false",
        b"/items/1%2E5",
        b"/files/a%2Etxt",
        b"/files/a.json",
        b"/users/7",
        b"/items/export/x",
    ]


async def test_non_empty_path_arguments_still_sent(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    add_path_templates(root)
    async with running(path=root) as rt:
        await rt.client.call_tool("catalog_user_profile", {"id": "0"})
        await rt.client.call_tool("catalog_team_retrieve", {"org": "o", "team": " t "})
        await rt.client.call_tool("messaging_items_retrieve", {"id": 0})
    assert [r.url.raw_path for r in rt.app_seen] == [b"/users/0/profile", b"/orgs/o/teams/%20t%20"]
    assert rt.pay_seen[0].url.path == "/items/0"


async def test_empty_path_argument_checked_under_its_renamed_argument(tmp_path: Path) -> None:
    # A query parameter with the same name makes FastMCP call them id__path / id__query.
    root = copy_fixture(tmp_path)
    schema = root / "backends" / "catalog" / "openapi.yaml"
    document = yaml.safe_load(schema.read_text())
    string = {"type": "string"}
    document["paths"]["/things/{id}"] = {
        "get": {
            "operationId": "thing_retrieve",
            "parameters": [
                {"name": "id", "in": "path", "required": True, "schema": string},
                {"name": "id", "in": "query", "schema": string},
            ],
            "responses": {"200": {"description": "OK"}},
        }
    }
    schema.write_text(yaml.safe_dump(document))
    async with running(path=root) as rt:
        listed = {t.name: t for t in await rt.client.list_tools()}
        assert {"id__path", "id__query"} <= set(
            listed["catalog_thing_retrieve"].input_schema["properties"]
        )
        with pytest.raises(ToolError) as e:
            await rt.client.call_tool("catalog_thing_retrieve", {"id__path": "", "id__query": "q"})
        await rt.client.call_tool("catalog_thing_retrieve", {"id__path": "p", "id__query": ""})
    assert json.loads(str(e.value))["error"] == "invalid_argument"
    assert "'id__path'" in json.loads(str(e.value))["message"]
    assert [str(r.url) for r in rt.app_seen] == ["https://catalog.example.com/things/p?id="]


async def test_slash_in_path_argument_stays_one_segment() -> None:
    async with running() as rt:
        await rt.client.call_tool("catalog_items_retrieve", {"id": "a/b?c"})
    assert rt.app_seen[0].url.raw_path == b"/items/a%2Fb%3Fc"


async def test_path_argument_cannot_escape_base_path_prefix() -> None:
    _, prepared = load()
    prefixed = [
        replace(p, config=replace(p.config, base_url="https://catalog.example.com/api/v2"))
        if p.config.name == "catalog"
        else p
        for p in prepared
    ]
    async with running(prepared=prefixed) as rt:
        for value in ("../../admin", "..%2F..%2Fadmin", "%2e%2e/%2e%2e/admin"):
            with suppress(ToolError):
                await rt.client.call_tool("catalog_items_retrieve", {"id": value})
    for request in rt.app_seen:
        assert request.url.path.startswith("/api/v2/items/"), request.url.raw_path


def add_header_parameters(root: Path) -> None:
    """Operations whose schema declares the credential and Host headers as parameters."""
    string = {"type": "string"}
    ok = {"200": {"description": "OK"}}
    for backend, credential in (("catalog", "Authorization"), ("messaging", "X-API-Key")):
        schema = root / "backends" / backend / "openapi.yaml"
        document = yaml.safe_load(schema.read_text())
        document["paths"]["/whoami"] = {
            "get": {
                "operationId": "whoami",
                "parameters": [
                    {"name": credential, "in": "header", "schema": string},
                    {"name": "Host", "in": "header", "schema": string},
                ],
                "responses": ok,
            }
        }
        schema.write_text(yaml.safe_dump(document))


async def test_header_parameters_cannot_override_credentials_or_host(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    add_header_parameters(root)
    async with running(path=root) as rt:
        await rt.client.call_tool(
            "catalog_whoami", {"Authorization": "Bearer agent", "Host": "admin.internal"}
        )
        await rt.client.call_tool(
            "messaging_whoami", {"X-API-Key": "agent-key", "Host": "messaging.example.com.evil"}
        )
    app, pay = rt.app_seen[0], rt.pay_seen[0]
    assert app.headers.get_list("authorization") == ["Bearer app-token"]
    assert app.headers.get_list("host") == ["catalog.example.com"]
    assert pay.headers.get_list("x-api-key") == ["pay-key"]
    assert pay.headers.get_list("host") == ["messaging.example.com"]


# --- errors and authorization -------------------------------------------------------


async def test_upstream_401_reaches_client_as_structured_error() -> None:
    async with running(app=lambda r: httpx2.Response(401, json={"detail": "no"})) as rt:
        with pytest.raises(ToolError) as e:
            await rt.client.call_tool("catalog_items_retrieve", {"id": "7"})
    payload = json.loads(str(e.value))
    assert payload["error"] == "authentication_failed"
    assert payload["status"] == 401
    assert payload["backend"] == "catalog"


async def test_excluded_and_non_selected_operations_cannot_be_invoked() -> None:
    async with running() as rt:
        for name in ["catalog_items_create", "catalog_items_export"]:
            with pytest.raises(ToolError):
                await rt.client.call_tool(name, {})
    assert rt.app_seen == []


async def test_missing_credentials_raise_credentials_error() -> None:
    cfg, prepared = load()
    with pytest.raises(CredentialsError):
        build_server(cfg, prepared, {"CATALOG_API_TOKEN": "app-token"})


async def test_name_mismatch_raises_mcp_init_error() -> None:
    cfg, prepared = load()
    corrupted = [
        replace(p, tool_names={**p.tool_names, "items_list": "catalog_items_listing"})
        if p.config.name == "catalog"
        else p
        for p in prepared
    ]
    with pytest.raises(McpInitError) as e:
        build_server(cfg, corrupted, ENV)
    assert "catalog_items_listing" in str(e.value)


AWKWARD_IDS = ["items--list", "_private_op_", "a__b", "Mixed.Case-Id", "v2/items:list"]


async def test_awkward_operation_ids_listed_exactly_as_pipeline_names(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    schema = root / "backends" / "messaging" / "openapi.yaml"
    document = yaml.safe_load(schema.read_text())
    for i, operation_id in enumerate(AWKWARD_IDS):
        document["paths"][f"/awkward/{i}"] = {
            "get": {"operationId": operation_id, "responses": {"200": {"description": "OK"}}}
        }
    document["paths"]["/items"]["get"]["operationId"] = "items_index"  # items--list collides
    schema.write_text(yaml.safe_dump(document))
    _, prepared = load(root)
    expected = sorted(n for p in prepared for n in p.tool_names.values())
    assert {
        "messaging_items_list",
        "messaging_private_op",
        "messaging_a_b",
        "messaging_Mixed_Case_Id",
        "messaging_v2_items_list",
    } <= set(expected)
    async with running(path=root) as rt:
        assert sorted(t.name for t in await rt.client.list_tools()) == expected
        await rt.client.call_tool("messaging_Mixed_Case_Id", {})
    assert rt.pay_seen[0].url.path == "/awkward/3"


async def test_name_guard_catches_names_fastmcp_would_not_list() -> None:
    # The pipeline never produces such a name now; corrupt one to exercise the guard.
    cfg, prepared = load()
    corrupted = [
        replace(p, tool_names={**p.tool_names, "items_list": "messaging_items__list"})
        if p.config.name == "messaging"
        else p
        for p in prepared
    ]
    with pytest.raises(McpInitError) as e:
        build_server(cfg, corrupted, ENV)
    assert "missing messaging_items__list" in str(e.value)
    assert "unexpected messaging_items_list" in str(e.value)


async def test_fastmcp_construction_error_raises_mcp_init_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        msg = "Invalid OpenAPI specification: boom"
        raise ValueError(msg)

    monkeypatch.setattr(gateway_module, "build_provider", broken)
    cfg, prepared = load()
    with pytest.raises(McpInitError) as e:
        build_server(cfg, prepared, ENV)
    assert "boom" in str(e.value)


# --- logging ------------------------------------------------------------------------


async def test_startup_log_lines(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "text")
    cfg, prepared = load()
    build_server(cfg, prepared, ENV, inner_transports={})
    err = capsys.readouterr().err
    assert "INFO backend.loaded name=catalog" in err
    assert "INFO backend.loaded name=messaging" in err
    assert "INFO tools.registered count=5" in err


async def test_tool_executed_log_line(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "text")
    async with running() as rt:
        capsys.readouterr()
        await rt.client.call_tool("catalog_items_retrieve", {"id": "7"})
    out, err = capsys.readouterr()
    assert out == ""
    lines = [line for line in err.splitlines() if "tool.executed" in line]
    assert len(lines) == 1
    assert lines[0].startswith(
        "INFO tool.executed backend=catalog operation=items_retrieve method=GET status=200 "
        "duration_ms="
    )


async def test_tool_executed_logged_for_failures(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "text")
    async with running(pay=lambda r: httpx2.Response(404, json={"detail": "gone"})) as rt:
        capsys.readouterr()
        with pytest.raises(ToolError):
            await rt.client.call_tool("messaging_items_retrieve", {"id": 1})
    lines = [line for line in capsys.readouterr().err.splitlines() if "tool.executed" in line]
    assert len(lines) == 1
    assert "backend=messaging operation=items_retrieve method=GET status=404" in lines[0]
    assert "error=not_found" in lines[0]


async def test_tool_executed_logged_when_request_never_built(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO", "text")
    async with running() as rt:
        capsys.readouterr()
        with pytest.raises(ToolError):
            await rt.client.call_tool("catalog_items_retrieve", {"id": "../admin"})
    lines = [line for line in capsys.readouterr().err.splitlines() if "tool.executed" in line]
    assert len(lines) == 1
    assert "backend=catalog operation=items_retrieve method=None status=None" in lines[0]
    assert "error=tool_failed" in lines[0]
    assert rt.app_seen == []


async def test_tool_executed_logged_for_invalid_path_argument(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO", "text")
    async with running() as rt:
        capsys.readouterr()
        with pytest.raises(ToolError):
            await rt.client.call_tool("catalog_items_retrieve", {"id": ""})
    lines = [line for line in capsys.readouterr().err.splitlines() if "tool.executed" in line]
    assert len(lines) == 1
    assert "backend=catalog operation=items_retrieve method=None status=None" in lines[0]
    assert "error=invalid_argument" in lines[0]


async def test_no_secret_or_body_in_logs_at_debug(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("DEBUG", "text")

    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"echo": "RESPONSE-BODY-MARKER"})

    async with running(pay=respond, app=respond) as rt:
        await rt.client.call_tool("messaging_items_create", {"name": "REQUEST-BODY-MARKER"})
        await rt.client.call_tool("catalog_items_list", {"status": "open", "limit": 7})
    err = capsys.readouterr().err
    assert "tool.executed" in err  # stderr capture is live
    for secret in ("app-token", "pay-key", "REQUEST-BODY-MARKER", "RESPONSE-BODY-MARKER"):
        assert secret not in err, secret
    assert "limit=7" not in err  # query strings carry values too


def json_lines(err: str) -> list[dict[str, Any]]:
    """Every non-empty stderr line parsed as one JSON object (fails otherwise)."""
    lines = [line for line in err.splitlines() if line.strip()]
    parsed = [json.loads(line) for line in lines]
    assert all(isinstance(p, dict) for p in parsed)
    return parsed


async def test_failing_calls_leave_only_json_lines_on_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO", "json")
    async with running(pay=lambda r: httpx2.Response(404, json={"detail": "gone"})) as rt:
        capsys.readouterr()
        for name, arguments in [
            ("messaging_items_retrieve", {"id": 1}),  # upstream 404
            ("catalog_items_retrieve", {"id": ".."}),  # FastMCP cannot build the request
            ("catalog_items_retrieve", {"id": ""}),  # refused by the gateway
        ]:
            with pytest.raises(ToolError):
                await rt.client.call_tool(name, arguments)
    out, err = capsys.readouterr()
    assert out == ""
    records = json_lines(err)
    events = [r["event"] for r in records]
    assert events.count("tool.executed") == 3
    # FastMCP's own records still arrive, through the gateway's handler and formatter.
    assert any(e.startswith("Error calling tool") for e in events), events
    assert "Traceback" not in err


async def test_rejected_path_argument_logs_no_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO", "text")
    async with running() as rt:
        capsys.readouterr()
        with pytest.raises(ToolError):
            await rt.client.call_tool("catalog_items_retrieve", {"id": ".."})
    err = capsys.readouterr().err
    assert "Traceback" not in err
    lines = [line for line in err.splitlines() if line.strip()]
    # FastMCP's error record, tool.executed and gateway.stopped: one line each.
    assert [line.split(" ", 2)[1] for line in lines] == [
        "Error",
        "tool.executed",
        "gateway.stopped",
    ]
    assert lines[0] == "ERROR Error calling tool 'catalog_items_retrieve'"


async def test_traceback_logged_only_at_debug(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("DEBUG", "json")
    async with running() as rt:
        capsys.readouterr()
        with pytest.raises(ToolError):
            await rt.client.call_tool("catalog_items_retrieve", {"id": ".."})
    records = json_lines(capsys.readouterr().err)
    assert any("Traceback" in r.get("traceback", "") for r in records), records


async def test_cancelled_call_logged_as_cancelled(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "text")
    middleware = ToolLoggingMiddleware({"messaging_items_list": ("messaging", "items_list")})
    context = SimpleNamespace(message=SimpleNamespace(name="messaging_items_list"))

    async def call_next(_: object) -> object:
        current_call.get()["method"] = "GET"  # type: ignore[index]
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await middleware.on_call_tool(context, call_next)  # type: ignore[arg-type]
    lines = [line for line in capsys.readouterr().err.splitlines() if "tool.executed" in line]
    assert len(lines) == 1
    assert "backend=messaging operation=items_list method=GET status=None" in lines[0]
    assert "error=cancelled" in lines[0]


# --- lifecycle and isolation ------------------------------------------------------


async def test_clients_closed_after_shutdown() -> None:
    async with running() as rt:
        await rt.client.call_tool("catalog_items_list", {})
        assert not any(t.closed for t in rt.transports.values())
    assert all(t.closed for t in rt.transports.values())


async def test_concurrent_calls_do_not_share_state() -> None:
    async with running() as rt:
        calls = [
            rt.client.call_tool(
                "catalog_items_retrieve" if i % 2 else "messaging_items_retrieve",
                {"id": str(i)} if i % 2 else {"id": i},
            )
            for i in range(50)
        ]
        results = await asyncio.gather(*calls)
    assert [r.structured_content for r in results] == [{"path": f"/items/{i}"} for i in range(50)]
    assert len(rt.app_seen) == 25
    assert len(rt.pay_seen) == 25
    for request in rt.app_seen:
        assert request.headers["authorization"] == "Bearer app-token"
        assert "x-api-key" not in request.headers
        assert int(request.url.path.rsplit("/", 1)[1]) % 2 == 1
    for request in rt.pay_seen:
        assert request.headers["x-api-key"] == "pay-key"
        assert "authorization" not in request.headers
        assert int(request.url.path.rsplit("/", 1)[1]) % 2 == 0


async def test_schema_update_changes_input_schema(tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    async with running(path=root) as rt:
        before = {t.name: t for t in await rt.client.list_tools()}
    assert "sort" not in before["catalog_items_list"].input_schema["properties"]
    schema = root / "backends" / "catalog" / "openapi.yaml"
    document = yaml.safe_load(schema.read_text())
    document["paths"]["/items"]["get"]["parameters"].append(
        {"name": "sort", "in": "query", "schema": {"type": "string"}}
    )
    schema.write_text(yaml.safe_dump(document))
    async with running(path=root) as rt:
        after = {t.name: t for t in await rt.client.list_tools()}
        await rt.client.call_tool("catalog_items_list", {"sort": "name"})
    assert "sort" in after["catalog_items_list"].input_schema["properties"]
    assert dict(rt.app_seen[0].url.params) == {"sort": "name"}


def test_mcp_subpackage_does_not_shadow_third_party_mcp() -> None:
    import mcp

    assert "openapi_mcp_gateway" not in (mcp.__file__ or "")
    assert gateway_module.__name__ == "openapi_mcp_gateway.mcp.gateway"

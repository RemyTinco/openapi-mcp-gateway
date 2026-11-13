from __future__ import annotations

import gzip
import json
import os
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastmcp.exceptions import ToolError

from openapi_mcp_gateway.config.models import AuthApiKey, AuthBearer, AuthNone, BackendConfig
from openapi_mcp_gateway.exceptions import CredentialsError
from openapi_mcp_gateway.http import client as client_module
from openapi_mcp_gateway.http.client import (
    MAX_REDIRECTS,
    GatewayToolError,
    GatewayTransport,
    build_client,
    current_call,
    origin_of,
)

SECRET = "t0k"
ENV = {"CATALOG_API_TOKEN": SECRET}
BASE = "https://catalog.example.com"
LIMIT = 1048576

Handler = Callable[[httpx2.Request], httpx2.Response]


def make_cfg(**overrides: Any) -> BackendConfig:
    cfg = BackendConfig(
        name="catalog",
        directory=Path("/nonexistent"),
        config_path=Path("/nonexistent/backend.yaml"),
        schema_path=Path("/nonexistent/openapi.yaml"),
        base_url=BASE,
        auth=AuthBearer(type="bearer", token_env="CATALOG_API_TOKEN"),
        mode="read_only",
        operations=(),
        exclude_operations=(),
        exclude_paths=(),
        timeout_seconds=30.0,
        follow_redirects=False,
        max_response_bytes=LIMIT,
        max_connections=10,
    )
    return replace(cfg, **overrides)


class Recorder:
    """Inner-transport handler that records every request it receives."""

    def __init__(self, respond: Handler) -> None:
        self.respond = respond
        self.calls: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.calls.append(request)
        return self.respond(request)


def make_client(respond: Handler, **overrides: Any) -> tuple[httpx2.AsyncClient, Recorder]:
    recorder = Recorder(respond)
    client = build_client(
        make_cfg(**overrides), ENV, inner_transport=httpx2.MockTransport(recorder)
    )
    return client, recorder


def json_response(status: int = 200, body: object = None, **headers: str) -> Handler:
    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            status, json=body if body is not None else {"ok": True}, headers=headers
        )

    return respond


class WireStream(httpx2.AsyncByteStream):
    """Body as it arrives from the network: not pre-read, possibly still encoded."""

    def __init__(self, content: bytes, chunk_size: int = 65536) -> None:
        self.content = content
        self.chunk_size = chunk_size

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for i in range(0, len(self.content), self.chunk_size):
            yield self.content[i : i + self.chunk_size]


def raw_response(status: int, content: bytes, headers: dict[str, str] | None = None) -> Handler:
    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, headers=headers or {}, stream=WireStream(content))

    return respond


def raising(exc: Exception) -> Handler:
    def respond(request: httpx2.Request) -> httpx2.Response:
        raise exc

    return respond


async def call_error(
    respond: Handler, url: str = "/x", **overrides: Any
) -> tuple[GatewayToolError, Recorder]:
    client, recorder = make_client(respond, **overrides)
    async with client:
        with pytest.raises(GatewayToolError) as e:
            await client.get(url)
    return e.value, recorder


# --- origin_of ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://catalog.example.com", ("https", "catalog.example.com", 443)),
        ("https://CATALOG.Example.COM/x", ("https", "catalog.example.com", 443)),
        ("HTTPS://catalog.example.com:443/x", ("https", "catalog.example.com", 443)),
        ("http://catalog.example.com", ("http", "catalog.example.com", 80)),
        ("http://catalog.example.com:80/", ("http", "catalog.example.com", 80)),
        ("https://catalog.example.com:8443", ("https", "catalog.example.com", 8443)),
        ("https://[::1]/x", ("https", "::1", 443)),
        ("https://[2001:DB8::1]:443/x", ("https", "2001:db8::1", 443)),
        ("http://[2001:db8::1]:8080", ("http", "2001:db8::1", 8080)),
    ],
)
def test_origin_of(url: str, expected: tuple[str, str, int]) -> None:
    assert origin_of(url) == expected


def test_origin_of_default_port_equals_explicit() -> None:
    assert origin_of("https://x.com:443") == origin_of("https://x.com")
    assert origin_of("https://x.com:443") != origin_of("http://x.com:443")


# --- status mapping ----------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "validation_error"),
        (401, "authentication_failed"),
        (403, "authorization_failed"),
        (404, "not_found"),
        (429, "rate_limited"),
        (418, "upstream_client_error"),
        (500, "upstream_error"),
        (503, "upstream_error"),
    ],
)
async def test_status_mapping(status: int, code: str) -> None:
    err, _ = await call_error(json_response(status, {"error": "nope"}))
    assert err.payload["error"] == code
    assert err.payload["status"] == status
    assert err.payload["backend"] == "catalog"
    assert isinstance(err.payload["message"], str)
    assert err.payload["message"]


async def test_error_is_tool_error_with_json_message() -> None:
    err, _ = await call_error(json_response(404))
    assert isinstance(err, ToolError)
    assert str(err) == json.dumps(err.payload)
    assert json.loads(str(err)) == err.payload


async def test_4xx_detail_carries_upstream_text() -> None:
    err, _ = await call_error(raw_response(400, b"field 'x' is required"))
    assert err.payload["detail"] == "field 'x' is required"


async def test_4xx_detail_bounded() -> None:
    err, _ = await call_error(raw_response(400, b"e" * 5000))
    assert len(err.payload["detail"]) <= 1000


async def test_5xx_body_not_forwarded() -> None:
    err, _ = await call_error(raw_response(500, b"Traceback: stack trace at db.py line 3"))
    assert "detail" not in err.payload
    assert "stack trace" not in str(err)


async def test_429_retry_after() -> None:
    err, _ = await call_error(json_response(429, {"e": 1}, **{"Retry-After": "30"}))
    assert err.payload["retry_after"] == "30"


async def test_429_without_retry_after_has_no_key() -> None:
    err, _ = await call_error(json_response(429))
    assert "retry_after" not in err.payload


async def test_non_429_ignores_retry_after() -> None:
    err, _ = await call_error(json_response(503, {"e": 1}, **{"Retry-After": "30"}))
    assert "retry_after" not in err.payload


# --- transport errors --------------------------------------------------------


async def test_timeout() -> None:
    err, _ = await call_error(raising(httpx2.ReadTimeout("read timed out after 30s")))
    assert err.payload["error"] == "timeout"
    assert err.payload["status"] is None


async def test_connect_error() -> None:
    err, _ = await call_error(raising(httpx2.ConnectError("[Errno 111] internal-db-7:5432")))
    assert err.payload["error"] == "backend_unavailable"
    assert err.payload["status"] is None
    assert "internal-db-7" not in str(err)


# --- origin enforcement ------------------------------------------------------


async def test_absolute_url_other_host_rejected_before_send() -> None:
    err, recorder = await call_error(json_response(), url="https://evil.example.com/x")
    assert err.payload["error"] == "origin_rejected"
    assert err.payload["status"] is None
    assert recorder.calls == []  # handler never invoked, so no credentials left the process


@pytest.mark.parametrize(
    "url",
    [
        "http://catalog.example.com/x",
        "https://catalog.example.com:8443/x",
        "https://CATALOG.example.com.evil.io/x",
    ],
)
async def test_scheme_port_host_mismatch(url: str) -> None:
    err, recorder = await call_error(json_response(), url=url)
    assert err.payload["error"] == "origin_rejected"
    assert recorder.calls == []


@pytest.mark.parametrize(
    "url",
    [
        "https://CATALOG.EXAMPLE.COM/x",
        "https://catalog.example.com:443/x",
    ],
)
async def test_same_origin_spellings_accepted(url: str) -> None:
    client, recorder = make_client(json_response())
    async with client:
        r = await client.get(url)
    assert r.status_code == 200
    assert len(recorder.calls) == 1


async def test_ipv6_backend_origin() -> None:
    client, recorder = make_client(json_response(), base_url="https://[2001:db8::1]:8443")
    async with client:
        assert (await client.get("/x")).status_code == 200
        with pytest.raises(GatewayToolError) as e:
            await client.get("https://[2001:db8::2]:8443/x")
    assert e.value.payload["error"] == "origin_rejected"
    assert len(recorder.calls) == 1


async def test_origin_check_uses_parsed_request_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # The per-request check compares request.url's own fields, the ones the connection
    # uses; it never re-parses str(request.url). origin_of is only for the base_url.
    client, recorder = make_client(json_response())

    def no_reparse(url: str) -> tuple[str, str, int]:
        msg = f"origin_of called per request with {url!r}"
        raise AssertionError(msg)

    monkeypatch.setattr(client_module, "origin_of", no_reparse)
    async with client:
        assert (await client.get("/x")).status_code == 200
        with pytest.raises(GatewayToolError) as e:
            await client.get("https://evil.example.com/x")
    assert e.value.payload["error"] == "origin_rejected"
    assert len(recorder.calls) == 1


async def test_punycode_backend_origin() -> None:
    # httpx2 shows an IDNA host decoded in .host; the check uses the wire form.
    client, recorder = make_client(json_response(), base_url="https://xn--bcher-kva.example")
    async with client:
        assert (await client.get("/x")).status_code == 200
        assert (await client.get("https://bücher.example/y")).status_code == 200
        with pytest.raises(GatewayToolError) as e:
            await client.get("https://bucher.example/x")
    assert e.value.payload["error"] == "origin_rejected"
    assert [c.url.raw_host for c in recorder.calls] == [b"xn--bcher-kva.example"] * 2


async def test_caller_authorization_header_to_other_origin_not_sent() -> None:
    client, recorder = make_client(json_response())
    async with client:
        request = client.build_request(
            "GET", "https://evil.example.com/x", headers={"Authorization": "Bearer other"}
        )
        with pytest.raises(GatewayToolError) as e:
            await client.send(request)
    assert e.value.payload["error"] == "origin_rejected"
    assert recorder.calls == []


# --- redirects ---------------------------------------------------------------


def redirecting(location: str) -> Handler:
    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/x":
            return httpx2.Response(302, headers={"Location": location})
        return httpx2.Response(200, json={"hop": request.url.path})

    return respond


async def test_redirect_not_followed_by_default() -> None:
    err, recorder = await call_error(redirecting("/y"))
    assert err.payload["error"] == "redirect_not_followed"
    assert err.payload["status"] == 302
    assert len(recorder.calls) == 1


async def test_cross_origin_redirect_rejected_when_following() -> None:
    err, recorder = await call_error(
        redirecting("https://evil.example.com/y"), follow_redirects=True
    )
    assert err.payload["error"] == "origin_rejected"
    assert [str(c.url) for c in recorder.calls] == [f"{BASE}/x"]  # second hop not sent


async def test_same_origin_redirect_followed_when_enabled() -> None:
    client, recorder = make_client(redirecting("/y"), follow_redirects=True)
    async with client:
        r = await client.get("/x")
    assert r.status_code == 200
    assert r.json() == {"hop": "/y"}
    assert [c.url.path for c in recorder.calls] == ["/x", "/y"]
    assert recorder.calls[1].headers["authorization"] == f"Bearer {SECRET}"


async def test_3xx_without_location_not_followed_even_when_enabled() -> None:
    err, _ = await call_error(raw_response(300, b"choices"), follow_redirects=True)
    assert err.payload["error"] == "redirect_not_followed"
    assert err.payload["status"] == 300


async def test_redirect_loop_is_structured_backend_unavailable() -> None:
    # httpx2 would raise TooManyRedirects after the transport, which FastMCP masks;
    # the transport stops the loop itself, at the same hop httpx2 would.
    err, recorder = await call_error(redirecting("/x"), follow_redirects=True)
    assert err.payload["error"] == "backend_unavailable"
    assert err.payload["status"] == 302
    assert "redirect" in err.payload["message"]
    assert len(recorder.calls) == MAX_REDIRECTS + 1
    assert {c.url.host for c in recorder.calls} == {"catalog.example.com"}


async def test_redirect_hops_below_limit_still_followed() -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        hop = int(request.url.path.strip("/") or 0)
        if hop < 20:
            return httpx2.Response(302, headers={"Location": f"/{hop + 1}"})
        return httpx2.Response(200, json={"hop": hop})

    client, recorder = make_client(respond, follow_redirects=True)
    async with client:
        r = await client.get("/0")
    assert r.json() == {"hop": 20}
    assert len(recorder.calls) == 21


@pytest.mark.parametrize(
    "location",
    [
        b"http://[::1",  # httpx2: InvalidURL -> RemoteProtocolError after the transport
        b"https://catalog.example.com\x00/y",
        b"http://[zz]/y",
        "https://-\u00e9.example/y".encode(),  # invalid IDNA label
        "https://\u2603.example:x/y".encode(),
    ],
)
async def test_malformed_redirect_location_is_backend_unavailable(location: bytes) -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(302, headers=[(b"Location", location)])

    err, recorder = await call_error(respond, follow_redirects=True)
    assert err.payload["error"] == "backend_unavailable"
    assert err.payload["status"] == 302
    assert "Location" in err.payload["message"]
    assert len(recorder.calls) == 1


async def test_non_ascii_redirect_location_path_still_followed() -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/x":
            return httpx2.Response(302, headers=[(b"Location", "/caf\u00e9".encode())])
        return httpx2.Response(200, json={"hop": request.url.raw_path.decode()})

    client, _ = make_client(respond, follow_redirects=True)
    async with client:
        r = await client.get("/x")
    assert r.json() == {"hop": "/caf%C3%A9"}


@pytest.mark.parametrize("follow", [False, True])
async def test_304_reported_as_not_modified(follow: bool) -> None:
    err, _ = await call_error(raw_response(304, b""), follow_redirects=follow)
    assert err.payload["error"] == "redirect_not_followed"
    assert err.payload["status"] == 304
    assert "304 Not Modified" in err.payload["message"]
    assert "responded with a redirect" not in err.payload["message"]


@pytest.mark.parametrize("status", [300, 302, 307])
@pytest.mark.parametrize("follow", [False, True])
async def test_3xx_without_location_message_is_accurate(status: int, follow: bool) -> None:
    err, _ = await call_error(raw_response(status, b""), follow_redirects=follow)
    assert err.payload["error"] == "redirect_not_followed"
    assert err.payload["status"] == status
    assert "without a Location header" in err.payload["message"]


async def test_redirect_with_location_message_unchanged_when_not_following() -> None:
    err, _ = await call_error(redirecting("/y"))
    assert err.payload["message"] == "the backend responded with a redirect, which is not followed"


# --- success bodies ----------------------------------------------------------


async def test_json_ok() -> None:
    client, _ = make_client(json_response(200, {"id": 1}))
    async with client:
        r = await client.get("/x")
    assert r.json() == {"id": 1}


async def test_plus_json_ok() -> None:
    client, _ = make_client(
        raw_response(200, b'{"a": 1}', {"Content-Type": "application/problem+json"})
    )
    async with client:
        r = await client.get("/x")
    assert r.json() == {"a": 1}


async def test_204_empty() -> None:
    client, _ = make_client(raw_response(204, b""))
    async with client:
        r = await client.get("/x")
    assert r.status_code == 204
    assert r.content == b""


async def test_text_plain() -> None:
    client, _ = make_client(raw_response(200, b"hello", {"Content-Type": "text/plain"}))
    async with client:
        r = await client.get("/x")
    assert r.text == "hello"


async def test_binary_rejected() -> None:
    pdf = b"%PDF-1.7 \x00\x01\x02 BINARYMARKER"
    err, _ = await call_error(raw_response(200, pdf, {"Content-Type": "application/pdf"}))
    assert err.payload["error"] == "unsupported_content_type"
    assert err.payload["status"] == 200
    assert "BINARYMARKER" not in str(err)
    assert "PDF-1.7" not in str(err)


async def test_body_without_content_type_rejected() -> None:  # Review Focus 4
    err, _ = await call_error(raw_response(200, b"some bytes"))
    assert err.payload["error"] == "unsupported_content_type"


async def test_invalid_json_with_json_content_type_passes_as_text() -> None:  # Review Focus 4
    client, _ = make_client(
        raw_response(200, b"{not json", {"Content-Type": "application/json; charset=utf-8"})
    )
    async with client:
        r = await client.get("/x")
    assert r.status_code == 200
    assert r.text == "{not json"
    with pytest.raises(json.JSONDecodeError):
        r.json()


async def test_gzip_json_is_decoded_once() -> None:
    body = json.dumps({"id": 7}).encode()
    client, _ = make_client(
        raw_response(
            200,
            gzip.compress(body),
            {"Content-Type": "application/json", "Content-Encoding": "gzip"},
        )
    )
    async with client:
        r = await client.get("/x")
    assert r.json() == {"id": 7}
    assert "content-encoding" not in r.headers
    assert r.headers["content-length"] == str(len(body))


# --- size limits -------------------------------------------------------------


async def test_exactly_at_limit_ok() -> None:
    client, _ = make_client(raw_response(200, b"a" * LIMIT, {"Content-Type": "text/plain"}))
    async with client:
        r = await client.get("/x")
    assert len(r.content) == LIMIT


async def test_over_limit() -> None:
    err, _ = await call_error(raw_response(200, b"a" * (LIMIT + 1), {"Content-Type": "text/plain"}))
    assert err.payload["error"] == "response_too_large"
    assert err.payload["status"] == 200


async def test_gzip_bomb_over_limit_decoded() -> None:  # Review Focus 4
    bomb = gzip.compress(b"0" * (10 * LIMIT))
    assert len(bomb) < LIMIT
    err, _ = await call_error(
        raw_response(200, bomb, {"Content-Type": "text/plain", "Content-Encoding": "gzip"})
    )
    assert err.payload["error"] == "response_too_large"


async def test_wire_bytes_over_limit_even_when_decoded_fits() -> None:
    noise = os.urandom(90)
    wire = gzip.compress(noise)
    assert len(wire) > 100 >= len(noise)
    err, _ = await call_error(
        raw_response(200, wire, {"Content-Type": "text/plain", "Content-Encoding": "gzip"}),
        max_response_bytes=100,
    )
    assert err.payload["error"] == "response_too_large"


class EndlessStream(httpx2.AsyncByteStream):
    def __init__(self, chunk: bytes) -> None:
        self.chunk = chunk
        self.pulled = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            self.pulled += 1
            yield self.chunk

    async def aclose(self) -> None:
        self.closed = True


async def test_streaming_stops_reading_and_closes_over_limit() -> None:
    stream = EndlessStream(b"x" * 1024)

    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"Content-Type": "text/plain"}, stream=stream)

    err, _ = await call_error(respond, max_response_bytes=10 * 1024)
    assert err.payload["error"] == "response_too_large"
    assert stream.pulled <= 11
    assert stream.closed


class EmptyDeflateBlocks(httpx2.AsyncByteStream):
    """A gzip stream of endless empty stored blocks: wire bytes grow, decoded output never does."""

    HEADER = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff"
    EMPTY_BLOCK = b"\x00\x00\x00\xff\xff"

    def __init__(self) -> None:
        self.pulled = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.HEADER
        while True:
            self.pulled += 1
            yield self.EMPTY_BLOCK * 200  # 1000 wire bytes, zero decoded bytes

    async def aclose(self) -> None:
        self.closed = True


async def test_wire_limit_holds_when_nothing_decodes() -> None:
    stream = EmptyDeflateBlocks()

    def respond(request: httpx2.Request) -> httpx2.Response:
        headers = {"Content-Type": "text/plain", "Content-Encoding": "gzip"}
        return httpx2.Response(200, headers=headers, stream=stream)

    err, _ = await call_error(respond, max_response_bytes=10_000)
    assert err.payload["error"] == "response_too_large"
    assert stream.pulled <= 11
    assert stream.closed


async def test_corrupt_gzip_is_upstream_error() -> None:
    err, _ = await call_error(
        raw_response(
            200, b"not gzip at all", {"Content-Type": "text/plain", "Content-Encoding": "gzip"}
        )
    )
    assert err.payload["error"] == "upstream_error"
    assert err.payload["status"] == 200
    assert "not gzip" not in str(err)


# --- credentials and client configuration ------------------------------------


async def test_auth_header_sent() -> None:
    client, recorder = make_client(json_response())
    async with client:
        await client.get("/x")
    assert recorder.calls[0].headers["authorization"] == f"Bearer {SECRET}"
    assert str(recorder.calls[0].url) == f"{BASE}/x"


async def test_api_key_header_sent() -> None:
    recorder = Recorder(json_response())
    cfg = make_cfg(
        auth=AuthApiKey(
            type="api_key", location="header", name="X-API-Key", value_env="MESSAGING_API_KEY"
        )
    )
    client = build_client(
        cfg, {"MESSAGING_API_KEY": "k3y"}, inner_transport=httpx2.MockTransport(recorder)
    )
    async with client:
        await client.get("/x")
    assert recorder.calls[0].headers["x-api-key"] == "k3y"
    assert "authorization" not in recorder.calls[0].headers


async def test_caller_cannot_override_bearer_credential_or_host() -> None:
    client, recorder = make_client(redirecting("/y"), follow_redirects=True)
    async with client:
        await client.get(
            "/x", headers={"Authorization": "Bearer agent-chosen", "Host": "admin.internal"}
        )
    assert len(recorder.calls) == 2  # the redirect hop is checked too
    for request in recorder.calls:
        assert request.headers.get_list("authorization") == [f"Bearer {SECRET}"]
        assert request.headers.get_list("host") == ["catalog.example.com"]


async def test_caller_cannot_override_api_key_header_in_any_case() -> None:
    recorder = Recorder(json_response())
    cfg = make_cfg(
        base_url="https://catalog.example.com:8443",
        auth=AuthApiKey(
            type="api_key", location="header", name="X-API-Key", value_env="MESSAGING_API_KEY"
        ),
    )
    client = build_client(
        cfg, {"MESSAGING_API_KEY": "k3y"}, inner_transport=httpx2.MockTransport(recorder)
    )
    async with client:
        await client.get("/x", headers={"x-api-key": "agent-chosen", "HOST": "other:1"})
    assert recorder.calls[0].headers.get_list("x-api-key") == ["k3y"]
    assert recorder.calls[0].headers.get_list("host") == ["catalog.example.com:8443"]


async def test_caller_authorization_kept_when_backend_has_no_auth() -> None:
    # Only configured credentials are pinned; the Host header always is.
    client, recorder = make_client(json_response(), auth=AuthNone(type="none"))
    async with client:
        await client.get("/x", headers={"Authorization": "Bearer agent", "Host": "evil"})
    assert recorder.calls[0].headers["authorization"] == "Bearer agent"
    assert recorder.calls[0].headers["host"] == "catalog.example.com"


def test_build_client_missing_credentials() -> None:
    with pytest.raises(CredentialsError, match="CATALOG_API_TOKEN"):
        build_client(make_cfg(), {}, inner_transport=httpx2.MockTransport(json_response()))


async def test_build_client_settings() -> None:
    client = build_client(
        make_cfg(base_url=f"{BASE}/api/v1", timeout_seconds=5.0, follow_redirects=True),
        ENV,
        inner_transport=httpx2.MockTransport(json_response()),
    )
    async with client:
        assert str(client.base_url) == f"{BASE}/api/v1/"
        assert client.timeout == httpx2.Timeout(5.0)
        assert client.follow_redirects is True


async def test_build_client_default_inner_transport() -> None:
    client = build_client(make_cfg(), ENV)
    async with client:
        transport = client._transport
        assert isinstance(transport, GatewayTransport)
        assert isinstance(transport.inner, httpx2.AsyncHTTPTransport)


def test_default_inner_transport_keeps_tls_trust_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Proxies and .netrc are ignored (client trust_env=False), but the inner transport
    # still reads SSL_CERT_FILE / SSL_CERT_DIR so a private CA can be trusted.
    import httpcore2

    seen: list[bool] = []
    real = httpcore2.default_ssl_context

    def spy(*, _trust_env: bool = True) -> Any:
        seen.append(_trust_env)
        return real(_trust_env=_trust_env)

    monkeypatch.setattr(httpcore2, "default_ssl_context", spy)
    client = build_client(make_cfg(), ENV)
    assert client.trust_env is False
    assert seen == [True]


# --- current_call ------------------------------------------------------------


async def test_current_call_records_status() -> None:
    client, _ = make_client(json_response())
    record: dict[str, Any] = {}
    token = current_call.set(record)
    try:
        async with client:
            await client.post("/x", json={"a": 1})
    finally:
        current_call.reset(token)
    assert record == {"method": "POST", "status": 200}


async def test_current_call_records_error_status() -> None:
    record: dict[str, Any] = {}
    token = current_call.set(record)
    try:
        await call_error(json_response(404))
    finally:
        current_call.reset(token)
    assert record == {"method": "GET", "status": 404}


async def test_current_call_records_method_on_origin_rejection() -> None:
    record: dict[str, Any] = {}
    token = current_call.set(record)
    try:
        await call_error(json_response(), url="https://evil.example.com/x")
    finally:
        current_call.reset(token)
    assert record == {"method": "GET"}


async def test_current_call_unset_is_harmless() -> None:
    assert current_call.get() is None
    client, _ = make_client(json_response())
    async with client:
        assert (await client.get("/x")).status_code == 200


# --- no secrets, bodies or exception text in errors ---------------------------


def failing_cases() -> Iterator[tuple[Handler, str, dict[str, Any]]]:
    secretish = f"Bearer {SECRET} REQUESTBODY"
    for status in (400, 401, 403, 404, 418, 429, 500, 503):
        yield raw_response(status, b"upstream says no"), "/x", {}
    yield raising(httpx2.ReadTimeout(secretish)), "/x", {}
    yield raising(httpx2.ConnectError(secretish)), "/x", {}
    yield json_response(), "https://evil.example.com/x", {}
    yield redirecting("/y"), "/x", {}
    yield redirecting("https://evil.example.com/y"), "/x", {"follow_redirects": True}
    yield raw_response(200, b"\x00RESPONSEBODY", {"Content-Type": "application/pdf"}), "/x", {}
    yield (
        raw_response(200, b"RESPONSEBODY" * 10, {"Content-Type": "text/plain"}),
        "/x",
        {"max_response_bytes": 50},
    )
    yield (
        raw_response(
            200, b"RESPONSEBODY", {"Content-Type": "text/plain", "Content-Encoding": "gzip"}
        ),
        "/x",
        {},
    )


async def test_secret_not_in_any_error() -> None:
    count = 0
    for respond, url, overrides in failing_cases():
        client, _ = make_client(respond, **overrides)
        async with client:
            with pytest.raises(GatewayToolError) as e:
                await client.post(url, content=b"REQUESTBODY")
        text = str(e.value) + repr(e.value) + json.dumps(e.value.payload)
        assert SECRET not in text
        assert "REQUESTBODY" not in text
        assert "RESPONSEBODY" not in text
        count += 1
    assert count == 16


async def test_secret_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    for respond, url, overrides in failing_cases():
        client, _ = make_client(respond, **overrides)
        async with client:
            with pytest.raises(GatewayToolError):
                await client.get(url)
    logged = caplog.text + "".join(repr(vars(r)) for r in caplog.records)
    assert SECRET not in logged
    assert "RESPONSEBODY" not in logged

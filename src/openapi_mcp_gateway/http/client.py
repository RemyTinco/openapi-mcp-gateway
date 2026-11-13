"""Per-backend HTTP client and the transport every outbound request passes through.

``GatewayTransport`` is the single choke point for backend traffic: it refuses any
request (including each redirect hop) whose origin differs from the backend's,
pins the configured credential and the ``Host`` header, bounds the response body,
and turns every failure into a ``GatewayToolError`` whose message is a JSON
payload safe to show an MCP client.
"""

from __future__ import annotations

import contextlib
import json
import logging
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx2
from fastmcp.exceptions import ToolError

from openapi_mcp_gateway.http.authentication import auth_headers
from openapi_mcp_gateway.logging import log_event

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from openapi_mcp_gateway.config.models import BackendConfig

logger = logging.getLogger(__name__)

DETAIL_MAX_CHARS = 1000
MAX_REDIRECTS = 20
"""Redirect hops followed per request (httpx2's default, set explicitly on the client)."""
# httpx2 copies request extensions onto each redirect request, so a hop count
# stored here travels along one redirect chain and nowhere else.
_HOPS_EXTENSION = "openapi_mcp_gateway.redirect_hops"
_DEFAULT_PORTS = {"http": 80, "https": 443}
_STATUS_ERRORS = {
    400: ("validation_error", "the backend rejected the request as invalid"),
    401: ("authentication_failed", "the backend rejected the gateway's credentials"),
    403: ("authorization_failed", "the backend denied access to this operation"),
    404: ("not_found", "the backend could not find the requested resource"),
    429: ("rate_limited", "the backend's rate limit was exceeded"),
}
# Headers describing the wire encoding; dropped because the rebuilt body is decoded.
_WIRE_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})

current_call: ContextVar[dict[str, Any] | None] = ContextVar("current_call", default=None)
"""When set, the transport records the request ``method`` and response ``status`` here."""


class GatewayToolError(ToolError):
    """Tool failure whose message is ``json.dumps(payload)``; never carries secrets."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(json.dumps(payload))
        self.payload = payload


def origin_of(url: str) -> tuple[str, str, int]:
    """``(scheme, lowercase host, port)``; default ports 80/443 are made explicit.

    Raises ``ValueError`` for a malformed port.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    port = parts.port
    if port is None:
        port = _DEFAULT_PORTS.get(scheme, 0)
    return scheme, (parts.hostname or "").lower(), port


def _record(**fields: Any) -> None:
    call = current_call.get()
    if call is not None:
        call.update(fields)


def _is_accepted_content_type(media_type: str) -> bool:
    return (
        media_type == "application/json"
        or media_type.endswith("+json")
        or media_type.startswith("text/")
    )


def _decode_text(body: bytes, charset: str | None) -> str:
    try:
        return body.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


class _WireLimitedStream(httpx2.AsyncByteStream):
    """Counts bytes as received from the network and stops once over ``limit``."""

    def __init__(self, inner: httpx2.AsyncByteStream, limit: int) -> None:
        self.inner = inner
        self.limit = limit
        self.received = 0
        self.exceeded = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self.inner:
            self.received += len(chunk)
            if self.received > self.limit:
                self.exceeded = True
                return  # the caller then closes the response, and with it ``inner``
            yield chunk

    async def aclose(self) -> None:
        await self.inner.aclose()


class GatewayTransport(httpx2.AsyncBaseTransport):
    """Origin-pinned, size-bounded wrapper around a real transport."""

    def __init__(  # noqa: PLR0913 - one keyword per independent setting
        self,
        inner: httpx2.AsyncBaseTransport,
        *,
        backend: str,
        origin: tuple[str, str, int],
        max_response_bytes: int,
        follow_redirects: bool = False,
        max_redirects: int = MAX_REDIRECTS,
        auth_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.inner = inner
        self.backend = backend
        self.origin = origin
        self.auth_headers = dict(auth_headers or {})
        self.max_response_bytes = max_response_bytes
        self.follow_redirects = follow_redirects
        self.max_redirects = max_redirects

    def _error(self, code: str, message: str, status: int | None, **extra: Any) -> GatewayToolError:
        payload: dict[str, Any] = {
            "error": code,
            "backend": self.backend,
            "status": status,
            "message": message,
        }
        payload.update(extra)
        return GatewayToolError(payload)

    def _check_origin(self, request: httpx2.Request) -> None:
        # Compare the fields the connection is opened with (httpx2 hands raw_host and
        # port to the connection pool), not a re-parse of the URL text.
        url = request.url
        scheme = url.scheme.lower()
        port = url.port if url.port is not None else _DEFAULT_PORTS.get(scheme, 0)
        try:
            host = url.raw_host.decode("ascii").lower()
        except UnicodeDecodeError:
            host = None
        if (scheme, host, port) != self.origin:
            log_event(
                logger,
                logging.WARNING,
                "http.origin_rejected",
                backend=self.backend,
                method=request.method,
                host=request.url.host,
            )
            raise self._error(
                "origin_rejected",
                "request URL is outside the backend's configured origin; nothing was sent",
                None,
            )

    def _pin_headers(self, request: httpx2.Request) -> None:
        """Make configured credentials and the real ``Host`` win over caller headers.

        An OpenAPI document may declare ``Authorization``, the API-key header or
        ``Host`` as a header parameter; the agent's value must never replace the
        configured credential or route the request to another virtual host.
        """
        for name, value in self.auth_headers.items():
            request.headers[name] = value
        request.headers["Host"] = request.url.netloc.decode("ascii")

    def _check_redirect_location(self, request: httpx2.Request, response: httpx2.Response) -> None:
        """Resolve ``Location`` the way httpx2 will; a value it cannot use is a tool error.

        httpx2 builds the next request after the transport has returned, where a
        malformed ``Location`` raises ``InvalidURL`` / ``RemoteProtocolError`` /
        ``UnicodeError`` that would reach the client unstructured.
        """
        try:
            url = httpx2.URL(response.headers["Location"])
            if url.scheme and not url.host:
                url = url.copy_with(host=request.url.host)
            if url.is_relative_url:
                url = request.url.join(url)
            url.netloc.decode("ascii")
        except httpx2.InvalidURL, ValueError:  # UnicodeError is a ValueError
            raise self._error(
                "backend_unavailable",
                "the backend's redirect has a Location header that is not a valid URL",
                response.status_code,
            ) from None

    def _transport_error(self, exc: httpx2.TransportError) -> GatewayToolError:
        if isinstance(exc, httpx2.TimeoutException):
            return self._error("timeout", "the request to the backend timed out", None)
        return self._error("backend_unavailable", "the backend could not be reached", None)

    async def _read_bounded(self, response: httpx2.Response) -> tuple[bytes, bool]:
        """Read at most ``max_response_bytes + 1`` decoded bytes; True if over the limit.

        Both the decoded size and the bytes received on the wire count; reading
        stops and the response is closed as soon as either exceeds the limit.
        """
        limit = self.max_response_bytes
        wire: _WireLimitedStream | None = None
        if isinstance(response.stream, httpx2.AsyncByteStream):
            wire = _WireLimitedStream(response.stream, limit)
            response.stream = wire
        body = bytearray()
        over = False
        try:
            async with contextlib.aclosing(response.aiter_bytes()) as chunks:
                async for chunk in chunks:
                    body += chunk[: limit + 1 - len(body)]
                    if len(body) > limit or response.num_bytes_downloaded > limit:
                        over = True
                        break
        finally:
            await response.aclose()
        return bytes(body), over or (wire is not None and wire.exceeded)

    def _rebuild(
        self, response: httpx2.Response, body: bytes, request: httpx2.Request
    ) -> httpx2.Response:
        # Raw bytes: decoded values with non-ASCII text would not re-encode.
        headers = [
            (k, v)
            for k, v in response.headers.raw
            if k.decode("latin-1").lower() not in _WIRE_HEADERS
        ]
        return httpx2.Response(
            response.status_code,
            headers=headers,
            content=body,
            request=request,
            extensions=response.extensions,
        )

    # One linear pass over every outcome of a request; splitting it would hide the order
    # in which the checks run.
    async def handle_async_request(  # noqa: C901, PLR0912, PLR0915
        self, request: httpx2.Request
    ) -> httpx2.Response:
        """Send one request (or redirect hop) under every guard of this transport."""
        _record(method=request.method)
        self._check_origin(request)
        self._pin_headers(request)

        try:
            response = await self.inner.handle_async_request(request)
        except httpx2.TransportError as exc:
            raise self._transport_error(exc) from None

        status = response.status_code
        _record(status=status)
        try:
            body, over_limit = await self._read_bounded(response)
        except httpx2.TransportError as exc:
            raise self._transport_error(exc) from None
        except httpx2.DecodingError:
            raise self._error(
                "upstream_error", "the backend's response could not be decoded", status
            ) from None

        too_large = self._error(
            "response_too_large",
            f"the backend's response exceeds the {self.max_response_bytes}-byte limit",
            status,
        )
        if httpx2.codes.is_success(status):
            if over_limit:
                raise too_large
            content_type = response.headers.get("content-type")
            media_type = (content_type or "").split(";", 1)[0].strip().lower()
            if body and not _is_accepted_content_type(media_type):
                shown = repr(media_type[:100]) if media_type else "none"
                raise self._error(
                    "unsupported_content_type",
                    f"unsupported response content type {shown}; "
                    "only JSON and text responses are supported",
                    status,
                )
            return self._rebuild(response, body, request)

        if httpx2.codes.is_redirect(status):
            if self.follow_redirects and response.has_redirect_location:
                if over_limit:
                    raise too_large
                self._check_redirect_location(request, response)
                # Stop where httpx2 would raise TooManyRedirects, which is not a ToolError.
                hops = int(request.extensions.get(_HOPS_EXTENSION, 0)) + 1
                if hops > self.max_redirects:
                    raise self._error(
                        "backend_unavailable",
                        f"the backend exceeded the redirect limit of {self.max_redirects}",
                        status,
                    )
                request.extensions[_HOPS_EXTENSION] = hops
                return self._rebuild(response, body, request)  # next hop re-enters here
            if status == httpx2.codes.NOT_MODIFIED:
                message = (
                    "the backend responded 304 Not Modified; conditional requests are not "
                    "supported, so there is no body to return"
                )
            elif "location" not in response.headers:
                message = f"the backend responded with status {status} without a Location header"
            elif response.has_redirect_location:
                message = "the backend responded with a redirect, which is not followed"
            else:
                message = f"the backend responded with status {status}, which cannot be followed"
            raise self._error("redirect_not_followed", message, status)

        if httpx2.codes.is_client_error(status):
            code, message = _STATUS_ERRORS.get(
                status, ("upstream_client_error", f"the backend rejected the request ({status})")
            )
            extra: dict[str, Any] = {
                "detail": _decode_text(body, response.charset_encoding)[:DETAIL_MAX_CHARS]
            }
            retry_after = response.headers.get("retry-after")
            if status == httpx2.codes.TOO_MANY_REQUESTS and retry_after is not None:
                extra["retry_after"] = retry_after
            raise self._error(code, message, status, **extra)

        raise self._error("upstream_error", f"the backend failed with status {status}", status)

    async def aclose(self) -> None:
        """Close the wrapped transport."""
        await self.inner.aclose()


def build_client(
    cfg: BackendConfig,
    env: Mapping[str, str],
    *,
    inner_transport: httpx2.AsyncBaseTransport | None = None,
) -> httpx2.AsyncClient:
    """Build an ``AsyncClient`` bound to one backend's origin, credentials and limits."""
    headers = auth_headers(cfg.auth, env)
    if inner_transport is None:
        # The client's trust_env=False below stops environment proxies and .netrc. This
        # transport keeps its default trust_env=True on purpose: it reads only
        # SSL_CERT_FILE / SSL_CERT_DIR, so a private CA bundle can still be trusted.
        inner_transport = httpx2.AsyncHTTPTransport(
            retries=0, limits=httpx2.Limits(max_connections=cfg.max_connections)
        )
    transport = GatewayTransport(
        inner_transport,
        backend=cfg.name,
        origin=origin_of(cfg.base_url),
        max_response_bytes=cfg.max_response_bytes,
        follow_redirects=cfg.follow_redirects,
        max_redirects=MAX_REDIRECTS,
        auth_headers=headers,
    )
    return httpx2.AsyncClient(
        base_url=cfg.base_url,
        headers=headers,
        timeout=httpx2.Timeout(cfg.timeout_seconds),
        follow_redirects=cfg.follow_redirects,
        max_redirects=MAX_REDIRECTS,
        transport=transport,
        trust_env=False,
    )

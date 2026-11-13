"""One FastMCP ``OpenAPIProvider`` per prepared backend."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp.server.providers.openapi import OpenAPIProvider

from openapi_mcp_gateway.openapi.operations import slug

if TYPE_CHECKING:
    import httpx2

    from openapi_mcp_gateway.pipeline import PreparedBackend


def build_provider(prepared: PreparedBackend, client: httpx2.AsyncClient) -> OpenAPIProvider:
    """Tools for exactly the operations left in ``prepared.document``.

    Names are the un-namespaced slugs; the server adds the backend namespace.
    The explicit client makes its ``base_url`` authoritative over any ``servers``.
    """
    return OpenAPIProvider(
        prepared.document,
        client,
        mcp_names={op_id: slug(op_id) for op_id in prepared.tool_names},
        validate_output=False,
    )

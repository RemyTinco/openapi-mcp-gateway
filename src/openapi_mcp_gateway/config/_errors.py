"""Helpers for reporting several errors at once."""

from __future__ import annotations

from typing import TYPE_CHECKING

from openapi_mcp_gateway.exceptions import GatewayError, MultipleErrors

if TYPE_CHECKING:
    from collections.abc import Iterable


def raise_collected(errors: Iterable[GatewayError]) -> None:
    """Raise nothing for no errors, the error itself for one, else MultipleErrors."""
    flat: list[GatewayError] = []
    for error in errors:
        if isinstance(error, MultipleErrors):
            flat.extend(error.errors)
        else:
            flat.append(error)
    if len(flat) == 1:
        raise flat[0]
    if flat:
        raise MultipleErrors(flat)

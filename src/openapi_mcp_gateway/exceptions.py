"""Exception hierarchy; each class maps to a process exit code."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from pathlib import Path


class GatewayError(Exception):
    """Base error carrying optional backend, file and field context."""

    exit_code: ClassVar[int] = 1

    def __init__(
        self,
        message: str,
        *,
        backend: str | None = None,
        file: Path | None = None,
        field: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.backend = backend
        self.file = file
        self.field = field

    def __str__(self) -> str:
        parts: list[str] = []
        if self.backend is not None:
            parts.append(f"[{self.backend}]")
        if self.file is not None:
            parts.append(f"{self.file}:")
        if self.field is not None:
            parts.append(f"{self.field}:")
        parts.append(self.message)
        return " ".join(parts)


class ConfigError(GatewayError):
    """A configuration file is missing, malformed or inconsistent."""

    exit_code = 2


class SchemaError(GatewayError):
    """An OpenAPI document cannot be loaded, validated or exposed."""

    exit_code = 3


class CredentialsError(GatewayError):
    """A credential's environment variable is missing or unusable."""

    exit_code = 4


class McpInitError(GatewayError):
    """FastMCP could not build or start the server."""

    exit_code = 5


class MultipleErrors(GatewayError):  # noqa: N818 - public name
    """Aggregate of several errors; exits with the lowest member code."""

    def __init__(self, errors: list[GatewayError]) -> None:
        super().__init__("; ".join(str(e) for e in errors))
        self.errors = errors

    @property
    def exit_code(self) -> int:  # type: ignore[override]
        """Lowest exit code among the collected errors."""
        return min((e.exit_code for e in self.errors), default=GatewayError.exit_code)

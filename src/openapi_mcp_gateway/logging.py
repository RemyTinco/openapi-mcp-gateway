"""Logging to stderr, in plain text or JSON, with secret redaction."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Literal

REDACTED = "***"
_SENSITIVE_WORDS = ("authorization", "token", "api_key", "password", "secret", "body")
_HANDLER_MARK = "_openapi_mcp_gateway_handler"


def _is_sensitive(name: str) -> bool:
    normalised = name.lower().replace("-", "_")
    return any(word in normalised for word in _SENSITIVE_WORDS)


def _text_value(value: object) -> str:
    text = str(value)
    if text == "" or any(c.isspace() for c in text) or '"' in text:
        return json.dumps(text, ensure_ascii=False)
    return text


class _StderrHandler(logging.StreamHandler[Any]):
    """Writes to whatever ``sys.stderr`` is at emit time (never stdout)."""

    def __init__(self) -> None:
        super().__init__(sys.stderr)

    @property
    def stream(self) -> Any:
        return sys.stderr

    @stream.setter
    def stream(self, value: Any) -> None:
        pass


class _Formatter(logging.Formatter):
    """Base for both formats; a record's traceback is shown only when asked for."""

    def __init__(self, *, tracebacks: bool) -> None:
        super().__init__()
        self.tracebacks = tracebacks

    def traceback(self, record: logging.LogRecord) -> str | None:
        if not (self.tracebacks and record.exc_info):
            return None
        return self.formatException(record.exc_info)


class _TextFormatter(_Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "event", None)
        if event is None:
            line = f"{record.levelname} {record.getMessage()}"
        else:
            fields: dict[str, object] = getattr(record, "fields", {})
            parts = [record.levelname, event]
            parts += [f"{k}={_text_value(v)}" for k, v in fields.items()]
            line = " ".join(parts)
        traceback = self.traceback(record)
        return line if traceback is None else f"{line}\n{traceback}"


class _JsonFormatter(_Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "event", None)
        fields: dict[str, object] = getattr(record, "fields", {})
        payload: dict[str, object] = dict(fields)
        payload["level"] = record.levelname
        payload["event"] = event if event is not None else record.getMessage()
        traceback = self.traceback(record)
        if traceback is not None:
            payload["traceback"] = traceback  # still one JSON object on one line
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str, fmt: Literal["text", "json"]) -> None:
    """Send all logging to stderr; calling again replaces the previous configuration.

    Tracebacks attached to log records are printed only at ``DEBUG``.
    """
    root = logging.getLogger()
    for existing in [h for h in root.handlers if getattr(h, _HANDLER_MARK, False)]:
        root.removeHandler(existing)
    handler = _StderrHandler()
    setattr(handler, _HANDLER_MARK, True)
    tracebacks = level.upper() == "DEBUG"
    formatter = _JsonFormatter if fmt == "json" else _TextFormatter
    handler.setFormatter(formatter(tracebacks=tracebacks))
    root.addHandler(handler)
    root.setLevel(level.upper())


def log_event(logger: logging.Logger, level: int, event: str, **fields: object) -> None:
    """Log ``event`` with key/value fields; sensitive field values are replaced by ``***``."""
    safe = {k: REDACTED if _is_sensitive(k) else v for k, v in fields.items()}
    logger.log(level, event, extra={"event": event, "fields": safe})

"""Structured JSON logging for the M&A Due Diligence sample.

Emits one JSON object per log record so logs are easy to query in CloudWatch
Logs Insights. Extra fields passed via ``logger.info("msg", extra={...})``
are merged into the emitted object.

Usage::

    from mna.logging_config import get_logger
    logger = get_logger(__name__)
    logger.info("agent_invoked", extra={"agent": "supervisor", "session_id": "abc"})
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

# Attributes present on every ``LogRecord`` that we don't want to echo into
# the ``extra`` section of the JSON payload.
_RESERVED_LOG_RECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

_DEFAULT_LEVEL = os.getenv("MNA_LOG_LEVEL", "INFO").upper()


class JsonFormatter(logging.Formatter):
    """Formatter that emits one JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "module": record.module,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Merge any ``extra={...}`` fields without clobbering reserved keys.
        for key, value in record.__dict__.items():
            if key in _RESERVED_LOG_RECORD_ATTRS or key in payload:
                continue
            try:
                json.dumps(value)  # fail fast for non-serializable values
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = record.stack_info

        return json.dumps(payload, default=str)


def configure_logging(level: str | int | None = None) -> logging.Logger:
    """Install the JSON formatter on the ``mna`` package logger.

    Idempotent: calling it multiple times won't stack handlers. Respects the
    ``MNA_LOG_LEVEL`` environment variable when no explicit level is passed.
    """

    package_logger = logging.getLogger("mna")
    resolved_level = level if level is not None else _DEFAULT_LEVEL
    package_logger.setLevel(resolved_level)

    # Avoid double-handling when the root logger is also configured (e.g.,
    # inside the AgentCore Runtime container).
    package_logger.propagate = False

    already_configured = any(
        isinstance(h, logging.StreamHandler) and isinstance(h.formatter, JsonFormatter)
        for h in package_logger.handlers
    )
    if not already_configured:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
        package_logger.addHandler(handler)

    return package_logger


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the ``mna`` namespace with JSON formatting."""

    configure_logging()
    if name == "mna" or name.startswith("mna."):
        return logging.getLogger(name)
    return logging.getLogger(f"mna.{name}")

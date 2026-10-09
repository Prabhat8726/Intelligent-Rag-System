"""Structured logging (structlog) shared by the API, workers, CLI and migrations.

* Console output for local development, JSON lines everywhere else.
* Standard-library loggers (uvicorn, sqlalchemy, alembic) are routed through the same
  processors so every line has the same shape.
* A redaction processor masks secrets by key name before anything is rendered. Document
  content must never be logged; log identifiers instead.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any, TextIO

import structlog
from structlog.types import EventDict, Processor, WrappedLogger

from docintel.core.config import LogFormat

REDACTED = "***"

# Matches keys such as password, new_password, api_key, gemini_api_key, authorization,
# access_token, refresh_token, cookie, client_secret - but not counters like input_tokens.
_SENSITIVE_KEY = re.compile(
    r"(^|[_-])(password|passwd|secret|api[_-]?key|authorization|cookie|credentials?)($|[_-])"
    r"|(^|[_-])token$",
    re.IGNORECASE,
)
_BEARER_VALUE = re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)


def _is_sensitive_key(key: object) -> bool:
    return isinstance(key, str) and bool(_SENSITIVE_KEY.search(key))


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: REDACTED if _is_sensitive_key(k) else _redact(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(_redact(item) for item in value)
    if isinstance(value, str):
        return _BEARER_VALUE.sub(f"Bearer {REDACTED}", value)
    return value


def redact_sensitive(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """structlog processor that masks secret-looking keys and bearer tokens."""
    redacted: MutableMapping[str, Any] = {}
    for key, value in event_dict.items():
        redacted[key] = REDACTED if _is_sensitive_key(key) else _redact(value)
    return dict(redacted)


def configure_logging(*, level: str, log_format: LogFormat, stream: TextIO | None = None) -> None:
    """Configure structlog and the standard library logging tree. Safe to call repeatedly.

    `stream` defaults to stdout; the stdio MCP server logs to stderr (stdout is its protocol)."""
    output = stream or sys.stdout
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_sensitive,
        structlog.processors.StackInfoRenderer(),
    ]

    renderer: Processor
    final_processors: list[Processor] = [structlog.stdlib.ProcessorFormatter.remove_processors_meta]
    if log_format == LogFormat.JSON:
        final_processors.append(structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=output.isatty())
    final_processors.append(renderer)

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=final_processors,
    )
    handler = logging.StreamHandler(output)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # Route uvicorn through the root handler; request logging is done by our middleware.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    # Third-party HTTP clients log full URLs at INFO/DEBUG; keep them quiet.
    for noisy in ("httpx", "httpcore", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger

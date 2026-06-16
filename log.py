"""Structured logging for the BookLore MCP server, built on structlog.

Configurable via environment (or the `configure_logging` arguments, which win):

  LOG_LEVEL   DEBUG | INFO | WARNING | ERROR | CRITICAL   (default: INFO)
  LOG_FORMAT  console | json                              (default: console)

`console` is a human-friendly, optionally coloured renderer for local use; `json`
emits one JSON object per line for log aggregators. Both structlog loggers and
stdlib logging (httpx, uvicorn, fastmcp, …) are routed through the same renderer.

Everything is written to **stderr** on purpose: when the server runs over the stdio
transport, stdout carries the MCP JSON-RPC stream and must not be polluted by logs.
"""

from __future__ import annotations

import logging
import os
import sys

import structlog

__all__ = ["configure_logging", "get_logger"]

_VALID_FORMATS = {"console", "json"}


def _resolve_level(level: str | None) -> int:
    name = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    resolved = logging.getLevelName(name)
    # getLevelName returns the int for a known name, or the string "Level X" for an
    # unknown one — fall back to INFO in that case.
    return resolved if isinstance(resolved, int) else logging.INFO


def _resolve_format(fmt: str | None) -> str:
    value = (fmt or os.environ.get("LOG_FORMAT", "console")).lower()
    return value if value in _VALID_FORMATS else "console"


def configure_logging(level: str | None = None, fmt: str | None = None) -> None:
    """Configure structlog + stdlib logging. Safe to call more than once.

    `level` / `fmt` override the LOG_LEVEL / LOG_FORMAT environment variables.
    """
    log_level = _resolve_level(level)
    output = _resolve_format(fmt)

    # Processors shared by structlog-native and foreign (stdlib) log records.
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.format_exc_info,
            # Hand off to the stdlib ProcessorFormatter so structlog and stdlib
            # records share one renderer and one handler.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        cache_logger_on_first_use=True,
    )

    if output == "json":
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger (lazily bound, so order vs. configure
    doesn't matter)."""
    return structlog.get_logger(name)

"""Structured logging for SweepAI."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any


class JSONFormatter(logging.Formatter):
    """JSON log formatter for production use."""

    def format(self, record: logging.LogRecord) -> str:
        log_data: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if record.exc_info and record.exc_info[0] is not None:
            log_data["exception"] = self.formatException(record.exc_info)

        if hasattr(record, "extra_data"):
            log_data["extra"] = record.extra_data

        return json.dumps(log_data)


class HumanFormatter(logging.Formatter):
    """Human-readable log formatter for development."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )


def setup_logging(
    level: str = "INFO",
    json_format: bool = False,
    output: str | None = None,
) -> None:
    """
    Configure logging for SweepAI.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        json_format: Use JSON formatting (production)
        output: Output file path (None for stderr)
    """
    root_logger = logging.getLogger("sweepai")
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers
    root_logger.handlers.clear()

    # Create handler
    handler: logging.Handler
    if output:
        handler = logging.FileHandler(output)
    else:
        handler = logging.StreamHandler(sys.stderr)

    # Set formatter
    if json_format:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(HumanFormatter())

    root_logger.addHandler(handler)

    # Set third-party loggers to WARNING
    for name in ("httpx", "httpcore", "aiosqlite", "langgraph"):
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get a logger for a specific module."""
    return logging.getLogger(f"sweepai.{name}")

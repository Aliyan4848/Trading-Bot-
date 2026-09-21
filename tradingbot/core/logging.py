"""Structured JSON logging (one JSON object per line).

Kept dependency-free on purpose: a ``JsonLogger`` wrapper over stdlib
``logging``. All money-relevant events are also persisted to the
``system_logs`` table by the event subscribers; the console JSON is for
operational logs.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "extra_json", None)
        if isinstance(extra, dict):
            entry.update(extra)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        try:
            return json.dumps(entry, default=str, ensure_ascii=False)
        except Exception:  # pragma: no cover - never let logging kill the bot
            return f'{{"msg": "{entry["msg"]}"}}'


class JsonLogger:
    """Thin wrapper: ``log.info("msg", key=value)`` becomes structured JSON."""

    def __init__(self, name: str) -> None:
        self._l = logging.getLogger(name)

    def log(self, level: int, msg: str, **fields: Any) -> None:
        self._l.log(level, msg, extra={"extra_json": fields} if fields else {})

    def debug(self, msg: str, **fields: Any) -> None:
        self.log(10, msg, **fields)

    def info(self, msg: str, **fields: Any) -> None:
        self.log(20, msg, **fields)

    def warning(self, msg: str, **fields: Any) -> None:
        self.log(30, msg, **fields)

    def error(self, msg: str, **fields: Any) -> None:
        self.log(40, msg, **fields)

    def exception(self, msg: str, **fields: Any) -> None:
        self._l.exception(msg, extra={"extra_json": fields} if fields else {})


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # third-party noise down
    for noisy in ("uvicorn.access", "watchfiles"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> JsonLogger:
    return JsonLogger(name)


def log_event(logger: Any, level: int, msg: str, **fields: Any) -> None:
    """Log with structured fields (no f-string interpolation of secrets)."""
    if isinstance(logger, JsonLogger):
        logger.log(level, msg, **fields)
    else:  # raw stdlib logger
        logger.log(level, msg, extra={"extra_json": fields} if fields else {})

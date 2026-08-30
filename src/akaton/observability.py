from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization|api[-_]?key|token|password)([\"'=:\s]+)([^\s\"']+)"),
    re.compile(r"(?i)(https?://|socks5://)([^/@\s:]+):([^/@\s]+)@"),
)


def redact(value: str) -> str:
    result = value
    result = SECRET_PATTERNS[0].sub(r"\1\2***", result)
    result = SECRET_PATTERNS[1].sub(r"\1***:***@", result)
    return result


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key in (
            "run_id",
            "trace_id",
            "candidate_id",
            "event_id",
            "stage",
            "outcome",
            "error_type",
        ):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

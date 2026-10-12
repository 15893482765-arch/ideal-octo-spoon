"""结构化日志：request_id 绑定 + 敏感字段脱敏。

红线：日志中不得出现密码、完整 token、密钥明文（PRD §9.4 安全）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.core.context import get_request_id

SENSITIVE_KEYS = frozenset(
    {
        "password",
        "password_hash",
        "secret",
        "secret_key",
        "access_key",
        "token",
        "authorization",
        "credential",
        "phone",
        "contact_phone",
        "location",
    }
)


def redact(value: Any) -> Any:
    """递归脱敏：命中敏感键的值一律替换为 ***REDACTED***。"""
    if isinstance(value, dict):
        return {
            k: ("***REDACTED***" if str(k).lower() in SENSITIVE_KEYS else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(redact(v) for v in value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": get_request_id(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(redact(extra))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

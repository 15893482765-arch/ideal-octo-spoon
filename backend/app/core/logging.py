"""结构化日志：request_id 绑定 + 敏感字段脱敏（T04 升级为消息级正则脱敏）。

红线（PRD §9.4 / T04）：日志中不得出现密码、完整 token、密钥明文；
手机号在日志中一律掩码（Q020 敏感词扫描）。
"""

from __future__ import annotations

import json
import logging
import re
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

REDACTED = "***REDACTED***"

# 消息级正则：key=value / key: value 形式的凭据
_KV_CREDENTIAL_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|secret_key|token|authorization|credential|"
    r"access_key|api_key)\b(\s*[=:]\s*)\S+"
)
# JWT（三段 base64url）
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{10,}")
# Argon2 哈希
_ARGON2_RE = re.compile(r"\$argon2\S+")
# 中国大陆手机号（前后无数字）
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")


def mask_phone_text(text: str) -> str:
    """文本中的手机号 → 138****1234。"""
    return _PHONE_RE.sub(r"\1****\2", text)


def sanitize_message(message: str) -> str:
    """对日志消息做正则脱敏：凭据键值、JWT、Argon2 哈希、手机号。"""
    text = _KV_CREDENTIAL_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", message)
    text = _JWT_RE.sub(REDACTED, text)
    text = _ARGON2_RE.sub(REDACTED, text)
    text = mask_phone_text(text)
    return text


def redact(value: Any) -> Any:
    """递归脱敏：命中敏感键的值一律替换为 ***REDACTED***。"""
    if isinstance(value, dict):
        return {
            k: (REDACTED if str(k).lower() in SENSITIVE_KEYS else redact(v))
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
            "message": sanitize_message(record.getMessage()),
            "request_id": get_request_id(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(redact(extra))
        if record.exc_info:
            # 异常文本同样过脱敏（堆栈可能带参数）
            payload["exc"] = sanitize_message(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

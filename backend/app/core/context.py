"""请求上下文：request_id 贯穿中间件、日志与错误响应。"""

from __future__ import annotations

from contextvars import ContextVar
from uuid import uuid4

_request_id: ContextVar[str] = ContextVar("request_id", default="-")


def set_request_id(value: str) -> None:
    _request_id.set(value)


def new_request_id() -> str:
    return uuid4().hex


def get_request_id() -> str:
    return _request_id.get()

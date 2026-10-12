"""进程内限流：登录失败计数 + 账号锁定。

T03 阶段先把"登录失败限流"与"账号被踢"在内存内实现为可用状态；
T04 引入 Redis 后，把计数器与撤销名单迁到 Redis（接口保持不变）。

注意：进程内状态不能跨实例共享，**生产部署必须做 Redis 化**（T04 跟进）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from uuid import UUID


@dataclass
class _LoginCounter:
    count: int = 0
    locked_until_ts: float = 0.0


class LoginRateLimiter:
    """以 username 为键的登录失败计数与锁定。

    同时维护（key=username, scope='login'）和（key=user_id, scope='user'）两份内存表：
    - 前者防"同一用户名从多 IP 撞库"；
    - 后者用于登录成功后把绑定到 user_id 的状态清零。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_username: dict[str, _LoginCounter] = {}
        self._by_user_id: dict[UUID, _LoginCounter] = {}

    def record_failure(
        self, *, username: str, user_id: UUID | None, max_failures: int, lockout_seconds: int
    ) -> bool:
        """记录一次失败；达到上限则锁定并返回 True（表示已锁定）。"""
        now = time.monotonic()
        with self._lock:
            c = self._by_username.setdefault(username.lower(), _LoginCounter())
            c.count += 1
            if c.count >= max_failures:
                c.locked_until_ts = now + lockout_seconds
                if user_id is not None:
                    self._by_user_id.setdefault(user_id, _LoginCounter()).locked_until_ts = (
                        c.locked_until_ts
                    )
                return True
            if user_id is not None:
                self._by_user_id.setdefault(user_id, _LoginCounter())
            return False

    def is_locked(self, *, username: str) -> bool:
        now = time.monotonic()
        with self._lock:
            c = self._by_username.get(username.lower())
            if c is None:
                return False
            if c.locked_until_ts == 0.0:
                return False
            return c.locked_until_ts > now

    def remaining_lock_seconds(self, *, username: str) -> int:
        now = time.monotonic()
        with self._lock:
            c = self._by_username.get(username.lower())
            if c is None or c.locked_until_ts == 0.0:
                return 0
            return max(0, int(c.locked_until_ts - now))

    def reset(self, *, username: str, user_id: UUID | None) -> None:
        with self._lock:
            self._by_username.pop(username.lower(), None)
            if user_id is not None:
                self._by_user_id.pop(user_id, None)


# 模块级单例；测试中可用 monkeypatch.setattr 替换
login_rate_limiter = LoginRateLimiter()

"""IdempotencyService（T04 / PRD §5.4 第 7 条、R27）。

覆盖移动端提交、审批、AI 写工具、外部回调四类入口。

语义：
- 同 (scope, key) + 同参数（params_hash）→ 重放首次结果，业务函数不重复执行；
- 同 (scope, key) + 异参数 → IDEMPOTENCY_PARAMS_CONFLICT（HTTP 409）；
- 同 (scope, key) + status=PENDING（并发占位中）→ IDEMPOTENCY_IN_PROGRESS（409）；
- status=FAILED → 删除旧记录后允许重试；
- 与业务写操作同 session 同事务：业务回滚时 PENDING/COMPLETED 记录一并消失。

Redis 化说明：T04 先以数据库唯一约束实现（(scope, key) 唯一即并发安全），
跨进程重放窗口 = idempotency_retention_hours；Redis 分布式锁优化留待后续按需引入，
不改变本接口。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.context import get_request_id
from app.core.errors import ConflictError
from app.models import IdempotencyKey

STATUS_PENDING = "PENDING"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"


def hash_params(params: Any) -> str:
    """参数摘要：规范化 JSON 后取 SHA-256。dict 键排序保证同参同哈希。"""
    canonical = json.dumps(params, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _expired(row: IdempotencyKey) -> bool:
    return row.expires_at <= _now()


class IdempotencyService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -------- 查询 --------

    def _lookup(self, scope: str, key: str) -> IdempotencyKey | None:
        stmt = select(IdempotencyKey).where(
            IdempotencyKey.scope == scope, IdempotencyKey.idem_key == key
        )
        return self.session.execute(stmt).scalar_one_or_none()

    # -------- 执行入口 --------

    def run(
        self,
        *,
        scope: str,
        key: str,
        params: Any,
        actor_id: UUID | None,
        fn: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        """幂等执行：返回 {"replayed": bool, "response": dict}。

        fn 内的一切写操作必须使用 self.session（与幂等记录同事务）。
        fn 抛异常 → 幂等记录置 FAILED（同事务，随调用方回滚/提交）并原样上抛。
        """
        params_hash = hash_params(params)

        row = self._lookup(scope, key)
        if row is not None and _expired(row):
            # 过期：窗口外同键视为新请求
            self.session.delete(row)
            self.session.flush()
            row = None

        if row is not None:
            if row.params_hash != params_hash:
                raise ConflictError(
                    code="IDEMPOTENCY_PARAMS_CONFLICT",
                    message="幂等键已存在但请求参数不一致",
                    details={"scope": scope, "key": key},
                )
            if row.status == STATUS_PENDING:
                raise ConflictError(
                    code="IDEMPOTENCY_IN_PROGRESS",
                    message="相同请求正在处理中，请稍后重试",
                    details={"scope": scope, "key": key},
                )
            if row.status == STATUS_COMPLETED:
                return {"replayed": True, "response": dict(row.response or {})}
            if row.status == STATUS_FAILED:
                # 失败允许同键重试：删除旧记录重新执行
                self.session.delete(row)
                self.session.flush()

        # 占位：唯一约束保证并发安全；SAVEPOINT 防止冲突回滚波及调用方事务
        retention = get_settings().idempotency_retention_hours
        pending = IdempotencyKey(
            scope=scope,
            idem_key=key,
            params_hash=params_hash,
            actor_id=actor_id,
            status=STATUS_PENDING,
            request_id=get_request_id(),
            expires_at=_now() + timedelta(hours=retention),
        )
        try:
            with self.session.begin_nested():
                self.session.add(pending)
        except IntegrityError as exc:
            # 并发同键：唯一约束拒绝 → 冲突（调用方事务自然回滚，不在此处强行 rollback）
            raise ConflictError(
                code="IDEMPOTENCY_IN_PROGRESS",
                message="相同请求正在处理中，请稍后重试",
                details={"scope": scope, "key": key},
            ) from exc

        try:
            response = fn()
        except Exception as exc:
            pending.status = STATUS_FAILED
            pending.error_message = str(exc)[:1000]
            raise

        pending.status = STATUS_COMPLETED
        pending.response = response
        self.session.flush()
        return {"replayed": False, "response": response}

    # -------- 低阶 API（供 T05 状态机等需要手控生命周期的调用方）--------

    def begin(
        self, *, scope: str, key: str, params: Any, actor_id: UUID | None
    ) -> tuple[str, dict[str, Any] | None]:
        """占位或重放判定。返回 ("NEW", None) 或 ("REPLAY", stored_response)。

        抛 ConflictError：异参冲突 / 并发占位中。
        """
        params_hash = hash_params(params)
        row = self._lookup(scope, key)
        if row is not None and _expired(row):
            self.session.delete(row)
            self.session.flush()
            row = None
        if row is not None:
            if row.params_hash != params_hash:
                raise ConflictError(
                    code="IDEMPOTENCY_PARAMS_CONFLICT",
                    message="幂等键已存在但请求参数不一致",
                    details={"scope": scope, "key": key},
                )
            if row.status == STATUS_PENDING:
                raise ConflictError(
                    code="IDEMPOTENCY_IN_PROGRESS",
                    message="相同请求正在处理中，请稍后重试",
                    details={"scope": scope, "key": key},
                )
            if row.status == STATUS_FAILED:
                self.session.delete(row)
                self.session.flush()
                row = None
            else:  # COMPLETED
                return ("REPLAY", dict(row.response or {}))

        retention = get_settings().idempotency_retention_hours
        try:
            with self.session.begin_nested():
                self.session.add(
                    IdempotencyKey(
                        scope=scope,
                        idem_key=key,
                        params_hash=params_hash,
                        actor_id=actor_id,
                        status=STATUS_PENDING,
                        request_id=get_request_id(),
                        expires_at=_now() + timedelta(hours=retention),
                    )
                )
        except IntegrityError as exc:
            raise ConflictError(
                code="IDEMPOTENCY_IN_PROGRESS",
                message="相同请求正在处理中，请稍后重试",
                details={"scope": scope, "key": key},
            ) from exc
        return ("NEW", None)

    def complete(self, *, scope: str, key: str, response: dict[str, Any]) -> None:
        row = self._lookup(scope, key)
        if row is None:
            raise ValueError("幂等记录不存在，无法 complete")
        row.status = STATUS_COMPLETED
        row.response = response
        self.session.flush()

    def fail(self, *, scope: str, key: str, error: str) -> None:
        row = self._lookup(scope, key)
        if row is not None:
            row.status = STATUS_FAILED
            row.error_message = error[:1000]
            self.session.flush()

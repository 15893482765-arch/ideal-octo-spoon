"""幂等键（T04 / PRD §5.4、R27）。

T02 首建本表（key+actor+response_ref），T04 演进为通用幂等内核：
覆盖移动端提交、审批、AI 写工具、外部回调四类入口。

- (scope, key) 全库唯一：同键同参数 → 重放首次结果；异参 → 409 冲突；
- status PENDING 表示并发进行中：直接拒绝，不排队等待；
- FAILED 允许同键重试：下一次调用删除旧记录重新执行；
- 与业务写操作同 session 同事务：业务回滚时本表记录一并消失（不留成功幂等）。

数据库列名沿用 T02 的 `key`（模型属性名 idem_key），避免列重命名迁移。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CHAR, CheckConstraint, DateTime, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import BigInteger

from app.db.base import Base, pk_uuid


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        # T04：幂等域+键联合唯一（替代 T02 的 key+actor 唯一）
        UniqueConstraint("scope", "key", name="uq_idempotency_scope_key"),
        CheckConstraint(
            "status IN ('PENDING','COMPLETED','FAILED')",
            name="idempotency_keys_status_check",
        ),
        Index("ix_idempotency_keys_expires", "expires_at"),
    )

    id: Mapped[UUID] = pk_uuid()
    # 幂等域：如 visit.submit / approval.decide / ai.tool.write / callback.inbound
    scope: Mapped[str] = mapped_column(
        String(64), nullable=False, default="general", server_default=text("'general'")
    )
    # 客户端/调用方提供的幂等键（沿用 T02 列名 key，长度放宽到 128）
    idem_key: Mapped[str] = mapped_column("key", String(128), nullable=False)
    # 请求参数摘要（SHA-256）：同键异参视为冲突
    params_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, server_default=text("''")
    )
    actor_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="PENDING", server_default=text("'PENDING'")
    )
    # 首次执行结果快照：重放时原样返回（T02 response_ref 已由本列替代）
    response: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # 过期后由后台清理；重放窗口与保留期一致
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # 并发占位乐观锁（预留，T05 状态机使用）
    lock_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )

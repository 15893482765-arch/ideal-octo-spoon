"""通知已读标记（T27 / PRD §4.2 通知中心）。

通知本身是**派生视图**：由任务（TaskItem）、待审批（ApprovalRequest）、
风险预警（RiskItem）实时聚合，不另建物化通知表、不复制业务数据。

已读状态必须**独立**于来源对象（PRD：标记已读不得改变任务/审批/风险状态），
因此单独建一张「用户 × 通知键」标记表：
- 只记录 (user_id, notification_key) 与 read_at；
- 通知键由来源类型与来源主键确定性拼出（见 NotificationService._key）；
- 行存在即“已读”；不写来源对象、不触发任何状态机。

已读幂等：(user_id, notification_key) 唯一，重复标记不报错。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, pk_uuid


class NotificationRead(Base):
    __tablename__ = "notification_reads"
    __table_args__ = (
        UniqueConstraint("user_id", "notification_key",
                         name="uq_notification_read_user_key"),
        CheckConstraint("char_length(notification_key) BETWEEN 1 AND 96",
                        name="notification_read_key_len_check"),
        Index("ix_notification_read_user", "user_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, comment="接收用户（服务端会话注入）"
    )
    # 形如 TASK:<uuid> / APPROVAL:<uuid> / RISK:<uuid>
    notification_key: Mapped[str] = mapped_column(String(96), nullable=False)
    read_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<NotificationRead {self.notification_key} user={self.user_id}>"

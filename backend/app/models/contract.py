from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Date,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AuditMixin, Base, DatasetMixin, pk_uuid
from app.models.enums import ContractStatus


class Contract(AuditMixin, DatasetMixin, Base):
    __tablename__ = "contracts"
    __table_args__ = (UniqueConstraint("contract_no", name="uq_contracts_contract_no"),)

    id: Mapped[UUID] = pk_uuid()
    merchant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("merchants.id"), nullable=False, index=True
    )
    contract_no: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    party_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24),
        default=ContractStatus.DRAFT.value,
        nullable=False,
        index=True,
    )
    template_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    signed_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    expire_at: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    commission_rate: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    # 陈列补贴金额：BIGINT 存“分”（红线：金额一律 BIGINT 分，业务层用 Decimal 换算）
    display_subsidy_amount: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sales_rep_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    manager_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    attachment_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # ---- T09：合同固化口径（PRD §4.4 陈列阈值/提成规则版本/当前版本号）----
    # 陈列达标最低占比百分比（0-100），默认 50；建合同即固化，Q045 验证生效后不可变
    display_threshold_pct: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("50")
    )
    # 提成规则版本（规则中心版本号）；合同生效时固化，变更不覆盖历史
    commission_rule_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 当前最新版本号，从 1 起，每次批准/变更 +1
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    # 续签评估待办已生成所对应的到期日（幂等标记；同一到期周期不重复生成，R38）
    renewal_generated_for_expire_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 续约来源：本份合同由哪份合同续约而来（同一商家，新合同生效日必须晚于旧合同到期日）
    renewed_from_contract_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("contracts.id"),
        nullable=True,
        index=True,
    )
    # 店长姓名：纸质合同乙方签署人；创建草稿时可由商家主联系人自动预填
    store_manager_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 纸质合同正文（模板渲染结果，草稿期可手工修订；审批生效后随版本快照固化）
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)


class ContractVersion(AuditMixin, DatasetMixin, Base):
    """合同版本快照（T09 / PRD §4.4）。

    首次批准生效生成 v1；变更生效生成新版本。旧版本完整保留（Q046），
    合同主表字段始终指向最新版本；生效后历史版本不可覆盖。
    """

    __tablename__ = "contract_versions"
    __table_args__ = (
        UniqueConstraint("contract_id", "version", name="uq_contract_versions_no"),
    )

    id: Mapped[UUID] = pk_uuid()
    contract_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("contracts.id"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # INIT 首次生效 / CHANGE 变更 / TERMINATE 终止留档
    change_type: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    party_name: Mapped[str] = mapped_column(String(200), nullable=False)
    template_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    signed_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    expire_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    commission_rate: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    # 同主表：金额 BIGINT 存分
    display_subsidy_amount: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    display_threshold_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    commission_rule_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 与主表一致：纸质合同签署人与正文随版本固化
    store_manager_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachment_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

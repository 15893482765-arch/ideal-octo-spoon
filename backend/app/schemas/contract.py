"""合同中心 API 请求/响应模型（PRD §4.4 / R38）。ORM 不直接作为 API 返回。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

# 合同变更类型
ChangeKind = Literal["INIT", "CHANGE", "TERMINATE"]


# ----------------------------- 请求 -----------------------------
class ContractDraftRequest(BaseModel):
    """创建/更新草稿：固化陈列阈值、提成规则版本、生效/到期日。"""

    merchant_id: UUID
    contract_no: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=200)
    party_name: str = Field(min_length=1, max_length=200)
    template_version: str | None = Field(default=None, max_length=16)
    effective_from: date | None = None
    expire_at: date | None = None
    # 提成（百分比口径，如 3.5000 表示 3.5%）；金额类补贴仍按元由服务端转分
    commission_rate: float | None = Field(default=None, ge=0, le=100)
    display_subsidy_amount: int | None = Field(default=None, ge=0, description="陈列补贴，单位分")
    # 陈列达标最低占比百分比（0-100），Q045 50% 阈值即固化于此
    display_threshold_pct: int | None = Field(default=None, ge=0, le=100)
    commission_rule_version: str | None = Field(default=None, max_length=32)
    # 店长姓名：纸质合同乙方签署人；留空则创建时取商家主联系人
    store_manager_name: str | None = Field(default=None, max_length=64)
    # 纸质合同正文：显式传入则按提交内容保存；留空由服务端按模板渲染
    body_text: str | None = Field(default=None, max_length=20000)
    attachment_id: UUID | None = None
    attachment_filename: str | None = Field(default=None, max_length=255)
    attachment_content_type: str | None = Field(default=None, max_length=128)
    attachment_size_bytes: int | None = Field(default=None, ge=0)
    reason: str = Field(default="创建合同草稿", min_length=1, max_length=255)

    @field_validator("contract_no", "title", "party_name")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class ContractSubmitRequest(BaseModel):
    reason: str = Field(default="提交合同审批", min_length=1, max_length=255)


class ContractDecisionRequest(BaseModel):
    """审批：approve / return / reject。业务员不能自审（Q044 在服务端校验）。"""

    decision: Literal["APPROVE", "RETURN", "REJECT"]
    comment: str = Field(default="", max_length=500)


class ContractChangeRequest(BaseModel):
    """生效后变更：生成新版本，不覆盖旧版（Q046）。"""

    reason: str = Field(min_length=1, max_length=255)
    title: str | None = Field(default=None, max_length=200)
    party_name: str | None = Field(default=None, max_length=200)
    template_version: str | None = Field(default=None, max_length=16)
    effective_from: date | None = None
    expire_at: date | None = None
    commission_rate: float | None = Field(default=None, ge=0, le=100)
    display_subsidy_amount: int | None = Field(default=None, ge=0)
    display_threshold_pct: int | None = Field(default=None, ge=0, le=100)
    commission_rule_version: str | None = Field(default=None, max_length=32)
    attachment_id: UUID | None = None
    attachment_filename: str | None = Field(default=None, max_length=255)
    attachment_content_type: str | None = Field(default=None, max_length=128)
    attachment_size_bytes: int | None = Field(default=None, ge=0)


class ContractTerminateRequest(BaseModel):
    """终止申请：终止不是删除（Q047）。"""

    reason: str = Field(min_length=1, max_length=255)
    terminate_date: date | None = None


class ContractRenewalRequest(BaseModel):
    """续约申请：基于生效合同生成一份接续的新合同（草稿），审批通过后按新生效日生效。

    - 不修改被续约合同：旧合同到期自然失效，无需"先终止再重签"；
    - 新生效日必须晚于旧合同到期日，避免两份合同的有效期重叠；
    - 编号缺省时由服务端生成 `<原编号>-R<n>`（全局唯一）。
    """

    contract_no: str | None = Field(default=None, max_length=32)
    effective_from: date
    expire_at: date
    title: str | None = Field(default=None, max_length=200)
    party_name: str | None = Field(default=None, max_length=200)
    commission_rate: float | None = Field(default=None, ge=0, le=100)
    display_subsidy_amount: int | None = Field(default=None, ge=0, description="陈列补贴，单位分")
    display_threshold_pct: int | None = Field(default=None, ge=0, le=100)
    commission_rule_version: str | None = Field(default=None, max_length=32)
    reason: str = Field(default="合同续约", min_length=1, max_length=255)

    @field_validator("contract_no", "title", "party_name")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return v.strip() if isinstance(v, str) else v


# ----------------------------- 响应 -----------------------------
class ContractView(BaseModel):
    id: UUID
    merchant_id: UUID
    contract_no: str
    title: str
    party_name: str
    status: str
    version: int
    template_version: str | None = None
    signed_at: date | None = None
    effective_from: date | None = None
    expire_at: date | None = None
    commission_rate: float | None = None
    display_subsidy_amount: int | None = None
    display_threshold_pct: int | None = None
    commission_rule_version: str | None = None
    store_manager_name: str | None = None
    # 纸质合同正文（模板渲染或手工修订后的最终文本）
    body_text: str | None = None
    sales_rep_user_id: UUID | None = None
    manager_user_id: UUID | None = None
    attachment_id: UUID | None = None
    has_attachment: bool = False
    renewal_generated_for_expire_at: date | None = None
    # 续约来源（本份合同由哪份合同续约而来）
    renewed_from_contract_id: UUID | None = None
    created_at: datetime | None = None


class ContractVersionView(BaseModel):
    id: UUID
    contract_id: UUID
    version: int
    change_type: str
    title: str
    party_name: str
    effective_from: date | None = None
    expire_at: date | None = None
    commission_rate: float | None = None
    display_subsidy_amount: int | None = None
    display_threshold_pct: int | None = None
    commission_rule_version: str | None = None
    attachment_id: UUID | None = None
    created_at: datetime | None = None


class ContractRenewalView(BaseModel):
    """续约关系（旧合同详情里展示由它续约出的新合同）。"""

    id: UUID
    contract_no: str
    status: str
    effective_from: date | None = None
    expire_at: date | None = None


class RenewalRunView(BaseModel):
    """R38 续签扫描结果。"""

    scanned: int
    generated: int
    skipped: int
    contract_ids: list[UUID] = Field(default_factory=list)

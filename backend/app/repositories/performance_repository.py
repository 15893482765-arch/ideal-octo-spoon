"""T19 绩效规则版本 / 月度快照持久化（Repository 只做查询与持久化）。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Activity,
    ActivityParticipant,
    ApprovalRequest,
    Contract,
    MerchantAssignment,
    PerformanceMonthlySnapshot,
    PerformanceRuleVersion,
    PerformanceSnapshotAdjustment,
    PerformanceSnapshotLine,
    Role,
    SalesConfirmation,
    User,
    VisitRecord,
)
from app.models.enums import (
    ActivityStatus,
    ContractStatus,
)
from app.repositories.transfer_repository import _tstz_point

# 责任扣减审批的动作标识（R40）：区别于商家主动取消（不产生扣减）
DEDUCTION_ACTION = "performance.deduction"
ADJUSTMENT_ACTION = "performance.adjustment"


class PerformanceRepository:
    def __init__(self, session: Session):
        self.session = session

    # ---------- 规则版本 ----------

    def add_rule_version(self, version: PerformanceRuleVersion) -> None:
        self.session.add(version)
        self.session.flush()

    def get_rule_version(self, version_id: UUID) -> PerformanceRuleVersion | None:
        return self.session.get(PerformanceRuleVersion, version_id)

    def get_version(self, code: str, version: int,
                    dataset_id: UUID) -> PerformanceRuleVersion | None:
        return self.session.scalar(
            select(PerformanceRuleVersion).where(
                PerformanceRuleVersion.code == code,
                PerformanceRuleVersion.version == version,
                PerformanceRuleVersion.dataset_id == dataset_id,
            )
        )

    def max_version(self, code: str, dataset_id: UUID) -> int:
        return self.session.scalar(
            select(func.coalesce(func.max(PerformanceRuleVersion.version), 0)).where(
                PerformanceRuleVersion.code == code,
                PerformanceRuleVersion.dataset_id == dataset_id,
            )
        ) or 0

    def effective_rule_on(self, day: date, dataset_id: UUID,
                          code: str | None = None) -> PerformanceRuleVersion | None:
        """按生效区间命中某日的规则版本，**不限状态**。

        历史月冻结时引用当时版本：旧版本已被后继接替为 SUPERSEDED，但其生效区间
        仍覆盖历史月，故这里不能只看 ACTIVE（Q105/Q106 改规则不回算历史）。
        code 可省略——绩效冻结按“该数据集当日唯一生效规则”解析，避免调用方硬编码。
        """
        stmt = select(PerformanceRuleVersion).where(
            PerformanceRuleVersion.dataset_id == dataset_id,
            PerformanceRuleVersion.effective_from <= day,
            or_(PerformanceRuleVersion.effective_to.is_(None),
                PerformanceRuleVersion.effective_to > day),
        )
        if code is not None:
            stmt = stmt.where(PerformanceRuleVersion.code == code)
        return self.session.scalar(
            stmt.order_by(PerformanceRuleVersion.version.desc())
        )

    def list_active_rules(self, code: str, dataset_id: UUID):
        return list(self.session.scalars(
            select(PerformanceRuleVersion).where(
                PerformanceRuleVersion.dataset_id == dataset_id,
                PerformanceRuleVersion.code == code,
                PerformanceRuleVersion.status == "ACTIVE",
            ).order_by(PerformanceRuleVersion.version)
        ))

    def list_rule_versions(self, dataset_id: UUID,
                           code: str | None = None
                           ) -> list[PerformanceRuleVersion]:
        """规则版本列表（含 DRAFT/ACTIVE/SUPERSEDED 全量，历史版本仍可反查）。"""
        stmt = select(PerformanceRuleVersion).where(
            PerformanceRuleVersion.dataset_id == dataset_id,
        )
        if code:
            stmt = stmt.where(PerformanceRuleVersion.code == code)
        return list(self.session.scalars(
            stmt.order_by(PerformanceRuleVersion.code,
                          PerformanceRuleVersion.version.desc())
        ))

    # ---------- 快照 ----------

    def add_snapshot(self, snap: PerformanceMonthlySnapshot) -> None:
        self.session.add(snap)
        self.session.flush()

    def add_line(self, line: PerformanceSnapshotLine) -> None:
        self.session.add(line)

    def add_adjustment(self, adj: PerformanceSnapshotAdjustment) -> None:
        self.session.add(adj)
        self.session.flush()

    def get_snapshot(self, snapshot_id: UUID) -> PerformanceMonthlySnapshot | None:
        return self.session.get(PerformanceMonthlySnapshot, snapshot_id)

    def find_snapshot(self, user_id: UUID, period_month: str,
                      dataset_id: UUID) -> PerformanceMonthlySnapshot | None:
        return self.session.scalar(
            select(PerformanceMonthlySnapshot).where(
                PerformanceMonthlySnapshot.user_id == user_id,
                PerformanceMonthlySnapshot.period_month == period_month,
                PerformanceMonthlySnapshot.dataset_id == dataset_id,
            )
        )

    def list_snapshots(self, dataset_id: UUID, *, period_month: str | None = None,
                       status: str | None = None, user_id: UUID | None = None,
                       limit: int = 200
                       ) -> list[tuple[PerformanceMonthlySnapshot, User]]:
        """月度快照列表（连带业务员，供页面直接展示，不再逐行回查用户）。

        行级范围由服务层按角色收窄（业务员仅本人），此处只做数据范围与筛选。
        """
        stmt = (
            select(PerformanceMonthlySnapshot, User)
            .join(User, User.id == PerformanceMonthlySnapshot.user_id)
            .where(PerformanceMonthlySnapshot.dataset_id == dataset_id)
        )
        if period_month:
            stmt = stmt.where(
                PerformanceMonthlySnapshot.period_month == period_month)
        if status:
            stmt = stmt.where(PerformanceMonthlySnapshot.status == status)
        if user_id:
            stmt = stmt.where(PerformanceMonthlySnapshot.user_id == user_id)
        rows = self.session.execute(
            stmt.order_by(PerformanceMonthlySnapshot.period_month.desc(),
                          User.display_name.asc())
            .limit(limit)
        ).all()
        return [(snap, user) for snap, user in rows]

    def list_lines(self, snapshot_id: UUID) -> list[PerformanceSnapshotLine]:
        return list(self.session.scalars(
            select(PerformanceSnapshotLine).where(
                PerformanceSnapshotLine.snapshot_id == snapshot_id
            ).order_by(PerformanceSnapshotLine.fact_date,
                       PerformanceSnapshotLine.line_type)
        ))

    def list_adjustments(self, snapshot_id: UUID) -> list[PerformanceSnapshotAdjustment]:
        return list(self.session.scalars(
            select(PerformanceSnapshotAdjustment).where(
                PerformanceSnapshotAdjustment.snapshot_id == snapshot_id
            ).order_by(PerformanceSnapshotAdjustment.created_at)
        ))

    def get_approval(self, approval_id: UUID) -> ApprovalRequest | None:
        return self.session.get(ApprovalRequest, approval_id)

    # ---------- 页面下拉（T19 前端联调） ----------

    def list_dataset_users(self, dataset_id: UUID,
                           role_code: str | None = None) -> list[User]:
        """数据集内在职业用户（冻结快照的“业务员”下拉）。"""
        stmt = select(User).where(
            User.status == "ACTIVE",
            User.dataset_id == dataset_id,
        )
        if role_code:
            stmt = stmt.join(Role, Role.id == User.role_id).where(
                Role.role_code == role_code)
        return list(self.session.scalars(stmt.order_by(User.display_name.asc())))

    def list_approval_options(self, start: date, end: date, dataset_id: UUID,
                              user_id: UUID, action: str
                              ) -> list[ApprovalRequest]:
        """该用户当月的已审批绩效扣减/调整依据（更正单必须引用其中之一）。"""
        return [
            ap for ap in self.approved_performance_approvals(
                start, end, dataset_id)
            if ap.action == action
            and str((ap.request_payload or {}).get("user_id")) == str(user_id)
        ]

    # ---------- 已审核事实采集（月窗口 [start, end]，含端点） ----------

    def active_contracts(self, start: date, end: date, dataset_id: UUID,
                         merchant_ids: list[UUID] | None = None) -> list[Contract]:
        # 仅“新签”：当月签署且已生效（T09 生效态 EFFECTIVE，signed_at 落月）；
        # 草稿/审批中/驳回不计；续签（有 renewal 关联）由调用期事实去重控制
        stmt = select(Contract).where(
            Contract.dataset_id == dataset_id,
            Contract.status == ContractStatus.EFFECTIVE.value,
            Contract.signed_at.is_not(None),
            Contract.signed_at >= start,
            Contract.signed_at <= end,
        )
        if merchant_ids is not None:
            stmt = stmt.where(Contract.merchant_id.in_(merchant_ids or [UUID(int=0)]))
        return list(self.session.scalars(stmt))

    def valid_visits(self, start: date, end: date, dataset_id: UUID,
                     merchant_ids: list[UUID] | None = None) -> list[VisitRecord]:
        """有效巡访：validity=VALID（含经理 RECOGNIZED 认可，其 validity 仍为
        VALID）；ABNORMAL_PENDING/INVALID 不计；SUPERVISION 督导跟访不计业务员。

        按提交时间落月（Asia/Shanghai），归属由服务层用事实日 owner_at 判定，
        避免把巡访直接记到提交者（业务员离职/调拨时仍按事实日归属）。
        """
        stmt = select(VisitRecord).where(
            VisitRecord.dataset_id == dataset_id,
            # 督导跟访（SUPERVISION）不进入业务员绩效；周排班/临时/整改计入
            VisitRecord.source != "SUPERVISION",
            VisitRecord.is_official.is_(True),
            VisitRecord.validity == "VALID",
            func.date(func.timezone("Asia/Shanghai", VisitRecord.submitted_at))
                >= start,
            func.date(func.timezone("Asia/Shanghai", VisitRecord.submitted_at))
                <= end,
        )
        if merchant_ids is not None:
            stmt = stmt.where(VisitRecord.merchant_id.in_(
                merchant_ids or [UUID(int=0)]))
        return list(self.session.scalars(stmt))

    def confirmed_sales(self, start: date, end: date, dataset_id: UUID,
                        merchant_ids: list[UUID] | None = None
                        ) -> list[SalesConfirmation]:
        # 经理确认销量：confirmer_id 非空（确认即生效），按业务日落月
        stmt = select(SalesConfirmation).where(
            SalesConfirmation.dataset_id == dataset_id,
            SalesConfirmation.biz_date >= start,
            SalesConfirmation.biz_date <= end,
        )
        if merchant_ids is not None:
            stmt = stmt.where(SalesConfirmation.merchant_id.in_(
                merchant_ids or [UUID(int=0)]))
        return list(self.session.scalars(stmt))

    def reviewed_activities_for_user(self, start: date, end: date,
                                     dataset_id: UUID,
                                     user_id: UUID) -> list[Activity]:
        """该用户参与且已复盘（REVIEWED/CLOSED）、按活动开始日落月的活动。"""
        stmt = (
            select(Activity)
            .join(ActivityParticipant,
                  ActivityParticipant.activity_id == Activity.id)
            .where(
                Activity.dataset_id == dataset_id,
                Activity.status.in_([ActivityStatus.REVIEWED.value,
                                     ActivityStatus.CLOSED.value]),
                func.date(func.timezone(
                    "Asia/Shanghai", Activity.start_at)) >= start,
                func.date(func.timezone(
                    "Asia/Shanghai", Activity.start_at)) <= end,
                ActivityParticipant.user_id == user_id,
                ActivityParticipant.dataset_id == dataset_id,
            )
        )
        return list(self.session.scalars(stmt))

    def primary_owner_at(self, merchant_id: UUID, at: datetime,
                         dataset_id: UUID) -> UUID | None:
        """商家在某时点（含调拨生效日边界）的主负责人（TSTZRANGE 命中）。

        T14：区间采用 PostgreSQL 默认 [) 边界，调拨生效日 00:00（上海）起事实
        归新负责人，故“生效日含当天”天然成立。
        """
        row = self.session.scalar(
            select(MerchantAssignment.owner_user_id).where(
                MerchantAssignment.merchant_id == merchant_id,
                MerchantAssignment.dataset_id == dataset_id,
                MerchantAssignment.assignment_type == "PRIMARY",
                MerchantAssignment.valid_range.op("@>")(_tstz_point(at)),
            ).order_by(MerchantAssignment.created_at.desc())
        )
        return row

    # ---------- 已审批扣减依据（R40：仅责任扣减，不含商家主动取消） ----------

    def approved_performance_approvals(self, start: date, end: date,
                                       dataset_id: UUID) -> list[ApprovalRequest]:
        """已审批的绩效扣减/调整依据（R40），按动作类型区分。

        商家主动取消合作走合同终止/审批的其它动作，**不在此集合**，故不默认扣款。
        """
        return list(self.session.scalars(
            select(ApprovalRequest).where(
                ApprovalRequest.dataset_id == dataset_id,
                ApprovalRequest.action.in_([DEDUCTION_ACTION, ADJUSTMENT_ACTION]),
                ApprovalRequest.status == "APPROVED",
                func.date(func.coalesce(ApprovalRequest.decided_at,
                                        ApprovalRequest.updated_at)) >= start,
                func.date(func.coalesce(ApprovalRequest.decided_at,
                                        ApprovalRequest.updated_at)) <= end,
            )
        ))

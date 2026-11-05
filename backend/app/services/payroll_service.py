"""PayrollService（T20 / PRD §4.10、§6.5：工资单与发放状态）。

红线：
- 工资 = 底薪 + T19 **已财务复核**绩效（PerformanceMonthlySnapshot REVIEWED）
  + 审批调整；Service 只接受同一 dataset_type+dataset_id 的上游事实，
  DataScope.PRODUCTION 工资单引用 DEMO 绩效时**整单拒绝**（Q：跨域整单拒）；
- DRAFT→REVIEWED（经理确认）→APPROVED（财务复核）→PAID（财务发放）；
  **系统管理员无默认发放权**（无 payroll.disburse 能力）；生产可配置财务
  双人复核（复核人≠发放人，settings.payroll_dual_finance_review）（Q110/Q111）；
- PAID 后原四项金额固化、构成行不改，差异只追加**更正单**（is_correction=True），
  累加到 correction_fen/corrected_pay_fen，原工资单保留（Q112/Q113）；
- 金额整数分；首版不接银行代发 / 支付 / 税务 / 发票（附录 A）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ConflictError, DomainError, PermissionDeniedError
from app.core.permissions import (
    MODULE_PAYROLL,
    MODULE_PAYROLL_DISBURSE,
    ActorContext,
    PermissionService,
)
from app.models import User
from app.models.enums import (
    PayrollAdjustmentKind,
    PayrollStatus,
    RoleCode,
)
from app.models.payroll import (
    PayrollAdjustment,
    PayrollLine,
    PayrollStatement,
)
from app.repositories.payroll_repository import PayrollRepository
from app.services.audit_service import (
    EVENT_CREATE,
    EVENT_STATE_TRANSITION,
    EVENT_UPDATE,
    AuditService,
)
from app.services.business_transition_service import BusinessTransitionService
from app.services.state_machine import DOMAIN_PAYROLL

_BONUS_ACTION = "payroll.bonus"
_PENALTY_ACTION = "payroll.penalty"
_OPEN_STATES = (PayrollStatus.DRAFT.value, PayrollStatus.REVIEWED.value)


def _month_bounds(period_month: str) -> tuple[date, date]:
    try:
        year, month = map(int, period_month.split("-"))
        start = date(year, month, 1)
    except (ValueError, AttributeError) as exc:
        raise DomainError(code="PAYROLL_PERIOD_INVALID",
                          message="period_month 必须为 YYYY-MM 格式") from exc
    if not 1 <= month <= 12:
        raise DomainError(code="PAYROLL_PERIOD_INVALID",
                          message="月份必须在 1-12 之间")
    end = date(year, 12, 31) if month == 12 \
        else date(year, month + 1, 1) - timedelta(days=1)
    return start, end


class PayrollService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = PayrollRepository(session)
        self.permissions = PermissionService()
        self.audit = AuditService(session)
        self.transition = BusinessTransitionService(session)

    # ===================== 生成工资单（DRAFT）=====================

    def generate_statement(
        self,
        actor: ActorContext,
        *,
        user_id: UUID,
        period_month: str,
        base_salary_fen: int | None = None,
        performance_snapshot_id: UUID | None = None,
        adjustments: list[dict] | None = None,
    ) -> PayrollStatement:
        """生成月度工资单；重复提交幂等返回已有单（不重算）。

        T35：`base_salary_fen` 可省略 —— 缺省时按 `period_month` 首日解析
        「薪资配置」里生效的底薪（自动带出，前端仍可覆盖）；
        没有可用配置则报 `SALARY_CONFIG_MISSING`，避免生成 0 底薪工资单。

        adjustments: [{kind: BONUS/PENALTY, amount_fen, approval_id, reason}]，
        每一笔均须引用匹配的已审批单据。
        """
        ds_type, ds_id = self._dataset(actor)
        start, end = _month_bounds(period_month)
        target = self.session.get(User, user_id)
        if target is None or target.dataset_id != ds_id:
            raise DomainError(code="PAYROLL_USER_NOT_FOUND",
                              message="工资对象不存在或数据范围不一致")

        existing = self.repo.find_statement(user_id, period_month,
                                            ds_type, ds_id)
        if existing is not None:
            return existing  # 提交幂等：不重算、不覆盖

        if base_salary_fen is None:
            from app.services.salary_config_service import SalaryConfigService

            config = SalaryConfigService(self.session).resolve_config(
                user_id, on_date=start, dataset_id=ds_id
            )
            if config is None:
                raise DomainError(
                    code="SALARY_CONFIG_MISSING",
                    message="该员工在工资归属月没有生效的薪资配置，请先在「薪资配置」录入月薪",
                    details={"user_id": str(user_id),
                             "period_month": period_month},
                )
            base_salary_fen = int(config.base_salary_fen)
        if base_salary_fen < 0:
            raise DomainError(code="PAYROLL_BASE_INVALID",
                              message="底薪必须为非负整数分")

        # T19 已复核绩效：显式指定则校验，否则按 user+month 解析（必须 REVIEWED）
        snapshot = self._resolve_snapshot(
            performance_snapshot_id, user_id, period_month, ds_type, ds_id)
        performance_fen = snapshot.net_performance_fen if snapshot else 0

        statement = PayrollStatement(
            statement_no=f"PY-{uuid4().hex[:14].upper()}",
            user_id=user_id, period_month=period_month,
            period_start=start, period_end=end,
            performance_snapshot_id=snapshot.id if snapshot else None,
            base_salary_fen=base_salary_fen,
            performance_fen=performance_fen,
            adjustment_fen=0, gross_pay_fen=0,
            status=PayrollStatus.DRAFT.value,
            dataset_type=ds_type, dataset_id=ds_id,
        )
        self.session.add(statement)
        self.session.flush()

        self.session.add(PayrollLine(
            statement_id=statement.id, line_type="BASE_SALARY",
            source_object_type="BASE_SALARY", source_object_id=statement.id,
            amount_fen=base_salary_fen,
            basis_json={"base_salary_fen": base_salary_fen},
            dataset_type=ds_type, dataset_id=ds_id))
        if snapshot is not None:
            self.session.add(PayrollLine(
                statement_id=statement.id, line_type="PERFORMANCE",
                source_object_type="PERFORMANCE_MONTHLY_SNAPSHOT",
                source_object_id=snapshot.id, amount_fen=performance_fen,
                basis_json={"snapshot_id": str(snapshot.id),
                            "period_month": period_month,
                            "snapshot_status": snapshot.status,
                            "frozen_at": (snapshot.frozen_at.isoformat()
                                          if snapshot.frozen_at else None),
                            "net_performance_fen": performance_fen},
                dataset_type=ds_type, dataset_id=ds_id))

        adj_sum = 0
        for item in adjustments or []:
            adj = self._build_adjustment(statement, item, ds_type, ds_id,
                                         is_correction=False,
                                         created_by_id=actor.user_id)
            self.session.add(adj)
            self.session.add(PayrollLine(
                statement_id=statement.id, line_type="ADJUSTMENT",
                source_object_type="APPROVAL_REQUEST",
                source_object_id=adj.approval_id, amount_fen=adj.amount_fen,
                basis_json={"kind": adj.kind,
                            "approval_id": str(adj.approval_id),
                            "reason": adj.reason},
                dataset_type=ds_type, dataset_id=ds_id))
            adj_sum += adj.amount_fen

        statement.adjustment_fen = adj_sum
        gross = base_salary_fen + performance_fen + adj_sum
        if gross < 0:
            raise DomainError(code="PAYROLL_GROSS_NEGATIVE",
                              message="应发工资不得为负")
        statement.gross_pay_fen = gross
        self.session.flush()
        self.audit.record(object_type="PAYROLL_STATEMENT",
                          object_id=statement.id, event_type=EVENT_CREATE,
                          actor_id=actor.user_id,
                          after={"statement_no": statement.statement_no,
                                 "user_id": str(user_id),
                                 "period_month": period_month,
                                 "gross_pay_fen": gross})
        return statement

    # ===================== 发放前追加审批调整（DRAFT/REVIEWED）=====================

    def add_adjustment(
        self, actor: ActorContext, statement_id: UUID, *,
        kind: str, amount_fen: int, approval_id: UUID, reason: str,
    ) -> PayrollAdjustment:
        """发放前工资调整（经理/财务）；PAID 后请走 apply_correction。"""
        ds_type, ds_id = self._dataset(actor)
        st = self._load(statement_id, ds_type, ds_id)
        if st.status not in _OPEN_STATES:
            raise ConflictError(
                code="PAYROLL_ADJUST_STATE_INVALID",
                message="仅未发放工资单可追加调整；已发放请走更正单",
                details={"status": st.status})
        existing = self.repo.find_adjustment_by_approval(st.id, approval_id)
        if existing is not None:
            return existing  # 同审批幂等，不重复落行/改合计
        adj = self._build_adjustment(
            st, {"kind": kind, "amount_fen": amount_fen,
                 "approval_id": approval_id, "reason": reason},
            ds_type, ds_id, is_correction=False,
            created_by_id=actor.user_id)
        self.session.add(adj)
        self.session.add(PayrollLine(
            statement_id=st.id, line_type="ADJUSTMENT",
            source_object_type="APPROVAL_REQUEST",
            source_object_id=approval_id, amount_fen=amount_fen,
            basis_json={"kind": kind, "approval_id": str(approval_id),
                        "reason": reason.strip()},
            dataset_type=ds_type, dataset_id=ds_id))
        self.session.flush()
        self._recompute_open(st)
        self.audit.record(object_type="PAYROLL_ADJUSTMENT", object_id=adj.id,
                          event_type=EVENT_CREATE, actor_id=actor.user_id,
                          after={"statement_id": str(st.id), "kind": kind,
                                 "amount_fen": amount_fen})
        return adj

    # ===================== 经理确认 / 财务复核 / 发放 =====================

    def confirm_statement(
        self, actor: ActorContext, statement_id: UUID,
    ) -> PayrollStatement:
        """区域经理确认（DRAFT→REVIEWED）。"""
        ds_type, ds_id = self._dataset(actor)
        st = self._load(statement_id, ds_type, ds_id)
        self._require_manager(actor)
        self._transition(st, PayrollStatus.REVIEWED.value, actor,
                         "区域经理确认月度工资单")
        st.confirmed_by_id = actor.user_id
        st.confirmed_at = datetime.now(UTC)
        self.session.flush()
        return st

    def review_statement(
        self, actor: ActorContext, statement_id: UUID,
    ) -> PayrollStatement:
        """财务复核（REVIEWED→APPROVED）。"""
        ds_type, ds_id = self._dataset(actor)
        st = self._load(statement_id, ds_type, ds_id)
        self._require_payroll_finance(actor)
        self._transition(st, PayrollStatus.APPROVED.value, actor,
                         "财务复核通过月度工资单")
        st.reviewed_by_id = actor.user_id
        st.reviewed_at = datetime.now(UTC)
        self.session.flush()
        return st

    def mark_paid(
        self, actor: ActorContext, statement_id: UUID, *,
        paid_on: date | None = None,
    ) -> PayrollStatement:
        """财务标记已发放（APPROVED→PAID）；固化 gross_pay_fen。

        - 系统管理员无 payroll.disburse 能力 → 403（Q110）；
        - 开启双人复核后，发放人不得与复核人相同（Q111）。
        """
        ds_type, ds_id = self._dataset(actor)
        st = self._load(statement_id, ds_type, ds_id)
        if not self.permissions.has_ability(actor, MODULE_PAYROLL_DISBURSE):
            raise PermissionDeniedError(
                code="PAYROLL_DISBURSE_FORBIDDEN",
                message="无工资发放权限；系统管理员无默认发放权",
                details={"actor_id": str(actor.user_id)})
        if st.status != PayrollStatus.APPROVED.value:
            raise ConflictError(
                code="PAYROLL_STATUS_CONFLICT",
                message="仅财务复核通过(APPROVED)的工资单可标记发放",
                details={"status": st.status})
        settings = get_settings()
        if settings.payroll_dual_finance_review \
                and st.reviewed_by_id is not None \
                and st.reviewed_by_id == actor.user_id:
            raise PermissionDeniedError(
                code="PAYROLL_DUAL_REVIEW_REQUIRED",
                message="生产双人复核开启：发放人与复核人不得为同一财务")
        self._transition(st, PayrollStatus.PAID.value, actor,
                         "财务标记工资已发放")
        st.paid_by_id = actor.user_id
        st.paid_at = datetime.now(UTC)
        st.paid_on = paid_on or st.paid_at.date()
        # 发放固化应发（PAID 后 gross_pay_fen 不再随更正单变动）
        self.session.flush()

        # T35：线下已发放 → 登记工资支出流水（金额 = 应发合计；重复发放已挡在状态机）
        from app.models.finance import SOURCE_EXPENSE_PAYROLL
        from app.services.finance_ledger_service import FinanceLedgerService

        FinanceLedgerService(self.session).record_expense(
            actor,
            biz_date=st.paid_on,
            amount_fen=int(st.gross_pay_fen),
            source_kind=SOURCE_EXPENSE_PAYROLL,
            related_type="PAYROLL_STATEMENT",
            related_id=st.id,
            note=f"工资单 {st.statement_no} 发放（线下）",
            dataset_type=st.dataset_type,
            dataset_id=st.dataset_id,
        )
        return st

    # ===================== 已发放更正单（Q113）=====================

    def apply_correction(
        self, actor: ActorContext, statement_id: UUID, *,
        kind: str, amount_fen: int, approval_id: UUID, reason: str,
    ) -> PayrollAdjustment:
        """PAID 后只可追加更正单；原四项金额与构成行不改，只累加更正口径。"""
        ds_type, ds_id = self._dataset(actor)
        st = self._load(statement_id, ds_type, ds_id)
        if st.status != PayrollStatus.PAID.value:
            raise ConflictError(
                code="PAYROLL_CORRECTION_STATE_INVALID",
                message="仅已发放(PAID)工资单可追加更正单；未发放请直接调整",
                details={"status": st.status})
        self._require_payroll_finance(actor)
        # 同一审批依据幂等：已存在更正单则直接返回，不重复累加更正口径
        existing = self.repo.find_adjustment_by_approval(st.id, approval_id)
        if existing is not None:
            return existing
        adj = self._build_adjustment(
            st, {"kind": kind, "amount_fen": amount_fen,
                 "approval_id": approval_id, "reason": reason},
            ds_type, ds_id, is_correction=True,
            created_by_id=actor.user_id)
        self.session.add(adj)
        self.session.flush()

        st.correction_fen = (st.correction_fen or 0) + adj.amount_fen
        corrected = st.gross_pay_fen + st.correction_fen
        if corrected < 0:
            raise DomainError(code="PAYROLL_CORRECTED_NEGATIVE",
                              message="更正后应发不得为负")
        st.corrected_pay_fen = corrected
        st.corrected_by_id = actor.user_id
        st.corrected_at = datetime.now(UTC)
        self.session.flush()
        self.audit.record(object_type="PAYROLL_STATEMENT", object_id=st.id,
                          event_type=EVENT_UPDATE, actor_id=actor.user_id,
                          after={"correction_fen": st.correction_fen,
                                 "corrected_pay_fen": corrected},
                          reason=f"已发放工资更正单：{reason.strip()}")
        return adj

    # ===================== 查询 =====================

    def list_statements(
        self,
        actor: ActorContext,
        *,
        period_month: str | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[dict], int]:
        """工资单列表（财务工资发放页）：仅工资能力持有者，按数据集隔离。"""
        self._require_payroll_finance(actor)
        if period_month is not None:
            _month_bounds(period_month)
        ds_id = self._dataset(actor)[1]
        rows, total = self.repo.list_statements(
            dataset_id=ds_id,
            period_month=period_month,
            status=status,
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        return [{"statement": st, "user_name": name} for st, name in rows], total

    def list_own_statements(
        self,
        actor: ActorContext,
        *,
        period_month: str | None = None,
        page: int = 1,
        page_size: int = 200,
    ) -> tuple[list[dict], int]:
        """本人工资单（员工自查口径）：行级=本人，不校验财务能力。

        员工查自己的工资条是正当诉求（AI 助手「查我近半年工资」走这里）；
        越权放大（查他人）在仓库层 user_id 条件处截断，服务端注入身份。
        """
        if period_month is not None:
            _month_bounds(period_month)
        ds_id = self._dataset(actor)[1]
        rows, total = self.repo.list_statements(
            dataset_id=ds_id,
            period_month=period_month,
            user_id=actor.user_id,
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        return [{"statement": st, "user_name": name} for st, name in rows], total

    def get_detail(self, actor: ActorContext,
                   statement_id: UUID) -> dict:
        ds_type, ds_id = self._dataset(actor)
        st = self._load(statement_id, ds_type, ds_id)
        user = self.session.get(User, st.user_id)
        return {"statement": st,
                "user_name": user.display_name if user is not None else None,
                "lines": self.repo.list_lines(st.id),
                "adjustments": self.repo.list_adjustments(st.id)}

    # ===================== 内部 =====================

    def _resolve_snapshot(
        self, snapshot_id: UUID | None, user_id: UUID, period_month: str,
        ds_type: str, ds_id: UUID,
    ):
        """解析绩效快照；PRODUCTION 工资引用 DEMO 绩效 → 整单拒绝。

        T35：缺省按「当月最新快照」解析（**含未复核 DRAFT**），因为快照已由
        巡访完成自动重算，工资草稿需要即时取用；显式指定时仍校验数据域与归属。
        """
        if snapshot_id is None:
            return self.repo.latest_snapshot_for_user(
                user_id, period_month, ds_type, ds_id)
        from app.models.performance import PerformanceMonthlySnapshot
        snap = self.session.get(PerformanceMonthlySnapshot, snapshot_id)
        if snap is None:
            raise DomainError(code="PAYROLL_SNAPSHOT_NOT_FOUND",
                              message="引用的绩效快照不存在")
        # 跨数据域（含 PRODUCTION 工资引用 DEMO 绩效）整单拒绝
        if snap.dataset_type != ds_type or snap.dataset_id != ds_id:
            raise DomainError(
                code="PAYROLL_SCOPE_DEMO_MIXED",
                message="工资单与绩效快照不属于同一数据域；整单拒绝",
                details={"statement_dataset": f"{ds_type}/{ds_id}",
                         "snapshot_dataset":
                             f"{snap.dataset_type}/{snap.dataset_id}"})
        if snap.user_id != user_id:
            raise DomainError(code="PAYROLL_SNAPSHOT_USER_MISMATCH",
                              message="绩效快照不属于该工资对象")
        return snap

    def _build_adjustment(
        self, st: PayrollStatement, item: dict, ds_type: str, ds_id: UUID,
        *, is_correction: bool, created_by_id: UUID,
    ) -> PayrollAdjustment:
        kind = item.get("kind")
        amount = item.get("amount_fen")
        approval_id = item.get("approval_id")
        reason = (item.get("reason") or "").strip()
        if kind not in (PayrollAdjustmentKind.BONUS.value,
                        PayrollAdjustmentKind.PENALTY.value):
            raise DomainError(code="PAYROLL_ADJUST_KIND_INVALID",
                              message="调整类型必须为 BONUS/PENALTY")
        if not isinstance(amount, int) or isinstance(amount, bool):
            raise DomainError(code="PAYROLL_ADJUST_AMOUNT_INVALID",
                              message="调整金额必须为整数分")
        if kind == PayrollAdjustmentKind.BONUS.value and amount < 0:
            raise DomainError(code="PAYROLL_ADJUST_SIGN_INVALID",
                              message="奖金(BONUS)必须非负")
        if kind == PayrollAdjustmentKind.PENALTY.value and amount >= 0:
            raise DomainError(code="PAYROLL_ADJUST_SIGN_INVALID",
                              message="罚扣(PENALTY)必须为负")
        if not reason:
            raise DomainError(code="PAYROLL_ADJUST_REASON_REQUIRED",
                              message="工资调整必须填写原因")
        if not approval_id:
            raise DomainError(code="PAYROLL_ADJUST_APPROVAL_REQUIRED",
                              message="工资调整必须引用已审批依据单据")

        # 同一审批单在同一工资单上幂等
        existing = self.repo.find_adjustment_by_approval(st.id, approval_id)
        if existing is not None:
            return existing

        ap = self.repo.approved_approval(approval_id)
        if ap is None:
            raise DomainError(code="PAYROLL_ADJUST_APPROVAL_INVALID",
                              message="审批依据不存在或未通过",
                              details={"approval_id": str(approval_id)})
        want_action = (_BONUS_ACTION if kind == PayrollAdjustmentKind.BONUS.value
                       else _PENALTY_ACTION)
        if ap.action != want_action:
            raise DomainError(code="PAYROLL_ADJUST_ACTION_MISMATCH",
                              message="审批动作与工资调整类型不匹配")
        if ap.dataset_type != ds_type or ap.dataset_id != ds_id:
            raise DomainError(
                code="PAYROLL_SCOPE_DEMO_MIXED",
                message="审批依据与工资单不属于同一数据域；整单拒绝")
        payload = ap.request_payload or {}
        if payload.get("user_id") is not None \
                and UUID(str(payload["user_id"])) != st.user_id:
            raise DomainError(code="PAYROLL_ADJUST_USER_MISMATCH",
                              message="审批单据的目标人员与工资对象不一致")
        if payload.get("amount_fen") is not None \
                and int(payload["amount_fen"]) != amount:
            raise DomainError(code="PAYROLL_ADJUST_AMOUNT_MISMATCH",
                              message="调整金额与审批依据不一致")

        return PayrollAdjustment(
            statement_id=st.id, kind=kind, amount_fen=amount,
            approval_id=approval_id, reason=reason,
            is_correction=is_correction, created_by_id=created_by_id,
            dataset_type=ds_type, dataset_id=ds_id)

    def _recompute_open(self, st: PayrollStatement) -> None:
        """发放前：重算 adjustment_fen / gross_pay_fen（原底薪/绩效固化）。"""
        adjs = [a for a in self.repo.list_adjustments(st.id)
                if not a.is_correction]
        st.adjustment_fen = sum(a.amount_fen for a in adjs)
        gross = st.base_salary_fen + st.performance_fen + st.adjustment_fen
        if gross < 0:
            raise DomainError(code="PAYROLL_GROSS_NEGATIVE",
                              message="应发工资不得为负")
        st.gross_pay_fen = gross

    def _transition(self, st: PayrollStatement, to_state: str,
                    actor: ActorContext, reason: str) -> None:
        before = {"status": st.status, "gross_pay_fen": st.gross_pay_fen}
        self.transition.transition(
            entity=st, domain=DOMAIN_PAYROLL, to_state=to_state,
            actor_id=actor.user_id, reason=reason)
        self.audit.record(
            object_type="PAYROLL_STATEMENT", object_id=st.id,
            event_type=EVENT_STATE_TRANSITION, actor_id=actor.user_id,
            before=before, after={"status": to_state}, reason=reason)

    def _load(self, statement_id: UUID, ds_type: str,
              ds_id: UUID) -> PayrollStatement:
        st = self.repo.get_statement(statement_id)
        if st is None:
            raise DomainError(code="PAYROLL_NOT_FOUND",
                              message="工资单不存在")
        if st.dataset_type != ds_type or st.dataset_id != ds_id:
            raise PermissionDeniedError(code="PAYROLL_DATASET_FORBIDDEN",
                                        message="无权访问其他数据域的工资单")
        return st

    def _require_manager(self, actor: ActorContext) -> None:
        if actor.role_code != RoleCode.REGIONAL_MANAGER.value:
            raise PermissionDeniedError(
                code="PAYROLL_MANAGER_ONLY",
                message="仅区域经理可确认工资单",
                details={"actor_id": str(actor.user_id)})

    def _require_payroll_finance(self, actor: ActorContext) -> None:
        if not self.permissions.has_ability(actor, MODULE_PAYROLL):
            raise PermissionDeniedError(
                code="PAYROLL_FINANCE_ONLY",
                message="仅财务可复核/更正工资单",
                details={"actor_id": str(actor.user_id)})

    def _dataset(self, actor: ActorContext) -> tuple[str, UUID]:
        user = self.session.get(User, actor.user_id)
        if user is None:
            raise DomainError(code="USER_NOT_FOUND", message="登录用户不存在")
        return "PRODUCTION", user.dataset_id

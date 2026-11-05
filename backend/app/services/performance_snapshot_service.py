"""PerformanceSnapshotService：绩效规则版本、月末冻结快照与确认/复核（T19）。

职责边界（PRD §4.10、§6.5、R19/R20/R24/R40）：
- 规则版本：配置人起草（DRAFT）→ 审核人审核生效（ACTIVE）；新版本生效后旧版本
  置 SUPERSEDED；历史快照固化引用旧版本，**改规则不回算历史**（Q105/Q106）。
- 月末冻结：只采集**已审核事实**（生效合同 / 有效巡访 / 经理确认销量 / 已复盘
  活动），按 T14 调拨日 owner_at 归属到当月负责人，调用纯函数 PerformanceEngine
  计算并落快照头+逐项依据行；冻结结果为 DRAFT。
- 经理确认（→CONFIRMED）、财务复核（→REVIEWED）；复核可退回 CONFIRMED。
- CONFIRMED/REVIEWED 后原行不可改，任何扣减/调整只追加“经审批依据”的更正单，
  重算净额；**商家主动取消不默认扣款，无 APPROVED 依据的扣减一律拒绝**（Q183）。
- 金额整数分、箱数 Decimal，禁止 float；不让 AI 决定工资（本服务只算个人绩效，
  不含底薪/社保/个税/发放，那些在 payroll 后续域）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.errors import DomainError, PermissionDeniedError
from app.core.permissions import (
    MODULE_PERFORMANCE,
    ActorContext,
    PermissionService,
)
from app.models import (
    PerformanceMonthlySnapshot,
    PerformanceRuleVersion,
    PerformanceSnapshotAdjustment,
    PerformanceSnapshotLine,
    User,
)
from app.models.enums import (
    PerformanceRuleStatus,
    PerformanceSnapshotStatus,
    RoleCode,
)
from app.repositories.performance_repository import (
    PerformanceRepository,
)
from app.services.audit_service import (
    EVENT_CREATE,
    EVENT_UPDATE,
    AuditService,
)
from app.services.business_transition_service import BusinessTransitionService
from app.services.demo_data_guard import DataScope, assert_production_scope
from app.services.performance_engine import (
    EngineFact,
    PerformanceEngine,
    RuleParams,
)
from app.services.state_machine import DOMAIN_PERFORMANCE_SNAPSHOT

DEFAULT_RULE_CODE = "SALES_PERSON_COMMISSION"

# 查询入参白名单（避免非法枚举值把 SQL 打成空结果）
_SNAPSHOT_STATUSES = {s.value for s in PerformanceSnapshotStatus}
_ROLE_CODES = {r.value for r in RoleCode}


def _month_bounds(period_month: str) -> tuple[date, date]:
    """'YYYY-MM' → 月初、月末（按日历月）。"""
    try:
        year, month = map(int, period_month.split("-"))
        start = date(year, month, 1)
    except (ValueError, AttributeError) as exc:
        raise DomainError(
            code="PERF_PERIOD_INVALID",
            message="period_month 必须为 YYYY-MM 格式",
        ) from exc
    if not 1 <= month <= 12:
        raise DomainError(code="PERF_PERIOD_INVALID", message="月份必须在 1-12 之间")
    if month == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)
    return start, end


# 事实单据类型常量
_OT_CONTRACT = "CONTRACT"
_OT_VISIT = "VISIT_RECORD"
_OT_SALES = "SALES_CONFIRMATION"
_OT_ACTIVITY = "ACTIVITY"
_OT_APPROVAL = "APPROVAL_REQUEST"


class PerformanceSnapshotService:
    def __init__(self, session: Session):
        self.session = session
        self.repo = PerformanceRepository(session)
        self.permissions = PermissionService()
        self.audit = AuditService(session)
        self.transition = BusinessTransitionService(session)
        self.engine = PerformanceEngine()

    # ---------- 数据集 / 权限 ----------

    def _dataset(self, actor: ActorContext) -> UUID:
        user = self.session.get(User, actor.user_id)
        if user is None:
            raise DomainError(code="USER_NOT_FOUND", message="登录用户不存在")
        return user.dataset_id

    def _require_performance(self, actor: ActorContext) -> None:
        self.permissions.require_ability(actor, MODULE_PERFORMANCE)

    @staticmethod
    def _params(version: PerformanceRuleVersion) -> RuleParams:
        p = version.params_json or {}
        try:
            return RuleParams(
                new_contract_award_fen=int(p["new_contract_award_fen"]),
                valid_visit_award_fen=int(p["valid_visit_award_fen"]),
                sales_tiers=tuple(
                    (int(cap) if cap is not None else None, int(bp))
                    for cap, bp in p["sales_tiers"]
                ),
                activity_award_fen=int(p["activity_award_fen"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DomainError(
                code="PERF_RULE_PARAMS_INVALID",
                message="规则参数缺失或非法（需含 4 类金额/阶梯配置）",
            ) from exc

    # ================= 规则版本 =================

    def create_rule_version(
        self,
        actor: ActorContext,
        *,
        params: dict,
        effective_from: date,
        name: str,
        code: str = DEFAULT_RULE_CODE,
        effective_to: date | None = None,
    ) -> PerformanceRuleVersion:
        """配置人起草新规则版本（DRAFT）。version 由服务端递增。"""
        self._require_performance(actor)
        ds = self._dataset(actor)
        assert_production_scope(scope=DataScope.PRODUCTION,
                                where="performance_rule.create")
        # 参数先经纯参数对象校验（金额非负、阶梯递增、封顶档 None）
        self._coerce_params(params)

        version_no = self.repo.max_version(code, ds) + 1
        rule = PerformanceRuleVersion(
            code=code,
            version=version_no,
            name=name,
            status=PerformanceRuleStatus.DRAFT.value,
            effective_from=effective_from,
            effective_to=effective_to,
            params_json=params,
            configured_by_id=actor.user_id,
            dataset_type=DataScope.PRODUCTION.value,
            dataset_id=ds,
        )
        self.repo.add_rule_version(rule)
        self.audit.record(
            object_type="PERFORMANCE_RULE_VERSION", object_id=rule.id,
            event_type=EVENT_CREATE, actor_id=actor.user_id,
            after={"code": code, "version": version_no,
                   "effective_from": str(effective_from)},
        )
        return rule

    def review_rule_version(
        self, actor: ActorContext, rule_version_id: UUID, *,
        note: str | None = None,
    ) -> PerformanceRuleVersion:
        """审核人审核通过规则：DRAFT→ACTIVE，并把同 code 旧 ACTIVE 置 SUPERSEDED。

        配置人与审核人必须分离（R19：审核不能由配置人自审）。
        """
        self._require_performance(actor)
        ds = self._dataset(actor)
        rule = self.repo.get_rule_version(rule_version_id)
        if rule is None or rule.dataset_id != ds:
            raise DomainError(code="PERF_RULE_NOT_FOUND",
                              message="规则版本不存在或数据范围不一致")
        if rule.status != PerformanceRuleStatus.DRAFT.value:
            raise DomainError(
                code="PERF_RULE_STATE_INVALID",
                message="仅 DRAFT 规则可审核生效",
                details={"status": rule.status},
            )
        if rule.configured_by_id == actor.user_id:
            raise PermissionDeniedError(
                code="PERF_RULE_SELF_REVIEW_FORBIDDEN",
                message="规则配置人不能审核自己配置的规则（职责分离）",
            )
        # 生效区间校验：现行 ACTIVE 规则只有一条（open-ended），新版本从生效日起
        # 接替并把它在该日闭合（[) 边界，生效日当天归新版本），属正常版本接替，
        # 不算重叠；仅当存在“已设结束日且结束日晚于新生效日”的 ACTIVE 区间
        # （新版本会切入其内部）时才拒绝。
        for active in self.repo.list_active_rules(rule.code, ds):
            if (active.effective_to is not None
                    and active.effective_to > rule.effective_from):
                raise DomainError(
                    code="PERF_RULE_RANGE_OVERLAP",
                    message="新生效区间与现有 ACTIVE 规则区间重叠",
                    details={"active_version": active.version},
                )

        rule.status = PerformanceRuleStatus.ACTIVE.value
        rule.reviewed_by_id = actor.user_id
        rule.review_note = note
        for active in self.repo.list_active_rules(rule.code, ds):
            if active.id != rule.id:
                active.status = PerformanceRuleStatus.SUPERSEDED.value
                if active.effective_to is None:
                    active.effective_to = rule.effective_from
                # 约束 perf_rule_active_reviewed：仅 ACTIVE 可持有 reviewed_by_id；
                # 审核人留痕已写入 audit_logs，接替时清空该列。
                active.reviewed_by_id = None
        self.session.flush()
        self.audit.record(
            object_type="PERFORMANCE_RULE_VERSION", object_id=rule.id,
            event_type=EVENT_UPDATE, actor_id=actor.user_id,
            before={"status": "DRAFT"},
            after={"status": "ACTIVE", "version": rule.version, "note": note},
        )
        return rule

    @staticmethod
    def _coerce_params(params: dict) -> RuleParams:
        return RuleParams(
            new_contract_award_fen=int(params["new_contract_award_fen"]),
            valid_visit_award_fen=int(params["valid_visit_award_fen"]),
            sales_tiers=tuple(
                (int(cap) if cap is not None else None, int(bp))
                for cap, bp in params["sales_tiers"]
            ),
            activity_award_fen=int(params["activity_award_fen"]),
        )

    def get_effective_rule(self, on_day: date, dataset_id: UUID,
                           code: str | None = None) -> PerformanceRuleVersion:
        """解析某日生效规则（按生效区间命中，含已被接替的 SUPERSEDED 历史版本）。"""
        rule = self.repo.effective_rule_on(on_day, dataset_id, code)
        if rule is None:
            raise DomainError(
                code="PERF_RULE_NOT_EFFECTIVE",
                message=f"{on_day} 无生效的绩效规则版本",
            )
        return rule

    # ================= 月末冻结 =================

    def freeze_month(
        self, actor: ActorContext, *, user_id: UUID, period_month: str,
        rule_version_id: UUID | None = None,
    ) -> PerformanceMonthlySnapshot:
        """冻结某业务员某月绩效：采集已审核事实→引擎计算→落 DRAFT 快照。

        幂等：同一 (user, month) 已冻结则直接返回原快照（月末冻结重复提交）。
        """
        self._require_performance(actor)
        ds = self._dataset(actor)
        assert_production_scope(scope=DataScope.PRODUCTION,
                                where="performance_snapshot.freeze")
        start, end = _month_bounds(period_month)
        target_user = self.session.get(User, user_id)
        if target_user is None or target_user.dataset_id != ds:
            raise DomainError(code="PERF_USER_NOT_FOUND",
                              message="目标用户不存在或数据范围不一致")

        existing = self.repo.find_snapshot(user_id, period_month, ds)
        if existing is not None:
            return existing  # Q-幂等：月末冻结重复提交不重算

        # 规则版本：以月末日期选生效版本（可显式指定且必须 ACTIVE/同数据集）
        if rule_version_id is not None:
            rule = self.repo.get_rule_version(rule_version_id)
            if (rule is None or rule.dataset_id != ds
                    or rule.status != PerformanceRuleStatus.ACTIVE.value):
                raise DomainError(code="PERF_RULE_NOT_ACTIVE",
                                  message="指定规则不存在、跨数据集或未生效")
        else:
            # 按月末日期解析生效规则：不限 code（该数据集当日唯一生效版本），
            # 含已被接替的历史版本——历史月快照固化引用生成时版本。
            rule = self.get_effective_rule(end, ds)
        params = self._params(rule)

        # 采集当月已审核事实，并按 T14 调拨日 owner_at 归属到 user_id
        facts = self._collect_facts(ds, user_id, start, end)

        result = self.engine.compute(
            params=params,
            new_contracts=facts["contracts"],
            activities=facts["activities"],
        )

        snap = PerformanceMonthlySnapshot(
            user_id=user_id,
            period_month=period_month,
            period_start=start,
            period_end=end,
            rule_version_id=rule.id,
            rule_code=rule.code,
            rule_version=rule.version,
            new_contract_fen=result.new_contract_fen,
            visit_bonus_fen=result.visit_bonus_fen,
            sales_commission_fen=result.sales_commission_fen,
            activity_bonus_fen=result.activity_bonus_fen,
            deduction_fen=result.deduction_fen,
            adjustment_fen=result.adjustment_fen,
            net_performance_fen=result.net_fen,
            status=PerformanceSnapshotStatus.DRAFT.value,
            frozen_by_id=actor.user_id,
            frozen_at=datetime.now(UTC),
            dataset_type=DataScope.PRODUCTION.value,
            dataset_id=ds,
        )
        self.repo.add_snapshot(snap)
        for ln in result.lines:
            if ln.source_object_type == "SALES_TIER_SUMMARY":
                continue  # 阶梯汇总依据已并入各销量行，不单独落“事实单据行”
            self.repo.add_line(PerformanceSnapshotLine(
                snapshot_id=snap.id,
                line_type=ln.line_type,
                source_object_type=ln.source_object_type,
                source_object_id=UUID(ln.source_object_id),
                merchant_id=UUID(ln.merchant_id) if ln.merchant_id else None,
                owner_user_id=user_id,
                fact_date=ln.fact_date or end,
                quantity=ln.quantity if ln.quantity else None,
                amount_fen=ln.amount_fen,
                basis_json=ln.basis,
                dataset_type=DataScope.PRODUCTION.value,
                dataset_id=ds,
            ))
        self.session.flush()
        self.audit.record(
            object_type="PERFORMANCE_SNAPSHOT", object_id=snap.id,
            event_type=EVENT_CREATE, actor_id=actor.user_id,
            after={"user_id": str(user_id), "period_month": period_month,
                   "net_fen": result.net_fen, "rule_version": rule.version},
        )
        return snap

    # ---------- 事实采集 + 调拨日归属 ----------

    @staticmethod
    def _day_point(day: date) -> datetime:
        """业务日 → 上海时区当日 00:00（aware UTC），用于 TSTZRANGE 归属。"""
        return datetime(day.year, day.month, day.day,
                        tzinfo=ZoneInfo("Asia/Shanghai"))

    def _collect_facts(self, ds: UUID, user_id: UUID,
                       start: date, end: date) -> dict:
        """采集当月事实并只保留 fact 当日 primary owner==user_id 的部分（T14）。

        归属时点取业务日上海 00:00：调拨生效日（含）起区间切给新负责人，
        生效日前归原负责人，与 T14 owner_at 口径一致。
        """
        contracts: list[EngineFact] = []
        activities: list[EngineFact] = []

        for c in self.repo.active_contracts(start, end, ds):
            signed = c.signed_at or start
            owner = self.repo.primary_owner_at(
                c.merchant_id, self._day_point(signed), ds)
            if owner != user_id:
                continue
            contracts.append(EngineFact(
                object_type=_OT_CONTRACT, object_id=str(c.id),
                fact_date=signed, merchant_id=str(c.merchant_id)))

        # 活动按参与者本人归属（非商家归属），已在 SQL 用 user_id 过滤
        for a in self.repo.reviewed_activities_for_user(start, end, ds, user_id):
            aday = self._shanghai_date(a.start_at)
            activities.append(EngineFact(
                object_type=_OT_ACTIVITY, object_id=str(a.id),
                fact_date=aday))

        # T35：有效巡访与确认销量不再计入绩效，返回空列表保持调用方签名稳定
        return {"contracts": contracts, "visits": [],
                "sales": [], "activities": activities}

    def refresh_month(
        self, user_id: UUID, biz_date: date,
    ) -> PerformanceMonthlySnapshot | None:
        """业务事件触发的当月绩效自动重算（T35）。

        调用点：巡访任务提交/判定完成（同事务）。规则：
        - 没有快照 → 建 DRAFT；
        - DRAFT → 就地重算金额并替换依据行（未复核数据可变）；
        - CONFIRMED/REVIEWED → 一律跳过（已审数据不可变，避免静默改已确认绩效）；
        - 当月无生效规则 → 返回 None，不阻断现场提交。

        异常由调用方兜底（巡访提交只记告警，不让绩效算不出来卡住业务）。
        """
        target = self.session.get(User, user_id)
        if target is None:
            return None
        ds = target.dataset_id
        period_month = biz_date.strftime("%Y-%m")
        start, end = _month_bounds(period_month)

        existing = self.repo.find_snapshot(user_id, period_month, ds)
        if existing is not None and existing.status != (
            PerformanceSnapshotStatus.DRAFT.value
        ):
            return existing

        rule = self.repo.effective_rule_on(end, ds)
        if rule is None:
            return None
        params = self._params(rule)
        facts = self._collect_facts(ds, user_id, start, end)
        result = self.engine.compute(
            params=params,
            new_contracts=facts["contracts"],
            activities=facts["activities"],
        )

        if existing is None:
            snap = PerformanceMonthlySnapshot(
                user_id=user_id,
                period_month=period_month,
                period_start=start,
                period_end=end,
                rule_version_id=rule.id,
                rule_code=rule.code,
                rule_version=rule.version,
                new_contract_fen=result.new_contract_fen,
                visit_bonus_fen=result.visit_bonus_fen,
                sales_commission_fen=result.sales_commission_fen,
                activity_bonus_fen=result.activity_bonus_fen,
                deduction_fen=result.deduction_fen,
                adjustment_fen=result.adjustment_fen,
                net_performance_fen=result.net_fen,
                status=PerformanceSnapshotStatus.DRAFT.value,
                frozen_by_id=user_id,
                frozen_at=datetime.now(UTC),
                dataset_type=DataScope.PRODUCTION.value,
                dataset_id=ds,
            )
            self.repo.add_snapshot(snap)
        else:
            snap = existing
            snap.rule_version_id = rule.id
            snap.rule_code = rule.code
            snap.rule_version = rule.version
            snap.new_contract_fen = result.new_contract_fen
            snap.visit_bonus_fen = result.visit_bonus_fen
            snap.sales_commission_fen = result.sales_commission_fen
            snap.activity_bonus_fen = result.activity_bonus_fen
            snap.deduction_fen = result.deduction_fen
            snap.adjustment_fen = result.adjustment_fen
            snap.net_performance_fen = result.net_fen
            snap.frozen_at = datetime.now(UTC)
            # 未复核快照的依据行整体替换，避免旧事实残留
            self.session.execute(
                delete(PerformanceSnapshotLine).where(
                    PerformanceSnapshotLine.snapshot_id == snap.id
                )
            )

        for ln in result.lines:
            self.repo.add_line(PerformanceSnapshotLine(
                snapshot_id=snap.id,
                line_type=ln.line_type,
                source_object_type=ln.source_object_type,
                source_object_id=UUID(ln.source_object_id),
                merchant_id=UUID(ln.merchant_id) if ln.merchant_id else None,
                owner_user_id=user_id,
                fact_date=ln.fact_date or end,
                quantity=ln.quantity if ln.quantity else None,
                amount_fen=ln.amount_fen,
                basis_json=ln.basis,
                dataset_type=DataScope.PRODUCTION.value,
                dataset_id=ds,
            ))
        self.session.flush()
        self.audit.record(
            object_type="PERFORMANCE_SNAPSHOT", object_id=snap.id,
            event_type=EVENT_UPDATE, actor_id=user_id,
            after={"period_month": period_month,
                   "net_fen": result.net_fen, "rule_version": rule.version,
                   "trigger": "VISIT_COMPLETED"},
            reason="巡访完成后自动重算当月绩效快照",
        )
        return snap

    @staticmethod
    def _shanghai_date(dt: datetime) -> date:
        # SQL 已按上海日落月；这里把 UTC 提交时间换算上海日做事实日归属
        return dt.astimezone(ZoneInfo("Asia/Shanghai")).date()

    # ================= 确认 / 复核 / 退回 =================

    def confirm_snapshot(self, actor: ActorContext, snapshot_id: UUID
                         ) -> PerformanceMonthlySnapshot:
        """区域经理确认 DRAFT→CONFIRMED。"""
        self._require_performance(actor)
        ds = self._dataset(actor)
        snap = self._load_snapshot(snapshot_id, ds)
        self.transition.transition(
            entity=snap, domain=DOMAIN_PERFORMANCE_SNAPSHOT,
            to_state=PerformanceSnapshotStatus.CONFIRMED.value,
            actor_id=actor.user_id,
            reason="区域经理确认月度绩效快照",
            extra_after={"confirmed_by_id": str(actor.user_id)},
        )
        snap.confirmed_by_id = actor.user_id
        snap.confirmed_at = datetime.now(UTC)
        self.session.flush()
        return snap

    def review_snapshot(self, actor: ActorContext, snapshot_id: UUID
                        ) -> PerformanceMonthlySnapshot:
        """财务复核 CONFIRMED→REVIEWED（A28）。"""
        self._require_finance(actor)
        ds = self._dataset(actor)
        snap = self._load_snapshot(snapshot_id, ds)
        self.transition.transition(
            entity=snap, domain=DOMAIN_PERFORMANCE_SNAPSHOT,
            to_state=PerformanceSnapshotStatus.REVIEWED.value,
            actor_id=actor.user_id,
            reason="财务复核通过月度绩效快照",
            extra_after={"reviewed_by_id": str(actor.user_id)},
        )
        snap.reviewed_by_id = actor.user_id
        snap.reviewed_at = datetime.now(UTC)
        self.session.flush()
        return snap

    def return_snapshot(self, actor: ActorContext, snapshot_id: UUID, *,
                        reason: str) -> PerformanceMonthlySnapshot:
        """财务复核时退回 CONFIRMED（REVIEWED→CONFIRMED），需原因。"""
        self._require_finance(actor)
        ds = self._dataset(actor)
        snap = self._load_snapshot(snapshot_id, ds)
        if not reason or not reason.strip():
            raise DomainError(code="PERF_RETURN_REASON_REQUIRED",
                              message="退回必须填写原因")
        self.transition.transition(
            entity=snap, domain=DOMAIN_PERFORMANCE_SNAPSHOT,
            to_state=PerformanceSnapshotStatus.CONFIRMED.value,
            actor_id=actor.user_id,
            reason=f"财务复核退回：{reason.strip()}",
        )
        snap.reviewed_by_id = None
        snap.reviewed_at = None
        self.session.flush()
        return snap

    # ================= 更正单（T35 已停用） =================

    def apply_adjustment(
        self, actor: ActorContext, *, snapshot_id: UUID, kind: str,
        amount_fen: int, approval_id: UUID, reason: str,
    ) -> PerformanceSnapshotAdjustment:
        """T35 起绩效只按「新签合同 + 活动达成」计算，不再接受扣减/调整更正单。

        保留方法只为让旧调用方拿到明确错误（而不是 AttributeError），
        对应接口入口已下线。
        """
        del actor, snapshot_id, kind, amount_fen, approval_id, reason
        raise DomainError(
            code="PERF_ADJUSTMENT_DISABLED",
            message="绩效已改为仅按新签合同与活动达成计算，不再提供扣减/调整更正单",
        )

    # ---------- 页面只读列表（T19 前端联调） ----------

    def list_rule_versions(self, actor: ActorContext, *,
                           code: str | None = None
                           ) -> list[PerformanceRuleVersion]:
        """规则版本列表：含 DRAFT/ACTIVE/SUPERSEDED，供配置/审核与历史反查。"""
        self._require_performance(actor)
        ds = self._dataset(actor)
        return self.repo.list_rule_versions(ds, code)

    def list_snapshots(self, actor: ActorContext, *,
                       period_month: str | None = None,
                       status: str | None = None,
                       user_id: UUID | None = None,
                       ) -> list[tuple[PerformanceMonthlySnapshot, User]]:
        """月度快照列表：行级范围由服务端收窄（业务员仅本人，其余限本数据集）。"""
        self._require_performance(actor)
        ds = self._dataset(actor)
        if status is not None and status not in _SNAPSHOT_STATUSES:
            raise DomainError(code="PERF_SNAPSHOT_STATUS_INVALID",
                              message="快照状态取值非法")
        if period_month:
            _month_bounds(period_month)  # 仅做格式校验
        scope_user = user_id
        if actor.role_code == RoleCode.SALES_REP.value:
            scope_user = actor.user_id
        return self.repo.list_snapshots(ds, period_month=period_month,
                                        status=status, user_id=scope_user)

    def list_user_options(self, actor: ActorContext, *,
                          role_code: str | None = None) -> list[User]:
        """绩效人员下拉：数据集内在职用户（默认不限角色，避免漏掉经理本人）。"""
        self._require_performance(actor)
        ds = self._dataset(actor)
        if role_code is not None and role_code not in _ROLE_CODES:
            raise DomainError(code="PERF_ROLE_CODE_INVALID", message="角色取值非法")
        return self.repo.list_dataset_users(ds, role_code)

    # ---------- 查询（Q108 可反查） ----------

    def preview(
        self, actor: ActorContext, *, user_id: UUID, period_month: str,
    ) -> dict:
        """只读绩效预估（T23 `performance.preview`，PRD §5.3）。

        - 若该 (user, month) 已有冻结快照 → 直接返回快照读数（与月末冻结同源）；
        - 否则按当期生效规则**只读试算**：复用 `_collect_facts` + `PerformanceEngine`，
          不落库、不写审计、不进工资（预估不等于已发放绩效）。

        行级范围只由服务端 actor 决定：业务员仅本人；经理/财务限本数据集。
        """
        self._require_performance(actor)
        ds = self._dataset(actor)
        assert_production_scope(
            scope=DataScope.PRODUCTION, where="performance_snapshot.preview")
        if (actor.role_code == RoleCode.SALES_REP.value
                and user_id != actor.user_id):
            raise PermissionDeniedError(
                code="PERF_SCOPE_FORBIDDEN",
                message="业务员只能查看本人绩效预估",
            )
        start, end = _month_bounds(period_month)
        target = self.session.get(User, user_id)
        if target is None or target.dataset_id != ds:
            raise DomainError(
                code="PERF_USER_NOT_FOUND",
                message="目标用户不存在或数据范围不一致",
            )

        existing = self.repo.find_snapshot(user_id, period_month, ds)
        if existing is not None:
            return {
                "preview": False,
                "user_id": user_id,
                "subject_name": target.display_name,
                "period_month": period_month,
                "snapshot_id": existing.id,
                "status": existing.status,
                "rule_code": existing.rule_code,
                "rule_version": existing.rule_version,
                "new_contract_fen": existing.new_contract_fen,
                "visit_bonus_fen": existing.visit_bonus_fen,
                "sales_commission_fen": existing.sales_commission_fen,
                "activity_bonus_fen": existing.activity_bonus_fen,
                "deduction_fen": existing.deduction_fen,
                "adjustment_fen": existing.adjustment_fen,
                "net_performance_fen": existing.net_performance_fen,
            }

        rule = self.get_effective_rule(end, ds)
        params = self._params(rule)
        facts = self._collect_facts(ds, user_id, start, end)
        result = self.engine.compute(
            params=params,
            new_contracts=facts["contracts"],
            activities=facts["activities"],
        )
        return {
            "preview": True,
            "user_id": user_id,
            "subject_name": target.display_name,
            "period_month": period_month,
            "snapshot_id": None,
            "status": "ESTIMATED",
            "rule_code": rule.code,
            "rule_version": rule.version,
            "new_contract_fen": result.new_contract_fen,
            "visit_bonus_fen": result.visit_bonus_fen,
            "sales_commission_fen": result.sales_commission_fen,
            "activity_bonus_fen": result.activity_bonus_fen,
            "deduction_fen": result.deduction_fen,
            "adjustment_fen": result.adjustment_fen,
            "net_performance_fen": result.net_fen,
            "lines": [
                {
                    "line_type": ln.line_type,
                    "source_object_type": ln.source_object_type,
                    "source_object_id": ln.source_object_id,
                    "amount_fen": ln.amount_fen,
                }
                for ln in result.lines
            ],
        }

    def get_snapshot_detail(self, actor: ActorContext, snapshot_id: UUID) -> dict:
        self._require_performance(actor)
        ds = self._dataset(actor)
        snap = self._load_snapshot(snapshot_id, ds)
        # 业务员只能看自己；经理/财务可看本数据集
        if (actor.role_code == RoleCode.SALES_REP.value
                and snap.user_id != actor.user_id):
            raise PermissionDeniedError(
                code="PERF_SCOPE_FORBIDDEN",
                message="业务员只能查看本人绩效快照",
            )
        return {
            "snapshot": snap,
            "lines": self.repo.list_lines(snap.id),
            "adjustments": self.repo.list_adjustments(snap.id),
        }

    # ---------- 内部 ----------

    def _load_snapshot(self, snapshot_id: UUID,
                       dataset_id: UUID) -> PerformanceMonthlySnapshot:
        snap = self.repo.get_snapshot(snapshot_id)
        if snap is None or snap.dataset_id != dataset_id:
            raise DomainError(code="PERF_SNAPSHOT_NOT_FOUND",
                              message="快照不存在或数据范围不一致")
        return snap

    def _require_finance(self, actor: ActorContext) -> None:
        # 财务角色持有 finance 能力；绩效复核必须财务（A28）
        from app.core.permissions import MODULE_FINANCE
        if not self.permissions.has_ability(actor, MODULE_FINANCE):
            raise PermissionDeniedError(
                code="PERF_REVIEW_FORBIDDEN",
                message="仅财务可复核/退回绩效快照",
            )

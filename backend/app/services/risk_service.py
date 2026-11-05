"""RiskService（T21 / PRD §4.11 RK-1～RK-7；R36、A25）。

红线：
- 风险规则、生成、升级、关闭由本服务唯一实现；工作台/排班/导出/AI 只引用其结果
  （R36），不得各自判定；
- 生成/关闭幂等：同一 (对象, 规则, 周期) 不重复生成（`risk_items` 唯一约束）；
- 关闭必须填写处理结果，风险记录不物理删除（R24）；
- 新生成的未处理风险自动进入片区经理待办；RK-1 在周排班建议中提升优先级；
- 依赖事实缺失时标注“数据不足，暂不判定”，不得默认视为无风险，也不得虚构风险。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import DomainError, PermissionDeniedError
from app.core.permissions import (
    MODULE_DASHBOARD,
    MODULE_RISK_HANDLE,
    ActorContext,
    permission_service,
)
from app.models import Role, User, UserRegionBrandAuth
from app.models.enums import RiskRuleCode, RiskStatus, RoleCode
from app.models.risk import RiskItem
from app.repositories.reporting_repository import ReportingRepository
from app.services.audit_service import EVENT_CREATE, EVENT_STATE_TRANSITION, AuditService
from app.services.business_transition_service import BusinessTransitionService
from app.services.demo_data_guard import DataScope, assert_production_scope
from app.services.report_scope import MerchantScope, resolve_merchant_scope
from app.services.state_machine import DOMAIN_RISK
from app.services.task_service import TASK_KIND_RISK_HANDLING, TaskService

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

_DEFAULT_FREQUENCY_DAYS = 7
_REQUIRED_VISIT_HISTORY = 2


@dataclass
class RiskInsufficient:
    """“数据不足，暂不判定”标注（不生成风险项，避免虚构风险）。"""

    rule_code: str
    object_type: str
    object_id: str
    reason: str

    def to_dict(self) -> dict:
        return {
            "rule_code": self.rule_code,
            "object_type": self.object_type,
            "object_id": self.object_id,
            "reason": self.reason,
            "data_incomplete": True,
        }


@dataclass
class RiskScanResult:
    items: list[RiskItem] = field(default_factory=list)
    created: list[UUID] = field(default_factory=list)
    insufficient: list[RiskInsufficient] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "items": [r.id for r in self.items],
            "created": [str(i) for i in self.created],
            "insufficient": [i.to_dict() for i in self.insufficient],
        }


class RiskService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = ReportingRepository(session)
        self.transition = BusinessTransitionService(session)
        self.audit = AuditService(session)
        self.tasks = TaskService(session)

    # ============================ 扫描 / 幂等生成 ============================

    def scan(
        self, actor: ActorContext, *, scope: DataScope | None,
        as_of: date | None = None,
    ) -> RiskScanResult:
        """评估 RK-1～RK-7 并幂等落库；新风险自动进入片区经理待办。"""
        self._require_scope(scope, "risk.scan")
        self._require_dashboard(actor)
        user = self.session.get(User, actor.user_id)
        if user is None:
            raise DomainError(code="USER_NOT_FOUND", message="登录用户不存在")
        dataset_id = user.dataset_id
        mscope = resolve_merchant_scope(self.session, actor)
        as_of = as_of or datetime.now(SHANGHAI_TZ).date()
        settings = get_settings()

        result = RiskScanResult()
        self._scan_rk1(dataset_id, mscope, as_of, settings, result)
        self._scan_rk2(dataset_id, mscope, as_of, result)
        self._scan_rk3(dataset_id, mscope, as_of, result)
        self._scan_rk4(dataset_id, mscope, as_of, settings, result)
        self._scan_rk5(dataset_id, mscope, as_of, result)
        self._scan_rk6(dataset_id, mscope, as_of, settings, result)
        self._scan_rk7(dataset_id, as_of, settings, result)
        return result

    # ------------------------------ RK-1 ------------------------------

    def _scan_rk1(self, dataset_id, mscope, as_of, settings, result) -> None:
        merchants = self.repo.merchant_rows(dataset_id, mscope)
        if not merchants:
            return
        freq_map = self.repo.grade_frequency_map(as_of)
        default_days = settings.risk_no_visit_days or _DEFAULT_FREQUENCY_DAYS
        last_visit = self.repo.last_valid_visit_at(list(merchants.keys()), dataset_id)
        period = _iso_week(as_of)
        for mid, (grade, region_id) in merchants.items():
            frequency = freq_map.get(grade or "", default_days)
            last = last_visit.get(mid)
            if last is None:
                overdue = True
            else:
                overdue = (as_of - last.astimezone(SHANGHAI_TZ).date()).days >= frequency
            if not overdue:
                continue
            self._upsert(result, dataset_id=dataset_id, object_type="MERCHANT",
                         object_id=mid, rule_code=RiskRuleCode.RK1_NO_VISIT.value,
                         period=period, region_id=region_id)

    # ------------------------------ RK-2 ------------------------------

    def _scan_rk2(self, dataset_id, mscope, as_of, result) -> None:
        merchant_region = self.repo.merchant_region_map(dataset_id)
        ids = list(merchant_region.keys())
        recent = self.repo.recent_valid_visits(ids, dataset_id, _REQUIRED_VISIT_HISTORY)
        period = _iso_week(as_of)
        for mid, visits in recent.items():
            if not _in_scope(mid, mscope, merchant_region):
                continue
            if len(visits) < _REQUIRED_VISIT_HISTORY:
                continue
            latest_two = visits[:_REQUIRED_VISIT_HISTORY]
            if all(v.status == "ABNORMAL_PENDING" for v in latest_two):
                self._upsert(result, dataset_id=dataset_id, object_type="MERCHANT",
                             object_id=mid, rule_code=RiskRuleCode.RK2_ABNORMAL_LOCATION.value,
                             period=period, region_id=merchant_region.get(mid))

    # ------------------------------ RK-3 ------------------------------

    def _scan_rk3(self, dataset_id, mscope, as_of, result) -> None:
        merchant_region = self.repo.merchant_region_map(dataset_id)
        ids = list(merchant_region.keys())
        recent = self.repo.recent_valid_visits(ids, dataset_id, 4)
        period = _iso_week(as_of)
        for mid, visits in recent.items():
            if not _in_scope(mid, mscope, merchant_region):
                continue
            checks = [v for v in visits if v.display_met is not None]
            if not checks:
                result.insufficient.append(RiskInsufficient(
                    rule_code=RiskRuleCode.RK3_DISPLAY_BELOW.value,
                    object_type="MERCHANT", object_id=str(mid),
                    reason="无有效陈列检查记录，无法判定连续两次陈列不达标",
                ))
                continue
            if len(checks) < _REQUIRED_VISIT_HISTORY:
                continue
            if all(v.display_met is False for v in checks[:_REQUIRED_VISIT_HISTORY]):
                self._upsert(result, dataset_id=dataset_id, object_type="MERCHANT",
                             object_id=mid,
                             rule_code=RiskRuleCode.RK3_DISPLAY_BELOW.value,
                             period=period, region_id=merchant_region.get(mid))

    # ------------------------------ RK-4 ------------------------------

    def _scan_rk4(self, dataset_id, mscope, as_of, settings, result) -> None:
        merchants = self.repo.merchant_rows(dataset_id, mscope)
        if not merchants:
            return
        weeks = max(settings.risk_zero_sales_weeks, 1)
        ever_confirmed = self.repo.merchants_with_any_sales_confirmation(dataset_id)
        this_monday = as_of - timedelta(days=as_of.weekday())
        period = _iso_week(as_of)
        # 最近 N 周窗口
        window_start = this_monday - timedelta(days=7 * (weeks - 1))
        with_sales = self.repo.confirmed_sales_merchant_ids_in(
            window_start, as_of, dataset_id)
        for mid, (_grade, region_id) in merchants.items():
            if mid not in ever_confirmed:
                result.insufficient.append(RiskInsufficient(
                    rule_code=RiskRuleCode.RK4_ZERO_SALES.value,
                    object_type="MERCHANT", object_id=str(mid),
                    reason="该商家无任何确认销量记录，销量未确认，暂不判定",
                ))
                continue
            if mid not in with_sales:
                self._upsert(result, dataset_id=dataset_id, object_type="MERCHANT",
                             object_id=mid, rule_code=RiskRuleCode.RK4_ZERO_SALES.value,
                             period=period, region_id=region_id)

    # ------------------------------ RK-5 ------------------------------

    def _scan_rk5(self, dataset_id, mscope, as_of, result) -> None:
        orders = self.repo.overdue_replenishments(as_of, dataset_id)
        period = as_of.isoformat()
        merchant_region = self.repo.merchant_region_map(dataset_id)
        for o in orders:
            if not _in_scope(o.merchant_id, mscope, merchant_region):
                continue
            self._upsert(result, dataset_id=dataset_id, object_type="REPLENISHMENT",
                         object_id=o.id, rule_code=RiskRuleCode.RK5_REPLENISHMENT_OVERDUE.value,
                         period=period, region_id=merchant_region.get(o.merchant_id))

    # ------------------------------ RK-6 ------------------------------

    def _scan_rk6(self, dataset_id, mscope, as_of, settings, result) -> None:
        days = settings.contract_renewal_remind_days or 30
        contracts = self.repo.contracts_expiring(as_of, days, dataset_id)
        if not contracts:
            return
        merchant_region = self.repo.merchant_region_map(dataset_id)
        scoped = [c for c in contracts if _in_scope(c.merchant_id, mscope, merchant_region)]
        pending = self.repo.open_renewal_task_contract_ids([c.id for c in scoped])
        for c in scoped:
            if c.id in pending:
                continue  # 续签评估待办已完成/已生成 → 不判风险
            self._upsert(result, dataset_id=dataset_id, object_type="CONTRACT",
                         object_id=c.id, rule_code=RiskRuleCode.RK6_CONTRACT_EXPIRING.value,
                         period=f"EXP:{c.expire_at.isoformat() if c.expire_at else 'NA'}",
                         region_id=merchant_region.get(c.merchant_id))

    # ------------------------------ RK-7 ------------------------------

    def _scan_rk7(self, dataset_id, as_of, settings, result) -> None:
        assets = self.repo.repairing_freezers(dataset_id)
        if not assets:
            return
        starts = self.repo.repair_start_at([a.id for a in assets], dataset_id)
        limit_hours = settings.risk_freezer_repair_limit_hours or 72
        now = datetime.now(UTC)
        period = as_of.isoformat()
        for a in assets:
            started = starts.get(a.id)
            if started is None:
                result.insufficient.append(RiskInsufficient(
                    rule_code=RiskRuleCode.RK7_FREEZER_REPAIR_OVERDUE.value,
                    object_type="FREEZER", object_id=str(a.id),
                    reason="缺少维修开始流水，无法计算维修时长，暂不判定",
                ))
                continue
            hours = (now - started).total_seconds() / 3600
            if hours >= limit_hours:
                self._upsert(result, dataset_id=dataset_id, object_type="FREEZER",
                             object_id=a.id,
                             rule_code=RiskRuleCode.RK7_FREEZER_REPAIR_OVERDUE.value,
                             period=period)

    # ============================ 幂等落库 ============================

    def _upsert(
        self, result: RiskScanResult, *, dataset_id: UUID, object_type: str,
        object_id: UUID, rule_code: str, period: str, region_id: UUID | None = None,
    ) -> None:
        existing = self.session.scalar(
            select(RiskItem).where(
                RiskItem.object_type == object_type,
                RiskItem.object_id == object_id,
                RiskItem.rule_code == rule_code,
                RiskItem.period == period,
            )
        )
        if existing is not None:
            existing.last_seen_at = datetime.now(UTC)
            self.session.flush()
            result.items.append(existing)
            return
        item = RiskItem(
            object_type=object_type, object_id=object_id, rule_code=rule_code,
            period=period, status=RiskStatus.OPEN.value, severity="NORMAL",
            first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
            data_incomplete=False,
            dataset_type="PRODUCTION", dataset_id=dataset_id,
        )
        self.session.add(item)
        self.session.flush()
        self.audit.record(
            object_type="RISK", object_id=item.id, event_type=EVENT_CREATE,
            actor_id=None,
            after={"rule_code": rule_code, "object_type": object_type,
                   "object_id": str(object_id), "period": period},
            reason=f"风险 {rule_code} 生成",
        )
        # 新生成未处理风险 → 片区经理待办（幂等）
        self._ensure_manager_todo(item, region_id=region_id, dataset_id=dataset_id)
        result.items.append(item)
        result.created.append(item.id)

    def _ensure_manager_todo(
        self, item: RiskItem, *, region_id: UUID | None, dataset_id: UUID,
    ) -> None:
        manager_id = self._find_manager(region_id, dataset_id)
        if manager_id is None:
            return
        self.tasks.assign_idempotent(
            title=f"处理风险 {item.rule_code}",
            assignee_id=manager_id,
            task_kind=TASK_KIND_RISK_HANDLING,
            related_object_type="RISK",
            related_object_id=item.id,
            description=f"风险项 {item.rule_code}（对象 {item.object_type}）待处置",
        )

    def _find_manager(self, region_id: UUID | None, dataset_id: UUID) -> UUID | None:
        if region_id is None:
            return None
        stmt = (
            select(User.id)
            .join(Role, Role.id == User.role_id)
            .where(
                Role.role_code == RoleCode.REGIONAL_MANAGER.value,
                User.dataset_id == dataset_id,
                User.id.in_(
                    select(UserRegionBrandAuth.user_id).where(
                        UserRegionBrandAuth.region_id == region_id
                    )
                ),
            )
            .order_by(User.id)
        )
        return self.session.scalar(stmt)

    # ============================ 查询 / 处置 ============================

    def list_risks(
        self, actor: ActorContext, *, scope: DataScope | None,
        status: str | None = None, rule_code: str | None = None,
    ) -> list[RiskItem]:
        self._require_scope(scope, "risk.list")
        self._require_dashboard(actor)
        user = self.session.get(User, actor.user_id)
        if user is None:
            raise DomainError(code="USER_NOT_FOUND", message="登录用户不存在")
        mscope = resolve_merchant_scope(self.session, actor)
        stmt = select(RiskItem).where(
            RiskItem.dataset_type == "PRODUCTION", RiskItem.dataset_id == user.dataset_id
        )
        if status is not None:
            stmt = stmt.where(RiskItem.status == status)
        if rule_code is not None:
            stmt = stmt.where(RiskItem.rule_code == rule_code)
        items = list(self.session.execute(stmt).scalars().all())
        if mscope.is_global:
            return items
        allowed_merchants = self.repo.scope_merchant_ids(user.dataset_id, mscope)
        return [
            r for r in items
            if r.object_type != "MERCHANT" or r.object_id in allowed_merchants
        ]

    def acknowledge(self, actor: ActorContext, risk_id: UUID) -> RiskItem:
        item = self._get(risk_id, actor)
        self.transition.transition(
            entity=item, domain=DOMAIN_RISK,
            to_state=RiskStatus.ACKNOWLEDGED.value, actor_id=actor.user_id,
            reason="片区经理确认风险并开始处理",
        )
        self.session.flush()
        return item

    def close(
        self, actor: ActorContext, risk_id: UUID, *, resolution: str,
    ) -> RiskItem:
        """关闭风险：必须填写处理结果；记录不物理删除。"""
        resolution = (resolution or "").strip()
        if not resolution:
            raise DomainError(
                code="RISK_RESOLUTION_REQUIRED", message="关闭风险必须填写处理结果"
            )
        item = self._get(risk_id, actor)
        # 处置入口受角色矩阵控制：无 risk.handle 的角色（业务员/厂商）即使拿到
        # 风险 id 也不能关闭（PRD §4.11 处置入口；Q185）。
        if not permission_service.has_ability(actor, MODULE_RISK_HANDLE):
            raise PermissionDeniedError(
                code="RISK_FORBIDDEN", message="当前角色无权处置风险"
            )
        before = {"status": item.status}
        self.transition.transition(
            entity=item, domain=DOMAIN_RISK, to_state=RiskStatus.CLOSED.value,
            actor_id=actor.user_id, reason=f"风险处置完成：{resolution}",
        )
        item.closed_at = datetime.now(UTC)
        item.closed_by_id = actor.user_id
        item.resolution = resolution
        self.session.flush()
        self.audit.record(
            object_type="RISK", object_id=item.id, event_type=EVENT_STATE_TRANSITION,
            actor_id=actor.user_id, before=before,
            after={"status": item.status, "resolution": resolution},
            reason=f"风险关闭：{resolution}",
        )
        return item

    # ============================ 排班优先级（RK-1）============================

    def risk_priority_provider(
        self, merchant_ids: list[UUID], dataset_id: UUID,
    ) -> list[UUID]:
        """T13 排班建议注入：返回被标记 RK-1 的商家（有序，钉首）。"""
        if not merchant_ids:
            return []
        stmt = select(RiskItem.object_id, RiskItem.first_seen_at).where(
            RiskItem.dataset_type == "PRODUCTION",
            RiskItem.dataset_id == dataset_id,
            RiskItem.rule_code == RiskRuleCode.RK1_NO_VISIT.value,
            RiskItem.object_type == "MERCHANT",
            RiskItem.status.in_([RiskStatus.OPEN.value, RiskStatus.ACKNOWLEDGED.value]),
            RiskItem.object_id.in_(merchant_ids),
        ).order_by(RiskItem.first_seen_at)
        seen: list[UUID] = []
        for mid, _ts in self.session.execute(stmt).all():
            if mid not in seen:
                seen.append(mid)
        return seen

    # ===================== T27 工作台：裁剪 + 优先级排序 =====================

    # RK-1～RK-7 工作台展示优先级（数字越小越靠前；PRD §4.11）
    RULE_PRIORITY: dict[str, int] = {
        RiskRuleCode.RK1_NO_VISIT.value: 1,
        RiskRuleCode.RK2_ABNORMAL_LOCATION.value: 2,
        RiskRuleCode.RK3_DISPLAY_BELOW.value: 3,
        RiskRuleCode.RK4_ZERO_SALES.value: 4,
        RiskRuleCode.RK5_REPLENISHMENT_OVERDUE.value: 5,
        RiskRuleCode.RK6_CONTRACT_EXPIRING.value: 6,
        RiskRuleCode.RK7_FREEZER_REPAIR_OVERDUE.value: 7,
    }

    def worklist(
        self, actor: ActorContext, *, limit: int = 100
    ) -> list[RiskItem]:
        """角色工作台风险清单（PRD §4.11）。

        - 权限裁剪：复用行级范围解析（region/brand/self），越权对象一律剔除；
        - 只返回未处置风险（OPEN，ACKNOWLEDGED 视为处理中也保留以便经理跟踪）；
        - 排序：规则优先级 RK-1→RK-7，再按首次发现时间升序（越早越紧急）。
        """
        self._require_dashboard(actor)
        user = self.session.get(User, actor.user_id)
        if user is None:
            raise DomainError(code="USER_NOT_FOUND", message="登录用户不存在")
        rows = list(self.session.scalars(
            select(RiskItem)
            .where(
                RiskItem.dataset_type == "PRODUCTION",
                RiskItem.dataset_id == user.dataset_id,
                RiskItem.status.in_([
                    RiskStatus.OPEN.value, RiskStatus.ACKNOWLEDGED.value,
                ]),
            )
        ).all())
        visible = self.filter_by_scope(actor, rows)
        return self.sort_by_priority(visible)[: max(1, min(int(limit), 500))]

    def filter_by_scope(
        self, actor: ActorContext, items: list[RiskItem]
    ) -> list[RiskItem]:
        """按 actor 行级范围裁剪风险（对象→商家→片区/授权）。

        全局角色不过滤；其余角色通过对象反查 merchant_id，再用 ReportingRepository
        的统一范围集合判定（不自行重算行级范围）。
        """
        mscope = resolve_merchant_scope(self.session, actor)
        if mscope.is_global:
            return list(items)
        user = self.session.get(User, actor.user_id)
        allowed_merchants = self.repo.scope_merchant_ids(user.dataset_id, mscope)
        merchant_of = self._merchant_ids_for(items, user.dataset_id)
        return [
            r for r in items
            if (mid := merchant_of.get(r.id)) is not None and mid in allowed_merchants
        ]

    @classmethod
    def sort_by_priority(cls, items: list[RiskItem]) -> list[RiskItem]:
        return sorted(
            items,
            key=lambda r: (
                cls.RULE_PRIORITY.get(r.rule_code, 99),
                # OPEN 排在 ACKNOWLEDGED 前；同规则内越早发现越靠前
                0 if r.status == RiskStatus.OPEN.value else 1,
                r.first_seen_at,
            ),
        )

    def _merchant_ids_for(
        self, items: list[RiskItem], dataset_id: UUID
    ) -> dict[UUID, UUID | None]:
        """{risk_item_id: merchant_id}：按对象类型批量反查所属商家。"""
        ids_by_type: dict[str, set[UUID]] = {}
        for r in items:
            ids_by_type.setdefault(r.object_type, set()).add(r.object_id)

        m_by_contract: dict[UUID, UUID] = {}
        m_by_freezer: dict[UUID, UUID] = {}
        m_by_repl: dict[UUID, UUID] = {}

        if ids_by_type.get("CONTRACT"):
            from app.models.contract import Contract

            for cid, mid in self.session.execute(
                select(Contract.id, Contract.merchant_id).where(
                    Contract.dataset_id == dataset_id,
                    Contract.id.in_(ids_by_type["CONTRACT"]),
                )
            ).all():
                m_by_contract[cid] = mid
        if ids_by_type.get("FREEZER"):
            from app.models.freezer import FreezerMovement

            current = self._freezer_current_merchant(ids_by_type["FREEZER"], dataset_id)
            # current_merchant_id 为空时回退到最近一条投放流水
            missing = [fid for fid in ids_by_type["FREEZER"] if fid not in current]
            m_by_freezer.update(current)
            if missing:
                for fid, mid in self.session.execute(
                    select(FreezerMovement.asset_id, FreezerMovement.merchant_id)
                    .where(
                        FreezerMovement.asset_id.in_(missing),
                        FreezerMovement.merchant_id.is_not(None),
                    )
                    .order_by(FreezerMovement.occurred_at.desc())
                ).all():
                    m_by_freezer.setdefault(fid, mid)
        if ids_by_type.get("REPLENISHMENT"):
            from app.models.replenishment import ReplenishmentOrder

            for rid, mid in self.session.execute(
                select(ReplenishmentOrder.id, ReplenishmentOrder.merchant_id).where(
                    ReplenishmentOrder.dataset_id == dataset_id,
                    ReplenishmentOrder.id.in_(ids_by_type["REPLENISHMENT"]),
                )
            ).all():
                m_by_repl[rid] = mid

        out: dict[UUID, UUID | None] = {}
        for r in items:
            if r.object_type == "MERCHANT":
                out[r.id] = r.object_id
            elif r.object_type == "CONTRACT":
                out[r.id] = m_by_contract.get(r.object_id)
            elif r.object_type == "FREEZER":
                out[r.id] = m_by_freezer.get(r.object_id)
            elif r.object_type == "REPLENISHMENT":
                out[r.id] = m_by_repl.get(r.object_id)
            else:
                out[r.id] = None
        return out

    def _freezer_current_merchant(
        self, freezer_ids: set[UUID], dataset_id: UUID
    ) -> dict[UUID, UUID]:
        from app.models.freezer import FreezerAsset

        rows = self.session.execute(
            select(FreezerAsset.id, FreezerAsset.current_merchant_id).where(
                FreezerAsset.dataset_id == dataset_id,
                FreezerAsset.id.in_(freezer_ids),
                FreezerAsset.current_merchant_id.is_not(None),
            )
        ).all()
        return {fid: mid for fid, mid in rows if mid is not None}

    # ============================ 内部 ============================

    def _get(self, risk_id: UUID, actor: ActorContext) -> RiskItem:
        item = self.session.get(RiskItem, risk_id)
        if item is None:
            raise DomainError(code="RISK_NOT_FOUND", message="风险项不存在")
        user = self.session.get(User, actor.user_id)
        if user is None or item.dataset_id != user.dataset_id:
            raise PermissionDeniedError(
                code="RISK_DATASET_FORBIDDEN", message="无权访问其他数据域的风险"
            )
        return item

    @staticmethod
    def _require_scope(scope: DataScope | None, where: str) -> None:
        if scope is None:
            raise DomainError(
                code="DATA_SCOPE_REQUIRED",
                message="必须显式传入 DataScope.PRODUCTION",
                details={"where": where},
            )
        if not isinstance(scope, DataScope):
            scope = DataScope.parse(scope)
        assert_production_scope(scope=scope, where=where)

    @staticmethod
    def _require_dashboard(actor: ActorContext) -> None:
        if not permission_service.has_ability(actor, MODULE_DASHBOARD):
            raise DomainError(
                code="RISK_FORBIDDEN", message="当前角色无权查看风险清单",
                details={"role": actor.role_code},
            )


def _iso_week(day: date) -> str:
    iso = day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _in_scope(merchant_id: UUID, scope: MerchantScope,
              region_map: dict[UUID, UUID]) -> bool:
    if scope is None or scope.is_global:
        return True
    if scope.merchant_ids is not None:
        return merchant_id in set(scope.merchant_list or [])
    if scope.region_ids is not None:
        return region_map.get(merchant_id) in set(scope.region_list or ())
    return True




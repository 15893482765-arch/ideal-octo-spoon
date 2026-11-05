"""角色工作台聚合服务（T27 / PRD §4.2、§1.5、§4.11）。

一次聚合返回角色首页：指标卡（MetricCard 直接渲染 MetricsService 的口径与截至时间）、
待办（任务 + 待审批 + 风险预警通知）、风险清单（§4.11）、快捷动作。

红线：
- 指标只出自 MetricsService，本服务不重算任何指标/金额（Q156）；
- 风险只出自 RiskService（裁剪 + RK-1～RK-7 优先级排序），不自行判定（Q185）；
- 通知/任务/审批只做受控读取，行级范围来自服务端 actor；
- 五类角色（业务员/经理/督导/财务/厂商）展示差异化指标、待办与快捷动作（Q154）。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.errors import DomainError
from app.core.permissions import (
    MODULE_DASHBOARD,
    ActorContext,
    RoleCode,
    permission_service,
)
from app.models.user import User
from app.services.demo_data_guard import DataScope
from app.services.metrics_service import (
    M_ACTIVE_MERCHANTS,
    M_CONFIRMED_SALES_BOXES,
    M_DISPLAY_COMPLIANCE_RATE,
    M_FREEZER_STOCK_DIFF,
    M_NEW_CONTRACTS,
    M_ON_TIME_VISIT_RATE,
    M_REPLENISHMENT_ON_TIME_RATE,
    M_WEEK_COVERAGE_RATE,
    MetricsService,
)
from app.services.notification_service import NotificationService
from app.services.risk_service import RiskService
from app.services.search_service import build_deep_link

# 每类角色首页展示的指标卡（顺序即展示顺序；全部来自 MetricsService 同一口径）
_ROLE_METRICS: dict[str, tuple[str, ...]] = {
    RoleCode.SALES_REP.value: (
        M_WEEK_COVERAGE_RATE,
        M_ON_TIME_VISIT_RATE,
        M_DISPLAY_COMPLIANCE_RATE,
        M_CONFIRMED_SALES_BOXES,
        M_REPLENISHMENT_ON_TIME_RATE,
    ),
    RoleCode.REGIONAL_MANAGER.value: (
        M_ACTIVE_MERCHANTS,
        M_NEW_CONTRACTS,
        M_WEEK_COVERAGE_RATE,
        M_ON_TIME_VISIT_RATE,
        M_DISPLAY_COMPLIANCE_RATE,
        M_CONFIRMED_SALES_BOXES,
        M_REPLENISHMENT_ON_TIME_RATE,
        M_FREEZER_STOCK_DIFF,
    ),
    RoleCode.SUPERVISOR.value: (
        M_ON_TIME_VISIT_RATE,
        M_DISPLAY_COMPLIANCE_RATE,
        M_WEEK_COVERAGE_RATE,
    ),
    # T35：工作台不再展示「月度净利润」（损益口径只在经营损益/财务总账页看）
    RoleCode.FINANCE.value: (
        M_CONFIRMED_SALES_BOXES,
        M_REPLENISHMENT_ON_TIME_RATE,
        M_FREEZER_STOCK_DIFF,
    ),
    RoleCode.SUPPLIER.value: (
        M_CONFIRMED_SALES_BOXES,
        M_REPLENISHMENT_ON_TIME_RATE,
    ),
}

# 每类角色快捷动作：label + 目标页 + 所需模块（无模块权限则不下发）
_ROLE_ACTIONS: dict[str, tuple[tuple[str, str, str], ...]] = {
    RoleCode.SALES_REP.value: (
        ("新增巡访", "/visits/new", "visit"),
        ("补货确认", "/replenishment", "replenishment"),
        ("我的排班", "/schedule", "schedule"),
        ("商家档案", "/merchants", "merchant"),
    ),
    RoleCode.REGIONAL_MANAGER.value: (
        ("周排班", "/schedule", "schedule"),
        ("补货汇总", "/replenishment", "replenishment"),
        ("风险处置", "/risks", "dashboard.view"),
        ("审批中心", "/approvals", "approval"),
        ("经营看板", "/dashboard", "dashboard.view"),
    ),
    RoleCode.SUPERVISOR.value: (
        ("跟访计划", "/supervision", "supervision"),
        ("问题整改", "/supervision/issues", "supervision"),
        ("巡访记录", "/visits", "visit"),
    ),
    RoleCode.FINANCE.value: (
        ("工资核算", "/payroll", "payroll"),
        ("采购单", "/procurement", "procurement"),
        ("经营看板", "/dashboard", "dashboard.view"),
        ("审批中心", "/approvals", "approval"),
    ),
    RoleCode.SUPPLIER.value: (
        ("待确认供货", "/procurement/confirm", "supplier.confirm_supply"),
        ("采购订单", "/procurement", "procurement"),
    ),
}

_RISK_ROLES = frozenset(
    {RoleCode.REGIONAL_MANAGER.value, RoleCode.SUPERVISOR.value}
)


class WorkbenchService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.metrics = MetricsService(session)
        self.notifications = NotificationService(session)
        self.risks = RiskService(session)

    def home(
        self, actor: ActorContext, *, period: str, risk_limit: int = 10,
        todo_limit: int = 20,
    ) -> dict:
        """聚合角色首页。一次调用返回指标、待办、风险、快捷动作、截至时间。"""
        user = self.session.get(User, actor.user_id)
        if user is None:
            raise DomainError(code="USER_NOT_FOUND", message="登录用户不存在")
        if not permission_service.has_ability(actor, MODULE_DASHBOARD):
            raise DomainError(
                code="WORKBENCH_FORBIDDEN", message="当前角色无权访问工作台"
            )

        role = actor.role_code
        metric_cards, as_of = self._metric_cards(actor, role, period)
        todos = self.notifications.list_notifications(actor, limit=todo_limit)
        unread = sum(1 for t in todos if not t.read)

        payload: dict = {
            "role": role,
            "period": period,
            "as_of": as_of,
            "metric_cards": metric_cards,
            "todos": [t.to_dict() for t in todos],
            "unread_count": unread,
            "quick_actions": self._quick_actions(actor, role),
            "deep_links": self._deep_links(period),
        }
        # 风险清单区块（§4.11）：经理/督导可见，按权限裁剪 + RK 优先级排序
        if role in _RISK_ROLES:
            payload["risk_list"] = [
                self._risk_card(r) for r in self.risks.worklist(actor, limit=risk_limit)
            ]
        else:
            payload["risk_list"] = []
        return payload

    # ----------------------------- 指标卡 -----------------------------

    def _metric_cards(
        self, actor: ActorContext, role: str, period: str
    ) -> tuple[list[dict], str | None]:
        keys = _ROLE_METRICS.get(role, ())
        if not keys:
            return [], None
        # 一次算全量（MetricsService 内有口径复用），按角色需要挑选，保证同源
        all_results = {
            r.key: r
            for r in self.metrics.all(actor, scope=DataScope.PRODUCTION, period=period)
        }
        cards: list[dict] = []
        as_of: str | None = None
        for key in keys:
            r = all_results.get(key)
            if r is None:
                continue
            as_of = r.as_of.isoformat()
            cards.append(
                {
                    "key": r.key,
                    "name": r.name,
                    "value": self._json_value(r.value),
                    "unit": r.unit,
                    "definition": r.definition,  # MetricCard 直接渲染同源口径
                    "as_of": r.as_of.isoformat(),  # 数据截至时间
                    "period": r.period,
                    "data_complete": r.data_complete,
                    "note": r.note,
                    "source_refs": list(r.source_refs),
                }
            )
        return cards, as_of

    # ----------------------------- 风险卡 -----------------------------

    def _risk_card(self, item) -> dict:
        return {
            "id": str(item.id),
            "rule_code": item.rule_code,
            "object_type": item.object_type,
            "object_id": str(item.object_id),
            "status": item.status,
            "severity": item.severity,
            "first_seen_at": item.first_seen_at.isoformat(),
            "priority": RiskService.RULE_PRIORITY.get(item.rule_code, 99),
            "deep_link": build_deep_link(
                "/risks", status=item.status, rule_code=item.rule_code,
                risk_id=item.id,
            ),
        }

    # ----------------------------- 快捷动作 / 深链 -----------------------------

    def _quick_actions(self, actor: ActorContext, role: str) -> list[dict]:
        out: list[dict] = []
        for label, path, ability in _ROLE_ACTIONS.get(role, ()):
            if permission_service.has_ability(actor, ability):
                out.append({"label": label, "path": path})
        return out

    def _deep_links(self, period: str) -> dict:
        # 深链保留筛选（Q157）：从卡片/区块跳列表页可恢复 period 等过滤
        return {
            "dashboard": build_deep_link("/dashboard", period=period),
            "risks": build_deep_link("/risks", period=period),
            "approvals": build_deep_link("/approvals"),
            "tasks": build_deep_link("/tasks"),
        }

    @staticmethod
    def _json_value(value) -> object:
        if value is None:
            return None
        if isinstance(value, Decimal):
            return str(value)
        return value

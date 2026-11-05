"""MetricsService（T21 / PRD §1.5 核心指标唯一计算处；R22、R34）。

红线：
- 指标只出自本服务；Dashboard / 导出 / AI 不得各自重算（PRD R22、§1.5）；
- 必须显式传入 `DataScope.PRODUCTION`；缺省/ALL/DEMO 一律拒绝（R34、A24）；
- 每项返回 `value + definition + as_of + filters + source_refs`（PRD §1.5、附录 B）；
- 依赖事实缺失（如未接入库存）时标注 `data_complete=False` + 说明，**不得默认当 0**；
- 金额一律整数分；比率用 Decimal，禁 float。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.errors import DomainError
from app.core.permissions import MODULE_DASHBOARD, ActorContext, get_profile
from app.models import User
from app.repositories.reporting_repository import ReportingRepository
from app.services.demo_data_guard import DataScope, assert_production_scope
from app.services.report_scope import MerchantScope, resolve_merchant_scope

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

# ---- 指标键 ----
M_ACTIVE_MERCHANTS = "ACTIVE_MERCHANTS"
M_NEW_CONTRACTS = "NEW_CONTRACTS"
M_CONTRACT_CANCEL_RATE = "CONTRACT_CANCEL_RATE"
M_WEEK_COVERAGE_RATE = "WEEK_COVERAGE_RATE"
M_ON_TIME_VISIT_RATE = "ON_TIME_VISIT_RATE"
M_DISPLAY_COMPLIANCE_RATE = "DISPLAY_COMPLIANCE_RATE"
M_CONFIRMED_SALES_BOXES = "CONFIRMED_SALES_BOXES"
M_REPLENISHMENT_ON_TIME_RATE = "REPLENISHMENT_ON_TIME_RATE"
M_FREEZER_STOCK_DIFF = "FREEZER_STOCK_DIFF"
M_MONTHLY_NET_PROFIT = "MONTHLY_NET_PROFIT"

ALL_METRICS: tuple[str, ...] = (
    M_ACTIVE_MERCHANTS, M_NEW_CONTRACTS, M_CONTRACT_CANCEL_RATE,
    M_WEEK_COVERAGE_RATE, M_ON_TIME_VISIT_RATE, M_DISPLAY_COMPLIANCE_RATE,
    M_CONFIRMED_SALES_BOXES, M_REPLENISHMENT_ON_TIME_RATE,
    M_FREEZER_STOCK_DIFF, M_MONTHLY_NET_PROFIT,
)

_METRIC_NAME = {
    M_ACTIVE_MERCHANTS: "活跃合作商家数",
    M_NEW_CONTRACTS: "新签合同数",
    M_CONTRACT_CANCEL_RATE: "取消合同率",
    M_WEEK_COVERAGE_RATE: "商家周覆盖率",
    M_ON_TIME_VISIT_RATE: "准时巡访率",
    M_DISPLAY_COMPLIANCE_RATE: "陈列达标率",
    M_CONFIRMED_SALES_BOXES: "确认销量（箱）",
    M_REPLENISHMENT_ON_TIME_RATE: "补货按时满足率",
    M_FREEZER_STOCK_DIFF: "冰柜账实差异",
    M_MONTHLY_NET_PROFIT: "月度净利润",
}

# 口径文案（PRD §1.5 表，逐字对齐；UI/导出/AI 复用同一文案）
_METRIC_DEFINITION = {
    M_ACTIVE_MERCHANTS: "截止统计日合同为“生效”，且未终止的去重商家数",
    M_NEW_CONTRACTS: "统计期内由待审变为生效的合同数，而非仅创建数",
    M_CONTRACT_CANCEL_RATE: "当期终止合同数 ÷（期初有效合同数 + 当期新生效合同数）",
    M_WEEK_COVERAGE_RATE: "当周至少存在一条有效巡访的合作商家数 ÷ 当周应巡合作商家数",
    M_ON_TIME_VISIT_RATE: "在计划窗口内完成的有效巡访数 ÷ 应完成巡访任务数",
    M_DISPLAY_COMPLIANCE_RATE: "本品牌陈列比例 ≥ 合同阈值的有效检查数 ÷ 有效陈列检查数",
    M_CONFIRMED_SALES_BOXES: "已由经理确认的各 SKU 销量箱数，不以草稿或退回记录计入",
    M_REPLENISHMENT_ON_TIME_RATE: "在承诺到货日期前完成的补货数量 ÷ 已确认补货需求数量",
    M_FREEZER_STOCK_DIFF: "实物盘点数量与系统按状态汇总数量的差值",
    M_MONTHLY_NET_PROFIT: (
        "已确认销售收入（送达口径）− 已收货进货成本（收货月）− 冰柜购置/维修 "
        "− 活动费用 − 已发放绩效及其他费用"
    ),
}

_METRIC_SOURCES = {
    M_ACTIVE_MERCHANTS: ("merchants", "contracts"),
    M_NEW_CONTRACTS: ("contracts",),
    M_CONTRACT_CANCEL_RATE: ("contracts", "contract_versions"),
    M_WEEK_COVERAGE_RATE: ("merchants", "visit_records", "contracts"),
    M_ON_TIME_VISIT_RATE: ("visit_tasks", "visit_records"),
    M_DISPLAY_COMPLIANCE_RATE: ("visit_records",),
    M_CONFIRMED_SALES_BOXES: ("sales_confirmations",),
    M_REPLENISHMENT_ON_TIME_RATE: ("replenishment_orders",),
    M_FREEZER_STOCK_DIFF: ("freezer_assets",),
    M_MONTHLY_NET_PROFIT: (
        "sales_confirmations", "procurement_orders", "freezer_movements",
        "expenses", "payroll_statements",
    ),
}

_EXPENSE_TYPE_ACTIVITY = ("ACTIVITY",)
_EXPENSE_TYPE_OTHER = (
    "PROCUREMENT", "FREEZER_PURCHASE", "FREEZER_REPAIR", "OTHER",
)


@dataclass(frozen=True)
class MetricResult:
    """单一指标结果：值 + 口径 + 数据截至 + 筛选 + 来源（PRD §1.5）。"""

    key: str
    name: str
    value: int | Decimal | None
    unit: str
    definition: str
    as_of: datetime
    period: str
    filters: dict
    data_complete: bool
    note: str | None
    source_refs: tuple[str, ...] = field(default_factory=tuple)

    @property
    def display_value(self) -> str:
        if self.value is None:
            return "数据不足，暂不判定"
        if isinstance(self.value, Decimal):
            return str(self.value)
        return str(self.value)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "value": None if self.value is None else (
                str(self.value) if isinstance(self.value, Decimal) else self.value
            ),
            "unit": self.unit,
            "definition": self.definition,
            "as_of": self.as_of.isoformat(),
            "period": self.period,
            "filters": self.filters,
            "data_complete": self.data_complete,
            "note": self.note,
            "source_refs": list(self.source_refs),
        }


def parse_period(period: str) -> tuple[date, date]:
    """解析统计周期 → (起, 止含)。

    - `2026-10` → 整月；
    - `2026-W42` → ISO 周（周一..周日）。
    """
    if not period:
        raise DomainError(code="METRIC_PERIOD_REQUIRED", message="必须指定统计周期")
    period = period.strip().upper()
    try:
        if "-W" in period:
            year_s, week_s = period.split("-W", 1)
            year, week = int(year_s), int(week_s)
            monday = date.fromisocalendar(year, week, 1)
            return monday, monday + timedelta(days=6)
        year_s, month_s = period.split("-", 1)
        year, month = int(year_s), int(month_s)
        first = date(year, month, 1)
        next_month = date(year + (month == 12), (month % 12) + 1, 1)
        return first, next_month - timedelta(days=1)
    except (ValueError, TypeError) as exc:
        raise DomainError(
            code="METRIC_PERIOD_INVALID",
            message="统计周期格式非法（应为 YYYY-MM 或 YYYY-Www）",
            details={"period": period},
        ) from exc


def _shanghai_day_bounds_utc(first: date, last: date) -> tuple[datetime, datetime]:
    """[first 00:00, last+1 00:00) 的 UTC 表示（Asia/Shanghai 业务日）。"""
    start = datetime.combine(first, time(0, 0), tzinfo=SHANGHAI_TZ).astimezone(UTC)
    end = datetime.combine(last + timedelta(days=1), time(0, 0),
                           tzinfo=SHANGHAI_TZ).astimezone(UTC)
    return start, end


def _ratio(numerator: int | Decimal, denominator: int | Decimal) -> Decimal:
    if not denominator:
        return Decimal("0.0000")
    return (Decimal(numerator) / Decimal(denominator)).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )


class MetricsService:
    """指标唯一计算处（PRD §1.5）。"""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = ReportingRepository(session)

    # ------------------------------ 入口 ------------------------------

    def compute(
        self,
        actor: ActorContext,
        key: str,
        *,
        scope: DataScope | None,
        period: str,
        filters: dict | None = None,
    ) -> MetricResult:
        """计算单个指标；scope 缺省/非 PRODUCTION 一律拒绝。"""
        self._require_scope(scope, key)
        if key not in ALL_METRICS:
            raise DomainError(
                code="METRIC_UNKNOWN", message="未知指标", details={"key": key}
            )
        self._require_dashboard(actor)
        user = self.session.get(User, actor.user_id)
        if user is None:
            raise DomainError(code="USER_NOT_FOUND", message="登录用户不存在")
        dataset_id = user.dataset_id
        mscope = resolve_merchant_scope(self.session, actor)
        start, end = parse_period(period)
        as_of = datetime.combine(end, time(23, 59, 59), tzinfo=SHANGHAI_TZ).astimezone(UTC)
        merged_filters = {
            "period": period,
            "scope": DataScope.PRODUCTION.value,
            "row_scope": get_profile(actor.role_code).row_scope,
        }
        if filters:
            merged_filters.update(filters)

        value, unit, complete, note = self._compute_value(
            key, dataset_id, mscope, start, end, period
        )
        return MetricResult(
            key=key,
            name=_METRIC_NAME[key],
            value=value,
            unit=unit,
            definition=_METRIC_DEFINITION[key],
            as_of=as_of,
            period=period,
            filters=merged_filters,
            data_complete=complete,
            note=note,
            source_refs=_METRIC_SOURCES[key],
        )

    def all(
        self, actor: ActorContext, *, scope: DataScope | None, period: str,
        filters: dict | None = None,
    ) -> list[MetricResult]:
        return [
            self.compute(actor, k, scope=scope, period=period, filters=filters)
            for k in ALL_METRICS
        ]

    # ------------------------------ 计算 ------------------------------

    def _compute_value(
        self, key: str, dataset_id: UUID, mscope: MerchantScope,
        start: date, end: date, period: str,
    ) -> tuple[int | Decimal | None, str, bool, str | None]:
        if key == M_ACTIVE_MERCHANTS:
            ids = self.repo.effective_merchant_ids_at(end, dataset_id, mscope)
            return len(ids), "家", True, None

        if key == M_NEW_CONTRACTS:
            return self.repo.new_contract_count(start, end, dataset_id, mscope), \
                "份", True, None

        if key == M_CONTRACT_CANCEL_RATE:
            begin_utc, end_utc = _shanghai_day_bounds_utc(start, end)
            opening = len(self.repo.effective_merchant_ids_at(
                start - timedelta(days=1), dataset_id, mscope))
            new_effective = self.repo.new_contract_count(start, end, dataset_id, mscope)
            terminated = len(self.repo.terminated_contract_ids_in(
                begin_utc, end_utc, dataset_id, mscope))
            denominator = opening + new_effective
            if denominator == 0:
                return Decimal("0.0000"), "RATIO", True, \
                    "期初有效合同数与当期新生效合同数均为 0，取消率按 0 处理"
            return _ratio(terminated, denominator), "RATIO", True, None

        if key == M_WEEK_COVERAGE_RATE:
            due_ids = self.repo.active_merchant_ids(dataset_id, mscope)
            start_utc, end_utc = _shanghai_day_bounds_utc(start, end)
            covered = self.repo.valid_visit_merchant_ids_in(
                start_utc, end_utc, dataset_id, mscope)
            return _ratio(len(covered & due_ids), len(due_ids)), "RATIO", True, None

        if key == M_ON_TIME_VISIT_RATE:
            start_utc, end_utc = _shanghai_day_bounds_utc(start, end)
            on_time, due = self.repo.on_time_visit_counts(
                start_utc, end_utc, dataset_id, mscope)
            return _ratio(on_time, due), "RATIO", True, None

        if key == M_DISPLAY_COMPLIANCE_RATE:
            start_utc, end_utc = _shanghai_day_bounds_utc(start, end)
            met, total = self.repo.display_check_counts(
                start_utc, end_utc, dataset_id, mscope)
            return _ratio(met, total), "RATIO", True, None

        if key == M_CONFIRMED_SALES_BOXES:
            boxes = self.repo.confirmed_sales_boxes(start, end, dataset_id, mscope)
            return boxes, "箱", True, None

        if key == M_REPLENISHMENT_ON_TIME_RATE:
            on_time, committed = self.repo.replenishment_fulfillment_counts(
                start, end, dataset_id, mscope)
            return _ratio(on_time, committed), "RATIO", True, None

        if key == M_FREEZER_STOCK_DIFF:
            # 首版未接入库存域：实物盘点与系统按状态汇总不可比（不得默认 0）
            return None, "台", False, "平台未接入库存域，实物盘点数不可得，暂不判定"

        if key == M_MONTHLY_NET_PROFIT:
            parts = self._profit_parts(dataset_id, mscope, start, end, period)
            return parts["net"], "分", True, None

        raise DomainError(code="METRIC_UNKNOWN", message="未知指标", details={"key": key})

    # ------------------------------ 净利润构成（指标与逐月汇总共用） ------------------------------

    def _profit_parts(
        self, dataset_id: UUID, mscope: MerchantScope,
        start: date, end: date, period: str,
    ) -> dict[str, int]:
        """单月损益构成（分）：收入与四项支出 + 净利润，口径同 M_MONTHLY_NET_PROFIT。"""
        begin_utc, end_utc = _shanghai_day_bounds_utc(start, end)
        sales = self.repo.confirmed_sales_amount_fen(start, end, dataset_id, mscope)
        # T33 口径修正：进货成本取 T18 采购单（收货月权责发生），
        # 旧 procurement_orders 表已废弃、无业务写入方
        procurement = self.repo.purchase_received_cost_fen(start, end, dataset_id)
        freezer = self.repo.freezer_cost_fen(begin_utc, end_utc, dataset_id)
        activity = self.repo.approved_expense_fen_by_type(
            period, dataset_id, _EXPENSE_TYPE_ACTIVITY)
        other_expense = self.repo.approved_expense_fen_by_type(
            period, dataset_id, _EXPENSE_TYPE_OTHER)
        performance = self.repo.paid_performance_fen(period, dataset_id)
        expense_total = procurement + freezer + activity + other_expense + performance
        return {
            "sales": sales,
            "procurement": procurement,
            "freezer": freezer,
            "activity": activity,
            "other_expense": other_expense,
            "performance": performance,
            "expense_total": expense_total,
            "net": sales - expense_total,
        }

    def profit_trend(
        self, actor: ActorContext, *, scope: DataScope | None, months: int = 6
    ) -> list[dict[str, int | str]]:
        """逐月收支汇总 + 累计净利润（T33：财务「每月支出和收入不断累加」）。

        从当前自然月往前取 months 个月，逐月口径与 M_MONTHLY_NET_PROFIT
        完全一致（共用 _profit_parts），累计列为逐月净利润代数和。
        """
        self._require_scope(scope, "PROFIT_TREND")
        self._require_dashboard(actor)
        user = self.session.get(User, actor.user_id)
        if user is None:
            raise DomainError(code="USER_NOT_FOUND", message="登录用户不存在")
        dataset_id = user.dataset_id
        mscope = resolve_merchant_scope(self.session, actor)

        today = datetime.now(SHANGHAI_TZ).date()
        year, month = today.year, today.month
        items: list[dict[str, int | str]] = []
        for _ in range(max(1, min(months, 24))):
            period = f"{year:04d}-{month:02d}"
            start, end = parse_period(period)
            items.append({"period": period, **self._profit_parts(
                dataset_id, mscope, start, end, period)})
            month -= 1
            if month == 0:
                month, year = 12, year - 1
        items.reverse()  # 时间正序：最早月在前
        # 累计按正序递进：上月累计 + 本月净利 = 本月累计
        cumulative = 0
        for item in items:
            cumulative += item["net"]
            item["cumulative"] = cumulative
        return items

    # ------------------------------ 校验 ------------------------------

    @staticmethod
    def _require_scope(scope: DataScope | None, where: str) -> None:
        if scope is None:
            raise DomainError(
                code="DATA_SCOPE_REQUIRED",
                message="必须显式传入 DataScope.PRODUCTION",
                details={"where": f"metrics.{where}"},
            )
        if not isinstance(scope, DataScope):
            scope = DataScope.parse(scope)
        assert_production_scope(scope=scope, where=f"metrics.{where}")

    def _require_dashboard(self, actor: ActorContext) -> None:
        from app.core.permissions import permission_service

        if not permission_service.has_ability(actor, MODULE_DASHBOARD):
            raise DomainError(
                code="METRIC_FORBIDDEN",
                message="当前角色无权查看经营指标",
                details={"role": actor.role_code},
            )

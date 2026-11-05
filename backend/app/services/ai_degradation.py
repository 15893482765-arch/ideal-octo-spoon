"""AI 逐能力降级（T26 / PRD §5.5 降级矩阵、§9.3）。

AI 故障（超时/断路器打开/结构化校验失败）时，每类能力回落到**不依赖模型**的
确定性结果，业务页面/工具永不因 AI 故障 500：

| 能力        | 降级动作（PRD §5.5）                                   |
|-------------|--------------------------------------------------------|
| 巡访记录整理 | 保留原文（不整理），提示人工核对                        |
| 周排班建议   | 返回可排序清单（风险/逾期/补货/距离），不给路线描述     |
| 经营分析     | 直接返回指标表（MetricsService），不生成解释            |
| 补货建议     | 返回已确认需求汇总，不生成采购建议                      |
| 自然语言问答 | 返回页面入口（模块链接），不自由回答                    |

这些产出全部来自受控 Service 的确定性数据，本模块不重算任何指标/金额。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.core.permissions import ActorContext

# 五类能力标识（与审计 intent 对齐）
CAP_VISIT_COMPOSE = "VISIT_COMPOSE"
CAP_SCHEDULE_SUGGEST = "SCHEDULE_SUGGEST"
CAP_BUSINESS_INSIGHT = "BUSINESS_INSIGHT"
CAP_REPLENISHMENT = "REPLENISHMENT_AGGREGATE"
CAP_QA = "QA"

DEGRADED_REASON_PREFIX = "AI_DEGRADED"


@dataclass(frozen=True)
class DegradedResult:
    """统一降级结果：payload 为确定性数据，入口/提示随能力不同。"""

    capability: str
    reason: str
    payload: dict[str, Any] = field(default_factory=dict)
    notice: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "degraded": True,
            "degraded_reason": self.reason,
            "capability": self.capability,
            "notice": self.notice,
            "data": self.payload,
        }


def _reason(code: str) -> str:
    return f"{DEGRADED_REASON_PREFIX}:{code}"


def record_ai_degraded(reason_code: str) -> None:
    """记录一次 AI 降级（PRD §9.4 监控"AI 失败率"）。旁路安全：永不抛出。"""
    try:
        from app.core.metrics import METRIC_AI_DEGRADED, registry

        registry.counter(METRIC_AI_DEGRADED, "").inc(reason=reason_code)
    except Exception:  # noqa: BLE001 - 观测旁路不影响降级主流程
        pass


# ------------------------- 巡访记录整理：保留原文 -------------------------


def degrade_visit_compose(
    *, raw_text: str, merchant_id: UUID | None = None
) -> DegradedResult:
    """AI 不可用：原文原样返回（落到草稿），不做结构化整理，提示人工核对。"""
    payload = {
        "raw_text": raw_text,
        "merchant_id": str(merchant_id) if merchant_id else None,
        "structured": False,
        "draft_suggestion": {
            "display_check": None,
            "stock_records": [],
            "abnormal_records": [],
            "visit_photos": [],
            "order_lines": [],
        },
    }
    return DegradedResult(
        capability=CAP_VISIT_COMPOSE,
        reason=_reason("VISIT_RAW_TEXT_ONLY"),
        payload=payload,
        notice="AI 暂不可用，已保留巡访原文未做整理，请人工核对后再提交。",
    )


# ------------------------- 周排班：可排序清单 -------------------------


def degrade_schedule_suggest(
    actor: ActorContext,
    *,
    session,
    region_id: UUID,
    week_start: date,
    lng: float | None = None,
    lat: float | None = None,
    limit: int = 50,
) -> DegradedResult:
    """AI 不可用：返回确定性排序清单（无路线语言描述），仍不自动发布。"""
    from app.services.schedule_service import ScheduleService

    plan = ScheduleService(session).suggest_plan(
        actor, region_id=region_id, week_start=week_start, lng=lng, lat=lat,
        limit=limit,
    )
    return DegradedResult(
        capability=CAP_SCHEDULE_SUGGEST,
        reason=_reason("SCHEDULE_SORTED_LIST_ONLY"),
        payload=plan.to_dict(),
        notice="AI 暂不可用，仅返回按风险/逾期/补货/距离排序的清单，未生成路线描述。",
    )


# ------------------------- 经营分析：指标表 -------------------------


def degrade_business_insight(
    actor: ActorContext, *, session, period: str
) -> DegradedResult:
    """AI 不可用：直接返回 MetricsService 指标表，不生成任何解释文本。"""
    from app.services.demo_data_guard import DataScope
    from app.services.metrics_service import MetricsService

    results = MetricsService(session).all(
        actor, scope=DataScope.PRODUCTION, period=period
    )
    metrics_table = [
        {
            "key": r.key,
            "name": r.name,
            "value": str(r.value) if isinstance(r.value, Decimal) else r.value,
            "unit": r.unit,
            "definition": r.definition,
            "data_complete": r.data_complete,
            "note": r.note,
        }
        for r in results
    ]
    return DegradedResult(
        capability=CAP_BUSINESS_INSIGHT,
        reason=_reason("METRICS_TABLE_ONLY"),
        payload={
            "period": period,
            "metrics": metrics_table,
            "metrics_source": "MetricsService",
        },
        notice="AI 暂不可用，已直接返回指标表，未生成经营解释。",
    )


# ------------------------- 补货：已确认需求汇总 -------------------------


def degrade_replenishment(
    actor: ActorContext,
    *,
    session,
    region_id: UUID | None = None,
    requested_from: date | None = None,
    requested_to: date | None = None,
) -> DegradedResult:
    """AI 不可用：只返回已确认补货需求汇总，不生成采购建议。"""
    from app.services.replenishment_service import ReplenishmentService

    summary = ReplenishmentService(session).summarize_confirmed(
        actor,
        region_id=region_id,
        requested_from=requested_from,
        requested_to=requested_to,
    )
    return DegradedResult(
        capability=CAP_REPLENISHMENT,
        reason=_reason("CONFIRMED_DEMAND_ONLY"),
        payload={
            "confirmed_order_count": summary["confirmed_order_count"],
            "confirmed_boxes_total": str(summary["confirmed_boxes_total"]),
            "by_sku": {k: str(v) for k, v in summary["by_sku"].items()},
        },
        notice="AI 暂不可用，仅返回已确认补货需求汇总，未生成采购建议（不会自动下单）。",
    )


# ------------------------- 自然语言问答：页面入口 -------------------------

# 模块 → 前端页面入口（与 catalog page 字段一致；降级只给入口不自由作答）
_PAGE_ENTRIES = {
    "visit": "/visits",
    "schedule": "/schedule",
    "merchant": "/merchants",
    "contract": "/contracts",
    "replenishment": "/replenishment",
    "finance": "/finance",
    "dashboard": "/dashboard",
    "procurement": "/procurement",
    "risk": "/risk",
}
_FALLBACK_PAGE = "/dashboard"


def degrade_qa(*, question: str) -> DegradedResult:
    """AI 不可用：不自由回答，只返回最相关的页面入口（关键词命中，确定性）。"""
    q = (question or "").lower()
    hit = next((page for mod, page in _PAGE_ENTRIES.items() if mod in q), None)
    # 中文关键词兜底（不依赖模型）
    if hit is None:
        zh = {
            "巡": "/visits", "排班": "/schedule", "店": "/merchants",
            "合同": "/contracts", "补货": "/replenishment", "利润": "/finance",
            "看板": "/dashboard", "采购": "/procurement", "风险": "/risk",
        }
        hit = next((p for kw, p in zh.items() if kw in question or ""), _FALLBACK_PAGE)
    return DegradedResult(
        capability=CAP_QA,
        reason=_reason("PAGE_ENTRY_ONLY"),
        payload={"page": hit, "answered": False},
        notice="AI 暂不可用，未自由作答，请从该页面入口查看受控数据。",
    )

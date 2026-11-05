"""经营解释服务（T25 / PRD §5.2、§9.1、§1.5、§5.5）。

事实与观点分离（PRD §5.2、§9.1）：
- **事实层（evidence）**：指标值、口径、来源、环比差异、数据完整性全部唯一来自
  `MetricsService`；本服务**不重算任何指标/金额**，只做相邻两期 `MetricResult`
  的确定性差分（减法是呈现层对比，不产生新业务口径）；
- **观点层（narrative）**：由 AI Provider 仅组织语言；所有原因一律放在
  `assumptions` 并标注「假设」，不得当业务事实落库/进指标；
- 数据不完整或 Provider 返回非结构化输出时，按 §5.5 降级：仍返回确定性证据，
  `narrative=None` + `degraded_reason`，不虚构解释。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.core.permissions import MODULE_DASHBOARD, ActorContext, PermissionService
from app.schemas.ai import Narrative
from app.services.ai_provider import AIOutputInvalid, get_ai_provider
from app.services.demo_data_guard import DataScope
from app.services.metrics_service import (
    MetricResult,
    MetricsService,
    parse_period,
)

# 相邻期偏移（月）：本期 → 上一可比期
_PREV_PERIOD_MONTHS = 1

# 环比变化方向判定阈值（比率类按 0，金额/计数按相对变化 0）；相等即 FLAT。
_DIR_UP = "UP"
_DIR_DOWN = "DOWN"
_DIR_FLAT = "FLAT"


def previous_period(period: str) -> str:
    """返回相邻可比期：``2026-10``→``2026-09``；周期暂不支持经营解释环比。"""
    start, _ = parse_period(period)
    if "-W" in period.upper():
        # 经营解释首版只承诺月度环比；周期显式报错而非静默乱算。
        raise ValueError("经营解释首版仅支持月度周期（YYYY-MM）")
    y, m = start.year, start.month
    pm = m - _PREV_PERIOD_MONTHS
    if pm == 0:
        y, pm = y - 1, 12
    return f"{y:04d}-{pm:02d}"


@dataclass(frozen=True)
class MetricEvidence:
    """单个指标的确定性证据：本期值、上期值、差异（只做呈现层减法）。"""

    key: str
    name: str
    unit: str
    definition: str
    current: int | Decimal | None
    previous: int | Decimal | None
    delta: int | Decimal | None
    direction: str
    data_complete: bool
    note: str | None
    source_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        def _s(v):
            if v is None:
                return None
            return str(v) if isinstance(v, Decimal) else v

        return {
            "key": self.key,
            "name": self.name,
            "unit": self.unit,
            "definition": self.definition,
            "current": _s(self.current),
            "previous": _s(self.previous),
            "delta": _s(self.delta),
            "direction": self.direction,
            "data_complete": self.data_complete,
            "note": self.note,
            "source_refs": list(self.source_refs),
        }


@dataclass(frozen=True)
class BusinessInsight:
    """经营解释：确定性证据 + 可选的语言层（降级时 narrative 为 None）。"""

    period: str
    previous_period: str
    evidence: list[MetricEvidence]
    narrative: Narrative | None
    degraded: bool = False
    degraded_reason: str | None = None
    # 事实来源声明：供 UI/导出/AI 上下文统一引用，禁止重算。
    metrics_source: str = "MetricsService"
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "previous_period": self.previous_period,
            "metrics_source": self.metrics_source,
            "evidence": [e.to_dict() for e in self.evidence],
            "narrative": self.narrative.model_dump() if self.narrative else None,
            "narrative_is_assumption_only": True,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
        }


def _diff_direction(cur: int | Decimal, prev: int | Decimal) -> str:
    if cur > prev:
        return _DIR_UP
    if cur < prev:
        return _DIR_DOWN
    return _DIR_FLAT


class BusinessInsightService:
    """组装经营解释：指标同源 + 确定性差异分解 + LLM 仅组织语言。"""

    def __init__(self, session) -> None:
        self.session = session
        self.metrics = MetricsService(session)
        self.permissions = PermissionService()

    def explain(
        self,
        actor: ActorContext,
        *,
        period: str,
        provider=None,
        force_invalid: bool = False,
    ) -> BusinessInsight:
        """生成某月经营解释。

        :param provider: 可注入 AIProvider（测试用）；默认按 AI_PROVIDER 装配；
        :param force_invalid: 测试用：强制 Provider 返回非结构化输出以验证降级。
        """
        self.permissions.require_ability(actor, MODULE_DASHBOARD)
        prev = previous_period(period)

        current_results = self.metrics.all(
            actor, scope=DataScope.PRODUCTION, period=period
        )
        previous_results = {
            r.key: r
            for r in self.metrics.all(actor, scope=DataScope.PRODUCTION, period=prev)
        }

        evidence = [self._evidence(r, previous_results.get(r.key)) for r in current_results]
        data_complete = all(e.data_complete for e in evidence)

        # 语言层只接收「已算好的事实摘要」，Provider 拿不到会话/数据库，无法重算。
        headline_metric = next(
            (e for e in evidence if e.key == "MONTHLY_NET_PROFIT"), None
        )
        facts = {
            "period": period,
            "direction": headline_metric.direction if headline_metric else _DIR_FLAT,
            "data_complete": data_complete,
            "metric_count": len(evidence),
        }

        narrative, degraded, reason = self._safe_narrate(
            provider, facts, force_invalid, data_complete
        )
        return BusinessInsight(
            period=period,
            previous_period=prev,
            evidence=evidence,
            narrative=narrative,
            degraded=degraded,
            degraded_reason=reason,
        )

    @staticmethod
    def _evidence(cur: MetricResult, prev: MetricResult | None) -> MetricEvidence:
        """确定性差异分解：仅对相邻两期 MetricResult 做减法，不产生新口径。"""
        if cur.value is None or prev is None or prev.value is None:
            # 数据缺失（如未接入库存域）：只标注不判定，绝不默认 0 后再算差异。
            return MetricEvidence(
                key=cur.key, name=cur.name, unit=cur.unit, definition=cur.definition,
                current=cur.value,
                previous=prev.value if prev else None,
                delta=None, direction=_DIR_FLAT,
                data_complete=cur.data_complete, note=cur.note,
                source_refs=cur.source_refs,
            )
        delta = cur.value - prev.value
        return MetricEvidence(
            key=cur.key, name=cur.name, unit=cur.unit, definition=cur.definition,
            current=cur.value, previous=prev.value, delta=delta,
            direction=_diff_direction(cur.value, prev.value),
            data_complete=cur.data_complete, note=cur.note,
            source_refs=cur.source_refs,
        )

    @staticmethod
    def _safe_narrate(
        provider, facts: dict[str, Any], force_invalid: bool, data_complete: bool
    ) -> tuple[Narrative | None, bool, str | None]:
        """语言层失败/非结构化 → 降级为只给确定性证据（PRD §5.5）。"""
        if not data_complete:
            # 数据不完整本就不做确定性结论；语言层仍尝试，但强制低置信。
            pass
        try:
            p = provider or get_ai_provider()
            narrative = p.narrate(
                kind="business_insight", facts=facts, force_invalid=force_invalid
            )
            if not isinstance(narrative, Narrative):
                raise AIOutputInvalid("Provider 未返回 Narrative 结构化对象")
            if not data_complete:
                # 红线：事实不完整时，模型不得给出高置信结论。
                narrative = narrative.model_copy(update={"confidence": "LOW"})
            return narrative, False, None
        except AIOutputInvalid as exc:
            return None, True, f"AI_NARRATIVE_DEGRADED:{exc}"

"""AI Provider 抽象与内置确定性 Mock（T25 / PRD §5.2、§9.1、§5.5）。

边界（红线）：
- 本模块**只负责语言生成与结构化校验**，不读业务库、不算指标/金额；
- 业务事实（指标、差异、在途、库存状态、口径）由调用方 Service 计算后作为
  `facts` 传入；Provider 只把事实「组织成语言」，原因一律放进 ``assumptions``
  并由上层标注为「假设」，绝不把模型猜测当业务事实（PRD §9.1）；
- 结构化输出必须通过 Pydantic（`Narrative`）校验；模型返回非结构化 / 字段缺失 /
  类型不符时抛 `AIOutputInvalid`，由上层按 §5.5 降级为确定性结果。

Mock 可复现（Q146）：
- 同一 (kind, facts, seed) 用内容派生的稳定散列选模板，**不调用随机数 / 时间**，
  因此同输入永远同输出，跨进程一致；
- `force_invalid=True` 用于 Q147：返回无法通过校验的非结构化文本，验证降级路径。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from app.core.config import get_settings
from app.schemas.ai import Narrative


class AIOutputInvalid(Exception):
    """Provider 输出无法通过结构化（Pydantic）校验：上层必须降级，不得采纳。"""


@runtime_checkable
class AIProvider(Protocol):
    """语言生成 Provider 协议（首版仅 mock；真实 LLM 后置实现同一契约）。"""

    name: str

    def narrate(
        self,
        *,
        kind: str,
        facts: Mapping[str, Any],
        force_invalid: bool = False,
    ) -> Narrative:
        """把确定性事实组织为结构化叙述。

        :param kind: 叙述类型（如 ``business_insight``），决定 mock 模板族；
        :param facts: 已由 Service 算好的确定性事实（Provider 不重算）；
        :param force_invalid: 测试用：返回非结构化输出以触发降级（Q147）。
        """
        ...


def _stable_digest(kind: str, facts: Mapping[str, Any]) -> str:
    """对 (kind, facts) 求稳定散列：键排序、默认 JSON，保证跨进程一致。"""
    payload = json.dumps(
        {"kind": kind, "facts": dict(facts)},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class MockAIProvider:
    """确定性、可复现的内置 Provider（不访问网络/模型/时钟/随机源）。"""

    name = "mock"

    def __init__(self, seed: int | None = None) -> None:
        self._seed = seed

    def _resolved_seed(self) -> int:
        if self._seed is not None:
            return self._seed
        return get_settings().ai_mock_seed

    def narrate(
        self,
        *,
        kind: str,
        facts: Mapping[str, Any],
        force_invalid: bool = False,
    ) -> Narrative:
        if force_invalid:
            # Q147：模拟真实模型偶发返回一段无法解析为结构化对象的自由文本。
            raise AIOutputInvalid("模型返回非结构化文本，未通过结构化输出校验")

        # 内容 + 种子共同决定模板选择：同输入同种子 → 同输出（可复现）。
        digest = _stable_digest(kind, facts)
        templates = _TEMPLATES_BY_KIND.get(kind, _TEMPLATES)
        selector = (int(digest[:8], 16) ^ self._resolved_seed()) % len(templates)
        tpl = templates[selector]

        direction = str(facts.get("direction", "FLAT"))
        headline = tpl["headline"].format_map(
            _SafeFormat(
                direction=_DIRECTION_TEXT.get(direction, "持平"),
                count=str(facts.get("count", 0)),
            )
        )
        summary = tpl["summary"]
        assumptions = list(_ASSUMPTIONS_BY_KIND.get(kind, _BUSINESS_ASSUMPTIONS))
        # 置信度不允许模型自封：事实数据不完整时强制 LOW（由 facts 显式驱动）。
        confidence = "LOW" if facts.get("data_complete") is False else "MEDIUM"

        return Narrative(
            headline=headline,
            summary=summary,
            assumptions=assumptions,
            confidence=confidence,
        )


_DIRECTION_TEXT = {
    "UP": "环比上行",
    "DOWN": "环比下行",
    "FLAT": "环比基本持平",
}

# 仅语言模板：不含任何数值（数值一律来自确定性证据 facts/evidence）。
_TEMPLATES: tuple[dict[str, str], ...] = (
    {
        "headline": "本期经营指标{direction}（以下数值均来自系统确定性口径）",
        "summary": (
            "已基于系统确认的经营指标给出差异分解。数值与口径均来自受控指标服务，"
            "本结论未引入任何模型估算；下方可能原因仅为假设，需业务确认后方可采信。"
        ),
    },
    {
        "headline": "经营表现{direction}，详见确定性指标证据",
        "summary": (
            "指标证据、差异与来源均由系统计算，AI 仅负责组织语言。"
            "任何原因性解释均标注为假设，不作为业务事实、不进入指标与考核。"
        ),
    },
    {
        "headline": "本期经营走势{direction}（证据优先，观点分离）",
        "summary": (
            "请优先查看随附的确定性指标与差异分解；自然语言部分不替代证据。"
            "假设性原因仅供排查方向，确认前不得据此下单、发布或调整考核。"
        ),
    },
)


class _SafeFormat(dict):
    """模板占位符容错：模板未使用的键不会因缺失而抛 KeyError。"""

    def __missing__(self, key: str) -> str:
        return ""


# 经营解释类叙述的假设条目（模型推断，必须由上层标注为假设）。
_BUSINESS_ASSUMPTIONS: tuple[str, ...] = (
    "假设该变化主要受终端动销与补货节奏影响（模型推断，未经业务确认）",
    "假设在途与活动投入按既定计划兑现（模型推断，未经业务确认）",
)

# 对话类叙述的边界声明：数据全部来自受控只读工具，模型不参与取数。
_ASSISTANT_ASSUMPTIONS: tuple[str, ...] = (
    "答复中的业务数据来自受控只读工具的真实返回（非模型推断）",
    "权限与数据范围由服务端注入，模型不得自行扩大（服务端强制）",
)

_ASSUMPTIONS_BY_KIND: dict[str, tuple[str, ...]] = {
    "assistant_chat": _ASSISTANT_ASSUMPTIONS,
}

# 对话语言模板：只描述「查询已完成」与边界，不产出任何业务数值。
_CHAT_TEMPLATES: tuple[dict[str, str], ...] = (
    {
        "headline": "已在你可见的数据范围内完成查询（{count} 条）",
        "summary": (
            "以下内容来自受控只读工具的实际返回，AI 仅负责组织语言；"
            "未扩大你的权限、片区或品牌范围。"
        ),
    },
    {
        "headline": "查询完成：结果均为系统确定性口径（{count} 条）",
        "summary": (
            "AI 不参与取数与口径计算，只汇总工具返回；如需下钻请查看证据条目。"
        ),
    },
    {
        "headline": "已在授权范围内汇总查询结果（{count} 条）",
        "summary": (
            "结果为空表示当前角色/片区/品牌范围内没有匹配记录，不代表全局不存在。"
        ),
    },
)

# 按 kind 选择模板族：未登记的 kind 回落经营模板（保持既有输出不变）。
_TEMPLATES_BY_KIND: dict[str, tuple[dict[str, str], ...]] = {
    "assistant_chat": _CHAT_TEMPLATES,
}


def get_ai_provider() -> AIProvider:
    """按配置返回 Provider；首版仅支持 mock（测试默认 mock）。"""
    settings = get_settings()
    if settings.ai_provider != "mock":
        # 预留真实 Provider 的装配点；未接入前显式失败而非静默回落到未知实现。
        raise AIOutputInvalid(f"未实现的 AI_PROVIDER: {settings.ai_provider}")
    return MockAIProvider(seed=settings.ai_mock_seed)

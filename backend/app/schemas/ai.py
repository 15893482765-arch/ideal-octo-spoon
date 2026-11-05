"""AI 建议 / 经营解释的结构化输出模型（T25 / PRD §5.2、§9.1）。

红线（PRD §9.1、§5.5）：
- LLM 只组织语言、提出**标注为假设**的原因，不得产出任何业务事实数值；
- 所有事实证据（指标、差异、口径、来源）由确定性 Service 计算后外挂在
  `BusinessInsight` 上，不在本 Pydantic 模型内、也不由模型填写；
- 结构化输出必须通过 Pydantic 校验；校验失败即按「非结构化输出」降级
  （返回确定性证据，不采纳模型文本）。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator

# 单条原因/叙述最大长度：防止模型输出膨胀（PRD：短答复优先）
_MAX_HEADLINE = 200
_MAX_SUMMARY = 1000
_MAX_REASON = 300


class Narrative(BaseModel):
    """LLM 产出的纯语言层（不含任何事实数值，事实只来自确定性证据）。

    对 mock 与未来真实 LLM 同一契约：
    - ``confidence`` 必须是 LOW/MEDIUM/HIGH 三档（不允许自由文本）；
    - ``assumptions`` 中每条都是「假设」，服务端对其统一加假设标注，
      且业务层不得把任何假设当事实落库或进指标。
    """

    headline: Annotated[str, Field(min_length=1, max_length=_MAX_HEADLINE)]
    summary: Annotated[str, Field(min_length=1, max_length=_MAX_SUMMARY)]
    assumptions: list[Annotated[str, Field(min_length=1, max_length=_MAX_REASON)]] = (
        Field(default_factory=list)
    )
    confidence: Literal["LOW", "MEDIUM", "HIGH"] = "LOW"

    @field_validator("headline", "summary")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("叙述文本不能为空")
        return v

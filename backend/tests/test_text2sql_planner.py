"""PlanGenerator 单测：FakeLlm + mini catalog，不依赖数据库与真实模型。"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest

from app.retrieval.context import SemanticContext
from app.semantic.catalog import SemanticCatalog
from app.services.ai_llm import LlmReply
from app.text2sql import (
    CannotAnswerError,
    PlanGenerationError,
    PlanGenerator,
)
from tests.test_text2sql_plan import _mini_catalog

# ---------------------------------------------------------------------------
# 桩
# ---------------------------------------------------------------------------


class _StubLlm:
    """按队列依次吐出回复，记录每次调用的 messages。"""

    model = "stub-planner"

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[list[dict[str, Any]]] = []

    def complete(self, *, messages, tools=None, temperature=None, max_tokens=800):
        self.calls.append(messages)
        if not self._replies:
            raise AssertionError("StubLlm 回复队列耗尽")
        return LlmReply(content=self._replies.pop(0), tool_calls=())


def _context() -> SemanticContext:
    return SemanticContext(
        text="[命中指标]\nconfirmed_boxes_total（确认箱数）\n[相关维度]\nregion_name（区域）",
        entities=["sales_confirmation"],
        metrics=["confirmed_boxes_total"],
        dimensions=["region_name"],
    )


def _plan_json(**overrides) -> str:
    payload = {
        "question": "上个月销量",
        "entity": "sales_confirmation",
        "metrics": [{"name": "confirmed_boxes_total"}],
        "time_range": {"kind": "relative", "relative": "last_month"},
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


@pytest.fixture(scope="module")
def catalog() -> SemanticCatalog:
    return _mini_catalog()


# ---------------------------------------------------------------------------
# 解析路径
# ---------------------------------------------------------------------------


class TestParse:
    def test_plain_json(self, catalog: SemanticCatalog) -> None:
        llm = _StubLlm([_plan_json()])
        generation = PlanGenerator(llm, catalog).generate("上个月销量", _context())
        assert generation.rounds == 1
        assert generation.resolved.primary.name == "sales_confirmation"
        assert len(llm.calls) == 1

    def test_json_in_fence(self, catalog: SemanticCatalog) -> None:
        llm = _StubLlm([f"```json\n{_plan_json()}\n```"])
        generation = PlanGenerator(llm, catalog).generate("上个月销量", _context())
        assert generation.rounds == 1

    def test_json_with_chatter(self, catalog: SemanticCatalog) -> None:
        """模型前后带话：截取首个 { 到末个 }。"""
        llm = _StubLlm([f"好的，规划如下：\n{_plan_json()}\n请查收"])
        generation = PlanGenerator(llm, catalog).generate("上个月销量", _context())
        assert generation.rounds == 1

    def test_cannot_answer_no_retry(self, catalog: SemanticCatalog) -> None:
        llm = _StubLlm(['{"cannot_answer": "语义层没有天气预报指标"}'])
        with pytest.raises(CannotAnswerError, match="天气预报"):
            PlanGenerator(llm, catalog).generate("明天天气如何", _context())
        assert len(llm.calls) == 1  # 主动声明不重试


# ---------------------------------------------------------------------------
# 回喂重试
# ---------------------------------------------------------------------------


class TestRetry:
    def test_invalid_json_then_fixed(self, catalog: SemanticCatalog) -> None:
        llm = _StubLlm(["这不是 JSON", _plan_json()])
        generation = PlanGenerator(llm, catalog).generate("上个月销量", _context())
        assert generation.rounds == 2
        # 第二轮 messages：回喂含错误说明 + 模型可见自己上轮输出
        second = llm.calls[1]
        assert second[-1]["role"] == "user"
        assert "不是合法 JSON" in second[-1]["content"]
        assert second[-2] == {"role": "assistant", "content": "这不是 JSON"}

    def test_semantic_issues_fed_back(self, catalog: SemanticCatalog) -> None:
        bad = _plan_json(metrics=[{"name": "nope_metric"}])
        llm = _StubLlm([bad, _plan_json()])
        generation = PlanGenerator(llm, catalog).generate("上个月销量", _context())
        assert generation.rounds == 2
        feedback = llm.calls[1][-1]["content"]
        assert "UNKNOWN_METRIC" in feedback
        assert "metrics[0].name" in feedback

    def test_exhausted_raises(self, catalog: SemanticCatalog) -> None:
        llm = _StubLlm(["垃圾输出", "还是垃圾"])
        with pytest.raises(PlanGenerationError) as err:
            PlanGenerator(llm, catalog, max_rounds=2).generate(
                "上个月销量", _context()
            )
        assert err.value.rounds == 2
        assert err.value.feedback

    def test_single_round_mode(self, catalog: SemanticCatalog) -> None:
        llm = _StubLlm(["垃圾输出"])
        with pytest.raises(PlanGenerationError):
            PlanGenerator(llm, catalog, max_rounds=1).generate(
                "上个月销量", _context()
            )
        assert len(llm.calls) == 1


# ---------------------------------------------------------------------------
# prompt 构造
# ---------------------------------------------------------------------------


class TestPrompt:
    def test_system_prompt_contents(self, catalog: SemanticCatalog) -> None:
        llm = _StubLlm([_plan_json()])
        PlanGenerator(llm, catalog).generate(
            "上个月销量", _context(), today=date(2026, 10, 22)
        )
        system = llm.calls[0][0]
        assert system["role"] == "system"
        content = system["content"]
        # 语义上下文、schema 关键字段、时间锚点、SQL 禁令都在
        assert "confirmed_boxes_total" in content
        assert '"relative"' in content and "time_range" in content
        assert "2026-10-22" in content
        assert "绝不输出 SQL" in content
        assert "cannot_answer" in content

    def test_history_threaded(self, catalog: SemanticCatalog) -> None:
        llm = _StubLlm([_plan_json()])
        history = [
            {"role": "user", "content": "华东上个月销量"},
            {"role": "assistant", "content": "华东 9 月销量 15 箱。"},
            {"role": "system", "content": "不该透传"},
        ]
        PlanGenerator(llm, catalog).generate(
            "那华南呢", _context(), history=history
        )
        messages = llm.calls[0]
        roles = [m["role"] for m in messages]
        assert roles == ["system", "user", "assistant", "user"]
        assert messages[-1]["content"].startswith("用户问题：那华南呢")

    def test_max_tokens_passed(self, catalog: SemanticCatalog) -> None:
        class _ProbeLlm(_StubLlm):
            seen_max_tokens: int | None = None

            def complete(self, *, messages, tools=None, temperature=None, max_tokens=800):
                self.seen_max_tokens = max_tokens
                return super().complete(
                    messages=messages,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )

        llm = _ProbeLlm([_plan_json()])
        PlanGenerator(llm, catalog, max_tokens=900).generate("上个月销量", _context())
        assert llm.seen_max_tokens == 900

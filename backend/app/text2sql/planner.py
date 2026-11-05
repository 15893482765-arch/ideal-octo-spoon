"""LLM 查询规划器：问题 + 语义上下文 -> QueryPlan。

LLM 在本模块的唯一职责是把自然语言映射为结构化 QueryPlan JSON——
它永远不写 SQL（编译器负责）、永远看不到数据范围（QueryContext 注入）。
规划失败的三类反馈（JSON 解析 / Pydantic 结构 / 语义层校验 issues）
原样回喂，给模型最多 ``max_rounds`` 轮修正机会；模型也可主动输出
``{"cannot_answer": ...}`` 声明无法规划（语义层覆盖不到的问题不该硬猜）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from pydantic import ValidationError as PydanticValidationError

from app.retrieval.context import SemanticContext
from app.semantic.catalog import SemanticCatalog

from .errors import PlanValidationError, Text2SqlError
from .plan import QueryPlan, plan_json_schema
from .validator import ResolvedPlan, validate_plan

DEFAULT_MAX_ROUNDS = 2
DEFAULT_MAX_TOKENS = 1500

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_MAX_FEEDBACK_OUTPUT = 800


class LlmClient(Protocol):
    """规划器依赖的最小 LLM 接口（与 OpenAiCompatClient / FakeLlm 兼容）。"""

    model: str

    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: Any = None,
        temperature: float | None = None,
        max_tokens: int = 800,
    ) -> Any: ...


class CannotAnswerError(Text2SqlError):
    """模型主动声明：当前语义层无法回答该问题（不回喂重试）。"""

    def __init__(self, reason: str) -> None:
        super().__init__(f"模型声明无法规划：{reason}")
        self.reason = reason


class PlanGenerationError(Text2SqlError):
    """重试耗尽仍未得到合法计划；feedback 为最后一轮回喂内容（不含 SQL/取值）。"""

    def __init__(self, feedback: str, *, rounds: int) -> None:
        super().__init__(f"查询规划失败（{rounds} 轮）：{feedback}")
        self.feedback = feedback
        self.rounds = rounds


class _Feedback(Exception):
    """内部：本轮输出不可用，携带回喂文本进入下一轮。"""


@dataclass(frozen=True)
class PlanGeneration:
    resolved: ResolvedPlan
    rounds: int  # 实际消耗轮次（含修正轮）


class PlanGenerator:
    def __init__(
        self,
        llm: LlmClient,
        catalog: SemanticCatalog,
        *,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        if max_rounds < 1:
            raise ValueError("max_rounds 至少为 1")
        self._llm = llm
        self._catalog = catalog
        self._max_rounds = max_rounds
        self._max_tokens = max_tokens

    def generate(
        self,
        question: str,
        context: SemanticContext,
        *,
        history: list[dict[str, str]] | tuple[dict[str, str], ...] = (),
        today: date | None = None,
    ) -> PlanGeneration:
        """生成并校验查询计划。

        :raises CannotAnswerError: 模型声明语义层覆盖不了该问题
        :raises PlanGenerationError: 重试耗尽仍不合法
        :raises LlmUnavailableError: 模型调用失败（向上传递，由网关降级）
        """
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": _system_prompt(context, today=today or date.today()),
            },
            *(
                {"role": turn["role"], "content": str(turn.get("content") or "")}
                for turn in history
                if turn.get("role") in {"user", "assistant"}
            ),
            {
                "role": "user",
                "content": f"用户问题：{question}\n请只输出 QueryPlan JSON。",
            },
        ]
        feedback: str | None = None
        for round_no in range(1, self._max_rounds + 1):
            if feedback is not None:
                messages.append({"role": "user", "content": feedback})
            reply = self._llm.complete(messages=messages, max_tokens=self._max_tokens)
            content = (getattr(reply, "content", None) or "").strip()
            try:
                plan = _parse_plan(content)
            except _Feedback as exc:
                messages.append(
                    {"role": "assistant", "content": content or "（空输出）"}
                )
                feedback = _retry_message(str(exc))
                continue
            try:
                resolved = validate_plan(plan, self._catalog)
            except PlanValidationError as exc:
                messages.append({"role": "assistant", "content": content})
                feedback = _retry_message(_issues_text(exc))
                continue
            return PlanGeneration(resolved=resolved, rounds=round_no)
        raise PlanGenerationError(
            (feedback or "未获得模型输出")[:_MAX_FEEDBACK_OUTPUT],
            rounds=self._max_rounds,
        )


# ---------------------------------------------------------------------------
# prompt 与解析
# ---------------------------------------------------------------------------


def _system_prompt(context: SemanticContext, *, today: date) -> str:
    schema = json.dumps(
        plan_json_schema(), ensure_ascii=False, separators=(",", ":")
    )
    example = json.dumps(
        {
            "question": "上个月各区域的确认销量和销售额分别是多少",
            "entity": "<实体名>",
            "metrics": [{"name": "<指标名>"}, {"name": "<另一指标名>"}],
            "dimensions": [{"name": "<维度名>"}],
            "time_range": {"kind": "relative", "relative": "last_month"},
            "order_by": [{"key": "<指标名>", "direction": "desc"}],
            "limit": 50,
        },
        ensure_ascii=False,
    )
    return f"""你是企业数据分析助手的查询规划器。把用户问题映射为一个 QueryPlan JSON，
交给确定性编译器生成 SQL。你绝不输出 SQL，也不臆造任何口径。

# 硬性规则
1. 只输出一个 JSON 对象（可包在 ```json 围栏中），禁止输出解释文字
2. entity / metrics / dimensions 必须使用下方「语义上下文」中出现的名字，不得编造
3. filters 只能引用实体（含其可关联实体，用 entity 字段指定）的列；敏感列禁止过滤；
   值填业务原值（如商家名、等级），不要写 SQL 表达式或函数
4. 时间：今天是 {today.isoformat()}。
   「上个月」→ {{"kind":"relative","relative":"last_month"}}；「本月」→ this_month；
   「近7天」→ last_7d；具体某月 → {{"kind":"month","month":"YYYY-MM"}}；
   明确起止日期 → {{"kind":"absolute","start":"YYYY-MM-DD","end":"YYYY-MM-DD"}}（end 为包含当日）；
   「最近 N 个月」→ 用 absolute，start 取 N 个月前的当月 1 号、end 取今天
5. 按月趋势/对比（每月、近几个月、月度）必须选带 month 分桶的维度：
   sales_month（月度销量）、finance_month（月度收支）、contract_month（月度合同）；
   按天用 sales_day。不要用原始日期列分组，否则每天一行、无法按月对比
6. 单位换算由消费侧处理，规划时原样选择指标即可
7. 若问题无法用「语义上下文」中的指标/维度/实体回答，输出
   {{"cannot_answer": "一句话原因"}}，不要硬猜

# QueryPlan 结构（JSON Schema）
{schema}

# 语义上下文
{context.text}

# 输出示例（字段名照此，取值替换为上文真实名字）
{example}"""


def _parse_plan(content: str) -> QueryPlan:
    text = _extract_json(content)
    if not text:
        raise _Feedback("输出为空或不包含 JSON 对象")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _Feedback(
            f"输出不是合法 JSON（第 {exc.lineno} 行：{exc.msg}）"
        ) from exc
    if isinstance(payload, dict) and "cannot_answer" in payload:
        reason = str(payload.get("cannot_answer") or "未说明原因").strip()[:200]
        raise CannotAnswerError(reason or "未说明原因")
    try:
        return QueryPlan.model_validate(payload)
    except PydanticValidationError as exc:
        raise _Feedback(
            "JSON 不符合 QueryPlan 结构：" + _summarize_pydantic(exc)
        ) from exc


def _extract_json(content: str) -> str:
    """剥代码围栏；无围栏时截取首个 { 到末个 }（容错模型前后带话）。"""
    text = content.strip()
    if not text:
        return ""
    match = _FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        return text[start : end + 1]
    return text


def _summarize_pydantic(exc: PydanticValidationError, *, limit: int = 3) -> str:
    parts = []
    for error in exc.errors()[:limit]:
        loc = ".".join(str(item) for item in error["loc"]) or "(root)"
        parts.append(f"{loc}: {error['msg']}")
    suffix = " 等" if len(exc.errors()) > limit else ""
    return "；".join(parts) + suffix


def _issues_text(exc: PlanValidationError) -> str:
    lines = [f"- [{issue.code}] {issue.path}: {issue.message}" for issue in exc.issues]
    return "语义校验未通过：\n" + "\n".join(lines)


def _retry_message(detail: str) -> str:
    return (
        "上一次的输出未通过校验。请根据下面的问题修正后，重新输出完整的 "
        "QueryPlan JSON（仍然不要输出 SQL）：\n" + detail
    )

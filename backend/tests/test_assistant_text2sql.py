"""助手 x Text2SQL 端到端：真实 PG 执行 + FakeLlm 双段（规划/总结）。

链路：chat() -> 信号路由 -> AiGateway(规划) -> 只读执行 -> LLM 总结。
规划阶段检索器/执行器 monkeypatch 为测试替身（Qdrant/embedding 不进
测试路径；执行器绑测试库 engine）。
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.models.ai_interaction import INTENT_TEXT2SQL, AiInteractionLog
from app.models.enums import RoleCode
from app.retrieval.retriever import RetrievalResult, ScoredDocument
from app.schemas.assistant import AssistantChatView
from app.services.ai_llm import LlmReply
from app.services.assistant_chat_service import AssistantChatService
from app.text2sql import ReadOnlyExecutor
from tests.helpers_text2sql import purge_sales_world, seed_sales_world
from tests.test_t18_procurement import _internal_actor

# ---------------------------------------------------------------------------
# 桩与造数
# ---------------------------------------------------------------------------


class _ScriptedLlm:
    """按脚本吐回复；记录每次调用 messages。tool_calls 恒为空。"""

    model = "scripted-t2sql"

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[list[dict]] = []

    def complete(self, *, messages, tools=None, temperature=None, max_tokens=800):
        self.calls.append(messages)
        if not self._replies:
            raise AssertionError("脚本化 LLM 回复队列耗尽")
        return LlmReply(content=self._replies.pop(0), tool_calls=())


class _FakeRetriever:
    """固定命中：销量/销售额指标 + 区域维度 + 销售确认实体。"""

    def search(self, query: str, **_: object) -> RetrievalResult:
        docs = [
            ScoredDocument(
                doc_id="metric:confirmed_sales_boxes",
                kind="metric",
                entity_name="sales_confirmation",
                name="confirmed_sales_boxes",
                text="指标 confirmed_sales_boxes：确认销量（箱）",
                score=0.9,
            ),
            ScoredDocument(
                doc_id="metric:confirmed_sales_amount",
                kind="metric",
                entity_name="sales_confirmation",
                name="confirmed_sales_amount",
                text="指标 confirmed_sales_amount：确认销售额（分）",
                score=0.88,
            ),
            ScoredDocument(
                doc_id="dimension:region_name",
                kind="dimension",
                entity_name="region",
                name="region_name",
                text="维度 region_name：区域名称",
                score=0.85,
            ),
            ScoredDocument(
                doc_id="entity:sales_confirmation",
                kind="entity",
                entity_name="sales_confirmation",
                name="sales_confirmation",
                text="实体 sales_confirmation：销售确认单",
                score=0.8,
            ),
        ]
        return RetrievalResult(
            query=query,
            entities=[d for d in docs if d.kind == "entity"],
            columns=[d for d in docs if d.kind != "entity"],
        )


def _sales_plan_json() -> str:
    return json.dumps(
        {
            "question": "上个月各区域的确认销量和销售额",
            "entity": "sales_confirmation",
            "metrics": [
                {"name": "confirmed_sales_boxes"},
                {"name": "confirmed_sales_amount"},
            ],
            "dimensions": [{"name": "region_name"}],
            "time_range": {"kind": "relative", "relative": "last_month"},
            "order_by": [{"key": "confirmed_sales_amount", "direction": "desc"}],
        },
        ensure_ascii=False,
    )


@pytest.fixture
def world(engine: Engine, monkeypatch: pytest.MonkeyPatch):
    """造数 commit + 执行器/检索器替换为测试替身 + LLM 桩挂载。"""
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session: Session = factory()
    try:
        w = seed_sales_world(session)
        manager, actor = _internal_actor(
            session, w, RoleCode.SYSTEM_ADMIN, "t2sql-admin"
        )
        w["manager"] = manager
        w["actor"] = actor
        session.commit()

        settings = get_settings()
        executor = ReadOnlyExecutor(
            engine, statement_timeout_ms=settings.text2sql_statement_timeout_ms
        )
        monkeypatch.setattr(
            "app.text2sql.pipeline.get_text2sql_executor", lambda: executor
        )
        monkeypatch.setattr(
            "app.text2sql.pipeline.get_text2sql_retriever", lambda: _FakeRetriever()
        )
        w["session"] = session
        yield w
    finally:
        session.close()
        purge_sales_world(engine, w)


def _service(world: dict) -> AssistantChatService:
    return AssistantChatService(world["session"])


def _use_llm(monkeypatch: pytest.MonkeyPatch, llm: _ScriptedLlm) -> None:
    monkeypatch.setattr(
        "app.services.assistant_chat_service.get_chat_llm", lambda: llm
    )


def _text2sql_audits(session: Session) -> list[AiInteractionLog]:
    return list(
        session.scalars(
            select(AiInteractionLog).where(
                AiInteractionLog.intent == INTENT_TEXT2SQL
            )
        ).all()
    )


def test_text2sql_prompt_states_money_unit() -> None:
    """金额单位必须写进提示词：语义层金额存「分」，不说明会被模型当「元」复述。"""
    from app.services.assistant_chat_service import _answer_messages, _ToolOutcome

    messages = _answer_messages(
        "最近几天收支",
        "text2sql.query",
        _ToolOutcome(status="OK", raw={"columns": [], "rows": []}),
        [],
    )
    prompt = messages[-1]["content"]
    assert "分" in prompt and "元" in prompt and "100" in prompt
    # null 指标不得被解释成权限/同步问题
    assert "权限不足" in prompt


def test_scope_blocked_compile_failure_reply_is_actionable() -> None:
    """行级范围套不上实体（如总账）时，回话要讲清口径原因，而不是暗示是问法问题。"""
    from app.services.assistant_chat_service import _compile_failure_reply
    from app.text2sql import PlanCompileError

    blocked = _compile_failure_reply(
        PlanCompileError(
            "行级范围 region_column 无法应用到手实体 'finance_transaction'"
        )
    )
    assert "总账" in blocked
    assert "财务或系统管理员" in blocked
    assert "换个问法" not in blocked

    other = _compile_failure_reply(PlanCompileError("列不存在"))
    assert "换个问法" in other


# ---------------------------------------------------------------------------
# 端到端
# ---------------------------------------------------------------------------


class TestText2SqlChat:
    def test_sales_by_region_end_to_end(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llm = _ScriptedLlm(
            [
                _sales_plan_json(),
                "9月确认销量：华东15箱（1500元），华南7箱（约777.77元）。",
            ]
        )
        _use_llm(monkeypatch, llm)
        data = _service(world).chat(
            world["actor"], "上个月各区域的确认销量和销售额分别是多少"
        )
        assert data["tool"] == "text2sql.query"
        assert data["tool_status"] == "OK"
        assert data["reply"].startswith("9月确认销量")
        assert data["audit_id"]
        # 聚合行不是业务对象（无 object_id）：records 必须为空，
        # 否则 AssistantRecordRefView 校验失败会让整条请求 500（历史缺陷）。
        assert data["records"] == []
        # 回归：按 API 层的响应模型走一遍，确保路由不会抛 ValidationError
        view = AssistantChatView(**data)
        assert view.chart is not None
        assert view.records == []
        # A2 图表与 reply 同源：分类轴取聚合后的区域，数值取同一批 rows
        chart = data["chart"]
        assert chart is not None
        assert chart["chart_type"] == "bar"
        assert chart["unit"] == "box"
        assert set(chart["categories"]) == {"华东", "华南"}
        assert chart["row_count"] == 2
        # 规划 + 总结两次 LLM 调用
        assert len(llm.calls) == 2

        world["session"].flush()
        audits = _text2sql_audits(world["session"])
        assert len(audits) == 2  # 规划（网关）+ 总结（最终回答）各记一条
        assert all(audit.status == "OK" for audit in audits)

    def test_no_records_returns_no_chart(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """查无数据：不出图，并在回复末尾明确声明（不依赖模型自觉）。"""
        plan = json.dumps(
            {
                "question": "不存在的片区的销量",
                "entity": "sales_confirmation",
                "metrics": [{"name": "confirmed_sales_boxes"}],
                "dimensions": [{"name": "region_name"}],
                "filters": [
                    {
                        "entity": "region",
                        "column": "name",
                        "op": "eq",
                        "value": "不存在的片区",
                    }
                ],
                "time_range": {"kind": "relative", "relative": "last_month"},
            },
            ensure_ascii=False,
        )
        llm = _ScriptedLlm([plan, "未查到相关销量数据。"])
        _use_llm(monkeypatch, llm)

        data = _service(world).chat(world["actor"], "上个月不存在的片区的销量汇总")

        assert data["tool_status"] == "NO_RECORDS"
        assert data["chart"] is None
        assert data["reply"].endswith("未查询到数据，无法生成图表。")

    def test_region_role_row_scope(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """区域经理只见到本片区的聚合（范围服务端注入，模型不可指定）。"""
        _, region_actor = _internal_actor(
            world["session"], world, RoleCode.REGIONAL_MANAGER, "t2sql-region"
        )
        world["session"].commit()
        llm = _ScriptedLlm([_sales_plan_json(), "华东 9 月销量 15 箱。"])
        _use_llm(monkeypatch, llm)
        data = _service(world).chat(region_actor, "上个月各区域销量汇总")
        assert data["tool"] == "text2sql.query"
        # 聚合行不入 records（无主键可指向）；范围生效证据看图表分类轴
        assert data["records"] == []
        assert data["chart"] is not None
        assert data["chart"]["categories"] == ["华东"]

    def test_cannot_answer_falls_back(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """模型声明无法规划 -> 回退通用自由回答路径（行为同接入前）。"""
        llm = _ScriptedLlm(
            [
                '{"cannot_answer": "语义层没有市场份额指标"}',
                "（规划输出：不调用工具）",
                "这个问题我暂时无法从系统数据回答。",
            ]
        )
        _use_llm(monkeypatch, llm)
        data = _service(world).chat(world["actor"], "我们的市场份额占比是多少")
        assert data["tool"] is None  # 未走 text2sql，也未命中 MCP 工具
        assert data["reply"] == "这个问题我暂时无法从系统数据回答。"
        # text2sql 规划 1 次 + 回退后「规划 + 最终回答」各 1 次
        assert len(llm.calls) == 3

    def test_chitchat_not_routed_to_text2sql(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        llm = _ScriptedLlm(
            [
                "（规划输出：不调用工具）",
                "你好，我是经销商助手，可以帮你查数据。",
            ]
        )
        _use_llm(monkeypatch, llm)
        data = _service(world).chat(world["actor"], "你好，在吗")
        assert data["tool"] is None
        # 通用自由回答路径：规划 + 最终回答各 1 次，无 text2sql 规划调用
        assert len(llm.calls) == 2
        assert data["reply"] == "你好，我是经销商助手，可以帮你查数据。"

    def test_scope_not_applicable_message(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """区域角色查工资（无区域路径的实体）-> 明确的范围话术，不泄漏。"""
        _, region_actor = _internal_actor(
            world["session"], world, RoleCode.REGIONAL_MANAGER, "t2sql-region2"
        )
        world["session"].commit()
        payroll_plan = json.dumps(
            {
                "question": "上个月应发人力成本总额",
                "entity": "payroll_statement",
                "metrics": [{"name": "gross_pay_amount"}],
                "time_range": {"kind": "month", "month": "2026-09"},
            },
            ensure_ascii=False,
        )
        llm = _ScriptedLlm([payroll_plan])
        _use_llm(monkeypatch, llm)
        # 问句避开「工资」规则词（否则被 payroll.list 规则截胡，轮不到 Text2SQL）
        data = _service(world).chat(region_actor, "统计一下上个月的人力成本总额")
        assert data["tool"] == "text2sql.query"
        assert data["tool_status"] == "ERROR"
        assert "查询范围" in data["reply"]
        assert len(llm.calls) == 1  # 执行失败不再走总结 LLM

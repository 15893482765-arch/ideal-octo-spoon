"""AI 助手对话（PRD §5.1 / §5.4）：意图路由、越权降级、语言层与审计。

覆盖：
- 只读工具取数：经理问商家 → merchant.search，条目来自真实返回；
- 越权不报错：业务员问补货汇总 → NO_PERMISSION，不泄露任何数据；
- 未识别问题：不调用工具但要留审计（不能「静默无记录」）；
- 语言层：mock 可复现、不含数值、置信度受 facts 驱动；
- 审计：每次对话在同事务写 ai_interaction_logs，记录允许工具与角色范围。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.test_t09_contract import _merchant, _user, _world
from tests.test_t18_procurement import _internal_actor

from app.models.ai_interaction import INTENT_QA, INTENT_REPLENISHMENT, AiInteractionLog
from app.models.enums import RoleCode
from app.schemas.ai import Narrative
from app.schemas.assistant import AssistantChatRequest
from app.services import assistant_chat_service as svc
from app.services.ai_provider import MockAIProvider
from app.services.assistant_chat_service import AssistantChatService
from mcp.envelope import RESULT_NO_PERMISSION, RESULT_NO_RECORDS


def _manager(session: Session, w: dict, tag: str = "asst-mgr"):
    return _internal_actor(session, w, RoleCode.REGIONAL_MANAGER, tag)


def _audit_rows(session: Session) -> list[AiInteractionLog]:
    return list(session.scalars(select(AiInteractionLog)).all())


# ============================== 只读查询 ==============================


def test_manager_merchant_query_returns_tool_rows(session: Session) -> None:
    """经理问商家：走 merchant.search，回话与证据均来自工具真实返回。"""
    w = _world(session)
    _mgr_user, actor = _manager(session, w)
    for i in range(3):
        _merchant(session, w, name=f"助手测试店-{i}")
    session.flush()

    data = AssistantChatService(session).chat(actor, "查一下有哪些商家")

    assert data["tool"] == "merchant.search"
    assert data["tool_status"] == "OK"
    assert data["item_count"] >= 3
    assert data["highlights"], "应返回可读的条目摘要"
    assert "商家" in data["reply"]
    # 语言层通过 Pydantic 校验；数据完整性由工具状态驱动
    narrative = data["narrative"]
    assert narrative is not None
    assert narrative["confidence"] in {"LOW", "MEDIUM"}
    assert data["degraded"] is False
    assert data["audit_id"]
    assert data["intent"] == INTENT_QA


def test_sales_rep_replenishment_query_is_denied_without_leak(session: Session) -> None:
    """业务员问补货汇总：工具角色门禁生效，返回 NO_PERMISSION 而不是 4xx/500。"""
    w = _world(session)
    _user_obj, actor = _internal_actor(session, w, RoleCode.SALES_REP, "asst-rep")

    data = AssistantChatService(session).chat(actor, "本月补货需求汇总")

    assert data["tool"] == "replenishment.aggregate"
    assert data["tool_status"] == RESULT_NO_PERMISSION
    assert data["highlights"] == []
    assert data["item_count"] == 0
    assert "无权" in data["reply"]
    assert data["intent"] == INTENT_REPLENISHMENT
    # 越权拒绝同样留审计（allowed_tools 记录被拒工具，便于回溯）
    rows = _audit_rows(session)
    assert len(rows) == 1
    assert rows[0].status in {"OK", "DEGRADED"}


def test_unrecognized_question_returns_guide_and_still_audits(session: Session) -> None:
    """无法归入任何只读工具时不猜、不调工具，但仍写一条审计。"""
    w = _world(session)
    _mgr_user, actor = _manager(session, w, "asst-mgr-guide")

    data = AssistantChatService(session).chat(actor, "你好呀")

    assert data["tool"] is None
    assert data["tool_status"] == "NOT_CALLED"
    assert data["item_count"] == 0
    assert "商家" in data["reply"]
    assert data["suggestions"]
    rows = _audit_rows(session)
    assert len(rows) == 1
    assert rows[0].allowed_tools == ""


def test_performance_query_defaults_to_current_period(session: Session) -> None:
    """绩效问句未写周期时按当前月查询（不猜未来月），无快照即 NO_RECORDS。"""
    w = _world(session)
    _mgr_user, actor = _manager(session, w, "asst-mgr-perf")

    data = AssistantChatService(session).chat(actor, "我的绩效预估怎么样")

    assert data["tool"] == "performance.preview"
    assert data["tool_status"] == RESULT_NO_RECORDS
    rows = _audit_rows(session)
    assert len(rows) == 1
    assert rows[0].params_summary_json.get("tool") == "performance.preview"


_PERF_RECORD = {
    "preview": True,
    "period_month": "2026-10",
    "status": "ESTIMATED",
    "new_contract_fen": 120000,
    "visit_bonus_fen": 30000,
    "sales_commission_fen": 250000,
    "activity_bonus_fen": 0,
    "deduction_fen": 5000,
    "adjustment_fen": 0,
    "net_performance_fen": 395000,
}


def test_performance_detail_reply_lists_amounts() -> None:
    """绩效单记录的确定性回话：六项金额构成 + 预计结算，全部来自工具记录。"""
    from app.services.assistant_chat_service import (
        _compose_reply,
        _Rule,
        _ToolOutcome,
    )

    outcome = _ToolOutcome(status="OK", item_count=1, raw={"record": dict(_PERF_RECORD)})
    reply = _compose_reply(_Rule("performance.preview", "绩效预估", ()), outcome)

    assert "2026-10" in reply
    assert "新签合同：1200.00 元" in reply
    assert "巡访奖励：300.00 元" in reply
    assert "销售提成：2500.00 元" in reply
    assert "扣款：-50.00 元" in reply
    assert "预计结算：3950.00 元" in reply
    assert "不代表已发放" in reply


def test_followup_inherits_last_rule_when_llm_down(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """降级期追问继承：「看它每项金额」无关键词，继承上轮绩效规则重跑工具，
    且参数来源是上一轮用户消息（含「10月」周期），不是追问本身。"""
    from app.services.assistant_chat_service import _ToolOutcome

    w = _world(session)
    _mgr_user, actor = _manager(session, w, "asst-mgr-followup")
    calls: list[tuple[str, str]] = []

    def fake_call_tool(self, actor, rule, message, *, session_id=None):  # noqa: ANN001, ANN202
        calls.append((rule.tool, message))
        return _ToolOutcome(status="OK", item_count=1, raw={"record": dict(_PERF_RECORD)})

    monkeypatch.setattr(AssistantChatService, "_call_tool", fake_call_tool)
    svc = AssistantChatService(session)

    first = svc.chat(actor, "查看我10月的绩效预估")
    assert first["tool"] == "performance.preview"

    second = svc.chat(
        actor,
        "我要看它具体的每项金额，以及最后结算金额",
        conversation_id=first["conversation_id"],
    )
    assert second["tool"] == "performance.preview"
    assert "预计结算" in second["reply"]
    assert calls[-1] == ("performance.preview", "查看我10月的绩效预估")


def test_followup_without_history_still_guides(session: Session) -> None:
    """无历史的追问不猜：仍回能力引导，不凭空继承。"""
    w = _world(session)
    _mgr_user, actor = _manager(session, w, "asst-mgr-followup-none")

    data = AssistantChatService(session).chat(actor, "我要看它具体的每项金额")

    assert data["tool"] is None
    assert data["tool_status"] == "NOT_CALLED"
    assert "商家" in data["reply"]


def test_performance_subject_resolved_by_name(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """点名查成员绩效：解析在册成员姓名 → subject_user_id，明细头部带姓名。"""
    from app.services.assistant_chat_service import _ToolOutcome

    w = _world(session)
    _mgr_user, actor = _manager(session, w, "asst-mgr-perf-zw")
    zhang = _user(session, w["ds"], RoleCode.SALES_REP, "张伟")
    captured: dict = {}

    def fake_run_tool(self, actor, tool, arguments, *, session_id=None):  # noqa: ANN001, ANN202
        captured.update(arguments)
        record = dict(_PERF_RECORD, subject_name="张伟")
        return _ToolOutcome(status="OK", item_count=1, raw={"record": record})

    monkeypatch.setattr(AssistantChatService, "_run_tool", fake_run_tool)

    data = AssistantChatService(session).chat(actor, "查看张伟10月的绩效明细")

    assert data["tool"] == "performance.preview"
    assert captured.get("subject_user_id") == str(zhang.id)
    assert "张伟 2026-10 绩效明细" in data["reply"]


def test_performance_unknown_member_refused(session: Session) -> None:
    """点名的成员不在册：明确回复未找到，绝不静默查本人（防张冠李戴）。"""
    w = _world(session)
    _mgr_user, actor = _manager(session, w, "asst-mgr-perf-none")

    data = AssistantChatService(session).chat(actor, "查看李雷10月的绩效明细")

    assert data["tool"] == "performance.preview"
    assert data["tool_status"] == "NO_SUCH_MEMBER"
    assert "未找到成员「李雷」" in data["reply"]
    assert data["item_count"] == 0


def test_write_intent_refused_without_tool_call(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """祈使式改期指令（把X的寻访改到周五）：明确拒绝，绝不调任何工具。"""
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("写指令不得触发工具调用")

    monkeypatch.setattr(svc.AssistantChatService, "_run_tool", forbidden)
    w = _world(session)
    _mgr_user, actor = _manager(session, w, "asst-mgr-write-refuse")

    data = AssistantChatService(session).chat(actor, "把新诚小卖部的寻访改到周五")

    assert data["tool"] is None
    assert data["tool_status"] == "NOT_CALLED"
    assert "只能查询和整理草稿" in data["reply"]


@pytest.mark.parametrize(
    ("message", "expected_tool"),
    [
        ("有哪些取消的合同", "contract.search"),  # 「取消」出现在列举问句中是筛选条件
        ("合同怎么修改", "knowledge.search"),  # 「怎么改」是制度咨询
        ("新城小卖部的寻访时间是周几", "visit.list_due"),  # 「寻访」同义词命中
    ],
)
def test_question_form_not_intercepted_by_write_guard(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    expected_tool: str,
) -> None:
    w = _world(session)
    _mgr_user, actor = _manager(session, w, f"asst-mgr-q-{expected_tool}")
    captured: list[dict[str, Any]] = []

    def fake_run(
        self: object,
        actor: object,
        tool: str,
        arguments: dict[str, Any],
        *,
        session_id: object = None,
    ) -> svc._ToolOutcome:
        captured.append(arguments)
        return svc._ToolOutcome(status="OK", item_count=0)

    monkeypatch.setattr(svc.AssistantChatService, "_run_tool", fake_run)

    data = AssistantChatService(session).chat(actor, message)

    assert data["tool"] == expected_tool
    assert captured, "查询问句必须真正调用工具"


def test_visit_synonym_keyword_clean(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """「寻访时间是周几」剥离后 keyword 只剩商家名。"""
    w = _world(session)
    _mgr_user, actor = _manager(session, w, "asst-mgr-visit-kw")
    captured: list[dict[str, Any]] = []

    def fake_run(
        self: object,
        actor: object,
        tool: str,
        arguments: dict[str, Any],
        *,
        session_id: object = None,
    ) -> svc._ToolOutcome:
        captured.append(arguments)
        return svc._ToolOutcome(status="OK", item_count=1)

    monkeypatch.setattr(svc.AssistantChatService, "_run_tool", fake_run)

    AssistantChatService(session).chat(actor, "新城小卖部的寻访时间是周几")

    assert captured and captured[0].get("keyword") == "新城小卖部"


def test_visit_reply_states_weekday_directly() -> None:
    """问「周几巡访」必须直接答日期星期，不能只报「查到 N 条」。"""
    outcome = svc._ToolOutcome(
        status="OK",
        item_count=1,
        raw={
            "items": [
                {
                    "id": "t1",
                    "merchant_name": "新城小卖部",
                    "planned_date": "2026-10-23",  # 周五
                    "status": "PENDING",
                }
            ]
        },
    )
    rule = svc._Rule("visit.list_due", "待巡访任务", ())

    reply = svc._compose_reply(rule, outcome)

    assert "新城小卖部" in reply
    assert "周五" in reply
    assert "10月23日" in reply
    assert "待巡访" in reply
    assert "PENDING" not in reply


def test_visit_highlight_carries_date_and_weekday() -> None:
    rows = [
        {
            "merchant_name": "汇佳便利店",
            "planned_date": "2026-10-21",  # 周三
            "status": "IN_PROGRESS",
        }
    ]
    (highlight,) = svc._summarize_rows(rows)
    assert "汇佳便利店" in highlight
    assert "周三" in highlight
    assert "巡访中" in highlight


def test_payroll_recent_half_year_routes_with_months(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """「近半年工资单」：路由 payroll.list + 窗口 months=6，回话直接给金额。"""
    from app.services.assistant_chat_service import _ToolOutcome

    w = _world(session)
    _mgr_user, actor = _manager(session, w, "asst-mgr-payroll")
    captured: dict = {}

    rows = [
        {
            "statement_no": "PAY-1",
            "user_name": "高建民",
            "period_month": "2026-09",
            "base_salary_fen": 480000,
            "gross_pay_fen": 530000,
            "corrected_pay_fen": None,
            "status": "PAID",
        }
    ]

    def fake_run(self, actor, tool, arguments, *, session_id=None):  # noqa: ANN001, ANN202
        captured.update({"tool": tool, **arguments})
        return _ToolOutcome(
            status="OK",
            item_count=len(rows),
            raw={"items": rows, "scope": "self", "count": len(rows)},
        )

    monkeypatch.setattr(svc.AssistantChatService, "_run_tool", fake_run)

    data = AssistantChatService(session).chat(actor, "查询我近半年的工资单")

    assert captured["tool"] == "payroll.list"
    assert captured["months"] == 6
    assert "period" not in captured
    assert "应发 5300.00 元" in data["reply"]
    assert "已发放" in data["reply"]
    assert "PAID" not in data["reply"]
    # 规则路径也出图：单月只有一个点 → 柱状，金额元化
    chart = data["chart"]
    assert chart is not None
    assert chart["chart_type"] == "bar"
    assert chart["unit"] == "money_yuan"
    assert chart["categories"] == ["2026-09"]
    assert chart["series"][0]["data"] == [5300.0]


def test_payroll_explicit_month_uses_period(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """点名月份（「9月工资」）按单月查询，不走近半年窗口。"""
    from app.services.assistant_chat_service import _ToolOutcome

    w = _world(session)
    _mgr_user, actor = _manager(session, w, "asst-mgr-payroll-m")
    captured: dict = {}

    def fake_run(self, actor, tool, arguments, *, session_id=None):  # noqa: ANN001, ANN202
        captured.update(arguments)
        return _ToolOutcome(status="OK", item_count=0, raw={"items": []})

    monkeypatch.setattr(svc.AssistantChatService, "_run_tool", fake_run)

    AssistantChatService(session).chat(actor, "查一下我9月的工资")

    assert captured == {"period": f"{date.today().year}-09"}


@pytest.mark.parametrize(
    "question",
    [
        "最近一周的沁爽饮用水销量如何？",
        "最近一周售出的不同饮料销量占比如何？",
        "最近5个月的收入和支出",
        "最近六个月的利润如何",
        "最近六个月签订合同的数量对比",
    ],
)
def test_analysis_questions_reach_text2sql(question: str) -> None:
    """示例分析问句必须能进 Text2SQL（含「如何」结尾与「合同」关键词）。"""
    assert svc._should_try_text2sql(question) is True


def test_contract_question_yields_contract_search_rule() -> None:
    """「合同」命中检索规则，但问的是数量对比 → 让路给 Text2SQL。"""
    question = "最近六个月签订合同的数量对比"
    rule = svc._match_rule(question, svc._RULES)

    assert rule is not None and rule.tool == "contract.search"
    assert svc._analytic_overrides_rule(question, rule) is True


def test_knowledge_questions_keep_knowledge_route() -> None:
    """制度/流程类问题仍归知识库，不被「统计」等字眼抢走。"""
    question = "补货流程和制度规定是什么"
    rule = svc._match_rule(question, svc._RULES)

    assert rule is not None and rule.tool == "knowledge.search"
    assert svc._analytic_overrides_rule(question, rule) is False
    assert svc._should_try_text2sql(question) is False


def test_payroll_question_keeps_rule_route() -> None:
    """工资问句走 payroll.list 规则（该路径自己出图），不被 Text2SQL 抢走。"""
    question = "张伟最近五个月工资发放情况如何"
    rule = svc._match_rule(question, svc._RULES)

    assert rule is not None and rule.tool == "payroll.list"
    assert svc._analytic_overrides_rule(question, rule) is False


def test_payroll_reply_lists_amounts() -> None:
    """问「工资多少」必须直接答金额：逐月应发 + 中文状态。"""
    outcome = svc._ToolOutcome(
        status="OK",
        item_count=2,
        raw={
            "scope": "self",
            "items": [
                {
                    "period_month": "2026-09",
                    "gross_pay_fen": 530000,
                    "corrected_pay_fen": None,
                    "status": "PAID",
                },
                {
                    "period_month": "2026-08",
                    "gross_pay_fen": 480000,
                    "corrected_pay_fen": 481000,
                    "status": "APPROVED",
                },
            ],
        },
    )
    reply = svc._compose_reply(svc._Rule("payroll.list", "工资单", ()), outcome)

    assert "近半年工资单（不含本月）" in reply
    assert "2026-09：应发 5300.00 元，已发放" in reply
    # 有冲正时按冲正后金额回答
    assert "2026-08：应发 4810.00 元，已复核，待发放" in reply
    assert "PAID" not in reply and "APPROVED" not in reply


def test_payroll_highlight_carries_amount() -> None:
    rows = [
        {
            "user_name": "张伟",
            "period_month": "2026-09",
            "gross_pay_fen": 530000,
            "corrected_pay_fen": None,
            "status": "PAID",
        }
    ]
    (highlight,) = svc._summarize_rows(rows)
    assert "张伟 2026-09" in highlight
    assert "5300.00 元" in highlight
    assert "已发放" in highlight


def test_payroll_named_employee_passes_user_name(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """点名员工（「刘美玲近半年工资单」）：姓名解析为库内 ACTIVE 用户传给工具。"""
    from app.services.assistant_chat_service import _ToolOutcome

    w = _world(session)
    _mgr_user, actor = _manager(session, w, "asst-mgr-pay-named")
    _user(session, w["ds"], RoleCode.SALES_REP, "刘美玲")
    captured: dict = {}

    def fake_run(self, actor, tool, arguments, *, session_id=None):  # noqa: ANN001, ANN202
        captured.update(arguments)
        return _ToolOutcome(status="OK", item_count=0, raw={"items": []})

    monkeypatch.setattr(svc.AssistantChatService, "_run_tool", fake_run)

    AssistantChatService(session).chat(actor, "查询刘美玲近半年的工资单")

    assert captured["user_name"] == "刘美玲"
    assert captured["months"] == 6


def test_payroll_longest_name_wins(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """「刘美」撞「刘美玲」：取最长名，不截短。"""
    from app.services.assistant_chat_service import _ToolOutcome

    w = _world(session)
    _mgr_user, actor = _manager(session, w, "asst-mgr-pay-long")
    _user(session, w["ds"], RoleCode.SALES_REP, "刘美")
    _user(session, w["ds"], RoleCode.SALES_REP, "刘美玲")
    captured: dict = {}

    def fake_run(self, actor, tool, arguments, *, session_id=None):  # noqa: ANN001, ANN202
        captured.update(arguments)
        return _ToolOutcome(status="OK", item_count=0, raw={"items": []})

    monkeypatch.setattr(svc.AssistantChatService, "_run_tool", fake_run)

    AssistantChatService(session).chat(actor, "查一下刘美玲的工资单")

    assert captured["user_name"] == "刘美玲"


def test_payroll_ambiguous_names_not_guessed(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同时点名两个互不包含的姓名：不猜，按全员/期间口径查。"""
    from app.services.assistant_chat_service import _ToolOutcome

    w = _world(session)
    _mgr_user, actor = _manager(session, w, "asst-mgr-pay-amb")
    _user(session, w["ds"], RoleCode.SALES_REP, "张伟")
    _user(session, w["ds"], RoleCode.SALES_REP, "李静")
    captured: dict = {}

    def fake_run(self, actor, tool, arguments, *, session_id=None):  # noqa: ANN001, ANN202
        captured.update(arguments)
        return _ToolOutcome(status="OK", item_count=0, raw={"items": []})

    monkeypatch.setattr(svc.AssistantChatService, "_run_tool", fake_run)

    AssistantChatService(session).chat(actor, "对比一下张伟和李静9月的工资")

    assert "user_name" not in captured
    assert captured["period"] == f"{date.today().year}-09"


def test_payroll_detail_head_carries_single_name() -> None:
    """点名查询结果全为同一人：姓名提到头部，行内不重复。"""
    outcome = svc._ToolOutcome(
        status="OK",
        item_count=2,
        raw={
            "scope": "dataset",
            "items": [
                {
                    "user_name": "刘美玲",
                    "period_month": "2026-09",
                    "gross_pay_fen": 750000,
                    "status": "PAID",
                },
                {
                    "user_name": "刘美玲",
                    "period_month": "2026-08",
                    "gross_pay_fen": 750000,
                    "status": "PAID",
                },
            ],
        },
    )
    reply = svc._compose_reply(svc._Rule("payroll.list", "工资单", ()), outcome)

    assert reply.startswith("刘美玲 近半年工资单（不含本月）：")
    assert "· 2026-09：应发 7500.00 元，已发放" in reply
    assert reply.count("刘美玲") == 1


def test_read_outcome_extracts_record_refs() -> None:
    """工具信封的 links → 记录引用（前端右栏展示依据），label 取同序高亮。"""
    from app.services.assistant_chat_service import _read_outcome

    outcome = _read_outcome(
        {
            "result_state": "OK",
            "count": 1,
            "items": [
                {"title": "汇佳便利店年度合同", "status": "EFFECTIVE"},
            ],
            "links": [
                {
                    "object_type": "CONTRACT",
                    "object_id": "abc-123",
                    "path": "/contracts/abc-123",
                }
            ],
        }
    )

    assert outcome.records == (
        {
            "object_type": "CONTRACT",
            "object_id": "abc-123",
            "path": "/contracts/abc-123",
            "label": "汇佳便利店年度合同（EFFECTIVE）",
        },
    )
    # 无 links 的旧信封：records 为空，不报错
    assert _read_outcome({"result_state": "OK", "items": []}).records == ()


def test_extract_keyword_degrades_to_none_for_filler() -> None:
    """语气词剥光 → 不传 keyword（按默认范围列全部），业务词仍保留且连写。"""
    from app.services.assistant_chat_service import _RULES, _extract_keyword

    contract = next(r for r in _RULES if r.tool == "contract.search")
    merchant = next(r for r in _RULES if r.tool == "merchant.search")

    assert _extract_keyword("查看已经拥有合同的商家", contract) is None
    assert _extract_keyword("查一下我的商家", merchant) is None
    assert _extract_keyword("查看汇佳便利店的合同", contract) == "汇佳便利店"
    assert _extract_keyword("我想看一下汇佳便利店的合同", contract) == "汇佳便利店"
    assert _extract_keyword("汇佳便利店的合同什么时候到期", contract) == "汇佳便利店"
    assert _extract_keyword("华东的便利店有哪些", merchant) == "华东便利店"


def test_audit_records_role_and_scope_not_identity(session: Session) -> None:
    """审计只落角色与范围计数，不落身份凭据；问题经脱敏后保存。"""
    w = _world(session)
    _mgr_user, actor = _manager(session, w, "asst-mgr-audit")

    AssistantChatService(session).chat(actor, "查一下合同 手机号 13800001111")

    row = _audit_rows(session)[0]
    assert row.role_code == RoleCode.REGIONAL_MANAGER.value
    assert row.scope_json.get("role") == RoleCode.REGIONAL_MANAGER.value
    assert row.allowed_tools == "contract.search"
    assert row.model == "mock"
    # 脱敏：完整手机号不得出现在审计正文
    assert "13800001111" not in row.prompt_redacted


# ============================== 语言层 ==============================


def test_chat_narrative_is_deterministic_and_bounded() -> None:
    """同 (kind, facts, seed) 输出一致；chat 模板不产出任何业务数值。"""
    facts = {"tool": "merchant.search", "status": "OK", "count": 3, "data_complete": True}
    p1, p2 = MockAIProvider(seed=7), MockAIProvider(seed=7)

    first = p1.narrate(kind="assistant_chat", facts=facts)
    second = p2.narrate(kind="assistant_chat", facts=facts)

    assert isinstance(first, Narrative)
    assert first.model_dump() == second.model_dump()
    assert first.confidence == "MEDIUM"
    assert "3" in first.headline
    # 对话类假设必须是边界声明，而不是业务推断
    assert all("假设" not in a or "模型推断" in a for a in first.assumptions)

    # 不完整数据强制低置信
    incomplete = p1.narrate(kind="assistant_chat", facts={**facts, "data_complete": False})
    assert incomplete.confidence == "LOW"


def test_business_insight_template_family_unchanged() -> None:
    """扩展模板族不影响经营解释（T25）的既有输出。"""
    facts = {
        "period": "2026-10",
        "direction": "UP",
        "data_complete": True,
        "metric_count": 8,
    }
    text = MockAIProvider(seed=11).narrate(kind="business_insight", facts=facts)
    assert "经营" in text.headline or "走势" in text.headline
    assert text.assumptions[0].startswith("假设")


# ============================== HTTP 边界 ==============================


def test_assistant_routes_registered(client) -> None:
    """/api/v1/assistant/chat 与 /tools 必须真实挂载（前端点击后才不会 404）。"""
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/assistant/chat" in paths
    assert "/api/v1/assistant/tools" in paths


def test_chat_requires_bearer_token(client) -> None:
    """对话入口必须先鉴权：无 token 一律 401，不进入意图解析。"""
    res = client.post("/api/v1/assistant/chat", json={"message": "查一下商家"})
    assert res.status_code == 401
    assert res.json()["code"] == "MISSING_BEARER"


def test_chat_request_rejects_empty_message() -> None:
    """空问题在 schema 边界拒绝（422），不消耗工具与审计。"""
    with pytest.raises(ValidationError):
        AssistantChatRequest(message="")


def test_http_chat_with_token_returns_tool_result(
    isolated_client, isolated_session: Session
) -> None:
    """真实 Bearer 令牌走通 HTTP：前端点击后的链路（鉴权→意图→工具→审计）可用。"""
    from app.core.tokens import issue_token

    w = _world(isolated_session)
    user, _actor = _internal_actor(
        isolated_session, w, RoleCode.REGIONAL_MANAGER, "asst-http"
    )
    _merchant(isolated_session, w, name="HTTP 助手店")
    isolated_session.flush()
    token = issue_token(
        user_id=user.id,
        username=user.username,
        role_code=RoleCode.REGIONAL_MANAGER.value,
        password_version=user.password_version,
        kind="ACCESS",
    )

    res = isolated_client.post(
        "/api/v1/assistant/chat",
        json={"message": "查一下有哪些商家", "session_id": "sess-http"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["tool"] == "merchant.search"
    assert body["tool_status"] == "OK"
    assert body["item_count"] >= 1
    assert body["audit_id"]
    assert body["session_id"] == "sess-http"
    assert body["narrative"]["confidence"] in {"LOW", "MEDIUM"}


def test_http_tools_list_only_read_and_bound_drafts(
    isolated_client, isolated_session: Session
) -> None:
    """能力清单只暴露只读 + 已绑定的 A1 草稿，写工具一律不出现。"""
    from app.core.tokens import issue_token

    w = _world(isolated_session)
    user, _actor = _internal_actor(
        isolated_session, w, RoleCode.REGIONAL_MANAGER, "asst-tools"
    )
    isolated_session.flush()
    token = issue_token(
        user_id=user.id,
        username=user.username,
        role_code=RoleCode.REGIONAL_MANAGER.value,
        password_version=user.password_version,
        kind="ACCESS",
    )

    res = isolated_client.get(
        "/api/v1/assistant/tools", headers={"Authorization": f"Bearer {token}"}
    )

    assert res.status_code == 200, res.text
    tools = res.json()
    assert tools
    assert {tool["kind"] for tool in tools} <= {"READ", "DRAFT"}
    names = {tool["name"] for tool in tools}
    assert "merchant.search" in names
    assert "campaign.create_draft" in names
    # A2/A3 写工具绝不进入对话能力清单
    assert "visit.submit" not in names
    assert "schedule.publish" not in names


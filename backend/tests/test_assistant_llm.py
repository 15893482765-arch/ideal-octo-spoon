"""AI 助手对话模型编排（PRD §5.1、§5.4）：模型只选只读工具与组织语言。

覆盖：
- 模型选只读工具 → 服务端执行 → 模型据结果作答（审计记录真实 model）；
- 模型试图调用写工具 → 拒绝执行，草稿/业务表零写入；
- 模型不可用 → 确定性规则回退，不抛 500；
- 工具不在角色白名单 → 拒绝，不执行。
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tests.test_t09_contract import _merchant, _world
from tests.test_t18_procurement import _internal_actor

from app.models import VisitRecord
from app.models.ai_interaction import AiInteractionLog
from app.models.enums import RoleCode
from app.services.ai_llm import (
    LlmReply,
    LlmToolCall,
    LlmUnavailableError,
    OpenAiCompatClient,
)
from app.services.assistant_chat_service import AssistantChatService


class FakeLlm:
    """按顺序返回预设回复；记录每次调用，便于断言提示词与工具清单。"""

    model = "kimi-k2.7-code"

    def __init__(self, replies: list[Any]) -> None:
        self._replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        *,
        messages: Any,
        tools: Any = None,
        temperature: float | None = None,
        max_tokens: int = 800,
    ) -> LlmReply:
        self.calls.append({"messages": list(messages), "tools": list(tools or [])})
        item = self._replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _use_llm(monkeypatch, fake: FakeLlm) -> None:
    monkeypatch.setattr(
        "app.services.assistant_chat_service.get_chat_llm", lambda: fake
    )


def _audit(session: Session) -> list[AiInteractionLog]:
    return list(session.scalars(select(AiInteractionLog)).all())


def test_model_selects_read_tool_and_answers_from_result(
    session: Session, monkeypatch
) -> None:
    """模型选 merchant.search → 工具真实执行 → 回答用模型文本，审计记真实 model。"""
    w = _world(session)
    _mgr, actor = _internal_actor(session, w, RoleCode.REGIONAL_MANAGER, "asst-llm-ok")
    _merchant(session, w, name="华东便利店")
    _merchant(session, w, name="城西便利店")
    session.flush()

    fake = FakeLlm(
        [
            LlmReply(
                content="",
                # Moonshot 的函数名不允许点号：模型返回的是下划线形式
                tool_calls=(LlmToolCall("merchant_search", {"limit": 10}),),
            ),
            LlmReply(content="你在本片区有 2 家商家；先看华东便利店，再看城西便利店。"),
        ]
    )
    _use_llm(monkeypatch, fake)

    data = AssistantChatService(session).chat(actor, "我这个片区有哪些店？")

    assert data["tool"] == "merchant.search"
    assert data["tool_status"] == "OK"
    assert data["item_count"] == 2
    assert data["reply"].startswith("你在本片区有 2 家商家")
    assert data["degraded"] is False
    # 提示词里应带上工具清单（下划线形式），且第二次调用拿到工具结果
    names = {t["function"]["name"] for t in fake.calls[0]["tools"]}
    assert "merchant_search" in names
    assert all("." not in name for name in names)
    assert "华东便利店" in fake.calls[1]["messages"][-1]["content"]

    rows = _audit(session)
    assert len(rows) == 1
    assert rows[0].model == "kimi-k2.7-code"
    assert rows[0].allowed_tools == "merchant.search"


def test_model_calling_write_tool_is_rejected(session: Session, monkeypatch) -> None:
    """模型幻觉出写工具：服务端拒绝，不执行、不落业务数据。"""
    w = _world(session)
    _mgr, actor = _internal_actor(session, w, RoleCode.REGIONAL_MANAGER, "asst-llm-write")
    _merchant(session, w, name="华东便利店")
    session.flush()

    fake = FakeLlm(
        [
            LlmReply(
                content="",
                tool_calls=(
                    LlmToolCall(
                        "visit.submit",
                        {"merchant_id": "00000000-0000-0000-0000-000000000000"},
                    ),
                ),
            )
        ]
    )
    _use_llm(monkeypatch, fake)

    data = AssistantChatService(session).chat(actor, "帮我把这单提交了")

    assert data["tool"] == "visit.submit"
    assert data["tool_status"] == "REJECTED"
    assert "不能调用" in data["reply"]
    assert data["draft"] is None
    assert session.scalar(select(func.count()).select_from(VisitRecord)) == 0
    # 只调用了规划那一次，拒绝后没有再让模型组织语言
    assert len(fake.calls) == 1
    assert _audit(session)[0].model == "kimi-k2.7-code"


def test_model_failure_falls_back_to_rules(session: Session, monkeypatch) -> None:
    """模型不可用：回退关键词规则 + 模板回话，请求不失败。"""
    w = _world(session)
    _mgr, actor = _internal_actor(session, w, RoleCode.REGIONAL_MANAGER, "asst-llm-down")
    _merchant(session, w, name="华东便利店")
    session.flush()

    # AiGateway 最多 2 次尝试：两次都失败才降级
    fake = FakeLlm([LlmUnavailableError("timeout"), LlmUnavailableError("timeout")])
    _use_llm(monkeypatch, fake)

    data = AssistantChatService(session).chat(actor, "查一下有哪些商家")

    assert data["tool"] == "merchant.search"
    assert data["tool_status"] == "OK"
    assert "商家" in data["reply"]
    assert data["degraded"] is True
    assert _audit(session)[0].model == "kimi-k2.7-code"


def test_model_picking_role_forbidden_tool_is_rejected(
    session: Session, monkeypatch
) -> None:
    """业务员让模型查补货汇总：工具不在白名单，直接拒绝且不执行。"""
    w = _world(session)
    _rep, actor = _internal_actor(session, w, RoleCode.SALES_REP, "asst-llm-scope")

    fake = FakeLlm(
        [
            LlmReply(
                content="",
                tool_calls=(LlmToolCall("replenishment.aggregate", {}),),
            )
        ]
    )
    _use_llm(monkeypatch, fake)

    data = AssistantChatService(session).chat(actor, "这个月一共要补多少货？")

    assert data["tool_status"] == "REJECTED"
    assert data["item_count"] == 0
    assert data["highlights"] == []
    # 规划提示词里不应出现该工具（按角色裁剪）
    names = {t["function"]["name"] for t in fake.calls[0]["tools"]}
    assert "replenishment_aggregate" not in names


def test_rule_hit_costs_single_llm_call(session: Session, monkeypatch) -> None:
    """规则能路由时不再让模型规划：每轮只消耗一次模型调用（RPM 友好）。"""
    w = _world(session)
    _mgr, actor = _internal_actor(session, w, RoleCode.REGIONAL_MANAGER, "asst-llm-quota")
    _merchant(session, w, name="华东便利店")
    session.flush()

    fake = FakeLlm([LlmReply(content="本片区有 1 家：华东便利店。")])
    _use_llm(monkeypatch, fake)

    data = AssistantChatService(session).chat(actor, "查一下有哪些商家")

    assert data["tool"] == "merchant.search"
    assert len(fake.calls) == 1
    # 第二次调用只带数据上下文，不带工具清单
    assert fake.calls[0]["tools"] == []
    assert "华东便利店" in fake.calls[0]["messages"][-1]["content"]


@pytest.mark.skipif(
    os.environ.get("RUN_LLM_E2E") != "1",
    reason="手动 e2e：需要真实模型配额（默认关闭，避免 CI 依赖外网）",
)
def test_real_llm_end_to_end(session: Session, monkeypatch) -> None:
    """真实模型 + 真实工具 + 真实 DB：验证回答由模型基于工具结果生成。"""
    from dotenv import dotenv_values

    cfg = dotenv_values(".env")
    key = cfg.get("AI_API_KEY") or ""
    if not key:
        pytest.skip("未配置 AI_API_KEY")
    real = OpenAiCompatClient(
        base_url=cfg.get("AI_BASE_URL") or "https://api.deepseek.com/v1",
        api_key=key,
        model=cfg.get("AI_MODEL") or "deepseek-flash",
        timeout=40.0,
    )
    monkeypatch.setattr(
        "app.services.assistant_chat_service.get_chat_llm", lambda: real
    )

    w = _world(session)
    _mgr, actor = _internal_actor(session, w, RoleCode.REGIONAL_MANAGER, "asst-e2e")
    _merchant(session, w, name="华东便利店")
    _merchant(session, w, name="城西便利店")
    session.flush()

    data = AssistantChatService(session).chat(actor, "查一下有哪些商家")

    print("\n[LLM_REPLY]", data["reply"])
    print("[LLM_DEGRADED]", data["degraded"], data["degraded_reason"])
    assert data["tool"] == "merchant.search"
    assert data["tool_status"] == "OK"
    assert data["item_count"] == 2
    assert len(data["reply"]) > 4
    assert data["degraded"] is False
    assert _audit(session)[0].model == real.model


def test_llm_draft_path_is_still_deterministic(session: Session, monkeypatch) -> None:
    """草稿不交给模型拼 ID：即使模型可用，也走确定性解析并落草稿。"""
    from tests.test_t11_visit import _assign_primary

    w = _world(session)
    rep, actor = _internal_actor(session, w, RoleCode.SALES_REP, "asst-llm-draft")
    merchant = _merchant(session, w, name="华东便利店")
    _assign_primary(session, merchant, rep)
    session.flush()

    fake = FakeLlm([])
    _use_llm(monkeypatch, fake)

    data = AssistantChatService(session).chat(
        actor, f"帮我给{merchant.name}整理巡访草稿"
    )

    assert data["tool_status"] == "DRAFT_CREATED"
    assert data["draft"]["kind"] == "visit"
    # 草稿路径没有调用模型
    assert fake.calls == []

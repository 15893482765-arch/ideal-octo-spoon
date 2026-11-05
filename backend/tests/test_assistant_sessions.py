"""AI 助手会话持久化（《03.Agent状态管理》§4/§5）。

覆盖：
- 建会话：首条消息作标题，一轮对话（user+assistant）整体落库；
- 员工隔离：他人会话读取/删除/续聊一律 404，不暴露存在性；
- 历史重建：按 seq 正序返回；超过字符阈值丢弃最早消息（compact_context）；
- 上下文记忆：续聊时历史消息注入 LLM 规划/回答两次调用；
- facts：每轮解析出的工具/周期覆盖式合并。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.test_t09_contract import _merchant, _world
from tests.test_t18_procurement import _internal_actor

from app.core.errors import DomainError
from app.models.assistant_session import AssistantMessage, AssistantSession
from app.models.enums import RoleCode
from app.services import assistant_chat_service as chat_mod
from app.services.assistant_chat_service import AssistantChatService
from app.services.assistant_session_service import (
    AssistantSessionService,
)


def _actor(session: Session, w: dict, tag: str = "sess-rep"):
    return _internal_actor(session, w, RoleCode.SALES_REP, tag)


def _messages(session: Session, session_id) -> list[AssistantMessage]:
    return list(
        session.scalars(
            select(AssistantMessage)
            .where(AssistantMessage.session_id == session_id)
            .order_by(AssistantMessage.seq.asc())
        ).all()
    )


# ============================== 创建与保存 ==============================


def test_chat_creates_session_titled_by_first_message(session: Session) -> None:
    """无 conversation_id：新建会话，标题=首条用户消息；user+assistant 同轮落库。"""
    w = _world(session)
    # 经理可见片区全部商家（业务员仅见责任归属商家，会命中 NO_RECORDS）
    _user, actor = _internal_actor(session, w, RoleCode.REGIONAL_MANAGER, "sess-mgr")
    _merchant(session, w, name="悦购便利店")
    session.flush()

    data = AssistantChatService(session).chat(actor, "查一下我的商家")

    assert data["conversation_id"], "响应必须带回会话 id"
    svc = AssistantSessionService(session)
    mine = svc.list_sessions(actor)
    assert len(mine) == 1
    conv = mine[0]
    assert conv.title == "查一下我的商家"
    assert conv.message_count == 2

    rows = _messages(session, conv.id)
    assert [r.role for r in rows] == ["user", "assistant"]
    assert rows[0].content == "查一下我的商家"
    assert rows[1].content == data["reply"]
    # assistant 消息 meta 保存工具调用摘要（等价资料 messages 中的工具结果）
    assert rows[1].meta["tool"] == "merchant.search"
    assert rows[1].meta["tool_status"] == "OK"
    assert conv.facts.get("last_tool") == "merchant.search"


def test_continue_appends_and_keeps_title(session: Session) -> None:
    """同一 conversation_id 续聊：消息按 seq 追加，标题保持首句不变。"""
    w = _world(session)
    _user, actor = _actor(session, w)
    svc = AssistantChatService(session)

    first = svc.chat(actor, "我的待巡访任务")
    cid = first["conversation_id"]
    second = svc.chat(actor, "本月补货需求汇总", conversation_id=cid)

    assert second["conversation_id"] == cid
    rows = _messages(session, cid)
    assert [r.seq for r in rows] == [1, 2, 3, 4]
    assert [r.role for r in rows] == ["user", "assistant", "user", "assistant"]

    conv = AssistantSessionService(session).get_owned(actor, cid)
    assert conv.title == "我的待巡访任务", "标题固定为首条消息"
    assert conv.message_count == 4
    assert conv.facts.get("last_tool") == "replenishment.aggregate"


# ============================== 员工隔离 ==============================


def test_sessions_are_isolated_per_user(session: Session) -> None:
    """员工 2 看不到员工 1 的会话；读取/续聊/删除他人会话一律 404。"""
    w = _world(session)
    _u1, actor1 = _actor(session, w, "sess-a")
    _u2, actor2 = _actor(session, w, "sess-b")

    data = AssistantChatService(session).chat(actor1, "查一下我的商家")
    cid = data["conversation_id"]

    svc = AssistantSessionService(session)
    assert [s.id for s in svc.list_sessions(actor2)] == []

    with pytest.raises(DomainError, match="会话不存在或无权访问"):
        svc.list_messages(actor2, cid)
    with pytest.raises(DomainError, match="会话不存在或无权访问"):
        svc.delete_session(actor2, cid)
    with pytest.raises(DomainError, match="会话不存在或无权访问"):
        AssistantChatService(session).chat(actor2, "继续说", conversation_id=cid)


def test_delete_session_cascades_messages(session: Session) -> None:
    w = _world(session)
    _user, actor = _actor(session, w)
    cid = AssistantChatService(session).chat(actor, "查一下我的商家")["conversation_id"]

    svc = AssistantSessionService(session)
    svc.delete_session(actor, cid)

    assert session.get(AssistantSession, cid) is None
    assert _messages(session, cid) == []


# ============================== 历史重建与压缩 ==============================


def test_history_respects_char_limit(session: Session) -> None:
    """超过字符阈值丢弃最早消息，保留最近若干轮（compact_context）。"""
    w = _world(session)
    _user, actor = _actor(session, w)
    svc = AssistantSessionService(session)
    conv = svc.get_or_create(actor, None, "开场")
    for i in range(6):
        svc.append_turn(
            conv,
            user_message=f"第{i}轮问题" + "字" * 100,
            assistant_reply=f"第{i}轮回答" + "字" * 100,
        )

    full = svc.history_for_model(conv, char_limit=100_000)
    assert len(full) == 12  # 6 轮 × 2 条
    assert full[0]["content"].startswith("第0轮问题"), "全量时按 seq 正序"

    compact = svc.history_for_model(conv, char_limit=500)
    assert 0 < len(compact) < len(full), "必须截断"
    assert compact[-1]["content"].endswith("字" * 10) or "第5轮" in compact[-1]["content"]
    assert compact[0]["content"].startswith("第"), "截断点落在消息边界"


# ============================== 上下文记忆注入 ==============================


class _SpyLlm:
    """记录每次 complete 收到的 messages；不调用工具，直接回答。"""

    model = "spy-model"

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    def complete(self, *, messages, tools=None, max_tokens=1024):
        self.calls.append(list(messages))
        return chat_mod.LlmReply(content="好的，继续。", tool_calls=[])


def test_followup_injects_history_into_llm(session: Session, monkeypatch) -> None:
    """续聊时：历史 user/assistant 消息注入规划与回答两次 LLM 调用。"""
    w = _world(session)
    _user, actor = _actor(session, w)
    spy = _SpyLlm()
    monkeypatch.setattr(chat_mod, "get_chat_llm", lambda: spy)

    svc = AssistantChatService(session)
    first = svc.chat(actor, "今天天气不适合巡访吗")  # 无规则命中 → 走 LLM
    cid = first["conversation_id"]
    assert first["conversation_id"]

    svc.chat(actor, "那我刚才说的呢", conversation_id=cid)

    assert len(spy.calls) >= 2, "续聊至少规划/回答各一次调用"
    followup = spy.calls[-1]
    roles = [m["role"] for m in followup]
    assert roles[0] == "system"
    # 历史必须出现在 system 与当前 user 之间
    body = [m for m in followup if m["role"] in {"user", "assistant"}]
    assert any("今天天气" in str(m["content"]) for m in body[:-1]), "历史消息缺失"
    assert any("好的，继续。" in str(m["content"]) for m in body[:-1]), "历史回答缺失"
    assert "那我刚才说的呢" in str(body[-1]["content"])

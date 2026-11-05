"""AI 助手 A1 草稿（PRD §5.3 A1、§5.4）：只产草稿、不回写正式业务表。

覆盖：
- 巡访草稿：唯一商家 + 无历史草稿时生成 `mcp_drafts`，不产生 VisitRecord；
- 商家歧义：多命中时回问，不瞎选、不落草稿；
- 地推活动草稿：经理可用，标题来自剥离口令后的正文；
- 补货草稿：需商品 + 箱数，生成草稿但不产生 ReplenishmentOrder；
- 越权：角色不在工具 allowed_roles 时直接拒绝，且不检索商家（不暴露范围）。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tests.test_t09_contract import _merchant, _world
from tests.test_t11_visit import _assign_primary
from tests.test_t18_procurement import _internal_actor, _sku

from app.models import ReplenishmentOrder, VisitRecord
from app.models.enums import RoleCode
from app.models.mcp_draft import McpDraft
from app.services.assistant_chat_service import AssistantChatService


def _drafts(session: Session) -> list[McpDraft]:
    return list(session.scalars(select(McpDraft)).all())


def test_visit_draft_created_without_business_record(session: Session) -> None:
    """生成巡访草稿：只落 mcp_drafts，正式巡访记录仍为 0。"""
    w = _world(session)
    rep, actor = _internal_actor(session, w, RoleCode.SALES_REP, "asst-draft-visit")
    merchant = _merchant(session, w, name="华东便利店")
    _assign_primary(session, merchant, rep)
    session.flush()

    data = AssistantChatService(session).chat(
        actor, f"帮我给{merchant.name}整理巡访草稿", session_id="sess-draft-visit"
    )

    assert data["tool"] == "visit.compose_draft"
    assert data["tool_status"] == "DRAFT_CREATED"
    assert data["draft"]["kind"] == "visit"
    assert data["draft"]["target_path"] == "/visits"
    assert data["draft"]["target_label"] == "巡访管理"
    assert "未写入正式业务表" in data["reply"]
    assert data["intent"] == "VISIT_COMPOSE"

    drafts = _drafts(session)
    assert len(drafts) == 1
    assert drafts[0].kind == "visit"
    assert drafts[0].status == "OPEN"  # mcp_drafts 的暂存态
    # 红线：草稿不得回写正式业务表
    assert session.scalar(select(func.count()).select_from(VisitRecord)) == 0


def test_visit_draft_asks_when_merchant_ambiguous(session: Session) -> None:
    """商家多命中时只回问候选，不落任何草稿。"""
    w = _world(session)
    rep, actor = _internal_actor(session, w, RoleCode.SALES_REP, "asst-draft-ambig")
    first = _merchant(session, w, name="华东便利店")
    second = _merchant(session, w, name="华东便利店二店")
    _assign_primary(session, first, rep)
    _assign_primary(session, second, rep)
    session.flush()

    data = AssistantChatService(session).chat(actor, "帮我给华东便利店整理巡访草稿")

    assert data["tool_status"] == "NEED_INPUT"
    assert data["draft"] is None
    assert data["item_count"] == 2
    assert len(data["highlights"]) == 2
    assert "请说明具体是哪一家" in data["reply"]
    assert _drafts(session) == []


def test_campaign_draft_title_strips_command_words(session: Session) -> None:
    """地推活动草稿：标题取剥离口令后的正文，草稿类型为 campaign。"""
    w = _world(session)
    _mgr, actor = _internal_actor(session, w, RoleCode.REGIONAL_MANAGER, "asst-draft-camp")

    data = AssistantChatService(session).chat(
        actor, "帮我创建一个地推活动草稿：双十一进店"
    )

    assert data["tool"] == "campaign.create_draft"
    assert data["tool_status"] == "DRAFT_CREATED"
    assert data["draft"]["kind"] == "campaign"
    assert data["draft"]["target_path"] == "/campaigns"

    drafts = _drafts(session)
    assert len(drafts) == 1
    assert drafts[0].payload.get("title") == "双十一进店"


def test_replenishment_draft_does_not_create_order(session: Session) -> None:
    """补货草稿：需商品 + 箱数；只落草稿，不产生补货单（DRAFT 补货单也不产生）。"""
    w = _world(session)
    rep, actor = _internal_actor(session, w, RoleCode.SALES_REP, "asst-draft-repl")
    merchant = _merchant(session, w, name="华东便利店")
    _assign_primary(session, merchant, rep)
    _sku(session, w, "ASST")
    session.flush()

    data = AssistantChatService(session).chat(
        actor, f"帮我给{merchant.name}建个补货草稿，沁爽汽水 5 箱"
    )

    assert data["tool"] == "replenishment.create_draft"
    assert data["tool_status"] == "DRAFT_CREATED"
    assert data["draft"]["kind"] == "replenishment"
    assert data["draft"]["target_path"] == "/replenishments"

    drafts = _drafts(session)
    assert len(drafts) == 1
    assert drafts[0].payload.get("requested_boxes") == 5.0
    assert session.scalar(select(func.count()).select_from(ReplenishmentOrder)) == 0


def test_replenishment_draft_requires_boxes(session: Session) -> None:
    """缺箱数时回问，不生成草稿。"""
    w = _world(session)
    rep, actor = _internal_actor(session, w, RoleCode.SALES_REP, "asst-draft-boxes")
    merchant = _merchant(session, w, name="华东便利店")
    _assign_primary(session, merchant, rep)
    _sku(session, w, "ASST2")
    session.flush()

    data = AssistantChatService(session).chat(
        actor, f"帮我给{merchant.name}建个补货草稿，沁爽汽水"
    )

    assert data["tool_status"] == "NEED_INPUT"
    assert data["draft"] is None
    assert "箱数" in data["reply"]
    assert _drafts(session) == []


def test_http_visit_draft_created_and_committed(
    isolated_client, isolated_session: Session
) -> None:
    """HTTP 下走通草稿：handler 自行 commit 后仍能正常写审计并返回 200。"""
    from app.core.tokens import issue_token

    w = _world(isolated_session)
    rep, _actor = _internal_actor(
        isolated_session, w, RoleCode.SALES_REP, "asst-draft-http"
    )
    merchant = _merchant(isolated_session, w, name="HTTP 草稿店")
    _assign_primary(isolated_session, merchant, rep)
    isolated_session.flush()
    token = issue_token(
        user_id=rep.id,
        username=rep.username,
        role_code=RoleCode.SALES_REP.value,
        password_version=rep.password_version,
        kind="ACCESS",
    )

    res = isolated_client.post(
        "/api/v1/assistant/chat",
        json={
            "message": f"帮我给{merchant.name}整理巡访草稿",
            "session_id": "sess-draft-http",
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["tool_status"] == "DRAFT_CREATED"
    assert body["draft"]["target_path"] == "/visits"
    assert body["audit_id"]
    assert len(_drafts(isolated_session)) == 1


def test_draft_denied_for_role_outside_catalog(session: Session) -> None:
    """经理不在巡访草稿 allowed_roles：直接拒绝，且不检索商家、不落草稿。"""
    w = _world(session)
    _mgr, actor = _internal_actor(session, w, RoleCode.REGIONAL_MANAGER, "asst-draft-deny")
    _merchant(session, w, name="华东便利店")
    session.flush()

    data = AssistantChatService(session).chat(actor, "帮我给华东便利店整理巡访草稿")

    assert data["tool"] == "visit.compose_draft"
    assert data["tool_status"] == "NO_PERMISSION"
    assert data["draft"] is None
    assert data["highlights"] == []
    assert "无权" in data["reply"]
    assert _drafts(session) == []

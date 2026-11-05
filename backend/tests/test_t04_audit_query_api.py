"""审计查询接口 HTTP 联调（T04 只读读取面补齐）。

覆盖：
- 权限：未认证 401；`audit` 能力门禁（当前仅系统管理员持有）；
- 过滤：对象类型 / 对象 ID / 事件类型 / 操作人关键字 / 时间区间 / 原因关键字；
- 分页：`limit/offset` 与 `total`，单页上限拦截；
- 认证事件：事件类型校验、用户关键字、payload 回显；
- facets：对象类型取库中实际值，事件类型取受控枚举。

用 `isolated_client` + `isolated_session`（conftest）：审计行与 HTTP 查询处于同一
SAVEPOINT 事务，用例结束整体回滚，不给其它用例留痕。断言统一限定在**本用例专属的
对象类型标记**内，避免受测试库中其它已提交审计行影响。
不新增 Q 编号（覆盖 R30 的读取面）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import AuditLog, AuthEvent
from app.models.enums import AuthEventType

BASE = "/api/admin/audit-logs"


def _admin_headers(isolated_client: TestClient, admin: dict[str, str]) -> dict[str, str]:
    token = isolated_client.post(
        "/api/auth/login",
        json={"username": admin["username"], "password": admin["password"]},
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _seed_audit(
    isolated_session: Session, actor_id: UUID, mark: str
) -> tuple[dict[str, str], dict[str, str]]:
    now = datetime.now(tz=UTC)
    rows = {
        "contract": AuditLog(
            object_type=mark,
            object_id=uuid4(),
            event_type="CREATE",
            actor_id=actor_id,
            occurred_at=now - timedelta(minutes=1),
            request_id="req-contract",
            reason="新签合同提交",
        ),
        "freezer": AuditLog(
            object_type=mark,
            object_id=uuid4(),
            event_type="STATE_TRANSITION",
            actor_id=actor_id,
            occurred_at=now - timedelta(minutes=2),
            request_id="req-freezer",
            reason="撤柜验收通过",
            before_json={"status": "DEPLOYED"},
            after_json={"status": "IN_STOCK"},
            payload={"origin": "acceptance"},
        ),
        "denied": AuditLog(
            object_type=mark,
            object_id=uuid4(),
            event_type="DELETE_DENIED",
            actor_id=None,
            occurred_at=now - timedelta(minutes=3),
            reason="已审核合同不允许删除",
        ),
    }
    isolated_session.add_all(list(rows.values()))
    isolated_session.flush()
    # 审计行主键（items[].id）与业务对象 ID（object_id 过滤参数）是两回事
    return (
        {key: str(log.id) for key, log in rows.items()},
        {key: str(log.object_id) for key, log in rows.items()},
    )


def test_t04_audit_query_requires_ability(isolated_client: TestClient) -> None:
    """未认证一律 401（审计日志对普通用户不可见）。"""
    assert isolated_client.get(BASE).status_code == 401
    assert isolated_client.get(f"{BASE}/facets").status_code == 401
    assert isolated_client.get(f"{BASE}/auth-events").status_code == 401


def test_t04_audit_query_filters_pagination_and_facets(
    isolated_session: Session, isolated_client: TestClient, bootstrap_admin
) -> None:
    headers = _admin_headers(isolated_client, bootstrap_admin)
    mark = f"T04Q{uuid4().hex[:8].upper()}"
    ids, object_ids = _seed_audit(
        isolated_session, UUID(bootstrap_admin["user_id"]), mark
    )

    # 默认按时间倒序：最新在前；操作人展示名来自 users 表
    page = isolated_client.get(
        BASE, params={"object_type": mark}, headers=headers
    ).json()
    assert page["total"] == 3
    assert [item["id"] for item in page["items"]] == [
        ids["contract"], ids["freezer"], ids["denied"],
    ]
    assert page["items"][0]["actor_name"] == "T03 管理员"
    assert page["items"][2]["actor_id"] is None

    # 事件类型 + 对象 ID 过滤；前后值与 payload 原样透传（入库前已脱敏）
    transition = isolated_client.get(
        BASE,
        params={
            "object_type": mark,
            "event_type": "STATE_TRANSITION",
            "object_id": object_ids["freezer"],
        },
        headers=headers,
    ).json()
    assert transition["total"] == 1
    item = transition["items"][0]
    assert item["id"] == ids["freezer"]
    assert item["object_id"] == object_ids["freezer"]
    assert item["before_json"] == {"status": "DEPLOYED"}
    assert item["after_json"] == {"status": "IN_STOCK"}
    assert item["payload"] == {"origin": "acceptance"}
    assert item["request_id"] == "req-freezer"

    # 原因关键字
    by_keyword = isolated_client.get(
        BASE, params={"object_type": mark, "keyword": "撤柜"}, headers=headers
    ).json()
    assert by_keyword["total"] == 1

    # 操作人关键字：只有两条带 actor
    by_actor = isolated_client.get(
        BASE,
        params={"object_type": mark, "actor_keyword": bootstrap_admin["username"]},
        headers=headers,
    ).json()
    assert by_actor["total"] == 2

    # 时间区间：最近 2 分 30 秒内只有两条
    since = (datetime.now(tz=UTC) - timedelta(minutes=2, seconds=30)).isoformat()
    by_time = isolated_client.get(
        BASE, params={"object_type": mark, "occurred_from": since}, headers=headers
    ).json()
    assert by_time["total"] == 2

    # 分页：limit/offset
    first_page = isolated_client.get(
        BASE, params={"object_type": mark, "limit": 1}, headers=headers
    ).json()
    assert first_page["limit"] == 1 and len(first_page["items"]) == 1
    assert first_page["items"][0]["id"] == ids["contract"]
    second_page = isolated_client.get(
        BASE, params={"object_type": mark, "limit": 1, "offset": 1}, headers=headers
    ).json()
    assert second_page["items"][0]["id"] == ids["freezer"]

    # 非法入参被拒（DomainError 统一映射 422，带业务错误码）
    bad_event = isolated_client.get(
        BASE, params={"event_type": "BOGUS"}, headers=headers
    )
    assert bad_event.status_code == 422
    assert bad_event.json()["code"] == "AUDIT_EVENT_TYPE_INVALID"

    too_large = isolated_client.get(BASE, params={"limit": 500}, headers=headers)
    assert too_large.status_code == 422
    assert too_large.json()["code"] == "AUDIT_PAGE_TOO_LARGE"

    # facets：对象类型取库中实际值（含本例标记），事件类型取受控枚举
    facets = isolated_client.get(f"{BASE}/facets", headers=headers).json()
    assert mark in facets["object_types"]
    assert set(facets["event_types"]) == {
        "CREATE", "UPDATE", "STATE_TRANSITION", "DELETE_DENIED",
    }
    assert AuthEventType.LOGIN_FAILED.value in facets["auth_event_types"]


def test_t04_auth_events_query(
    isolated_session: Session, isolated_client: TestClient, bootstrap_admin
) -> None:
    """认证域事件：按类型 + 用户检索，payload 原样透传（不含凭据正文）。"""
    headers = _admin_headers(isolated_client, bootstrap_admin)
    admin_id = UUID(bootstrap_admin["user_id"])
    username = bootstrap_admin["username"]

    isolated_session.add_all([
        AuthEvent(
            user_id=admin_id,
            actor_id=None,
            event_type=AuthEventType.LOGIN_FAILED.value,
            occurred_at=datetime.now(tz=UTC) - timedelta(minutes=5),
            request_id="req-login-failed",
            payload={"reason": "bad_password", "failed_count": 1},
        ),
        AuthEvent(
            user_id=admin_id,
            actor_id=admin_id,
            event_type=AuthEventType.PASSWORD_CHANGED.value,
            occurred_at=datetime.now(tz=UTC) - timedelta(minutes=4),
            payload={"must_change_password": False},
        ),
    ])
    isolated_session.flush()

    only_failed = isolated_client.get(
        f"{BASE}/auth-events",
        params={"event_type": "LOGIN_FAILED", "user_keyword": username},
        headers=headers,
    ).json()
    assert only_failed["total"] == 1
    assert only_failed["items"][0]["payload"] == {
        "reason": "bad_password",
        "failed_count": 1,
    }
    assert only_failed["items"][0]["user_name"] == "T03 管理员"
    assert only_failed["items"][0]["request_id"] == "req-login-failed"

    only_changed = isolated_client.get(
        f"{BASE}/auth-events",
        params={"event_type": "PASSWORD_CHANGED", "user_keyword": username},
        headers=headers,
    ).json()
    assert only_changed["total"] == 1
    # 目标用户与操作人同为管理员，两个展示名都要能取到
    assert only_changed["items"][0]["actor_name"] == "T03 管理员"

    bad_auth = isolated_client.get(
        f"{BASE}/auth-events", params={"event_type": "NOT_A_TYPE"}, headers=headers
    )
    assert bad_auth.status_code == 422
    assert bad_auth.json()["code"] == "AUTH_EVENT_TYPE_INVALID"

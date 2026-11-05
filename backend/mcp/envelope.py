"""MCP 只读工具统一返回封装（T23 / PRD §5.1、§5.3）。

每个只读工具的结果都携带 PRD §5.1 要求的四件套：

- **数据范围** `scope`：当前 actor 的角色与行级范围（只来自服务端 JWT/session）；
- **统计口径** `definition`：本工具取数的确定性口径说明；
- **数据截至时间** `as_of`（UTC + Asia/Shanghai 展示）；
- **业务记录链接** `links`：可回到业务页面的引用（仍受鉴权保护）。

并用 `result_state` 明确区分「有数据 / 无记录 / 无权限」：

- `OK`：有权访问且有数据；
- `NO_RECORDS`：有权访问但确实没有记录（空数据）；
- `NO_PERMISSION`：行级/字段级权限不允许该对象。

二者**不可混淆**：无权对象绝不静默返回空列表冒充「无记录」。
本模块不做任何数据库访问，只做结构化封装与 JSON 安全化（金额/箱数用字符串，禁止 float）。
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from app.core.errors import DomainError
from mcp.identity import McpContext

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

RESULT_OK = "OK"
RESULT_NO_RECORDS = "NO_RECORDS"
RESULT_NO_PERMISSION = "NO_PERMISSION"

# 业务记录详情路由（与 app/api 的实际挂载路径一一对应；T23 只引用，不新增路由）
_RECORD_ROUTES: dict[str, str] = {
    "MERCHANT": "/api/merchants/{id}",
    "CONTRACT": "/api/contracts/{id}",
    "FREEZER": "/api/freezers/{id}",
    "VISIT_TASK": "/api/visits/tasks/{id}",
    "VISIT_RECORD": "/api/visits/records/{id}",
    "REPLENISHMENT": "/api/replenishment/orders/{id}",
    "PAYROLL": "/api/payroll/statements/{id}",
}

# 列表/对象级路由（无逐条详情路由时的回链）
_LIST_ROUTES: dict[str, str] = {
    "RISK": "/api/reporting/risks",
    "APPROVAL": "/api/reporting/risks",
}

# 风险对象类型 → 详情路由（风险项回链到其指向的业务对象）
_RISK_OBJECT_TYPES: frozenset[str] = frozenset(
    {"MERCHANT", "CONTRACT", "FREEZER", "REPLENISHMENT"}
)


def jsonable(value: Any) -> Any:
    """把 ORM/Pydantic 值转成 JSON 安全结构（金额/NUMERIC 一律字符串，禁止 float）。"""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # 金额/箱数红线：不得以 float 进入响应
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in value]
    if hasattr(value, "model_dump"):
        return jsonable(value.model_dump())
    return str(value)


def now_as_of() -> datetime:
    return datetime.now(UTC)


def scope_of(context: McpContext) -> dict[str, Any]:
    """行级范围描述：只由服务端注入的 actor 推导，模型不可指定。"""
    actor = context.actor
    profile = actor.profile
    return {
        "role": actor.role_code,
        "row_scope": profile.row_scope,
        "is_global": profile.row_scope in ("global", "global_readonly"),
        "region_ids": sorted(str(r) for r in actor.region_ids),
        "brand_ids": sorted(str(b) for b in actor.brand_ids),
        "data_scope": context.data_scope or "PRODUCTION",
    }


def record_link(object_type: str, object_id: Any) -> dict[str, Any]:
    """业务记录链接：对象类型 + 对象 ID + 仍受鉴权的页面路径。"""
    kind = str(object_type).upper()
    oid = str(object_id)
    template = _RECORD_ROUTES.get(kind)
    return {
        "object_type": kind,
        "object_id": oid,
        "path": template.format(id=oid) if template else None,
    }


def list_link(object_type: str) -> dict[str, Any]:
    kind = str(object_type).upper()
    return {"object_type": kind, "object_id": None, "path": _LIST_ROUTES.get(kind)}


def risk_links(object_type: str, object_id: Any) -> list[dict[str, Any]]:
    """风险项回链：指向被风险标记的业务对象 + 风险清单页。"""
    links: list[dict[str, Any]] = []
    kind = str(object_type).upper()
    if kind in _RISK_OBJECT_TYPES:
        links.append(record_link(kind, object_id))
    links.append(list_link("RISK"))
    return links


def _base(
    context: McpContext,
    *,
    definition: str,
    result_state: str,
    as_of: datetime | str | None,
    links: list[dict[str, Any]],
    note: str | None,
    extra: Mapping[str, Any] | None,
) -> dict[str, Any]:
    stamp = as_of or now_as_of()
    if isinstance(stamp, str):
        stamp_utc: Any = stamp
        stamp_display: Any = stamp
    else:
        stamp_utc = stamp.astimezone(UTC).isoformat()
        stamp_display = stamp.astimezone(SHANGHAI_TZ).isoformat()
    payload: dict[str, Any] = {
        "result_state": result_state,
        "scope": scope_of(context),
        "definition": definition,
        "as_of": stamp_utc,
        "as_of_display": stamp_display,
        "links": links,
        "note": note,
    }
    if extra:
        payload.update({k: jsonable(v) for k, v in extra.items()})
    return payload


def list_envelope(
    context: McpContext,
    *,
    definition: str,
    items: Sequence[Any],
    links: list[dict[str, Any]] | None = None,
    as_of: datetime | str | None = None,
    note: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    rows = [jsonable(item) for item in items]
    payload = _base(
        context,
        definition=definition,
        result_state=RESULT_OK if rows else RESULT_NO_RECORDS,
        as_of=as_of,
        links=list(links or []),
        note=note,
        extra=extra,
    )
    payload["items"] = rows
    payload["count"] = len(rows)
    return payload


def record_envelope(
    context: McpContext,
    *,
    definition: str,
    record: Any,
    links: list[dict[str, Any]] | None = None,
    as_of: datetime | str | None = None,
    note: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = _base(
        context,
        definition=definition,
        result_state=RESULT_OK,
        as_of=as_of,
        links=list(links or []),
        note=note,
        extra=extra,
    )
    payload["record"] = jsonable(record)
    return payload


def no_records(
    context: McpContext,
    *,
    definition: str,
    reason_code: str,
    message: str,
    links: list[dict[str, Any]] | None = None,
    as_of: datetime | str | None = None,
) -> dict[str, Any]:
    """有权访问但无记录：明确「无记录」，与「无权限」区分。"""
    payload = _base(
        context,
        definition=definition,
        result_state=RESULT_NO_RECORDS,
        as_of=as_of,
        links=list(links or []),
        note=message,
        extra={"reason_code": reason_code},
    )
    payload["items"] = []
    payload["count"] = 0
    return payload


def no_permission(
    context: McpContext,
    *,
    definition: str,
    reason_code: str,
    message: str,
    links: list[dict[str, Any]] | None = None,
    as_of: datetime | str | None = None,
) -> dict[str, Any]:
    """行级/字段级权限不允许：明确「无权限」，不返回空列表冒充「无记录」。"""
    payload = _base(
        context,
        definition=definition,
        result_state=RESULT_NO_PERMISSION,
        as_of=as_of,
        links=list(links or []),
        note=message,
        extra={"reason_code": reason_code},
    )
    payload["items"] = []
    payload["count"] = 0
    return payload


def permission_envelope(
    context: McpContext,
    *,
    definition: str,
    exc: DomainError,
    links: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return no_permission(
        context,
        definition=definition,
        reason_code=exc.code,
        message=exc.message,
        links=links,
    )


# ===== 入参辅助（只做类型转换，不做权限判定）=====


def uuid_arg(args: Mapping[str, Any], key: str) -> UUID:
    try:
        return UUID(str(args.get(key)))
    except (TypeError, ValueError) as exc:
        raise DomainError(
            code="MCP_ARGUMENT_INVALID",
            message="参数不是合法 UUID",
            details={"argument": key},
        ) from exc


def date_arg(args: Mapping[str, Any], key: str) -> date | None:
    raw = args.get(key)
    if raw in (None, ""):
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    try:
        return date.fromisoformat(str(raw))
    except ValueError as exc:
        raise DomainError(
            code="MCP_ARGUMENT_INVALID",
            message="参数不是合法日期（应为 YYYY-MM-DD）",
            details={"argument": key},
        ) from exc


def limit_arg(
    args: Mapping[str, Any], *, default: int = 20, maximum: int = 100
) -> int:
    raw = args.get("limit")
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise DomainError(
            code="MCP_ARGUMENT_INVALID",
            message="limit 必须是整数",
            details={"argument": "limit"},
        ) from exc
    return max(1, min(value, maximum))

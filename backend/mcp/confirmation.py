"""结构化确认令牌签发与消费（T24 / PRD §5.2–§5.4）。

两阶段受控写入：

1. 写工具首次调用 → 只产出 `execution_preview` 并由本模块**签发**一次性令牌；
   自由文本“确认”不构成确认，必须由结构化确认 UI 携带令牌二次提交。
2. 二次调用 → 网关**消费**令牌：逐要素校验绑定（actor/session/tool/参数
   hash/幂等键）、有效期、一次性，全部通过才调用对应受控 Service。

令牌原文（`secrets.token_urlsafe`）只在签发响应中下发一次，库内只存其
SHA-256 摘要；业务参数摘要与幂等内核 `hash_params` 同算法（规范化 JSON）。

本模块在函数内惰性导入 ORM/DB，保证 `import mcp` 仍不拉入 SQLAlchemy；
绑定/校验的纯函数部分不触库，可直接单测（Q136/Q137）。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from app.core.config import get_settings
from mcp.errors import (
    McpConfirmationConsumedError,
    McpConfirmationExpiredError,
    McpConfirmationInvalidError,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# 绑定要素不匹配原因码（details.reason_code），供结构化 UI 精确提示
REASON_TOKEN_MISSING = "TOKEN_MISSING"
REASON_TOKEN_MALFORMED = "TOKEN_MALFORMED"
REASON_ACTOR_MISMATCH = "ACTOR_MISMATCH"
REASON_SESSION_MISMATCH = "SESSION_MISMATCH"
REASON_TOOL_MISMATCH = "TOOL_MISMATCH"
REASON_PARAMS_MISMATCH = "PARAMS_MISMATCH"
REASON_IDEMPOTENCY_KEY_MISMATCH = "IDEMPOTENCY_KEY_MISMATCH"
REASON_NOT_FOUND = "TOKEN_NOT_FOUND"

_PREVIEW_KIND = "execution_preview"


def _now() -> datetime:
    return datetime.now(tz=UTC)


def hash_params(params: Any) -> str:
    """业务参数摘要：与幂等内核一致的规范化 JSON SHA-256。"""
    import json

    canonical = json.dumps(params, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def hash_token(raw_token: str) -> str:
    """令牌原文 → SHA-256 摘要（库内不存明文）。"""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _invalid(tool_name: str, reason: str, message: str) -> McpConfirmationInvalidError:
    return McpConfirmationInvalidError(tool_name, reason, message)


@dataclass(frozen=True)
class ConfirmationBinding:
    """令牌与本次执行必须一致的绑定要素（Q136 逐要素校验依据）。"""

    actor_id: UUID
    session_id: str
    tool_name: str
    params_hash: str
    idempotency_key: str

    def mismatches(self, other: ConfirmationBinding) -> list[str]:
        """返回不一致的要素原因码列表；空列表表示完全一致。"""
        diffs: list[str] = []
        if self.actor_id != other.actor_id:
            diffs.append(REASON_ACTOR_MISMATCH)
        if self.session_id != other.session_id:
            diffs.append(REASON_SESSION_MISMATCH)
        if self.tool_name != other.tool_name:
            diffs.append(REASON_TOOL_MISMATCH)
        if not hmac.compare_digest(self.params_hash, other.params_hash):
            diffs.append(REASON_PARAMS_MISMATCH)
        if self.idempotency_key != other.idempotency_key:
            diffs.append(REASON_IDEMPOTENCY_KEY_MISMATCH)
        return diffs


_MISMATCH_MESSAGES: dict[str, str] = {
    REASON_ACTOR_MISMATCH: "确认令牌绑定的发起人与当前用户不一致，请重新预览确认",
    REASON_SESSION_MISMATCH: "确认令牌绑定的会话与当前会话不一致，请在同一会话内确认",
    REASON_TOOL_MISMATCH: "确认令牌不能用于其他工具，请重新预览确认",
    REASON_PARAMS_MISMATCH: "业务参数在确认后发生变化，请核对后重新预览确认",
    REASON_IDEMPOTENCY_KEY_MISMATCH: "确认令牌绑定的幂等键与本次请求不一致",
}


class ConfirmationService:
    """签发/消费结构化确认令牌（与业务执行同 session 同事务）。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ----------------------------- 签发 -----------------------------

    def issue(
        self,
        *,
        binding: ConfirmationBinding,
        preview: dict[str, Any],
        request_id: str = "",
    ) -> tuple[str, datetime]:
        """为执行预览签发一次性令牌，返回 (令牌原文, 过期时间)；令牌仅此次可见。

        与最终执行同库同事务边界：预览不落业务表，仅落令牌记录。
        """
        from app.models import McpConfirmationToken

        ttl = get_settings().mcp_confirmation_ttl_seconds
        issued_at = _now()
        expires_at = issued_at + timedelta(seconds=ttl)
        raw_token = secrets.token_urlsafe(32)

        record = McpConfirmationToken(
            tool_name=binding.tool_name,
            actor_id=binding.actor_id,
            session_id=binding.session_id,
            params_hash=binding.params_hash,
            token_hash=hash_token(raw_token),
            idempotency_key=binding.idempotency_key,
            preview=preview,
            status="ISSUED",
            issued_at=issued_at,
            expires_at=expires_at,
            request_id=request_id or None,
        )
        self.session.add(record)
        self.session.flush()
        return raw_token, expires_at

    # ----------------------------- 消费 -----------------------------

    def consume(
        self,
        *,
        binding: ConfirmationBinding,
        raw_token: str | None,
    ) -> dict[str, Any]:
        """校验并消费令牌；通过后返回签发时保存的执行预览快照。

        任何不满足都抛 McpConfirmation* 领域异常，绝不执行写入。
        """
        from sqlalchemy import select

        from app.models import McpConfirmationToken

        tool_name = binding.tool_name
        if not raw_token or not raw_token.strip():
            raise _invalid(tool_name, REASON_TOKEN_MISSING, "缺少结构化确认令牌")
        raw_token = raw_token.strip()
        if len(raw_token) < 16:
            raise _invalid(tool_name, REASON_TOKEN_MALFORMED, "确认令牌格式非法")

        # 令牌原文为高熵随机串，其 SHA-256 全局唯一：只按 token_hash 定位记录，
        # 工具归属作为绑定要素在下方 mismatches 中裁决。若用 (tool, token_hash)
        # 联合查找，跨工具复用令牌会先报 TOKEN_NOT_FOUND，无法精确给出
        # TOOL_MISMATCH（Q136 要求逐要素判定并返回对应原因码）。
        stmt = (
            select(McpConfirmationToken)
            .where(McpConfirmationToken.token_hash == hash_token(raw_token))
            .with_for_update()
        )
        record = self.session.execute(stmt).scalar_one_or_none()
        if record is None:
            raise _invalid(tool_name, REASON_NOT_FOUND, "确认令牌不存在")

        # 一次性优先于过期判定：已消费令牌复用直接拒绝（Q137）
        if record.status == "CONSUMED" or record.consumed_at is not None:
            raise McpConfirmationConsumedError(tool_name)
        if record.expires_at <= _now():
            raise McpConfirmationExpiredError(tool_name)

        stored = ConfirmationBinding(
            actor_id=record.actor_id,
            session_id=record.session_id,
            tool_name=record.tool_name,
            params_hash=record.params_hash,
            idempotency_key=record.idempotency_key,
        )
        diffs = stored.mismatches(binding)
        if diffs:
            reason = diffs[0]
            raise _invalid(tool_name, reason, _MISMATCH_MESSAGES[reason])

        # 消费：原子置位；同一事务内随后执行业务 Service
        record.status = "CONSUMED"
        record.consumed_at = _now()
        self.session.flush()
        return dict(record.preview or {})


def build_preview(
    *,
    tool_name: str,
    title: str,
    arguments: dict[str, Any],
    before_state: dict[str, Any] | None = None,
    after_state: dict[str, Any] | None = None,
    conflicts: list[dict[str, Any]] | None = None,
    approval_chain: dict[str, Any] | None = None,
    risks: list[dict[str, Any]] | None = None,
    executor: str = "",
    final_state: tuple[str, ...] = (),
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造统一的 `execution_preview` 载荷（对象/字段/前后状态/冲突/审批链/风险）。

    纯数据组装，不访问数据库；字段值由各工具适配器依据受控 Service 的只读
    结论填充，适配器不自行重算业务规则。
    """
    preview: dict[str, Any] = {
        "kind": _PREVIEW_KIND,
        "tool": tool_name,
        "title": title,
        "executor": executor,
        "object": {"tool": tool_name},
        "fields": [
            {"name": key, "value": value}
            for key, value in sorted(arguments.items(), key=lambda kv: kv[0])
        ],
        "before_state": before_state or {},
        "after_state": after_state or {},
        "expected_final_state": list(final_state),
        "conflicts": conflicts or [],
        "approval_chain": approval_chain or {},
        "risks": risks or [],
    }
    if extra:
        preview.update(extra)
    return preview


def is_execution_preview(payload: dict[str, Any]) -> bool:
    return payload.get("kind") == _PREVIEW_KIND

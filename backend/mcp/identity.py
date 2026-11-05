"""受信身份注入（T22 / PRD §5.4.1、§2.4）。

红线：
- `actor` / `role` / `region` / `brand` / `dataset` 等**权限字段只能由服务端
  JWT/session 注入**；模型（工具参数）自行指定的同类字段一律**忽略**，
  绝不进入 Handler，也绝不改变行级范围；
- 工具的输入 Schema **不得声明**这些字段（注册期校验，见 `registry`）。

本模块只做「参数净化 + 受信上下文承载」，不做任何数据库访问。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.core.permissions import ActorContext

# 精确匹配的保留参数名（小写比较）
RESERVED_IDENTITY_PARAMS: frozenset[str] = frozenset(
    {
        "actor",
        "actor_id",
        "user",
        "user_id",
        "role",
        "role_code",
        "role_id",
        "roles",
        "region",
        "region_id",
        "region_ids",
        "region_code",
        "brand",
        "brand_id",
        "brand_ids",
        "brand_code",
        "dataset",
        "dataset_id",
        "dataset_type",
        "tenant_id",
        "org_id",
        "organization_id",
        "is_admin",
        "is_superuser",
        "admin",
        "permission",
        "permissions",
        "abilities",
        "ability",
        "scope",
        "data_scope",
        "row_scope",
        "session_id",
        "token",
        "access_token",
        "authorization",
    }
)

# 归一化参数名前缀：命中即视为权限字段（如 actor_role、region_scope）
RESERVED_IDENTITY_PREFIXES: tuple[str, ...] = (
    "actor",
    "role",
    "region",
    "brand",
    "dataset",
    "tenant",
    "permission",
    "abilit",
    "is_admin",
    "auth",
    "session",
)

# 数据范围参数（PRD R34：正式口径必须显式 PRODUCTION；由服务端注入，不接受模型指定）
RESERVED_SCOPE_PARAM = "data_scope"
# 正式口径（本包自带字面量，避免从 app.services 反向依赖）
PRODUCTION_SCOPE = "PRODUCTION"


def normalize_param_name(name: str) -> str:
    """参数名归一化：小写 + 去空白，便于比对保留字段。"""
    return name.strip().lower()


def is_reserved_identity_param(name: str) -> bool:
    """是否为权限/身份字段（模型不得指定）。"""
    normalized = normalize_param_name(name)
    if normalized in RESERVED_IDENTITY_PARAMS:
        return True
    return any(normalized.startswith(prefix) for prefix in RESERVED_IDENTITY_PREFIXES)


@dataclass(frozen=True)
class McpContext:
    """受信调用上下文：身份/权限/会话全部由服务端注入。

    `session` 为**不透明**对象（由适配器传入的数据库会话或 None），
    `mcp` 包自身不导入 DB 层、不持有连接。
    """

    actor: ActorContext
    session: Any | None = None
    request_id: str = ""
    session_id: str = ""
    client_kind: str = "unknown"
    # 数据域：由服务端上下文提供；声明 requires_data_scope 的工具只接受 PRODUCTION
    data_scope: str | None = None
    confirmed: bool = False  # 结构化确认 UI 已签发（T24 落地 token 校验）
    confirmation_ref: str = ""

    @property
    def user_id(self) -> UUID:
        return self.actor.user_id

    @property
    def role_code(self) -> str:
        return self.actor.role_code

    @classmethod
    def from_actor(
        cls,
        actor: ActorContext,
        *,
        session: Any | None = None,
        request_id: str = "",
        session_id: str = "",
        client_kind: str = "mcp",
        data_scope: str | None = PRODUCTION_SCOPE,
    ) -> McpContext:
        """由**服务端**已解析的 ActorContext 构造上下文（唯一入口）。"""
        return cls(
            actor=actor,
            session=session,
            request_id=request_id,
            session_id=session_id,
            client_kind=client_kind,
            data_scope=data_scope,
        )


# 结构化确认信封字段（由网关在写工具上识别，不参与行级范围）
CONFIRMATION_FIELD = "confirmation"


@dataclass(frozen=True)
class ConfirmationEnvelope:
    """结构化确认 UI 二次提交携带的确认信封（T24）。"""

    confirmed: bool = False
    token: str = ""

    @property
    def is_structured(self) -> bool:
        """只有结构化确认 UI 明确 confirmed=true 才成立；自由文本“确认”无法构造本对象。"""
        return self.confirmed is True


@dataclass(frozen=True)
class SanitizedArguments:
    """净化后的入参：业务参数与「被忽略的权限字段」分离。"""

    business: dict[str, Any] = field(default_factory=dict)
    ignored_identity_params: tuple[str, ...] = ()
    confirmation: ConfirmationEnvelope = field(default_factory=ConfirmationEnvelope)

    @property
    def ignored_count(self) -> int:
        return len(self.ignored_identity_params)


def _parse_confirmation(value: Any) -> ConfirmationEnvelope:
    if not isinstance(value, Mapping):
        return ConfirmationEnvelope()
    confirmed = value.get("confirmed") is True
    raw_token = value.get("token")
    token = raw_token.strip() if isinstance(raw_token, str) else ""
    return ConfirmationEnvelope(confirmed=confirmed, token=token)


def sanitize_arguments(arguments: Mapping[str, Any] | None) -> SanitizedArguments:
    """剥离模型自行指定的权限字段，只保留业务参数。

    被剥离的字段**不进入** Handler，也不影响行级范围（伪造 region 被忽略）。
    结构化确认信封（`confirmation`）单独解析为 `ConfirmationEnvelope`，不落入
    业务参数，业务 Service 永远看不到该字段。
    """
    if not arguments:
        return SanitizedArguments()
    business: dict[str, Any] = {}
    ignored: list[str] = []
    confirmation = ConfirmationEnvelope()
    for raw_key, value in arguments.items():
        key = str(raw_key)
        if is_reserved_identity_param(key):
            ignored.append(key)
            continue
        if key == CONFIRMATION_FIELD:
            confirmation = _parse_confirmation(value)
            continue
        business[key] = value
    return SanitizedArguments(
        business=business,
        ignored_identity_params=tuple(ignored),
        confirmation=confirmation,
    )

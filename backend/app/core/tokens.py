"""Token 服务：access/refresh JWT 签发、校验、撤销。

设计原则（PRD §2.1）：
- access token 短期；refresh token 长期但立即可被踢；
- 角色变更 / 密码修改通过 `password_version` 提升，使该用户所有旧 token 失效；
- 撤销名单存内存（黑名单 `jti` + 绑定 user_id）；
- 一次性激活凭据 / 密码重置凭据 走同套签发，但用 `kind=ACTIVATION/PASSWORD_RESET`
  并在消费时强制一次性校验。

T04 将撤销名单与计数器迁到 Redis，接口与调用方不变。
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Literal, overload
from uuid import UUID

import jwt
from pydantic import SecretStr

from app.core.config import Settings, get_settings

# 撤销名单：双索引
# - 显式吊销的 jti（黑名单），用于登出 / 主动封禁
# - 已升高的 password_version：超过签发时版本即视为失效
_TOKEN_KIND = Literal["ACCESS", "REFRESH", "ACTIVATION", "PASSWORD_RESET"]


@dataclass
class _TokenClaims:
    user_id: UUID
    username: str
    role_code: str
    password_version: int
    kind: str
    jti: str
    iat: int
    exp: int


class TokenError(Exception):
    """Token 校验失败。"""


class TokenRevocationStore:
    """进程内撤销名单。

    生产必须换 Redis；接口与本类保持一致。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._revoked_jti: set[str] = set()
        # user_id -> 当前已吊销的最大 password_version
        self._user_pw_version: dict[UUID, int] = {}

    def revoke_jti(self, jti: str) -> None:
        with self._lock:
            self._revoked_jti.add(jti)

    def bump_user_version(self, user_id: UUID, new_version: int) -> None:
        with self._lock:
            cur = self._user_pw_version.get(user_id, 0)
            if new_version > cur:
                self._user_pw_version[user_id] = new_version

    def is_revoked(self, *, jti: str, user_id: UUID, password_version: int) -> bool:
        with self._lock:
            if jti in self._revoked_jti:
                return True
            min_required = self._user_pw_version.get(user_id, 0)
            return password_version < min_required

    def reset(self) -> None:
        """测试夹具用：清空撤销状态。"""
        with self._lock:
            self._revoked_jti.clear()
            self._user_pw_version.clear()


token_revocation = TokenRevocationStore()


def _secret_bytes(settings: Settings) -> bytes:
    raw: SecretStr = settings.secret_key
    return raw.get_secret_value().encode("utf-8")


def _ttl_seconds(kind: _TOKEN_KIND, settings: Settings) -> int:
    if kind == "ACCESS":
        return settings.access_token_ttl_seconds
    if kind == "REFRESH":
        return settings.refresh_token_ttl_seconds
    if kind == "ACTIVATION":
        return settings.activation_ttl_seconds
    if kind == "PASSWORD_RESET":
        return settings.password_reset_ttl_seconds
    raise ValueError(f"unknown token kind: {kind}")


@overload
def issue_token(
    *,
    user_id: UUID,
    username: str,
    role_code: str,
    password_version: int,
    kind: Literal["ACCESS"],
    settings: Settings | None = None,
) -> str: ...


@overload
def issue_token(
    *,
    user_id: UUID,
    username: str,
    role_code: str,
    password_version: int,
    kind: Literal["REFRESH"],
    settings: Settings | None = None,
) -> str: ...


@overload
def issue_token(
    *,
    user_id: UUID,
    username: str,
    role_code: str,
    password_version: int,
    kind: Literal["ACTIVATION", "PASSWORD_RESET"],
    settings: Settings | None = None,
) -> str: ...


def issue_token(
    *,
    user_id: UUID,
    username: str,
    role_code: str,
    password_version: int,
    kind: _TOKEN_KIND,
    settings: Settings | None = None,
) -> str:
    settings = settings or get_settings()
    now = int(time.time())
    ttl = _ttl_seconds(kind, settings)
    jti = uuid.uuid4().hex
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role_code,
        "pv": password_version,
        "kind": kind,
        "jti": jti,
        "iat": now,
        "exp": now + ttl,
    }
    return jwt.encode(payload, _secret_bytes(settings), algorithm="HS256")


def decode_token(token: str, *, expected_kind: _TOKEN_KIND | None = None) -> _TokenClaims:
    """校验 token 签名、有效期、撤销状态；失败抛 TokenError。"""
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            _secret_bytes(settings),
            algorithms=["HS256"],
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise TokenError("INVALID_TOKEN") from exc

    try:
        user_id = UUID(str(payload["sub"]))
    except (KeyError, ValueError) as exc:
        raise TokenError("INVALID_TOKEN") from exc

    kind = str(payload.get("kind", ""))
    if expected_kind is not None and kind != expected_kind:
        raise TokenError("WRONG_TOKEN_KIND")
    if kind not in {"ACCESS", "REFRESH", "ACTIVATION", "PASSWORD_RESET"}:
        raise TokenError("INVALID_TOKEN")

    pv = int(payload.get("pv", 0))
    jti = str(payload.get("jti", ""))
    if token_revocation.is_revoked(jti=jti, user_id=user_id, password_version=pv):
        raise TokenError("TOKEN_REVOKED")

    return _TokenClaims(
        user_id=user_id,
        username=str(payload.get("username", "")),
        role_code=str(payload.get("role", "")),
        password_version=pv,
        kind=kind,
        jti=jti,
        iat=int(payload.get("iat", 0)),
        exp=int(payload.get("exp", 0)),
    )


def generate_one_time_credential() -> str:
    """生成一次性凭据（激活 / 密码重置）。

    使用 128 bit URL-safe 随机串；首登后强制改密。
    """
    return secrets.token_urlsafe(24)

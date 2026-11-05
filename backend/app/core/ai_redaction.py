"""AI 交互脱敏（T26 / PRD §5.4-6、§9.4）。

用于写入 ai_interaction_logs 前的问题文本与工具参数摘要：
- 密码 / token / secret / 凭据键值与 JWT / Argon2 → REDACTED；
- 完整手机号 → 138****1234（掩码，不保留完整电话）；
- 精确坐标（经纬度数字、WKT POINT）→ 粗化或剔除，不保留精确坐标；
- 不保留原始问题原文（PRD：原始问题默认不落库），只保存脱敏后文本。

与 `app.core.logging` 的区别：本模块额外处理自由文本里的精确坐标，并产出
"参数摘要"（键名保留、敏感值掩码、长值截断），供审计人核对"调了什么"。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from app.core.logging import REDACTED, sanitize_message

# 中文/自然语言里的密码、凭据陈述（不依赖 key= 形式）：
# 「密码是 hunter2」「验证码：123456」「secret 为 xyz」
_CN_CREDENTIAL_RE = re.compile(
    r"(?:密码|口令|验证码|动态码|密钥|secret\s*(?:is|:|为|是))"
    r"[^A-Za-z0-9]{0,4}([A-Za-z0-9!@#$%^&*_.\-]{4,})",
    flags=re.IGNORECASE,
)

# 命中即整体掩码的参数键（小写比较）
_SECRET_KEYS = frozenset(
    {
        "password", "passwd", "pwd", "old_password", "new_password",
        "password_hash", "secret", "secret_key", "access_key", "api_key",
        "token", "access_token", "refresh_token", "authorization", "credential",
        "credentials",
    }
)
# 精确坐标键：不保留精确值（PRD §9.4），统一替换为占位
_COORD_KEYS = frozenset(
    {
        "lng", "lat", "longitude", "latitude", "location", "location_wkt",
        "merchant_location", "merchant_location_wkt", "gps", "coordinate",
        "coordinates", "geom",
    }
)
_PHONE_KEYS = frozenset({"phone", "contact_phone", "mobile", "telephone"})

# 自由文本中的坐标：
# 1) 十进制度数键值对  lng=121.4737 / lat: 31.2304（保留 1 位小数做粗化）
_LNG_RE = re.compile(r"(?i)\b(lng|longitude)(\s*[=:]\s*)(-?\d{1,3}(?:\.\d+)?)")
_LAT_RE = re.compile(r"(?i)\b(lat|latitude)(\s*[=:]\s*)(-?\d{1,2}(?:\.\d+)?)")
# 2) WKT：POINT(121.4737 31.2304)
_WKT_RE = re.compile(r"(?i)POINT\s*\(\s*-?\d+(?:\.\d+)?\s+-?\d+(?:\.\d+)?\s*\)")

# 参数摘要：字符串值最大保留长度（超出截断，避免把整段外部文本塞进审计）
_VALUE_PREVIEW_LEN = 120
_PROMPT_MAX_LEN = 2000

COARSE_LOCATION = "<location:redacted>"


def _coarse_degree(raw: str) -> str:
    """坐标粗化到 0.1 度（约 10km 量级），不保留精确坐标。"""
    try:
        return f"{round(float(raw), 1)}"
    except ValueError:
        return COARSE_LOCATION


def redact_prompt(text: Any) -> str:
    """脱敏自由问题/外部内容：凭据 + 电话 + 精确坐标；超长截断。

    外部备注/合同/OCR 文本都应过本函数后才允许进审计。
    """
    if text is None:
        return ""
    s = str(text)
    # 复用日志脱敏：凭据键值、JWT、Argon2、手机号
    s = sanitize_message(s)
    # 中文/自然语言里的密码陈述：「密码是 hunter2」
    s = _CN_CREDENTIAL_RE.sub(lambda m: m.group(0).replace(m.group(1), REDACTED), s)
    # 精确坐标：WKT 整体剔除；经纬度粗化（不保留精确值）
    s = _WKT_RE.sub(COARSE_LOCATION, s)
    s = _LNG_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_coarse_degree(m.group(3))}", s)
    s = _LAT_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_coarse_degree(m.group(3))}", s)
    if len(s) > _PROMPT_MAX_LEN:
        s = s[:_PROMPT_MAX_LEN] + "…<truncated>"
    return s


def _preview(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= _VALUE_PREVIEW_LEN else (
            value[:_VALUE_PREVIEW_LEN] + "…"
        )
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    # Decimal 是金额/箱数业务类型：转字符串保留精度（不转 float，避免误差）；
    # 审计参数摘要仅用于核对“调了什么”，不参与计算。
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        return value
    if isinstance(value, Mapping):
        return summarize_params(value)
    if isinstance(value, (list, tuple)):
        return [_preview(v) for v in value[:10]]
    return str(value)[:_VALUE_PREVIEW_LEN]


def summarize_params(params: Mapping[str, Any] | None) -> dict[str, Any]:
    """生成工具参数摘要：键名保留（用于审计调了什么），敏感值掩码/剔除。"""
    if not params:
        return {}
    out: dict[str, Any] = {}
    for key, value in params.items():
        k = str(key)
        kl = k.lower()
        if kl in _SECRET_KEYS:
            out[k] = REDACTED
            continue
        if kl in _COORD_KEYS:
            out[k] = COARSE_LOCATION
            continue
        if kl in _PHONE_KEYS and isinstance(value, str):
            from app.core.redaction import mask_phone

            out[k] = mask_phone(value)
            continue
        out[k] = _preview(value)
    return out

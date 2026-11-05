"""T07 工作簿解析器（只读，不碰数据库）。

支持三类工作簿：
1. 用户提供的《品牌与账号数据.xlsx》：
   - 品牌与产品（34 SKU，价格为「元」）、账号数据（**含必须忽略的登录密码列**）、
     角色权限（仅差异预览）、说明；
2. 《初始化夹具与补充演示数据.xlsx》：
   - 主数据SKU（价格已为「分」，sku_code 稳定）、主数据账号、角色映射、片区、
     商家DEMO、合同DEMO、冰柜DEMO、补货/调拨/排班（仅引用与边界校验）；
3. 用户常见的《商家数据》中文表（商家建档用）：
   - 列：商家编码 / 商家名称 / 片区 / 品牌 / 地址 / 联系电话 / 主要联系人，
     可选 状态 / 等级 / 负责人账号 / 经度 / 纬度 / 商家类型；
     片区写中文名（金水片区…）时按演示片区映射为编码。

安全红线：
- 「登录密码 / 密码 / password」列在表头识别后**整列不读取**，绝不进入 DTO、日志、
  批次 payload 或返回结构；忽略动作记录到 ignored_columns 供审计。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from app.core.errors import DomainError
from app.models.enums import RoleCode
from app.services.master_data.schemas import (
    ParsedAccount,
    ParsedContract,
    ParsedFreezer,
    ParsedMerchant,
    ParsedRegion,
    ParsedReplenishment,
    ParsedRolePermission,
    ParsedSku,
    ParsedWorkbook,
    ReferentialRow,
)

# 源角色中文 → 受控 role_code（PRD §2.2 六类角色锚）
ROLE_LABEL_TO_CODE: dict[str, str] = {
    "业务员": RoleCode.SALES_REP.value,
    "经理": RoleCode.REGIONAL_MANAGER.value,
    "管理员/督导": RoleCode.SUPERVISOR.value,
    "督导": RoleCode.SUPERVISOR.value,
    "财务": RoleCode.FINANCE.value,
    "系统管理员": RoleCode.SYSTEM_ADMIN.value,
    "厂商": RoleCode.SUPPLIER.value,
}

# 夹具「角色映射 / 账号」表里的简写 role_code → 受控枚举
ROLE_SHORT_TO_CODE: dict[str, str] = {
    "SALES": RoleCode.SALES_REP.value,
    "REGION_MANAGER": RoleCode.REGIONAL_MANAGER.value,
    "SUPERVISOR": RoleCode.SUPERVISOR.value,
    "FINANCE": RoleCode.FINANCE.value,
    "SYS_ADMIN": RoleCode.SYSTEM_ADMIN.value,
    "SYSTEM_ADMIN": RoleCode.SYSTEM_ADMIN.value,
    "SUPPLIER": RoleCode.SUPPLIER.value,
}

# 全局/跨片区角色（region_scope="*"）：不绑定具体片区
GLOBAL_SCOPE_ROLE_CODES = frozenset(
    {
        RoleCode.REGIONAL_MANAGER.value,
        RoleCode.SUPERVISOR.value,
        RoleCode.FINANCE.value,
        RoleCode.SYSTEM_ADMIN.value,
    }
)

# 必须整列忽略的密码类列名（精确匹配，去空格后比较）
PASSWORD_COLUMN_KEYWORDS = ("登录密码", "密码", "password", "初始密码", "口令")

# 用户工作簿常用的片区中文名 → 演示片区编码（与《初始化夹具》「片区」表一致）
CN_REGION_NAME_TO_CODE: dict[str, str] = {
    "金水片区": "DEMO-JS",
    "二七片区": "DEMO-EQ",
    "中原片区": "DEMO-ZY",
    "管城片区": "DEMO-GC",
    "惠济片区": "DEMO-HJ",
}

# 反向表：标准演示片区编码 → 中文名。工作簿不自带「片区」表时，服务层按需补建这些片区
DEMO_REGION_NAME_BY_CODE: dict[str, str] = {
    code: name for name, code in CN_REGION_NAME_TO_CODE.items()
}

# 仅做引用/边界校验、不在 T07 落库的补充业务表
REFERENTIAL_SHEETS = (
    "商家调拨DEMO",
    "排班DEMO",
    "地推DEMO",
    "绩效规则",
    "巡访DEMO",
)

# 夹具补货状态 → 受控 ReplenishmentStatus
REPLENISHMENT_STATUS_MAP = {
    "REQUESTED": "REQUESTED",
    "SUBMITTED": "REQUESTED",  # 夹具历史词：已提交待确认 → 已请求
    "CONFIRMED": "CONFIRMED",
    "CLOSED": "DELIVERED",  # 夹具历史词：已闭环 → 已送达完结
    "IN_TRANSIT": "IN_TRANSIT",
    "DELIVERED": "DELIVERED",
    "FULFILLED": "DELIVERED",  # 已全部满足 → 已送达
    "PARTIALLY_FULFULLED": "IN_TRANSIT",  # 夹具历史拼写（双 L）：部分满足 → 在途
    "PARTIALLY_FULFILLED": "IN_TRANSIT",
    "OVERDUE": "OVERDUE",
    "CANCELLED": "CANCELLED",
}


class WorkbookParseError(DomainError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(code="WORKBOOK_PARSE_ERROR", message=message, details=details)


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def yuan_to_fen(value: Any) -> int:
    """元 → 分（四舍五入到整数分）；禁止 float 传导误差，统一经 Decimal。"""
    if value is None or str(value).strip() == "":
        raise WorkbookParseError("价格为空")
    dec = Decimal(str(value).strip())
    if dec < 0:
        raise WorkbookParseError("价格不得为负", {"value": str(value)})
    return int((dec * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _to_int_fen(value: Any) -> int:
    """已经是「分」的列（夹具）→ int 校验。"""
    if value is None or str(value).strip() == "":
        raise WorkbookParseError("价格(分)为空")
    dec = Decimal(str(value).strip())
    if dec < 0:
        raise WorkbookParseError("价格不得为负", {"value": str(value)})
    return int(dec.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


_CAPACITY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*ml", re.IGNORECASE)
_CASE_RE = re.compile(r"[×x*]\s*(\d+(?:\.\d+)?)\s*(?:瓶|罐|盒|个|只)?")


def parse_spec(spec: str | None) -> tuple[int | None, Decimal]:
    """从规格原文解析容量 ml 与每箱包数。

    例："500ml/瓶×24瓶" → (500, 24)；"100ml×5/排" → (100, 5)。
    无法识别时容量为 None、箱数为 0（不阻断导入，记录 warning 由上层处理）。
    """
    if not spec:
        return None, Decimal("0")
    cap_match = _CAPACITY_RE.search(spec)
    capacity = int(float(cap_match.group(1))) if cap_match else None
    case_match = _CASE_RE.search(spec)
    case = Decimal(case_match.group(1)) if case_match else Decimal("0")
    return capacity, case


def stable_sku_code(seq: int) -> str:
    """源工作簿（无 sku_code）按序号生成稳定编码 QS-SKU-###。"""
    return f"QS-SKU-{seq:03d}"


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _clean_number(value: Any) -> str:
    """数字型单元格（手机号、编码）去掉 Excel 浮点尾巴后转文本。"""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _resolve_region_code(text: str) -> str:
    """片区中文名 → 演示片区编码；已是编码则原样返回；空/全局返回空串。

    未识别的中文名原样返回，由 MasterDataImportService 预检判为「未知片区」并拦截
    （比在这里静默丢弃更可诊断）。
    """
    text = text.strip()
    if not text or text in ("*", "全局", "跨片区", "郑州全片区"):
        return ""
    return CN_REGION_NAME_TO_CODE.get(text, text)


def _is_password_column(header: str) -> bool:
    h = header.strip().lower()
    return any(k.lower() in h for k in PASSWORD_COLUMN_KEYWORDS)


def _sheet_rows(ws: Any) -> tuple[list[str], list[tuple[int, list[Any]]]]:
    """返回 (表头列表, [(行号, 行值)])；密码类列从表头与数据中一并剔除。"""
    raw = list(ws.iter_rows(values_only=True))
    if not raw:
        return [], []
    headers = [_clean(h) for h in raw[0]]
    keep_idx = [i for i, h in enumerate(headers) if h]
    rows: list[tuple[int, list[Any]]] = []
    for rno, row in enumerate(raw[1:], start=2):
        if all(v is None or _clean(v) == "" for v in row):
            continue
        rows.append((rno, [row[i] if i < len(row) else None for i in keep_idx]))
    return [headers[i] for i in keep_idx], rows


def _row_map(headers: list[str], values: list[Any]) -> dict[str, Any]:
    return {headers[i]: values[i] if i < len(values) else None for i in range(len(headers))}


# 密码列在「完整原始表头」里识别并上报，但不进入数据映射
def _ignored_password_columns(ws: Any) -> list[str]:
    raw = list(ws.iter_rows(min_row=1, max_row=1, values_only=True))
    if not raw:
        return []
    return [_clean(h) for h in raw[0] if h and _is_password_column(_clean(h))]


def _split_scope(value: Any) -> tuple[list[str], bool]:
    """把 region_scope 解析为 (片区编码列表, 是否全局)。

    "*" / 全局 / 跨片区 / 郑州全片区 → 全局；"DEMO-JS|DEMO-EQ" → 多片区。
    """
    text = _clean(value)
    if text in ("", "*", "全局", "跨片区", "郑州全片区"):
        return [], True
    if "|" in text:
        return [p.strip() for p in text.split("|") if p.strip()], False
    return [text], False


def _parse_brand_products(ws: Any, out: ParsedWorkbook) -> None:
    """用户工作簿「品牌与产品」：价格为元，sku_code 按序号稳定生成。"""
    ignored = _ignored_password_columns(ws)
    headers, rows = _sheet_rows(ws)
    for rno, values in rows:
        m = _row_map(headers, values)
        seq_raw = _clean(m.get("序号"))
        try:
            seq = int(float(seq_raw))
        except ValueError as exc:
            raise WorkbookParseError(
                "产品序号非法", {"row": rno, "序号": seq_raw}
            ) from exc
        spec = _clean(m.get("规格"))
        capacity, case_spec = parse_spec(spec)
        brand_raw = _clean(m.get("品牌"))
        brand_code = "QINSHUANG" if "QINSHUANG" in brand_raw.upper() else brand_raw
        out.skus.append(
            ParsedSku(
                source_key=f"产品:{seq}",
                sku_code=stable_sku_code(seq),
                brand_code=brand_code,
                name=_clean(m.get("产品名称")),
                series_name=_clean(m.get("系列")) or None,
                category_name=_clean(m.get("品类")) or None,
                spec_text=spec or None,
                selling_points=_clean(m.get("口感/卖点")) or None,
                suggested_price_fen=yuan_to_fen(m.get("建议零售价(元)")),
                purchase_price_fen=yuan_to_fen(m.get("参考进价(元)")),
                shelf_life_text=_clean(m.get("保质期")) or None,
                target_channel=_clean(m.get("目标渠道")) or None,
                capacity_ml=capacity,
                case_spec=case_spec,
            )
        )
    out.ignored_columns.extend(ignored)


def _parse_accounts_source(ws: Any, out: ParsedWorkbook) -> None:
    """用户工作簿「账号数据」：忽略登录密码列；角色中文→受控枚举；片区名→编码。"""
    ignored = _ignored_password_columns(ws)
    headers, rows = _sheet_rows(ws)
    # 源工作簿用片区中文名；先收集已有/即将导入的片区名→编码
    region_name_to_code = {r.name: r.code for r in out.regions}
    # 账号表出现的片区名可能早于片区表（本工作簿无独立片区表），用固定中文映射兜底
    for rno, values in rows:
        m = _row_map(headers, values)
        label = _clean(m.get("角色"))
        role_code = ROLE_LABEL_TO_CODE.get(label)
        if role_code is None:
            raise WorkbookParseError(
                "未知角色，无法映射到受控角色枚举",
                {"row": rno, "角色": label},
            )
        scope_text = _clean(m.get("所属片区/关联"))
        region_codes: list[str] = []
        scope_global = False
        if role_code in GLOBAL_SCOPE_ROLE_CODES or scope_text in ("全局", "跨片区", "郑州全片区"):
            scope_global = True
        elif role_code == RoleCode.SUPPLIER.value:
            # 厂商关联工厂，不绑定片区；品牌授权在执行期给 QINSHUANG
            scope_global = False
        else:
            code = region_name_to_code.get(scope_text) or CN_REGION_NAME_TO_CODE.get(scope_text)
            if code is None:
                # 未知片区：整批回滚（Q036 由 service 层依据预解析集合二次校验）
                out.warnings.append(f"row {rno}: 未知片区 '{scope_text}'")
                region_codes = []
            else:
                region_codes = [code]
        is_supplier = role_code == RoleCode.SUPPLIER.value
        out.accounts.append(
            ParsedAccount(
                source_key=f"账号:{_clean(m.get('序号')) or rno}",
                login_name=_clean(m.get("登录账号")),
                display_name=_clean(m.get("姓名")),
                phone=_clean(m.get("手机号")) or None,
                source_role_label=label,
                role_code=role_code,
                region_codes=tuple(region_codes),
                brand_codes=tuple(),
                scope_global=scope_global,
                initial_active=False,  # 管理员逐个确认后才启用
                is_supplier=is_supplier,
                pending_activation=True,
            )
        )
    out.ignored_columns.extend(ignored)


def _parse_role_permissions_source(ws: Any, out: ParsedWorkbook) -> None:
    """用户工作簿「角色权限」：仅差异预览。"""
    headers, rows = _sheet_rows(ws)
    for _rno, values in rows:
        m = _row_map(headers, values)
        label = _clean(m.get("角色"))
        out.role_permissions.append(
            ParsedRolePermission(
                role_label=label,
                role_code=ROLE_LABEL_TO_CODE.get(label),
                function_permissions=_clean(m.get("功能权限")),
                data_permissions=_clean(m.get("数据权限")),
                note=_clean(m.get("备注")),
            )
        )


def _parse_fixture_sku(ws: Any, out: ParsedWorkbook) -> None:
    ignored = _ignored_password_columns(ws)
    headers, rows = _sheet_rows(ws)
    for rno, values in rows:
        m = _row_map(headers, values)
        spec = _clean(m.get("规格"))
        capacity, case_spec = parse_spec(spec)
        out.skus.append(
            ParsedSku(
                source_key=_clean(m.get("source_row_key")) or f"row:{rno}",
                sku_code=_clean(m.get("sku_code")),
                brand_code=_clean(m.get("brand_code")),
                name=_clean(m.get("产品名称")),
                series_name=_clean(m.get("系列")) or None,
                category_name=_clean(m.get("品类")) or None,
                spec_text=spec or None,
                selling_points=_clean(m.get("卖点")) or None,
                suggested_price_fen=_to_int_fen(m.get("建议零售价(分)")),
                purchase_price_fen=_to_int_fen(m.get("参考进价(分)")),
                shelf_life_text=_clean(m.get("保质期")) or None,
                target_channel=_clean(m.get("目标渠道")) or None,
                capacity_ml=capacity,
                case_spec=case_spec,
            )
        )
    out.ignored_columns.extend(ignored)


def _parse_fixture_regions(ws: Any, out: ParsedWorkbook) -> None:
    headers, rows = _sheet_rows(ws)
    for _rno, values in rows:
        m = _row_map(headers, values)
        out.regions.append(
            ParsedRegion(
                code=_clean(m.get("region_code")),
                name=_clean(m.get("片区名称")),
                city=_clean(m.get("城市")) or None,
                is_active=_clean(m.get("状态")).upper() == "ACTIVE",
            )
        )


def _parse_fixture_accounts(ws: Any, out: ParsedWorkbook) -> None:
    ignored = _ignored_password_columns(ws)
    headers, rows = _sheet_rows(ws)
    for rno, values in rows:
        m = _row_map(headers, values)
        short = _clean(m.get("role_code"))
        role_code = ROLE_SHORT_TO_CODE.get(short)
        if role_code is None:
            raise WorkbookParseError(
                "未知 role_code，无法映射受控枚举", {"row": rno, "role_code": short}
            )
        region_codes, scope_global = _split_scope(m.get("region_scope"))
        brand_raw = _clean(m.get("brand_scope"))
        brand_codes: tuple[str, ...] = ()
        if brand_raw and brand_raw != "*":
            brand_codes = tuple(p.strip() for p in brand_raw.split("|") if p.strip())
        initial_status = _clean(m.get("initial_status")).upper()
        is_supplier = role_code == RoleCode.SUPPLIER.value
        # 厂商默认待激活；DISABLED → 待管理员确认；ACTIVE 仍需管理员执行确认才启用
        pending = is_supplier or initial_status in ("DISABLED", "PENDING_ACTIVATION")
        if role_code in GLOBAL_SCOPE_ROLE_CODES:
            scope_global = True
            region_codes = []
        out.accounts.append(
            ParsedAccount(
                source_key=_clean(m.get("source_row_key")) or f"row:{rno}",
                login_name=_clean(m.get("login_name")),
                display_name=_clean(m.get("姓名")),
                phone=_clean(m.get("演示手机号")) or None,
                source_role_label=_clean(m.get("源角色")),
                role_code=role_code,
                region_codes=tuple(region_codes),
                brand_codes=brand_codes,
                scope_global=scope_global,
                initial_active=initial_status == "ACTIVE",
                is_supplier=is_supplier,
                pending_activation=pending,
            )
        )
    out.ignored_columns.extend(ignored)


def _parse_fixture_role_map(ws: Any, out: ParsedWorkbook) -> None:
    """夹具「角色映射」：与用户「角色权限」同义，仅预览。"""
    headers, rows = _sheet_rows(ws)
    for _rno, values in rows:
        m = _row_map(headers, values)
        short = _clean(m.get("role_code"))
        out.role_permissions.append(
            ParsedRolePermission(
                role_label=_clean(m.get("源角色")),
                role_code=ROLE_SHORT_TO_CODE.get(short),
                function_permissions=_clean(m.get("默认范围")),
                data_permissions=_clean(m.get("授权规则")),
                note=_clean(m.get("导入状态")),
            )
        )


def _looks_like_merchant_sheet(ws: Any) -> bool:
    """表头含「商家编码 / 商家名称」即视为商家建档表（避开「商家调拨DEMO」等）。"""
    raw = list(ws.iter_rows(min_row=1, max_row=1, values_only=True))
    if not raw:
        return False
    heads = {_clean(h) for h in raw[0] if h is not None}
    return bool({"商家编码", "商家编号", "商家名称"} & heads)


def _parse_merchant_source_zh(ws: Any, out: ParsedWorkbook) -> None:
    """用户《商家数据》中文表 → ParsedMerchant（商家建档）。

    必需：商家编码、商家名称、片区；其余列可选（缺省即空/默认）。
    """
    ignored = _ignored_password_columns(ws)
    headers, rows = _sheet_rows(ws)

    def _pick(m: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            if key in m and m[key] not in (None, ""):
                return m[key]
        return None

    for rno, values in rows:
        m = _row_map(headers, values)
        code = _clean_number(_pick(m, "商家编码", "商家编号", "merchant_code"))
        if not code:
            raise WorkbookParseError("商家编码为空", {"row": rno})
        name = _clean(_pick(m, "商家名称", "店名", "merchant_name"))
        if not name:
            raise WorkbookParseError("商家名称为空", {"row": rno, "商家编码": code})
        region_code = _resolve_region_code(
            _clean(_pick(m, "片区", "片区编码", "region_code"))
        )
        if not region_code:
            raise WorkbookParseError(
                "商家片区为空", {"row": rno, "商家编码": code}
            )
        status = _clean(_pick(m, "状态", "商家状态")).upper() or "LEAD"
        grade = _clean(_pick(m, "等级", "商家等级")).upper() or None
        longitude = _optional_float(_pick(m, "经度", "longitude"), rno, "经度", code)
        latitude = _optional_float(_pick(m, "纬度", "latitude"), rno, "纬度", code)
        out.merchants.append(
            ParsedMerchant(
                code=code,
                name=name,
                merchant_type=_clean(_pick(m, "商家类型", "类型")) or None,
                region_code=region_code,
                owner_login=_clean(_pick(m, "负责人账号", "负责人")) or None,
                status=status,
                grade=grade,
                address=_clean(_pick(m, "地址", "演示地址")) or None,
                longitude=longitude,
                latitude=latitude,
                contact_name=_clean(_pick(m, "主要联系人", "联系人")) or None,
                contact_phone=_clean_number(
                    _pick(m, "联系电话", "演示手机号", "手机号")
                )
                or None,
            )
        )
    out.ignored_columns.extend(ignored)


def _optional_float(value: Any, rno: int, label: str, code: str) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        raise WorkbookParseError(
            f"商家{label}非法", {"row": rno, "商家编码": code, label: str(value)}
        ) from None


def _parse_fixture_merchants(ws: Any, out: ParsedWorkbook) -> None:
    headers, rows = _sheet_rows(ws)
    for rno, values in rows:
        m = _row_map(headers, values)

        def _f(key: str, _m=m, _rno=rno) -> float | None:
            v = _m.get(key)
            try:
                return float(str(v).strip()) if v not in (None, "") else None
            except ValueError:
                raise WorkbookParseError(
                    "经纬度非法", {"row": _rno, key: str(v)}
                ) from None

        out.merchants.append(
            ParsedMerchant(
                code=_clean(m.get("merchant_code")),
                name=_clean(m.get("店名")),
                merchant_type=_clean(m.get("类型")) or None,
                region_code=_clean(m.get("region_code")),
                owner_login=_clean(m.get("负责人账号")) or None,
                status=_clean(m.get("状态")).upper() or "LEAD",
                grade=(_clean(m.get("等级")).upper() or None),
                address=_clean(m.get("演示地址")) or None,
                longitude=_f("经度"),
                latitude=_f("纬度"),
                contact_name=_clean(m.get("联系人")) or None,
                contact_phone=_clean(m.get("演示手机号")) or None,
            )
        )


def _parse_fixture_contracts(ws: Any, out: ParsedWorkbook) -> None:
    headers, rows = _sheet_rows(ws)
    for rno, values in rows:
        m = _row_map(headers, values)
        threshold_raw = _clean(m.get("陈列阈值"))
        try:
            # 夹具为 0.5（0-1 小数），库内为 0-100 百分比整数
            dec = Decimal(threshold_raw)
            threshold = int(dec * 100) if dec <= 1 else int(dec)
        except ValueError as exc:
            raise WorkbookParseError(
                "陈列阈值非法", {"row": rno, "陈列阈值": threshold_raw}
            ) from exc
        start = _clean(m.get("生效日")).split(" ")[0]
        end = _clean(m.get("到期日")).split(" ")[0]
        out.contracts.append(
            ParsedContract(
                code=_clean(m.get("contract_no")),
                merchant_code=_clean(m.get("merchant_code")),
                brand_code=_clean(m.get("brand_code")),
                signed_date=_clean(m.get("签订日")).split(" ")[0] or None,
                start_date=start,
                end_date=end,
                display_threshold_pct=threshold,
                status=_clean(m.get("状态")).upper(),
            )
        )


# T10（PRD §4.5 v1.2）：夹具是无在店绑定关系的台账清单，DEPLOYED 已投放冰柜在
# v1.2 必须绑定一个当前商家（DB CHECK）。导入时统一归到无需绑定的状态：
# 在投/在途/维护 → 按语义映射为在库或维修中，不臆造商家归属；真实投放走 FreezerService。
FREEZER_STATUS_MAP = {
    "IN_STOCK": "IN_STOCK",
    "IN_TRANSIT": "IN_STOCK",             # 夹具历史词：在途无绑定 → 在库
    "PENDING_ACCEPTANCE": "IN_STOCK",     # 夹具历史词：发运待验收 → 在库
    "DEPLOYED": "IN_STOCK",               # 夹具台账无商家绑定 → 在库，待真实投放
    "UNDER_MAINTENANCE": "REPAIRING",     # 维护中 → 维修中
    "REPAIRING": "REPAIRING",
    "SCRAPPED": "SCRAPPED",
}


def _parse_fixture_freezers(ws: Any, out: ParsedWorkbook) -> None:
    headers, rows = _sheet_rows(ws)
    for rno, values in rows:
        m = _row_map(headers, values)
        raw_status = _clean(m.get("状态")).upper()
        status = FREEZER_STATUS_MAP.get(raw_status)
        if status is None:
            raise WorkbookParseError(
                "冰柜状态非法", {"row": rno, "状态": raw_status}
            )
        out.freezers.append(
            ParsedFreezer(
                asset_code=_clean(m.get("asset_no")),
                brand_code=_clean(m.get("品牌")),
                model=_clean(m.get("型号")),
                purchase_date=_clean(m.get("购置日")).split(" ")[0],
                purchase_price_fen=_to_int_fen(m.get("购置金额(分)")),
                status=status,
                current_merchant_code=_clean(m.get("当前商家")) or None,
            )
        )


def _parse_fixture_replenishments(ws: Any, out: ParsedWorkbook) -> None:
    headers, rows = _sheet_rows(ws)
    for rno, values in rows:
        m = _row_map(headers, values)
        raw_status = _clean(m.get("状态")).upper()
        status = REPLENISHMENT_STATUS_MAP.get(raw_status)
        if status is None:
            raise WorkbookParseError(
                "补货状态非法", {"row": rno, "状态": raw_status}
            )

        def _boxes(key: str, _m=m, _rno=rno) -> Decimal:
            try:
                return Decimal(_clean(_m[key])).quantize(Decimal("0.01"))
            except (KeyError, ValueError) as exc:
                raise WorkbookParseError(
                    "箱数非法", {"row": _rno, key: _clean(_m.get(key))}
                ) from exc

        promised = _clean(m.get("期望到货日")).split(" ")[0] or None
        fulfilled = _boxes("已满足箱数")
        # 夹具无请求日列；以期望到货日前 3 天作为请求日，保证 promised >= request
        request_date = None
        actual_date = None
        if promised:
            promised_d = datetime.strptime(promised, "%Y-%m-%d").date()
            request_date = (promised_d - timedelta(days=3)).isoformat()
            # FULFILLED / 已全部满足视为已送达，实际到货日=期望日
            if status == "DELIVERED":
                actual_date = promised_d.isoformat()
        out.replenishments.append(
            ParsedReplenishment(
                code=_clean(m.get("request_no")),
                merchant_code=_clean(m.get("merchant_code")),
                sku_code=_clean(m.get("sku_code")),
                requested_boxes=_boxes("需求箱数"),
                committed_boxes=_boxes("确认箱数"),
                fulfilled_boxes=fulfilled,
                request_date=request_date,
                promised_date=promised,
                actual_date=actual_date,
                status=status,
            )
        )


def _parse_referential(ws_title: str, ws: Any, out: ParsedWorkbook) -> None:
    """补货/调拨/排班/地推/绩效/巡访：仅抽取外键样式列做引用完整性校验。"""
    headers, rows = _sheet_rows(ws)
    ref_columns = (
        "merchant_code",
        "sku_code",
        "source_no",
        "assignee",
        "业务员账号",
        "原负责人",
        "新负责人",
        "参与账号",
        "region_code",
    )
    for rno, values in rows:
        m = _row_map(headers, values)
        refs = {k: (_clean(m[k]) or None) for k in ref_columns if k in m}
        out.referential.append(ReferentialRow(sheet=ws_title, row_no=rno, refs=refs))


def parse_workbook(path: str | Path) -> ParsedWorkbook:
    """解析工作簿为 ParsedWorkbook；按工作表名分发。不做数据库写入。"""
    import openpyxl

    path = Path(path)
    if not path.exists():
        raise WorkbookParseError("工作簿不存在", {"path": str(path)})
    digest = sha256_file(path)
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # openpyxl 异常类型较杂，统一转领域异常
        raise WorkbookParseError(
            "工作簿无法打开（非有效 xlsx）", {"filename": path.name}
        ) from exc

    out = ParsedWorkbook(source_filename=path.name, file_hash=digest)
    # 用户手工制作/第三方导出的工作簿可能缺少或写错 <dimension>：只读模式会据此
    # 误判行数（只扫到表头），逐表重置尺寸以按实际单元格扫描全部行。
    for ws in wb.worksheets:
        reset = getattr(ws, "reset_dimensions", None)
        if callable(reset):
            reset()
    try:
        titles = set(wb.sheetnames)
        # 用户工作簿
        if "品牌与产品" in titles:
            _parse_brand_products(wb["品牌与产品"], out)
        if "账号数据" in titles:
            _parse_accounts_source(wb["账号数据"], out)
        if "角色权限" in titles:
            _parse_role_permissions_source(wb["角色权限"], out)
        # 夹具工作簿
        if "片区" in titles:
            _parse_fixture_regions(wb["片区"], out)
        if "主数据SKU" in titles:
            _parse_fixture_sku(wb["主数据SKU"], out)
        if "主数据账号" in titles:
            _parse_fixture_accounts(wb["主数据账号"], out)
        if "角色映射" in titles:
            _parse_fixture_role_map(wb["角色映射"], out)
        if "商家DEMO" in titles:
            _parse_fixture_merchants(wb["商家DEMO"], out)
        else:
            # 用户《商家数据》中文表：按表头识别，避开「商家调拨DEMO」等引用校验表
            for title in wb.sheetnames:
                if title in REFERENTIAL_SHEETS:
                    continue
                if _looks_like_merchant_sheet(wb[title]):
                    _parse_merchant_source_zh(wb[title], out)
                    break
        if "合同DEMO" in titles:
            _parse_fixture_contracts(wb["合同DEMO"], out)
        if "冰柜DEMO" in titles:
            _parse_fixture_freezers(wb["冰柜DEMO"], out)
        if "补货DEMO" in titles:
            _parse_fixture_replenishments(wb["补货DEMO"], out)
        for ref_title in REFERENTIAL_SHEETS:
            if ref_title in titles:
                _parse_referential(ref_title, wb[ref_title], out)
    finally:
        wb.close()

    # 去重 ignored_columns，保持顺序
    seen: set[str] = set()
    deduped = [c for c in out.ignored_columns if not (c in seen or seen.add(c))]
    out.ignored_columns = deduped
    return out

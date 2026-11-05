"""T07 主数据导入解析结果 DTO（纯数据，不含 ORM 会话）。

所有金额字段统一为「分」的 int；箱数为 Decimal。
**任何 DTO 都不得携带源表明文密码**——解析器在读取阶段即丢弃该列。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class ParsedSku:
    source_key: str  # 行稳定主键（如 "产品:1" 或序号）
    sku_code: str
    brand_code: str
    name: str
    series_name: str | None
    category_name: str | None
    spec_text: str | None
    selling_points: str | None
    suggested_price_fen: int
    purchase_price_fen: int | None
    shelf_life_text: str | None
    target_channel: str | None
    capacity_ml: int | None
    case_spec: Decimal


@dataclass(frozen=True)
class ParsedRegion:
    code: str
    name: str
    city: str | None
    is_active: bool = True


@dataclass(frozen=True)
class ParsedAccount:
    source_key: str
    login_name: str
    display_name: str
    phone: str | None
    source_role_label: str  # 源角色中文（业务员/经理/…），仅展示
    role_code: str  # 受控枚举锚（SALES_REP/REGIONAL_MANAGER/…）
    region_codes: tuple[str, ...]  # 授权片区编码；空元组表示全局/跨片区
    brand_codes: tuple[str, ...]  # 授权品牌编码；空元组表示全部（"*"）
    scope_global: bool  # 全局/跨片区（经理/督导/财务/系统管理员）
    initial_active: bool  # 解析初始启用建议；最终由管理员逐个确认
    is_supplier: bool
    pending_activation: bool  # 厂商默认 True；其余由 initial_status 推导
    # 注意：**没有** password 字段；源密码列在解析阶段即被丢弃。


@dataclass(frozen=True)
class ParsedRolePermission:
    """角色权限表：仅做差异预览，不覆盖代码权限矩阵（PRD §2.2 红线）。"""

    role_label: str
    role_code: str | None
    function_permissions: str
    data_permissions: str
    note: str


@dataclass(frozen=True)
class ParsedMerchant:
    code: str
    name: str
    merchant_type: str | None
    region_code: str
    owner_login: str | None
    status: str
    grade: str | None
    address: str | None
    longitude: float | None
    latitude: float | None
    contact_name: str | None
    contact_phone: str | None


@dataclass(frozen=True)
class ParsedContract:
    code: str
    merchant_code: str
    brand_code: str
    signed_date: str | None
    start_date: str
    end_date: str
    display_threshold_pct: int
    status: str


@dataclass(frozen=True)
class ParsedFreezer:
    asset_code: str
    brand_code: str
    model: str
    purchase_date: str
    purchase_price_fen: int
    status: str
    current_merchant_code: str | None


@dataclass(frozen=True)
class ParsedReplenishment:
    code: str
    merchant_code: str
    sku_code: str
    requested_boxes: Decimal
    committed_boxes: Decimal
    fulfilled_boxes: Decimal
    request_date: str | None
    promised_date: str | None
    actual_date: str | None
    status: str  # 已映射到受控 ReplenishmentStatus


@dataclass(frozen=True)
class ReferentialRow:
    """补充业务表中本卡不持久化、仅做引用/边界校验的行（补货/调拨/排班等）。"""

    sheet: str
    row_no: int
    refs: dict[str, str | None] = field(default_factory=dict)


@dataclass
class ParsedWorkbook:
    """一个工作簿的解析产物。"""

    source_filename: str
    file_hash: str
    skus: list[ParsedSku] = field(default_factory=list)
    regions: list[ParsedRegion] = field(default_factory=list)
    accounts: list[ParsedAccount] = field(default_factory=list)
    role_permissions: list[ParsedRolePermission] = field(default_factory=list)
    merchants: list[ParsedMerchant] = field(default_factory=list)
    contracts: list[ParsedContract] = field(default_factory=list)
    freezers: list[ParsedFreezer] = field(default_factory=list)
    replenishments: list[ParsedReplenishment] = field(default_factory=list)
    # 仅校验引用完整性，不在 T07 建表/落库（调拨/排班/地推/绩效/巡访）
    referential: list[ReferentialRow] = field(default_factory=list)
    # 被显式忽略的列名（安全审计用，如 "登录密码"）
    ignored_columns: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def master_rows_total(self) -> int:
        return len(self.skus) + len(self.regions) + len(self.accounts)

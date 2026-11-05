"""DEMO 账号直登播种（仅 dev/test）。

把用户提供的《品牌与账号数据.xlsx》「账号数据」表播种为**可直接登录**的账号：

1. 采用源表「登录密码」列作为初始密码。明文只在播种进程内存中短暂存在，写库前
   即经 Argon2id 哈希；明文不落库、不写日志、不进审计 payload（T07 红线仍成立）；
2. 账号直接 `ACTIVE` 且 `must_change_password=False`，跳过"管理员逐个确认 +
   一次性激活凭据"的既有流程，账号建好即可用源表密码登录；
3. 幂等：以 username 为锚做覆盖式 upsert，重复执行结果一致（重跑会重置密码、
   解锁账号并递增 `password_version` 使旧 token 失效）。

与 T07 主数据导入（`MasterDataImportService`）是两条独立路径：本模块不改动导入
批次、预检、确认与凭据签发的既有语义，避免影响 T07 的验收行为。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import DomainError
from app.core.security import hash_password
from app.models import (
    Brand,
    Organization,
    Region,
    Role,
    User,
    UserRegionBrandAuth,
)
from app.models.enums import RoleCode, UserStatus
from app.services import demo_dataset as ds
from app.services.demo_data_guard import assert_demo_disabled_in_production
from app.services.master_data.schemas import ParsedAccount
from app.services.master_data.workbook_parser import parse_workbook

logger = logging.getLogger(__name__)

# 源表缺密码列时的兜底口令（与用户提供的数据一致）
DEFAULT_LOGIN_PASSWORD = "Qs@123456"

# 默认账号工作簿（相对 backend/ 目录）
DEFAULT_WORKBOOK = "../沁爽饮料-品牌与账号数据.xlsx"

DEMO_ORG_CODE = "DEMO-QINSHUANG-ORG"
DEMO_ORG_NAME = "沁爽演示组织"

# 演示片区：与《初始化夹具与补充演示数据.xlsx》「片区」表一致
DEMO_REGIONS: tuple[tuple[str, str], ...] = (
    ("DEMO-JS", "金水片区"),
    ("DEMO-EQ", "二七片区"),
    ("DEMO-ZY", "中原片区"),
    ("DEMO-GC", "管城片区"),
    ("DEMO-HJ", "惠济片区"),
)

ROLE_DISPLAY_NAMES: dict[str, str] = {
    RoleCode.SALES_REP.value: "业务员",
    RoleCode.REGIONAL_MANAGER.value: "片区经理",
    RoleCode.SUPERVISOR.value: "督导",
    RoleCode.FINANCE.value: "财务",
    RoleCode.SYSTEM_ADMIN.value: "系统管理员",
    RoleCode.SUPPLIER.value: "厂商",
}


def _find_col(headers: list[str], names: tuple[str, ...]) -> int:
    for i, h in enumerate(headers):
        if h in names:
            return i
    return -1


def read_login_passwords(path: str | Path) -> dict[str, str]:
    """读取「登录账号 → 登录密码」映射；仅在内存中使用，绝不落库或写日志。"""
    import openpyxl

    out: dict[str, str] = {}
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            # 同 parse_workbook：容忍缺失/错误的 <dimension>，否则只读模式只能读到表头
            reset = getattr(ws, "reset_dimensions", None)
            if callable(reset):
                reset()
        for title in wb.sheetnames:
            rows = wb[title].iter_rows(values_only=True)
            header = next(rows, None)
            if header is None:
                continue
            headers = ["" if h is None else str(h).strip() for h in header]
            login_i = _find_col(headers, ("登录账号", "login_name"))
            if login_i < 0:
                continue
            pw_i = _find_col(headers, ("登录密码", "初始密码", "密码", "password", "口令"))
            if pw_i < 0:
                continue
            for row in rows:
                if login_i >= len(row) or pw_i >= len(row):
                    continue
                login = "" if row[login_i] is None else str(row[login_i]).strip()
                pw = "" if row[pw_i] is None else str(row[pw_i]).strip()
                if login and pw:
                    out.setdefault(login, pw)
    finally:
        wb.close()
    return out


class DemoAccountSeeder:
    """把账号工作簿播种为可直接登录的演示账号（幂等）。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def seed(self, *, path: str | Path, password: str | None = None) -> list[str]:
        assert_demo_disabled_in_production(dataset_type=ds.DEMO_DATASET_TYPE)

        wb_path = Path(path)
        if not wb_path.exists():
            raise DomainError(
                code="DEMO_ACCOUNT_SOURCE_MISSING",
                message="账号工作簿不存在",
                details={"path": str(wb_path)},
            )
        parsed = parse_workbook(wb_path)
        if not parsed.accounts:
            raise DomainError(
                code="DEMO_ACCOUNT_SOURCE_EMPTY",
                message="账号工作簿未解析出任何账号（缺少「账号数据」表）",
            )
        passwords = read_login_passwords(wb_path)
        fallback = password or DEFAULT_LOGIN_PASSWORD

        brand = self._ensure_brand()
        org = self._ensure_org()
        regions = self._ensure_regions(org)
        roles = self._ensure_roles()

        logins: list[str] = []
        for acc in parsed.accounts:
            user = self._upsert_user(acc, roles, passwords.get(acc.login_name, fallback))
            self._authorize(user, acc, regions, brand)
            logins.append(user.username)
        self.session.flush()
        return logins

    # ---------------- 主数据 ----------------

    def _ensure_brand(self) -> Brand:
        brand = self.session.execute(
            select(Brand).where(Brand.code == ds.QINSHUANG_BRAND_CODE).limit(1)
        ).scalar_one_or_none()
        if brand is not None:
            return brand
        brand = Brand(
            code=ds.QINSHUANG_BRAND_CODE,
            name=ds.QINSHUANG_BRAND_NAME,
            is_demo=True,
            dataset_type=ds.DEMO_DATASET_TYPE,
            dataset_id=ds.DEMO_DATASET_UUID,
        )
        self.session.add(brand)
        self.session.flush()
        return brand

    def _ensure_org(self) -> Organization:
        org = self.session.execute(
            select(Organization).where(Organization.code == DEMO_ORG_CODE).limit(1)
        ).scalar_one_or_none()
        if org is not None:
            return org
        org = Organization(
            code=DEMO_ORG_CODE,
            name=DEMO_ORG_NAME,
            dataset_type=ds.DEMO_DATASET_TYPE,
            dataset_id=ds.DEMO_DATASET_UUID,
        )
        self.session.add(org)
        self.session.flush()
        return org

    def _ensure_regions(self, org: Organization) -> dict[str, Region]:
        out: dict[str, Region] = {}
        for code, name in DEMO_REGIONS:
            region = self.session.execute(
                select(Region).where(Region.code == code).limit(1)
            ).scalar_one_or_none()
            if region is None:
                region = Region(
                    code=code,
                    name=name,
                    org_id=org.id,
                    dataset_type=ds.DEMO_DATASET_TYPE,
                    dataset_id=ds.DEMO_DATASET_UUID,
                )
                self.session.add(region)
                self.session.flush()
            out[code] = region
        return out

    def _ensure_roles(self) -> dict[str, Role]:
        out: dict[str, Role] = {}
        for rc in RoleCode:
            role = self.session.execute(
                select(Role)
                .where(Role.role_code == rc.value)
                .where(Role.dataset_id == ds.DEMO_DATASET_UUID)
                .limit(1)
            ).scalar_one_or_none()
            if role is None:
                role = Role(
                    code=f"DEMO-{rc.value}",
                    name=ROLE_DISPLAY_NAMES.get(rc.value, rc.value),
                    role_code=rc.value,
                    dataset_type=ds.DEMO_DATASET_TYPE,
                    dataset_id=ds.DEMO_DATASET_UUID,
                )
                self.session.add(role)
                self.session.flush()
            out[rc.value] = role
        return out

    # ---------------- 账号 ----------------

    def _upsert_user(
        self, acc: ParsedAccount, roles: dict[str, Role], password: str
    ) -> User:
        role = roles.get(acc.role_code)
        if role is None:
            raise DomainError(
                code="DEMO_ACCOUNT_UNKNOWN_ROLE",
                message="账号引用了未播种的角色",
                details={"login": acc.login_name, "role_code": acc.role_code},
            )
        user = self.session.execute(
            select(User).where(User.username == acc.login_name).limit(1)
        ).scalar_one_or_none()
        if user is None:
            user = User(
                username=acc.login_name,
                display_name=acc.display_name,
                phone=acc.phone,
                role_id=role.id,
                password_hash=hash_password(password),
                dataset_type=ds.DEMO_DATASET_TYPE,
                dataset_id=ds.DEMO_DATASET_UUID,
            )
            self.session.add(user)
            self.session.flush()
        else:
            user.display_name = acc.display_name
            user.phone = acc.phone
            user.role_id = role.id
            user.password_hash = hash_password(password)
            # 重置口令 → 递增版本，旧 token 立即失效
            user.password_version = (user.password_version or 1) + 1
        user.status = UserStatus.ACTIVE.value
        user.must_change_password = False
        user.failed_login_count = 0
        user.locked_until = None
        return user

    def _authorize(
        self,
        user: User,
        acc: ParsedAccount,
        regions: dict[str, Region],
        brand: Brand,
    ) -> None:
        existing = {
            (r.region_id, r.brand_id)
            for r in self.session.execute(
                select(UserRegionBrandAuth).where(UserRegionBrandAuth.user_id == user.id)
            ).scalars().all()
        }
        # 厂商 / 全局 / 未指定片区的账号：授权全部片区 × 沁爽
        global_scope = acc.is_supplier or acc.scope_global or not acc.region_codes
        codes = tuple(regions.keys()) if global_scope else acc.region_codes
        for code in codes:
            region = regions.get(code)
            if region is None:
                raise DomainError(
                    code="DEMO_ACCOUNT_UNKNOWN_REGION",
                    message="账号引用了未播种的片区",
                    details={"login": acc.login_name, "region_code": code},
                )
            if (region.id, brand.id) in existing:
                continue
            self.session.add(
                UserRegionBrandAuth(user_id=user.id, region_id=region.id, brand_id=brand.id)
            )


def resolve_workbook_path(raw: str | None) -> Path | None:
    """把配置里的工作簿路径解析为绝对路径（相对路径按 backend/ 解析）。"""
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    return path


def seed_demo_accounts_from_settings(session: Session, settings: Any) -> list[str]:
    """启动期播种入口：非生产 + 已配置工作簿时执行。

    文件缺失或播种失败都不阻塞进程启动（只记日志），避免演示环境因为一个
    可选的数据文件起不来。
    """
    # 只在本地开发/演示环境自动播种：生产与测试环境都不应隐式写入数据
    if str(getattr(settings, "environment", "")) not in {"dev", "development", "local"}:
        return []
    if not bool(getattr(settings, "demo_data_enabled", False)):
        return []
    path = resolve_workbook_path(getattr(settings, "demo_account_workbook", None))
    if path is None:
        return []
    if not path.exists():
        logger.warning("demo_account_seeder: 工作簿不存在，跳过播种 path=%s", path)
        return []
    try:
        logins = DemoAccountSeeder(session).seed(path=path)
    except Exception:  # noqa: BLE001 - 播种失败不影响服务启动
        logger.exception("demo_account_seeder: 播种失败，已跳过")
        return []
    logger.info("demo_account_seeder: 已播种 %d 个可直接登录的演示账号", len(logins))
    return logins

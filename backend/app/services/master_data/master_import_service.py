"""T07 主数据四步导入服务（上传 → 预检 → 确认 → 执行）。

职责与边界（PRD §2.1/§2.2/§6 R18/§8/§10.2）：
- 全部行固化 dataset_type=DEMO、dataset_id=固定 UUID；关联链数据域一致；
- 幂等：同一文件 SHA-256 或同一业务主键重复时只返回既有批次，不重复创建
  （34 SKU、15 账号、商家绝不重复落库）；
- 价格已在解析期转分；SKU 编码稳定；**先建片区，再建账号，最后建商家**；
- 工作簿引用标准演示片区（DEMO-JS…）却未自带「片区」表时，按需补建片区；
- 工作簿若含「商家DEMO」表（商家建档初始化的推荐载体）则一并落库：
  商家按编号幂等；负责人账号存在时建立主责任归属区间（T08 归属模型），
  账号缺失只跳过归属、不阻断导入；
- 明确忽略源表「登录密码」列；账号默认禁用/待激活，管理员逐个确认启用；
- 执行后仅为"被确认启用的非厂商账号"一次性签发激活凭据并只在响应中返回；
- 厂商账号一律 PENDING_ACTIVATION，且不自动开启厂商门户（supplier_portal_enabled）；
- 角色权限表仅差异预览，绝不覆盖代码权限矩阵；
- 主数据批次「全有或全无」：执行期任何错误整体回滚（单一事务）。

写操作与审计在同一事务（AuditService）。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from geoalchemy2.shape import from_shape
from shapely.geometry import Point
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, DomainError
from app.core.security import hash_password
from app.core.tokens import issue_token
from app.models import (
    Brand,
    Contract,
    ImportBatch,
    Merchant,
    MerchantAssignment,
    Organization,
    ProductCategory,
    Region,
    Role,
    Sku,
    User,
    UserRegionBrandAuth,
)
from app.models.enums import RoleCode, UserStatus
from app.services import demo_dataset as ds
from app.services.audit_service import AuditService
from app.services.demo_data_guard import assert_demo_disabled_in_production
from app.services.master_data.schemas import (
    ParsedAccount,
    ParsedMerchant,
    ParsedSku,
    ParsedWorkbook,
)
from app.services.master_data.workbook_parser import (
    DEMO_REGION_NAME_BY_CODE,
    parse_workbook,
)

BATCH_KIND_MASTER = "MASTER"
STAGE_UPLOADED = "UPLOADED"
STAGE_PREFLIGHT = "PREFLIGHT"
STAGE_CONFIRMED = "CONFIRMED"
STAGE_EXECUTED = "EXECUTED"

# 账号确认项：login_name -> 是否启用
AccountDecision = dict[str, bool]


@dataclass(frozen=True)
class ActivationCredential:
    """一次性激活凭据：明文只在此返回，调用方必须用完即弃，绝不入库。"""

    login_name: str
    role_code: str
    credential: str


@dataclass
class PreflightReport:
    batch_id: UUID
    file_hash: str
    sku_count: int
    region_codes: list[str]
    account_count: int
    merchant_count: int
    contract_count: int
    role_preview: list[dict[str, str | None]]
    unknown_regions: list[str]
    ignored_columns: list[str]
    warnings: list[str]
    ready: bool


# 无法登录的占位密码（每用户随机），满足 password_hash 的 Argon2id 格式 CHECK
def _placeholder_hash() -> str:
    return hash_password(secrets.token_urlsafe(24))


class MasterDataImportService:
    """主数据（品牌/片区/角色/SKU/账号）导入。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    # -------- 第一步：上传（登记 + 解析 + 幂等） --------

    def upload(
        self, *, path: str, actor_user_id: UUID, filename: str | None = None
    ) -> ImportBatch:
        parsed = parse_workbook(path)

        # 目标数据域：从操作者所属数据域派生（正式初始化导入进 PRODUCTION，
        # 夹具演示显式传入 DEMO）。这是"数据域隔离设计"的核心改动。
        actor_user = self.session.get(User, actor_user_id)
        if actor_user is None:
            raise DomainError(code="IMPORT_ACTOR_NOT_FOUND", message="操作者账号不存在")
        target_dataset_type = actor_user.dataset_type
        target_dataset_id = actor_user.dataset_id

        if target_dataset_type == ds.DEMO_DATASET_TYPE:
            assert_demo_disabled_in_production(dataset_type=target_dataset_type)

        # 幂等 1：同一文件哈希（非空）已有成功批次 → 只返回既有批次
        existing = self._find_batch_by_hash(parsed.file_hash, target_dataset_id)
        if existing is not None:
            return existing

        self._prevalidate(parsed)

        batch = ImportBatch(
            dataset_type=target_dataset_type,
            dataset_id=target_dataset_id,
            source_filename=filename or parsed.source_filename,
            file_hash=parsed.file_hash,
            seed_kind=BATCH_KIND_MASTER,
            rows_total=self._rows_total(parsed),
            rows_imported=0,
            rows_rejected=0,
            status="PENDING",
            payload_json={
                "stage": STAGE_UPLOADED,
                "ignored_columns": parsed.ignored_columns,
                "sku_count": len(parsed.skus),
                "account_count": len(parsed.accounts),
                "merchant_count": len(parsed.merchants),
                "region_codes": [r.code for r in parsed.regions],
            },
            imported_by_id=actor_user_id,
        )
        self.session.add(batch)
        self.session.flush()
        AuditService(self.session).record(
            object_type="IMPORT_BATCH",
            object_id=batch.id,
            event_type="CREATE",
            actor_id=actor_user_id,
            after={"stage": STAGE_UPLOADED, "file_hash": parsed.file_hash},
            reason="T07 主数据工作簿上传",
        )
        # 解析结果随调用链使用；此处仅持久化批次，不缓存敏感数据
        self.session.info[f"t07_parsed_{batch.id}"] = parsed
        return batch

    # -------- 第二步：预检 --------

    def preflight(
        self, batch: ImportBatch, parsed: ParsedWorkbook | None = None
    ) -> PreflightReport:
        parsed = parsed or self._require_parsed(batch)
        unknown = self._unknown_regions(parsed)
        role_preview = [
            {
                "role_label": rp.role_label,
                "role_code": rp.role_code,
                "function_permissions": rp.function_permissions,
                "data_permissions": rp.data_permissions,
            }
            for rp in parsed.role_permissions
        ]
        ready = not unknown
        batch.payload_json = {
            **(batch.payload_json or {}),
            "stage": STAGE_PREFLIGHT,
            "ready": ready,
            "unknown_regions": unknown,
            "role_preview": role_preview,
        }
        if ready:
            batch.status = "PENDING"
        self.session.flush()
        return PreflightReport(
            batch_id=batch.id,
            file_hash=batch.file_hash,
            sku_count=len(parsed.skus),
            region_codes=[r.code for r in parsed.regions],
            account_count=len(parsed.accounts),
            merchant_count=len(parsed.merchants),
            contract_count=len(parsed.contracts),
            role_preview=role_preview,
            unknown_regions=unknown,
            ignored_columns=parsed.ignored_columns,
            warnings=parsed.warnings,
            ready=ready,
        )

    # -------- 第三步：确认（管理员逐个决定账号角色/片区/启用） --------

    def confirm(
        self,
        batch: ImportBatch,
        *,
        actor_user_id: UUID,
        account_decisions: AccountDecision,
        parsed: ParsedWorkbook | None = None,
    ) -> ImportBatch:
        parsed = parsed or self._require_parsed(batch)
        unknown = self._unknown_regions(parsed)
        if unknown:
            raise DomainError(
                code="IMPORT_UNKNOWN_REGION",
                message="存在未映射的片区，主数据批次必须先修正后才能确认",
                details={"unknown_regions": unknown},
            )
        known_logins = {a.login_name for a in parsed.accounts}
        extra = [k for k in account_decisions if k not in known_logins]
        if extra:
            raise DomainError(
                code="IMPORT_UNKNOWN_ACCOUNT",
                message="确认项包含工作簿中不存在的账号",
                details={"logins": extra},
            )
        # 仅记录布尔决定；不接受任何密码输入
        normalized = {login: bool(account_decisions.get(login, False)) for login in known_logins}
        batch.payload_json = {
            **(batch.payload_json or {}),
            "stage": STAGE_CONFIRMED,
            "account_enabled": normalized,
            # 显式声明：角色权限表仅预览，代码权限矩阵不被覆盖
            "role_matrix_source": "CODE_PERMISSION_MATRIX",
            "supplier_portal_enabled": False,
            "confirmed_at": datetime.now(tz=UTC).isoformat(),
        }
        batch.status = "PENDING"
        self.session.flush()
        AuditService(self.session).record(
            object_type="IMPORT_BATCH",
            object_id=batch.id,
            event_type="UPDATE",
            actor_id=actor_user_id,
            after={"stage": STAGE_CONFIRMED, "enabled": sum(normalized.values())},
            reason="管理员确认账号角色/片区/启用状态",
        )
        return batch

    # -------- 第四步：执行（单事务，全有或全无） --------

    def execute(
        self,
        batch: ImportBatch,
        *,
        actor_user_id: UUID,
        parsed: ParsedWorkbook | None = None,
        account_decisions: AccountDecision | None = None,
        issue_credentials: bool = True,
    ) -> list[ActivationCredential]:
        if batch.status == "SUCCEEDED" and (batch.payload_json or {}).get(
            "stage"
        ) == STAGE_EXECUTED:
            raise ConflictError(
                code="IMPORT_ALREADY_EXECUTED",
                message="主数据批次已执行；不得重复执行",
                details={"batch_id": str(batch.id)},
            )
        parsed = parsed or self._require_parsed(batch)
        if account_decisions is not None:
            decisions = {
                a.login_name: bool(account_decisions.get(a.login_name, False))
                for a in parsed.accounts
            }
        else:
            decisions = (batch.payload_json or {}).get("account_enabled", {})

        # 幂等 2：业务主键已存在（重复执行/重复导入）→ 不重复创建
        if self._master_data_already_landed(batch.dataset_id, parsed):
            batch.status = "SUCCEEDED"
            batch.rows_imported = self._rows_total(parsed)
            batch.payload_json = {
                **(batch.payload_json or {}),
                "stage": STAGE_EXECUTED,
                "idempotent": True,
            }
            self.session.flush()
            return []

        credentials: list[ActivationCredential] = []
        owner_assigned = 0
        # 单一逻辑事务 + SAVEPOINT：业务写入失败仅回滚业务数据，
        # 批次记录仍可标记 FAILED；对外承诺「主数据批次全有或全无」。
        nested = self.session.begin_nested()
        try:
            ds_type = batch.dataset_type
            ds_id = batch.dataset_id
            brand = self._ensure_brand(ds_type, ds_id)
            org = self._ensure_org(ds_type, ds_id)
            region_map = self._persist_regions(org, parsed.regions, ds_type, ds_id)
            region_map = self._ensure_canonical_regions(org, region_map, parsed, ds_type, ds_id)
            category_map = self._persist_categories(parsed.skus, ds_type, ds_id)
            self._persist_skus(brand, category_map, parsed.skus, ds_type, ds_id)
            role_map = self._persist_roles(ds_type, ds_id)
            if issue_credentials:
                credentials = self._persist_accounts(
                    role_map, region_map, brand, parsed.accounts, decisions, ds_type, ds_id
                )
            else:
                # 夹具演示导入：账号一律禁用/待激活，不签发任何激活凭据
                credentials = []
                self._persist_accounts(
                    role_map, region_map, brand, parsed.accounts,
                    {a.login_name: False for a in parsed.accounts},
                    ds_type, ds_id,
                )
            # 商家建档：排在片区与账号之后（引用的片区/负责人已可解析）
            _, owner_assigned = self._persist_merchants(
                brand, region_map, parsed.merchants, ds_type, ds_id
            )
            # 合同导入：商家已落地，按合同号幂等
            contract_imported = self._persist_contracts(
                brand, parsed.contracts, ds_type, ds_id
            )
            nested.commit()
        except Exception as exc:
            nested.rollback()
            batch.status = "FAILED"
            batch.error_log = "主数据执行失败，已整批回滚"
            self.session.flush()
            if isinstance(exc, DomainError):
                raise
            raise DomainError(
                code="IMPORT_EXECUTE_FAILED",
                message="主数据导入失败，已整批回滚（全有或全无）",
            ) from exc

        batch.status = "SUCCEEDED"
        batch.rows_imported = self._rows_total(parsed)
        batch.rows_rejected = 0
        batch.payload_json = {
            **(batch.payload_json or {}),
            "stage": STAGE_EXECUTED,
            "idempotent": False,
            "sku_imported": len(parsed.skus),
            "account_imported": len(parsed.accounts),
            "merchant_imported": len(parsed.merchants),
            "merchant_owner_assigned": owner_assigned,
            "account_enabled": sum(1 for v in decisions.values() if v),
            "credentials_issued": len(credentials),
            "contract_imported": contract_imported,
            # 明确不含任何凭据明文；凭据仅经返回值一次性给出
        }
        self.session.flush()
        AuditService(self.session).record(
            object_type="IMPORT_BATCH",
            object_id=batch.id,
            event_type="UPDATE",
            actor_id=actor_user_id,
            after={
                "stage": STAGE_EXECUTED,
                "rows_imported": batch.rows_imported,
                "credentials_issued": len(credentials),
            },
            reason="主数据批次执行完成",
        )
        return credentials

    # -------- 查询/幂等 --------

    def get(self, batch_id: UUID) -> ImportBatch | None:
        return self.session.get(ImportBatch, batch_id)

    def _find_batch_by_hash(self, file_hash: str, target_dataset_id: UUID) -> ImportBatch | None:
        """幂等命中：只认**目标数据集 + 已成功**的批次。

        必须同时限定 `dataset_id` 与 `status`，否则任何一份字节相同的批次都会劫持
        后续上传：其它数据集的批次会把内容错并入本数据集；未成功（PENDING/FAILED/
        ROLLED_BACK）的批次会让重传永远停在旧阶段，用户既拿不到新批次也无法重试。
        同一文件导入到不同数据集（如先 DEMO 后 PRODUCTION）视为独立批次。
        """
        if not file_hash:
            return None
        return self.session.execute(
            select(ImportBatch)
            .where(ImportBatch.file_hash == file_hash)
            .where(ImportBatch.seed_kind == BATCH_KIND_MASTER)
            .where(ImportBatch.dataset_id == target_dataset_id)
            .where(ImportBatch.status == "SUCCEEDED")
            .order_by(ImportBatch.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    def _require_parsed(self, batch: ImportBatch) -> ParsedWorkbook:
        parsed = self.session.info.get(f"t07_parsed_{batch.id}")
        if parsed is None:
            raise DomainError(
                code="IMPORT_WORKBOOK_NOT_ATTACHED",
                message="该批次的工作簿解析结果不在当前会话；请重新上传后再预检/执行",
            )
        return parsed  # type: ignore[no-any-return]

    @staticmethod
    def _rows_total(parsed: ParsedWorkbook) -> int:
        """批次总行数 = 主数据（SKU/片区/账号）+ 商家 + 合同。

        `ParsedWorkbook.master_rows_total` 的口径不含商家/合同，这里统一加上。
        """
        return parsed.master_rows_total + len(parsed.merchants) + len(parsed.contracts)

    def _prevalidate(self, parsed: ParsedWorkbook) -> None:
        if not parsed.skus and not parsed.accounts and not parsed.merchants:
            raise DomainError(
                code="IMPORT_EMPTY_WORKBOOK",
                message="工作簿未解析出任何 SKU、账号或商家",
            )
        # SKU 编码 / 主键不得在文件内重复
        sku_codes = [s.sku_code for s in parsed.skus]
        dup_sku = {c for c in sku_codes if sku_codes.count(c) > 1}
        if dup_sku:
            raise DomainError(
                code="IMPORT_DUPLICATE_SKU",
                message="工作簿内存在重复 SKU 编码",
                details={"codes": sorted(dup_sku)},
            )
        logins = [a.login_name for a in parsed.accounts]
        dup_login = {c for c in logins if logins.count(c) > 1}
        if dup_login:
            raise DomainError(
                code="IMPORT_DUPLICATE_ACCOUNT",
                message="工作簿内存在重复登录账号",
                details={"logins": sorted(dup_login)},
            )
        merchant_codes = [m.code for m in parsed.merchants]
        dup_merchant = {c for c in merchant_codes if merchant_codes.count(c) > 1}
        if dup_merchant:
            raise DomainError(
                code="IMPORT_DUPLICATE_MERCHANT",
                message="工作簿内存在重复商家编号",
                details={"codes": sorted(dup_merchant)},
            )
        # 状态/等级按 DB CHECK 同口径预检，避免执行期才整批回滚
        for mch in parsed.merchants:
            if mch.status not in {"LEAD", "NEGOTIATING", "ACTIVE", "PAUSED", "TERMINATED"}:
                raise DomainError(
                    code="IMPORT_MERCHANT_STATUS_INVALID",
                    message="商家状态非法",
                    details={"merchant": mch.code, "status": mch.status},
                )
            if mch.grade is not None and mch.grade not in {"A", "B", "C"}:
                raise DomainError(
                    code="IMPORT_MERCHANT_GRADE_INVALID",
                    message="商家等级必须为 A/B/C",
                    details={"merchant": mch.code, "grade": mch.grade},
                )
        for sku in parsed.skus:
            if sku.suggested_price_fen < 0 or (
                sku.purchase_price_fen is not None and sku.purchase_price_fen < 0
            ):
                raise DomainError(
                    code="IMPORT_PRICE_INVALID",
                    message="价格不得为负",
                    details={"sku_code": sku.sku_code},
                )

    def _unknown_regions(self, parsed: ParsedWorkbook) -> list[str]:
        # 已知片区 = 本工作簿解析出的片区 ∪ 已落在同数据集（DEMO）库内的片区。
        # 用户《品牌与账号数据》工作簿只有「账号数据」表、没有独立「片区」表，账号引用的
        # 片区只能靠中文名兜底映射（见 workbook_parser）；执行期 _apply_auth_scope 同样
        # 会从库内取片区，两处判定必须一致，否则预检恒判「未知片区」把批次卡死。
        # 标准演示片区编码（DEMO-JS…）由 _ensure_canonical_regions 按需补建，视为已知
        known = (
            {r.code for r in parsed.regions}
            | set(self._load_all_regions())
            | set(DEMO_REGION_NAME_BY_CODE)
        )
        unknown: set[str] = set()
        for acc in parsed.accounts:
            if acc.scope_global or acc.is_supplier:
                continue
            for code in acc.region_codes:
                if code not in known:
                    unknown.add(code)
        # 商家建档同样引用片区；未知即拦截，避免执行期才发现（Q036）
        for mch in parsed.merchants:
            if mch.region_code and mch.region_code not in known:
                unknown.add(mch.region_code)
        # 解析期中文片区名无法映射时记录的 warning 也纳入
        for w in parsed.warnings:
            if "未知片区" in w:
                unknown.add(w.split("'", 1)[1].rsplit("'", 1)[0])
        return sorted(unknown)

    def _master_data_already_landed(self, dataset_id: UUID, parsed: ParsedWorkbook) -> bool:
        """业务主键是否已落库（重复导入/重复执行的幂等判定）。

        SKU 与商家各自的编号都是稳定主键；纯商家批次（无 SKU）以首个商家编号判定。
        按目标数据集隔离：DEMO 已导入不阻挡 PRODUCTION 重新导入。
        """
        if parsed.skus:
            return (
                self.session.execute(
                    select(Sku.id)
                    .where(Sku.code == parsed.skus[0].sku_code)
                    .where(Sku.dataset_id == dataset_id)
                    .limit(1)
                ).first()
                is not None
            )
        if parsed.merchants:
            return (
                self.session.execute(
                    select(Merchant.id)
                    .where(Merchant.code == parsed.merchants[0].code)
                    .where(Merchant.dataset_id == dataset_id)
                    .limit(1)
                ).first()
                is not None
            )
        return False

    # -------- 持久化（同数据集，关联一致） --------

    def _ensure_brand(self, dataset_type: str, dataset_id: UUID) -> Brand:
        brand = self.session.execute(
            select(Brand).where(Brand.code == ds.QINSHUANG_BRAND_CODE).limit(1)
        ).scalar_one_or_none()
        if brand is not None:
            return brand
        brand = Brand(
            code=ds.QINSHUANG_BRAND_CODE,
            name=ds.QINSHUANG_BRAND_NAME,
            is_demo=(dataset_type == ds.DEMO_DATASET_TYPE),
            dataset_type=dataset_type,
            dataset_id=dataset_id,
        )
        self.session.add(brand)
        self.session.flush()
        return brand

    def _ensure_org(self, dataset_type: str, dataset_id: UUID) -> Organization:
        code = "DEMO-QINSHUANG-ORG"
        org = self.session.execute(
            select(Organization).where(Organization.code == code).limit(1)
        ).scalar_one_or_none()
        if org is not None:
            return org
        org = Organization(
            code=code,
            name="沁爽演示组织",
            dataset_type=dataset_type,
            dataset_id=dataset_id,
        )
        self.session.add(org)
        self.session.flush()
        return org

    def _persist_regions(
        self, org: Organization, parsed_regions, dataset_type: str, dataset_id: UUID
    ) -> dict[str, Region]:
        out: dict[str, Region] = {}
        for pr in parsed_regions:
            region = self.session.execute(
                select(Region).where(Region.code == pr.code).limit(1)
            ).scalar_one_or_none()
            if region is None:
                region = Region(
                    code=pr.code,
                    name=pr.name,
                    org_id=org.id,
                    dataset_type=dataset_type,
                    dataset_id=dataset_id,
                )
                self.session.add(region)
                self.session.flush()
            out[pr.code] = region
        return out

    def _ensure_canonical_regions(
        self,
        org: Organization,
        region_map: dict[str, Region],
        parsed: ParsedWorkbook,
        dataset_type: str,
        dataset_id: UUID,
    ) -> dict[str, Region]:
        """补建「被引用、但工作簿未带片区表且库内也不存在」的标准演示片区。

        解析器把「金水片区」等中文名映射为 DEMO-JS 等固定编码（见
        `CN_REGION_NAME_TO_CODE`）。用户《品牌与账号数据》《商家数据》工作簿都不带
        「片区」表，若库内也还没有这些片区，账号授权与商家建档都会因外键缺失而整批
        回滚。这里按需补建，使这类工作簿可独立导入；非标准编码仍由
        `_unknown_regions` 在预检阶段拦截。
        """
        referenced: set[str] = set()
        for acc in parsed.accounts:
            referenced.update(acc.region_codes)
        for mch in parsed.merchants:
            if mch.region_code:
                referenced.add(mch.region_code)
        # 已存在的判定必须同时看本工作簿与库：region_map 只含本工作簿「片区」表解析出的行，
        # 若只看它，第二次导入（工作簿仍无片区表）会重复插入标准片区而撞唯一约束。
        known = set(region_map) | set(self._load_all_regions())
        missing = (referenced & set(DEMO_REGION_NAME_BY_CODE)) - known
        for code in sorted(missing):
            region = Region(
                code=code,
                name=DEMO_REGION_NAME_BY_CODE[code],
                org_id=org.id,
                dataset_type=dataset_type,
                dataset_id=dataset_id,
            )
            self.session.add(region)
            self.session.flush()
            region_map = {**region_map, code: region}
        return region_map

    def _persist_categories(
        self, skus: list[ParsedSku], dataset_type: str, dataset_id: UUID
    ) -> dict[str, ProductCategory]:
        names = {s.category_name for s in skus if s.category_name}
        out: dict[str, ProductCategory] = {}
        for i, name in enumerate(sorted(names), start=1):
            code = f"DEMO-CAT-{i:02d}"
            cat = self.session.execute(
                select(ProductCategory).where(ProductCategory.code == code).limit(1)
            ).scalar_one_or_none()
            if cat is None:
                cat = ProductCategory(
                    code=code,
                    name=name,
                    dataset_type=dataset_type,
                    dataset_id=dataset_id,
                )
                self.session.add(cat)
                self.session.flush()
            out[name] = cat
        return out

    def _persist_skus(
        self,
        brand: Brand,
        category_map: dict[str, ProductCategory],
        skus: list[ParsedSku],
        dataset_type: str,
        dataset_id: UUID,
    ) -> None:
        for s in skus:
            existing = self.session.execute(
                select(Sku).where(Sku.code == s.sku_code).limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                continue
            cat = category_map.get(s.category_name or "")
            self.session.add(
                Sku(
                    code=s.sku_code,
                    name=s.name,
                    brand_id=brand.id,
                    category_id=cat.id if cat else None,
                    capacity_ml=s.capacity_ml,
                    suggested_price_fen=s.suggested_price_fen,
                    case_spec=s.case_spec if s.case_spec is not None else Decimal("0"),
                    purchase_price_fen=s.purchase_price_fen,
                    series_name=s.series_name,
                    category_name=s.category_name,
                    spec_text=s.spec_text,
                    selling_points=s.selling_points,
                    shelf_life_text=s.shelf_life_text,
                    target_channel=s.target_channel,
                    dataset_type=dataset_type,
                    dataset_id=dataset_id,
                )
            )
        self.session.flush()

    def _persist_roles(self, dataset_type: str, dataset_id: UUID) -> dict[str, Role]:
        out: dict[str, Role] = {}
        for rc in RoleCode:
            role = self.session.execute(
                select(Role)
                .where(Role.role_code == rc.value)
                .where(Role.dataset_id == dataset_id)
                .limit(1)
            ).scalar_one_or_none()
            if role is None:
                role = Role(
                    code=f"DEMO-{rc.value}",
                    name=_ROLE_DISPLAY_NAMES.get(rc.value, rc.value),
                    role_code=rc.value,
                    dataset_type=dataset_type,
                    dataset_id=dataset_id,
                )
                self.session.add(role)
                self.session.flush()
            out[rc.value] = role
        return out

    def _persist_accounts(
        self,
        role_map: dict[str, Role],
        region_map: dict[str, Region],
        brand: Brand,
        accounts: list[ParsedAccount],
        decisions: dict[str, bool],
        dataset_type: str,
        dataset_id: UUID,
    ) -> list[ActivationCredential]:
        credentials: list[ActivationCredential] = []
        # 账号引用的片区必须已落地（片区映射先于账号；缺失即整批失败 → Q036）
        all_regions = self._load_all_regions()
        for acc in accounts:
            existing = self.session.execute(
                select(User)
                .where(User.username == acc.login_name)
                .where(User.dataset_id == dataset_id)
                .limit(1)
            ).scalar_one_or_none()
            user = existing or User(
                username=acc.login_name,
                display_name=acc.display_name,
                phone=acc.phone,
                role_id=role_map[acc.role_code].id,
                password_hash=_placeholder_hash(),
                dataset_type=dataset_type,
                dataset_id=dataset_id,
            )
            enabled = bool(decisions.get(acc.login_name, False))
            if acc.is_supplier:
                # 厂商：门户未开启前一律待激活，且即便确认也不自动启用/不发凭据
                user.status = UserStatus.PENDING_ACTIVATION.value
                user.must_change_password = True
            elif enabled:
                user.status = UserStatus.ACTIVE.value
                user.must_change_password = True
            else:
                user.status = UserStatus.INACTIVE.value
                user.must_change_password = True
            if existing is None:
                self.session.add(user)
                self.session.flush()
            self._apply_auth_scope(user, acc, all_regions, region_map, brand)
            # 仅为"确认启用的非厂商账号"签发一次性激活凭据
            if enabled and not acc.is_supplier:
                token = issue_token(
                    user_id=user.id,
                    username=user.username,
                    role_code=acc.role_code,
                    password_version=user.password_version,
                    kind="ACTIVATION",
                )
                credentials.append(
                    ActivationCredential(
                        login_name=user.username,
                        role_code=acc.role_code,
                        credential=token,
                    )
                )
        self.session.flush()
        return credentials

    def _load_all_regions(self) -> dict[str, Region]:
        """返回全部片区（Region.code 全局唯一，不按数据域过滤）。"""
        rows = self.session.execute(select(Region)).scalars().all()
        return {r.code: r for r in rows}

    def _load_users_by_username(self, dataset_id: UUID) -> dict[str, User]:
        """返回指定数据域的全部账号（User.username 在数据域内唯一）。"""
        rows = self.session.execute(
            select(User).where(User.dataset_id == dataset_id)
        ).scalars().all()
        return {u.username: u for u in rows}

    # -------- 商家建档（工作簿「商家DEMO」表） --------

    def _persist_merchants(
        self,
        brand: Brand,
        region_map: dict[str, Region],
        merchants: list[ParsedMerchant],
        dataset_type: str,
        dataset_id: UUID,
    ) -> tuple[int, int]:
        """落库商家并（在可解析到账号时）建立主负责人归属区间。

        返回 (处理商家数, 新建归属数)。商家按编号幂等：已存在的档案只复用不覆盖，
        保证同一工作簿重复导入/重复上传不会产生重复商家。
        """
        if not merchants:
            return 0, 0
        all_regions = self._load_all_regions()
        owners = self._load_users_by_username(dataset_id)
        assigned = 0
        for m in merchants:
            region = all_regions.get(m.region_code) or region_map.get(m.region_code)
            if region is None:
                # 未知片区：整批回滚（与 _unknown_regions 的预检判定保持一致）
                raise DomainError(
                    code="IMPORT_UNKNOWN_REGION",
                    message="商家引用了未导入的片区，主数据批次整批回滚",
                    details={"merchant": m.code, "region_code": m.region_code},
                )
            merchant = self.session.execute(
                select(Merchant)
                .where(Merchant.code == m.code)
                .where(Merchant.dataset_id == dataset_id)
                .limit(1)
            ).scalar_one_or_none()
            if merchant is None:
                location = None
                if m.longitude is not None and m.latitude is not None:
                    location = from_shape(Point(m.longitude, m.latitude), srid=4326)
                merchant = Merchant(
                    code=m.code,
                    name=m.name,
                    status=m.status,
                    grade=m.grade,
                    region_id=region.id,
                    brand_id=brand.id,
                    location=location,
                    address=m.address,
                    contact_phone=m.contact_phone,
                    primary_contact_name=m.contact_name,
                    dataset_type=dataset_type,
                    dataset_id=dataset_id,
                )
                self.session.add(merchant)
                self.session.flush()
            # 负责人账号存在才建归属（T08 责任归属模型）；缺失只跳过，不阻断建档
            owner = owners.get(m.owner_login) if m.owner_login else None
            if owner is not None and self._assign_primary(merchant, owner, dataset_type, dataset_id):
                assigned += 1
        self.session.flush()
        return len(merchants), assigned

    def _assign_primary(
        self, merchant: Merchant, owner: User, dataset_type: str, dataset_id: UUID
    ) -> bool:
        """建立开放区间的主责任归属；已有主责任直接跳过（幂等）。"""
        existing = self.session.execute(
            select(MerchantAssignment.id)
            .where(MerchantAssignment.merchant_id == merchant.id)
            .where(MerchantAssignment.assignment_type == "PRIMARY")
            .limit(1)
        ).first()
        if existing is not None:
            return False
        start = datetime.now(tz=UTC)
        self.session.add(
            MerchantAssignment(
                merchant_id=merchant.id,
                owner_user_id=owner.id,
                assignment_type="PRIMARY",
                valid_range=func.tstzrange(
                    func.to_timestamp(start.timestamp()), None, "[)"
                ),
                reason="T07 主数据导入随商家建档指派",
                dataset_type=dataset_type,
                dataset_id=dataset_id,
            )
        )
        self.session.flush()
        return True

    def _persist_contracts(
        self,
        brand: Brand,
        contracts: list,
        dataset_type: str,
        dataset_id: UUID,
    ) -> int:
        """落地「合同DEMO」表（商家建档后才可落库，按合同号幂等）。

        `contract_no` 有全局唯一约束，跨数据集共享——同一合同号无论哪个域导入，
        都视为同一份，避免 PRODUCTION 重新导入时被 DEMO 批次劫持。
        商家必须属于当前目标数据集；非沁爽品牌直接跳过（演示夹具只含沁爽）。
        """
        if not contracts:
            return 0
        _STATUS_MAP = {
            "DRAFT": "DRAFT",
            "PENDING_APPROVAL": "PENDING_APPROVAL",
            "ACTIVE": "EFFECTIVE",
            "EFFECTIVE": "EFFECTIVE",
            "EXPIRED": "TERMINATED",
            "TERMINATED": "TERMINATED",
        }
        # 只加载目标数据集的商家（合同 merchant_id 必须是同域商家）
        merchants_in_domain = {
            m.code: m
            for m in self.session.execute(
                select(Merchant).where(Merchant.dataset_id == dataset_id)
            ).scalars().all()
        }
        imported = 0
        for c in contracts:
            existing = self.session.execute(
                select(Contract).where(Contract.contract_no == c.code).limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                continue
            merchant = merchants_in_domain.get(c.merchant_code)
            if merchant is None:
                raise DomainError(
                    code="IMPORT_BAD_CONTRACT_REFERENCE",
                    message="合同引用了未导入（或非本域）商家",
                    details={"contract": c.code, "merchant_code": c.merchant_code},
                )
            if c.brand_code and c.brand_code != ds.QINSHUANG_BRAND_CODE:
                continue  # 演示夹具仅含沁爽，其他品牌行静默跳过
            self.session.add(
                Contract(
                    contract_no=c.code,
                    merchant_id=merchant.id,
                    title=f"陈列合作协议 {c.code}",
                    party_name=merchant.name,
                    status=_STATUS_MAP.get(c.status, "DRAFT"),
                    signed_at=(
                        date.fromisoformat(c.signed_date) if c.signed_date else None
                    ),
                    effective_from=date.fromisoformat(c.start_date),
                    expire_at=date.fromisoformat(c.end_date),
                    display_threshold_pct=c.display_threshold_pct,
                    commission_rule_version="fixture-v1",
                    commission_rate=Decimal("0"),
                    dataset_type=dataset_type,
                    dataset_id=dataset_id,
                )
            )
            imported += 1
        self.session.flush()
        return imported

    def _apply_auth_scope(
        self,
        user: User,
        acc: ParsedAccount,
        all_regions: dict[str, Region],
        region_map: dict[str, Region],
        brand: Brand,
    ) -> None:
        # 幂等：已有授权不重复插入
        existing = {
            (r.region_id, r.brand_id)
            for r in self.session.execute(
                select(UserRegionBrandAuth).where(
                    UserRegionBrandAuth.user_id == user.id
                )
            ).scalars().all()
        }

        def _add(region_id: UUID, brand_id: UUID) -> None:
            if (region_id, brand_id) in existing:
                return
            self.session.add(
                UserRegionBrandAuth(
                    user_id=user.id, region_id=region_id, brand_id=brand_id
                )
            )

        if acc.is_supplier:
            # 厂商：授权全部片区 × 其品牌（QINSHUANG）
            brand_id = brand.id
            for code in acc.region_codes or tuple(all_regions.keys()):
                region = all_regions.get(code)
                if region is not None:
                    _add(region.id, brand_id)
            return
        if acc.scope_global or not acc.region_codes:
            # 全局/跨片区角色：授权全部片区 × QINSHUANG
            for region in all_regions.values():
                _add(region.id, brand.id)
            return
        # 业务员：限定其片区 × QINSHUANG
        for code in acc.region_codes:
            region = all_regions.get(code) or region_map.get(code)
            if region is None:
                # 未知片区：整批回滚（Q036）
                raise DomainError(
                    code="IMPORT_UNKNOWN_REGION",
                    message="账号引用了未导入的片区，主数据批次整批回滚",
                    details={"login": acc.login_name, "region_code": code},
                )
            _add(region.id, brand.id)


_ROLE_DISPLAY_NAMES = {
    RoleCode.SALES_REP.value: "业务员",
    RoleCode.REGIONAL_MANAGER.value: "片区经理",
    RoleCode.SUPERVISOR.value: "督导",
    RoleCode.FINANCE.value: "财务",
    RoleCode.SYSTEM_ADMIN.value: "系统管理员",
    RoleCode.SUPPLIER.value: "厂商",
}

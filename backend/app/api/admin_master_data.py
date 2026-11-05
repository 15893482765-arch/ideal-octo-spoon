"""/api/admin/master-data 路由：主数据台账与 T07 四步导入。

- 台账（品牌 / 片区 / SKU）**只读**：主数据只停用不删除（ImmutableMixin），
  这里只暴露编码/名称等非敏感参照字段，供系统管理员核对导入结果；
- 导入严格复用 `MasterDataImportService` 的「上传 → 预检 → 确认 → 执行」四步，
  本路由只承担：文件校验与暂存、跨请求时重建解析结果、响应组装；
- 解析结果由服务放在 `session.info`（仅同一次请求内有效），跨请求时从对象存储
  取回工作簿重新解析，不在任何地方缓存工作簿内容；
- 确认阶段只接收布尔决定（是否启用账号），**绝不接收任何密码输入**；
  激活凭据只在执行响应中一次性返回，不落库、不写审计 payload（PRD §2.1 红线）。
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import current_actor, db_session, require_ability
from app.core.errors import ConflictError, DomainError, PermissionDeniedError
from app.core.permissions import (
    MODULE_REGION_MANAGE,
    MODULE_SETTINGS,
    ActorContext,
    get_profile,
)
from app.models import (
    Brand,
    ImportBatch,
    Organization,
    ProductCategory,
    Region,
    Sku,
    User,
)
from app.services.master_data.master_import_service import (
    BATCH_KIND_MASTER,
    STAGE_EXECUTED,
    MasterDataImportService,
)
from app.services.master_data.schemas import ParsedWorkbook
from app.services.master_data.workbook_parser import parse_workbook
from app.services.storage import get_storage_backend, object_key_for

router = APIRouter(prefix="/api/admin/master-data", tags=["admin/master-data"])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
# 工作簿对象键记在批次 payload 里（服务合并式写 payload，键不会被覆盖）
WORKBOOK_OBJECT_KEY = "workbook_object_key"
# .xlsx 是 zip 容器，文件头必为 PK
_XLSX_MAGIC = b"PK"


# ============== Schemas ==============


class BrandOut(BaseModel):
    id: str
    code: str
    name: str
    is_demo: bool
    is_active: bool


class RegionOut(BaseModel):
    id: str
    code: str
    name: str
    is_active: bool


class SkuOut(BaseModel):
    id: str
    code: str
    name: str
    brand_code: str
    category_name: str | None
    capacity_ml: int | None
    case_spec: str
    suggested_price_fen: int
    is_active: bool


class RoleRuleOut(BaseModel):
    """工作簿里的角色权限表：仅差异预览，不覆盖代码权限矩阵（PRD §2.2）。"""

    role_label: str
    role_code: str | None = None
    function_permissions: str | None = None
    data_permissions: str | None = None


class ImportAccountOut(BaseModel):
    """账号预览：只回展示与决定所需字段，**不含**任何密码列。"""

    login_name: str
    display_name: str
    source_role_label: str
    role_code: str
    region_codes: list[str]
    scope_global: bool
    is_supplier: bool
    pending_activation: bool
    suggested_enabled: bool


class PreflightReportOut(BaseModel):
    sku_count: int
    region_codes: list[str]
    account_count: int
    merchant_count: int
    contract_count: int
    unknown_regions: list[str]
    ignored_columns: list[str]
    warnings: list[str]
    ready: bool
    role_rules: list[RoleRuleOut]


class ImportBatchOut(BaseModel):
    id: str
    source_filename: str
    status: str
    stage: str
    file_hash: str
    seed_kind: str
    rows_total: int
    rows_imported: int
    rows_rejected: int
    imported_by_id: str | None
    created_at: datetime | None
    workbook_stored: bool
    ready: bool | None = None
    unknown_regions: list[str] = Field(default_factory=list)
    ignored_columns: list[str] = Field(default_factory=list)


class PreflightOut(BaseModel):
    batch: ImportBatchOut
    report: PreflightReportOut
    accounts: list[ImportAccountOut]


class ImportUploadOut(BaseModel):
    """上传响应。

    `idempotent_hit=true` 表示该文件此前已成功导入过：直接返回既有批次，
    `report`/`accounts` 为空，**不重新预检**（否则会把已执行批次降级回预检阶段）。
    """

    batch: ImportBatchOut
    report: PreflightReportOut | None = None
    accounts: list[ImportAccountOut] = Field(default_factory=list)
    idempotent_hit: bool = False


class ConfirmIn(BaseModel):
    # login_name -> 是否启用；缺省一律视为不启用
    account_enabled: dict[str, bool] = Field(default_factory=dict)


class CredentialOut(BaseModel):
    login_name: str
    role_code: str
    credential: str


class ExecuteOut(BaseModel):
    batch: ImportBatchOut
    credentials: list[CredentialOut]


# ============== 内部辅助 ==============


def _validate_upload(data: bytes, filename: str | None) -> None:
    if not data:
        raise DomainError(code="IMPORT_FILE_EMPTY", message="上传文件为空")
    if len(data) > MAX_UPLOAD_BYTES:
        raise DomainError(code="IMPORT_FILE_TOO_LARGE", message="工作簿不得超过 10MB")
    if filename and not filename.lower().endswith(".xlsx"):
        raise DomainError(code="IMPORT_FILE_TYPE", message="仅支持 .xlsx 工作簿")
    if not data.startswith(_XLSX_MAGIC):
        raise DomainError(code="IMPORT_FILE_TYPE", message="文件不是有效的 .xlsx 工作簿")


@contextmanager
def _temp_workbook(data: bytes) -> Iterator[str]:
    """解析器只接受文件路径：落临时文件，解析完立即删除（用完即弃）。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    try:
        tmp.write(data)
        tmp.close()
        yield tmp.name
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def _parse_bytes(data: bytes) -> ParsedWorkbook:
    with _temp_workbook(data) as path:
        return parse_workbook(path)


def _store_workbook(
    session: Session, batch: ImportBatch, data: bytes, filename: str
) -> None:
    """把工作簿落到对象存储（数据集隔离前缀）；已存在则跳过（幂等命中时）。"""
    payload = batch.payload_json or {}
    if payload.get(WORKBOOK_OBJECT_KEY):
        return
    key = object_key_for(
        batch.dataset_type or "DEMO",
        str(batch.dataset_id or uuid4()),
        "master-import",
        str(uuid4()),
        filename,
    )
    get_storage_backend().put(key, data)
    batch.payload_json = {**payload, WORKBOOK_OBJECT_KEY: key}
    session.flush()


def _parsed_of(session: Session, batch: ImportBatch) -> ParsedWorkbook:
    """拿解析结果：优先用同一请求内服务缓存的，否则从对象存储取回重解析。"""
    cached = session.info.get(f"t07_parsed_{batch.id}")
    if cached is not None:
        return cached  # type: ignore[no-any-return]
    key = (batch.payload_json or {}).get(WORKBOOK_OBJECT_KEY)
    if not key:
        raise DomainError(
            code="IMPORT_WORKBOOK_NOT_STORED",
            message="该批次的工作簿不在对象存储中，请重新上传后再预检 / 执行",
        )
    return _parse_bytes(get_storage_backend().get(str(key)))


def _load_batch(session: Session, batch_id: UUID) -> ImportBatch:
    batch = session.get(ImportBatch, batch_id)
    if batch is None or batch.seed_kind != BATCH_KIND_MASTER:
        raise DomainError(code="IMPORT_BATCH_NOT_FOUND", message="主数据导入批次不存在")
    return batch


def _batch_out(batch: ImportBatch) -> ImportBatchOut:
    payload = batch.payload_json or {}
    return ImportBatchOut(
        id=str(batch.id),
        source_filename=batch.source_filename,
        status=batch.status,
        stage=str(payload.get("stage") or ""),
        file_hash=batch.file_hash,
        seed_kind=batch.seed_kind,
        rows_total=batch.rows_total,
        rows_imported=batch.rows_imported,
        rows_rejected=batch.rows_rejected,
        imported_by_id=str(batch.imported_by_id) if batch.imported_by_id else None,
        created_at=batch.created_at,
        workbook_stored=bool(payload.get(WORKBOOK_OBJECT_KEY)),
        ready=payload.get("ready"),
        unknown_regions=list(payload.get("unknown_regions") or []),
        ignored_columns=list(payload.get("ignored_columns") or []),
    )


def _preflight_out(batch: ImportBatch, parsed: ParsedWorkbook, report) -> PreflightOut:
    return PreflightOut(
        batch=_batch_out(batch),
        report=PreflightReportOut(
            sku_count=report.sku_count,
            region_codes=report.region_codes,
            account_count=report.account_count,
            merchant_count=report.merchant_count,
            contract_count=report.contract_count,
            unknown_regions=report.unknown_regions,
            ignored_columns=report.ignored_columns,
            warnings=report.warnings,
            ready=report.ready,
            role_rules=[RoleRuleOut(**preview) for preview in report.role_preview],
        ),
        accounts=[
            ImportAccountOut(
                login_name=a.login_name,
                display_name=a.display_name,
                source_role_label=a.source_role_label,
                role_code=a.role_code,
                region_codes=list(a.region_codes),
                scope_global=a.scope_global,
                is_supplier=a.is_supplier,
                pending_activation=a.pending_activation,
                # 建议值只代表解析结论，最终由管理员逐个确认（厂商一律不启用）
                suggested_enabled=(
                    a.initial_active and not a.is_supplier and not a.pending_activation
                ),
            )
            for a in parsed.accounts
        ],
    )


# ============== 只读台账 ==============


def _require_region_manage(actor: ActorContext = Depends(current_actor)) -> ActorContext:  # noqa: B008
    """片区管理权限：SYSTEM_ADMIN（settings）或 REGIONAL_MANAGER（region.manage）。"""
    abilities = get_profile(actor.role_code).abilities
    if MODULE_SETTINGS not in abilities and MODULE_REGION_MANAGE not in abilities:
        raise PermissionDeniedError(
            code="PERMISSION_DENIED",
            message="当前角色无权管理片区",
            details={
                "required_any_of": [MODULE_SETTINGS, MODULE_REGION_MANAGE],
                "role": actor.role_code,
            },
        )
    return actor


@router.get("/brands", response_model=list[BrandOut])
def list_brands(
    actor: ActorContext = Depends(require_ability(MODULE_SETTINGS)),  # noqa: B008
    session: Session = Depends(db_session),  # noqa: B008
) -> list[BrandOut]:
    rows = session.execute(select(Brand).order_by(Brand.code.asc())).scalars().all()
    return [
        BrandOut(
            id=str(b.id), code=b.code, name=b.name, is_demo=b.is_demo,
            is_active=b.is_active,
        )
        for b in rows
    ]


@router.get("/regions", response_model=list[RegionOut])
def list_regions(
    actor: ActorContext = Depends(_require_region_manage),  # noqa: B008
    session: Session = Depends(db_session),  # noqa: B008
) -> list[RegionOut]:
    """片区台账：片区管理能力（SYSTEM_ADMIN / REGIONAL_MANAGER）即可读。"""
    rows = session.execute(select(Region).order_by(Region.code.asc())).scalars().all()
    return [
        RegionOut(id=str(r.id), code=r.code, name=r.name, is_active=r.is_active)
        for r in rows
    ]


class CreateRegionIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=128)


DEFAULT_ORG_CODE = "DEMO-QINSHUANG-ORG"


def _ensure_default_org(session: Session) -> Organization:
    """片区必须挂在组织下（regions.org_id NOT NULL）；复用导入流程的标准组织。"""
    org = session.execute(
        select(Organization).where(Organization.code == DEFAULT_ORG_CODE).limit(1)
    ).scalar_one_or_none()
    if org is not None:
        return org
    org = Organization(code=DEFAULT_ORG_CODE, name="沁爽演示组织")
    session.add(org)
    session.flush()
    return org


@router.post("/regions", response_model=RegionOut, status_code=status.HTTP_201_CREATED)
def create_region(
    payload: CreateRegionIn,
    actor: ActorContext = Depends(_require_region_manage),  # noqa: B008
    session: Session = Depends(db_session),  # noqa: B008
) -> RegionOut:
    """片区创建：SYSTEM_ADMIN（MODULE_SETTINGS）或 REGIONAL_MANAGER（MODULE_REGION_MANAGE）均可操作。

    片区编码全局唯一；重复编码返回 409（幂等同键不同参），不做静默覆盖。
    """
    code = payload.code.strip()
    exists = session.execute(
        select(Region).where(Region.code == code).limit(1)
    ).scalar_one_or_none()
    if exists is not None:
        raise ConflictError(
            code="REGION_CODE_EXISTS",
            message=f"片区编码 {code} 已存在，请换一个编码",
            details={"code": code, "region_id": str(exists.id)},
        )

    org = _ensure_default_org(session)
    region = Region(
        code=code,
        name=payload.name.strip(),
        org_id=org.id,
        created_by_id=actor.user_id,
    )
    session.add(region)
    session.flush()
    return RegionOut(
        id=str(region.id), code=region.code, name=region.name, is_active=region.is_active
    )


@router.get("/skus", response_model=list[SkuOut])
def list_skus(
    actor: ActorContext = Depends(require_ability(MODULE_SETTINGS)),  # noqa: B008
    session: Session = Depends(db_session),  # noqa: B008
) -> list[SkuOut]:
    rows = session.execute(
        select(Sku, Brand.code, ProductCategory.name)
        .join(Brand, Brand.id == Sku.brand_id)
        .outerjoin(ProductCategory, ProductCategory.id == Sku.category_id)
        .order_by(Sku.code.asc())
    ).all()
    return [
        SkuOut(
            id=str(s.id),
            code=s.code,
            name=s.name,
            brand_code=brand_code,
            category_name=category_name,
            capacity_ml=s.capacity_ml,
            case_spec=str(s.case_spec),
            suggested_price_fen=s.suggested_price_fen,
            is_active=s.is_active,
        )
        for s, brand_code, category_name in rows
    ]


class CreateSkuIn(BaseModel):
    """主数据新建 SKU（单条；批量仍走四步导入）。

    价格一律整数分；编码全局唯一，重复返回 409 而不是静默覆盖（主数据只停用不删除）。
    """

    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=128)
    brand_code: str = Field(min_length=1, max_length=32)
    capacity_ml: int | None = Field(default=None, ge=0)
    case_spec: Decimal | None = Field(default=None, ge=0)
    suggested_price_fen: int = Field(default=0, ge=0)
    purchase_price_fen: int | None = Field(default=None, ge=0)
    spec_text: str | None = Field(default=None, max_length=64)
    series_name: str | None = Field(default=None, max_length=64)
    category_name: str | None = Field(default=None, max_length=64)


@router.post("/skus", response_model=SkuOut, status_code=status.HTTP_201_CREATED)
def create_sku(
    payload: CreateSkuIn,
    actor: ActorContext = Depends(require_ability(MODULE_SETTINGS)),  # noqa: B008
    session: Session = Depends(db_session),  # noqa: B008
) -> SkuOut:
    """新建单个 SKU（系统管理员）：采购手工加行时蜜需，不必为此跑一次 Excel 导入。

    约束与服务端口径保持一致：编码唯一、品牌必须存在、金额为非负整数分。
    """
    code = payload.code.strip().upper()
    exists = session.execute(
        select(Sku).where(Sku.code == code).limit(1)
    ).scalar_one_or_none()
    if exists is not None:
        raise ConflictError(
            code="SKU_CODE_EXISTS",
            message=f"SKU 编码 {code} 已存在，请换一个编码",
            details={"code": code, "sku_id": str(exists.id)},
        )

    brand = session.execute(
        select(Brand).where(Brand.code == payload.brand_code.strip()).limit(1)
    ).scalar_one_or_none()
    if brand is None:
        raise DomainError(
            code="BRAND_NOT_FOUND",
            message=f"品牌 {payload.brand_code} 不存在，请先导入或新建品牌",
            details={"brand_code": payload.brand_code},
        )

    operator = session.get(User, actor.user_id)
    if operator is None:
        raise DomainError(code="USER_NOT_FOUND", message="操作人不存在")

    sku = Sku(
        code=code,
        name=payload.name.strip(),
        brand_id=brand.id,
        capacity_ml=payload.capacity_ml,
        case_spec=payload.case_spec if payload.case_spec is not None else Decimal("0"),
        suggested_price_fen=payload.suggested_price_fen,
        purchase_price_fen=payload.purchase_price_fen,
        spec_text=payload.spec_text,
        series_name=payload.series_name,
        category_name=payload.category_name,
        dataset_type=operator.dataset_type,
        dataset_id=operator.dataset_id,
        created_by_id=actor.user_id,
    )
    session.add(sku)
    session.flush()
    return SkuOut(
        id=str(sku.id),
        code=sku.code,
        name=sku.name,
        brand_code=brand.code,
        category_name=sku.category_name,
        capacity_ml=sku.capacity_ml,
        case_spec=str(sku.case_spec),
        suggested_price_fen=sku.suggested_price_fen,
        is_active=sku.is_active,
    )


# ============== T07 四步导入 ==============


@router.get("/imports", response_model=list[ImportBatchOut])
def list_imports(
    actor: ActorContext = Depends(require_ability(MODULE_SETTINGS)),  # noqa: B008
    session: Session = Depends(db_session),  # noqa: B008
) -> list[ImportBatchOut]:
    """最近 50 个主数据批次（含阶段与行数，状态机见 payload.stage）。"""
    rows = (
        session.execute(
            select(ImportBatch)
            .where(ImportBatch.seed_kind == BATCH_KIND_MASTER)
            .order_by(ImportBatch.created_at.desc())
            .limit(50)
        )
        .scalars()
        .all()
    )
    return [_batch_out(b) for b in rows]


@router.post(
    "/imports", response_model=ImportUploadOut, status_code=status.HTTP_201_CREATED
)
async def upload_workbook(
    file: UploadFile = File(...),  # noqa: B008
    actor: ActorContext = Depends(require_ability(MODULE_SETTINGS)),  # noqa: B008
    session: Session = Depends(db_session),  # noqa: B008
) -> ImportUploadOut:
    """第一步 + 第二步：上传工作簿并立即预检。

    同一文件（SHA-256 相同且已有**成功**批次）会直接返回既有批次，不重复落库（A21）；
    此时不做预检，避免把已执行批次的阶段覆盖回预检。
    """
    data = await file.read()
    filename = file.filename or "master-data.xlsx"
    _validate_upload(data, file.filename)

    service = MasterDataImportService(session)
    with _temp_workbook(data) as path:
        batch = service.upload(path=path, actor_user_id=actor.user_id, filename=filename)

    # 工作簿已存过 ⇒ 本次是幂等命中既有批次
    idempotent_hit = bool((batch.payload_json or {}).get(WORKBOOK_OBJECT_KEY))
    _store_workbook(session, batch, data, filename)

    if idempotent_hit and (batch.payload_json or {}).get("stage") == STAGE_EXECUTED:
        session.commit()
        return ImportUploadOut(batch=_batch_out(batch), idempotent_hit=True)

    parsed = _parsed_of(session, batch)
    report = service.preflight(batch, parsed=parsed)
    session.commit()
    out = _preflight_out(batch, parsed, report)
    return ImportUploadOut(
        batch=out.batch, report=out.report, accounts=out.accounts,
        idempotent_hit=idempotent_hit,
    )


@router.post("/imports/{batch_id}/preflight", response_model=PreflightOut)
def preflight_import(
    batch_id: UUID,
    actor: ActorContext = Depends(require_ability(MODULE_SETTINGS)),  # noqa: B008
    session: Session = Depends(db_session),  # noqa: B008
) -> PreflightOut:
    """第二步：重新预检（未知片区拦截、忽略列与角色规则预览）。"""
    batch = _load_batch(session, batch_id)
    service = MasterDataImportService(session)
    parsed = _parsed_of(session, batch)
    report = service.preflight(batch, parsed=parsed)
    session.commit()
    return _preflight_out(batch, parsed, report)


@router.post("/imports/{batch_id}/confirm", response_model=ImportBatchOut)
def confirm_import(
    batch_id: UUID,
    payload: ConfirmIn,
    actor: ActorContext = Depends(require_ability(MODULE_SETTINGS)),  # noqa: B008
    session: Session = Depends(db_session),  # noqa: B008
) -> ImportBatchOut:
    """第三步：管理员逐个确认账号是否启用（只接受布尔值，不接受密码）。"""
    batch = _load_batch(session, batch_id)
    service = MasterDataImportService(session)
    parsed = _parsed_of(session, batch)
    service.confirm(
        batch,
        actor_user_id=actor.user_id,
        account_decisions=payload.account_enabled,
        parsed=parsed,
    )
    session.commit()
    return _batch_out(batch)


@router.post("/imports/{batch_id}/execute", response_model=ExecuteOut)
def execute_import(
    batch_id: UUID,
    actor: ActorContext = Depends(require_ability(MODULE_SETTINGS)),  # noqa: B008
    session: Session = Depends(db_session),  # noqa: B008
) -> ExecuteOut:
    """第四步：单事务执行（全有或全无）；激活凭据只在此一次性返回。"""
    batch = _load_batch(session, batch_id)
    service = MasterDataImportService(session)
    parsed = _parsed_of(session, batch)
    credentials = service.execute(batch, actor_user_id=actor.user_id, parsed=parsed)
    session.commit()
    return ExecuteOut(
        batch=_batch_out(batch),
        credentials=[
            CredentialOut(
                login_name=c.login_name, role_code=c.role_code, credential=c.credential
            )
            for c in credentials
        ],
    )

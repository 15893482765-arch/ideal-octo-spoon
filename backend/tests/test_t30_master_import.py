"""T30 全链路 UAT · 主数据 Excel 导入（PRD A20/A21/A22/A23）。

T07 已实现完整的“上传→预检→确认→执行”演示主数据导入（强制 DEMO 数据集），
此前仅有种子校验，没有走真实 openpyxl 工作簿的端到端测试。本文件以真实
xlsx（夹具格式工作表：片区/主数据SKU/主数据账号）驱动 MasterDataImportService，
覆盖：
- A20 预检拦截未知片区；成功批次可追溯（用户/SKU 落库）；
- A21 相同文件 SHA-256 重复上传只返回已有批次，不重复落库；
- A22 源表“登录密码”列被预检提示忽略，库 / 批次 payload / 审计均无明文；
- A23 厂商账号导入后为待激活且不发凭据（门户门禁见 test_q014）。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from openpyxl import Workbook
from sqlalchemy import func, select

from app.models.audit import AuditLog
from app.models.enums import UserStatus
from app.models.product import Sku
from app.models.user import User
from app.services.master_data.master_import_service import MasterDataImportService

REGION_HEADER = ["region_code", "片区名称", "城市", "状态"]
SKU_HEADER = [
    "source_row_key", "sku_code", "brand_code", "产品名称", "系列", "品类",
    "规格", "建议零售价(分)", "参考进价(分)", "卖点", "保质期", "目标渠道",
]
ACCOUNT_HEADER = [
    "source_row_key", "login_name", "姓名", "演示手机号", "源角色", "role_code",
    "region_scope", "brand_scope", "initial_status",
]


def _wb(path: Path, sheets: dict[str, list[list[object]]]) -> str:
    wb = Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title)
        for row in rows:
            ws.append(row)
    wb.save(path)
    return str(path)


def _regions(*rows: list[object]) -> list[list[object]]:
    return [REGION_HEADER, *rows]


def _skus(*rows: list[object]) -> list[list[object]]:
    return [SKU_HEADER, *rows]


def _accounts(*rows: list[object]) -> list[list[object]]:
    return [ACCOUNT_HEADER, *rows]


# 标准好数据
GOOD_REGION = ["DEMO-JS", "金水片区", "郑州", "ACTIVE"]
GOOD_SKU = [
    "sku:9001", "SKU9001", "QINSHUANG", "导入柠檬茶", "经典系列", "茶饮",
    "500ml/瓶×24瓶", 350, 220, "清新", "12个月", "便利店",
]
GOOD_SALES = [
    "acc:rep9001", "rep9001", "导入业务员甲", "13800009001", "业务员",
    "SALES", "DEMO-JS", "QINSHUANG", "ACTIVE",
]


def _write_good(path: Path, *, with_password: bool = False,
                supplier: bool = False) -> str:
    sheets = {
        "片区": _regions(GOOD_REGION),
        "主数据SKU": _skus(GOOD_SKU),
    }
    if supplier:
        acc = [
            "acc:sup9001", "sup9001", "导入厂商", None, "厂商",
            "SUPPLIER", "*", "QINSHUANG", "ACTIVE",
        ]
    else:
        acc = list(GOOD_SALES)
    acc_rows = [acc]
    headers = list(ACCOUNT_HEADER)
    if with_password:
        headers = headers + ["登录密码"]
        acc_rows = [acc + ["Pl@in-Secret-123456"]]
    sheets["主数据账号"] = [headers, *acc_rows]
    return _wb(path, sheets)


def _svc(session) -> MasterDataImportService:
    return MasterDataImportService(session)


def _admin_id(session) -> uuid.UUID:
    """导入/确认/执行的操作者必须是真实存在的用户（FK 约束）。

    这里**自建**系统管理员（本用例事务内 flush，不外提交），不再 select 其它测试
    文件提交的全局夹具：原先的实现要求「某个提交过 bootstrap_admin/all_roles 的
    用例先跑过」，否则 admin_role/admin 为 None → `'NoneType' object has no
    attribute 'id'`。单独运行本文件、或与 test_migrations（会重建 schema，清空
    已提交夹具）同跑时必然复现，全量运行也会因夹具残留与否而随机红。
    """
    from app.core.security import hash_password
    from app.models.enums import RoleCode, UserStatus
    from app.models.user import Role, User

    role_code = RoleCode.SYSTEM_ADMIN.value
    admin_role = session.scalar(select(Role).where(Role.role_code == role_code))
    if admin_role is None:
        admin_role = Role(
            code=f"T30-{role_code}-{uuid.uuid4().hex[:8]}",
            name="系统管理员",
            role_code=role_code,
            dataset_type="PRODUCTION",
            dataset_id=uuid.uuid4(),
        )
        session.add(admin_role)
        session.flush()

    admin = session.scalar(select(User).where(User.role_id == admin_role.id))
    if admin is None:
        admin = User(
            username=f"t30_admin_{uuid.uuid4().hex[:8]}",
            display_name="T30 管理员",
            role_id=admin_role.id,
            status=UserStatus.ACTIVE.value,
            must_change_password=False,
            password_hash=hash_password("T30-Test-Pass-9876"),
            dataset_type="PRODUCTION",
            dataset_id=uuid.uuid4(),
        )
        session.add(admin)
        session.flush()
    return admin.id


# ---------------------------------------------------------------------------
# A20
# ---------------------------------------------------------------------------

def test_q174_a20_preflight_blocks_unknown_region(session, tmp_path):
    svc = _svc(session)
    path = _wb(
        tmp_path / "bad_region.xlsx",
        {
            "片区": _regions(GOOD_REGION),
            "主数据SKU": _skus(GOOD_SKU),
            "主数据账号": _accounts(
                ["acc:r", "rep9100", "甲", None, "业务员", "SALES",
                 "DEMO-UNKNOWN", "QINSHUANG", "ACTIVE"]
            ),
        },
    )
    batch = svc.upload(path=path, actor_user_id=_admin_id(session),
                       filename="bad_region.xlsx")
    report = svc.preflight(batch)
    assert report.unknown_regions
    assert not report.ready


def test_q174_a20_preflight_blocks_duplicate_login_and_sku(session, tmp_path):
    """文件内重复 SKU 与重复登录账号在上传即被 _prevalidate 硬拒绝（DomainError）。"""
    from app.core.errors import DomainError

    def _build() -> str:
        return _wb(
            tmp_path / "dup.xlsx",
            {
                "片区": _regions(GOOD_REGION),
                "主数据账号": _accounts(
                    ["acc:a", "rep9200", "甲", None, "业务员", "SALES",
                     "DEMO-JS", "QINSHUANG", "ACTIVE"],
                    ["acc:b", "rep9200", "乙重复", None, "业务员", "SALES",
                     "DEMO-JS", "QINSHUANG", "ACTIVE"],
                ),
                "主数据SKU": _skus(
                    ["sku:a", "SKU9200", "QINSHUANG", "A",
                     "系", "类", "500ml", 300, 200, "x", "12", "便利店"],
                    ["sku:b", "SKU9200", "QINSHUANG", "A重复",
                     "系", "类", "500ml", 300, 200, "x", "12", "便利店"],
                ),
            },
        )

    svc = _svc(session)
    with pytest.raises(DomainError) as e:
        svc.upload(path=_build(), actor_user_id=_admin_id(session), filename="dup.xlsx")
    assert e.value.code in {"IMPORT_DUPLICATE_SKU", "IMPORT_DUPLICATE_ACCOUNT"}
    # 批次不得创建（重复文件零写入）
    from app.models.audit import ImportBatch

    assert session.scalar(select(func.count()).select_from(ImportBatch)) == 0


def test_q174_a20_happy_path_batch_succeeds_and_traceable(session, tmp_path):
    svc = _svc(session)
    path = _write_good(tmp_path / "ok.xlsx")
    batch = svc.upload(path=path, actor_user_id=_admin_id(session), filename="ok.xlsx")
    report = svc.preflight(batch)
    assert report.ready, report.unknown_regions
    svc.confirm(batch, actor_user_id=_admin_id(session),
                account_decisions={"rep9001": True})
    creds = svc.execute(batch, actor_user_id=_admin_id(session))
    assert batch.status == "SUCCEEDED"
    assert any(c.login_name == "rep9001" for c in creds)  # 一次性凭据只返回一次
    assert session.scalar(select(User).where(User.username == "rep9001")) is not None
    assert session.scalar(select(Sku).where(Sku.code == "SKU9001")) is not None


# ---------------------------------------------------------------------------
# A21
# ---------------------------------------------------------------------------

def test_q174_a21_same_file_hash_is_idempotent(session, tmp_path):
    svc = _svc(session)
    aid = _admin_id(session)
    src = tmp_path / "a.xlsx"
    first = svc.upload(path=_write_good(src), actor_user_id=aid, filename="a.xlsx")
    svc.preflight(first)
    svc.confirm(first, actor_user_id=aid,
                account_decisions={"rep9001": True})
    svc.execute(first, actor_user_id=aid)

    # 不同文件名、相同字节 → 按文件哈希返回同一批次。
    # 必须复用第一次写出的字节：openpyxl 会把写入时刻带进 zip，两次独立生成的工作簿
    # 未必逐字节相同（跨秒即不同），那样「相同字节」前提不成立，用例会随执行时刻变红。
    alias = tmp_path / "different-name.xlsx"
    alias.write_bytes(Path(src).read_bytes())
    again = svc.upload(
        path=str(alias), actor_user_id=aid, filename="different-name.xlsx",
    )
    assert again.id == first.id
    assert session.scalar(
        select(func.count()).select_from(User).where(User.username == "rep9001")
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(Sku).where(Sku.code == "SKU9001")
    ) == 1


def test_q174_a21b_hash_hit_requires_same_dataset_and_succeeded(session, tmp_path):
    """A21 加固：文件哈希幂等只在「本数据集 + 已成功」时成立。

    回归背景：原实现只按 file_hash 查询，任何同字节批次都会劫持上传——
    未成功批次让重传永远停在旧阶段（无法重试），其它数据集的批次会把内容
    错并入本数据集（跨用例污染也会因此产生随机失败）。
    """
    from app.models.audit import ImportBatch

    svc = _svc(session)
    aid = _admin_id(session)
    path = _write_good(tmp_path / "a.xlsx")

    first = svc.upload(path=path, actor_user_id=aid, filename="a.xlsx")

    # 1) 未成功（仍是 PENDING）不算命中：同字节重传必须新建批次，否则失败后无法重试
    again = svc.upload(path=path, actor_user_id=aid, filename="a-2.xlsx")
    assert again.id != first.id
    assert again.status == "PENDING"

    # 2) 其它数据集的同字节成功批次不得劫持本数据集上传（此处它还是最新的一行，
    #    所以若缺少 dataset 过滤，下面的 fresh 就会错误地命中它）
    other = svc.upload(path=path, actor_user_id=aid, filename="a-3.xlsx")
    other.dataset_id = uuid.uuid4()
    other.status = "SUCCEEDED"
    session.flush()
    assert other.id != first.id

    fresh = svc.upload(path=path, actor_user_id=aid, filename="a-4.xlsx")
    assert fresh.id not in {first.id, again.id, other.id}
    assert session.scalar(select(func.count()).select_from(ImportBatch)) == 4

    # 3) 本数据集的批次执行成功后，才作为幂等命中返回同一批次
    svc.preflight(first)
    svc.confirm(first, actor_user_id=aid, account_decisions={"rep9001": True})
    svc.execute(first, actor_user_id=aid)
    hit = svc.upload(path=path, actor_user_id=aid, filename="a-5.xlsx")
    assert hit.id == first.id
    # 命中既有批次不应新建第 5 行
    assert session.scalar(select(func.count()).select_from(ImportBatch)) == 4


# ---------------------------------------------------------------------------
# A22
# ---------------------------------------------------------------------------

def test_q174_a22_password_column_ignored_and_never_persisted(session, tmp_path):
    svc = _svc(session)
    path = _write_good(tmp_path / "withpw.xlsx", with_password=True)
    batch = svc.upload(path=path, actor_user_id=_admin_id(session),
                       filename="withpw.xlsx")

    report = svc.preflight(batch)
    assert any("密码" in c for c in report.ignored_columns), report.ignored_columns

    svc.confirm(batch, actor_user_id=_admin_id(session),
                account_decisions={"rep9001": True})
    svc.execute(batch, actor_user_id=_admin_id(session))

    user = session.scalar(select(User).where(User.username == "rep9001"))
    assert user is not None
    assert user.password_hash.startswith("$argon2id$")
    assert "Pl@in-Secret-123456" not in user.password_hash

    assert "Pl@in-Secret-123456" not in str(batch.payload_json)
    for log in session.scalars(select(AuditLog)).all():
        for col in (log.after_json, log.before_json, log.payload):
            assert "Pl@in-Secret-123456" not in str(col)


# ---------------------------------------------------------------------------
# A23
# ---------------------------------------------------------------------------

def test_q174_a23_supplier_import_is_pending_activation(session, tmp_path):
    svc = _svc(session)
    path = _write_good(tmp_path / "supplier.xlsx", supplier=True)
    batch = svc.upload(path=path, actor_user_id=_admin_id(session),
                       filename="supplier.xlsx")
    report = svc.preflight(batch)
    assert report.ready
    svc.confirm(batch, actor_user_id=_admin_id(session),
                account_decisions={"sup9001": True})
    creds = svc.execute(batch, actor_user_id=_admin_id(session))
    # 厂商即便确认也不发凭据、保持待激活，等待门户开启+授权重发
    assert creds == []
    sup = session.scalar(select(User).where(User.username == "sup9001"))
    assert sup is not None
    assert sup.status == UserStatus.PENDING_ACTIVATION.value

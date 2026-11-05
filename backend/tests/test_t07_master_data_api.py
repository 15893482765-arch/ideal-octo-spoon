"""T07 主数据导入 HTTP 联调：上传 → 预检 → 确认 → 执行（外加台账只读接口）。

解析/落库规则本身由 `test_t30_master_import.py` 在服务层覆盖；本文件补的是
HTTP 链路，尤其是**跨请求**行为：
- 服务的解析结果只活在 `session.info`（单请求），因此预检/确认/执行必须能从
  对象存储取回工作簿重新解析 —— 这是 API 层最容易出错的地方；
- 成功批次重复上传同一文件走幂等：直接返回既有批次，不得把阶段覆盖回预检；
- 台账（品牌 / 片区 / SKU / 批次列表）只读且需要 `settings` 能力；
- 片区是唯一例外：SYSTEM_ADMIN（settings）与 REGIONAL_MANAGER（region.manage）
  均可新建，编码冲突返回 409（T02 片区管理需求）。

用例统一用 `isolated_client`（conftest）：导入服务内部会 commit，普通 client 会把
演示主数据真实写进测试库，污染依赖全表计数的既有用例。

对应既有验收条目 A20/A21/A22 的接口面，不新增 Q 编号。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy.orm import Session
from tests.conftest import TEST_PASSWORD_ALT

REGION_HEADER = ["region_code", "片区名称", "城市", "状态"]
SKU_HEADER = [
    "source_row_key", "sku_code", "brand_code", "产品名称", "系列", "品类",
    "规格", "建议零售价(分)", "参考进价(分)", "卖点", "保质期", "目标渠道",
]
ACCOUNT_HEADER = [
    "source_row_key", "login_name", "姓名", "演示手机号", "源角色", "role_code",
    "region_scope", "brand_scope", "initial_status",
]

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
BASE = "/api/admin/master-data"


def _write_workbook(
    path: Path, *, region_code: str, sku_code: str, login: str
) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)

    ws = wb.create_sheet("片区")
    ws.append(REGION_HEADER)
    ws.append([region_code, f"{region_code} 片区", "郑州", "ACTIVE"])

    ws2 = wb.create_sheet("主数据SKU")
    ws2.append(SKU_HEADER)
    ws2.append([
        f"sku:{sku_code}", sku_code, "QINSHUANG", "接口柠檬茶", "经典系列", "茶饮",
        "500ml/瓶×24瓶", 350, 220, "清新", "12个月", "便利店",
    ])

    ws3 = wb.create_sheet("主数据账号")
    ws3.append(ACCOUNT_HEADER)
    # role_code 列按解析器约定填简写（SALES → SALES_REP），与源表格式一致
    ws3.append([
        f"acc:{login}", login, "接口业务员", "13800009111", "业务员",
        "SALES", region_code, "QINSHUANG", "ACTIVE",
    ])

    wb.save(path)
    return path.read_bytes()


def _admin_headers(isolated_client: TestClient, admin: dict[str, str]) -> dict[str, str]:
    token = isolated_client.post(
        "/api/auth/login",
        json={"username": admin["username"], "password": admin["password"]},
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _upload(
    isolated_client: TestClient,
    headers: dict[str, str],
    content: bytes,
    name: str = "master.xlsx",
):
    return isolated_client.post(
        f"{BASE}/imports",
        files={"file": (name, content, XLSX_MIME)},
        headers=headers,
    )


def test_t07_api_controlplane_and_ability(
    isolated_client: TestClient, bootstrap_admin
) -> None:
    """台账/批次列表需要 settings 能力；未认证一律 401；非 xlsx 被拒。"""
    assert isolated_client.get(f"{BASE}/brands").status_code == 401
    assert isolated_client.get(f"{BASE}/skus").status_code == 401

    headers = _admin_headers(isolated_client, bootstrap_admin)
    assert isolated_client.get(f"{BASE}/brands", headers=headers).status_code == 200
    assert isolated_client.get(f"{BASE}/regions", headers=headers).status_code == 200
    assert isolated_client.get(f"{BASE}/skus", headers=headers).status_code == 200
    assert isolated_client.get(f"{BASE}/imports", headers=headers).status_code == 200

    bad = _upload(isolated_client, headers, b"a,b,c", name="master.csv")
    assert bad.status_code in {400, 422}, bad.text
    empty = _upload(isolated_client, headers, b"", name="master.xlsx")
    assert empty.status_code in {400, 422}, empty.text


def test_t07_api_four_step_import_and_idempotency(
    isolated_client: TestClient, bootstrap_admin, tmp_path: Path
) -> None:
    """上传 → 预检 → 确认 → 执行全链路，并验证成功批次重复上传的幂等语义。"""
    headers = _admin_headers(isolated_client, bootstrap_admin)
    suffix = uuid.uuid4().hex[:6].upper()
    content = _write_workbook(
        tmp_path / "master.xlsx",
        region_code=f"API-R{suffix}",
        sku_code=f"APISKU{suffix}",
        login=f"api_rep_{suffix.lower()}",
    )

    # 第一步 + 第二步：上传即预检
    first = _upload(isolated_client, headers, content)
    assert first.status_code == 201, first.text
    body = first.json()
    batch_id = body["batch"]["id"]
    assert body["idempotent_hit"] is False
    assert body["batch"]["stage"] == "PREFLIGHT"
    assert body["report"]["ready"] is True
    assert body["report"]["sku_count"] == 1
    assert body["report"]["region_codes"] == [f"API-R{suffix}"]
    logins = [a["login_name"] for a in body["accounts"]]
    assert logins == [f"api_rep_{suffix.lower()}"]

    # 跨请求预检：本次请求的 session.info 里没有解析结果 → 从对象存储重建
    pre = isolated_client.post(
        f"{BASE}/imports/{batch_id}/preflight", headers=headers
    )
    assert pre.status_code == 200, pre.text
    assert pre.json()["report"]["ready"] is True
    assert [a["login_name"] for a in pre.json()["accounts"]] == logins

    # 第三步：确认（只接受布尔决定）
    confirm = isolated_client.post(
        f"{BASE}/imports/{batch_id}/confirm",
        json={"account_enabled": {logins[0]: True}},
        headers=headers,
    )
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["stage"] == "CONFIRMED"

    # 第四步：执行 → 一次性激活凭据
    executed = isolated_client.post(
        f"{BASE}/imports/{batch_id}/execute", headers=headers
    )
    assert executed.status_code == 200, executed.text
    result = executed.json()
    assert result["batch"]["stage"] == "EXECUTED"
    assert result["batch"]["status"] == "SUCCEEDED"
    assert [c["login_name"] for c in result["credentials"]] == logins
    assert result["credentials"][0]["credential"]

    # A21 幂等：执行成功后重复上传同一文件 → 既有批次，且阶段不被覆盖回预检
    again = _upload(isolated_client, headers, content)
    assert again.status_code == 201, again.text
    repeat = again.json()
    assert repeat["batch"]["id"] == batch_id
    assert repeat["idempotent_hit"] is True
    assert repeat["batch"]["stage"] == "EXECUTED"
    assert repeat["report"] is None and repeat["accounts"] == []

    # 台账与批次列表已反映落库结果
    skus = isolated_client.get(f"{BASE}/skus", headers=headers).json()
    assert f"APISKU{suffix}" in {s["code"] for s in skus}
    batches = isolated_client.get(f"{BASE}/imports", headers=headers).json()
    assert batch_id in {b["id"] for b in batches}


# ============== 片区新建（T02 片区管理） ==============


def _active_role_user(
    session: Session, *, role_id: UUID, prefix: str
) -> dict[str, str]:
    """在隔离会话内建一个可直接登录的账号（随用例回滚，不污染测试库）。"""
    from app.core.security import hash_password
    from app.models import User
    from app.models.enums import UserStatus

    user = User(
        username=f"{prefix}_{uuid.uuid4().hex[:8]}",
        display_name=f"{prefix} 测试账号",
        role_id=role_id,
        status=UserStatus.ACTIVE.value,
        must_change_password=False,
        password_hash=hash_password(TEST_PASSWORD_ALT),
        dataset_type="PRODUCTION",
        dataset_id=uuid.uuid4(),
    )
    session.add(user)
    session.flush()
    return {"username": user.username, "password": TEST_PASSWORD_ALT}


def _login_headers(isolated_client: TestClient, creds: dict[str, str]) -> dict[str, str]:
    token = isolated_client.post(
        "/api/auth/login",
        json={"username": creds["username"], "password": creds["password"]},
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_region_create_requires_admin_ability(
    isolated_client: TestClient, bootstrap_admin
) -> None:
    """未认证 401；SYSTEM_ADMIN 可建；重复编码 409；创建后出现在台账。"""
    payload = {"code": f"R{uuid.uuid4().hex[:8].upper()}", "name": "接口新建片区"}

    assert isolated_client.post(f"{BASE}/regions", json=payload).status_code == 401

    headers = _admin_headers(isolated_client, bootstrap_admin)
    created = isolated_client.post(f"{BASE}/regions", json=payload, headers=headers)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["code"] == payload["code"]
    assert body["name"] == payload["name"]
    assert body["is_active"] is True

    # 编码唯一：同码再建 → 409（不静默覆盖）
    dup = isolated_client.post(f"{BASE}/regions", json=payload, headers=headers)
    assert dup.status_code == 409, dup.text
    assert dup.json()["code"] == "REGION_CODE_EXISTS"

    codes = {r["code"] for r in isolated_client.get(f"{BASE}/regions", headers=headers).json()}
    assert payload["code"] in codes


def test_create_sku_endpoint(
    isolated_client: TestClient,
    isolated_session: Session,
    bootstrap_admin: dict[str, str],
) -> None:
    """主数据单条新建 SKU：用于采购手工加行（如「沁爽矿泉水」）。

    - 编码重复 409（不静默覆盖）；
    - 品牌不存在被拒；
    - 新建后出现在台账列表。
    """
    from app.models import Brand

    brand = Brand(code=f"T07B{uuid.uuid4().hex[:6].upper()}", name="T07 品牌")
    isolated_session.add(brand)
    isolated_session.flush()

    headers = _admin_headers(isolated_client, bootstrap_admin)
    code = f"T07-SKU-{uuid.uuid4().hex[:6].upper()}"
    payload = {
        "code": code,
        "name": "沁爽矿泉水",
        "brand_code": brand.code,
        "capacity_ml": 550,
        "case_spec": 24,
        "suggested_price_fen": 200,
        "purchase_price_fen": 120,
        "spec_text": "550ml/瓶×24瓶",
    }

    assert isolated_client.post(f"{BASE}/skus", json=payload).status_code == 401

    created = isolated_client.post(f"{BASE}/skus", json=payload, headers=headers)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["code"] == code
    assert body["name"] == "沁爽矿泉水"
    assert body["brand_code"] == brand.code

    dup = isolated_client.post(f"{BASE}/skus", json=payload, headers=headers)
    assert dup.status_code == 409, dup.text
    assert dup.json()["code"] == "SKU_CODE_EXISTS"

    bad_brand = isolated_client.post(
        f"{BASE}/skus",
        json={**payload, "code": f"{code}-X", "brand_code": "NO-SUCH-BRAND"},
        headers=headers,
    )
    # 项目统一约定：DomainError -> 422（引用/业务校验失败都走同一映射）
    assert bad_brand.status_code == 422, bad_brand.text
    assert bad_brand.json()["code"] == "BRAND_NOT_FOUND"

    codes = {s["code"] for s in isolated_client.get(f"{BASE}/skus", headers=headers).json()}
    assert code in codes


def test_region_create_allows_regional_manager(
    isolated_client: TestClient,
    isolated_session: Session,
    all_roles: dict[str, UUID],
) -> None:
    """REGIONAL_MANAGER 具备 region.manage，可新建片区。"""
    from app.models.enums import RoleCode

    creds = _active_role_user(
        isolated_session,
        role_id=all_roles[RoleCode.REGIONAL_MANAGER.value],
        prefix="t07_mgr",
    )
    headers = _login_headers(isolated_client, creds)
    payload = {"code": f"RM{uuid.uuid4().hex[:8].upper()}", "name": "片区经理新建片区"}

    resp = isolated_client.post(f"{BASE}/regions", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    assert resp.json()["code"] == payload["code"]


def test_region_create_forbidden_for_sales_rep(
    isolated_client: TestClient,
    isolated_session: Session,
    all_roles: dict[str, UUID],
) -> None:
    """业务员既无 settings 也无 region.manage → 403。"""
    from app.models.enums import RoleCode

    creds = _active_role_user(
        isolated_session,
        role_id=all_roles[RoleCode.SALES_REP.value],
        prefix="t07_sales",
    )
    headers = _login_headers(isolated_client, creds)

    resp = isolated_client.post(
        f"{BASE}/regions",
        json={"code": f"SR{uuid.uuid4().hex[:8].upper()}", "name": "不应创建"},
        headers=headers,
    )
    assert resp.status_code == 403, resp.text

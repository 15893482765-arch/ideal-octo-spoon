"""T35 薪资配置 + 绩效自动重算。

覆盖：
- 薪资配置：录入/列表/停用，重复生效日 409，非财务 403；
- 工资草稿底薪自动取「归属月首日生效」的配置，缺失报 SALARY_CONFIG_MISSING；
- 历史版本：新增版本不影响已生成工资单（工资单自带底薪快照）；
- 绩效自动重算：无快照建 DRAFT、DRAFT 就地重算、已确认快照不变。
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from tests.test_t09_contract import _user
from tests.test_t19_performance import (
    MONTH,
    PARAMS_V1,
    _activity,
    _actor,
    _auth,
    _contract,
    _rule,
    _setup,
)

from app.core.errors import DomainError, PermissionDeniedError
from app.models.enums import RoleCode
from app.services.performance_snapshot_service import PerformanceSnapshotService
from app.services.salary_config_service import SalaryConfigService

PASSWORD = "t09-pass-not-for-prod"
BASE = "/api/payroll"


def _headers(client: TestClient, username: str) -> dict[str, str]:
    resp = client.post(
        "/api/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


# ============================ 服务层 ============================

def test_salary_config_resolve_and_history(session: Session) -> None:
    w, mgr, mgr_actor, fin, rep, _r2, merchant = _setup(session)
    svc = SalaryConfigService(session)
    fin_actor = _actor(fin, RoleCode.FINANCE, w)
    _auth(session, rep, w)

    # 区域经理不具备 payroll 能力
    with pytest.raises(PermissionDeniedError):
        svc.create_config(
            mgr_actor, user_id=rep.id, base_salary_fen=500_000,
            effective_from=date(2026, 1, 1))

    v1 = svc.create_config(
        fin_actor, user_id=rep.id, base_salary_fen=500_000,
        effective_from=date(2026, 1, 1), note="2026 起薪")
    assert v1.base_salary_fen == 500_000

    # 同一生效日重复录入 → 冲突
    from app.core.errors import ConflictError

    with pytest.raises(ConflictError) as exc:
        svc.create_config(
            fin_actor, user_id=rep.id, base_salary_fen=600_000,
            effective_from=date(2026, 1, 1))
    assert exc.value.code == "SALARY_CONFIG_DUPLICATED"

    # 六月调薪：新版本不影响历史月份解析
    svc.create_config(
        fin_actor, user_id=rep.id, base_salary_fen=600_000,
        effective_from=date(2026, 6, 1))
    assert svc.resolve_config(
        rep.id, on_date=date(2026, 5, 1), dataset_id=w["ds"]
    ).base_salary_fen == 500_000
    assert svc.resolve_config(
        rep.id, on_date=date(2026, 6, 1), dataset_id=w["ds"]
    ).base_salary_fen == 600_000
    assert svc.resolve_config(
        rep.id, on_date=date(2026, 7, 15), dataset_id=w["ds"]
    ).base_salary_fen == 600_000

    # 停用后不再参与解析（历史工资单不受影响）
    svc.toggle_active(fin_actor, svc.resolve_config(
        rep.id, on_date=date(2026, 7, 1), dataset_id=w["ds"]).id)
    assert svc.resolve_config(
        rep.id, on_date=date(2026, 7, 1), dataset_id=w["ds"]
    ).base_salary_fen == 500_000

    rows = svc.list_configs(fin_actor, user_id=rep.id)
    assert len(rows) == 2
    assert {c.base_salary_fen for c, _name in rows} == {500_000, 600_000}


def test_salary_config_missing_blocks_generation(session: Session) -> None:
    w, _mgr, mgr_actor, _fin, rep, _r2, _merchant = _setup(session)
    from app.services.payroll_service import PayrollService

    with pytest.raises(DomainError) as exc:
        PayrollService(session).generate_statement(
            mgr_actor, user_id=rep.id, period_month="2026-06")
    assert exc.value.code == "SALARY_CONFIG_MISSING"


# ============================ 接口层 ============================

def test_salary_config_api_flow(
    isolated_client: TestClient, isolated_session: Session
) -> None:
    ds = uuid4()
    fin = _user(isolated_session, ds, RoleCode.FINANCE, "fin35sal")
    rep = _user(isolated_session, ds, RoleCode.SALES_REP, "rep35sal")
    isolated_session.flush()
    headers = _headers(isolated_client, fin.username)

    assert isolated_client.get(f"{BASE}/salary-configs").status_code == 401

    created = isolated_client.post(
        f"{BASE}/salary-configs",
        json={
            "user_id": str(rep.id),
            "base_salary_fen": 480_000,
            "effective_from": "2026-01-01",
            "note": "2026 年底薪",
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["base_salary_fen"] == 480_000
    assert body["user_name"] == rep.display_name
    assert body["is_active"] is True

    dup = isolated_client.post(
        f"{BASE}/salary-configs",
        json={
            "user_id": str(rep.id),
            "base_salary_fen": 500_000,
            "effective_from": "2026-01-01",
        },
        headers=headers,
    )
    assert dup.status_code == 409, dup.text
    assert dup.json()["code"] == "SALARY_CONFIG_DUPLICATED"

    listed = isolated_client.get(
        f"{BASE}/salary-configs", params={"user_id": str(rep.id)}, headers=headers
    )
    assert listed.status_code == 200
    assert [c["id"] for c in listed.json()] == [body["id"]]

    effective = isolated_client.get(
        f"{BASE}/salary-configs/effective",
        params={"user_id": str(rep.id), "period_month": "2026-06"},
        headers=headers,
    )
    assert effective.status_code == 200, effective.text
    assert effective.json()["base_salary_fen"] == 480_000

    missing = isolated_client.get(
        f"{BASE}/salary-configs/effective",
        params={"user_id": str(rep.id), "period_month": "2025-12"},
        headers=headers,
    )
    assert missing.status_code == 200
    assert missing.json()["base_salary_fen"] is None

    # 工资草稿不传底薪 → 自动取配置
    generated = isolated_client.post(
        f"{BASE}/statements/generate",
        json={"user_id": str(rep.id), "period_month": "2026-06"},
        headers=headers,
    )
    assert generated.status_code == 201, generated.text
    assert generated.json()["base_salary_fen"] == 480_000

    toggled = isolated_client.post(
        f"{BASE}/salary-configs/{body['id']}/toggle-active", headers=headers
    )
    assert toggled.status_code == 200
    assert toggled.json()["is_active"] is False


# ======================= 绩效自动重算（巡访完成钩子） =======================

def test_refresh_month_creates_and_updates_draft(session: Session) -> None:
    w, mgr, mgr_actor, _f, rep, _r2, merchant = _setup(session)
    svc = PerformanceSnapshotService(session)
    _rule(svc, mgr_actor, PARAMS_V1)

    _contract(session, w, merchant, date(2026, 6, 5))
    snap = svc.refresh_month(rep.id, date(2026, 6, 5))
    assert snap is not None
    assert snap.status == "DRAFT"
    assert snap.period_month == MONTH
    assert snap.new_contract_fen == PARAMS_V1["new_contract_award_fen"]
    first_net = snap.net_performance_fen
    first_line_count = len(svc.repo.list_lines(snap.id))

    # 新活动达成 → DRAFT 就地重算（同一个快照，依据行整体替换）
    _activity(session, w, mgr, rep, date(2026, 6, 18))
    again = svc.refresh_month(rep.id, date(2026, 6, 18))
    assert again is not None and again.id == snap.id
    assert again.net_performance_fen == (
        first_net + PARAMS_V1["activity_award_fen"]
    )
    assert len(svc.repo.list_lines(snap.id)) == first_line_count + 1

    # 已确认（经理确认）后不再自动改写
    svc.confirm_snapshot(mgr_actor, snap.id)
    frozen_net = snap.net_performance_fen
    _contract(session, w, merchant, date(2026, 6, 25))
    unchanged = svc.refresh_month(rep.id, date(2026, 6, 25))
    assert unchanged is not None
    assert unchanged.status == "CONFIRMED"
    assert unchanged.net_performance_fen == frozen_net

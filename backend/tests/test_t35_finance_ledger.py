"""T35 财务流水与总账（金额只记流水，不做在线支付）。

覆盖：
- 收入/支出以带符号金额入账，0 元不落流水；
- 同一来源单据重复记账幂等（唯一索引 + 冲突回读）；
- 月/年汇总分桶与三张指标（收入、支出、净利润）；
- 公司物品采购：录入 → 待审批 → 审批通过自动进支出流水 → 重复审批冲突；
- 权限：未登录 401，无 finance 能力 403。
"""

from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tests.test_t09_contract import _ctx, _user, _world

from app.core.permissions import PermissionError_
from app.models import FinanceTransaction
from app.models.enums import RoleCode
from app.services.finance_ledger_service import FinanceLedgerService

BASE = "/api/finance"
PASSWORD = "t09-pass-not-for-prod"


def _finance_headers(client: TestClient, username: str) -> dict[str, str]:
    resp = client.post(
        "/api/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _count(session: Session, source_kind: str | None = None) -> int:
    stmt = select(func.count()).select_from(FinanceTransaction)
    if source_kind is not None:
        stmt = stmt.where(FinanceTransaction.source_kind == source_kind)
    return int(session.execute(stmt).scalar() or 0)


# ============================ 服务层：记账与汇总 ============================

def test_ledger_record_is_signed_and_idempotent(session: Session) -> None:
    w = _world(session)
    fin = _user(session, w["ds"], RoleCode.FINANCE, "fin35")
    actor = _ctx(fin, RoleCode.FINANCE.value, frozenset({w["region"].id}))
    svc = FinanceLedgerService(session)

    order_id = uuid4()
    income = svc.record_income(
        actor,
        biz_date=date(2026, 10, 8),
        amount_fen=12_000,
        related_type="SALES_CONFIRMATION",
        related_id=order_id,
        note="送达确认收款",
    )
    assert income is not None
    assert income.tx_type == "INCOME"
    assert income.amount_fen == 12_000
    assert income.source_kind == "SALES_REVENUE"

    # 同一来源重复记账 → 回读既有行，不产生第二笔
    again = svc.record_income(
        actor,
        biz_date=date(2026, 10, 8),
        amount_fen=12_000,
        related_type="SALES_CONFIRMATION",
        related_id=order_id,
    )
    assert again is not None and again.id == income.id
    assert _count(session, "SALES_REVENUE") == 1

    # 支出以负数入账
    expense = svc.record_expense(
        actor,
        biz_date=date(2026, 10, 9),
        amount_fen=4_500,
        source_kind="PROCUREMENT",
        related_type="PURCHASE_ORDER",
        related_id=uuid4(),
    )
    assert expense is not None
    assert expense.amount_fen == -4_500

    # 0 元不落流水（模型有 amount_fen <> 0 约束）
    assert (
        svc.record_expense(
            actor,
            biz_date=date(2026, 10, 9),
            amount_fen=0,
            source_kind="OPEX",
            related_type="EXPENSE",
            related_id=uuid4(),
        )
        is None
    )

    summary = svc.summary(actor, granularity="month", year=2026, month=10)
    assert summary.income_fen == 12_000
    assert summary.expense_fen == 4_500
    assert summary.net_fen == 7_500
    assert len(summary.buckets) == 12
    october = next(b for b in summary.buckets if b.period == "2026-10")
    assert october.income_fen == 12_000
    assert october.net_fen == 7_500

    year_summary = svc.summary(actor, granularity="year", year=2026)
    assert [b.period for b in year_summary.buckets] == [
        "2022", "2023", "2024", "2025", "2026",
    ]
    assert year_summary.expense_fen == 4_500


def test_ledger_requires_finance_ability(session: Session) -> None:
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep35")
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    svc = FinanceLedgerService(session)

    try:
        svc.summary(rep_actor, granularity="month", year=2026, month=10)
    except PermissionError_:
        pass
    else:  # pragma: no cover - 权限必须拦住
        raise AssertionError("业务员不应看到财务总账")


# ============================ 接口层：公司物品采购 ============================

def test_company_purchase_flow_writes_ledger(
    isolated_client: TestClient, isolated_session: Session
) -> None:
    ds: UUID = uuid4()
    fin = _user(isolated_session, ds, RoleCode.FINANCE, "fin35api")
    rep = _user(isolated_session, ds, RoleCode.SALES_REP, "rep35api")
    isolated_session.flush()

    headers = _finance_headers(isolated_client, fin.username)
    rep_headers = _finance_headers(isolated_client, rep.username)

    # 未登录 401；无 finance 能力 403
    assert isolated_client.get(f"{BASE}/ledger/summary").status_code == 401
    assert (
        isolated_client.get(f"{BASE}/ledger/summary", headers=rep_headers).status_code
        == 403
    )
    assert (
        isolated_client.post(
            f"{BASE}/company-purchases",
            json={
                "title": "打印纸",
                "category": "OTHER",
                "amount_fen": 1000,
                "incurred_on": "2026-10-10",
                "voucher_file_id": str(uuid4()),
            },
            headers=rep_headers,
        ).status_code
        == 403
    )

    # 凭证上传（白名单 PDF）
    voucher = isolated_client.post(
        f"{BASE}/vouchers",
        files={"file": ("voucher.pdf", b"%PDF-1.4\n%test\n", "application/pdf")},
        headers=headers,
    )
    assert voucher.status_code == 201, voucher.text
    file_id = voucher.json()["file_id"]

    created = isolated_client.post(
        f"{BASE}/company-purchases",
        json={
            "title": "门店物料打印纸（10 箱）",
            "category": "OTHER",
            "amount_fen": 23_000,
            "incurred_on": "2026-10-10",
            "voucher_file_id": file_id,
            "remark": "总部办公用品",
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "PENDING_APPROVAL"
    assert body["category"] == "OTHER"
    purchase_id = body["id"]

    listed = isolated_client.get(f"{BASE}/company-purchases", headers=headers)
    assert listed.status_code == 200
    assert purchase_id in {p["id"] for p in listed.json()}

    # 待审批不进流水
    before = isolated_client.get(
        f"{BASE}/ledger/transactions", headers=headers
    ).json()
    assert all(t["related_object_id"] != purchase_id for t in before)

    approved = isolated_client.post(
        f"{BASE}/company-purchases/{purchase_id}/approve",
        json={"reason": "凭证齐全"},
        headers=headers,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"

    txs = isolated_client.get(f"{BASE}/ledger/transactions", headers=headers).json()
    mine = [t for t in txs if t["related_object_id"] == purchase_id]
    assert len(mine) == 1
    assert mine[0]["tx_type"] == "EXPENSE"
    assert mine[0]["source_kind"] == "COMPANY_PURCHASE"
    assert mine[0]["amount_fen"] == -23_000
    assert mine[0]["source_label"] == "公司物品采购"

    summary = isolated_client.get(
        f"{BASE}/ledger/summary",
        params={"granularity": "month", "year": 2026, "month": 10},
        headers=headers,
    )
    assert summary.status_code == 200, summary.text
    assert summary.json()["expense_fen"] == 23_000
    assert summary.json()["net_fen"] == -23_000

    # 重复审批：状态冲突，不会写第二笔流水
    dup = isolated_client.post(
        f"{BASE}/company-purchases/{purchase_id}/approve",
        json={"reason": "重复点击"},
        headers=headers,
    )
    assert dup.status_code == 409, dup.text
    txs2 = isolated_client.get(f"{BASE}/ledger/transactions", headers=headers).json()
    assert len([t for t in txs2 if t["related_object_id"] == purchase_id]) == 1

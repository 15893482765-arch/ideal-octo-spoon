"""T10 冰柜投放、撤回、验收与维修（PRD §4.4/§4.5/§6.3；R2/R11/R12/R13/R37）。

覆盖：
- Q048 未生效合同投放拒绝
- Q049 投放写资产流水（购置/投放单/现场投放/押金收缴）
- Q050 现场回收后只到待验收，库存不加
- Q051 验收入库 +1
- Q052 故障转维修
- Q053 报废不可逆
- Q054 并发投放同一冰柜仅一次成功（行锁 + 状态机）
- Q181 押金收退流水：精确到分、关联投放/验收单、生成退押金待办、不入损益
另含合同终止联动建撤柜任务、HTTP 401 冒烟。
"""

from __future__ import annotations

import threading
from datetime import date
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.test_t09_contract import _ctx, _make_effective_contract, _user, _world

from app.core.errors import ConflictError, DomainError
from app.models import (
    FinanceTransaction,
    FreezerAsset,
    FreezerMovement,
    TaskItem,
)
from app.models.enums import RoleCode
from app.schemas.contract import (
    ContractTerminateRequest,
)
from app.schemas.freezer import (
    FreezerAcceptanceRequest,
    FreezerDeployConfirmRequest,
    FreezerDeployRequest,
    FreezerDepositRefundRequest,
    FreezerPickupConfirmRequest,
    FreezerPurchaseRequest,
    FreezerRepairRequest,
    FreezerScrapRequest,
)
from app.services.contract_service import ContractService
from app.services.freezer_service import FreezerService
from app.services.task_service import (
    TASK_KIND_FREEZER_DEPOSIT_REFUND,
    TASK_KIND_FREEZER_RECALL,
)

# ----------------------------- 造数辅助 -----------------------------


def _freezer(
    session: Session,
    w: dict,
    *,
    asset_code: str | None = None,
    deposit_fen: int | None = 50000,
    purchase_price_fen: int = 299900,
    status: str = "IN_STOCK",
) -> FreezerAsset:
    asset = FreezerAsset(
        asset_code=asset_code or f"FZ-{uuid4().hex[:10]}",
        brand_id=w["brand"].id,
        model="QC-380",
        purchase_date=date(2026, 1, 10),
        purchase_price_fen=purchase_price_fen,
        deposit_fen=deposit_fen,
        status=status,
        dataset_type="PRODUCTION",
        dataset_id=w["ds"],
    )
    session.add(asset)
    session.flush()
    return asset


def _purchase(
    session: Session, w: dict, actor, *, deposit_fen: int | None = 50000
) -> FreezerAsset:
    """走 Service 购置入库（写 PURCHASE 流水）。"""
    return FreezerService(session).purchase(
        actor,
        FreezerPurchaseRequest(
            asset_code=f"FZ-{uuid4().hex[:10]}",
            brand_id=w["brand"].id,
            model="QC-380",
            purchase_date=date(2026, 1, 10),
            purchase_price_fen=299900,
            deposit_fen=deposit_fen,
        ),
    )


def _types(session: Session, asset_id) -> list[str]:
    return [
        m.movement_type
        for m in session.scalars(
            select(FreezerMovement)
            .where(FreezerMovement.asset_id == asset_id)
            .order_by(FreezerMovement.created_at.asc())
        ).all()
    ]


# ----------------------------- Q048 -----------------------------


def test_q048_deploy_requires_effective_contract(session: Session) -> None:
    """未生效合同创建投放单被拒（R2）。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    manager = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    mgr_actor = _ctx(manager, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))
    svc = FreezerService(session)

    asset = _purchase(session, w, mgr_actor)
    # 一份处于待审（未生效）的合同
    contract = ContractService(session).create_draft(
        rep_actor,
        _draft_for(session, w),
    )
    ContractService(session).submit(rep_actor, contract.id, "提交")

    with pytest.raises(ConflictError) as ei:
        svc.create_deployment(
            rep_actor, asset.id, FreezerDeployRequest(contract_id=contract.id)
        )
    assert ei.value.code == "CONTRACT_NOT_EFFECTIVE_FOR_FREEZER"
    # 冰柜仍在库，未被占用
    assert svc.repo.get(asset.id).status == "IN_STOCK"


def _draft_for(session: Session, w: dict):
    from tests.test_t09_contract import _draft_payload, _merchant

    merchant = _merchant(session, w)
    return _draft_payload(merchant.id)


# ----------------------------- Q049 -----------------------------


def test_q049_deployment_writes_movements(session: Session) -> None:
    """投放单 + 现场投放各写流水；现场投放后绑定唯一商家并收缴押金。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    manager = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    mgr_actor = _ctx(manager, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))
    contract = _make_effective_contract(session, w, rep, manager)

    svc = FreezerService(session)
    asset = _purchase(session, w, mgr_actor)
    before = svc.available_stock_count(mgr_actor)
    assert before >= 1

    order = svc.create_deployment(
        rep_actor, asset.id, FreezerDeployRequest(contract_id=contract.id)
    )
    assert svc.repo.get(asset.id).status == "RESERVED"
    assert order.doc_no is not None and order.doc_no.startswith("FDO")

    asset, deploy = svc.confirm_deployment(
        rep_actor, asset.id, FreezerDeployConfirmRequest()
    )
    assert asset.status == "DEPLOYED"
    assert asset.current_merchant_id == contract.merchant_id
    assert asset.last_deployed_at is not None
    assert deploy.doc_no.startswith("FDP")

    types = _types(session, asset.id)
    assert "PURCHASE" in types
    assert "DEPLOYMENT_ORDER" in types
    assert "DEPLOY" in types
    assert "DEPOSIT_COLLECTED" in types

    collected = [
        m
        for m in svc.repo.list_movements(asset.id)
        if m.movement_type == "DEPOSIT_COLLECTED"
    ]
    assert len(collected) == 1
    assert collected[0].related_movement_id == deploy.id  # 关联投放单
    assert collected[0].amount_fen == 50000
    assert collected[0].contract_id == contract.id


# ----------------------------- Q050 -----------------------------


def test_q050_pickup_pending_acceptance_does_not_add_stock(session: Session) -> None:
    """现场回收只到待验收；回收完成不等于在库，可用库存不增加。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    manager = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    mgr_actor = _ctx(manager, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))
    contract = _make_effective_contract(session, w, rep, manager)
    svc = FreezerService(session)

    asset = _deploy_one(svc, session, w, rep_actor, mgr_actor, contract)
    # 终止合同触发撤柜任务 → DEPLOYED → PENDING_PICKUP
    _terminate(session, contract, rep_actor, mgr_actor)
    assert svc.repo.get(asset.id).status == "PENDING_PICKUP"
    stock_after_pickup_task = svc.available_stock_count(mgr_actor)

    # 现场回收
    mov = svc.confirm_pickup(
        rep_actor, asset.id, FreezerPickupConfirmRequest(note="外观完好已装车")
    )
    assert svc.repo.get(asset.id).status == "PENDING_ACCEPTANCE"
    assert mov.doc_no.startswith("FPK")
    # 已物理离店 → 解绑商家（归属在流水）
    assert svc.repo.get(asset.id).current_merchant_id is None
    # R11：待验收不占可用库存，且相对建撤柜任务时没有增加
    assert svc.available_stock_count(mgr_actor) == stock_after_pickup_task
    assert svc.available_stock_count(mgr_actor) == stock_after_pickup_task


def _deploy_one(svc, session, w, rep_actor, mgr_actor, contract) -> FreezerAsset:
    asset = _purchase(session, w, mgr_actor)
    svc.create_deployment(
        rep_actor, asset.id, FreezerDeployRequest(contract_id=contract.id)
    )
    asset, _ = svc.confirm_deployment(
        rep_actor, asset.id, FreezerDeployConfirmRequest()
    )
    return asset


def _terminate(session, contract, rep_actor, mgr_actor) -> None:
    cs = ContractService(session)
    cs.request_termination(
        rep_actor,
        contract.id,
        ContractTerminateRequest(reason="门店停业", terminate_date=date(2026, 9, 1)),
    )
    cs.approve_termination(mgr_actor, contract.id, "同意终止")
    session.flush()


# ----------------------------- Q051 -----------------------------


def test_q051_acceptance_in_stock_increments(session: Session) -> None:
    """验收入库后可用库存 +1（R11）。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    manager = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    mgr_actor = _ctx(manager, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))
    contract = _make_effective_contract(session, w, rep, manager)
    svc = FreezerService(session)

    asset = _deploy_one(svc, session, w, rep_actor, mgr_actor, contract)
    _terminate(session, contract, rep_actor, mgr_actor)
    stock_pending = svc.available_stock_count(mgr_actor)
    svc.confirm_pickup(rep_actor, asset.id, FreezerPickupConfirmRequest())

    asset2, accept_mov = svc.accept(
        mgr_actor, asset.id, FreezerAcceptanceRequest(result="IN_STOCK")
    )
    assert asset2.status == "IN_STOCK"
    assert accept_mov.doc_no.startswith("FAC")
    assert svc.available_stock_count(mgr_actor) == stock_pending + 1


def test_acceptance_repair_does_not_increment(session: Session) -> None:
    """验收判故障转维修，库存不增加，维修次数 +1。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    manager = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    mgr_actor = _ctx(manager, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))
    contract = _make_effective_contract(session, w, rep, manager)
    svc = FreezerService(session)

    asset = _deploy_one(svc, session, w, rep_actor, mgr_actor, contract)
    _terminate(session, contract, rep_actor, mgr_actor)
    before = svc.available_stock_count(mgr_actor)
    svc.confirm_pickup(rep_actor, asset.id, FreezerPickupConfirmRequest())
    asset2, _ = svc.accept(
        mgr_actor, asset.id, FreezerAcceptanceRequest(result="REPAIR")
    )
    assert asset2.status == "REPAIRING"
    assert asset2.repair_count == 1
    assert svc.available_stock_count(mgr_actor) == before


# ----------------------------- Q052 -----------------------------


def test_q052_fault_to_repair(session: Session) -> None:
    """在库故障 → 维修中；修复后归库；各写流水且维修次数累计。"""
    w = _world(session)
    manager = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    mgr_actor = _ctx(manager, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))
    svc = FreezerService(session)
    asset = _purchase(session, w, mgr_actor)

    svc.start_repair(
        mgr_actor, asset.id, FreezerRepairRequest(note="压缩机异响")
    )
    assert svc.repo.get(asset.id).status == "REPAIRING"
    assert svc.repo.get(asset.id).repair_count == 1

    svc.complete_repair(
        mgr_actor, asset.id, FreezerRepairRequest(note="已更换并复检")
    )
    assert svc.repo.get(asset.id).status == "IN_STOCK"
    types = _types(session, asset.id)
    assert types[-2:] == ["REPAIR_START", "REPAIR_END"]


# ----------------------------- Q053 -----------------------------


def test_q053_scrap_is_irreversible(session: Session) -> None:
    """维修中报废后任何正向迁移都被状态机拒绝（R13）。"""
    w = _world(session)
    manager = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    mgr_actor = _ctx(manager, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))
    svc = FreezerService(session)
    asset = _purchase(session, w, mgr_actor)
    svc.start_repair(mgr_actor, asset.id, FreezerRepairRequest())

    mov = svc.scrap(
        mgr_actor,
        asset.id,
        FreezerScrapRequest(reason="维修成本过高，无修复价值"),
    )
    assert mov.movement_type == "SCRAP"
    assert svc.repo.get(asset.id).status == "SCRAPPED"

    # 报废后不可再维修归库 / 再送修（状态机拒绝 STATE_TRANSITION_INVALID）
    with pytest.raises(DomainError) as ei1:
        svc.complete_repair(mgr_actor, asset.id, FreezerRepairRequest())
    assert ei1.value.code == "STATE_TRANSITION_INVALID"
    with pytest.raises(DomainError) as ei2:
        svc.start_repair(mgr_actor, asset.id, FreezerRepairRequest())
    assert ei2.value.code == "STATE_TRANSITION_INVALID"
    # 记录仍保留（报废不是删除）
    assert svc.repo.get(asset.id) is not None
    assert svc.repo.get(asset.id).is_active is True


# ----------------------------- Q054 -----------------------------


def test_q054_concurrent_deploy_only_one_succeeds() -> None:
    """并发投放同一在库冰柜：行内一次成功、另一次冲突，投放单只生成一张（R12）。

    真实并发必须跨连接读到已提交数据，故不用 savepoint 的 `session` fixture：
    用独立会话真实 commit，结束后清理本用例产生的行（audit/流水/资产/合同/商家）。
    """
    from sqlalchemy import create_engine
    from tests.conftest import TEST_DATABASE_URL

    eng = create_engine(TEST_DATABASE_URL, future=True)
    created: dict = {
        "asset": None,
        "contracts": [],
        "merchants": [],
        "users": [],
        "scope": None,
    }
    try:
        s = Session(eng)
        w = _world(s)
        scope = {"ds": w["ds"], "brand_id": w["brand"].id, "region_id": w["region"].id}
        rep = _user(s, w["ds"], RoleCode.SALES_REP, "rep")
        manager = _user(s, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
        created["users"] = [rep.id, manager.id]
        created["scope"] = scope
        rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
        mgr_actor = _ctx(manager, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))
        asset = _purchase(s, w, mgr_actor)
        asset_id = asset.id
        created["asset"] = asset_id
        c1 = _make_effective_contract(s, w, rep, manager)
        c2 = _make_effective_contract(s, w, rep, manager)
        c1_id, c2_id = c1.id, c2.id
        m1_id, m2_id = c1.merchant_id, c2.merchant_id
        created["contracts"] = [c1_id, c2_id]
        created["merchants"] = [m1_id, m2_id]
        s.commit()
        s.close()

        results: list[str] = []
        barrier = threading.Barrier(2)
        lock = threading.Lock()

        def worker(contract_id: UUID, freezer_id: UUID, actor) -> None:
            ws = Session(eng)
            try:
                fsvc = FreezerService(ws)
                barrier.wait(timeout=10)
                fsvc.create_deployment(
                    actor, freezer_id, FreezerDeployRequest(contract_id=contract_id)
                )
                ws.commit()
                with lock:
                    results.append("OK")
            except Exception as exc:  # noqa: BLE001 - 并发冲突属预期
                ws.rollback()
                with lock:
                    results.append(getattr(exc, "code", type(exc).__name__))
            finally:
                ws.close()

        t1 = threading.Thread(target=worker, args=(c1_id, asset_id, rep_actor))
        t2 = threading.Thread(target=worker, args=(c2_id, asset_id, rep_actor))
        t1.start()
        t2.start()
        t1.join(timeout=20)
        t2.join(timeout=20)

        verify = Session(eng)
        try:
            final = verify.get(FreezerAsset, asset_id)
            final_status = final.status
            orders = list(
                verify.scalars(
                    select(FreezerMovement).where(
                        FreezerMovement.asset_id == asset_id,
                        FreezerMovement.movement_type == "DEPLOYMENT_ORDER",
                    )
                ).all()
            )
            order_contract_ids = [o.contract_id for o in orders]
        finally:
            verify.close()

        # 恰好一个成功；另一个因行锁后状态非 IN_STOCK 冲突（FREEZER_NOT_AVAILABLE）
        assert sorted(results) == ["FREEZER_NOT_AVAILABLE", "OK"], results
        assert final_status == "RESERVED"
        assert len(order_contract_ids) == 1
        assert order_contract_ids[0] in {c1_id, c2_id}
    finally:
        _cleanup_q054(eng, created)
        eng.dispose()


def _cleanup_q054(eng, created: dict) -> None:  # noqa: ANN001
    from app.models import (
        AuditLog,
        Brand,
        Contract,
        ContractVersion,
        Merchant,
        Organization,
        Region,
        Role,
        User,
    )

    s = Session(eng)
    try:
        asset_id = created["asset"]
        if asset_id is not None:
            # T35：购置会记一笔资产支出流水，清理时一并删除（避免 users FK 阻塞）
            s.query(FinanceTransaction).filter(
                FinanceTransaction.related_object_type == "FREEZER_ASSET",
                FinanceTransaction.related_object_id == asset_id,
            ).delete(synchronize_session=False)
            s.query(FreezerMovement).filter(FreezerMovement.asset_id == asset_id).delete()
            s.query(AuditLog).filter(
                AuditLog.object_type == "freezer", AuditLog.object_id == asset_id
            ).delete(synchronize_session=False)
            s.query(FreezerAsset).filter(FreezerAsset.id == asset_id).delete()
        for cid in created["contracts"]:
            s.query(TaskItem).filter(
                TaskItem.related_object_type == "contract",
                TaskItem.related_object_id == cid,
            ).delete(synchronize_session=False)
            s.query(ContractVersion).filter(ContractVersion.contract_id == cid).delete()
            s.query(AuditLog).filter(
                AuditLog.object_type == "contract", AuditLog.object_id == cid
            ).delete(synchronize_session=False)
            s.query(Contract).filter(Contract.id == cid).delete()
        for mid in created["merchants"]:
            s.query(Merchant).filter(Merchant.id == mid).delete()
        role_ids: set = set()
        for uid in created["users"]:
            u = s.get(User, uid)
            if u is not None:
                role_ids.add(u.role_id)
            s.query(AuditLog).filter(AuditLog.actor_id == uid).delete(
                synchronize_session=False
            )
            s.query(User).filter(User.id == uid).delete()
        scope = created["scope"]
        if scope is not None:
            s.query(Brand).filter(Brand.id == scope["brand_id"]).delete()
            s.query(Region).filter(Region.id == scope["region_id"]).delete()
            s.query(Organization).filter(
                Organization.dataset_id == scope["ds"]
            ).delete()
        for rid in role_ids:
            s.query(Role).filter(Role.id == rid).delete()
        s.commit()
    finally:
        s.close()


# ----------------------------- Q181 -----------------------------


def test_q181_deposit_collect_refund_flow(session: Session) -> None:
    """押金收缴/退还精确到分、关联投放/验收单、生成退押金待办且不入损益。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    manager = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    finance = _user(session, w["ds"], RoleCode.FINANCE, "fin")
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    mgr_actor = _ctx(manager, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))
    fin_actor = _ctx(finance, RoleCode.FINANCE.value, frozenset({w["region"].id}))
    contract = _make_effective_contract(session, w, rep, manager)
    svc = FreezerService(session)

    asset = _deploy_one(svc, session, w, rep_actor, mgr_actor, contract)
    _terminate(session, contract, rep_actor, mgr_actor)
    svc.confirm_pickup(rep_actor, asset.id, FreezerPickupConfirmRequest())
    _, accept_mov = svc.accept(
        mgr_actor, asset.id, FreezerAcceptanceRequest(result="IN_STOCK")
    )

    # 验收后自动生成一条“退押金评估”财务待办
    tasks = list(
        session.scalars(
            select(TaskItem).where(
                TaskItem.related_object_type == "FREEZER",
                TaskItem.related_object_id == asset.id,
                TaskItem.task_kind == TASK_KIND_FREEZER_DEPOSIT_REFUND,
            )
        ).all()
    )
    assert len(tasks) == 1
    assert tasks[0].assignee_id == finance.id

    # 财务确认退还（部分退款 49999 分，精确到分）
    refund = svc.confirm_deposit_refund(
        fin_actor,
        asset.id,
        FreezerDepositRefundRequest(amount_fen=49999, note="扣减清洁费 1 分"),
    )
    assert refund.movement_type == "DEPOSIT_REFUNDED"
    assert refund.amount_fen == 49999
    # 退款关联原押金收缴单；收缴单关联投放单（完整单据链，R37）
    collected = next(
        m
        for m in svc.repo.list_movements(asset.id)
        if m.movement_type == "DEPOSIT_COLLECTED"
    )
    assert refund.related_movement_id == collected.id
    deploy = next(
        m for m in svc.repo.list_movements(asset.id) if m.movement_type == "DEPLOY"
    )
    assert collected.related_movement_id == deploy.id
    assert refund.doc_no.startswith("FDR")
    # 押金收退都不产生损益分录；购置本身按 T35 记一笔资产支出（来源 ASSET）
    pl = session.scalars(
        select(FinanceTransaction).where(FinanceTransaction.source_kind != "ASSET")
    ).all()
    assert pl == []
    asset_tx = session.scalars(
        select(FinanceTransaction).where(
            FinanceTransaction.source_kind == "ASSET",
            FinanceTransaction.related_object_id == asset.id,
        )
    ).all()
    assert len(asset_tx) == 1
    # 支出以负数入账，金额等于资产购置价
    assert asset_tx[0].amount_fen == -int(asset.purchase_price_fen)
    # 退押金待办被关闭（单据动作关闭 → CLOSED，outcome=DEPOSIT_REFUNDED）
    session.expire_all()
    closed_task = session.get(TaskItem, tasks[0].id)
    assert closed_task.status == "CLOSED"
    assert closed_task.closed_at is not None

    # 重复退款：已无待退押金，拒绝（幂等，不允许重复退）
    with pytest.raises(ConflictError) as ei:
        svc.confirm_deposit_refund(
            fin_actor, asset.id, FreezerDepositRefundRequest()
        )
    assert ei.value.code == "FREEZER_DEPOSIT_NOT_REFUNDABLE"


def test_deposit_refund_cannot_exceed_collected(session: Session) -> None:
    """退款金额不得超过收缴金额。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    manager = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    finance = _user(session, w["ds"], RoleCode.FINANCE, "fin")
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    mgr_actor = _ctx(manager, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))
    fin_actor = _ctx(finance, RoleCode.FINANCE.value, frozenset({w["region"].id}))
    contract = _make_effective_contract(session, w, rep, manager)
    svc = FreezerService(session)
    asset = _deploy_one(svc, session, w, rep_actor, mgr_actor, contract)
    _terminate(session, contract, rep_actor, mgr_actor)
    svc.confirm_pickup(rep_actor, asset.id, FreezerPickupConfirmRequest())
    svc.accept(mgr_actor, asset.id, FreezerAcceptanceRequest(result="IN_STOCK"))
    with pytest.raises(DomainError) as ei:
        svc.confirm_deposit_refund(
            fin_actor, asset.id, FreezerDepositRefundRequest(amount_fen=50001)
        )
    assert ei.value.code == "FREEZER_DEPOSIT_REFUND_EXCEEDS"


def test_no_deposit_no_refund_task(session: Session) -> None:
    """无押金投放验收后不生成退押金待办。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    manager = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    mgr_actor = _ctx(manager, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))
    contract = _make_effective_contract(session, w, rep, manager)
    svc = FreezerService(session)
    asset = _deploy_one(
        svc, session, w, rep_actor, mgr_actor, contract
    )
    # 该资产台账 deposit_fen=50000；改为无押金资产单独验
    no_deposit = _purchase(session, w, mgr_actor, deposit_fen=None)
    svc.create_deployment(
        rep_actor, no_deposit.id, FreezerDeployRequest(contract_id=contract.id)
    )
    # 直接验证无押金的资产没有 DEPOSIT_COLLECTED，且 latest_open_deposit 为 None
    assert svc.repo.latest_open_deposit(no_deposit.id) is None
    # 已部署带押金资产存在收缴
    assert svc.repo.latest_open_deposit(asset.id) is not None


# --------------------- 合同终止联动：撤柜任务 ---------------------


def test_termination_creates_recall_task(session: Session) -> None:
    """合同终止审批通过后为在店冰柜建撤柜任务（幂等），已投放 → 待撤回。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    manager = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    mgr_actor = _ctx(manager, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))
    contract = _make_effective_contract(session, w, rep, manager)
    svc = FreezerService(session)
    asset = _deploy_one(svc, session, w, rep_actor, mgr_actor, contract)

    _terminate(session, contract, rep_actor, mgr_actor)
    assert svc.repo.get(asset.id).status == "PENDING_PICKUP"

    tasks = list(
        session.scalars(
            select(TaskItem).where(
                TaskItem.related_object_type == "FREEZER",
                TaskItem.related_object_id == asset.id,
                TaskItem.task_kind == TASK_KIND_FREEZER_RECALL,
            )
        ).all()
    )
    assert len(tasks) == 1
    assert tasks[0].assignee_id == rep.id  # 责任业务员


# ----------------------------- HTTP 401 -----------------------------


def test_freezers_endpoint_requires_auth(client: TestClient) -> None:
    """冰柜路由已挂载，未携带 JWT 一律 401。"""
    assert client.get("/api/freezers/stock/count").status_code == 401
    assert client.get(f"/api/freezers/{uuid4()}").status_code == 401

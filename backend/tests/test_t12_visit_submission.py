"""T12 巡访提交、销量与补货联动（PRD §4.6 提交表单 / §4.9 补货状态链）。

覆盖：
- Q061 陈列 49% < 合同阈值 50% → 不达标（display_met=False）+ 整改待办
- Q062 陈列 50% >= 合同阈值 50% → 达标（display_met=True），无整改待办
- Q063 多 SKU 销量行精度（NUMERIC(12,2) 原样保存，无 float 误差）
- Q064 提交联动原子回滚（补货草稿/销量/待办/记录同一事务，失败全回滚）
- Q065 重放幂等不重复（依赖 T04 idempotency_key：同 key 返回同一条，不产生重复联动）
- Q066 未确认销量不入指标（不产生 SalesConfirmation；补货停留 DRAFT/committed=0）

跨卡：有效性仍由 T11 VisitValidityService 裁决（近点+有照片=VALID），不复制规则。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from geoalchemy2 import WKTElement
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tests.test_t09_contract import _ctx, _merchant, _user, _world
from tests.test_t11_visit import (
    MERCHANT_LNG_LAT,
    NEAR_LNG_LAT,
    _assign_primary,
    _photo_file,
    _task,
)

from app.models import (
    Contract,
    ReplenishmentOrder,
    SalesConfirmation,
    Sku,
    TaskItem,
    UserRegionBrandAuth,
    VisitRecord,
    VisitSalesLine,
)
from app.models.enums import ReplenishmentStatus, RoleCode
from app.schemas.visit import (
    VisitPhotoInput,
    VisitReplenishmentLineInput,
    VisitSalesLineInput,
    VisitSubmit,
)
from app.services.visit_service import VisitService

# ----------------------------- 造数辅助 -----------------------------


def _merchant_at(session: Session, w: dict, lng: float, lat: float):
    m = _merchant(session, w, name="沁爽便利店-T12")
    m.location = WKTElement(f"POINT({lng} {lat})", srid=4326)
    session.flush()
    return m


def _sku(session: Session, w: dict, code: str, name: str) -> Sku:
    s = Sku(
        code=f"SKU-{code}-{uuid4().hex[:6]}",
        name=name,
        brand_id=w["brand"].id,
        capacity_ml=500,
        suggested_price_fen=300,
        case_spec=Decimal("24"),
        dataset_type="PRODUCTION",
        dataset_id=w["ds"],
    )
    session.add(s)
    session.flush()
    return s


def _effective_contract(session: Session, merchant, *, threshold: int) -> Contract:
    """直接落一份覆盖当日（2026 年）的 EFFECTIVE 合同，固化陈列阈值。"""
    c = Contract(
        merchant_id=merchant.id,
        contract_no=f"C-{uuid4().hex[:10]}",
        title="2026 陈列协议",
        party_name="天天便利店有限公司",
        status="EFFECTIVE",
        signed_at=date(2026, 1, 1),
        effective_from=date(2026, 1, 1),
        expire_at=date(2026, 12, 31),
        commission_rate=Decimal("3.5"),
        display_subsidy_amount=120000,
        display_threshold_pct=threshold,
        current_version=1,
        dataset_type="PRODUCTION",
        dataset_id=merchant.dataset_id,
    )
    session.add(c)
    session.flush()
    return c


def _setup(session: Session, *, threshold: int = 50):
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep12")
    mgr = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr12")
    merchant = _merchant_at(session, w, *MERCHANT_LNG_LAT)
    _assign_primary(session, merchant, rep)
    # 经理在该片区×品牌的服务端行级授权（补货复核待办据此指派）
    session.add(
        UserRegionBrandAuth(
            user_id=mgr.id,
            region_id=w["region"].id,
            brand_id=w["brand"].id,
        )
    )
    session.flush()
    contract = _effective_contract(session, merchant, threshold=threshold)
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    return w, rep, mgr, merchant, contract, rep_actor


def _valid_submit(session: Session, w: dict, rep, **overrides) -> VisitSubmit:
    photo = _photo_file(session, w, rep)
    data = dict(
        lng=NEAR_LNG_LAT[0],
        lat=NEAR_LNG_LAT[1],
        accuracy_meters=12,
        photos=[VisitPhotoInput(file_id=photo.id, photo_kind="STOREFRONT")],
        idempotency_key=f"t12-{uuid4().hex[:10]}",
    )
    data.update(overrides)
    return VisitSubmit(**data)


def _rectification_tasks(session: Session, record_id) -> list[TaskItem]:
    return list(
        session.scalars(
            select(TaskItem).where(
                TaskItem.related_object_type == "VISIT_RECORD",
                TaskItem.related_object_id == record_id,
                TaskItem.task_kind == "PROBLEM_RECTIFICATION",
            )
        ).all()
    )


# ----------------------------- Q061 -----------------------------


def test_q061_display_49pct_below_threshold_not_met(session: Session) -> None:
    """陈列 49% < 合同阈值 50% → display_met=False，快照阈值 50，并生成整改待办。"""
    w, rep, _mgr, merchant, contract, rep_actor = _setup(session, threshold=50)
    task = _task(session, rep_actor, merchant)
    payload = _valid_submit(session, w, rep, display_pct=49,
                            device_issue="冰柜不制冷")
    record = VisitService(session).submit(rep_actor, task.id, payload)
    session.flush()

    assert contract.display_threshold_pct == 50
    assert record.display_threshold_pct == 50   # 阈值快照固化
    assert record.display_met is False          # 49 < 50 不达标
    assert record.display_pct == 49
    # 设备异常/陈列不达标 → 整改待办指派给责任业务员
    rect = _rectification_tasks(session, record.id)
    assert len(rect) == 1
    assert rect[0].assignee_id == rep.id
    assert "陈列" in rect[0].title + (rect[0].description or "")
    # 近点 + 有照片 → 有效性仍由 T11 裁决为有效
    assert record.validity == "VALID"


# ----------------------------- Q062 -----------------------------


def test_q062_display_50pct_meets_threshold(session: Session) -> None:
    """陈列 50% >= 合同阈值 50% → display_met=True（边界包含），不生成整改待办。"""
    w, rep, _mgr, merchant, _contract, rep_actor = _setup(session, threshold=50)
    task = _task(session, rep_actor, merchant)
    payload = _valid_submit(session, w, rep, display_pct=50)
    record = VisitService(session).submit(rep_actor, task.id, payload)
    session.flush()

    assert record.display_threshold_pct == 50
    assert record.display_met is True           # 50 >= 50 达标
    assert _rectification_tasks(session, record.id) == []


def test_threshold_snapshot_not_affected_by_later_contract_change(
    session: Session,
) -> None:
    """R3：提交后合同阈值改为 60，不改变已固化的历史判定（display_threshold_pct=50）。"""
    w, rep, _mgr, merchant, contract, rep_actor = _setup(session, threshold=50)
    task = _task(session, rep_actor, merchant)
    record = VisitService(session).submit(
        rep_actor, task.id, _valid_submit(session, w, rep, display_pct=55)
    )
    session.flush()
    assert record.display_met is True
    # 合同事后变更阈值
    contract.display_threshold_pct = 60
    session.flush()
    session.refresh(record)
    assert record.display_threshold_pct == 50
    assert record.display_met is True


# ----------------------------- Q063 -----------------------------


def test_q063_multi_sku_sales_precision(session: Session) -> None:
    """多 SKU 上报销量：NUMERIC(12,2) 原样保存，Decimal 无 float 误差。"""
    w, rep, _mgr, merchant, _c, rep_actor = _setup(session)
    task = _task(session, rep_actor, merchant)
    sku1 = _sku(session, w, "A", "沁爽柠檬 500ml")
    sku2 = _sku(session, w, "B", "沁爽白桃 500ml")
    sku3 = _sku(session, w, "C", "沁爽气泡水 500ml")

    payload = _valid_submit(
        session, w, rep,
        sales_lines=[
            VisitSalesLineInput(sku_id=sku1.id, reported_boxes=Decimal("12.30"),
                                out_of_stock=False),
            VisitSalesLineInput(sku_id=sku2.id, reported_boxes=Decimal("0.10"),
                                feedback="滞销"),
            VisitSalesLineInput(sku_id=sku3.id, reported_boxes=Decimal("0"),
                                out_of_stock=True),
        ],
    )
    record = VisitService(session).submit(rep_actor, task.id, payload)
    session.flush()

    lines = {s.sku_id: s for s in VisitService(session).repo.list_sales_lines(record.id)}
    assert set(lines) == {sku1.id, sku2.id, sku3.id}
    # 精度：逐字节等价于输入 Decimal，不经过 float
    assert lines[sku1.id].reported_boxes == Decimal("12.30")
    assert lines[sku2.id].reported_boxes == Decimal("0.10")
    assert lines[sku3.id].reported_boxes == Decimal("0.00")
    assert lines[sku3.id].out_of_stock is True
    assert lines[sku2.id].feedback == "滞销"
    # 确保没有 float 漂移（0.1+0.2 问题不出现）
    assert lines[sku1.id].reported_boxes + lines[sku2.id].reported_boxes == Decimal("12.40")


# ----------------------------- Q064 -----------------------------


def test_q064_submission_linkage_atomic_rollback(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """联动失败时整体回滚：记录/销量/补货草稿/待办一个都不留。"""
    w, rep, mgr, merchant, _c, rep_actor = _setup(session)
    task = _task(session, rep_actor, merchant)
    sku = _sku(session, w, "ROLL", "回滚用 SKU")
    payload = _valid_submit(
        session, w, rep,
        display_pct=20,  # 触发整改待办
        sales_lines=[VisitSalesLineInput(sku_id=sku.id, reported_boxes=Decimal("3"))],
        replenishment_lines=[
            VisitReplenishmentLineInput(
                sku_id=sku.id, requested_boxes=Decimal("5"),
                expected_delivery_date=date(2026, 12, 20),
            )
        ],
    )

    # 在待办联动阶段强制失败（此时记录/销量/补货草稿均已 flush）
    def _boom(*_a, **_kw):
        raise RuntimeError("simulated linkage failure")

    monkeypatch.setattr(
        "app.services.visit_service.TaskService.assign", _boom
    )

    # 用 SAVEPOINT 精确模拟 Router 的“请求事务”：异常回滚该事务，外层任务保留
    with pytest.raises(RuntimeError, match="simulated linkage failure"):
        with session.begin_nested():
            VisitService(session).submit(rep_actor, task.id, payload)

    assert session.scalar(select(func.count()).select_from(VisitRecord)) == 0
    assert session.scalar(select(func.count()).select_from(VisitSalesLine)) == 0
    assert session.scalar(select(func.count()).select_from(ReplenishmentOrder)) == 0
    assert session.scalar(select(func.count()).select_from(TaskItem)) == 0
    # 任务未被关闭（仍待提交）
    session.expire_all()
    assert session.get(type(task), task.id).status == "PENDING"


def test_replenishment_draft_created_and_review_task_to_manager(
    session: Session,
) -> None:
    """补货行 → DRAFT 补货单（committed=0、回链巡访）+ 经理复核待办。"""
    w, rep, mgr, merchant, _c, rep_actor = _setup(session)
    task = _task(session, rep_actor, merchant)
    sku = _sku(session, w, "REPL", "补货用 SKU")
    payload = _valid_submit(
        session, w, rep,
        conclusion="NEEDS_REPLENISHMENT",
        replenishment_lines=[
            VisitReplenishmentLineInput(
                sku_id=sku.id, requested_boxes=Decimal("8.5"),
                expected_delivery_date=date(2026, 12, 18),
            )
        ],
    )
    record = VisitService(session).submit(rep_actor, task.id, payload)
    session.flush()

    orders = VisitService(session).repo.list_replenishments_for_record(record.id)
    assert len(orders) == 1
    order = orders[0]
    assert order.status == ReplenishmentStatus.DRAFT.value
    assert order.committed_boxes == Decimal("0")          # 确认前承诺量为 0
    assert order.requested_boxes == Decimal("8.5")
    assert order.source_visit_record_id == record.id
    assert order.promised_delivery_date == date(2026, 12, 18)
    # 复核待办指给片区经理，而非上报业务员
    review = list(
        session.scalars(
            select(TaskItem).where(
                TaskItem.related_object_type == "VISIT_RECORD",
                TaskItem.related_object_id == record.id,
                TaskItem.task_kind == "REPLENISHMENT_REVIEW",
            )
        ).all()
    )
    assert len(review) == 1
    assert review[0].assignee_id == mgr.id
    assert review[0].assignee_id != rep.id


# ----------------------------- Q065 -----------------------------


def test_q065_replay_same_idempotency_key_does_not_duplicate(session: Session) -> None:
    """移动端重放（同一 idempotency_key）：返回同一条记录，不重复创建联动。"""
    w, rep, _mgr, merchant, _c, rep_actor = _setup(session)
    task = _task(session, rep_actor, merchant)
    sku = _sku(session, w, "IDEM", "幂等用 SKU")
    key = f"replay-{uuid4().hex[:10]}"

    def build():
        photo = _photo_file(session, w, rep)
        return VisitSubmit(
            lng=NEAR_LNG_LAT[0], lat=NEAR_LNG_LAT[1],
            photos=[VisitPhotoInput(file_id=photo.id)],
            sales_lines=[VisitSalesLineInput(sku_id=sku.id, reported_boxes=Decimal("2"))],
            replenishment_lines=[
                VisitReplenishmentLineInput(
                    sku_id=sku.id, requested_boxes=Decimal("4"))
            ],
            idempotency_key=key,
        )

    svc = VisitService(session)
    first = svc.submit(rep_actor, task.id, build())
    session.flush()
    first_id = first.id

    # 重放：同一幂等键（即便新建 service，也命中同一数据）
    replay = VisitService(session).submit(rep_actor, task.id, build())
    session.flush()
    assert replay.id == first_id

    assert session.scalar(
        select(func.count()).select_from(VisitRecord).where(VisitRecord.id == first_id)
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(VisitSalesLine)
        .where(VisitSalesLine.visit_record_id == first_id)
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(ReplenishmentOrder)
        .where(ReplenishmentOrder.source_visit_record_id == first_id)
    ) == 1
    review_count = session.scalar(
        select(func.count()).select_from(TaskItem).where(
            TaskItem.related_object_type == "VISIT_RECORD",
            TaskItem.related_object_id == first_id,
            TaskItem.task_kind == "REPLENISHMENT_REVIEW",
        )
    )
    assert review_count == 1


# ----------------------------- Q066 -----------------------------


def test_q066_unconfirmed_sales_not_in_official_metrics(session: Session) -> None:
    """上报销量在经理确认前不进正式口径：无 SalesConfirmation；补货停留 DRAFT。"""
    w, rep, _mgr, merchant, _c, rep_actor = _setup(session)
    task = _task(session, rep_actor, merchant)
    sku = _sku(session, w, "METRIC", "指标用 SKU")
    payload = _valid_submit(
        session, w, rep,
        sales_lines=[
            VisitSalesLineInput(sku_id=sku.id, reported_boxes=Decimal("99.99"))
        ],
        replenishment_lines=[
            VisitReplenishmentLineInput(sku_id=sku.id, requested_boxes=Decimal("7"))
        ],
    )
    record = VisitService(session).submit(rep_actor, task.id, payload)
    session.flush()

    # 上报销量只落在 visit_sales_lines，不产生任何正式销量确认
    assert session.scalar(select(func.count()).select_from(SalesConfirmation)) == 0
    # 补货草稿未经经理确认：DRAFT 且承诺量为 0，不进正式补货池/采购
    orders = VisitService(session).repo.list_replenishments_for_record(record.id)
    assert len(orders) == 1
    assert orders[0].status == ReplenishmentStatus.DRAFT.value
    assert orders[0].committed_boxes == Decimal("0")


def test_sku_from_other_dataset_rejected(session: Session) -> None:
    """演示/跨数据范围 SKU 不得挂到本巡访（数据隔离红线）：拒绝且不产生记录。"""
    w, rep, _mgr, merchant, _c, rep_actor = _setup(session)
    other = _world(session)  # 另一数据范围
    foreign_sku = _sku(session, other, "X", "其他范围 SKU")
    task = _task(session, rep_actor, merchant)
    payload = _valid_submit(
        session, w, rep,
        sales_lines=[
            VisitSalesLineInput(sku_id=foreign_sku.id, reported_boxes=Decimal("1"))
        ],
    )
    with pytest.raises(Exception) as exc:  # noqa: PT011 - 接受 DomainError 或权限错误
        VisitService(session).submit(rep_actor, task.id, payload)
        session.flush()
    assert getattr(exc.value, "code", "") in {"VISIT_SKU_NOT_FOUND", "DATA_SCOPE_VIOLATION"}
    assert session.scalar(select(func.count()).select_from(VisitRecord)) == 0

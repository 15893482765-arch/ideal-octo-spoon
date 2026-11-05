"""T14 商家调拨与业绩分界（PRD §4.7 商家责任转移、R9、A8；§4.3 归属字段）。

- Q074 生效日（含）前后归属：生效前任务/归属仍为原负责人，生效后未完成任务转新负责人；
- Q075 历史不改写：已完成任务/已提交记录的 owner 快照保持原负责人，不回写；
- Q076 待办转移与交接：OPEN TaskItem 批量转新负责人，交接快照计数正确（调用 T05）；
- Q077 生效后拒绝覆盖：已生效不能撤回/再调拨覆盖，只能新建反向调拨；
- Q078 临时授权到期：MerchantAssignment TEMPORARY 区间到期后不再命中临时负责人。

归属/分界口径唯一由 TransferService + MerchantAssignment 区间表达，本测试不自算规则。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func
from sqlalchemy.orm import Session
from tests.test_t09_contract import _ctx, _merchant, _user, _world

from app.core.errors import ConflictError, DomainError, PermissionDeniedError
from app.models import (
    MerchantAssignment,
    TaskItem,
    UserRegionBrandAuth,
    VisitRecord,
    VisitTask,
)
from app.models.enums import RoleCode
from app.services.transfer_service import (
    TransferService,
    shanghai_day_start_utc,
)

EFFECTIVE = date(2026, 6, 15)


# ----------------------------- 造数辅助 -----------------------------


def _mgr_actor(session, w, tag="mgr14"):
    mgr = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, tag)
    session.add(UserRegionBrandAuth(
        user_id=mgr.id, region_id=w["region"].id, brand_id=w["brand"].id))
    session.flush()
    actor = _ctx(mgr, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))
    return mgr, actor


def _setup(session):
    w = _world(session)
    mgr, actor = _mgr_actor(session, w)
    rep_a = _user(session, w["ds"], RoleCode.SALES_REP, "repA14")
    rep_b = _user(session, w["ds"], RoleCode.SALES_REP, "repB14")
    merchant = _merchant(session, w, name=f"T14调拨店-{uuid4().hex[:5]}")
    session.flush()
    # 原负责人 A 自主归属开放区间
    session.add(MerchantAssignment(
        merchant_id=merchant.id, owner_user_id=rep_a.id,
        assignment_type="PRIMARY",
        valid_range=func.tstzrange(
            func.to_timestamp(datetime(2026, 1, 1, tzinfo=UTC).timestamp()),
            None, "[)"),
        dataset_type="PRODUCTION", dataset_id=w["ds"]))
    session.flush()
    return w, mgr, actor, rep_a, rep_b, merchant


def _visit_task(session, merchant, user, *, status="PENDING", planned=date(2026, 6, 10)):
    t = VisitTask(
        merchant_id=merchant.id, assigned_user_id=user.id,
        planned_date=planned, status=status, source="WEEKLY_SCHEDULE",
        priority=2, dataset_type="PRODUCTION", dataset_id=merchant.dataset_id)
    session.add(t)
    session.flush()
    return t


def _open_todo(session, merchant, user, *, kind="OTHER", status="OPEN"):
    t = TaskItem(
        title="交接待办", task_kind=kind, status=status,
        related_object_type="MERCHANT", related_object_id=merchant.id,
        assignee_id=user.id)
    session.add(t)
    session.flush()
    return t


def _create_and_apply(session, actor, merchant, rep_a, rep_b, effective=EFFECTIVE):
    svc = TransferService(session)
    tr = svc.create_transfer(
        actor, merchant_id=merchant.id, from_user_id=rep_a.id,
        to_user_id=rep_b.id, effective_date=effective, reason="片区调整，交接商家")
    return svc.apply(actor, transfer_id=tr.id)


# ----------------------------- Q074 -----------------------------


def test_q074_owner_before_and_on_after_effective(session: Session) -> None:
    """生效日（含）为界：生效前归原负责人；生效日当天及以后未完成任务转新负责人。"""
    w, _mgr, actor, rep_a, rep_b, merchant = _setup(session)
    svc = TransferService(session)
    ds = w["ds"]

    before = shanghai_day_start_utc(EFFECTIVE) - timedelta(hours=6)
    at_effective = shanghai_day_start_utc(EFFECTIVE)
    after = shanghai_day_start_utc(date(2026, 6, 20))

    # 一条生效日后计划的未完成任务：应在生效时转交新负责人
    future_task = _visit_task(session, merchant, rep_a, status="PENDING",
                              planned=date(2026, 6, 18))
    session.flush()

    # 生效前归属解析：A
    assert svc.owner_at(merchant_id=merchant.id, dataset_id=ds, at=before) == rep_a.id

    transfer = _create_and_apply(session, actor, merchant, rep_a, rep_b)
    assert transfer.status == "EFFECTIVE"

    # 生效日（含）起归属：B；生效前时点仍可解析到 A（区间不回写）
    assert svc.owner_at(merchant_id=merchant.id, dataset_id=ds, at=at_effective) == rep_b.id
    assert svc.owner_at(merchant_id=merchant.id, dataset_id=ds, at=after) == rep_b.id
    assert svc.owner_at(merchant_id=merchant.id, dataset_id=ds, at=before) == rep_a.id

    # 生效日之后计划的未完成任务已转 B
    assert session.get(VisitTask, future_task.id).assigned_user_id == rep_b.id


# ----------------------------- Q075 -----------------------------


def test_q075_history_not_rewritten(session: Session) -> None:
    """已完成任务与已提交巡访记录的 owner 快照不回写，仍归原负责人。"""
    w, _mgr, actor, rep_a, rep_b, merchant = _setup(session)

    # 生效前：一条已完成任务 + 其正式记录，owner 为 A
    done = _visit_task(session, merchant, rep_a, status="COMPLETED",
                       planned=date(2026, 6, 1))
    rec = VisitRecord(
        visit_task_id=done.id, merchant_id=merchant.id, user_id=rep_a.id,
        source="WEEKLY_SCHEDULE", is_official=True, status="RECOGNIZED",
        validity="VALID", conclusion="NORMAL", freezer_status="NORMAL",
        photo_count=1,
        dataset_type="PRODUCTION", dataset_id=w["ds"])
    session.add(rec)
    # 一条未完成任务（应被转移）
    pending = _visit_task(session, merchant, rep_a, status="PENDING",
                          planned=date(2026, 6, 20))
    session.flush()

    _create_and_apply(session, actor, merchant, rep_a, rep_b)
    session.expunge_all()

    done_after = session.get(VisitTask, done.id)
    rec_after = session.get(VisitRecord, rec.id)
    pending_after = session.get(VisitTask, pending.id)

    # 历史事实不改写
    assert done_after.assigned_user_id == rep_a.id
    assert rec_after.user_id == rep_a.id
    # 未完成任务转交
    assert pending_after.assigned_user_id == rep_b.id


# ----------------------------- Q076 -----------------------------


def test_q076_open_todos_transfer_and_handover(session: Session) -> None:
    """生效时 OPEN 待办批量转新负责人（T05），交接快照含任务/待办计数。"""
    w, _mgr, actor, rep_a, rep_b, merchant = _setup(session)
    t1 = _open_todo(session, merchant, rep_a, kind="PROBLEM_RECTIFICATION")
    t2 = _open_todo(session, merchant, rep_a, kind="FREEZER_RECALL")
    # 已关闭待办不转移
    closed = _open_todo(session, merchant, rep_a, kind="OTHER", status="CLOSED")
    visit = _visit_task(session, merchant, rep_a, status="PENDING")
    session.flush()

    transfer = _create_and_apply(session, actor, merchant, rep_a, rep_b)

    assert session.get(TaskItem, t1.id).assignee_id == rep_b.id
    assert session.get(TaskItem, t2.id).assignee_id == rep_b.id
    assert session.get(TaskItem, closed.id).assignee_id == rep_a.id
    assert session.get(VisitTask, visit.id).assigned_user_id == rep_b.id
    # 交接快照
    assert transfer.handover_json["open_todo_count"] == 2
    assert str(visit.id) in transfer.handover_json["open_visit_task_ids"]


# ----------------------------- Q077 -----------------------------


def test_q077_after_effective_reject_overwrite_allow_reverse(session: Session) -> None:
    """生效后不能撤回、不能再对该商家发起同向调拨；只能新建反向调拨。"""
    w, _mgr, actor, rep_a, rep_b, merchant = _setup(session)
    svc = TransferService(session)
    transfer = _create_and_apply(session, actor, merchant, rep_a, rep_b)

    # 已生效：撤回被拒
    with pytest.raises(ConflictError) as exc:
        svc.withdraw(actor, transfer_id=transfer.id, reason="想撤回")
    assert exc.value.code == "TRANSFER_ALREADY_EFFECTIVE"

    # 已生效：不能再发一张 A→B 的开放调拨（当前主负责人已是 B，from 不匹配）
    with pytest.raises(DomainError) as exc2:
        svc.create_transfer(
            actor, merchant_id=merchant.id, from_user_id=rep_a.id,
            to_user_id=rep_b.id, effective_date=date(2026, 6, 16), reason="再调一次")
    # 来自“原负责人不是当前主负责人”或开放单冲突，均为业务拒绝（这里主负责人已变）
    assert exc2.value.code in {
        "TRANSFER_FROM_NOT_OWNER", "TRANSFER_ALREADY_OPEN"}

    # 幂等：重复 apply 不重复转移
    again = svc.apply(actor, transfer_id=transfer.id)
    assert again.id == transfer.id and again.status == "EFFECTIVE"

    # 唯一允许：新建反向调拨 B→A，并回链
    reverse = svc.create_reverse(
        actor, original_id=transfer.id,
        effective_date=date(2026, 6, 20), reason="商家退回原负责人")
    session.refresh(transfer)
    assert reverse.from_user_id == rep_b.id
    assert reverse.to_user_id == rep_a.id
    assert reverse.reverses_id == transfer.id
    assert transfer.reversed_by_id == reverse.id

    # 反向调拨生效：归属回到 A（端到端），历史区间不回写
    svc.apply(actor, transfer_id=reverse.id)
    back_owner = svc.owner_at(
        merchant_id=merchant.id, dataset_id=w["ds"],
        at=shanghai_day_start_utc(date(2026, 6, 20)))
    assert back_owner == rep_a.id
    # 原调拨生效区间内（6/15~6/20）归属仍是 B，历史分界保留
    mid = shanghai_day_start_utc(date(2026, 6, 16))
    assert svc.owner_at(
        merchant_id=merchant.id, dataset_id=w["ds"], at=mid) == rep_b.id


def test_q077b_withdraw_allowed_before_effective(session: Session) -> None:
    """生效前允许撤回；撤回后归属与任务均不变，且可重新发起。"""
    w, _mgr, actor, rep_a, rep_b, merchant = _setup(session)
    svc = TransferService(session)
    tr = svc.create_transfer(
        actor, merchant_id=merchant.id, from_user_id=rep_a.id,
        to_user_id=rep_b.id, effective_date=EFFECTIVE, reason="拟调拨")
    withdrawn = svc.withdraw(actor, transfer_id=tr.id, reason="商家情况有变，暂缓")
    assert withdrawn.status == "WITHDRAWN"

    # 归属仍为 A
    assert svc.owner_at(
        merchant_id=merchant.id, dataset_id=w["ds"],
        at=shanghai_day_start_utc(date(2026, 6, 16))) == rep_a.id
    # 撤回后可重新发起
    tr2 = svc.create_transfer(
        actor, merchant_id=merchant.id, from_user_id=rep_a.id,
        to_user_id=rep_b.id, effective_date=date(2026, 6, 18), reason="重新调拨")
    assert tr2.status == "PENDING"


# ----------------------------- Q078 -----------------------------


def test_q078_temporary_grant_expiry(session: Session) -> None:
    """临时协作授权有到期时间；到期后不再命中临时负责人（PRD R7/§4.7 交接）。"""
    w, _mgr, actor, rep_a, rep_b, merchant = _setup(session)
    svc = TransferService(session)
    rep_c = _user(session, w["ds"], RoleCode.SALES_REP, "repC14")

    grant_start = datetime(2026, 6, 1, tzinfo=UTC)
    grant_end = shanghai_day_start_utc(EFFECTIVE)  # 生效日 00:00 到期（半开，不含）
    session.add(MerchantAssignment(
        merchant_id=merchant.id, owner_user_id=rep_c.id,
        assignment_type="TEMPORARY",
        valid_range=func.tstzrange(grant_start, grant_end, "[)"),
        dataset_type="PRODUCTION", dataset_id=w["ds"]))
    session.flush()

    # 到期前：临时授权有效
    before_end = grant_end - timedelta(hours=6)
    active = svc.repo.active_temporary_assignments(merchant.id, before_end)
    assert [a.owner_user_id for a in active] == [rep_c.id]

    # 生效（临时授权恰在生效日 00:00 到期）
    transfer = _create_and_apply(session, actor, merchant, rep_a, rep_b)
    # 交接时点（生效日 00:00，半开）临时授权已到期 → 不交接
    assert transfer.handover_json["temporary_grant_user_ids"] == []
    # 到期后无临时授权命中
    after = svc.repo.active_temporary_assignments(merchant.id, grant_end)
    assert active and after == []
    # 主归属已切到 B
    assert svc.owner_at(
        merchant_id=merchant.id, dataset_id=w["ds"], at=grant_end) == rep_b.id


# ----------------------------- 调拨表单选项（前端 UI） -----------------------------


def test_q188_transfer_options_lists_current_owner_and_candidates(
    session: Session,
) -> None:
    """调拨下拉：返回当前主负责人 + 本片区已授权的在职业务员；业务员无权读取。"""
    w, _mgr, actor, rep_a, rep_b, merchant = _setup(session)
    for user in (rep_a, rep_b):
        session.add(UserRegionBrandAuth(
            user_id=user.id, region_id=w["region"].id, brand_id=w["brand"].id))
    session.flush()

    data = TransferService(session).options(actor, merchant_id=merchant.id)
    assert data["from_user"]["id"] == rep_a.id
    candidate_ids = {u["id"] for u in data["items"]}
    assert {rep_a.id, rep_b.id} <= candidate_ids

    # 业务员没有片区范围：即使有 merchant 能力也不能读调拨选项
    rep_actor = _ctx(rep_a, RoleCode.SALES_REP.value, frozenset())
    with pytest.raises(PermissionDeniedError):
        TransferService(session).options(rep_actor, merchant_id=merchant.id)

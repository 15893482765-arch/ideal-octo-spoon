"""T16 督导跟访与整改闭环（PRD §4.8 / R14 / R15 / 验收 A10）。

- Q084 整改状态严格顺序 OPEN→IN_PROGRESS→SUBMITTED→VERIFIED→CLOSED，禁止跳级；
- Q085 验证人不能是整改提交人；非提交人（督导）验证通过；
- Q086 同一业务员被跟访两次：次数达标但覆盖业务员仅 1 人 → 不达标（预警）；
- Q087 跟访 2 次且覆盖 2 名不同业务员 → 达标；
- Q088 督导跟访不替代/不计入业务员巡访（不产生 VisitTask/VisitRecord）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tests.test_t09_contract import _ctx, _merchant, _user, _world

from app.core.errors import DomainError, PermissionDeniedError
from app.core.permissions import PermissionError_
from app.models import (
    Rectification,
    UserRegionBrandAuth,
    VisitRecord,
    VisitTask,
)
from app.models.enums import RoleCode
from app.services.supervision_service import SupervisionService

TZ = ZoneInfo("Asia/Shanghai")
MONDAY = date(2026, 12, 14)


# ----------------------------- 造数 -----------------------------


def _grant_region(session, user, region, brand, ds) -> None:
    session.add(UserRegionBrandAuth(
        user_id=user.id, region_id=region.id, brand_id=brand.id))


def _people(session, w, tag):
    """返回 (supervisor, sup_actor, rep, rep_actor)，均挂到片区。"""
    sup = _user(session, w["ds"], RoleCode.SUPERVISOR, f"{tag}-sup")
    rep = _user(session, w["ds"], RoleCode.SALES_REP, f"{tag}-rep")
    _grant_region(session, sup, w["region"], w["brand"], w["ds"])
    _grant_region(session, rep, w["region"], w["brand"], w["ds"])
    session.flush()
    sup_actor = _ctx(sup, RoleCode.SUPERVISOR.value, frozenset({w["region"].id}))
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    return sup, sup_actor, rep, rep_actor


def _plan_and_submit(session, actor, w, rep, shop, day, *, score=88, with_issue=True):
    svc = SupervisionService(session)
    due = day + timedelta(days=3)
    visit = svc.plan_visit(
        actor,
        region_id=w["region"].id,
        shadowed_user_id=rep.id,
        merchant_id=shop.id,
        planned_date=day,
    )
    issues = []
    if with_issue:
        issues.append({
            "category": "DISPLAY",
            "description": "端架陈列面不足",
            "owner_user_id": rep.id,
            "due_date": due,
        })
    visited_at = datetime.combine(day, time(10, 0), tzinfo=TZ).astimezone(UTC)
    svc.submit_visit(
        actor,
        visit_id=visit.id,
        action_score=score,
        photo_count=3,
        visited_at=visited_at,
        display_issue="陈列面不足" if with_issue else None,
        note="跟访记录",
        issues=issues,
    )
    return visit


def _open_rect(session, visit) -> Rectification:
    return session.scalars(
        select(Rectification).where(
            Rectification.supervision_visit_id == visit.id)
    ).one()


# ----------------------------- Q084 -----------------------------


def test_q084_rectification_status_sequence(session: Session) -> None:
    """整改五态严格顺序，禁止跳级。"""
    w = _world(session)
    sup, sup_actor, rep, rep_actor = _people(session, w, "t16-84")
    shop = _merchant(session, w, name="T16店-084")

    visit = _plan_and_submit(session, sup_actor, w, rep, shop, MONDAY)
    rect = _open_rect(session, visit)
    assert rect.status == "OPEN"

    svc = SupervisionService(session)
    # OPEN 不能直接跳到 SUBMITTED（必须先 IN_PROGRESS）
    with pytest.raises(DomainError) as exc_skip:
        svc.submit_rectification(rep_actor, rectification_id=rect.id, note="已整改")
    assert exc_skip.value.code == "STATE_TRANSITION_INVALID"

    # 正常顺序推进
    svc.start_rectification(rep_actor, rectification_id=rect.id)
    assert session.get(Rectification, rect.id).status == "IN_PROGRESS"

    svc.submit_rectification(rep_actor, rectification_id=rect.id, note="已补齐陈列")
    rect = session.get(Rectification, rect.id)
    assert rect.status == "SUBMITTED"
    assert rect.submitter_id == rep.id

    # 非提交人督导验证
    svc.verify_rectification(sup_actor, rectification_id=rect.id, passed=True,
                             note="现场复核合格")
    assert session.get(Rectification, rect.id).status == "VERIFIED"

    svc.close_rectification(sup_actor, rectification_id=rect.id)
    final = session.get(Rectification, rect.id)
    assert final.status == "CLOSED"
    assert final.closed_at is not None

    # CLOSED 为终态：不能再迁移
    with pytest.raises(DomainError) as exc_end:
        svc.verify_rectification(sup_actor, rectification_id=rect.id, passed=True)
    assert exc_end.value.code == "STATE_TRANSITION_INVALID"


# ----------------------------- Q085 -----------------------------


def test_q085_submitter_cannot_verify(session: Session) -> None:
    """提交人不能验证自己的整改；换非提交人（督导）才能验证。"""
    w = _world(session)
    sup, sup_actor, rep, rep_actor = _people(session, w, "t16-85")
    shop = _merchant(session, w, name="T16店-085")
    visit = _plan_and_submit(session, sup_actor, w, rep, shop, MONDAY)
    rect = _open_rect(session, visit)

    svc = SupervisionService(session)
    svc.start_rectification(rep_actor, rectification_id=rect.id)
    svc.submit_rectification(rep_actor, rectification_id=rect.id, note="整改完成")

    # 业务员（整改责任人）无 MODULE_SUPERVISION 能力，不能验证
    with pytest.raises(PermissionError_):
        svc.verify_rectification(rep_actor, rectification_id=rect.id, passed=True)

    # 督导（非提交人）验证通过
    verified = svc.verify_rectification(
        sup_actor, rectification_id=rect.id, passed=True, note="合格")
    assert verified.status == "VERIFIED"
    assert verified.verifier_id == sup.id
    assert verified.verifier_id != verified.submitter_id

    # DB CHECK 兜底：verifier == submitter 直接写库应被数据库拒绝（savepoint 回滚）
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.execute(
                text("UPDATE rectifications SET verifier_id = submitter_id WHERE id = :id"),
                {"id": rect.id},
            )


def test_q085_verifier_equal_submitter_rejected(session: Session) -> None:
    """即便提交人本身具备督导能力，也不能验证自己提交的整改（职责分离）。"""
    w = _world(session)
    sup, sup_actor, rep, rep_actor = _people(session, w, "t16-85s")
    shop = _merchant(session, w, name="T16店-085s")
    visit = _plan_and_submit(session, sup_actor, w, rep, shop, MONDAY)
    rect = _open_rect(session, visit)

    svc = SupervisionService(session)
    svc.start_rectification(rep_actor, rectification_id=rect.id)
    svc.submit_rectification(rep_actor, rectification_id=rect.id, note="完成")

    # 人为把 submitter 置为督导本人：督导尝试验证自己“提交”的整改 → 硬门禁
    rect = session.get(Rectification, rect.id)
    rect.submitter_id = sup.id
    session.flush()
    with pytest.raises(PermissionDeniedError) as exc:
        svc.verify_rectification(sup_actor, rectification_id=rect.id, passed=True)
    assert exc.value.code == "RECTIFICATION_VERIFIER_IS_SUBMITTER"


# ----------------------------- Q086 -----------------------------


def test_q086_two_visits_same_rep_not_compliant(session: Session) -> None:
    """同一业务员被跟访 2 次：visit_count=2 达标，但 distinct=1 → 不达标预警。"""
    w = _world(session)
    sup, sup_actor, rep, _ = _people(session, w, "t16-86")
    shop1 = _merchant(session, w, name="T16店-086a")
    shop2 = _merchant(session, w, name="T16店-086b")
    _plan_and_submit(session, sup_actor, w, rep, shop1, MONDAY, with_issue=False)
    _plan_and_submit(session, sup_actor, w, rep, shop2, MONDAY + timedelta(days=1),
                     with_issue=False)

    coverage = SupervisionService(session).weekly_coverage(
        sup_actor, region_id=w["region"].id, week_start=MONDAY,
        supervisor_id=sup.id,
    )
    assert len(coverage) == 1
    c = coverage[0]
    assert c.visit_count == 2
    assert c.distinct_rep_count == 1
    assert c.compliant is False
    assert c.needs_warning is True
    assert any("覆盖" in msg and "1" in msg for msg in c.warnings)


# ----------------------------- Q087 -----------------------------


def test_q087_two_visits_two_reps_compliant(session: Session) -> None:
    """跟访 2 次覆盖 2 名不同业务员 → 达标。"""
    w = _world(session)
    sup, sup_actor, rep1, _ = _people(session, w, "t16-87a")
    rep2 = _user(session, w["ds"], RoleCode.SALES_REP, "t16-87b-rep")
    _grant_region(session, rep2, w["region"], w["brand"], w["ds"])
    session.flush()
    shop1 = _merchant(session, w, name="T16店-087a")
    shop2 = _merchant(session, w, name="T16店-087b")
    _plan_and_submit(session, sup_actor, w, rep1, shop1, MONDAY, with_issue=False)
    _plan_and_submit(session, sup_actor, w, rep2, shop2, MONDAY + timedelta(days=2),
                     with_issue=False)

    coverage = SupervisionService(session).weekly_coverage(
        sup_actor, region_id=w["region"].id, week_start=MONDAY,
        supervisor_id=sup.id,
    )
    c = coverage[0]
    assert c.visit_count == 2
    assert c.distinct_rep_count == 2
    assert c.compliant is True
    assert c.warnings == ()


def test_q087_zero_visits_warning(session: Session) -> None:
    """整周零跟访也应预警（周末工作台）。"""
    w = _world(session)
    sup, sup_actor, _rep, _ = _people(session, w, "t16-87z")
    coverage = SupervisionService(session).weekly_coverage(
        sup_actor, region_id=w["region"].id, week_start=MONDAY,
        supervisor_id=sup.id,
    )
    c = coverage[0]
    assert c.visit_count == 0
    assert c.compliant is False


# ----------------------------- Q088 -----------------------------


def test_q088_supervision_does_not_count_as_visit(session: Session) -> None:
    """督导跟访不替代业务员巡访：不产生任何 VisitTask/VisitRecord。"""
    w = _world(session)
    sup, sup_actor, rep, _ = _people(session, w, "t16-88")
    shop = _merchant(session, w, name="T16店-088")
    _plan_and_submit(session, sup_actor, w, rep, shop, MONDAY)

    visit_tasks = session.scalar(
        select(func.count()).select_from(VisitTask)
        .where(VisitTask.dataset_id == w["ds"])
    )
    visit_records = session.scalar(
        select(func.count()).select_from(VisitRecord)
        .where(VisitRecord.dataset_id == w["ds"])
    )
    assert visit_tasks == 0
    assert visit_records == 0

    # 跟访事实本身独立存在
    from app.models import SupervisionVisit

    sv = session.scalars(select(SupervisionVisit)).one()
    assert sv.status == "SUBMITTED"
    assert sv.supervisor_id == sup.id
    assert sv.shadowed_user_id == rep.id
    assert sv.action_score == 88

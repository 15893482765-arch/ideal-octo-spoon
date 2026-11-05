"""T27 角色工作台、通知与全局搜索（PRD §4.2、§1.5、§4.11、§9.1）。

- Q154 五类角色聚合差异：业务员/经理/督导/财务/厂商的指标卡、待办、快捷动作不同；
- Q155 全局搜索越权为空：只返回当前行级范围内商家，跨片区/无权限角色搜不到；
- Q156 指标同源：工作台 MetricCard 与 MetricsService 直接同源（口径/截至时间一致）；
- Q157 深链恢复：深链保留 period/q/rule_code 等筛选参数；
- Q158 通知已读独立：标记已读不改任务/审批/风险状态；
- Q159 首屏 P95 基线：单次 home 聚合耗时低于阈值；
- Q185 风险清单：权限裁剪、RK-1→RK-7 优先级排序、关闭复用 RiskService 并留审计。
"""

from __future__ import annotations

import statistics
import time
from datetime import UTC, date

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tests.test_t09_contract import _ctx, _merchant, _user, _world

from app.core.errors import DomainError, PermissionDeniedError
from app.core.permissions import RoleCode
from app.models.approval import ApprovalRequest, TaskItem
from app.models.audit import AuditLog
from app.models.enums import (
    ApprovalStatus,
    RiskRuleCode,
    RiskStatus,
    TaskStatus,
)
from app.models.merchant_assignment import MerchantAssignment
from app.models.notification import NotificationRead
from app.models.risk import RiskItem
from app.models.user import UserRegionBrandAuth
from app.services.notification_service import (
    SRC_TASK,
    NotificationService,
    notification_key,
)
from app.services.risk_service import RiskService
from app.services.search_service import build_deep_link
from app.services.workbench_service import WorkbenchService

# ------------------------------- 构造助手 -------------------------------


def _auth(session, user, w, brand=None):
    session.add(UserRegionBrandAuth(
        user_id=user.id, region_id=w["region"].id,
        brand_id=(brand or w["brand"]).id,
    ))
    session.flush()


def _assign_primary(session, w, merchant, user):
    session.add(MerchantAssignment(
        merchant_id=merchant.id, owner_user_id=user.id,
        assignment_type="PRIMARY",
        valid_range=func.tstzrange(func.to_timestamp(946684800), None, "[)"),
        dataset_type="PRODUCTION", dataset_id=w["ds"],
    ))
    session.flush()


def _people(session, w, roles):
    return {role: _user(session, w["ds"], RoleCode(role), f"t27-{role}") for role in roles}


def _risk(session, w, merchant, rule_code, *, status=RiskStatus.OPEN.value,
          obj_type="MERCHANT", obj_id=None, first_seen=None, period=None):
    from datetime import datetime

    seen = first_seen or date(2026, 12, 1)
    if isinstance(seen, date) and not isinstance(seen, datetime):
        seen = datetime(seen.year, seen.month, seen.day, tzinfo=UTC)
    r = RiskItem(
        dataset_type="PRODUCTION", dataset_id=w["ds"],
        rule_code=rule_code,
        object_type=obj_type,
        object_id=obj_id or merchant.id,
        period=period or "2026-W50",
        status=status, severity="HIGH",
        first_seen_at=seen, last_seen_at=seen,
    )
    session.add(r)
    session.flush()
    return r


_TASK_KINDS = [
    "VISIT_PLANNED", "CONTRACT_RENEWAL_REVIEW", "FREEZER_RECALL",
    "FREEZER_ACCEPTANCE", "FREEZER_DEPOSIT_REFUND", "REPLENISHMENT_REVIEW",
    "PROBLEM_RECTIFICATION", "SUPERVISION_RECTIFICATION",
    "PROBLEM_RECTIFICATION_VERIFY",
]


def _task(session, user, *, status=TaskStatus.OPEN.value, kind=None,
          title="待办", related_id=None, related_type="MERCHANT"):
    kind = kind or _TASK_KINDS[0]
    t = TaskItem(
        task_kind=kind, title=title, status=status,
        assignee_id=user.id,
        created_by_id=user.id,
        related_object_type=related_type if related_id else None,
        related_object_id=related_id,
    )
    session.add(t)
    session.flush()
    return t


ROLES = [
    RoleCode.SALES_REP.value,
    RoleCode.REGIONAL_MANAGER.value,
    RoleCode.SUPERVISOR.value,
    RoleCode.FINANCE.value,
    RoleCode.SUPPLIER.value,
]


# ============================ Q154 五类角色聚合差异 ============================


def test_q154_five_roles_have_distinct_workbench(session: Session) -> None:
    w = _world(session)
    people = _people(session, w, ROLES)
    mgr = people[RoleCode.REGIONAL_MANAGER.value]
    _auth(session, mgr, w)
    merchant = _merchant(session, w, "T27-聚合店")
    _assign_primary(session, w, merchant, people[RoleCode.SALES_REP.value])
    for u in people.values():
        if u.role_id != mgr.role_id:
            _auth(session, u, w)
    _risk(session, w, merchant, RiskRuleCode.RK1_NO_VISIT.value)
    session.flush()

    homes: dict[str, dict] = {}
    svc = WorkbenchService(session)
    for role in ROLES:
        homes[role] = svc.home(_ctx(people[role], role, frozenset({w["region"].id})),
                               period="2026-12")

    keys_by_role = {role: {c["key"] for c in h["metric_cards"]}
                    for role, h in homes.items()}
    # 角色间指标卡集合不同（经理最全 9 张；督导只跟访相关；厂商最少）
    assert keys_by_role[RoleCode.REGIONAL_MANAGER.value] > keys_by_role[RoleCode.SUPERVISOR.value]
    assert keys_by_role[RoleCode.SUPERVISOR.value] == {
        "ON_TIME_VISIT_RATE", "DISPLAY_COMPLIANCE_RATE", "WEEK_COVERAGE_RATE",
    }
    # T35：工作台不再展示月度净利润（经理与财务两个视角都不再下发该卡）
    assert "MONTHLY_NET_PROFIT" not in keys_by_role[RoleCode.FINANCE.value]
    assert "MONTHLY_NET_PROFIT" not in keys_by_role[RoleCode.REGIONAL_MANAGER.value]
    assert keys_by_role[RoleCode.FINANCE.value] == {
        "CONFIRMED_SALES_BOXES",
        "REPLENISHMENT_ON_TIME_RATE", "FREEZER_STOCK_DIFF",
    }
    assert keys_by_role[RoleCode.SUPPLIER.value] == {
        "CONFIRMED_SALES_BOXES", "REPLENISHMENT_ON_TIME_RATE",
    }
    # 只有经理/督导首页带风险清单区块
    assert homes[RoleCode.REGIONAL_MANAGER.value]["risk_list"]
    assert homes[RoleCode.SUPERVISOR.value]["risk_list"]
    assert homes[RoleCode.SALES_REP.value]["risk_list"] == []
    assert homes[RoleCode.SUPPLIER.value]["risk_list"] == []
    # 快捷动作不同：财务有工资核算、厂商有待确认供货
    fin_paths = {a["path"] for a in homes[RoleCode.FINANCE.value]["quick_actions"]}
    sup_paths = {a["path"] for a in homes[RoleCode.SUPPLIER.value]["quick_actions"]}
    assert "/payroll" in fin_paths and "/payroll" not in sup_paths
    assert "/procurement/confirm" in sup_paths and "/procurement/confirm" not in fin_paths


# ============================ Q155 搜索越权为空 ============================


def test_q155_search_only_returns_in_scope_objects(session: Session) -> None:
    from app.services.search_service import GlobalSearchService

    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "t27-rep-search")
    mgr = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "t27-mgr-search")
    sup = _user(session, w["ds"], RoleCode.SUPPLIER, "t27-sup-search")
    _auth(session, rep, w)
    _auth(session, mgr, w)
    session.flush()
    mine = _merchant(session, w, "我的便利店旗舰店")
    other = _merchant(session, w, "别人的便利店")
    _assign_primary(session, w, mine, rep)
    session.flush()

    svc = GlobalSearchService(session)
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    mgr_actor = _ctx(mgr, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))
    sup_actor = _ctx(sup, RoleCode.SUPPLIER.value, frozenset({w["region"].id}))

    # 业务员只命中自己负责的商家
    rep_hits = svc.search_merchants(rep_actor, "便利店")
    assert {h.object_id for h in rep_hits} == {mine.id}
    assert other.id not in {h.object_id for h in rep_hits}
    # 经理（本片区）两个都能命中
    mgr_hits = svc.search_merchants(mgr_actor, "便利店")
    assert {h.object_id for h in mgr_hits} == {mine.id, other.id}
    # 越权关键字：用另一片区商家名搜索，本片区经理结果为空
    w2 = _world(session)
    mgr2 = _user(session, w2["ds"], RoleCode.REGIONAL_MANAGER, "t27-mgr2")
    _auth(session, mgr2, w2)
    alien = _merchant(session, w2, "跨片区便利店")
    session.flush()
    mgr2_actor = _ctx(mgr2, RoleCode.REGIONAL_MANAGER.value,
                      frozenset({w2["region"].id}))
    # 数据域隔离 + 片区裁剪：mgr2 只能看到本数据域本片区的 alien，
    # 绝看不到 w1 数据域的任何商家（越权为空）
    mgr2_hits = svc.search_merchants(mgr2_actor, "便利店")
    assert {h.object_id for h in mgr2_hits} == {alien.id}
    assert mine.id not in {h.object_id for h in mgr2_hits}
    assert other.id not in {h.object_id for h in mgr2_hits}
    assert alien.id not in {h.object_id for h in mgr_hits}
    # 厂商无商家模块权限：任何搜索都为空
    assert svc.search_merchants(sup_actor, "便利店") == []
    # 短词不搜
    assert svc.search_merchants(rep_actor, "便") == []


# ============================ Q156 指标同源 ============================


def test_q156_metric_cards_same_source_as_metrics_service(session: Session) -> None:
    from app.services.demo_data_guard import DataScope
    from app.services.metrics_service import MetricsService

    w = _world(session)
    mgr = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "t27-src-mgr")
    _auth(session, mgr, w)
    session.flush()
    actor = _ctx(mgr, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))
    period = "2026-12"

    home = WorkbenchService(session).home(actor, period=period)
    direct = {r.key: r for r in MetricsService(session).all(
        actor, scope=DataScope.PRODUCTION, period=period)}

    assert home["metric_cards"], "经理首页必须有指标卡"
    for card in home["metric_cards"]:
        src = direct[card["key"]]
        assert card["name"] == src.name
        assert card["definition"] == src.definition
        assert card["as_of"] == src.as_of.isoformat()
        assert card["unit"] == src.unit
        assert card["period"] == src.period
        assert card["source_refs"] == list(src.source_refs)
        val = src.value
        assert str(card["value"]) == (str(val) if val is not None else "None")


# ============================ Q157 深链恢复 ============================


def test_q157_deep_links_preserve_filters() -> None:
    link = build_deep_link("/merchants", q="便利店", status="ACTIVE")
    assert link == "/merchants?q=%E4%BE%BF%E5%88%A9%E5%BA%97&status=ACTIVE"
    # 空值被过滤
    assert build_deep_link("/risks", q=None, rule_code="") == "/risks"


def test_q157_workbench_and_search_deep_links_carry_filters(session: Session) -> None:
    w = _world(session)
    mgr = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "t27-link-mgr")
    _auth(session, mgr, w)
    m = _merchant(session, w, "深链便利店")
    session.flush()
    actor = _ctx(mgr, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))

    home = WorkbenchService(session).home(actor, period="2026-12")
    assert "period=2026-12" in home["deep_links"]["dashboard"]
    assert "period=2026-12" in home["deep_links"]["risks"]
    if home["risk_list"]:
        assert "rule_code=" in home["risk_list"][0]["deep_link"]

    from app.services.search_service import GlobalSearchService

    hit = GlobalSearchService(session).search_merchants(actor, "深链便利店")[0]
    assert "q=" in hit.deep_link and "merchant_id=" in hit.deep_link
    assert m.id is not None


# ============================ Q158 通知已读不影响任务状态 ============================


def test_q158_mark_read_does_not_change_source_state(session: Session) -> None:
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "t27-noti-rep")
    _auth(session, rep, w)
    task = _task(session, rep, title="完成冰柜陈列拍照")
    session.flush()
    actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    svc = NotificationService(session)

    notes = svc.list_notifications(actor)
    task_note = next(n for n in notes if n.source == SRC_TASK and n.source_id == task.id)
    assert task_note.read is False
    assert svc.unread_count(actor) >= 1

    svc.mark_read(actor, task_note.key)
    session.flush()
    # 已读标记落库
    assert session.scalar(select(func.count()).select_from(NotificationRead)
                          .where(NotificationRead.user_id == rep.id)) == 1
    # 任务状态仍是 OPEN，未被标记已读改动
    assert session.get(TaskItem, task.id).status == TaskStatus.OPEN.value
    # 再次聚合：该通知已读，未读数减少
    after = {n.key: n for n in svc.list_notifications(actor)}
    assert after[task_note.key].read is True
    # 幂等：重复标记不报错、不新增行
    svc.mark_read(actor, task_note.key)
    assert session.scalar(select(func.count()).select_from(NotificationRead)
                          .where(NotificationRead.user_id == rep.id)) == 1


def test_q158_notifications_aggregation_from_task_approval_risk(session: Session) -> None:
    w = _world(session)
    mgr = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "t27-noti-mgr")
    _auth(session, mgr, w)
    m = _merchant(session, w, "通知聚合店")
    session.flush()
    actor = _ctx(mgr, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))

    _task(session, mgr, title="经理的任务")
    _risk(session, w, m, RiskRuleCode.RK1_NO_VISIT.value)
    session.add(ApprovalRequest(
        dataset_type="PRODUCTION", dataset_id=w["ds"],
        target_object_type="MERCHANT_TRANSFER", target_object_id=m.id,
        action="approve_transfer",
        matrix_role_code=RoleCode.REGIONAL_MANAGER.value,
        status=ApprovalStatus.PENDING.value,
        request_payload={},
        requester_id=mgr.id,
    ))
    session.flush()

    svc = NotificationService(session)
    notes = svc.list_notifications(actor)
    sources = {n.source for n in notes}
    assert {"TASK", "APPROVAL", "RISK"} <= sources
    assert all(n.read is False for n in notes)
    marked = svc.mark_all_read(actor)
    assert marked == len(notes)
    assert svc.unread_count(actor) == 0


def test_q158_cannot_mark_other_users_notification(session: Session) -> None:
    w = _world(session)
    rep_a = _user(session, w["ds"], RoleCode.SALES_REP, "t27-a")
    rep_b = _user(session, w["ds"], RoleCode.SALES_REP, "t27-b")
    _auth(session, rep_a, w)
    _auth(session, rep_b, w)
    t = _task(session, rep_a)
    session.flush()
    svc = NotificationService(session)
    key = notification_key(SRC_TASK, t.id)
    actor_b = _ctx(rep_b, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    with pytest.raises(DomainError) as ei:
        svc.mark_read(actor_b, key)
    assert ei.value.code in ("NOTIFICATION_NOT_FOUND",)


# ============================ Q185 风险清单裁剪 / 排序 / 关闭留痕 ============================


def test_q185_risk_list_scope_filter_and_priority_sort(session: Session) -> None:
    w = _world(session)
    mgr = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "t27-risk-mgr")
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "t27-risk-rep")
    _auth(session, mgr, w)
    _auth(session, rep, w)
    m1 = _merchant(session, w, "风险店一")
    m2 = _merchant(session, w, "风险店二")
    _assign_primary(session, w, m1, rep)
    session.flush()

    # 乱序插入 RK-7 / RK-1 / RK-4，期望返回按 RK-1 < RK-4 < RK-7 排序
    r7 = _risk(session, w, m1, RiskRuleCode.RK7_FREEZER_REPAIR_OVERDUE.value,
               first_seen=date(2026, 12, 3))
    r1 = _risk(session, w, m1, RiskRuleCode.RK1_NO_VISIT.value,
               first_seen=date(2026, 12, 1))
    r4 = _risk(session, w, m2, RiskRuleCode.RK4_ZERO_SALES.value,
               first_seen=date(2026, 12, 2))
    session.flush()

    svc = RiskService(session)
    mgr_actor = _ctx(mgr, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))
    ordered = svc.worklist(mgr_actor)
    codes = [r.rule_code for r in ordered]
    assert codes.index("RK-1") < codes.index("RK-4") < codes.index("RK-7")
    assert {r.id for r in ordered} == {r1.id, r4.id, r7.id}

    # 业务员（self）只能看到自己负责商家 m1 上的风险（r1/r7），m2 的 r4 被裁剪
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    rep_risks = svc.filter_by_scope(rep_actor, [r1, r4, r7])
    assert {r.id for r in rep_risks} == {r1.id, r7.id}


def test_q185_cross_region_risk_is_filtered(session: Session) -> None:
    w = _world(session)
    w2 = _world(session)
    mgr = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "t27-region-mgr")
    _auth(session, mgr, w)
    other_mgr = _user(session, w2["ds"], RoleCode.REGIONAL_MANAGER, "t27-region-mgr2")
    _auth(session, other_mgr, w2)
    m_here = _merchant(session, w, "本区店")
    m_there = _merchant(session, w2, "外区店")
    r_here = _risk(session, w, m_here, RiskRuleCode.RK2_ABNORMAL_LOCATION.value)
    r_there = _risk(session, w2, m_there, RiskRuleCode.RK2_ABNORMAL_LOCATION.value)
    session.flush()

    svc = RiskService(session)
    actor = _ctx(mgr, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))
    ids = {r.id for r in svc.worklist(actor)}
    assert r_here.id in ids
    assert r_there.id not in ids


def test_q185_close_risk_via_service_leaves_audit(session: Session) -> None:
    w = _world(session)
    mgr = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "t27-mgr-close")
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "t27-rep-close")
    _auth(session, mgr, w)
    _auth(session, rep, w)
    m = _merchant(session, w, "关闭留痕店")
    r = _risk(session, w, m, RiskRuleCode.RK3_DISPLAY_BELOW.value)
    session.flush()
    mgr_actor = _ctx(mgr, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    svc = RiskService(session)

    # 关闭必须填写处理结果
    with pytest.raises(DomainError) as ei0:
        svc.close(mgr_actor, r.id, resolution="   ")
    assert ei0.value.code == "RISK_RESOLUTION_REQUIRED"

    # 经理在数据域内可关闭；其他数据域用户被 _get 拒绝（留痕前先拦）
    w2 = _world(session)
    alien = _user(session, w2["ds"], RoleCode.REGIONAL_MANAGER, "t27-alien")
    _auth(session, alien, w2)
    alien_actor = _ctx(alien, RoleCode.REGIONAL_MANAGER.value,
                       frozenset({w2["region"].id}))
    with pytest.raises(PermissionDeniedError):
        svc.close(alien_actor, r.id, resolution="越权关闭")

    # 业务员无 risk.handle 能力，不能关闭（处置入口仍受角色矩阵控制）
    with pytest.raises(PermissionDeniedError) as ei2:
        svc.close(rep_actor, r.id, resolution="业务员尝试关闭")
    assert ei2.value.code == "RISK_FORBIDDEN"

    # 经理合法关闭：状态迁移 CLOSED + 审计留痕（不物理删除）
    closed = svc.close(mgr_actor, r.id, resolution="现场复核达标")
    assert closed.status == RiskStatus.CLOSED.value
    assert closed.closed_by_id == mgr.id
    assert closed.resolution == "现场复核达标"
    log = session.scalars(select(AuditLog).where(
        AuditLog.object_type == "RISK",
        AuditLog.object_id == r.id)).first()
    assert log is not None and log.event_type == "STATE_TRANSITION"
    # 已关闭风险不再出现在工作台清单
    assert r.id not in {x.id for x in svc.worklist(mgr_actor)}


# ============================ Q159 首屏 P95 基线 ============================


def test_q159_home_p95_latency_baseline(session: Session) -> None:
    w = _world(session)
    mgr = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "t27-perf-mgr")
    _auth(session, mgr, w)
    m = _merchant(session, w, "性能店")
    for i, rule in enumerate(RiskRuleCode):
        _risk(session, w, m, rule.value, period=f"2026-W{40 + i}")
    for i in range(9):
        _task(session, mgr, title=f"性能任务{i}", kind=_TASK_KINDS[i],
              related_id=m.id)
    session.flush()
    actor = _ctx(mgr, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))

    # 预热（冷查询/编译缓存）后取多次，P95 必须在基线内（本地 PG，宽松阈值避免抖动）
    WorkbenchService(session).home(actor, period="2026-12")
    latencies_ms = []
    for _ in range(15):
        t0 = time.perf_counter()
        WorkbenchService(session).home(actor, period="2026-12")
        latencies_ms.append((time.perf_counter() - t0) * 1000)
    p95 = statistics.quantiles(latencies_ms, n=20)[-1]  # 95 分位
    assert p95 < 800, f"首屏 P95={p95:.1f}ms 超过 800ms 基线"

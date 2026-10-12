"""T05 服务内核测试（T05 / PRD §5.3.1、§4.5/4.6/4.8/4.9/4.10；R23/R24/R27）。

Q021 不可自审
Q022 重复审批（同一审批两次 decide 第二次拒）
Q023 复核任务拒绝直接 complete
Q024 交接留痕（transfer 写审计 / assignee 变更）
Q025 非法状态跳转
Q026 任务幂等（同 (object_type, object_id, task_kind) 不重复创建）
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, DomainError, PermissionDeniedError
from app.core.permissions import ActorContext
from app.core.security import hash_password
from app.models import (
    AuditLog,
    Role,
    TaskItem,
    User,
)
from app.models.enums import (
    ApprovalStatus,
    RoleCode,
    UserStatus,
)
from app.services.approval_service import (
    DECISION_APPROVED,
    DECISION_REJECTED,
    DECISION_RETURNED,
    ApprovalService,
)
from app.services.business_transition_service import BusinessTransitionService
from app.services.state_machine import (
    DOMAIN_CONTRACT,
    DOMAIN_FREEZER,
    assert_legal_transition,
    legal_targets,
)
from app.services.task_service import (
    SOURCE_RELATED_DOCUMENT,
    TASK_KIND_CONTRACT_EXPIRING_RENEWAL_EVAL,
    TASK_KIND_CONTRACT_RENEWAL_VERIFY,
    TASK_KIND_FREEZER_RECALL,
    TASK_KIND_PROBLEM_RECTIFICATION,
    TASK_KIND_PROBLEM_RECTIFICATION_VERIFY,
    TaskService,
)

# ===== 夹具 =====


def _make_role(session: Session, role_code: str, dsid: UUID) -> Role:
    role = Role(
        code=f"t05-{role_code.lower()}-{uuid4().hex[:6]}",
        name=f"T05 {role_code}",
        role_code=role_code,
        dataset_type="PRODUCTION",
        dataset_id=dsid,
    )
    session.add(role)
    session.flush()
    return role


def _make_user(session: Session, role: Role, dsid: UUID, *, name: str | None = None) -> User:
    u = User(
        username=f"t05_{role.role_code.lower()}_{uuid4().hex[:8]}",
        display_name=name or f"T05 {role.role_code}",
        role_id=role.id,
        status=UserStatus.ACTIVE.value,
        must_change_password=False,
        password_hash=hash_password("t05-pass-not-for-prod"),
        dataset_type="PRODUCTION",
        dataset_id=dsid,
    )
    session.add(u)
    session.flush()
    return u


@pytest.fixture
def t05_manager(session: Session) -> User:
    """片区经理（具备 approval 能力）。"""
    dsid = uuid4()
    role = _make_role(session, RoleCode.REGIONAL_MANAGER.value, dsid)
    return _make_user(session, role, dsid)


@pytest.fixture
def t05_sales_rep(session: Session) -> User:
    """业务员（无 approval 能力，用于 Q021 不可自审的反向证明）。"""
    dsid = uuid4()
    role = _make_role(session, RoleCode.SALES_REP.value, dsid)
    return _make_user(session, role, dsid)


@pytest.fixture
def t05_finance(session: Session) -> User:
    """财务（具备 approval 能力）。"""
    dsid = uuid4()
    role = _make_role(session, RoleCode.FINANCE.value, dsid)
    return _make_user(session, role, dsid)


def _actor(user: User, role_code: str) -> ActorContext:
    return ActorContext(
        user_id=user.id,
        role_code=role_code,
        region_ids=frozenset(),
        brand_ids=frozenset(),
    )


# ===== Q021 不可自审 =====


def test_q021_self_approval_forbidden(t05_manager: User, t05_sales_rep: User) -> None:
    """Q021：审批人 = 发起人时，ApprovalService.decide 拒绝并抛 SELF_APPROVAL_FORBIDDEN。"""
    ApprovalService.__new__(ApprovalService)  # 跳过 __init__，测试用单测不接 db_session
    # 走纯断言：仅验证 ApprovalMatrix.assert_decision_authority 行为
    from app.services.approval_matrix import (
        ApprovalRule,
        assert_decision_authority,
    )

    rule = ApprovalRule(
        action="approval.decide",
        required_ability="approval",
        self_approval_allowed=False,
    )
    actor = _actor(t05_manager, RoleCode.REGIONAL_MANAGER.value)
    # requester = actor.user_id → 不可自审
    with pytest.raises(PermissionDeniedError) as excinfo:
        assert_decision_authority(rule=rule, actor=actor, requester_id=t05_manager.id)
    assert excinfo.value.code == "SELF_APPROVAL_FORBIDDEN"

    # 反向：requester 不是 actor（业务员发起、经理审批）→ 通过
    assert (
        assert_decision_authority(
            rule=rule, actor=actor, requester_id=t05_sales_rep.id
        )
        is None
    )


def test_q021_decide_self_in_db_fails(
    session: Session, t05_manager: User
) -> None:
    """Q021 端到端：发起与审批为同一人 → 拒绝。"""
    svc = ApprovalService(session)
    # 经理 A 既做发起人又做审批人
    req = svc.submit(
        target_object_type="CONTRACT",
        target_object_id=uuid4(),
        action="approval.decide",
        requester_id=t05_manager.id,
        matrix_role_code=RoleCode.REGIONAL_MANAGER.value,
        request_payload={"note": "unit-test"},
    )
    session.flush()

    actor = _actor(t05_manager, RoleCode.REGIONAL_MANAGER.value)
    with pytest.raises(PermissionDeniedError) as excinfo:
        svc.decide(
            approval_id=req.id,
            actor=actor,
            decision=DECISION_APPROVED,
            reason="通过",
        )
    assert excinfo.value.code == "SELF_APPROVAL_FORBIDDEN"
    # 状态保持 PENDING
    session.refresh(req)
    assert req.status == ApprovalStatus.PENDING.value


def test_q021_missing_ability_fails(
    session: Session, t05_manager: User, t05_sales_rep: User
) -> None:
    """Q021 变体：业务员无 approval 能力 → 即便不是本人也不能审批。"""
    svc = ApprovalService(session)
    req = svc.submit(
        target_object_type="CONTRACT",
        target_object_id=uuid4(),
        action="approval.decide",
        requester_id=t05_manager.id,
        matrix_role_code=RoleCode.REGIONAL_MANAGER.value,
    )
    session.flush()

    actor = _actor(t05_sales_rep, RoleCode.SALES_REP.value)
    with pytest.raises(PermissionDeniedError) as excinfo:
        svc.decide(
            approval_id=req.id,
            actor=actor,
            decision=DECISION_APPROVED,
            reason="通过",
        )
    assert excinfo.value.code == "PERMISSION_DENIED"


# ===== Q022 重复审批 =====


def test_q022_repeated_decide_rejected(
    session: Session, t05_manager: User, t05_sales_rep: User
) -> None:
    """Q022：同一审批申请做出决定后，第二次 decide 抛 APPROVAL_NOT_PENDING。"""
    svc = ApprovalService(session)
    req = svc.submit(
        target_object_type="CONTRACT",
        target_object_id=uuid4(),
        action="approval.decide",
        requester_id=t05_sales_rep.id,
        matrix_role_code=RoleCode.REGIONAL_MANAGER.value,
    )
    session.flush()

    approver = _actor(t05_manager, RoleCode.REGIONAL_MANAGER.value)
    decided = svc.decide(
        approval_id=req.id,
        actor=approver,
        decision=DECISION_APPROVED,
        reason="通过",
    )
    assert decided.status == ApprovalStatus.APPROVED.value
    session.refresh(decided)

    # 二次决定
    with pytest.raises(ConflictError) as excinfo:
        svc.decide(
            approval_id=req.id,
            actor=approver,
            decision=DECISION_REJECTED,
            reason="想反悔",
        )
    assert excinfo.value.code == "APPROVAL_NOT_PENDING"


def test_q022_returned_requires_reason(
    session: Session, t05_manager: User, t05_sales_rep: User
) -> None:
    """Q022 变体：RETURNED 必须填原因，否则抛 APPROVAL_REASON_REQUIRED。"""
    svc = ApprovalService(session)
    req = svc.submit(
        target_object_type="CONTRACT",
        target_object_id=uuid4(),
        action="approval.decide",
        requester_id=t05_sales_rep.id,
        matrix_role_code=RoleCode.REGIONAL_MANAGER.value,
    )
    session.flush()

    approver = _actor(t05_manager, RoleCode.REGIONAL_MANAGER.value)
    with pytest.raises(DomainError) as excinfo:
        svc.decide(
            approval_id=req.id,
            actor=approver,
            decision=DECISION_RETURNED,
            reason="",
        )
    assert excinfo.value.code == "APPROVAL_REASON_REQUIRED"


def test_q022_duplicate_submit_blocked(
    session: Session, t05_sales_rep: User
) -> None:
    """Q022 变体：同 (object, action) 已有 PENDING，再次 submit 抛冲突。"""
    svc = ApprovalService(session)
    target_id = uuid4()
    svc.submit(
        target_object_type="CONTRACT",
        target_object_id=target_id,
        action="approval.decide",
        requester_id=t05_sales_rep.id,
        matrix_role_code=RoleCode.REGIONAL_MANAGER.value,
    )
    session.flush()
    with pytest.raises(ConflictError) as excinfo:
        svc.submit(
            target_object_type="CONTRACT",
            target_object_id=target_id,
            action="approval.decide",
            requester_id=t05_sales_rep.id,
            matrix_role_code=RoleCode.REGIONAL_MANAGER.value,
        )
    assert excinfo.value.code == "APPROVAL_ALREADY_PENDING"


# ===== Q023 复核任务拒绝直接完成 =====


def test_q023_verification_task_rejects_direct_complete(
    session: Session, t05_manager: User
) -> None:
    """Q023：复核任务（task_kind 以 VERIFY 结尾）禁止 TaskService.complete() 直接关闭。"""
    svc = TaskService(session)
    contract_id = uuid4()
    task = svc.assign(
        title="整改复核",
        assignee_id=t05_manager.id,
        task_kind=TASK_KIND_PROBLEM_RECTIFICATION_VERIFY,
        related_object_type="CONTRACT",
        related_object_id=contract_id,
        description="对问题整改进行现场复核",
    )
    session.flush()
    assert task.requires_related_document is True

    with pytest.raises(DomainError) as excinfo:
        svc.complete(task_id=task.id, actor_id=t05_manager.id, reason="想直接关闭")
    assert excinfo.value.code == "TASK_VERIFICATION_REQUIRES_DOCUMENT"


def test_q023_verification_closed_by_document(
    session: Session, t05_manager: User
) -> None:
    """Q023 路径：复核任务必须通过关联单据动作关闭，且单据对象与任务相关对象一致。"""
    svc = TaskService(session)
    contract_id = uuid4()
    task = svc.assign(
        title="整改复核",
        assignee_id=t05_manager.id,
        task_kind=TASK_KIND_CONTRACT_RENEWAL_VERIFY,
        related_object_type="CONTRACT",
        related_object_id=contract_id,
    )
    session.flush()

    # 关闭时传入的 document 与 related 不一致 → 拒绝
    with pytest.raises(DomainError) as excinfo:
        svc.close_by_document(
            task_id=task.id,
            actor_id=t05_manager.id,
            document_object_type="CONTRACT",
            document_object_id=uuid4(),  # 错的 id
            outcome="VERIFIED",
        )
    assert excinfo.value.code == "TASK_DOCUMENT_MISMATCH"

    # 正确对象 → 成功
    closed = svc.close_by_document(
        task_id=task.id,
        actor_id=t05_manager.id,
        document_object_type="CONTRACT",
        document_object_id=contract_id,
        outcome="VERIFIED",
        reason="整改已通过",
    )
    session.refresh(closed)
    assert closed.status == "CLOSED"
    assert closed.completion_source == SOURCE_RELATED_DOCUMENT
    assert closed.closed_by_id == t05_manager.id


# ===== Q024 交接留痕 =====


def test_q024_transfer_audited(session: Session, t05_manager: User, t05_finance: User) -> None:
    """Q024：任务交接必须填原因，且同事务写审计（before/after 含原/新责任人）。"""
    svc = TaskService(session)
    task = svc.assign(
        title="撤柜执行",
        assignee_id=t05_manager.id,
        task_kind=TASK_KIND_FREEZER_RECALL,
        related_object_type="FREEZER",
        related_object_id=uuid4(),
    )
    session.flush()

    transferred = svc.transfer(
        task_id=task.id,
        new_assignee_id=t05_finance.id,
        actor_id=t05_manager.id,
        reason="片区人员调整，转交财务现场执行",
    )
    session.flush()

    assert transferred.assignee_id == t05_finance.id
    # 状态：OPEN 转交后入 IN_PROGRESS
    assert transferred.status == "IN_PROGRESS"

    # 审计留痕
    rows = (
        session.query(AuditLog)
        .filter(AuditLog.object_type == "TASK", AuditLog.object_id == task.id)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].event_type == "UPDATE"
    assert rows[0].actor_id == t05_manager.id
    assert rows[0].before_json == {"status": "OPEN", "assignee_id": str(t05_manager.id)}
    assert rows[0].after_json == {
        "status": "IN_PROGRESS",
        "assignee_id": str(t05_finance.id),
    }
    assert rows[0].reason == "片区人员调整，转交财务现场执行"


def test_q024_transfer_requires_reason(
    session: Session, t05_manager: User, t05_finance: User
) -> None:
    svc = TaskService(session)
    task = svc.assign(
        title="撤柜",
        assignee_id=t05_manager.id,
        task_kind=TASK_KIND_FREEZER_RECALL,
        related_object_type="FREEZER",
        related_object_id=uuid4(),
    )
    session.flush()
    with pytest.raises(DomainError) as excinfo:
        svc.transfer(
            task_id=task.id,
            new_assignee_id=t05_finance.id,
            actor_id=t05_manager.id,
            reason="",
        )
    assert excinfo.value.code == "TASK_TRANSFER_REASON_REQUIRED"


def test_q024_batch_transfer_audited(
    session: Session, t05_manager: User, t05_finance: User
) -> None:
    """Q024 批量：同事务多任务交接，审计按对象留痕。"""
    svc = TaskService(session)
    task_ids = []
    for i in range(3):
        t = svc.assign(
            title=f"整改-{i}",
            assignee_id=t05_manager.id,
            task_kind=TASK_KIND_PROBLEM_RECTIFICATION,
            related_object_type="MERCHANT",
            related_object_id=uuid4(),
        )
        session.flush()
        task_ids.append(t.id)
    svc.batch_transfer(
        task_ids=task_ids,
        new_assignee_id=t05_finance.id,
        actor_id=t05_manager.id,
        reason="批量交接",
    )
    session.flush()
    for tid in task_ids:
        audit = (
            session.query(AuditLog)
            .filter(AuditLog.object_type == "TASK", AuditLog.object_id == tid)
            .one()
        )
        assert audit.reason == "批量交接"


# ===== Q025 非法状态跳转 =====


def test_q025_illegal_state_transition_rejected() -> None:
    """Q025 核心：state_machine.assert_legal_transition 对非法 from→to 抛错。"""
    # 合同：DRAFT → ACTIVE 不允许（必须经 PENDING_APPROVAL）
    with pytest.raises(DomainError) as excinfo:
        assert_legal_transition(DOMAIN_CONTRACT, "DRAFT", "ACTIVE")
    assert excinfo.value.code == "STATE_TRANSITION_INVALID"

    # 冰柜：DEPLOYED → IN_STOCK 不允许（必须经 IN_TRANSIT）
    with pytest.raises(DomainError) as excinfo:
        assert_legal_transition(DOMAIN_FREEZER, "DEPLOYED", "IN_STOCK")
    assert excinfo.value.code == "STATE_TRANSITION_INVALID"

    # 未知状态
    with pytest.raises(DomainError):
        assert_legal_transition(DOMAIN_CONTRACT, "UNKNOWN", "ACTIVE")


def test_q025_legal_transition_audited(
    session: Session, t05_manager: User
) -> None:
    """Q025 业务路径：合法迁移由 BusinessTransitionService 落审计。

    创建一个完整 Contract（依赖 Region/Brand/Merchant），验证：
    1. DRAFT → PENDING_APPROVAL 合法，写审计；
    2. 非法跳转被 state_machine 拒绝，实体不变。
    """
    from app.models import Brand, Contract, Merchant, Organization, Region

    dsid = uuid4()
    org = Organization(
        code=f"T05-ORG-{uuid4().hex[:6]}",
        name="T05 测试组织",
        dataset_type="PRODUCTION",
        dataset_id=dsid,
    )
    session.add(org)
    session.flush()  # 先取 org.id
    region = Region(
        code=f"T05-REG-{uuid4().hex[:6]}",
        name="T05 测试片区",
        org_id=org.id,
        dataset_type="PRODUCTION",
        dataset_id=dsid,
    )
    brand = Brand(
        code=f"T05-BR-{uuid4().hex[:6]}",
        name="T05 测试品牌",
        dataset_type="PRODUCTION",
        dataset_id=dsid,
    )
    session.add_all([region, brand])
    session.flush()
    merchant = Merchant(
        code=f"T05-M-{uuid4().hex[:6]}",
        name="T05 测试商家",
        region_id=region.id,
        brand_id=brand.id,
        status="ACTIVE",
        dataset_type="PRODUCTION",
        dataset_id=dsid,
    )
    session.add(merchant)
    session.flush()
    from datetime import date

    contract = Contract(
        code=f"T05-C-{uuid4().hex[:6]}",
        merchant_id=merchant.id,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        status="DRAFT",
        display_threshold_pct=50,
        dataset_type="PRODUCTION",
        dataset_id=dsid,
    )
    session.add(contract)
    session.flush()

    svc = BusinessTransitionService(session)
    svc.transition(
        entity=contract,
        domain=DOMAIN_CONTRACT,
        to_state="PENDING_APPROVAL",
        actor_id=t05_manager.id,
        reason="提交审批",
    )
    session.flush()
    assert contract.status == "PENDING_APPROVAL"

    audit = (
        session.query(AuditLog)
        .filter(
            AuditLog.object_type == DOMAIN_CONTRACT,
            AuditLog.object_id == contract.id,
        )
        .one()
    )
    assert audit.event_type == "STATE_TRANSITION"
    assert audit.reason == "提交审批"
    assert audit.before_json == {"status": "DRAFT"}
    assert audit.after_json == {"status": "PENDING_APPROVAL"}

    # 非法跳转：PENDING_APPROVAL → EXPIRED 不允许（EXPIRED 仅由 ACTIVE 经时间触发）
    with pytest.raises(DomainError) as excinfo:
        svc.transition(
            entity=contract,
            domain=DOMAIN_CONTRACT,
            to_state="EXPIRED",
            actor_id=t05_manager.id,
            reason="想跳到 EXPIRED",
        )
    assert excinfo.value.code == "STATE_TRANSITION_INVALID"
    # 实体状态未变
    assert contract.status == "PENDING_APPROVAL"


def test_q025_legal_targets_for_each_domain() -> None:
    """Q025 补强：每个域至少 1 条合法迁移 + 非法跳转示例。"""
    # 合同
    assert "PENDING_APPROVAL" in legal_targets(DOMAIN_CONTRACT, "DRAFT")
    assert "ACTIVE" not in legal_targets(DOMAIN_CONTRACT, "DRAFT")
    # 冰柜
    assert "DEPLOYED" in legal_targets(DOMAIN_FREEZER, "IN_TRANSIT")
    # DEPLOYED→SCRAPPED 已在 4.5 文本中允许（报废），仅作正向断言
    assert "SCRAPPED" in legal_targets(DOMAIN_FREEZER, "DEPLOYED")
    # 整改：OPEN→IN_PROGRESS 是常规路径
    from app.services.state_machine import DOMAIN_SUPERVISION_ISSUE

    assert "IN_PROGRESS" in legal_targets(DOMAIN_SUPERVISION_ISSUE, "OPEN")
    # 工资：DRAFT→REVIEWED 合法
    from app.services.state_machine import DOMAIN_PAYROLL

    assert "REVIEWED" in legal_targets(DOMAIN_PAYROLL, "DRAFT")
    # 不允许 PAID→REVIEWED
    assert "REVIEWED" not in legal_targets(DOMAIN_PAYROLL, "PAID")


# ===== Q026 任务幂等 =====


def test_q026_task_idempotent(session: Session, t05_manager: User) -> None:
    """Q026：同 (object_type, object_id, task_kind) 未关闭任务不重复创建。"""
    svc = TaskService(session)
    obj_id = uuid4()
    first = svc.assign(
        title="合同到期续签评估",
        assignee_id=t05_manager.id,
        task_kind=TASK_KIND_CONTRACT_EXPIRING_RENEWAL_EVAL,
        related_object_type="CONTRACT",
        related_object_id=obj_id,
    )
    session.flush()

    with pytest.raises(ConflictError) as excinfo:
        svc.assign(
            title="合同到期续签评估-重复",
            assignee_id=t05_manager.id,
            task_kind=TASK_KIND_CONTRACT_EXPIRING_RENEWAL_EVAL,
            related_object_type="CONTRACT",
            related_object_id=obj_id,
        )
    assert excinfo.value.code == "TASK_IDEMPOTENT_DUPLICATE"
    # 提示里带已存在 task id
    assert excinfo.value.details["task_id"] == str(first.id)

    # 关闭后允许重新创建
    svc.close_by_document(
        task_id=first.id,
        actor_id=t05_manager.id,
        document_object_type="CONTRACT",
        document_object_id=obj_id,
        outcome="DONE",
    )
    session.flush()
    second = svc.assign(
        title="合同到期续签评估-重新",
        assignee_id=t05_manager.id,
        task_kind=TASK_KIND_CONTRACT_EXPIRING_RENEWAL_EVAL,
        related_object_type="CONTRACT",
        related_object_id=obj_id,
    )
    assert second.id != first.id
    assert second.status == "OPEN"


def test_q026_idempotent_partial_unique_index_blocks_direct_insert(
    session: Session, t05_manager: User
) -> None:
    """Q026 兜底：部分唯一索引本身在数据库层阻止重复行。"""
    svc = TaskService(session)
    obj_id = uuid4()
    svc.assign(
        title="撤柜-1",
        assignee_id=t05_manager.id,
        task_kind=TASK_KIND_FREEZER_RECALL,
        related_object_type="FREEZER",
        related_object_id=obj_id,
    )
    session.flush()
    # 直接 INSERT 一条同样的 (object, kind) OPEN 任务 → 期望 IntegrityError
    bad = TaskItem(
        title="撤柜-2",
        assignee_id=t05_manager.id,
        task_kind=TASK_KIND_FREEZER_RECALL,
        related_object_type="FREEZER",
        related_object_id=obj_id,
        status="OPEN",
    )
    session.add(bad)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()

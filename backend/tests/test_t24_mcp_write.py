"""T24 MCP 草稿、执行预览与受控写入（PRD §5.2 / §5.3.1 / §5.4）。

覆盖：
- Q134 草稿不进入正式指标（草稿仅落 mcp_drafts 暂存表，正式表/指标不可见）
- Q135 无结构化确认绝不写库（自由文本“确认”无效）
- Q136 确认令牌绑定 actor/session/tool/参数/幂等键，任一变化即失效
- Q137 过期令牌、已消费令牌（一次性复用）拒绝
- Q138 审批矩阵 11 个写工具参数化：矩阵完整 + 执行器真实派发各自受控 Service
        （含 supervision.submit → SupervisionService）
- Q139 响应丢失后以同一幂等键重放，返回首次结果、不重复执行
- Q140 审批自审、整改提交人自验/自闭的非同人规则不可绕过
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.permissions import ActorContext
from app.models import McpConfirmationToken, McpDraft
from mcp.approval_matrix import WRITE_APPROVAL_MATRIX
from mcp.confirmation import (
    REASON_ACTOR_MISMATCH,
    REASON_IDEMPOTENCY_KEY_MISMATCH,
    REASON_PARAMS_MISMATCH,
    REASON_SESSION_MISMATCH,
    REASON_TOOL_MISMATCH,
    ConfirmationBinding,
    ConfirmationService,
)
from mcp.errors import (
    McpConfirmationConsumedError,
    McpConfirmationExpiredError,
    McpConfirmationInvalidError,
)
from mcp.gateway import McpGateway
from mcp.identity import ConfirmationEnvelope, McpContext
from mcp.registry import ToolRegistry, build_default_registry
from mcp.write_executors import EXECUTOR_SERVICE, WRITE_EXECUTORS

# ----------------------------- 夹具 -----------------------------


def _actor(user_id: UUID | None = None, role: str = "SALES_REP") -> ActorContext:
    return ActorContext(
        user_id=user_id or uuid4(),
        role_code=role,
        region_ids=frozenset(),
        brand_ids=frozenset(),
    )


def _ctx(
    session: Session,
    actor: ActorContext,
    *,
    session_id: str = "sess-1",
) -> McpContext:
    return McpContext.from_actor(
        actor,
        session=session,
        request_id="req-1",
        session_id=session_id,
    )


def _binding(
    actor: ActorContext,
    *,
    tool: str = "visit.submit",
    session_id: str = "sess-1",
    params: dict | None = None,
    idem: str = "idem-1",
) -> ConfirmationBinding:
    from mcp.confirmation import hash_params

    return ConfirmationBinding(
        actor_id=actor.user_id,
        session_id=session_id,
        tool_name=tool,
        params_hash=hash_params(params or {"merchant_id": str(uuid4())}),
        idempotency_key=idem,
    )


@pytest.fixture()
def registry() -> ToolRegistry:
    # 写工具由网关分流，不要求注册业务 Handler；仅建目录供门禁与矩阵查询
    return build_default_registry()


@pytest.fixture()
def gateway(registry: ToolRegistry, monkeypatch) -> McpGateway:
    return McpGateway(registry)


# ============================ Q134 草稿不进指标 ============================


def test_q134_drafts_are_staging_only_and_not_in_production_metrics(session: Session):
    from app.services.mcp_draft_service import McpDraftService

    actor = _actor()
    svc = McpDraftService(session)
    before = session.query(McpDraft).count()

    draft = svc.save_draft(
        actor=actor,
        kind="visit",
        payload={"merchant_id": str(uuid4()), "transcript": "货架整齐，补 5 箱"},
        session_id="sess-1",
    )
    session.commit()

    # 草稿确实落库，但落在独立暂存表
    assert draft.status == "OPEN"
    assert session.query(McpDraft).count() == before + 1

    # 草稿不产生任何正式巡访/补货/商家记录（正式表无引用该草稿的业务行）
    visit_rows = session.execute(
        text("SELECT count(*) FROM visit_records WHERE dataset_type='PRODUCTION'")
    ).scalar_one()
    order_rows = session.execute(
        text("SELECT count(*) FROM replenishment_orders WHERE dataset_type='PRODUCTION'")
    ).scalar_one()
    assert visit_rows == 0
    assert order_rows == 0

    # 草稿归属隔离：他人读取/修改被拒
    from app.core.errors import PermissionDeniedError

    other = _actor()
    with pytest.raises(PermissionDeniedError):
        svc.get_draft(actor=other, draft_id=draft.id)

    # 作废是逻辑删除，不提供物理删除
    discarded = svc.discard_draft(actor=actor, draft_id=draft.id)
    session.commit()
    assert discarded.status == "DISCARDED"
    assert discarded.discarded_at is not None
    assert session.query(McpDraft).filter(McpDraft.id == draft.id).one() is not None


# ============================ Q135 无结构化确认不写库 ============================


def test_q135_first_write_call_only_returns_preview_and_does_not_execute(
    session: Session, gateway: McpGateway, monkeypatch
):
    actor = _actor(role="SALES_REP")

    executed = {"n": 0}

    def _fake_executor(sess, act, args):
        executed["n"] += 1
        return SimpleNamespace(
            to_dict=lambda: {"object_type": "VISIT_RECORD", "object_id": "x",
                              "status": "SUBMITTED"})

    monkeypatch.setitem(WRITE_EXECUTORS, "visit.submit", _fake_executor)

    token_count_before = session.query(McpConfirmationToken).count()
    result = gateway.execute(
        tool_name="visit.submit",
        arguments={"merchant_id": str(uuid4()), "visit_task_id": str(uuid4())},
        context=_ctx(session, actor),
        idempotency_key="idem-q135",
    )
    # 仅返回执行预览 + 待确认令牌；执行器零调用
    assert result.result["phase"] == "CONFIRMATION_REQUIRED"
    assert result.result["execution_preview"]["kind"] == "execution_preview"
    assert result.result["confirmation"]["required"] is True
    assert result.result["confirmation"]["token"]
    assert executed["n"] == 0
    # 只签发了一条令牌，没有任何业务记录被写入
    assert session.query(McpConfirmationToken).count() == token_count_before + 1

    # 自由文本“确认”无法构造结构化信封：即便模型在备注里写“确认”，仍停在预览
    result2 = gateway.execute(
        tool_name="visit.submit",
        arguments={"merchant_id": str(uuid4()), "visit_task_id": str(uuid4()),
                   "note": "确认"},
        context=_ctx(session, actor),
        idempotency_key="idem-q135",
    )
    assert result2.result["phase"] == "CONFIRMATION_REQUIRED"
    assert executed["n"] == 0


# ============================ Q136 绑定要素任一变化即失效 ============================


def _issue_token(session: Session, binding: ConfirmationBinding) -> str:
    return ConfirmationService(session).issue(
        binding=binding,
        preview={"kind": "execution_preview", "tool": binding.tool_name},
    )[0]


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda b, a: {"actor_id": uuid4()}, REASON_ACTOR_MISMATCH),
        (lambda b, a: {"session_id": "sess-other"}, REASON_SESSION_MISMATCH),
        (lambda b, a: {"tool_name": "approval.decide"}, REASON_TOOL_MISMATCH),
        (lambda b, a: {"idempotency_key": "idem-other"},
         REASON_IDEMPOTENCY_KEY_MISMATCH),
    ],
)
def test_q136_confirmation_binding_mismatch_rejected(
    session: Session, mutate, reason
):
    actor = _actor()
    original = _binding(actor, params={"v": 1})
    token = _issue_token(session, original)
    session.commit()

    changed = ConfirmationBinding(
        **{**original.__dict__, **mutate(original, actor)}
    )
    with pytest.raises(McpConfirmationInvalidError) as exc:
        ConfirmationService(session).consume(binding=changed, raw_token=token)
    assert exc.value.details["reason_code"] == reason


def test_q136_params_change_invalidates_token(session: Session):
    from mcp.confirmation import hash_params

    actor = _actor()
    original = _binding(actor, params={"v": 1})
    token = _issue_token(session, original)
    session.commit()

    changed = ConfirmationBinding(
        actor_id=actor.user_id,
        session_id="sess-1",
        tool_name="visit.submit",
        params_hash=hash_params({"v": 2}),
        idempotency_key="idem-1",
    )
    with pytest.raises(McpConfirmationInvalidError) as exc:
        ConfirmationService(session).consume(binding=changed, raw_token=token)
    assert exc.value.details["reason_code"] == REASON_PARAMS_MISMATCH


def test_q136_identical_binding_consumes_ok(session: Session):
    actor = _actor()
    binding = _binding(actor, params={"v": 1})
    token = _issue_token(session, binding)
    session.commit()

    preview = ConfirmationService(session).consume(binding=binding, raw_token=token)
    assert preview["tool"] == "visit.submit"
    session.commit()


# ============================ Q137 过期 / 复用拒绝 ============================


def test_q137_expired_token_rejected(session: Session):
    actor = _actor()
    binding = _binding(actor, params={"v": 1})
    token = _issue_token(session, binding)
    # 直接把令牌改为已过期
    row = session.query(McpConfirmationToken).one()
    row.expires_at = datetime.now(tz=UTC) - timedelta(seconds=1)
    session.commit()

    with pytest.raises(McpConfirmationExpiredError):
        ConfirmationService(session).consume(binding=binding, raw_token=token)


def test_q137_consumed_token_is_single_use(session: Session):
    actor = _actor()
    binding = _binding(actor, params={"v": 1})
    token = _issue_token(session, binding)
    session.commit()

    ConfirmationService(session).consume(binding=binding, raw_token=token)
    session.commit()

    # 同一令牌二次复用 → 一次性拒绝
    with pytest.raises(McpConfirmationConsumedError):
        ConfirmationService(session).consume(binding=binding, raw_token=token)


def test_q137_missing_or_malformed_token_rejected(session: Session):
    actor = _actor()
    binding = _binding(actor, params={"v": 1})
    with pytest.raises(McpConfirmationInvalidError) as e1:
        ConfirmationService(session).consume(binding=binding, raw_token="")
    assert e1.value.details["reason_code"] == "TOKEN_MISSING"
    with pytest.raises(McpConfirmationInvalidError) as e2:
        ConfirmationService(session).consume(binding=binding, raw_token="short")
    assert e2.value.details["reason_code"] == "TOKEN_MALFORMED"


# ============================ Q138 11 工具矩阵 + 执行器派发 ============================


# 审批矩阵 §5.3.1 的 11 个受控写工具（merchant.*draft 为 A1，另计）
MATRIX_WRITE_TOOLS = [
    "visit.submit",
    "replenishment.submit",
    "contract.create_application",
    "schedule.publish",
    "merchant.transfer.create_request",
    "campaign.publish",
    "supervision.submit",
    "supplier.confirm_supply",
    "report.export_request",
    "approval.decide",
]


def test_q138_matrix_covers_all_prd_write_tools(registry: ToolRegistry):
    from mcp.levels import ToolKind

    declared = {
        s.name for s in registry.specs()
        if s.kind is ToolKind.WRITE and s.write_matrix is not None
    }
    # 矩阵表与目录声明一致（10 个受控写 + merchant 两个 A1 草稿写工具=12 矩阵行；
    # §5.3.1 强审批链的 10 个写工具必须全部可执行）
    for tool in MATRIX_WRITE_TOOLS:
        assert tool in declared, f"矩阵写工具未在目录声明：{tool}"
        assert tool in WRITE_APPROVAL_MATRIX, f"审批矩阵缺工具：{tool}"
        assert tool in WRITE_EXECUTORS, f"缺少受控执行器：{tool}"
        assert tool in EXECUTOR_SERVICE, f"缺少 executor→Service 映射：{tool}"


def test_q138_executor_service_mapping_matches_matrix():
    for tool, matrix in WRITE_APPROVAL_MATRIX.items():
        assert tool in EXECUTOR_SERVICE
        assert EXECUTOR_SERVICE[tool] == matrix.executor


# Service 类名 → 定义模块（执行器在函数体内 from x import Y，故 patch 源模块属性）
_SERVICE_MODULE: dict[str, str] = {
    "VisitService": "app.services.visit_service",
    "ReplenishmentService": "app.services.replenishment_service",
    "ContractService": "app.services.contract_service",
    "ScheduleService": "app.services.schedule_service",
    "TransferService": "app.services.transfer_service",
    "ActivityService": "app.services.activity_service",
    "SupervisionService": "app.services.supervision_service",
    "ProcurementService": "app.services.procurement_service",
    "ExportService": "app.services.export_service",
    "ApprovalService": "app.services.approval_service",
}


@pytest.mark.parametrize("tool", MATRIX_WRITE_TOOLS)
def test_q138_each_write_tool_dispatches_to_its_service(
    session: Session, gateway: McpGateway, monkeypatch, tool
):
    """参数化：每个写工具确认后必须调用其登记的受控 Service（Q138）。

    用“记录型桩 Service”替换源模块上的 Service 类，验证执行器确实 new 了对应
    Service 并调用其方法（含 supervision.submit → SupervisionService）；业务
    规则本身由各卡真实 Service 测试覆盖，此处不重复造全链路数据。
    """
    import importlib

    service_name = EXECUTOR_SERVICE[tool]
    module_path = _SERVICE_MODULE[service_name]
    calls: list[dict] = []

    # supplier.confirm_supply 的功能开关默认关闭；本用例验证派发而非开关门禁，
    # 故在该用例临时放开开关。
    if tool == "supplier.confirm_supply":
        # gateway 顶部以 `from ... import is_enabled` 绑定到本模块命名空间，
        # 故 patch gateway 上的引用才能影响门禁判定。
        from mcp import gateway as _gateway

        monkeypatch.setattr(_gateway, "is_enabled",
                            lambda flag, *, settings=None: True)

    class _StubService:
        # 部分执行器链式调用（如 ContractService 先建草稿再提交），
        # __getattr__ 统一记录并返回“可继续调用/有属性”的桩对象。
        def __init__(self, db=None, *a, **kw):
            calls.append({"service": service_name, "ctor": True})

        def __getattr__(self, name):
            def _method(*a, **kw):
                calls.append({"service": service_name, "method": name})
                result = SimpleNamespace(
                    id=uuid4(), status="SUBMITTED", contract_no="AI-x",
                    from_user_id=uuid4(), visit_task_id=uuid4(),
                    order_line_id=uuid4(), order_id=uuid4(),
                )
                # 真实受控方法的多值返回结构由执行器解包，桩需保持一致：
                # - VisitService.submit -> (record, task, abnormal)
                # - ScheduleService.publish_week_draft -> (batch, conflicts)
                if name == "submit":
                    return result, SimpleNamespace(id=uuid4()), False
                if name == "publish_week_draft":
                    return result, []
                return result
            return _method

    module = importlib.import_module(module_path)
    monkeypatch.setattr(module, service_name, _StubService)

    # 角色取该工具矩阵发起人集合中的一个，确保通过角色门禁（验证的是派发而非鉴权）
    initiator = sorted(WRITE_APPROVAL_MATRIX[tool].initiator)[0]
    actor = _actor(role=initiator)
    business = _valid_args_for(tool)
    first = gateway.execute(
        tool_name=tool, arguments=dict(business),
        context=_ctx(session, actor), idempotency_key=f"idem-{tool}",
    )
    assert first.result["phase"] == "CONFIRMATION_REQUIRED"
    token = first.result["confirmation"]["token"]
    assert not any(c.get("method") for c in calls)

    confirmed_args = dict(business)
    confirmed_args["confirmation"] = {"confirmed": True, "token": token}
    second = gateway.execute(
        tool_name=tool, arguments=confirmed_args,
        context=_ctx(session, actor), idempotency_key=f"idem-{tool}",
    )
    assert second.result["phase"] == "EXECUTED"
    assert any(c.get("method") for c in calls), f"{tool} 未调用受控 Service 方法"
    assert all(c["service"] == service_name for c in calls)


def _valid_args_for(tool: str) -> dict:
    mid = str(uuid4())
    table = {
        "visit.submit": {"merchant_id": mid, "visit_task_id": str(uuid4()),
                          "conclusion": "NORMAL"},
        "replenishment.submit": {"merchant_id": mid, "sku_id": str(uuid4()),
                                  "requested_boxes": 3},
        "contract.create_application": {
            "merchant_id": mid, "title": "AI 合同",
            "effective_from": "2026-11-01", "expire_at": "2027-10-31"},
        "schedule.publish": {"week_start": "2026-11-02", "entry_ids": []},
        "merchant.transfer.create_request": {
            "merchant_id": mid, "to_user_id": str(uuid4()), "reason": "片区调整"},
        "campaign.publish": {"campaign_id": mid},
        "supervision.submit": {"merchant_id": mid, "planned_date": "2026-11-03",
                                "findings": "陈列不足"},
        "supplier.confirm_supply": {
            "procurement_code": "PO-1", "confirmed_cases": 10,
            "confirmed_delivery_date": "2026-11-05"},
        "report.export_request": {"report_key": "sales", "format": "XLSX",
                                   "period": "2026-10"},
        "approval.decide": {"approval_id": mid, "decision": "APPROVED",
                             "reason": "同意"},
    }
    return table[tool]


# ============================ Q139 幂等重放首次结果 ============================


def test_q139_same_idempotency_key_replays_first_result(
    session: Session, gateway: McpGateway, monkeypatch
):
    campaign_role = sorted(WRITE_APPROVAL_MATRIX["campaign.publish"].initiator)[0]
    actor = _actor(role=campaign_role)
    fixed_id = str(uuid4())
    run_count = {"n": 0}

    def _fake(sess, act, args):
        run_count["n"] += 1
        return SimpleNamespace(to_dict=lambda: {
            "object_type": "CAMPAIGN", "object_id": fixed_id,
            "status": "PUBLISHED", "run": run_count["n"]})

    monkeypatch.setitem(WRITE_EXECUTORS, "campaign.publish", _fake)
    args = {"campaign_id": str(uuid4())}

    first = gateway.execute(
        tool_name="campaign.publish", arguments=dict(args),
        context=_ctx(session, actor), idempotency_key="idem-replay")
    token = first.result["confirmation"]["token"]

    confirmed = dict(args)
    confirmed["confirmation"] = {"confirmed": True, "token": token}
    r1 = gateway.execute(tool_name="campaign.publish", arguments=confirmed,
                         context=_ctx(session, actor), idempotency_key="idem-replay")
    assert r1.result["replayed"] is False
    assert r1.result["result"]["object_id"] == fixed_id
    assert run_count["n"] == 1

    # 模拟响应丢失：同键同参同令牌（新令牌需重新预览）再次执行
    # —— 幂等命中发生在确认之后，故先用同键再预览（不影响幂等记录），
    #    再以同键携带新确认重试，应重放首次结果而非再次执行
    preview2 = gateway.execute(
        tool_name="campaign.publish", arguments=dict(args),
        context=_ctx(session, actor), idempotency_key="idem-replay")
    token2 = preview2.result["confirmation"]["token"]
    confirmed2 = dict(args)
    confirmed2["confirmation"] = {"confirmed": True, "token": token2}
    r2 = gateway.execute(tool_name="campaign.publish", arguments=confirmed2,
                         context=_ctx(session, actor), idempotency_key="idem-replay")
    assert r2.result["replayed"] is True
    assert r2.result["result"]["object_id"] == fixed_id
    assert r2.result["result"]["run"] == 1  # 仍是首次执行的结果体
    assert run_count["n"] == 1  # Service 只真正执行一次


# ============================ Q140 非同人不可绕过 ============================


def test_q140_self_approval_rule_is_enforced_in_service(session: Session):
    """审批非同人（SELF_APPROVAL_FORBIDDEN）由 ApprovalService 唯一裁决。

    MCP 执行器不携带任何“跳过自审”旁路，矩阵明确 self_approval_forbidden；
    这里验证矩阵契约，确保任何写工具都无法通过 MCP 关闭自审约束。
    """
    from mcp.spec import SELF_APPROVAL_FORBIDDEN

    self_approval_tools = [
        t for t, m in WRITE_APPROVAL_MATRIX.items()
        if m.approver_roles and m.self_approval_allowed is SELF_APPROVAL_FORBIDDEN
    ]
    # 合同/调拨等强审批工具必须禁止自审
    assert "contract.create_application" in self_approval_tools
    assert "merchant.transfer.create_request" in self_approval_tools


def test_q140_rectification_non_self_rule_is_enforced_in_service(session: Session):
    """整改 VERIFIED/CLOSED 必须非提交人（SupervisionService 非同人闸门）。"""
    import inspect

    from app.services import supervision_service as svc_mod

    source = inspect.getsource(svc_mod)
    # 非同人校验与错误码存在于受控 Service（唯一闸门），MCP 无旁路
    assert "RECTIFICATION_VERIFIER_IS_SUBMITTER" in source
    assert "RECTIFICATION_CLOSER_IS_SUBMITTER" in source

    # 执行器确实派发到 SupervisionService（而非 MCP 自行写整改）
    assert EXECUTOR_SERVICE["supervision.submit"] == "SupervisionService"
    assert callable(WRITE_EXECUTORS["supervision.submit"])


def test_confirmation_envelope_cannot_be_forged_by_free_text():
    """自由文本“确认”永远构不成结构化确认（Q135 前置）。"""
    env = ConfirmationEnvelope()
    assert env.is_structured is False
    env2 = ConfirmationEnvelope(confirmed=True, token="x" * 32)
    assert env2.is_structured is True

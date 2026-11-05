"""MCP 写工具两阶段受控编排（T24 / PRD §5.2–§5.4）。

一次写操作的完整时序：

1. **门禁**（网关已完成）：白名单 / 开关 / 角色 / PRODUCTION / 参数净化 / Schema。
2. **首次调用（无有效确认令牌）**：
   - 不调用任何业务写 Service，只构造 `execution_preview`（对象/字段/前后状态/
     冲突/审批链/风险），并由 `ConfirmationService` 签发**一次性**结构化令牌；
   - 预览令牌与 (actor, session, tool, params_hash, idempotency_key, 有效期) 绑定；
   - 自由文本“确认”无法构造确认信封，因此永远停在本阶段，**绝不写库**（Q135）。
3. **二次调用（结构化确认 UI 携带令牌）**：
   - `ConfirmationService.consume` 逐要素校验绑定、有效期、一次性，任一不符即拒绝
     （Q136 actor/session/tool/参数/幂等键；Q137 过期/复用）；
   - 通过后经 `IdempotencyService.run` 调用 `WRITE_EXECUTORS` 中的受控执行器；
   - 执行器只调用对应受控 Service，权限/冲突/状态机/非同人仍由 Service 裁决；
   - 成功响应按幂等键落库；响应丢失后同键同参重试直接重放首次结果（Q139）。

事务：本模块是唯一提交点（对齐既有路由层 commit 模式）；异常即回滚，令牌消费
与业务写入同生共死，不产生“已消费但未执行”。
"""

from __future__ import annotations

from typing import Any

from app.services.idempotency_service import IdempotencyService
from mcp.confirmation import (
    ConfirmationBinding,
    ConfirmationService,
    build_preview,
    hash_params,
)
from mcp.identity import ConfirmationEnvelope, McpContext
from mcp.spec import (
    CONFIRMER_CURRENT_ACTOR,
    CONFIRMER_CURRENT_APPROVER,
    CONFIRMER_CURRENT_SUPPLIER_ACCOUNT,
    SELF_APPROVAL_FORBIDDEN,
    ToolSpec,
)
from mcp.write_executors import EXECUTOR_SERVICE, get_executor

IDEMPOTENCY_SCOPE = "mcp.write"

_PREVIEW_PHASE = "CONFIRMATION_REQUIRED"
_EXECUTED_PHASE = "EXECUTED"


def _idempotency_key(spec: ToolSpec, key: str) -> str:
    # 矩阵写工具强制幂等键（网关已门禁）；草稿工具给稳定回退键
    return key or f"mcp:{spec.name}:draft"


def _approval_chain(spec: ToolSpec) -> dict[str, Any]:
    matrix = spec.write_matrix
    if matrix is None:
        return {}
    approvers = sorted(matrix.approver_roles)
    chain: dict[str, Any] = {
        "initiator_roles": sorted(matrix.initiator),
        "confirmer": matrix.confirmer,
        "approver_roles": approvers,
        "requires_approval": bool(approvers),
        "self_approval_allowed": matrix.self_approval_allowed
        is not SELF_APPROVAL_FORBIDDEN,
    }
    if matrix.confirmer == CONFIRMER_CURRENT_SUPPLIER_ACCOUNT:
        chain["confirmer_note"] = "必须由该厂商账号在其授权范围内确认"
    elif matrix.confirmer == CONFIRMER_CURRENT_APPROVER:
        chain["confirmer_note"] = "由具备审批角色且非申请人的审批人确认"
    elif matrix.confirmer == CONFIRMER_CURRENT_ACTOR:
        chain["confirmer_note"] = "发起人在结构化确认界面核对后确认（不等于业务审批）"
    if approvers and matrix.self_approval_allowed is SELF_APPROVAL_FORBIDDEN:
        chain["non_self_rule"] = "审批人必须不是原业务申请人"
    return chain


def _build_execution_preview(
    spec: ToolSpec,
    context: McpContext,
    business: dict[str, Any],
    idem_key: str,
) -> dict[str, Any]:
    matrix = spec.write_matrix
    executor_name = EXECUTOR_SERVICE.get(spec.name, matrix.executor if matrix else "")
    final_state = tuple(matrix.final_state) if matrix else ()

    # before/after 状态为声明式口径（来自审批矩阵），真实裁决仍在 Service
    after_state: dict[str, Any] = {"expected_status": list(final_state)}
    if spec.name == "approval.decide":
        after_state["decision"] = business.get("decision")
    if spec.name == "schedule.publish":
        after_state["conflict_policy"] = "冲突即整批拒绝（全有或全无）"

    preview = build_preview(
        tool_name=spec.name,
        title=spec.description,
        arguments=business,
        before_state={"current_status": "（执行前现状由受控 Service 在执行时复核）"},
        after_state=after_state,
        conflicts=[],
        approval_chain=_approval_chain(spec),
        risks=_preview_risks(spec),
        executor=executor_name,
        final_state=final_state,
        extra={
            "idempotency_key": idem_key,
            "risk_level": spec.risk_level.value,
            "session_bound": True,
        },
    )
    return preview


def _preview_risks(spec: ToolSpec) -> list[dict[str, Any]]:
    risks: list[dict[str, Any]] = []
    matrix = spec.write_matrix
    if matrix is None:
        return risks
    if matrix.self_approval_allowed is SELF_APPROVAL_FORBIDDEN:
        risks.append({
            "code": "NON_SELF_APPROVAL",
            "message": "存在审批节点且禁止自审：审批人必须不是原业务申请人",
        })
    if spec.name == "supervision.submit":
        risks.append({
            "code": "RECTIFICATION_NON_SELF",
            "message": "如生成整改，整改验证/关闭必须由非提交人完成，AI 不可绕过",
        })
    if spec.name == "report.export_request":
        risks.append({
            "code": "SENSITIVE_EXPORT_APPROVAL",
            "message": "敏感报表导出在运行时转入非发起人审批分支",
        })
    return risks


def _binding(
    spec: ToolSpec, context: McpContext, business: dict[str, Any], idem_key: str
) -> ConfirmationBinding:
    return ConfirmationBinding(
        actor_id=context.user_id,
        session_id=context.session_id or "",
        tool_name=spec.name,
        params_hash=hash_params(business),
        idempotency_key=idem_key,
    )


def execute_write(
    *,
    spec: ToolSpec,
    context: McpContext,
    business: dict[str, Any],
    confirmation: ConfirmationEnvelope,
    idempotency_key: str,
) -> dict[str, Any]:
    """写工具两阶段总入口；返回可直接回给调用方的 result（含 phase 标记）。"""
    if context.session is None:
        from mcp.errors import McpSessionUnavailableError

        raise McpSessionUnavailableError(spec.name)

    idem_key = _idempotency_key(spec, (idempotency_key or "").strip())
    session = context.session
    binding = _binding(spec, context, business, idem_key)
    confirm_svc = ConfirmationService(session)

    # ---------- 阶段一：未携带结构化确认 → 只出预览 + 签发令牌 ----------
    if not confirmation.is_structured:
        preview = _build_execution_preview(spec, context, business, idem_key)
        raw_token, expires_at = confirm_svc.issue(
            binding=binding, preview=preview,
            request_id=context.request_id,
        )
        # 预览仅落令牌（不触业务表），提交以持久化令牌；后续确认才能查到
        session.commit()
        return {
            "phase": _PREVIEW_PHASE,
            "execution_preview": preview,
            "confirmation": {
                "required": True,
                "instruction": "请在结构化确认界面核对执行预览后确认；"
                               "自由文本“确认”无效",
                "confirm_field": "confirmation",
                "token_expires_at": expires_at.isoformat(),
                # 令牌只在本次响应下发一次，库内不存明文
                "token": raw_token,
            },
        }

    # ---------- 阶段二：结构化确认 → 校验并消费令牌 ----------
    confirm_svc.consume(binding=binding, raw_token=confirmation.token)

    # ---------- 幂等受控执行（Q139 首次结果重放） ----------
    executor = get_executor(spec.name)
    executor_args = dict(business)
    executor_args["__session_id"] = context.session_id or ""
    executor_args["__idempotency_key"] = idem_key

    def _do_execute() -> dict[str, Any]:
        result = executor(session, context.actor, executor_args)
        return result.to_dict()

    idem = IdempotencyService(session)
    outcome = idem.run(
        scope=IDEMPOTENCY_SCOPE,
        key=f"{spec.name}:{idem_key}",
        params=business,
        actor_id=context.user_id,
        fn=_do_execute,
    )
    session.commit()
    return {
        "phase": _EXECUTED_PHASE,
        "replayed": bool(outcome["replayed"]),
        "result": outcome["response"],
        "idempotency_key": idem_key,
    }

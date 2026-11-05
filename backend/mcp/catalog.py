"""MCP 工具目录（T22 / PRD §5.3「允许调用的 MCP 工具目录」）。

只做**声明**：工具名、作用、默认允许角色、风险等级、输入/输出 Schema、
数据范围要求与字段权限；执行器（Handler）由 T23/T24 适配器绑定，
本模块不导入任何 Service / Repository。

§5.3.1 矩阵内的工具，其 `allowed_roles` **必须**等于矩阵 `initiator`，
风险等级必须等于 `MATRIX_TOOL_RISK_LEVELS`（注册期强校验）。
"""

from __future__ import annotations

from typing import Any

from app.core.permissions import SENSITIVE_FIELDS_SUPPLIER
from mcp.approval_matrix import (
    APPROVER_NODE_ROLES,
    MATRIX_TOOL_NAMES,
    MATRIX_TOOL_RISK_LEVELS,
    R_ADMIN,
    R_FINANCE,
    R_MANAGER,
    R_SALES,
    R_SUPERVISOR,
    R_SUPPLIER,
    WRITE_APPROVAL_MATRIX,
    matrix_tool_kind,
)
from mcp.levels import ConfirmLevel, ToolKind
from mcp.spec import FieldPolicy, ToolSpec

INTERNAL_ROLES: frozenset[str] = frozenset({R_SALES, R_MANAGER, R_SUPERVISOR, R_FINANCE, R_ADMIN})

_SUPPLIER_REDACTED = FieldPolicy(hidden_fields=SENSITIVE_FIELDS_SUPPLIER)

_STATUS_ENUM = ["ACTIVE", "INACTIVE", "PENDING"]
_UUID = {"type": "string", "minLength": 8}
_DATE = {"type": "string", "minLength": 10, "maxLength": 10}

# 结构化确认信封（T24 / PRD §5.4）：只有结构化确认 UI 可携带；自由文本“确认”无效。
# 非保留权限字段，只承载「是否确认 + 服务端签发的一次性令牌」，模型无法自行授权。
CONFIRMATION_FIELD = "confirmation"
_CONFIRMATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "confirmed": {"type": "boolean"},
        "token": {"type": "string", "minLength": 16, "maxLength": 256},
    },
    "required": ["confirmed", "token"],
    "additionalProperties": False,
}


def _obj(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    """对象入参 Schema：不接受未声明字段（模型只能传业务参数）。"""
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _write_obj(
    properties: dict[str, Any], required: tuple[str, ...] = ()
) -> dict[str, Any]:
    """写工具入参 Schema：业务参数 + 可选结构化确认信封（T24 两阶段）。"""
    merged = dict(properties)
    merged[CONFIRMATION_FIELD] = _CONFIRMATION_SCHEMA
    return _obj(merged, required)


def _list_out() -> dict[str, Any]:
    return {"type": "object", "properties": {"items": {"type": "array"}}}


def _record_out() -> dict[str, Any]:
    return {"type": "object", "properties": {"record": {"type": "object"}}}


def _ack_out() -> dict[str, Any]:
    return {"type": "object", "properties": {"status": {"type": "string"}}}


# ===== 只读工具（A0）=====

_READ_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="merchant.search",
        title="查询授权范围商家",
        description="按关键词/状态查询当前用户行级范围内的商家摘要（厂商仅脱敏汇总）",
        kind=ToolKind.READ,
        risk_level=ConfirmLevel.A0,
        allowed_roles=frozenset({*INTERNAL_ROLES, R_SUPPLIER}),
        input_schema=_obj(
            {"keyword": {"type": "string", "maxLength": 64},
             "status": {"type": "string", "enum": _STATUS_ENUM},
             "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
        ),
        output_schema=_list_out(),
        field_policy=_SUPPLIER_REDACTED,
    ),
    ToolSpec(
        name="merchant.get",
        title="商家档案摘要",
        description="读取单个商家档案摘要（须在当前用户行级范围内）",
        kind=ToolKind.READ,
        risk_level=ConfirmLevel.A0,
        allowed_roles=frozenset({*INTERNAL_ROLES, R_SUPPLIER}),
        input_schema=_obj({"merchant_id": _UUID}, ("merchant_id",)),
        output_schema=_record_out(),
        field_policy=_SUPPLIER_REDACTED,
    ),
    ToolSpec(
        name="contract.search",
        title="查询合同",
        description="按商家/状态/关键词查询合同及状态（按行级权限裁剪）",
        kind=ToolKind.READ,
        risk_level=ConfirmLevel.A0,
        allowed_roles=INTERNAL_ROLES,
        input_schema=_obj(
            {"merchant_id": _UUID,
             "status": {"type": "string"},
             "keyword": {"type": "string", "maxLength": 64},
             "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
        ),
        output_schema=_list_out(),
    ),
    ToolSpec(
        name="contract.get",
        title="合同详情",
        description="读取单个合同状态与关键条款摘要",
        kind=ToolKind.READ,
        risk_level=ConfirmLevel.A0,
        allowed_roles=INTERNAL_ROLES,
        input_schema=_obj({"contract_id": _UUID}, ("contract_id",)),
        output_schema=_record_out(),
    ),
    ToolSpec(
        name="freezer.search",
        title="查询冰柜",
        description="查询冰柜状态（业务员/经理/财务；厂商按授权）",
        kind=ToolKind.READ,
        risk_level=ConfirmLevel.A0,
        allowed_roles=frozenset({R_SALES, R_MANAGER, R_FINANCE, R_SUPPLIER}),
        input_schema=_obj(
            {"asset_code": {"type": "string", "maxLength": 32},
             "status": {"type": "string", "maxLength": 24},
             "merchant_id": _UUID,
             "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
        ),
        output_schema=_list_out(),
        field_policy=_SUPPLIER_REDACTED,
    ),
    ToolSpec(
        name="freezer.get_history",
        title="冰柜流水",
        description="读取单个冰柜的投放/撤回/维修/押金流水",
        kind=ToolKind.READ,
        risk_level=ConfirmLevel.A0,
        allowed_roles=frozenset({R_SALES, R_MANAGER, R_FINANCE, R_SUPPLIER}),
        input_schema=_obj({"asset_id": _UUID}, ("asset_id",)),
        output_schema=_list_out(),
        field_policy=_SUPPLIER_REDACTED,
    ),
    ToolSpec(
        name="visit.list_due",
        title="待巡访清单",
        description="查询待巡访商家/任务（业务员本人、经理片区、督导范围）",
        kind=ToolKind.READ,
        risk_level=ConfirmLevel.A0,
        allowed_roles=frozenset({R_SALES, R_MANAGER, R_SUPERVISOR}),
        input_schema=_obj(
            {"week_start": _DATE,
             # 可选：按商家名过滤（「新城小卖部的寻访是周几」这类点查询）
             "keyword": {"type": "string", "maxLength": 64}},
        ),
        output_schema=_list_out(),
    ),
    ToolSpec(
        name="visit.get",
        title="巡访详情",
        description="读取历史巡访记录详情与有效性裁决",
        kind=ToolKind.READ,
        risk_level=ConfirmLevel.A0,
        allowed_roles=frozenset({R_SALES, R_MANAGER, R_SUPERVISOR}),
        input_schema=_obj({"visit_id": _UUID}, ("visit_id",)),
        output_schema=_record_out(),
    ),
    ToolSpec(
        name="replenishment.aggregate",
        title="需求汇总",
        description="汇总已确认补货需求与在途/未满足量（只汇总已确认需求）",
        kind=ToolKind.READ,
        risk_level=ConfirmLevel.A0,
        allowed_roles=frozenset({R_MANAGER, R_FINANCE}),
        input_schema=_obj(
            {"period": {"type": "string", "maxLength": 16},
             "merchant_id": _UUID,
             "status": {"type": "string", "maxLength": 24}},
        ),
        output_schema=_list_out(),
    ),
    ToolSpec(
        name="performance.preview",
        title="绩效预估",
        description="查询本人预估或片区绩效明细（经理/财务可查授权范围内成员）",
        kind=ToolKind.READ,
        risk_level=ConfirmLevel.A0,
        allowed_roles=frozenset({R_SALES, R_MANAGER, R_FINANCE}),
        input_schema=_obj(
            {"period": {"type": "string", "maxLength": 16},
             "subject_user_id": _UUID},
        ),
        output_schema=_record_out(),
    ),
    ToolSpec(
        name="finance.profit_report",
        title="损益与差异",
        description="生成损益与差异解释数据（财务全局；经理脱敏）",
        kind=ToolKind.READ,
        risk_level=ConfirmLevel.A0,
        allowed_roles=frozenset({R_FINANCE, R_MANAGER}),
        input_schema=_obj({"period": {"type": "string", "maxLength": 16}}),
        output_schema=_record_out(),
        field_policy=_SUPPLIER_REDACTED,
    ),
    ToolSpec(
        name="risk.list",
        title="风险清单",
        description="查询权限范围内风险清单与处置建议（唯一来源 RiskService）",
        kind=ToolKind.READ,
        risk_level=ConfirmLevel.A0,
        allowed_roles=frozenset({R_SALES, R_MANAGER, R_SUPERVISOR, R_FINANCE}),
        input_schema=_obj(
            {"status": {"type": "string", "enum": ["OPEN", "ACKNOWLEDGED", "CLOSED"]},
             "rule_code": {"type": "string", "maxLength": 8}},
        ),
        output_schema=_list_out(),
    ),
    ToolSpec(
        name="payroll.list",
        title="工资单查询",
        description="查询工资单（财务查数据集内全员，可用 user_name 精确点名单个员工；"
        "其余内部角色仅本人），默认最近 6 个完整月（不含本月）",
        kind=ToolKind.READ,
        risk_level=ConfirmLevel.A0,
        allowed_roles=INTERNAL_ROLES,
        input_schema=_obj(
            {"period": {"type": "string", "maxLength": 16},
             "months": {"type": "integer", "minimum": 1, "maximum": 12},
             "user_name": {"type": "string", "maxLength": 32}},
        ),
        output_schema=_list_out(),
    ),
    ToolSpec(
        name="approval.list",
        title="审批清单",
        description="查询本角色待办/已办审批",
        kind=ToolKind.READ,
        risk_level=ConfirmLevel.A0,
        allowed_roles=APPROVER_NODE_ROLES,
        input_schema=_obj(
            {"status": {"type": "string"},
             "target_object_type": {"type": "string", "maxLength": 32}},
        ),
        output_schema=_list_out(),
    ),
    ToolSpec(
        name="knowledge.search",
        title="制度知识库检索",
        description="检索公司制度知识库（合同/员工/冰柜等公开规范），"
        "返回来源文档编号、小节原文与相似度",
        kind=ToolKind.READ,
        risk_level=ConfirmLevel.A0,
        # 知识库为公司公开制度文档：全员（含厂商）可见
        allowed_roles=frozenset({*INTERNAL_ROLES, R_SUPPLIER}),
        input_schema=_obj(
            {"query": {"type": "string", "minLength": 1, "maxLength": 200},
             "top_k": {"type": "integer", "minimum": 1, "maximum": 10}},
            ("query",),
        ),
        output_schema=_list_out(),
    ),
)


# ===== 草稿工具（A1，不写正式业务表）=====

_DRAFT_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="visit.compose_draft",
        title="巡访草稿",
        description="把口述/表单信息整理为巡访草稿（不写业务表）",
        kind=ToolKind.DRAFT,
        risk_level=ConfirmLevel.A1,
        allowed_roles=frozenset({R_SALES, R_SUPERVISOR}),
        input_schema=_obj(
            {"merchant_id": _UUID, "transcript": {"type": "string", "maxLength": 4000}},
            ("merchant_id",),
        ),
        output_schema=_record_out(),
    ),
    ToolSpec(
        name="replenishment.create_draft",
        title="补货需求草稿",
        description="生成补货需求草稿（提交走 replenishment.submit）",
        kind=ToolKind.DRAFT,
        risk_level=ConfirmLevel.A1,
        allowed_roles=frozenset({R_SALES, R_MANAGER}),
        input_schema=_obj(
            {"merchant_id": _UUID, "sku_id": _UUID,
             "requested_boxes": {"type": "number", "minimum": 0}},
            ("merchant_id", "sku_id", "requested_boxes"),
        ),
        output_schema=_record_out(),
    ),
    ToolSpec(
        name="schedule.suggest",
        title="周排班建议",
        description="生成周排班建议并检测冲突（只返回建议，不发布）",
        kind=ToolKind.DRAFT,
        risk_level=ConfirmLevel.A1,
        allowed_roles=frozenset({R_MANAGER}),
        input_schema=_obj({"week_start": _DATE}, ("week_start",)),
        output_schema=_list_out(),
    ),
    ToolSpec(
        name="campaign.create_draft",
        title="地推活动草稿",
        description="创建地推活动草稿（发布走 campaign.publish）",
        kind=ToolKind.DRAFT,
        risk_level=ConfirmLevel.A1,
        allowed_roles=frozenset({R_MANAGER}),
        input_schema=_obj(
            {"title": {"type": "string", "maxLength": 128},
             "planned_start": _DATE,
             "merchant_ids": {"type": "array"}},
            ("title",),
        ),
        output_schema=_record_out(),
    ),
    ToolSpec(
        name="supervision.create_draft",
        title="跟访草稿",
        description="创建跟访/整改草稿（提交走 supervision.submit）",
        kind=ToolKind.DRAFT,
        risk_level=ConfirmLevel.A1,
        allowed_roles=frozenset({R_SUPERVISOR, R_MANAGER}),
        input_schema=_obj(
            {"merchant_id": _UUID, "planned_date": _DATE,
             "focus": {"type": "string", "maxLength": 500}},
            ("merchant_id", "planned_date"),
        ),
        output_schema=_record_out(),
    ),
    ToolSpec(
        name="procurement.suggest",
        title="采购建议",
        description="基于已确认需求生成采购建议（库存不可用时明确标注）",
        kind=ToolKind.DRAFT,
        risk_level=ConfirmLevel.A1,
        allowed_roles=frozenset({R_MANAGER, R_FINANCE}),
        input_schema=_obj({"period": {"type": "string", "maxLength": 16}}),
        output_schema=_list_out(),
    ),
)


# ===== 写工具（A2+；§5.3.1 矩阵内）=====

_WRITE_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "merchant.create_draft": _write_obj(
        {"name": {"type": "string", "minLength": 2, "maxLength": 128},
         "contact_name": {"type": "string", "maxLength": 64},
         "address_text": {"type": "string", "maxLength": 255}},
        ("name",),
    ),
    "merchant.update_draft": _write_obj(
        {"merchant_id": _UUID, "changes": {"type": "object"}},
        ("merchant_id", "changes"),
    ),
    "visit.submit": _write_obj(
        {"merchant_id": _UUID, "visit_task_id": _UUID,
         "display_pct": {"type": "integer", "minimum": 0, "maximum": 100},
         "conclusion": {"type": "string", "enum": [
             "NORMAL", "NEEDS_REPLENISHMENT", "NEEDS_RECTIFICATION",
             "RISK_MERCHANT", "PROPOSE_TERMINATION"]},
         "note": {"type": "string", "maxLength": 2000}},
        ("merchant_id", "visit_task_id"),
    ),
    "replenishment.submit": _write_obj(
        {"merchant_id": _UUID, "sku_id": _UUID,
         "requested_boxes": {"type": "number", "minimum": 0}},
        ("merchant_id", "sku_id", "requested_boxes"),
    ),
    "contract.create_application": _write_obj(
        {"merchant_id": _UUID,
         "title": {"type": "string", "minLength": 2, "maxLength": 200},
         "effective_from": _DATE, "expire_at": _DATE,
         "commission_rate": {"type": "number", "minimum": 0, "maximum": 100}},
        ("merchant_id", "title", "effective_from", "expire_at"),
    ),
    "schedule.publish": _write_obj(
        {"week_start": _DATE, "entry_ids": {"type": "array"}},
        ("week_start",),
    ),
    "merchant.transfer.create_request": _write_obj(
        {"merchant_id": _UUID, "to_user_id": _UUID,
         "reason": {"type": "string", "minLength": 2, "maxLength": 255}},
        ("merchant_id", "to_user_id", "reason"),
    ),
    "campaign.publish": _write_obj({"campaign_id": _UUID}, ("campaign_id",)),
    "supervision.submit": _write_obj(
        {"merchant_id": _UUID, "planned_date": _DATE,
         "findings": {"type": "string", "maxLength": 4000}},
        ("merchant_id", "planned_date"),
    ),
    "supplier.confirm_supply": _write_obj(
        {"procurement_code": {"type": "string", "minLength": 2, "maxLength": 32},
         "confirmed_cases": {"type": "number", "minimum": 0},
         "confirmed_delivery_date": _DATE},
        ("procurement_code", "confirmed_cases", "confirmed_delivery_date"),
    ),
    "report.export_request": _write_obj(
        {"report_key": {"type": "string", "minLength": 2, "maxLength": 32},
         "format": {"type": "string", "enum": ["XLSX", "CSV", "PDF"]},
         "period": {"type": "string", "maxLength": 16}},
        ("report_key", "format"),
    ),
    "approval.decide": _write_obj(
        {"approval_id": _UUID,
         "decision": {"type": "string", "enum": ["APPROVED", "REJECTED", "RETURNED"]},
         "reason": {"type": "string", "maxLength": 500}},
        ("approval_id", "decision"),
    ),
}

_WRITE_TITLES: dict[str, tuple[str, str]] = {
    "merchant.create_draft": ("商家建档草稿", "生成商家建档草稿（草稿不构成审批）"),
    "merchant.update_draft": ("商家变更草稿", "生成商家档案变更草稿"),
    "visit.submit": ("提交巡访", "提交已确认巡访（异常由非提交人经理判定）"),
    "replenishment.submit": ("提交补货需求", "提交补货需求，由非发起人的片区经理确认数量"),
    "contract.create_application": ("提交合同申请", "提交合同申请，由非申请人的片区经理审批"),
    "schedule.publish": ("发布排班", "发布周排班（冲突服务为硬门禁）"),
    "merchant.transfer.create_request": ("发起商家转移", "发起商家责任转移申请"),
    "campaign.publish": ("发布地推活动", "发布地推活动（超预算另需财务）"),
    "supervision.submit": ("提交跟访", "提交跟访现场记录（整改 VERIFIED/CLOSED 须非提交人）"),
    "supplier.confirm_supply": ("确认供货", "确认授权采购单的供货量与到货期"),
    "report.export_request": ("发起导出", "生成有权限的导出申请（敏感导出须非发起人）"),
    "approval.decide": ("处理审批", "处理本角色审批（不可审批本人申请）"),
}


def _write_specs() -> tuple[ToolSpec, ...]:
    specs: list[ToolSpec] = []
    for name in sorted(MATRIX_TOOL_NAMES):
        matrix = WRITE_APPROVAL_MATRIX[name]
        title, description = _WRITE_TITLES[name]
        specs.append(
            ToolSpec(
                name=name,
                title=title,
                description=description,
                kind=matrix_tool_kind(name),
                risk_level=MATRIX_TOOL_RISK_LEVELS[name],
                # 矩阵 initiator 是唯一真相（注册期强校验一致性）
                allowed_roles=matrix.initiator,
                input_schema=_WRITE_INPUT_SCHEMAS[name],
                output_schema=_ack_out(),
                field_policy=(
                    _SUPPLIER_REDACTED
                    if name == "supplier.confirm_supply"
                    else FieldPolicy()
                ),
                write_matrix=matrix,
            )
        )
    return tuple(specs)


ALL_TOOL_SPECS: tuple[ToolSpec, ...] = (*_READ_SPECS, *_DRAFT_SPECS, *_write_specs())

# 工具注册白名单（PRD §5.3 目录）
PRD_TOOL_CATALOG: frozenset[str] = frozenset(spec.name for spec in ALL_TOOL_SPECS)

__all__ = [
    "ALL_TOOL_SPECS",
    "PRD_TOOL_CATALOG",
    "CONFIRMATION_FIELD",
]

# T05 审批、任务与状态机内核

> 任务卡：T05 · 审批、任务与状态机内核
> PRD 章节：§4.5 冰柜、§4.6 巡访/补货、§4.8 整改、§4.9 采购/供货、§4.10 绩效/工资、
> §5.3.1 写工具审批矩阵、§5.4 确认 token 不可自审、§6.6 平台服务
> 业务规则：R23（写与审计同事务）、R24（已审核不物理删除，状态迁移留原因）、
> R27（自动任务幂等）、R30（审计不可改）

## 1. ApprovalService（app/services/approval_service.py）

不可自审 / 节点能力 / 退回-驳回原因 / 重复决定幂等。

- **不可自审**（Q021）：`actor.user_id == requester_id` 且规则不允许时
  → `PermissionDeniedError(SELF_APPROVAL_FORBIDDEN)`；
- **节点能力**：`actor.profile.abilities` 需包含规则 `required_ability`
  （T05 仅注册 `approval.decide`，需要 `MODULE_APPROVAL` 能力，授予
  片区经理 / 督导 / 财务 / 系统管理员四类角色，业务员/厂商不持有）；
- **退回/驳回原因**：`ApprovalRule.require_reason_for` 控制
  （T05 默认 `REJECTED` / `RETURNED` 强制 reason 非空）；
- **重复决定幂等**（Q022）：
  - 同 `(target_object_type, target_object_id, action)` 在 PENDING 期
    间由**部分唯一索引** `uq_approvals_pending_target` 兜底；
  - 非 PENDING 状态二次 `decide()` → `ConflictError(APPROVAL_NOT_PENDING)`；
- **同事务审计**：决定落 `audit_logs`（R23/R30），含 `before/after/reason/actor_id`；
- **审批动作注册表**：`ApprovalMatrix.get_rule(action)` 未命中即
  `APPROVAL_ACTION_NOT_REGISTERED` 拒绝提交/决定（符合 §5.4"未配置完整时
  拒绝注册"精神）。

### ApprovalRequest 表扩展（T05）
- 新增 `action: String(48)`：与 `ApprovalMatrix.action` 对应；
- 新增 `request_payload: JSONB`：发起端上下文（去敏后），便于复审；
- `status` CHECK 扩展 `RETURNED`（区别于 `REJECTED`，对应"退回补充材料"）；
- 部分唯一索引 `(target_object_type, target_object_id, action) WHERE status='PENDING'`。

## 2. TaskService（app/services/task_service.py）

派发 / 完成 / 交接 / 批量转移 / 复核任务关闭。

- **幂等创建**（Q026 / R27）：`assign()` 在插入前先查重，再插入；并发竞态
  由**部分唯一索引** `uq_task_items_idempotent` 兜底：
  `(related_object_type, related_object_id, task_kind) WHERE status IN ('OPEN','IN_PROGRESS')`。
  关闭（CLOSED）后允许重新创建。
- **复核任务拒绝直接完成**（Q023）：`task_kind` 以 `VERIFICATION::` 前缀
  或显式映射为复核类型（如 `PROBLEM_RECTIFICATION_VERIFY` /
  `CONTRACT_RENEWAL_VERIFY`）时，`requires_related_document=True`；
  `complete()` 抛 `TASK_VERIFICATION_REQUIRES_DOCUMENT`；
  关闭必须经 `close_by_document(document_object_type, document_object_id, outcome)`，
  关联单据与任务 `related_object` 不一致时 `TASK_DOCUMENT_MISMATCH`。
- **交接留痕**（Q024 / R30）：`transfer()` 强制 reason 非空，写审计
  （`before/after` 含原/新 `assignee_id`，状态由 OPEN→IN_PROGRESS），
  `batch_transfer()` 同事务多任务交接，任一失败整体回滚。
- **完成来源**：`completion_source` 记录 `DIRECT` / `RELATED_DOCUMENT` /
  `SYSTEM` / `OTHER`，便于审计追溯。

### TaskItem 表扩展（T05）
- 新增 `requires_related_document: Boolean`：复核任务标记；
- 新增 `completion_source: String(24)`：完成来源；
- 新增 `closed_at: DateTime` / `closed_by_id: UUID FK users`：关闭时间/操作人；
- 新增 CHECK 约束 `task_kind IN (...)` 与 `completion_source IN (...)`；
- 部分唯一索引 `uq_task_items_idempotent`。

## 3. 状态机内核（app/services/state_machine.py）

7 业务域 × 显式迁移图：

| 域                     | 状态集合                                                                                              |
| ---------------------- | ----------------------------------------------------------------------------------------------------- |
| CONTRACT               | DRAFT / PENDING_APPROVAL / ACTIVE / EXPIRED / TERMINATED                                              |
| FREEZER                | IN_STOCK / IN_TRANSIT / DEPLOYED / UNDER_MAINTENANCE / SCRAPPED                                       |
| REPLENISHMENT          | REQUESTED / CONFIRMED / IN_TRANSIT / DELIVERED / OVERDUE / CANCELLED                                  |
| SUPERVISION_ISSUE      | OPEN / IN_PROGRESS / RESOLVED / OVERDUE                                                               |
| PROCUREMENT            | REQUESTED / APPROVED / ORDERED / RECEIVED / CANCELLED                                                 |
| PERFORMANCE            | DRAFT / REVIEWED / APPROVED                                                                            |
| PAYROLL                | DRAFT / REVIEWED / APPROVED / PAID                                                                    |

- `assert_legal_transition(domain, frm, to)`：非法跳转抛 `DomainError(STATE_TRANSITION_INVALID)`；
- `legal_targets(domain, frm)`：返回可选目标集合（供 UI 展示）；
- **PRD §4.5 差异**：T02 既有 `FreezerAssetStatus` 用 `IN_TRANSIT` 而非
  PRD 文本中的 `RESERVED`，T05 以 T02 实际为准（不修改基础枚举，留 TODO）。
- **PRD §4.8 差异**：T02 整改状态为 `OPEN/IN_PROGRESS/RESOLVED/OVERDUE`，
  PRD 文本为 `OPEN→IN_PROGRESS→SUBMITTED→VERIFIED→CLOSED`。T05 以 T02 实际为准，
  `VERIFIED/CLOSED` 留待后续按业务细则扩展（TODO）。

## 4. BusinessTransitionService（app/services/business_transition_service.py）

跨域统一状态迁移入口。`transition(entity, domain, to_state, actor_id, reason, ...)`：

- 校验 from→to 合法性（Q025 核心）；
- 写审计（`AuditService.record_state_transition`，reason 必填 → R24）；
- 不 commit（调用方事务边界，R23）；
- 同一业务对象**不重复写** `STATE_TRANSITION` 审计时合并 `before/after`
  增量由调用方提供 `extra_after`。

## 5. 权限矩阵扩展（T05）

`MODULE_APPROVAL = "approval"` 授予 4 类"经理级"角色（与 T03 既有矩阵兼容）：

| 角色             | MODULE_APPROVAL |
| ---------------- | --------------- |
| SALES_REP        | ✗               |
| REGIONAL_MANAGER | ✓               |
| SUPERVISOR       | ✓               |
| FINANCE          | ✓               |
| SYSTEM_ADMIN     | ✓               |
| SUPPLIER         | ✗               |

> 系统管理员有 `MODULE_APPROVAL` 但仍不可审批自己发起的申请（不可自审红线）。

## 6. 新增/修改文件清单

- 修改 `app/models/approval.py`（ApprovalRequest +action/request_payload/RETURNED/部分唯一索引；
  TaskItem +requires_related_document/completion_source/closed_at/closed_by_id/部分唯一索引 +CHECK）；
- 修改 `app/models/enums.py`（ApprovalStatus 增加 RETURNED）；
- 修改 `app/core/permissions.py`（+MODULE_APPROVAL，授予 4 类经理级角色）；
- 新增 `app/services/state_machine.py`（7 业务域状态图 + assert_legal_transition + legal_targets）；
- 新增 `app/services/approval_matrix.py`（ApprovalRule + 不可自审/能力/原因三段式校验）；
- 新增 `app/services/approval_service.py`（ApprovalService.submit/decide/withdraw/list_pending）；
- 新增 `app/services/task_service.py`（TaskService.assign/complete/close_by_document/transfer/batch_transfer/mark_in_progress）；
- 新增 `app/services/business_transition_service.py`（跨域统一迁移入口）；
- 修改 `app/services/__init__.py`（导出）；
- 新增 `alembic/versions/e10d1fffe75d_t05_approval_tasks_state_machine.py`；
- 新增 `tests/test_t05_services.py`（Q021–Q026，16 用例）；
- 修改 `docs/test-traceability.csv`（T05 行）；
- 新增 `docs/approval-tasks-state-machine.md`（本文件）。

## 7. 有意未做的事项（对照红线与附录 A）

| 事项 | 处理 |
| --- | --- |
| §5.3.1 写工具审批矩阵全表 | T05 仅实现 `approval.decide` 工具元数据；其他工具元数据由 T08/T09/T13/T18 等业务卡按 §5.3.1 注册（不在 T05 越界） |
| 多节点审批链（顺序/会签） | T05 单节点单审；多节点由 T06 工作台/流程引擎扩展时再升级模型 |
| 审批催办/超时自动撤回 | 不在 T05；留 T18 业务卡或工作台任务 |
| PRD §4.5 冰柜 `RESERVED` 状态 | T02 实际为 `IN_TRANSIT`；T05 不修改基础枚举，留 TODO（待与 T02 维护者确认） |
| PRD §4.8 整改 `SUBMITTED/VERIFIED/CLOSED` 三态 | T02 实际为 `OPEN/IN_PROGRESS/RESOLVED/OVERDUE`；T05 以 T02 为准，差异留 TODO（按业务细则扩展） |
| AI 写工具的 `idempotency_key` 强约束与 §5.4 第 7 条 | 已在 T04 IdempotencyService 覆盖；T05 审批不再单独实现 |
| MCP `approval.decide` 注册表完整化 | 留 T23 MCP 工具注册卡 |

## 8. 遗留 TODO 及归属卡号

| TODO | 归属卡 |
| --- | --- |
| 写工具审批矩阵全表（`merchant.create_draft`/`visit.submit`/`schedule.publish` 等） | T08 商家中心 / T11 巡访 / T13 排班 / T09 合同 / T18 导出 |
| 多节点审批链（顺序/会签） | T06 工作台/流程引擎升级 |
| 审批超时催办/自动撤回 | T18 导出或 T27 风险工作台 |
| `FreezerAssetStatus.RESERVED` 与 PRD 文本对齐 | T02 维护者讨论；可能需要数据迁移 |
| `SupervisorIssue` 整改状态扩展 `SUBMITTED/VERIFIED/CLOSED` | T11 督导整改卡 |
| 审批前端工作台 UI | T06 应用外壳 |

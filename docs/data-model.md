# 数据模型（T02）

> 全部 38 张业务表由 Alembic 迁移 `4cb9f31f0c27_initial_full_schema.py` 管理；
> 运行 `uv run alembic upgrade head` 即可建表，建表前需启用 `postgis` 与 `pgcrypto` 扩展。

## 实体关系总览

```text
                                          ┌─────────────────────┐
                                          │ organizations (树)  │
                                          └──────────┬──────────┘
                                                     │ 1
                                                     │
                                          ┌──────────▼──────────┐
                                          │      regions        │
                                          └─────────────────────┘
                                                     │
                            ┌────────────────────────┼──────────────┐
                            │                        │              │
                            ▼                        ▼              ▼
                  ┌────────────────┐         ┌──────────────┐  ┌─────────┐
                  │    brands      │◀────────┤  merchants   │  │ users   │
                  └───────┬────────┘         └──────┬───────┘  └────┬────┘
                          │                         │              │
                          │                         ▼              │
                          │                ┌────────────────┐      │
                          │                │   contracts    │      │
                          │                └────────┬───────┘      │
                          │                         │               │
                          │                ┌────────▼────────┐     │
                          └────────────────┤  freezer_assets │     │
                                           └────────┬────────┘     │
                                                    │              │
                                                    ▼              │
                                            ┌────────────────┐      │
                                            │freezer_movements│     │
                                            └────────────────┘      │
                                                                    │
    ┌───────────────┐      ┌──────────────┐      ┌────────────────┐  │
    │ product_      │◀─────┤    skus      │      │  visit_tasks   │◀─┘
    │ categories    │      └──────────────┘      └────────┬───────┘
    └───────────────┘                                     │
                                                          ▼
                                                ┌────────────────┐
                                                │ visit_records  │
                                                └───────┬────────┘
                                                        │
                                                        ▼
                       ┌──────────────┐         ┌──────────────────┐
                       │  schedules   │         │ sales_confirmations│
                       └──────────────┘         └──────────────────┘

  activities + activity_sample_ledgers (R39)  supervisor_issues
  procurement_orders + procurement_deliveries  finance_transactions
  performance_records + deduction_approval_id → approval_requests
  payroll_records            approval_requests + task_items (待办)
  risk_items (R36 唯一约束)  audit_logs / ai_logs / import_batches
  idempotency_keys (幂等)
```

## 状态机（数据库 CHECK 强制）

| 表 | 状态字段 | 允许值 |
| --- | --- | --- |
| merchants | status | LEAD / NEGOTIATING / ACTIVE / PAUSED / TERMINATED |
| merchants | grade | A / B / C 或 NULL |
| contracts | status | DRAFT / PENDING_APPROVAL / ACTIVE / EXPIRED / TERMINATED |
| freezer_assets | status | IN_STOCK / IN_TRANSIT / DEPLOYED / UNDER_MAINTENANCE / SCRAPPED |
| freezer_movements | movement_type | PURCHASE / DEPLOY / RECALL / REPAIR_START / REPAIR_END / SCRAP / **DEPOSIT_COLLECTED / DEPOSIT_REFUNDED** (v1.2) |
| visit_tasks | status | PENDING / IN_PROGRESS / COMPLETED / OVERDUE / ABNORMAL / INVALIDATED |
| visit_records | status | DRAFT / SUBMITTED / RECOGNIZED / ABNORMAL / REJECTED |
| replenishment_orders | status | REQUESTED / CONFIRMED / IN_TRANSIT / DELIVERED / OVERDUE / CANCELLED |
| schedules | status | DRAFT / PUBLISHED / CANCELLED / COMPLETED |
| activities | status | DRAFT / PUBLISHED / IN_PROGRESS / REVIEWED / CLOSED / CANCELLED |
| procurement_orders | status | REQUESTED / APPROVED / ORDERED / RECEIVED / CANCELLED |
| approval_requests | status | PENDING / APPROVED / REJECTED / WITHDRAWN |
| payroll_records | status | DRAFT / REVIEWED / APPROVED / PAID |
| task_items | status | OPEN / IN_PROGRESS / CLOSED |
| **risk_items** | rule_code | **RK-1 .. RK-7** (v1.2) |
| risk_items | object_type | MERCHANT / CONTRACT / FREEZER / REPLENISHMENT |
| risk_items | status | OPEN / ACKNOWLEDGED / CLOSED |

## v1.2 关键建模点

1. **`freezer_assets.deposit_fen`**：可选 BIGINT（分），`CHECK (deposit_fen IS NULL OR deposit_fen >= 0)`；
   押金流水类型 `DEPOSIT_COLLECTED` / `DEPOSIT_REFUNDED` 走 `freezer_movements`，并通过
   `related_movement_id` 关联到投放/验收单（PRD R37）。
2. **`merchant_grade_frequencies`**：等级→巡访间隔天数，带 `start_date` / `end_date` 生效区间；
   `frequency_days > 0` 且 `end_date >= start_date`。
3. **`risk_items`**：唯一约束 `(object_type, object_id, rule_code, period)`，实现 RiskService 幂等生成；
   状态枚举 `OPEN / ACKNOWLEDGED / CLOSED`；`data_incomplete` 字段标记事实缺失降级。
4. **`performance_records.deduction_fen` + `deduction_approval_id`**：商家主动取消默认不扣款
   （`deduction_fen = 0`），扣减（`< 0`）必须引用 `approval_requests.id`（R40）。
5. **`activity_sample_ledgers`**：地推携带商品按样品领用/消耗/归还记录，**与 `sales_confirmations` 严格隔离**，
   不计入确认销量、绩效销量与库存（R39）。

## 唯一约束覆盖（Q007 验收）

- 账号：`uq_users_dataset_username (dataset_type, dataset_id, username)`
- 角色：`uq_roles_dataset_code (dataset_type, dataset_id, code)`
- SKU：`skus.code` 全局唯一
- 商家：`uq_merchants_dataset_code (dataset_type, dataset_id, code)`
- 合同：`uq_contracts_dataset_code (dataset_type, dataset_id, code)`
- 冰柜：`uq_freezer_assets_dataset_code (dataset_type, dataset_id, asset_code)`
- 采购单：`uq_procurement_orders_dataset_code (dataset_type, dataset_id, code)`
- 补货单：`uq_replenishment_orders_dataset_code (dataset_type, dataset_id, code)`
- 行级授权：`uq_user_region_brand (user_id, region_id, brand_id)`
- 排班时段：`uq_schedules_user_slot (user_id, week_start_date, slot_index)`
- 绩效：`uq_performance_user_period (user_id, period_start, period_end)`
- 工资：`uq_payroll_user_period (user_id, period_start, period_end)`
- 风险：`uq_risk_items_object_rule_period (object_type, object_id, rule_code, period)`（R36 幂等）
- 幂等键：`uq_idempotency_key_actor (key, actor_id)`

## 金额/箱数/时间规则（Q009）

- 金额一律 `BIGINT`（分），如 `monthly_rent_fen`、`deposit_fen`、`amount_fen`；
  CHECK `>= 0`（押金可选 NULL）；`deduction_fen` CHECK `<= 0`。
- 箱数一律 `NUMERIC(12,2)`（如 `quantity_cases`、`requested_boxes`）。
- 时间一律 `DateTime(timezone=True)`，UTC 存储；数据库时区不设为 `Asia/Shanghai`。
- 业务日期 `Date`，`start_date <= end_date`。
- 主数据软删除：`is_active BOOLEAN NOT NULL DEFAULT true`，模型层不暴露物理删除。
- 演示数据隔离：`dataset_type NOT NULL DEFAULT 'PRODUCTION'`，
  `dataset_id NOT NULL DEFAULT gen_random_uuid()`；`is_demo` 仅作兼容字段。

## 索引策略

- PostGIS 地理列 `GiST` 索引：`merchants.location`、`visit_records.location`（GeoAlchemy2 `spatial_index=True` 自动建立）。
- 状态+时间复合索引：`contracts(status, end_date)`、`replenishment_orders(status, promised_delivery_date)`、`risk_items(status)`、`risk_items(first_seen_at)`。
- 关系外键索引：`visit_records(visit_task_id, merchant_id)`、`visit_tasks(assigned_user_id, planned_date)`、`schedules(week_start_date)`、`payroll_records(user_id, period_start)`、`performance_records(user_id, period_start)`、`approvals(target_object_type, target_object_id)`。
- 数据集隔离：所有主表在 `(dataset_type, dataset_id)` 上建索引，便于演示数据与正式数据的快速分离查询。

## 关系约束/Service 二次校验

模型层已建立数据库外键（`ON DELETE RESTRICT` 为主，`ON DELETE CASCADE` 仅用于
`merchant_contacts` 与 `user_region_brand_auths` 这类纯附属表）。Service 层在
后续 T03 起会做"两端数据域一致"校验（`merchant.region` 与 `contract.region`、
`user.brand_auths` 与 `contract.brand` 等），保证行级权限与数据归属同步。

## 演示数据隔离（PRD §6 R18、§8）

- `demo_data_enabled = false` 时，导入路由对 `dataset_type='DEMO'` 直接拒绝（实现归属 T05/T29）。
- 所有主表含 `dataset_type` + `dataset_id` 不可变列，`is_demo` 仅作兼容。
- 性能/工资/采购/看板/导出统一传 `DataScope.PRODUCTION`（实现归属 T21），从入口规避演示数据混入。

## 重建流程

```bash
# 1. 建库 + 扩展
docker exec qinshuang-postgres psql -U qinshuang -d qinshuang -c "CREATE DATABASE qinshuang_test;"
docker exec qinshuang-postgres psql -U qinshuang -d qinshuang_test \
  -c "CREATE EXTENSION IF NOT EXISTS postgis; CREATE EXTENSION IF NOT EXISTS pgcrypto;"

# 2. 应用迁移
cd backend
uv run alembic upgrade head

# 3. 回退演练
uv run alembic downgrade base
uv run alembic upgrade head

# 4. 自动化测试
uv run pytest -q
```

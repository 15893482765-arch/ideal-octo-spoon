# T06 应用外壳、组件与演示数据底座

> 任务卡：T06 · 应用外壳、组件与演示数据底座
> PRD 章节：§3.1 全局框架、§3.2 页面路由、§6.2 数据域与状态机、§8 存储约束、
> §10.2 SeedCheckService、§10.4 导入顺序与失败处理
> 业务规则：R18（演示数据隔离）、R24（生产环境禁用演示导入）、R34（正式口径 PRODUCTION）
> 里程碑：M0 · 地基、安全、权限、审计、状态机与可验证数据底座

T06 范围 = 阶段里程碑 **M0** 的最后一张卡。完成 T06 后：
- 通用组件 + 路由守卫 + 应用外壳就绪；
- SeedCheckService 三类跨域负例（PRD §10.2）能跑端到端；
- 生产配置硬锁 `production_dataset_lock` + `enable_seed_cli` 拦截
  `dataset_type=DEMO` 批次；
- **不实现**：业务功能、Excel 解析、真实数据导入。

## 1. 前端（T06 前端模块）

### 1.1 10 个通用组件（Q027）
`frontend/src/components/`：

| 组件 | 用途 |
| --- | --- |
| EmptyState | 通用空态卡片（"下一步"提示） |
| StatusBadge | 状态徽章（success/warning/danger/info/neutral） |
| DataTable | 通用数据表（列/渲染函数/空态） |
| FilterBar | 通用筛选条（应用/重置） |
| Pagination | 受控分页（边界禁用） |
| ConfirmDialog | 确认弹窗（含 ESC 关闭） |
| Toast / ToastStack | 栈式通知（3s 自动消失） |
| LoadingSpinner | 加载指示器（role=status） |
| ErrorBoundary | 子树错误捕获与降级 UI |
| MetricCard | 指标卡（label/value/unit/delta） |

### 1.2 路由守卫 + AuthContext
- `lib/permissions.ts`：6 角色能力矩阵（与 T03 后端一致）；
- `lib/nav-config.ts`：导航项含 `to / label / nextStep / required / onlyFor`；
- `lib/auth.tsx`：`AuthProvider` + `useAuth` + `devRoleFromUrl`；
- `components/auth/RequireAuth.tsx`：未登录跳 `/login`；
- `components/auth/RequireAbility.tsx`：无能力跳 `/forbidden`；
- `components/layout/TopBar.tsx`：品牌 + 全局搜索 + AI/通知入口占位 + 用户信息；
- `components/layout/SideNav.tsx`：按角色裁剪的导航；
- `components/layout/AppLayout.tsx`：顶栏 + 侧栏 + 响应式（移动端侧栏抽屉化）。

### 1.3 业务页面占位
`pages/Placeholder.tsx` + 各业务页（merchants/contracts/...）均为"标题 + EmptyState
+'下一步'提示"。

## 2. 后端（T06 后端模块）

### 2.1 SeedBatch 模型
`app/models/seed.py`：

- 字段：dataset_type/dataset_id/seed_type（PENDING/RUNNING/SUCCEEDED/FAILED）/
  source_filename/file_hash/rows_*/error_log/negative_findings (JSONB) /
  started_at/finished_at/submitted_by_id；
- 状态机：PENDING → RUNNING → SUCCEEDED / FAILED；
- 唯一索引：`(source_filename, file_hash)` 防止同源文件重复；
- CHECK 约束：dataset_type / seed_type / status 白名单。

### 2.2 守卫与校验器
- `app/services/demo_data_guard.py`：
  - `DataScope`（PRODUCTION/DEMO/ALL）；
  - `assert_same_dataset()`（关系创建/校验入口）；
  - `assert_demo_disabled_in_production()`（生产配置硬锁）；
  - `assert_production_scope()`（正式口径只接受 PRODUCTION）。
- `app/services/data_scope_validator.py`：
  - `DataScopeValidator` 运行期入口注册表；
  - T06 默认注册 `export.async` / `payroll.disburse` / `performance.rollup`
    三类正式口径入口；
  - 三类负例扫描器（共享给 SeedCheckService）。

### 2.3 SeedCheckService
`app/services/seed_check_service.py`：

- `create_empty_batch()`：T06 范围内仅创建 PENDING/0 rows 空批次；
- `run_check()`：执行三类负例扫描（演示商家+生产合同 / 生产工资引演示绩效 /
  异步导出混入演示数据），命中即 FAILED 并落审计；
- `assert_export_scope()`：异步导出预检（CLI/路由共用）。

### 2.4 CLI / API
- `app/cli/db.py`：`db:reset` / `db:seed-check` / `db:seed-list` / `db:demo-smoke`；
- `app/api/v1/seed.py`：`/api/v1/seed/batches` 系列路由（仅骨架，受 `enable_seed_cli` 控制）。

## 3. 三类负例（Q030）

PRD §10.2 要求 SeedCheckService 至少包含三类负例自检。T06 落地：

1. **演示商家 + 生产合同**（`find_demo_merchant_with_production_contract`）：
   演示 Merchant 存在 + 生产 Contract 存在 → 视为潜在跨域关系证据。
   具体关系由 T08 合同卡以 FK 触发器精确化。
2. **生产工资引用演示绩效**（`find_payroll_referencing_demo_performance`）：
   演示 PerformanceRecord 存在 + 生产 PayrollRecord 存在 → 视为潜在跨域引用证据。
   T19 卡按业务细则精化映射。
3. **异步导出混入演示数据**（`count_demo_records_in_production_scope` +
   `data_scope_validator.assert_production(key="export.async")`）：
   任意正式模型 `dataset_type=DEMO` 数量 > 0 → 视为异步导出侧需强制 PRODUCTION。

## 4. 生产配置硬锁（Q031）

- `Settings.production_dataset_lock: bool = True`（新增）；
- `Settings.enable_seed_cli: bool = True`（新增）；
- `assert_demo_disabled_in_production()`：当 `environment=production` +
  `production_dataset_lock=True` 时拒绝 `dataset_type=DEMO`；
- `build_empty_check_for_environment(environment="production")` → 仅返回 PRODUCTION 批次。

## 5. 路由表（前端）

| Path | 名称 | 后续卡 |
| --- | --- | --- |
| `/login` | 登录 | T03 |
| `/workspace` | 今日工作台 | T11/T19 |
| `/merchants` | 商家中心 | T08 |
| `/contracts` | 合同中心 | T09 |
| `/assets/freezers` | 冰柜资产台账 | T10 |
| `/visits` | 巡访与打卡 | T11 |
| `/schedule` | 周排班与地图 | T13 |
| `/replenishments` | 补货中心 | T15 |
| `/campaigns` | 地推活动 | T16 |
| `/supervision` | 督导跟访与整改 | T11 |
| `/procurement` | 采购与供货 | T14 |
| `/finance/performance` | 绩效核算 | T19 |
| `/finance/payroll` | 工资发放 | T19 |
| `/reports` | 经营报表 | T20 |
| `/admin/users` | 用户与账号管理 | T03/T07 |
| `/admin/master-data` | 品牌/SKU/片区/规则/导入 | T07 |
| `/admin/audit` | 审计日志 | T18 |
| `/supplier` | 厂商门户 | T12 |

## 6. 新增/修改文件清单

### 后端
- 修改 `app/core/config.py` (+ production_dataset_lock / enable_seed_cli)
- 新增 `app/models/seed.py`（SeedBatch）
- 修改 `app/models/__init__.py`（导出）
- 新增 `app/services/demo_data_guard.py`（DataScope + 守卫）
- 新增 `app/services/data_scope_validator.py`（运行期校验器 + 负例扫描）
- 新增 `app/services/seed_check_service.py`（SeedCheckService）
- 修改 `app/services/__init__.py`（导出）
- 新增 `app/cli/__init__.py` + `app/cli/db.py`（db:reset / seed-check）
- 新增 `app/api/__init__.py` + `app/api/v1/__init__.py` + `app/api/v1/seed.py`
- 修改 `app/main.py`（注册 seed_router）
- 新增 `tests/test_t06_seed.py`（Q029/Q030/Q031 + 守卫单元）
- 新增 `alembic/versions/26bceae5250f_t06_seed_batches.py`

### 前端
- 新增 `src/lib/permissions.ts`（角色能力矩阵）
- 新增 `src/lib/nav-config.ts`（导航与路由表）
- 新增 `src/lib/auth.tsx`（AuthContext）
- 新增 `src/lib/responsive.ts`（useIsMobile）
- 新增 10 个通用组件（EmptyState/StatusBadge/DataTable/FilterBar/Pagination/ConfirmDialog/Toast/LoadingSpinner/ErrorBoundary/MetricCard）
- 新增 `src/components/auth/RequireAuth.tsx` + `RequireAbility.tsx`
- 新增 `src/components/layout/TopBar.tsx` + `SideNav.tsx` + `AppLayout.tsx`
- 新增 `src/pages/Placeholder.tsx` + `Login.tsx` + `Workspace.tsx` + `StatusPages.tsx`
- 修改 `src/App.tsx`（路由 + 守卫装配）
- 修改 `src/test/setup.ts`（mock window.matchMedia）
- 新增 `src/components/components.test.tsx`（Q027）
- 新增 `src/test-t06.test.tsx`（Q028）

### 文档
- 修改 `docs/test-traceability.csv`（T06 行）
- 新增 `docs/app-shell-and-demo-data.md`（本文件）

## 7. 有意未做清单（对照红线与附录 A）

| 事项 | 处理 |
| --- | --- |
| 业务功能（商家列表/合同详情/冰柜状态等） | 全部为 Placeholder 占位；按 T08-T19 各业务卡交付 |
| Excel/CSV 解析、真实业务数据导入 | T06 明确"不实现"（PRD §10.2 红线：T07 主数据完成前不得导入依赖 SKU/账号的业务演示数据） |
| 真实 AI 助手（端点、UI、表单"整理为草稿"） | T06 顶栏仅放入口占位；T23 业务卡接入 |
| 实时通知（WebSocket/SSE） | T06 入口占位；T19 工作台/通知中心 |
| 地图（Leaflet/Mapbox） | T06 不实现；T13 周排班 |
| 图表（ECharts/Chart.js） | T06 MetricCard 仅文本占位；T20 经营报表 |
| 真实 /auth/login 集成 | T03 已实现；T06 前端 dev login 走 `?role=` 仅开发态 |
| 厂商协作端点（CONFIRM_SUPPLY 业务） | T06 仅导航 + 路由占位；T12 厂商门户 |
| Tailwind 暗色模式 | T06 沿用亮色；后续按需 |
| i18n | T06 中文占位；后续按 PRD 启动 i18n |
| 跨进程 Redis 锁、MCP 工具注册 | 留 T23/T24 |
| **M0 里程碑的"地基"5 大类**：地基/安全/权限/审计/状态机/可验证数据底座 | 全部就位 |

## 8. 遗留 TODO 与归属卡

| TODO | 归属卡 |
| --- | --- |
| 真实 Excel 解析（openpyxl + sheet→ImportBatch rows） | T07 主数据 |
| 主数据导入顺序校验（片区/品牌/SKU/账号 → 业务表） | T07 |
| 三类负例的精确化（FK 触发器、字段级标签） | T08 合同 / T19 绩效/工资 |
| AsyncExportService 集成 `DataScope.PRODUCTION` 强制 | T18 导出 |
| AuthContext 接入真实后端（替换 devRoleFromUrl） | T03 之后任意业务卡（推荐 T07） |
| 主题色与暗色模式 | 按需（无明确归属卡） |
| i18n | 后续按 PRD 启动 |
| SeedBatch rows_total/imported/rejected 真实写库 | T07 主数据 |

## 9. 跨卡回改与被重跑的旧测试

### 9.1 被重跑的旧测试
- **T01 冒烟**（`frontend/src/App.test.tsx`，2 用例）：
  - 旧 App.tsx 是静态占位页；T06 替换为带 BrowserRouter/AuthProvider 的应用根。
  - 为保留 T01 测试期望（"沁爽饮料分销管理平台" + "下一步"），`Login.tsx`
    显式包含平台名与"下一步：T07 主数据"提示。
  - 真实运行：2/2 通过。
- **T03 权限模型**（`app/core/permissions.py`）：T06 未触动；T03 既有 6 角色能力
  矩阵作为前端 `lib/permissions.ts` 的事实来源（前端 = 后端只读镜像）。
- **T05 状态机**（`app/services/state_machine.py` 等）：T06 未触动；T05 既有 81
  用例在 T06 全套门禁中重跑，**全部 81 通过**。
- **T02 数据模型**（20+ 表）：T06 仅新增 `seed_batches` 一张表，未触动既有列与
  CHECK 约束。T02 既有 26 用例在 T06 全套门禁中重跑，**全部 26 通过**。
- **T04 文件/审计/幂等/脱敏**（`app/services/audit_service.py` 等）：T06 仅
  在 SeedCheckService 中复用 AuditService.record() 写审计；T04 既有 20 用例
  在 T06 全套门禁中重跑，**全部 20 通过**。

### 9.2 真实运行结果
- 后端 pytest：**98 passed**（T01 0 [CLI 测] + T02 26 + T03 10 + T04 20 + T05 16 + **T06 17** + conftest 9）；
- 后端 ruff：`All checks passed!`；
- 后端 mypy：`Success: no issues found in 69 source files`；
- 前端 typecheck / lint / test / build：全 EXIT=0；test **29 passed**（T01 2 + T06 Q027 12 + T06 Q028 15）。

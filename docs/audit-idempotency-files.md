# T04 审计、幂等、文件与敏感字段

> 任务卡：T04 · 审计、幂等、文件与敏感字段
> PRD 章节：§2.4（行级/字段脱敏）、§5.4（确认 token 绑定 idempotency_key）、
> §8（AuditLog 模型骨架 / S3 短期签名 URL / 手机号按字段级权限返回）、
> §9.4（日志不落凭据）、R23/R24/R27/R30、附录 A

## 1. AuditService（app/services/audit_service.py）

- 与业务写操作**同一 session、同一事务**：本服务绝不自行 commit/rollback；
- 记录 `actor_id` / `reason` / `before_json` / `after_json` / `request_id` /
  `payload`；`before/after/extra` 全部先经 `redact()` 脱敏再入库（凭据正文永不进审计）；
- `record_state_transition()` 强制 `reason` 非空（R24）；
- 本服务不提供任何 UPDATE/DELETE 入口（R30：审计只增不改）。

### 失败事务不留成功审计
AuditService 仅 `session.add + session.flush`；事务归属调用方；业务回滚（含
SAVEPOINT 回滚）时审计一并消失（Q016 核心）；同事务可被 `session_scope` 的
`with` 上下文统一管理。

## 2. IdempotencyService（app/services/idempotency_service.py）

覆盖：移动端提交、审批、AI 写工具、外部回调四类入口。

- 唯一约束 `(scope, key)`：并发安全由数据库约束保障；
- 同 (scope, key) + 同参数（params_hash） → 重放首次结果，业务函数不重复执行；
- 同 (scope, key) + 异参数 → `IDEMPOTENCY_PARAMS_CONFLICT`（HTTP 409）；
- `status=PENDING`（并发占位中）→ `IDEMPOTENCY_IN_PROGRESS`（HTTP 409）；
- `status=FAILED` 允许同键重试（删除旧记录重新执行）；
- 与业务同 session 同事务：业务回滚时 PENDING/COMPLETED 记录一并消失；
- 通过 `session.begin_nested()` SAVEPOINT 处理 IntegrityError，避免冲突回滚波及调用方事务。

### 数据模型演进
- T02 首建 `idempotency_keys (key, actor_id, response_ref)`；
- T04 扩展：`scope`、`params_hash`、`status`、`response(JSONB)`、`error_message`、
  `request_id`、`lock_version`；唯一约束从 `(key, actor_id)` 改为 `(scope, key)`；
- 列名 `key` 沿用 T02（属性名 `idem_key`），避免重命名迁移。

## 3. FileService（app/services/file_service.py）

- 上传：MIME + 扩展名双白名单（默认 `image/jpeg,image/png,image/webp,application/pdf`），
  MIME 与扩展名必须一致防止伪造头绕过；单文件 ≤ 10MB；
- SHA-256 落列；`object_key = {biz_type}/{uuid}/{安全文件名}`（UUID 防覆盖）；
- 下载走**短期签名 URL**（HMAC，绑定 file_id + actor_id + 过期时间，TTL 默认 5 分钟）；
- **访问时再次鉴权**：签名校验通过后仍要重查访问人身份与文件归属；签发前业务层
  必须先完成行级鉴权并把结果传入 `is_authorized=True`；
- 预留病毒扫描状态（`scan_status`：PENDING / CLEAN / INFECTED / SKIPPED），
  `mark_scan_status` 提供扫描器回调入口；
- **INFECTED 一律拒绝签发与下载**（隔离）；
- 不提供覆盖/改名接口（红线：原始定位/照片不可覆盖）。

### 存储后端
- `StorageBackend` 协议：`put` / `get` / `delete`；
- `LocalFSStorage`：本地文件系统实现（开发与测试默认后端）；
- `S3Storage`：MinIO/云 OSS 实现（生产后端；本地无 MinIO 容器，连接级
  验证随 T09 合同附件 / T11 巡访照片接入时进行；附录 A：真实病毒扫描不在首版）。

## 4. 响应 Schema 字段裁剪（app/core/redaction.py）

所有对外响应在返回前必须经过 `sanitize_response(actor, payload)`；禁止在
Router 内手写 `if role == "..."` 裁剪逻辑。

字段类 × 角色策略：

| 字段类             | 厂商 | 业务员 | 片区经理 | 督导 | 财务 | 系统管理员 |
| ------------------ | ---- | ------ | -------- | ---- | ---- | ---------- |
| 手机号             | 隐藏 | 掩码   | 掩码     | 掩码 | 掩码 | 明文       |
| 精确坐标           | 隐藏 | 明文   | 明文     | 明文 | 明文 | 明文       |
| 工资字段（payroll）| 隐藏 | 隐藏   | 隐藏     | 隐藏 | 明文 | 明文       |
| 成本/毛利（cost）  | 隐藏 | 隐藏   | 明文     | 隐藏 | 明文 | 明文       |

> 掩码 = 138\*\*\*\*1234（保留前 3 后 4）；明文 = 业务所需全值。
> 业务员的"看自己"绩效/工资由 T19 专用接口放行，不走通用列表。

## 5. 日志脱敏（app/core/logging.py）

- `JsonFormatter.format` 对 `record.getMessage()` 调用 `sanitize_message()`：
  - 凭据键值（`password=…` / `token=…` / `secret=…` 等）→ `***REDACTED***`；
  - JWT（`eyJ…`）→ `***REDACTED***`；
  - Argon2 哈希（`$argon2…`）→ `***REDACTED***`；
  - 中国大陆手机号 → 掩码 138\*\*\*\*1234；
- `redact()` 递归对 dict 命中敏感键的值做替换；
- 异常堆栈也走 `sanitize_message()`，防止堆栈带出参数。

## 6. 新增/修改文件清单

- 新增 `app/models/idempotency.py`（`IdempotencyKey` 演进为通用幂等内核）；
- 新增 `app/models/file_object.py`（`FileObject`）；
- 修改 `app/models/audit.py`（`AuditLog` 增加 reason / before_json / after_json）；
- 修改 `app/models/risk.py`（移除 `IdempotencyKey`，注释其迁移位置）；
- 修改 `app/models/__init__.py`（导出新模型）；
- 新增 `app/services/audit_service.py`；
- 新增 `app/services/idempotency_service.py`；
- 新增 `app/services/storage.py`；
- 新增 `app/services/file_service.py`；
- 修改 `app/services/__init__.py`（导出新服务）；
- 新增 `app/core/redaction.py`；
- 修改 `app/core/logging.py`（消息级正则脱敏）；
- 修改 `app/core/errors.py`（`ConflictError` / `FileAccessError`）；
- 修改 `app/core/handlers.py`（`ConflictError` → HTTP 409）；
- 修改 `app/core/config.py`（文件/幂等相关配置项）；
- 新增 `alembic/versions/4c97893f84d4_t04_audit_idempotency_files.py`；
- 新增 `tests/test_t04_services.py`（Q016–Q020 + 409 处理器 + 字段裁剪）；
- 新增 `docs/audit-idempotency-files.md`（本文件）；
- 更新 `docs/test-traceability.csv`（T04 行）。

## 7. 有意未做的事项（对照红线与附录 A）

| 事项 | 处理 |
| --- | --- |
| 真实病毒扫描器 | 附录 A 不做；仅保留 `scan_status` 字段与 `mark_scan_status` 入口（占位） |
| S3 存储后端的真实连接验证 | 本地无 MinIO 容器；`S3Storage` 仅按 boto3 接口实现，待 T09/T11 接入时实测 |
| 跨进程分布式锁优化幂等 | 当前由数据库唯一约束保障；Redis 锁后续按需引入，不改变 `IdempotencyService` 接口 |
| 工资/成本/手机号在特定业务接口的字段级策略微调 | 留待 T08 商家中心、T19 绩效等业务卡按需在 `redaction.py` 调整 |
| 审计查询 / 导出接口 | 留 T27 风险工作台 / T18 导出时统一实现 |

## 8. 遗留 TODO 与归属卡号

| TODO | 归属卡 |
| --- | --- |
| `S3Storage` 实环境连接验证 | T09 合同附件 / T11 巡访照片 |
| 病毒扫描器集成（替换 `mark_scan_status` 占位） | 二期（附录 A） |
| 审计查询 / 导出 API | T18 导出 / T27 风险工作台 |
| 跨进程幂等（Redis 锁）优化 | 后续按需 |
| 字段级策略按业务卡微调 | T08 / T19 |

# 开发约定（CONVENTIONS）

> 基线：《沁爽饮料分销管理平台-PRD-Python实施版-v1.2.md》§9.2/§9.4、《任务卡开发流程 v2.0》§一。

## 目录结构

```text
backend/     Python 3.12 + FastAPI + SQLAlchemy 2（uv 管理）
  app/
    api/        Router：鉴权、Pydantic 校验、调用 Service、返回 Response Schema
    services/   业务规则、状态机、事务边界、权限二次校验、审计（T03 起建）
    repositories/ SQLAlchemy 查询；复杂统计集中在 metrics_repository（T02 起建）
    models/     ORM，不直接作为 API 返回（T02 起建）
    schemas/    Request/Response 与字段裁剪/脱敏（T03 起建）
    core/       配置、认证、权限、异常、幂等、审计、日志
    mcp/        MCP 工具适配，只调 Service（T22 起建）
  tests/
frontend/    Vite + React + TS + Tailwind 4 + shadcn/ui 约定（pnpm 管理）
infra/       docker-compose：PostgreSQL/PostGIS、Redis、MinIO
docs/        规范文档与测试追踪矩阵
scripts/     CI 辅助脚本（测试追踪矩阵校验等）
```

## 命令

| 动作 | 后端（在 backend/ 下） | 前端（在 frontend/ 下） |
| --- | --- | --- |
| 安装 | `uv sync`（国内网络：`uv sync --default-index https://mirrors.aliyun.com/pypi/simple/`；`uv.lock` 中记录的阿里云下载地址全球可达且带 sha256 哈希，不影响完整性） | `pnpm install`（frontend/.npmrc 已配 npmmirror，可按需更换） |
| Lint | `uv run ruff check .` | `pnpm lint` |
| 类型 | `uv run mypy .` | `pnpm typecheck` |
| 测试 | `uv run pytest` | `pnpm test`（Vitest）/ `pnpm e2e`（Playwright，需 `pnpm exec playwright install`） |
| 构建/启动 | `uv run uvicorn app.main:app --reload` | `pnpm build` / `pnpm dev` |

基础设施：复制根目录 `.env.example` 为 `.env`，然后 `docker compose -f infra/docker-compose.yml up -d --wait`。

## 分层与事务

1. Router 不写业务逻辑、不 `commit()`；业务事务在 Service 层。
2. 写操作与审计日志在同一数据库事务（T04 起强制）。
3. 统一错误结构：`{code, message, details, request_id}`；业务错误抛 `DomainError`，全局处理器转换；禁止把堆栈、SQL、密钥返回前端。
4. 每个请求经 `RequestContextMiddleware` 生成/透传 `X-Request-Id`，日志与错误体携带。

## 数据与时间规则

- 金额 `BIGINT` 存“分”，业务层 `Decimal` 计算；禁止 `float/double` 参与金额。
- 箱数 `NUMERIC(12,2)`。
- 时间 UTC 存储、`Asia/Shanghai` 展示；数据库时区保持 UTC，不设为 `Asia/Shanghai`。
- 业务周：`Asia/Shanghai` 周一 00:00（含）至下周一 00:00（不含）。
- 密钥一律 `SecretStr`；日志经 `redact()` 脱敏，敏感键见 `app/core/logging.py`。

## 测试约定

- 测试编号 `Q001` 起全局唯一；每条 Q 映射登记在 `docs/test-traceability.csv`，由 `scripts/validate_traceability.py` 校验（重复、缺失、悬空引用即失败）。
- 提交信息格式：`T<NN>: <卡片名称>`（如 `T01: 工程初始化与开发约定`）。
- 跨卡回改必须重跑被改卡全部 Q 测试，并在提交说明中列出。

## 红线（违反即返工）

- 无自助注册；初始密码/凭据不得以明文进入数据库、日志、导出、种子脚本或 AI 上下文。
- 行级数据范围只能由服务端 JWT/session 注入；禁止客户端或模型指定 actor/role/region/brand。
- 已审核事实不物理删除、不覆盖历史；演示数据（`dataset_type=DEMO`）不得进入正式绩效、工资、采购、看板与导出口径。
- 不提前实现后续任务卡内容，不实现附录 A 中“首版不做”的项目。

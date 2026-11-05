# 运行手册（T30 / 首版上线运维）

适用于「沁爽饮料分销管理平台」首版（里程碑 M5）。所有命令在仓库根目录执行。

## 1. 部署（全新环境）

```bash
# 1) 准备 .env（见第 3 节 Secret 清单；INITIAL_ADMIN_* 仅首次需要）
cp .env.example .env   # 首次部署按清单填写

# 2) 构建镜像（生产镜像非 root、无内联密钥）
docker build -t qinshuang-backend:t30 -f backend/Dockerfile backend

# 3) 启动：migrate（init 容器，跑 alembic upgrade head）→ api
docker compose --env-file .env -f infra/docker-compose.yml up -d

# 4) 冒烟验证（真实全新库 → 迁移 → 起容器 → 登录 → 零演示数据）
python scripts/deploy_smoke.py --image qinshuang-backend:t30 --report docs/ops/deploy-smoke.json
```

`migrate` 是 `restart: "no"` 的一次性 init 容器：迁移失败时 `api` 不会启动（`service_completed_successfully`），
避免「未迁移就接流量」。

**部署后立即执行**：从 `.env` 移除 `INITIAL_ADMIN_PASSWORD` 并重启 `api`
（首任管理员 `must_change_password=true`，首登强制改密，明文凭据不长期滞留）。

## 2. 升级与回滚

```bash
# 升级
docker compose --env-file .env -f infra/docker-compose.yml run --rm migrate  # alembic upgrade head
docker compose --env-file .env -f infra/docker-compose.yml up -d --no-deps api

# 回滚（按迁移步数，而非整库回退）
docker compose --env-file .env -f infra/docker-compose.yml run --rm migrate alembic downgrade -1
docker compose --env-file .env -f infra/docker-compose.yml up -d --no-deps api
```

回滚演练（上线前必做，真实空库）：`pytest tests/test_t30_deployment.py -k q176`。
约束：**已审核事实不物理删除**——回滚只允许改变结构，禁止用回滚脚本删除业务数据；
确需数据修正走状态迁移 + 原因留痕。

## 3. Secret 清单（全部经环境变量注入，禁止写入镜像/代码/日志）

| 变量 | 必填 | 说明 | 轮换 |
|---|---|---|---|
| `SECRET_KEY` | ✅ | JWT 签名密钥，≥32 字符；变更会使存量 token 失效 | 90 天或泄露即换 |
| `DATABASE_URL` | ✅ | 含数据库口令；口令仅经 `PGPASSWORD`/DSN 传入，不进 argv | 180 天 |
| `POSTGRES_PASSWORD` | ✅ | 仅 compose 起库用 | 180 天 |
| `REDIS_URL` | ✅ | 缓存/限流 | 口令变更即换 |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | ✅ | 对象存储凭据（生产建议改用实例角色/STS） | 90 天 |
| `S3_ENDPOINT_URL` / `S3_BUCKET` | ✅ | 对象存储地址与桶 | — |
| `INITIAL_ADMIN_USERNAME/PASSWORD/DISPLAY_NAME` | 仅首启 | 首任管理员；**上线后必须移除密码** | 一次性 |
| `INITIAL_ADMIN_BOOTSTRAP` | 仅首启 | `true` 才引导；生产日常必须为 `false` | 一次性 |

校验：`pytest tests/test_config.py`（配置不得在 repr/日志中泄漏密钥）。

## 4. 监控与告警

| 信号 | 来源 | 阈值建议 | 处置 |
|---|---|---|---|
| 存活/就绪 | `GET /healthz` | 连续 3 次失败 | 重启实例；查日志与 DB 连接 |
| 业务指标 | `GET /metrics`（Prometheus 文本） | 见下 | 按指标定位 |
| 登录限流触发 | `metrics` 中 `rate_limited_total` | 突增 | 疑似爆破，查来源 IP |
| AI 降级 | `ai_degradation_total` | 持续上升 | 上游模型不可用，功能已降级不阻断 |
| 指标写入失败 | `metrics_emit_failed_total` | >0 且持续 | 观测旁路故障（已 fail-open，不影响业务） |
| 慢查询 | `p95` 基准（Q169） | 工作台 P95>1s / 列表>800ms | 查 EXPLAIN，见 `docs/performance/q169-benchmark.json` |
| 备份 | `scripts/backup.py` 退出码 | 非 0 即告警 | 见第 5 节 |
| 磁盘 | PG/MinIO 数据卷 | >80% | 扩容或清理归档 |

告警最小集：`/healthz` 探针失败、备份任务非 0 退出、`metrics_emit_failed_total` 上升、
登录限流突增。

## 5. 备份与恢复

- 每日 02:00 `python backend/scripts/backup.py backup`（保留 30 天，`BACKUP_RETENTION_DAYS`）；
- 每周 `python backend/scripts/backup.py drill` 恢复到隔离 DR 库并校验 RPO≤24h、RTO≤4h；
- 归档需同步到异地对象存储桶（开启版本 + Object Lock）；演练记录见
  `docs/security/backup-drill-q171.md`；
- 恢复演练：`bash infra/drill_restore.sh`（容器内真实备份→恢复→校验标记数据）。

## 6. 首日核对表（上线当天逐项打勾）

| # | 核对项 | 命令 / 依据 | 通过标准 |
|---|---|---|---|
| 1 | 迁移到 head | `docker compose run --rm migrate && alembic current` | 版本 = 代码 head |
| 2 | 服务健康 | `curl -fsS http://<host>:8000/healthz` | 200 且带 request_id |
| 3 | 部署冒烟 | `python scripts/deploy_smoke.py` | 10/10 检查通过 |
| 4 | 首任管理员可登录 | 浏览器登录 | 200 且强制改密 |
| 5 | 明文凭据已清理 | `.env` 无 `INITIAL_ADMIN_PASSWORD` | 已移除并重启 |
| 6 | 零演示数据 | `select count(*) from users where dataset_type='DEMO'` | 0 |
| 7 | 备份任务 | `python scripts/backup.py backup` | 退出码 0，产物存在 |
| 8 | 恢复演练 | `bash infra/drill_restore.sh` | RPO/RTO 达标 |
| 9 | 监控告警 | `/metrics` 可抓取，告警规则已挂 | 有数据、告警可达 |
| 10 | 全量测试 | `cd backend && uv run pytest -q` | 全绿 |
| 11 | 安全扫描 | Trivy / pip-audit | critical/high = 0 |
| 12 | UAT 全链路 | `python scripts/uat_full_chain.py` | 16/16 |
| 13 | **业务负责人签署** | `docs/uat/uat-signoff.md` | 签署栏完成 |

> 第 13 项未完成前不得宣布正式上线（PRD 第十一章上线门禁，自动化项全绿仅为前置条件）。

## 7. 常见故障处置

| 症状 | 可能原因 | 处置 |
|---|---|---|
| `/healthz` 200 但业务接口 500 | 迁移未跑完 / DB 连接耗尽 | 查 `alembic current`、连接池与慢查询 |
| 登录 500 且日志 `Multiple rows were found` | 引导并发产生重复管理员（历史缺陷，已在 T30 修复为唯一约束仲裁） | 保留业务需要的 1 条、其余转为非 ACTIVE；升级到 T30 镜像 |
| 迁移失败 `type "geography" does not exist` | 未建 PostGIS 扩展（历史缺陷，已在基线迁移内置） | 升级到 T30 镜像重跑迁移 |
| 限流误伤正常用户 | 阈值过低 | 调整限流配置；后端故障时已 fail-open |
| S3 不可用导致上传失败 | 对象存储故障 | 核心登录/巡访不受影响；恢复后重试上传 |
| AI 不可用 | 上游超时 | 页面已降级为草稿/排序清单/确定性指标，不阻断录入 |

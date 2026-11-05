# 安全扫描基线（T29 / Q166–Q172）

扫描器版本全部**固定**，结果可复现；critical/high 必须为 0。

| 扫描器 | 固定版本 | 覆盖对象 | 最近结果 | 工件 |
|---|---|---|---|---|
| pip-audit | 2.7.3 | Python 依赖（uv.lock 解析） | 0 已知漏洞 | `pip-audit-sbom.json`（CycloneDX SBOM） |
| Trivy | v0.56.2 | IaC（compose/Dockerfile）+ 镜像 CVE | critical/high = 0 | `trivy-iac.json`、`trivy-dockerfile.json`、`trivy-image.json` |
| Checkov | 3.2.269 | Dockerfile / IaC 策略 | 0 failed（见 CI） | `checkov-dockerfile.json` |

> 说明：Checkov 3.2.269 的 `docker_compose` 框架已并入 `yaml`，对本项目裸
> `docker-compose.yml`（非 K8s/Terraform）无内置策略，故 compose 主要由 Trivy 配置扫描覆盖，
> Dockerfile 由 Checkov dockerfile + Trivy 双重覆盖。

## 本地复现

```powershell
# 依赖漏洞（在 backend/ 下）
uv run --with "pip-audit==2.7.3" pip-audit --strict `
  --format cyclonedx-json -o ../docs/security/pip-audit-sbom.json

# Trivy IaC + 镜像（需要 Docker）
docker run --rm -v "${PWD}:/src" aquasec/trivy:0.56.2 config `
  --severity CRITICAL,HIGH --exit-code 1 --ignorefile .trivyignore.yaml infra backend/Dockerfile
docker build -t qinshuang-backend:t29 -f backend/Dockerfile backend
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v "${PWD}:/out" `
  aquasec/trivy:0.56.2 image --severity CRITICAL,HIGH --exit-code 1 --ignore-unfixed qinshuang-backend:t29

# Checkov Dockerfile
uvx --from "checkov==3.2.269" checkov --framework dockerfile -f backend/Dockerfile
```

## 例外策略

critical/high 一律阻断。确需例外：在仓库根 `.trivyignore.yaml` 写明 ID、**到期日（≤30 天）**、
负责人、书面审批工单、补偿控制；到期自动失效并重新阻断。本仓当前例外数为 0。

## Secret 管理

- 数据库/对象存储口令只来自环境变量（`DATABASE_URL`、S3/MinIO access/secret），
  镜像与代码不含明文密钥；`backend/.dockerignore` 排除 `.env*`（保留 `.env.example`）。
- `pg_dump/psql` 的口令经 `PGPASSWORD` 注入，不进进程 argv（见 `scripts/backup.py`）。
- 生产密钥应走 K8s Secret / 云 KMS；compose 中的口令仅为本地开发占位，禁止用于生产。

## 灾备

见 `backup-drill-q171.md`（真实备份→恢复演练记录：RPO≤24h、RTO≤4h 已验证）。

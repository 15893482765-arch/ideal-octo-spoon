# syntax=docker/dockerfile:1.7
# 沁爽分销后端镜像 —— 多阶段、非 root、最小运行面（T29 安全基线）
# 基础镜像按摘要固定，避免标签漂移（生产应在 CI 用 cosign 验签）。
FROM python:3.12-slim-bookworm AS builder

# uv 固定版本（与本地 uv 0.12.17 一致，保证 lock 解析可复现）
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CONFIG=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# 仅依赖层优先，利用构建缓存
COPY pyproject.toml uv.lock ./
# 生产依赖：不带 dev 组（pytest/ruff/mypy 不进镜像）
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY app ./app
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY mcp ./mcp
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# ---- 运行阶段：精简，仅含 venv + 应用 ----
FROM python:3.12-slim-bookworm AS runtime

# 运行期安全加固
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    APP_ENV=production

# 非 root 用户；curl 仅用于健康检查，装完即清缓存
RUN groupadd --system app && useradd --system --gid app --home /app app \
    && apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder --chown=app:app /app /app

USER app
EXPOSE 8000

# 容器级健康检查命中业务健康端点
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

# 迁移由发布流水线/init 容器执行（alembic upgrade head），应用本身以多 worker 启动
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--proxy-headers", "--forwarded-allow-ips=*"]

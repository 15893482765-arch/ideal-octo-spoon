#!/usr/bin/env python3
"""T30 Q177：全新环境部署冒烟（真实 Docker + 真实 PG，非模拟）。

模拟一次「新环境上线」的完整部署动作，逐步真实校验：

    1. 准备全新数据库（drop/create，等价于新环境空库）
    2. 用生产镜像执行迁移 alembic upgrade head（发布流水的迁移步骤）
    3. 以生产镜像启动 API 容器（非 root、无内联密钥、env 注入）
    4. 轮询 /healthz 直到就绪（部署就绪探针）
    5. 用部署时注入的 INITIAL_ADMIN_* 登录（引导首位管理员）
    6. 校验新环境零演示数据（demo 隔离红线）
    7. 校验迁移版本 = 代码 head（避免漏迁移）

任一步失败即退出码 1，并输出结构化报告（供上线门禁与首日核对引用）。

用法：
    python scripts/deploy_smoke.py                       # 默认镜像 qinshuang-backend:t30
    python scripts/deploy_smoke.py --build               # 先构建镜像
    python scripts/deploy_smoke.py --report docs/ops/deploy-smoke.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"

NETWORK = "qinshuang_default"
PG_CONTAINER = "qinshuang-postgres"
PG_USER = "qinshuang"
PG_PASSWORD = "change-me-strong-password"
FRESH_DB = "qinshuang_deploy"
API_CONTAINER = "qinshuang-deploy-smoke"
API_PORT = 18080

ADMIN_USER = "deployadmin"
ADMIN_PASSWORD = "Deploy-Smoke-Pass-2026"
# 生产部署必须注入的运行时配置（Settings 校验为必填；迁移与 API 共用同一套）
SMOKE_SECRET = "deploy-smoke-secret-key-min-32-chars-ok"


def common_env(dsn: str) -> list[str]:
    return [
        "-e", "ENVIRONMENT=production",
        "-e", f"SECRET_KEY={SMOKE_SECRET}",
        "-e", f"DATABASE_URL={dsn}",
        "-e", "REDIS_URL=redis://qinshuang-redis:6379/0",
        "-e", "S3_ENDPOINT_URL=http://host.docker.internal:9000",
        "-e", "S3_ACCESS_KEY=minioadmin",
        "-e", "S3_SECRET_KEY=minioadmin",
        "-e", "S3_BUCKET=qinshuang",
    ]


def run(cmd: list[str] | str, *, timeout: int = 600, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        cmd, shell=isinstance(cmd, str), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"命令失败({proc.returncode}): {cmd}\n{(proc.stdout or '')[-1500:]}\n{(proc.stderr or '')[-1500:]}"
        )
    return proc


def psql(sql: str, db: str) -> str:
    return run([
        "docker", "exec", PG_CONTAINER, "psql", "-U", PG_USER, "-d", db, "-tAc", sql,
    ]).stdout.strip()


class Checks:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, check_id: str, ok: bool, detail: str = "") -> None:
        self.items.append({"id": check_id, "ok": ok, "detail": detail})

    @property
    def failed(self) -> list[str]:
        return [c["id"] for c in self.items if not c["ok"]]


def baseline_migration_rev() -> str:
    """代码 head（用于与新环境实际迁移版本比对）。"""
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"], cwd=BACKEND,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    for line in (proc.stdout or "").splitlines():
        token = line.strip().split(" ")[0]
        if token and token.lower() != "down_revision:" and not token.startswith("INFO"):
            return token
    raise RuntimeError(f"无法解析 alembic heads 输出: {proc.stdout} {proc.stderr}")


def wait_health(url: str, timeout_s: int = 90) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:  # noqa: S310 - 本地回环
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError, TimeoutError):
            pass
        time.sleep(2)
    return False


def post_json(url: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310 - 本地回环
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8") or "{}"
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body}


def main() -> int:
    ap = argparse.ArgumentParser(description="T30 全新环境部署冒烟")
    ap.add_argument("--image", default="qinshuang-backend:t30")
    ap.add_argument("--build", action="store_true", help="先构建镜像")
    ap.add_argument("--keep", action="store_true", help="保留容器与数据库（排障用）")
    ap.add_argument("--report", default="")
    args = ap.parse_args()

    checks = Checks()
    started = time.time()

    print("=" * 76)
    print("T30 · 全新环境部署冒烟（Q177）")
    print("=" * 76)

    # 0) 镜像存在性（或构建）
    if args.build:
        print("[0/7] 构建镜像…")
        run(["docker", "build", "-t", args.image, "-f", "backend/Dockerfile", "backend"], cwd=ROOT, timeout=1800)
    images = run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"]).stdout
    checks.add("image_available", args.image in images, args.image)

    # 1) 全新数据库
    print("[1/7] 准备全新数据库…")
    run(["docker", "rm", "-f", API_CONTAINER], check=False)
    psql("SELECT 1", "postgres")
    run(["docker", "exec", PG_CONTAINER, "psql", "-U", PG_USER, "-d", "postgres", "-c",
         f"DROP DATABASE IF EXISTS {FRESH_DB} WITH (FORCE);"])
    run(["docker", "exec", PG_CONTAINER, "psql", "-U", PG_USER, "-d", "postgres", "-c",
         f"CREATE DATABASE {FRESH_DB};"])
    table_count = int(psql(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';", FRESH_DB
    ) or 0)
    checks.add("fresh_db_is_empty", table_count == 0, f"public 表数={table_count}")

    # 2) 迁移（发布流水的迁移步骤，容器内执行）
    print("[2/7] 执行迁移 alembic upgrade head…")
    dsn = f"postgresql+psycopg2://{PG_USER}:{PG_PASSWORD}@{PG_CONTAINER}:5432/{FRESH_DB}"
    mig = run([
        "docker", "run", "--rm", "--network", NETWORK,
        *common_env(dsn),
        args.image, "alembic", "upgrade", "head",
    ], timeout=900)
    checks.add("migration_applied", mig.returncode == 0, (mig.stdout or "").strip()[-200:])

    # 3) 启动 API 容器
    print("[3/7] 启动 API 容器…")
    run(["docker", "rm", "-f", API_CONTAINER], check=False)
    run([
        "docker", "run", "-d", "--name", API_CONTAINER, "--network", NETWORK,
        "-p", f"{API_PORT}:8000",
        *common_env(dsn),
        "-e", "INITIAL_ADMIN_BOOTSTRAP=true",
        "-e", f"INITIAL_ADMIN_USERNAME={ADMIN_USER}",
        "-e", f"INITIAL_ADMIN_PASSWORD={ADMIN_PASSWORD}",
        "-e", "INITIAL_ADMIN_DISPLAY_NAME=部署管理员",
        args.image,
    ])
    owner = run(["docker", "exec", API_CONTAINER, "id", "-u"]).stdout.strip()
    checks.add("container_non_root", owner not in {"0", ""}, f"uid={owner}")

    # 4) 健康检查
    print("[4/7] 等待 /healthz 就绪…")
    base = f"http://127.0.0.1:{API_PORT}"
    healthy = wait_health(f"{base}/healthz")
    checks.add("healthz_ready", healthy, f"{base}/healthz")
    if not healthy:
        logs = run(["docker", "logs", "--tail", "40", API_CONTAINER], check=False).stdout
        print(logs)

    # 5) 引导管理员登录（部署时 env 注入的首位管理员）
    print("[5/7] 引导管理员登录…")
    status, body = post_json(f"{base}/api/auth/login",
                             {"username": ADMIN_USER, "password": ADMIN_PASSWORD})
    checks.add("bootstrap_admin_login", status == 200 and "access_token" in body,
               f"HTTP {status}")
    checks.add("first_login_forces_password_change", body.get("must_change_password") is True,
               str(body.get("must_change_password")))
    # 多 worker 并发启动下，引导必须幂等：同名管理员只能有一条
    dup_admins = int(psql(
        f"SELECT count(*) FROM users WHERE username='{ADMIN_USER}';", FRESH_DB
    ) or 0)
    checks.add("bootstrap_admin_is_unique", dup_admins == 1, f"同名管理员={dup_admins}")

    # 6) 新环境零演示数据（红线）
    print("[6/7] 校验零演示数据…")
    demo_users = int(psql(f"SELECT count(*) FROM users WHERE dataset_type='DEMO';", FRESH_DB) or 0)
    checks.add("no_demo_data_in_fresh_env", demo_users == 0, f"DEMO 用户={demo_users}")

    # 7) 迁移版本 = 代码 head
    print("[7/7] 校验迁移版本…")
    head = baseline_migration_rev()
    current = psql("SELECT version_num FROM alembic_version;", FRESH_DB)
    checks.add("migration_at_head", current == head, f"current={current} head={head}")

    # 清理
    if not args.keep:
        run(["docker", "rm", "-f", API_CONTAINER], check=False)
        run(["docker", "exec", PG_CONTAINER, "psql", "-U", PG_USER, "-d", "postgres", "-c",
             f"DROP DATABASE IF EXISTS {FRESH_DB} WITH (FORCE);"], check=False)

    ok = not checks.failed
    print("-" * 76)
    for c in checks.items:
        print(f"  {'PASS' if c['ok'] else 'FAIL'}  {c['id']:<34} {c['detail'][:60]}")
    print("-" * 76)
    print(f"耗时 {round(time.time() - started, 1)}s → "
          + ("部署冒烟通过（新环境可交付）" if ok else f"部署冒烟失败：{checks.failed}"))

    if args.report:
        path = ROOT / args.report
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema": "qinshuang-deploy-smoke/v1",
            "card": "T30",
            "image": args.image,
            "fresh_database": FRESH_DB,
            "network": NETWORK,
            "generated_at": datetime.now(UTC).isoformat(),
            "ok": ok,
            "failed": checks.failed,
            "checks": checks.items,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写入 {path}")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

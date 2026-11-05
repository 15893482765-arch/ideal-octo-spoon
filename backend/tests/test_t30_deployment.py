"""T30 Q176/Q177/Q178：迁移回滚演练、全新环境部署、UAT 签署门禁。

与单元测试的区别：这三个用例验证的是**可交付性**（M5 里程碑的部署/验收/审计标准），
全部对真实 PostgreSQL / 真实 Docker 操作，不使用内存替身：

- Q176：在一次性空库上跑 `alembic upgrade head`（含扩展创建）→ 重复 upgrade 幂等
        → `downgrade base` 清空业务表 → 再次 `upgrade head` 复原；
- Q177：用生产镜像执行 `scripts/deploy_smoke.py`（全新库 → 迁移 → 起容器 → 健康检查
        → 引导管理员登录 → 零演示数据 → 迁移版本对齐）；
- Q178：UAT 签署记录必须存在且自洽（A1-A28 全通过 + 业务负责人签署 + 与运行报告一致）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

BACKEND = Path(__file__).resolve().parent.parent
ROOT = BACKEND.parent

PG_BASE = "postgresql+psycopg2://qinshuang:change-me-strong-password@localhost:5432"
SCRATCH_DB = "qinshuang_t30_mig"
SCRATCH_SYNC = f"{PG_BASE}/{SCRATCH_DB}"
SCRATCH_ASYNC = SCRATCH_SYNC.replace("postgresql+psycopg2://", "postgresql+asyncpg://")

# 业务表抽样（迁移前后存在性判断；权威清单在 test_migrations.py）
SAMPLE_TABLES = {"users", "merchants", "contracts", "freezer_assets", "task_items"}

# Settings 必填项（迁移过程会加载 Settings）
MIG_ENV = {
    "ENVIRONMENT": "test",
    "SECRET_KEY": "t30-migration-secret-key-min-32-chars-ok",
    "REDIS_URL": "redis://localhost:6379/0",
    "S3_ENDPOINT_URL": "http://localhost:9000",
    "S3_ACCESS_KEY": "minioadmin",
    "S3_SECRET_KEY": "minioadmin",
    "S3_BUCKET": "qinshuang",
}


def _admin_engine():
    return create_engine(f"{PG_BASE}/postgres", isolation_level="AUTOCOMMIT")


def _alembic(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, **MIG_ENV, "DATABASE_URL": SCRATCH_ASYNC}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=900,
    )


def _recreate_scratch_db() -> None:
    eng = _admin_engine()
    with eng.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {SCRATCH_DB}"))
    eng.dispose()


def _drop_scratch_db() -> None:
    eng = _admin_engine()
    with eng.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
    eng.dispose()


def _tables() -> set[str]:
    eng = create_engine(SCRATCH_SYNC, future=True)
    try:
        return set(inspect(eng).get_table_names())
    finally:
        eng.dispose()


# ===========================================================================
# Q176 迁移 / 回滚演练（一次性空库，真实执行）
# ===========================================================================


def test_q176_migration_rollback_and_reapply_on_empty_database() -> None:
    _recreate_scratch_db()
    try:
        # 空库：无业务表
        assert not (SAMPLE_TABLES & _tables()), "全新库不应有业务表"

        # 首次升级：迁移必须自建 postgis/pgcrypto 扩展（新环境零人工前置）
        up1 = _alembic("upgrade", "head")
        assert up1.returncode == 0, f"upgrade 失败: {up1.stderr[-1200:]}"
        after_up = _tables()
        assert SAMPLE_TABLES.issubset(after_up), f"缺表: {SAMPLE_TABLES - after_up}"

        eng = create_engine(SCRATCH_SYNC, future=True)
        try:
            with eng.connect() as conn:
                exts = {
                    r[0]
                    for r in conn.execute(text(
                        "SELECT extname FROM pg_extension WHERE extname IN ('postgis','pgcrypto')"
                    ))
                }
                version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        finally:
            eng.dispose()
        assert {"postgis", "pgcrypto"} <= exts, f"迁移应创建扩展，实际 {exts}"
        head = version

        # 重复 upgrade：幂等，不报错、版本不变
        up2 = _alembic("upgrade", "head")
        assert up2.returncode == 0, f"重复 upgrade 应为空操作: {up2.stderr[-800:]}"

        # 回滚到 base：业务表清空（扩展表保留）
        down = _alembic("downgrade", "base")
        assert down.returncode == 0, f"downgrade 失败: {down.stderr[-1200:]}"
        after_down = _tables()
        assert not (SAMPLE_TABLES & after_down), f"回滚后仍有业务表: {SAMPLE_TABLES & after_down}"

        # 再次升级：可重复执行，版本回到 head
        up3 = _alembic("upgrade", "head")
        assert up3.returncode == 0, f"二次 upgrade 失败: {up3.stderr[-1200:]}"
        assert SAMPLE_TABLES.issubset(_tables())
        eng = create_engine(SCRATCH_SYNC, future=True)
        try:
            with eng.connect() as conn:
                assert conn.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one() == head
        finally:
            eng.dispose()
    finally:
        _drop_scratch_db()


# ===========================================================================
# Q177 全新环境部署（真实 Docker + 真实镜像）
# ===========================================================================


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    proc = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=60)
    return proc.returncode == 0


@pytest.mark.skipif(not _docker_ready(), reason="本机无可用 Docker（部署冒烟需真实容器）")
def test_q177_fresh_environment_deployment_smoke(tmp_path: Path) -> None:
    report = tmp_path / "deploy-smoke.json"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "deploy_smoke.py"),
         "--image", "qinshuang-backend:t30", "--report", str(report)],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=1800,
    )
    assert proc.returncode == 0, f"部署冒烟失败:\n{proc.stdout[-2000:]}\n{proc.stderr[-1500:]}"

    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["ok"] is True and data["failed"] == []
    ids = {c["id"] for c in data["checks"]}
    required = {
        "fresh_db_is_empty", "migration_applied", "container_non_root",
        "healthz_ready", "bootstrap_admin_login", "first_login_forces_password_change",
        "bootstrap_admin_is_unique", "no_demo_data_in_fresh_env", "migration_at_head",
    }
    assert required.issubset(ids), f"部署冒烟缺少检查项: {required - ids}"


# ===========================================================================
# Q178 UAT 签署记录门禁
# ===========================================================================


def test_q178_uat_signoff_record_is_complete_and_consistent() -> None:
    """签署记录必须存在、字段齐全，且与真实 UAT 运行报告一致。"""
    signoff_path = ROOT / "docs" / "uat" / "uat-signoff.md"
    run_path = ROOT / "docs" / "uat" / "uat-run.json"
    assert signoff_path.exists(), "缺少 UAT 签署记录 docs/uat/uat-signoff.md"
    assert run_path.exists(), (
        "缺少 UAT 运行报告 docs/uat/uat-run.json（先跑 scripts/uat_full_chain.py）"
    )

    run = json.loads(run_path.read_text(encoding="utf-8"))
    assert run["failed_steps"] == [], f"UAT 存在失败步骤，不可签署: {run['failed_steps']}"
    assert run["passed"] == run["total"] and run["total"] >= 16

    text_body = signoff_path.read_text(encoding="utf-8")
    # 签署必填项（缺任一项视为未签署）
    for token in ("业务负责人", "签署日期", "运行报告", "A1", "A28", "结论"):
        assert token in text_body, f"签署记录缺少必填项: {token}"
    # 记录必须引用真实生成的运行时间戳，避免手写“假通过”
    assert run["generated_at"] in text_body, "签署记录必须引用真实 UAT 运行时间戳"

    # 矩阵必须完整覆盖 A1-A28 且无悬空
    matrix = json.loads(
        (ROOT / "docs" / "uat" / "acceptance-matrix.json").read_text(encoding="utf-8")
    )
    ids = [s["id"] for s in matrix["scenarios"]]
    assert ids == [f"A{i}" for i in range(1, 29)]

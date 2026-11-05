#!/usr/bin/env python3
"""数据库备份 / 恢复 / 保留 / 恢复演练 CLI（Q171/Q172）。

由调度器（生产为 k8s CronJob / 运维 cron；本地可用任务计划）每日调用：
    uv run python scripts/backup.py backup      # 每日备份 + 30 天保留清理
    uv run python scripts/backup.py drill       # 恢复演练：恢复到 DR 目标库并校验 RPO/RTO
    uv run python scripts/backup.py list
    uv run python scripts/backup.py restore --target-dsn postgresql://.../qinshuang_dr

安全：
- 数据库口令只经 PGPASSWORD 环境变量传给 pg_dump/psql，绝不进 argv（进程列表不可见）；
- 恢复前强制 sha256 校验，损坏即 BACKUP_CORRUPT，绝不静默成功；
- 备份失败不产生半成品文件、不污染清单。

环境变量：
    DATABASE_URL            源库（必填，postgresql://user:pass@host:port/db）
    BACKUP_DIR              备份目录（默认 /data/backups/qinshuang，本地默认 ./.backups）
    BACKUP_RETENTION_DAYS   保留窗口（默认 30，对应 PRD 30 天恢复窗口）
    DR_DATABASE_URL         drill 恢复目标库（演练库，默认取 <源库>_dr）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.backup_service import (  # noqa: E402
    BackupService,
    SubprocessBackupRunner,
    evaluate_rpo,
    evaluate_rto,
)

RPO_HOURS = 24
RTO_HOURS = 4


def _dsn() -> str:
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        print("ERROR: 未设置 DATABASE_URL", file=sys.stderr)
        raise SystemExit(2)
    return dsn


def _backup_dir() -> Path:
    return Path(os.environ.get("BACKUP_DIR", "./.backups"))


def _service() -> BackupService:
    return BackupService(
        database_url=_dsn(),
        backup_dir=_backup_dir(),
        runner=SubprocessBackupRunner(),
    )


def cmd_backup(_args: argparse.Namespace) -> int:
    svc = _service()
    record = svc.create_backup()
    retention_days = int(os.environ.get("BACKUP_RETENTION_DAYS", "30"))
    deleted = svc.apply_retention(retention_days=retention_days)
    print(json.dumps({
        "action": "backup", "ok": True, "file": record.filename,
        "size_bytes": record.size_bytes, "sha256": record.sha256,
        "retention_days": retention_days, "deleted": deleted,
    }, ensure_ascii=False))
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    svc = _service()
    backups = svc.load_set().backups
    for b in sorted(backups, key=lambda x: x.created_at, reverse=True):
        print(f"{b.filename}\t{b.size_bytes} bytes\t{b.created_at.isoformat()}\t{b.sha256[:12]}")
    print(f"共 {len(backups)} 个备份（目录 {_backup_dir()}）")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    svc = _service()
    target = args.target_dsn or _dsn()
    result = svc.restore_latest(target_database_url=target)
    print(json.dumps({"action": "restore", **result}, ensure_ascii=False, default=str))
    return 0


def cmd_drill(_args: argparse.Namespace) -> int:
    """恢复演练：恢复到独立 DR 库，校验 RPO≤24h、RTO≤4h。任一不达标即非零退出。"""
    svc = _service()
    latest = svc.load_set().latest()
    now = datetime.now(UTC)
    target = os.environ.get("DR_DATABASE_URL") or _dr_dsn(_dsn())
    result = svc.restore_latest(target_database_url=target)

    rpo_ok = evaluate_rpo(latest, now=now, rpo_hours=RPO_HOURS)
    rto_ok = evaluate_rto(float(str(result["elapsed_seconds"])), rto_hours=RTO_HOURS)
    report = {
        "action": "drill", "ok": bool(rpo_ok and rto_ok),
        "restored_file": result["restored_file"],
        "elapsed_seconds": result["elapsed_seconds"],
        f"rpo_le_{RPO_HOURS}h": rpo_ok, f"rto_le_{RTO_HOURS}h": rto_ok,
    }
    print(json.dumps(report, ensure_ascii=False))
    if not report["ok"]:
        print("恢复演练未达 RPO/RTO 目标", file=sys.stderr)
        return 1
    return 0


def _dr_dsn(source: str) -> str:
    # postgresql://u:p@h:port/db -> .../db_dr
    if "/" in source:
        head, db = source.rsplit("/", 1)
        db = db.split("?")[0]
        return f"{head}/{db}_dr"
    return source


def main() -> int:
    ap = argparse.ArgumentParser(description="数据库备份/恢复/演练")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("backup", help="执行一次备份并清理超期备份")
    sub.add_parser("list", help="列出备份清单")
    r = sub.add_parser("restore", help="用最近备份恢复到目标库")
    r.add_argument("--target-dsn", default="", help="目标库 DSN（默认源库自身，慎用）")
    sub.add_parser("drill", help="恢复演练并校验 RPO/RTO")

    args = ap.parse_args()
    return {
        "backup": cmd_backup, "list": cmd_list,
        "restore": cmd_restore, "drill": cmd_drill,
    }[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())

"""db:* CLI（T06 / PRD §10.2、§10.4）。

- `python -m app.cli.db reset`         : 在测试/dev 环境重置空库并跑全套迁移
- `python -m app.cli.db seed-check`    : 创建空批次并运行 SeedCheckService
  （dev/test 默认 DEMO，production 仅 PRODUCTION 且拒绝 DEMO）
- `python -m app.cli.db seed-list`     : 列出最近 50 个批次

T06 范围：
- 不解析 Excel / CSV（业务演示数据未到）；
- 仅运行 alembic upgrade head + SeedCheckService 空批次；
- 与 `app.api.v1.seed` 路由共用同一份 SeedCheckService 代码。
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from typing import Any

from app.core.config import get_settings
from app.db.session import session_scope
from app.services.seed_check_service import (
    SeedCheckService,
    build_empty_check_for_environment,
)


def _system_user_id() -> uuid.UUID:
    """CLI 系统提交者：固定空 UUID，AuditService 接受（actor_id nullable）。"""
    return uuid.UUID(int=0)


def _ensure_system_user(session) -> uuid.UUID:
    """取或创建一个 CLI 提交者用户（不进入 Admin 流程；仅占位）。

    T06 简化：使用一个 SYSTEM_CLI 角色 + 单一占位用户，确保 SeedBatch.submitted_by_id
    FK 成立。生产环境禁止使用本入口。
    """
    from app.core.security import hash_password
    from app.models.enums import RoleCode, UserStatus
    from app.models.user import Role, User

    role = session.query(Role).filter(Role.role_code == RoleCode.SYSTEM_ADMIN.value).first()
    if role is None:
        role = Role(
            code=f"cli-system-admin-{uuid.uuid4().hex[:6]}",
            name="CLI System Admin",
            role_code=RoleCode.SYSTEM_ADMIN.value,
            dataset_type="PRODUCTION",
            dataset_id=uuid.uuid4(),
        )
        session.add(role)
        session.flush()
    user = session.query(User).filter(User.username == "cli-system").first()
    if user is None:
        user = User(
            username="cli-system",
            display_name="CLI System",
            role_id=role.id,
            status=UserStatus.ACTIVE.value,
            must_change_password=False,
            password_hash=hash_password("cli-not-for-prod"),
            dataset_type="PRODUCTION",
            dataset_id=role.dataset_id,
        )
        session.add(user)
        session.flush()
    return user.id


def cmd_reset(_: argparse.Namespace) -> int:
    """重置：drop+create schema → alembic upgrade head。T06 简化为"跑迁移"。"""
    from alembic import command
    from alembic.config import Config

    settings = get_settings()
    if settings.environment == "production":
        print("[db:reset] 生产环境禁止执行 db:reset", file=sys.stderr)
        return 2
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", settings.database_url_sync)
    print(f"[db:reset] 环境={settings.environment} 跑 alembic upgrade head")
    command.upgrade(cfg, "head")
    print("[db:reset] 完成")
    return 0


def cmd_seed_check(args: argparse.Namespace) -> int:
    settings = get_settings()
    with session_scope() as session:
        submitter = _ensure_system_user(session)
        session.flush()
        batch = build_empty_check_for_environment(
            session,
            environment=settings.environment,
            submitted_by_id=submitter,
        )
        session.flush()
        result = SeedCheckService(session).run_check(batch)
        report: dict[str, Any] = {
            "batch_id": str(result.id),
            "dataset_type": result.dataset_type,
            "dataset_id": str(result.dataset_id),
            "status": result.status,
            "rows_rejected": result.rows_rejected,
            "negative_findings": result.negative_findings,
            "error_log": result.error_log,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if result.status == "FAILED":
            return 1
        return 0


def cmd_seed_list(args: argparse.Namespace) -> int:
    with session_scope() as session:
        batches = SeedCheckService(session).list_batches(limit=args.limit)
        rows = [
            {
                "id": str(b.id),
                "dataset_type": b.dataset_type,
                "seed_type": b.seed_type,
                "status": b.status,
                "rows_rejected": b.rows_rejected,
                "created_at": b.created_at.isoformat() if b.created_at else None,
            }
            for b in batches
        ]
        print(json.dumps({"batches": rows}, ensure_ascii=False, indent=2))
        return 0


def cmd_demo_accounts(args: argparse.Namespace) -> int:
    """把账号工作簿播种为可直接登录的演示账号（幂等；生产环境禁止）。"""
    from pathlib import Path

    from app.services import demo_account_seeder as seeder

    settings = get_settings()
    if settings.environment == "production":
        print("[db:demo-accounts] 生产环境禁止执行", file=sys.stderr)
        return 2
    raw = args.file or settings.demo_account_workbook or seeder.DEFAULT_WORKBOOK
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    with session_scope() as session:
        logins = seeder.DemoAccountSeeder(session).seed(path=path, password=args.password)
    print(
        json.dumps(
            {
                "workbook": str(path),
                "password": args.password or seeder.DEFAULT_LOGIN_PASSWORD,
                "count": len(logins),
                "logins": logins,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_demo_smoke(args: argparse.Namespace) -> int:
    """显式 demo 批次预检（CLI 给 Q030 单元测试外加端到端探针使用）。"""
    settings = get_settings()
    if settings.environment == "production":
        print("[db:demo-smoke] 生产环境禁止", file=sys.stderr)
        return 2
    with session_scope() as session:
        submitter = _ensure_system_user(session)
        session.flush()
        svc = SeedCheckService(session)
        batch = svc.create_empty_batch(submitted_by_id=submitter, dataset_type="DEMO")
        session.flush()
        result = svc.run_check(batch)
        print(
            json.dumps(
                {"status": result.status, "rows_rejected": result.rows_rejected},
                ensure_ascii=False,
            )
        )
        return 0


def cmd_demo_business(args: argparse.Namespace) -> int:
    """播种演示业务全链路数据（合同/冰柜/排班/巡访/补货/地推/督导/采购/绩效）。

    幂等：各段以确定性编码为锚点，重复执行只补齐缺失段。
    """
    settings = get_settings()
    if settings.environment == "production":
        print("[db:demo-business] 生产环境禁止", file=sys.stderr)
        return 2
    from app.services.demo_business_seeder import DemoBusinessSeeder

    with session_scope() as session:
        results = DemoBusinessSeeder(session).seed()
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(item["status"] == "OK" for item in results) else 1


def cmd_repair_schedule(args: argparse.Namespace) -> int:
    """修复排班与巡访任务负责人不一致（历史调拨只转任务不改排班）。"""
    from app.services.schedule_service import ScheduleService

    with session_scope() as session:
        result = ScheduleService(session).reconcile_schedule_assignees(
            apply=not args.dry_run
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result["conflicts"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli.db")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("reset", help="跑 alembic upgrade head（非生产）").set_defaults(
        func=cmd_reset
    )
    sub.add_parser("seed-check", help="创建空批次并跑 SeedCheck").set_defaults(
        func=cmd_seed_check
    )
    p_list = sub.add_parser("seed-list", help="列出最近批次")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.set_defaults(func=cmd_seed_list)
    sub.add_parser("demo-smoke", help="非生产 demo 预检").set_defaults(func=cmd_demo_smoke)
    p_demo_accounts = sub.add_parser(
        "demo-accounts", help="把账号工作簿播种为可直接登录的演示账号（非生产）"
    )
    p_demo_accounts.add_argument("--file", default=None, help="账号工作簿路径")
    p_demo_accounts.add_argument("--password", default=None, help="统一初始密码（默认取源表列）")
    p_demo_accounts.set_defaults(func=cmd_demo_accounts)
    sub.add_parser(
        "demo-business",
        help="播种演示业务全链路数据（合同/冰柜/排班/巡访/补货/地推/督导/采购/绩效，非生产）",
    ).set_defaults(func=cmd_demo_business)
    p_repair = sub.add_parser(
        "repair-schedule",
        help="修复排班与巡访任务负责人不一致（含 --dry-run 只统计）",
    )
    p_repair.add_argument("--dry-run", action="store_true", help="只统计不写库")
    p_repair.set_defaults(func=cmd_repair_schedule)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

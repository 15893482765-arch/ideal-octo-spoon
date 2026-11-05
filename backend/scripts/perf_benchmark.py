#!/usr/bin/env python3
"""Q169 固定规格性能基准（真实 PostgreSQL 16 + PostGIS）。

对应 PRD §9.1 三条硬指标：
- 工作台首页聚合 P95 ≤ 1000ms；
- 常用列表（商家分页列表）P95 ≤ 800ms；
- 巡访提交（不含文件上传）P95 ≤ 3000ms。

固定口径（任何环境/任何机器都用同一套参数，结果可复现、可横向比较）：
- 数据规模：20000 商家（约 5 万门店主数据的 40%）、1000 巡访任务/风险、20000 合同、
  1 个区域经理（行级范围=本片区）、30 个可提交巡访任务；
- 并发模型：单会话顺序压测（服务层调用，不含 HTTP 框架开销），另测 DB 端
  EXPLAIN (ANALYZE, BUFFERS) 计划；HTTP 并发压测由网关侧 locust 另行实施（见报告说明）；
- 预热：正式采样前每个场景先跑 10 次预热（清应用层 ORM 编译/连接冷启动，但不清 PG buffer）；
- 统计窗口：预热后取 50 次采样的 P50/P95/P99 + 错误率（0 错误为达标前提）；
- 机器规格：脚本自动采集 platform/CPU/内存/PG 版本，写入报告；
- 重复次数：场景重复 3 轮，报告记录 3 轮的 P95（取最大轮）。

用法（在 backend/ 目录）：
    # 用专用基准库，幂等重建（create_all）
    $env:Q169_DSN="postgresql+psycopg2://qinshuang:pwd@localhost:5432/qinshuang_q169"
    uv run python scripts/perf_benchmark.py --report ../docs/performance/q169-benchmark.json

无 PG 或未设置 Q169_DSN 时以退出码 3 跳过（CI 据此可选执行，不伪造结果）。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

# 允许从 backend/ 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from geoalchemy2 import WKTElement  # noqa: E402
from sqlalchemy import create_engine, func, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.metrics import percentile  # noqa: E402
from app.core.permissions import ActorContext  # noqa: E402
from app.models import Base  # noqa: E402  # 触发全部模型注册
from app.models.approval import TaskItem  # noqa: E402
from app.models.contract import Contract  # noqa: E402
from app.models.core import Brand, Organization, Region  # noqa: E402
from app.models.file_object import FileObject  # noqa: E402
from app.models.merchant import Merchant  # noqa: E402
from app.models.merchant_assignment import MerchantAssignment  # noqa: E402
from app.models.product import Sku  # noqa: E402
from app.models.risk import RiskItem  # noqa: E402
from app.models.user import Role, User, UserRegionBrandAuth  # noqa: E402
from app.schemas.merchant import MerchantListQuery  # noqa: E402
from app.schemas.visit import (  # noqa: E402
    VisitPhotoInput,
    VisitSalesLineInput,
    VisitSubmit,
    VisitTaskCreate,
)
from app.services.merchant_service import MerchantService  # noqa: E402
from app.services.visit_service import VisitService  # noqa: E402
from app.services.workbench_service import WorkbenchService  # noqa: E402

# 上海人民广场附近（与 T11 测试一致）：店员打卡点 ≈111m，在 200m 围栏内
MERCHANT_LNG_LAT = (121.4737, 31.2304)
NEAR_LNG_LAT = (121.4737, 31.2314)

# ---- 固定规格常量（勿随机器调整） ----
N_MERCHANTS = 20_000
N_CONTRACTS = 20_000
N_TASKS = 1_000
N_RISKS = 1_000
N_SUBMIT_TASKS = 60  # ≥ SAMPLE_ROUNDS，保证每轮都有全新任务可提交（一任务仅一条正式记录）
WARMUP_ROUNDS = 10
SAMPLE_ROUNDS = 50
REPEAT_RUNS = 3
BATCH = 2_000

THRESHOLDS_MS = {
    "workbench_home": 1000.0,
    "merchant_list": 800.0,
    "visit_submit": 3000.0,
}


def machine_spec() -> dict:
    import multiprocessing

    mem = None
    try:
        import psutil  # type: ignore[import-untyped]

        mem = psutil.virtual_memory().total
    except Exception:  # noqa: BLE001
        mem = None
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or platform.machine(),
        "cpu_count": multiprocessing.cpu_count(),
        "memory_bytes": mem,
        "measured_at_utc": datetime.now(UTC).isoformat(),
    }


def pg_version(session: Session) -> str:
    return str(session.execute(text("show server_version")).scalar_one())


def reset_schema(engine) -> None:
    # 幂等：专用基准库，drop+create 全部 ORM 表，并按需创建扩展
    with engine.begin() as conn:
        # postgis：地理点；btree_gist：EXCLUDE USING gist 的 uuid 等值操作类（归属区间排他约束）
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def seed(session: Session) -> dict:
    ds = uuid4()
    org = Organization(code=f"org-{uuid4().hex[:8]}", name="基准组织",
                       dataset_type="PRODUCTION", dataset_id=ds)
    session.add(org)
    session.flush()
    brand = Brand(code=f"brand-{uuid4().hex[:8]}", name="沁爽",
                  dataset_type="PRODUCTION", dataset_id=ds)
    region = Region(code="REG-BENCH", name="基准片区", org_id=org.id,
                    dataset_type="PRODUCTION", dataset_id=ds)
    session.add_all([brand, region])
    session.flush()

    role_mgr = Role(code=f"role-{uuid4().hex[:8]}", name="经理",
                    role_code="REGIONAL_MANAGER", dataset_type="PRODUCTION", dataset_id=ds)
    role_rep = Role(code=f"role-{uuid4().hex[:8]}", name="业务员",
                    role_code="SALES_REP", dataset_type="PRODUCTION", dataset_id=ds)
    session.add_all([role_mgr, role_rep])
    session.flush()

    from app.core.security import hash_password

    mgr = User(username="bench-mgr", display_name="经理", role_id=role_mgr.id,
               status="ACTIVE", must_change_password=False,
               password_hash=hash_password("bench-not-for-prod"),
               dataset_type="PRODUCTION", dataset_id=ds)
    rep = User(username="bench-rep", display_name="业务员", role_id=role_rep.id,
               status="ACTIVE", must_change_password=False,
               password_hash=hash_password("bench-not-for-prod"),
               dataset_type="PRODUCTION", dataset_id=ds)
    session.add_all([mgr, rep])
    session.flush()
    session.add_all([
        UserRegionBrandAuth(user_id=mgr.id, region_id=region.id, brand_id=brand.id),
        UserRegionBrandAuth(user_id=rep.id, region_id=region.id, brand_id=brand.id),
    ])
    session.flush()

    grades = ["A", "B", "C"]
    merchants: list[Merchant] = []
    for i in range(N_MERCHANTS):
        m = Merchant(
            code=f"MB{i:06d}",
            name=f"基准便利店{i:06d}",
            status="ACTIVE",
            grade=grades[i % 3],
            region_id=region.id,
            brand_id=brand.id,
            address=f"基准路 {i} 号",
            dataset_type="PRODUCTION",
            dataset_id=ds,
        )
        # 前 N_SUBMIT_TASKS 家作为可提交巡访门店：带 PostGIS 坐标（人民广场）
        if i < N_SUBMIT_TASKS:
            m.location = WKTElement(  # type: ignore[assignment]
                f"POINT({MERCHANT_LNG_LAT[0]} {MERCHANT_LNG_LAT[1]})", srid=4326
            )
        merchants.append(m)
        if len(merchants) == BATCH:
            session.add_all(merchants)
            session.flush()
            merchants = []
    if merchants:
        session.add_all(merchants)
        session.flush()

    # 合同（与商家同规模，仅填列表/聚合可能触及的必填列）
    mids = [r[0] for r in session.execute(
        text("SELECT id FROM merchants ORDER BY code LIMIT :n"), {"n": N_CONTRACTS}
    ).all()]
    contracts = [
        Contract(
            merchant_id=mid, contract_no=f"CB{i:06d}", title="基准陈列协议",
            party_name="基准商贸", status="EFFECTIVE", signed_at=date(2026, 1, 1),
            effective_from=date(2026, 1, 1), expire_at=date(2026, 12, 31),
            commission_rate=Decimal("3.5"), display_subsidy_amount=100000,
            display_threshold_pct=50, current_version=1,
            dataset_type="PRODUCTION", dataset_id=ds,
        )
        for i, mid in enumerate(mids)
    ]
    for i in range(0, len(contracts), BATCH):
        session.add_all(contracts[i:i + BATCH])
        session.flush()

    # 1000 任务 + 1000 风险（落在本片区商家上，工作台聚合要扫）
    tasks = [
        TaskItem(task_kind="VISIT_PLANNED", title=f"基准待办{i}", status="OPEN",
                 assignee_id=mgr.id, created_by_id=mgr.id,
                 related_object_type="MERCHANT", related_object_id=mids[i % len(mids)])
        for i in range(N_TASKS)
    ]
    for i in range(0, len(tasks), BATCH):
        session.add_all(tasks[i:i + BATCH])
        session.flush()

    rule_codes = ["RK-1", "RK-2", "RK-3", "RK-4", "RK-5", "RK-6", "RK-7"]
    risks = [
        RiskItem(dataset_type="PRODUCTION", dataset_id=ds,
                 rule_code=rule_codes[i % 7], object_type="MERCHANT",
                 object_id=mids[i % len(mids)], period=f"2026-W{1 + (i % 40):02d}",
                 status="OPEN", severity="HIGH",
                 first_seen_at=datetime(2026, 12, 1, tzinfo=UTC),
                 last_seen_at=datetime(2026, 12, 1, tzinfo=UTC))
        for i in range(N_RISKS)
    ]
    for i in range(0, len(risks), BATCH):
        session.add_all(risks[i:i + BATCH])
        session.flush()

    # 业务员主责分配（巡访提交前置），valid_range 用 SQLAlchemy tstzrange 构造
    epoch = datetime(2000, 1, 1, tzinfo=UTC)
    assigns = [
        MerchantAssignment(
            merchant_id=mids[i], owner_user_id=rep.id, assignment_type="PRIMARY",
            valid_range=func.tstzrange(epoch, None, "[)"),
            dataset_type="PRODUCTION", dataset_id=ds,
        )
        for i in range(N_SUBMIT_TASKS)
    ]
    session.add_all(assigns)
    session.flush()

    # 巡访提交前置：1 个在范围内 SKU + 1 张已扫描干净的门店照片 FileObject
    sku = Sku(
        code=f"SKU-BENCH-{uuid4().hex[:6]}", name="基准 500ml", brand_id=brand.id,
        capacity_ml=500, suggested_price_fen=300, case_spec=Decimal("24"),
        dataset_type="PRODUCTION", dataset_id=ds,
    )
    photo = FileObject(
        object_key=f"PHOTO/{uuid4().hex}/storefront.jpg",
        original_filename="storefront.jpg", content_type="image/jpeg",
        size_bytes=204800, sha256=uuid4().hex + uuid4().hex, biz_type="PHOTO",
        owner_user_id=rep.id, scan_status="CLEAN",
        dataset_type="PRODUCTION", dataset_id=ds,
    )
    session.add_all([sku, photo])
    session.commit()

    return {
        "ds": ds, "region_id": region.id, "brand_id": brand.id,
        "mgr_id": mgr.id, "rep_id": rep.id, "merchant_ids": mids,
        "sku_id": sku.id, "photo_id": photo.id,
    }


def mgr_actor(ctx: dict) -> ActorContext:
    return ActorContext(user_id=ctx["mgr_id"], role_code="REGIONAL_MANAGER",
                        region_ids=frozenset({ctx["region_id"]}), brand_ids=frozenset())


def rep_actor(ctx: dict) -> ActorContext:
    return ActorContext(user_id=ctx["rep_id"], role_code="SALES_REP",
                        region_ids=frozenset({ctx["region_id"]}), brand_ids=frozenset())


def time_once(fn) -> tuple[float, bool]:
    t0 = time.perf_counter()
    ok = True
    try:
        fn()
    except Exception:  # noqa: BLE001
        ok = False
    return (time.perf_counter() - t0) * 1000.0, ok


def measure(fn, *, warmup=WARMUP_ROUNDS, samples=SAMPLE_ROUNDS) -> dict:
    for _ in range(warmup):
        fn()
    latencies: list[float] = []
    errors = 0
    for _ in range(samples):
        ms, ok = time_once(fn)
        latencies.append(ms)
        if not ok:
            errors += 1
    ordered = sorted(latencies)
    return {
        "p50_ms": round(percentile(ordered, 50), 2),
        "p95_ms": round(percentile(ordered, 95), 2),
        "p99_ms": round(percentile(ordered, 99), 2),
        "max_ms": round(max(latencies), 2),
        "samples": samples,
        "errors": errors,
        "error_rate": round(errors / samples, 4),
    }


def explain_plan(session: Session, sql: str, params: dict | None = None) -> dict:
    rows = session.execute(
        text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}"), params or {}
    ).scalar_one()
    top = rows[0]
    node = top.get("Plan") or top.get("plan")
    return {
        "node_type": node.get("Node Type"),
        "total_runtime_ms": round(node.get("Actual Total Time", 0.0), 3),
        "actual_rows": node.get("Actual Rows"),
        "planning_time_ms": round(top.get("Planning Time", 0.0), 3),
        "execution_time_ms": round(top.get("Execution Time", 0.0), 3),
        "seq_scan": "Seq Scan" in json.dumps(rows, ensure_ascii=False),
        "plan_top": node.get("Plans", [{}])[0].get("Node Type") if node.get("Plans") else None,
    }


def build_visit_tasks(session: Session, ctx: dict) -> list:
    """为巡访提交场景造 N_SUBMIT_TASKS 个全新任务（每轮运行重建，保证可重复提交）。"""
    svc = VisitService(session)
    tasks = []
    for i in range(N_SUBMIT_TASKS):
        t = svc.create_task(
            rep_actor(ctx),
            VisitTaskCreate(
                merchant_id=ctx["merchant_ids"][i],
                planned_date=date(2026, 12, 1 + (i % 20)),
            ),
        )
        tasks.append(t)
    session.commit()
    return tasks


def submit_payload(ctx: dict, run: int, idx: int) -> VisitSubmit:
    """构造一次真实巡访提交（近点+照片+1 SKU 销量行；不含文件上传，照片已是现成 file_id）。"""
    return VisitSubmit(
        lng=NEAR_LNG_LAT[0], lat=NEAR_LNG_LAT[1], accuracy_meters=12,
        display_pct=60,
        photos=[VisitPhotoInput(file_id=ctx["photo_id"], photo_kind="STOREFRONT")],
        sales_lines=[
            VisitSalesLineInput(
                sku_id=ctx["sku_id"], reported_boxes=Decimal("5.0"), feedback=None,
            )
        ],
        idempotency_key=f"bench-sub-{run}-{idx}-{uuid4().hex[:8]}",
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.environ.get("Q169_DSN", ""))
    ap.add_argument("--report", default="")
    ap.add_argument("--keep", action="store_true", help="保留基准库数据（默认跑完保留，便于复查）")
    args = ap.parse_args()

    if not args.dsn:
        print("SKIP: 未设置 Q169_DSN（需要真实 PostgreSQL16/PostGIS），不伪造基准结果")
        return 3

    engine = create_engine(args.dsn, future=True)
    report: dict = {
        "scenario": "Q169",
        "spec": {
            "n_merchants": N_MERCHANTS, "n_contracts": N_CONTRACTS,
            "n_tasks": N_TASKS, "n_risks": N_RISKS, "n_submit_tasks": N_SUBMIT_TASKS,
            "warmup_rounds": WARMUP_ROUNDS, "sample_rounds": SAMPLE_ROUNDS,
            "repeat_runs": REPEAT_RUNS,
            "concurrency_model": "single-session sequential service-layer calls",
        },
        "thresholds_ms": THRESHOLDS_MS,
        "machine": machine_spec(),
    }

    try:
        with Session(engine) as setup_session:
            report["postgres_version"] = pg_version(setup_session)
            print("重置并创建 schema ...")
            reset_schema(engine)
            print(f"灌入固定规模数据（{N_MERCHANTS} 商家 / {N_CONTRACTS} 合同）...")
            t0 = time.perf_counter()
            ctx = seed(setup_session)
            print(f"造数完成，用时 {time.perf_counter() - t0:.1f}s")

        results: dict[str, list] = {k: [] for k in THRESHOLDS_MS}
        plans: dict[str, dict] = {}

        for run in range(REPEAT_RUNS):
            with Session(engine) as session:
                actor = mgr_actor(ctx)
                wb = WorkbenchService(session)
                ms = MerchantService(session)
                list_q = MerchantListQuery(page=1, page_size=20, status="ACTIVE")

                results["workbench_home"].append(
                    measure(lambda wb=wb, actor=actor: wb.home(actor, period="2026-12"))
                )
                results["merchant_list"].append(
                    measure(lambda ms=ms, actor=actor, q=list_q: ms.list_merchants(actor, q))
                )

                # 巡访提交：为每轮造全新任务（一次任务仅允许一条正式记录）
                submit_tasks = build_visit_tasks(session, ctx)
                vsvc = VisitService(session)

                # 预热 10 个任务（不计入统计），再采样 SAMPLE_ROUNDS 个
                for i in range(WARMUP_ROUNDS):
                    vsvc.submit(
                        rep_actor(ctx), submit_tasks[i].id, submit_payload(ctx, run, i)
                    )
                lat, errs = [], 0
                for i in range(WARMUP_ROUNDS, WARMUP_ROUNDS + SAMPLE_ROUNDS):
                    payload = submit_payload(ctx, run, i)

                    def _do(i=i, payload=payload, vsvc=vsvc, tasks=submit_tasks):
                        vsvc.submit(rep_actor(ctx), tasks[i].id, payload)

                    ms_v, ok = time_once(_do)
                    lat.append(ms_v)
                    errs += 0 if ok else 1
                ordered = sorted(lat)
                results["visit_submit"].append({
                    "p50_ms": round(percentile(ordered, 50), 2),
                    "p95_ms": round(percentile(ordered, 95), 2),
                    "p99_ms": round(percentile(ordered, 99), 2),
                    "max_ms": round(max(lat), 2),
                    "samples": SAMPLE_ROUNDS, "errors": errs,
                    "error_rate": round(errs / SAMPLE_ROUNDS, 4),
                })

            print(f"第 {run + 1}/{REPEAT_RUNS} 轮完成")

        # EXPLAIN：列表分页查询的真实计划
        with Session(engine) as session:
            plans["merchant_list_sql"] = explain_plan(
                session,
                "SELECT code, name, grade FROM merchants "
                "WHERE status = 'ACTIVE' ORDER BY code LIMIT 20",
            )

        # 取每场景 3 轮中最差（最大）P95 作为裁决值
        verdict = {}
        all_pass = True
        for key, runs in results.items():
            worst_p95 = max(r["p95_ms"] for r in runs)
            worst_err = max(r["error_rate"] for r in runs)
            passed = worst_p95 <= THRESHOLDS_MS[key] and worst_err == 0.0
            all_pass = all_pass and passed
            verdict[key] = {"worst_p95_ms": worst_p95,
                            "threshold_ms": THRESHOLDS_MS[key],
                            "worst_error_rate": worst_err, "pass": passed}

        report["runs"] = results
        report["explain"] = plans
        report["verdict"] = verdict
        report["overall_pass"] = all_pass

        if args.report:
            out = Path(args.report)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"报告已写入 {out}")

        print(json.dumps(verdict, ensure_ascii=False, indent=2))
        print("OVERALL:", "PASS" if all_pass else "FAIL")
        return 0 if all_pass else 1
    finally:
        if not args.keep:
            pass  # 默认保留基准库便于复查；加 --keep 仅作显式语义占位
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())

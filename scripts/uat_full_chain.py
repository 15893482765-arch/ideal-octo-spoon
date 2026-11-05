#!/usr/bin/env python3
"""T30 Q174/Q175/Q186：全链路 UAT 运行器（PRD 第十一章 A1-A28）。

本脚本是**薄运行器**：不重复实现断言，而是按业务链路顺序逐步执行既有测试节点
（真实 Service + 真实 PostgreSQL），每步独立收集结果，输出可签署的 UAT 报告。

链路顺序（PRD §11 UAT 场景）：
    管理员建号 → Excel 导入 → 商家/合同 → 投柜 → 排班/巡访 → 补货/采购 → 撤柜
    → 绩效/工资 → 报表/导出 → MCP → 风险闭环 → 续签待办 → 样品台账 → 押金收退

用法：
    python scripts/uat_full_chain.py                # 跑全部步骤
    python scripts/uat_full_chain.py --step A25_risk_closure
    python scripts/uat_full_chain.py --report docs/uat/uat-run.json
退出码：任一步骤失败即 1；全部通过为 0（用于上线门禁）。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"

# 步骤标记：docs/uat/acceptance-matrix.json 通过 'scripts/uat_full_chain.py#<step_id>' 引用
# 这些字符串是矩阵与脚本的契约，重命名必须同步矩阵。


@dataclass
class Step:
    step_id: str
    title: str
    nodes: list[str]
    prd: str
    kind: str = "test"  # test=执行 pytest 节点；script=外部命令
    command: str = ""
    extra: dict = field(default_factory=dict)


CHAIN = "tests/test_t30_full_chain_uat.py"

STEPS: list[Step] = [
    Step(
        "A1_admin_account",
        "管理员建号 + 无自助注册 + 首次登录强制改密",
        [
            "tests/test_auth_rbac.py::test_q010_no_register_route",
            "tests/test_auth_rbac.py::test_q011_first_time_change_password",
            "tests/test_auth_rbac.py::test_q012_role_module_matrix",
        ],
        "A1 / §2.2 §2.4",
    ),
    Step(
        "A20_excel_import",
        "Excel 主数据导入：预检、幂等、密码列忽略、厂商待激活",
        [
            "tests/test_t30_master_import.py::test_q174_a20_preflight_blocks_unknown_region",
            "tests/test_t30_master_import.py::test_q174_a20_preflight_blocks_duplicate_login_and_sku",
            "tests/test_t30_master_import.py::test_q174_a20_happy_path_batch_succeeds_and_traceable",
            "tests/test_t30_master_import.py::test_q174_a21_same_file_hash_is_idempotent",
            "tests/test_t30_master_import.py::test_q174_a22_password_column_ignored_and_never_persisted",
            "tests/test_t30_master_import.py::test_q174_a23_supplier_import_is_pending_activation",
        ],
        "A20-A23 / §3",
    ),
    Step(
        "A2_merchant_contract",
        "商家建档 + 合同生效（起草→提交→批准）+ 行级范围不可越权",
        [
            "tests/test_t09_contract.py::test_q043_freezer_requires_effective_contract",
            "tests/test_auth_rbac.py::test_q013_sales_rep_cross_region_forbidden",
            "tests/test_t08_merchant.py::test_q039_sales_rep_only_assigned",
        ],
        "A2 A3 / §4.1 §4.2",
    ),
    Step(
        "A4_freezer_deploy",
        "投柜：未生效合同被拒；生效投放写流水、押金收缴",
        [
            "tests/test_t10_freezer.py::test_q048_deploy_requires_effective_contract",
            "tests/test_t10_freezer.py::test_q049_deployment_writes_movements",
        ],
        "A3 A4 A28 / §4.4",
    ),
    Step(
        "A5_schedule_visit",
        "周排班发布 + 巡访提交（近点/照片/陈列阈值/多 SKU）",
        [
            "tests/test_t13_schedule_conflict.py::test_q071_one_conflict_whole_batch_zero_writes",
            "tests/test_t11_visit.py::test_q055_visit_within_200m_is_valid",
            "tests/test_t11_visit.py::test_q056_visit_beyond_200m_is_abnormal_pending",
            "tests/test_t12_visit_submission.py::test_q062_display_50pct_meets_threshold",
            "tests/test_t12_visit_submission.py::test_q063_multi_sku_sales_precision",
            "tests/test_t12_visit_submission.py::test_q065_replay_same_idempotency_key_does_not_duplicate",
        ],
        "A5 A6 / §4.6 §4.7",
    ),
    Step(
        "A11_replenishment_procurement",
        "巡访联动补货 → 部分满足/超送拒绝 → 采购单",
        [
            "tests/test_t17_replenishment.py::test_q090_partial_fulfillment_keeps_balance",
            "tests/test_t17_replenishment.py::test_q091_over_fulfillment_rejected_zero_write",
            "tests/test_t17_replenishment.py::test_q092_duplicate_callback_idempotent",
            "tests/test_t18_procurement.py::test_t18_demo_sku_rejected_from_production_po",
        ],
        "A11 A24 / §4.9 §4.10",
    ),
    Step(
        "A9_recall_acceptance",
        "合同终止 → 撤柜 → 回收待验收不入库 → 验收入库 +1",
        [
            "tests/test_t09_contract.py::test_q047_termination_keeps_state_until_approved",
            "tests/test_t10_freezer.py::test_q050_pickup_pending_acceptance_does_not_add_stock",
            "tests/test_t10_freezer.py::test_q051_acceptance_in_stock_increments",
            "tests/test_t10_freezer.py::test_termination_creates_recall_task",
        ],
        "A9 / §4.4 §4.5",
    ),
    Step(
        "A12_performance_payroll",
        "绩效（生效合同/有效巡访/确认销量）+ 工资发放与更正",
        [
            "tests/test_t19_performance.py::test_q102_only_effective_contracts",
            "tests/test_t19_performance.py::test_q103_only_valid_visits",
            "tests/test_t19_performance.py::test_q104_only_confirmed_sales",
            "tests/test_t19_performance.py::test_q108_snapshot_lines_traceable",
            "tests/test_t20_payroll.py::test_q109_payroll_composition_exact",
            "tests/test_t20_payroll.py::test_q110_admin_disburse_forbidden",
            "tests/test_t20_payroll.py::test_q112_paid_statement_immutable",
            "tests/test_t20_payroll.py::test_q113_correction_after_paid",
        ],
        "A12 A13 / §4.11 §4.12",
    ),
    Step(
        "A19_report_export",
        "指标/风险/导出：行列权限、签名链接过期、演示数据隔离",
        [
            "tests/test_t21_metrics_risk_export.py::test_q115_ten_metrics_known_data",
            "tests/test_t21_metrics_risk_export.py::test_q117_demo_isolated_and_scope_required",
            "tests/test_t21_metrics_risk_export.py::test_q119_export_permission",
            "tests/test_t21_metrics_risk_export.py::test_q120_export_link_expiry",
            "tests/test_t29_storage_backup.py::test_q168_dataset_whitelist_blocks_demo_file_from_production_path",
        ],
        "A19 A24 / §5 §6",
    ),
    Step(
        "A16_mcp",
        "MCP：只读裁剪 + 写工具预览/幂等 + 厂商字段脱敏",
        [
            "tests/test_t23_mcp_read.py::test_q128_sales_rep_reads_only_own",
            "tests/test_t23_mcp_read.py::test_q130_supplier_redacted_summary",
            "tests/test_t23_mcp_read.py::test_q133_links_do_not_bypass_row_scope",
            "tests/test_t24_mcp_write.py::test_q135_first_write_call_only_returns_preview_and_does_not_execute",
            "tests/test_t24_mcp_write.py::test_q139_same_idempotency_key_replays_first_result",
            "tests/test_t26_ai_audit_resilience.py::test_q152_timeout_retries_once_then_degrades_no_500",
        ],
        "A15-A18 / §7",
    ),
    Step(
        "A25_risk_closure",
        "风险闭环：7天未访入清单置顶 → 补访判定关闭 → 留审计不删除",
        [
            "tests/test_t21_metrics_risk_export.py::test_q184_risk_rules_idempotent_todo_and_degrade",
            "tests/test_t21_metrics_risk_export.py::test_q184_rk1_pinned_in_schedule_advice",
            "tests/test_t27_workbench.py::test_q185_close_risk_via_service_leaves_audit",
            f"{CHAIN}::test_q174_full_chain_admin_to_deposit_refund",
        ],
        "A25 / §6.3",
        extra={"uat_marker": "A25_risk_closure"},
    ),
    Step(
        "A26_renewal_todo",
        "合同到期前 30 天续签评估待办（幂等，窗口外不生成）",
        [
            "tests/test_t09_contract.py::test_q180_renewal_tasks_generated_once_for_both_owners",
            "tests/test_t09_contract.py::test_q180_no_renewal_outside_window",
            "tests/test_t09_contract.py::test_q180_renewal_regenerates_after_expiry_change",
        ],
        "A26 / §4.2",
        extra={"uat_marker": "A26_renewal_todo"},
    ),
    Step(
        "A27_sample_ledger",
        "地推样品台账：领用→消耗/归还/缺失平账，成本不进销量，未还提醒",
        [
            "tests/test_t15_campaign.py::test_q182_sample_ledger_and_return_reminder",
            "tests/test_t15_campaign.py::test_q182_sample_unbalanced_review_rejected",
            "tests/test_t15_campaign.py::test_q182_balanced_no_unreturned_no_reminder",
        ],
        "A27 / §4.8",
        extra={"uat_marker": "A27_sample_ledger"},
    ),
    Step(
        "A28_deposit_refund",
        "押金收退：验收后待办 → 财务退款留凭证 → 不入损益、精确到分",
        [
            "tests/test_t10_freezer.py::test_q181_deposit_collect_refund_flow",
            "tests/test_t10_freezer.py::test_deposit_refund_cannot_exceed_collected",
            "tests/test_t10_freezer.py::test_no_deposit_no_refund_task",
            "tests/test_v12_models.py::test_freezer_deposit_fen_optional_and_nonneg",
        ],
        "A28 / §4.4",
        extra={"uat_marker": "A28_deposit_refund"},
    ),
    Step(
        "Q175_transaction_consistency",
        "任一点失败：事务整体回滚、无半成品、无脏流水",
        [f"{CHAIN}::test_q175_failure_mid_chain_rolls_back_consistently"],
        "§3.5 事务边界 / 红线",
    ),
    Step(
        "Q176_migration_rollback",
        "迁移升级/回滚演练：alembic upgrade head → downgrade -1 → 再升级",
        [],
        "§8 迁移",
        kind="script",
        command=(
            "alembic upgrade head && alembic downgrade -1 && alembic upgrade head"
        ),
    ),
]


def _run_step(step: Step) -> dict:
    started = time.time()
    if step.kind == "script":
        proc = subprocess.run(
            step.command, cwd=BACKEND, shell=True, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=900,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return {
            "step_id": step.step_id,
            "title": step.title,
            "prd": step.prd,
            "kind": step.kind,
            "command": step.command,
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "seconds": round(time.time() - started, 2),
            "tail": output.strip().splitlines()[-25:],
        }

    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *step.nodes]
    proc = subprocess.run(
        cmd, cwd=BACKEND, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=1800,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return {
        "step_id": step.step_id,
        "title": step.title,
        "prd": step.prd,
        "kind": step.kind,
        "nodes": step.nodes,
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "seconds": round(time.time() - started, 2),
        "tail": out.strip().splitlines()[-25:],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="T30 全链路 UAT 运行器")
    ap.add_argument("--step", action="append", default=[],
                    help="仅运行指定 step_id（可多次），默认全部")
    ap.add_argument("--report", default="", help="写入 JSON 报告路径")
    ap.add_argument("--quiet", action="store_true", help="仅打印汇总")
    args = ap.parse_args()

    selected = [s for s in STEPS if not args.step or s.step_id in set(args.step)]
    if args.step:
        unknown = set(args.step) - {s.step_id for s in STEPS}
        if unknown:
            print(f"未知步骤: {sorted(unknown)}")
            return 2

    results: list[dict] = []
    print("=" * 78)
    print("沁爽饮料分销管理平台 · T30 全链路 UAT（PRD 第十一章 A1-A28）")
    print("=" * 78)
    for i, step in enumerate(selected, 1):
        r = _run_step(step)
        results.append(r)
        flag = "PASS" if r["ok"] else "FAIL"
        print(f"[{i:>2}/{len(selected)}] {flag}  {step.step_id}  {step.title}")
        if not r["ok"] and not args.quiet:
            for line in r["tail"]:
                print("        |", line)

    ok = sum(1 for r in results if r["ok"])
    failed = [r["step_id"] for r in results if not r["ok"]]
    print("-" * 78)
    print(f"汇总：{ok}/{len(results)} 步通过" + (f"，失败：{failed}" if failed else "，全部通过"))
    print("门禁：", "通过（可进入业务负责人签署）" if not failed else "未通过（禁止上线）")

    if args.report:
        path = ROOT / args.report
        path.parent.mkdir(parents=True, exist_ok=True)
        generated_at = datetime.now(UTC).isoformat()
        path.write_text(json.dumps({
            "schema": "qinshuang-uat-run/v1",
            "card": "T30",
            "generated_at": generated_at,
            "passed": ok,
            "total": len(results),
            "failed_steps": failed,
            "steps": results,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写入 {path}")
        # 签署记录必须引用真实运行时间戳（Q178 门禁）；此处同步，避免人工改数字漂移。
        # 注意：只同步「报告引用」，不触碰任何签署栏位。
        synced = _sync_signoff_timestamp(generated_at)
        if synced:
            print(f"已同步签署记录中的运行时间戳 → {generated_at}")

    return 1 if failed else 0


SIGNOFF_PATH = ROOT / "docs" / "uat" / "uat-signoff.md"
_STAMP_RE = re.compile(r"UAT 运行时间戳：`[^`]*`")


def _sync_signoff_timestamp(stamp: str) -> bool:
    if not SIGNOFF_PATH.exists():
        return False
    body = SIGNOFF_PATH.read_text(encoding="utf-8")
    replaced, count = _STAMP_RE.subn(f"UAT 运行时间戳：`{stamp}`", body)
    if count == 0:
        return False
    SIGNOFF_PATH.write_text(replaced, encoding="utf-8")
    return True


if __name__ == "__main__":
    raise SystemExit(main())

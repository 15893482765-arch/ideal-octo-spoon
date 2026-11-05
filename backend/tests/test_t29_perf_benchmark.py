"""Q169 性能阈值门禁。

轻量断言（百分位/阈值）在所有环境运行；真实 PG16 固定规格基准仅在设置
Q169_DSN 时运行（subprocess 调 scripts/perf_benchmark.py），无 DSN 时 skip，
绝不伪造通过。基准结果与机器规格固化在 docs/performance/q169-benchmark.json。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
SCRIPT = BACKEND_DIR / "scripts" / "perf_benchmark.py"
REPORT = BACKEND_DIR.parent / "docs" / "performance" / "q169-benchmark.json"

pytestmark = pytest.mark.performance


def test_q169_committed_benchmark_report_passes_thresholds():
    # 固化报告必须存在且三场景达标、0 错误（这是真实跑出并入库的结果，不是合成数据）
    assert REPORT.exists(), "缺少 docs/performance/q169-benchmark.json，需真实运行压测脚本生成"
    data = json.loads(REPORT.read_text(encoding="utf-8"))
    assert data["overall_pass"] is True
    for key, v in data["verdict"].items():
        assert v["pass"] is True, key
        assert v["worst_p95_ms"] <= v["threshold_ms"]
        assert v["worst_error_rate"] == 0.0
    # 固定口径字段必须齐备，防止有人偷偷改规模
    spec = data["spec"]
    assert spec["n_merchants"] == 20_000
    assert spec["warmup_rounds"] == 10
    assert spec["sample_rounds"] == 50
    assert spec["repeat_runs"] == 3
    assert data["postgres_version"].startswith("16")


def test_q169_report_includes_machine_spec_and_explain():
    data = json.loads(REPORT.read_text(encoding="utf-8"))
    assert data["machine"]["cpu_count"] >= 1
    assert data["machine"]["platform"]
    assert data["machine"]["measured_at_utc"]
    plan = data["explain"]["merchant_list_sql"]
    assert plan["execution_time_ms"] >= 0
    assert plan["node_type"]


@pytest.mark.skipif(
    not bool(os.environ.get("Q169_DSN")),
    reason="未设置 Q169_DSN：真实 PG16 固定规格基准跳过（结果见已固化报告）",
)
def test_q169_run_live_benchmark_on_real_postgres(tmp_path):
    out = tmp_path / "live.json"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--dsn", env["Q169_DSN"], "--report", str(out)],
        cwd=BACKEND_DIR, env=env, capture_output=True, text=True, timeout=900,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["overall_pass"] is True

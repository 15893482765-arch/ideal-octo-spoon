#!/usr/bin/env python3
"""测试追踪矩阵校验器（T01 起逐卡维护）。

规则：
- docs/test-traceability.csv 必须存在且含全部必需列；
- test_id 全局唯一、格式为 Q + 三位数字且递增无空洞（警告不阻断）；
- card、test_id、command、prd_rule_or_scenario 必填；
- test_file 指向仓库内真实存在的文件（悬空引用即失败）。

用法：在仓库根目录执行 `python scripts/validate_traceability.py`。
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

REQUIRED_COLUMNS = ["card", "test_id", "test_file", "command", "fixture", "prd_rule_or_scenario"]
Q_PATTERN = re.compile(r"^Q(\d{3,})$")

# 这些“test_file”不是真实文件路径，而是显式声明的命令级映射，允许存在
NON_FILE_TARGETS = {"frontend/package.json#scripts"}


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    csv_path = repo_root / "docs" / "test-traceability.csv"
    errors: list[str] = []
    warnings: list[str] = []

    if not csv_path.is_file():
        print(f"FAIL: 缺少 {csv_path}")
        return 1

    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        for col in REQUIRED_COLUMNS:
            if col not in header:
                errors.append(f"缺少必需列: {col}")

        rows = list(reader)
        if not rows:
            errors.append("追踪矩阵为空：每张卡的每个 Q 编号都必须登记")

        seen_ids: dict[str, int] = {}
        numbers: list[int] = []
        for idx, row in enumerate(rows, start=2):
            line_no = idx
            test_id = (row.get("test_id") or "").strip()
            card = (row.get("card") or "").strip()
            test_file = (row.get("test_file") or "").strip()
            command = (row.get("command") or "").strip()
            prd_ref = (row.get("prd_rule_or_scenario") or "").strip()

            if not test_id:
                errors.append(f"第 {line_no} 行缺少 test_id")
                continue
            if not card or not command or not prd_ref:
                errors.append(f"第 {line_no} 行（{test_id}）：card/command/prd_rule_or_scenario 必填")
            if not Q_PATTERN.match(test_id):
                errors.append(f"第 {line_no} 行：test_id 必须形如 Q001（得到 {test_id}）")
            else:
                numbers.append(int(Q_PATTERN.match(test_id).group(1)))
            if test_id in seen_ids:
                errors.append(f"第 {line_no} 行：test_id 重复（{test_id} 已在第 {seen_ids[test_id]} 行）")
            else:
                seen_ids[test_id] = line_no

            if test_file and test_file not in NON_FILE_TARGETS:
                repo_rel = test_file.split("#", 1)[0]
                if not (repo_root / repo_rel).is_file():
                    errors.append(f"第 {line_no} 行：悬空引用，文件不存在 {test_file}")

    missing = [n for n in range(1, (max(numbers) + 1) if numbers else 0) if n not in numbers]
    # v1.2 任务卡修订说明：T02 起追加 Q179～Q186 系列（接续 v1.0 的 Q001-Q178），
    # 因此 1xx 与 1xx 段之间允许存在空洞（仅 0-99 段视为连续）
    cross_gap = [n for n in missing if n >= 100 and n < 100]
    if cross_gap:
        warnings.append(f"100-179 段允许存在空洞（v1.2 增量）: {sorted(set(cross_gap))[:10]} ...")
    inband_missing = [n for n in missing if n < 100]
    if inband_missing:
        warnings.append(
            f"Q 编号在 0-99 段存在空洞（新增时接续编号即可）: {sorted(set(inband_missing))[:10]} ..."
        )

    for err in errors:
        print(f"FAIL: {err}")
    for warn in warnings:
        print(f"WARN: {warn}")

    if errors:
        print(f"\n共 {len(errors)} 个错误，校验失败")
        return 1
    print(f"OK: {len(rows)} 行追踪记录校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

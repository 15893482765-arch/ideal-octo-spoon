#!/usr/bin/env python3
"""T30 Q173：机器可检查的 A1-A28 验收追踪矩阵校验。

CI 门禁，任一不满足即退出码 1：
1. 必须恰好覆盖 A1..A28，无重复、无缺失；
2. 每个场景的 nodes 非空，且引用的 pytest 测试文件/节点真实存在（悬空引用失败）；
3. uat 脚本引用（scripts/uat_full_chain.py#step）必须是文件中真实存在的标记；
4. 每条 rule 非空；
5. A25-A28（T30 新增场景）必须有节点或 UAT 脚本覆盖（不允许只写 PRD 不落地）。

用法：python scripts/validate_acceptance_matrix.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MATRIX = ROOT / "docs" / "uat" / "acceptance-matrix.json"
BACKEND = ROOT / "backend"
EXPECTED = [f"A{i}" for i in range(1, 29)]
NEW_SCENARIOS = {"A25", "A26", "A27", "A28"}


def _collect_test_nodes() -> set[str]:
    """真实测试节点集合：优先 `pytest --collect-only -q`，失败则静态 AST 扫描兜底。"""
    import ast

    # 静态 AST 父节点（文件::函数），始终计算，作为参数化判定的基础
    static: set[str] = set()
    for path in (BACKEND / "tests").rglob("test_*.py"):
        rel = path.relative_to(BACKEND).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                static.add(f"{rel}::{node.name}")

    try:
        import sys as _sys

        out = subprocess.run(
            [_sys.executable, "-m", "pytest", "--collect-only", "-q",
             "-p", "no:cacheprovider", "tests"],
            cwd=BACKEND, capture_output=True, text=True, timeout=300, encoding="utf-8",
        )
        nodes: set[str] = set()
        # 注意：有 warning 时退出码非 0，但 stdout 仍含完整节点，不能因 returncode 丢弃
        for line in out.stdout.splitlines():
            line = line.strip()
            if "::" in line and line.startswith("tests/"):
                nodes.add(line.split(" ", 1)[0])
        if nodes:
            # 合并：pytest 节点（含参数实例）+ AST 父节点（使无参引用合法）
            return nodes | static
    except (subprocess.SubprocessError, OSError):
        pass
    return static


def _node_exists(ref: str, real_nodes: set[str]) -> bool:
    # real_nodes 已合并 pytest 参数实例与 AST 父函数节点
    return ref in real_nodes or ref.split("[", 1)[0] in real_nodes


def _uat_step_exists(ref: str) -> bool:
    # forms: scripts/uat_full_chain.py#A25_risk_closure
    path_part, _, step = ref.partition("#")
    path = ROOT / path_part
    if not step or not path.exists():
        return False
    return f'"{step}"' in path.read_text(encoding="utf-8") or f"'{step}'" in path.read_text(encoding="utf-8")


def main() -> int:
    errors: list[str] = []
    if not MATRIX.exists():
        print(f"FAIL: 矩阵不存在 {MATRIX}")
        return 1

    data = json.loads(MATRIX.read_text(encoding="utf-8"))
    scenarios = data.get("scenarios", [])
    ids = [s.get("id") for s in scenarios]

    # 1. 完整且唯一 A1..A28
    seen: set[str] = set()
    dup: list[str] = []
    for sid in ids:
        if sid in seen:
            dup.append(sid)
        seen.add(sid)
    if dup:
        errors.append(f"重复场景 ID: {sorted(set(dup))}")
    missing = [a for a in EXPECTED if a not in seen]
    extra = [s for s in seen if s not in EXPECTED]
    if missing:
        errors.append(f"缺失场景: {missing}")
    if extra:
        errors.append(f"越界场景（非 A1-A28）: {sorted(extra)}")

    real_nodes = _collect_test_nodes()

    # 2-5. 逐场景校验
    by_id = {s.get("id"): s for s in scenarios}
    for sid in EXPECTED:
        sc = by_id.get(sid)
        if not sc:
            continue
        if not str(sc.get("rule", "")).strip():
            errors.append(f"{sid}: rule 为空")
        nodes = sc.get("nodes", [])
        if not nodes:
            errors.append(f"{sid}: 无任何 nodes 覆盖")
        for ref in nodes:
            # 仅取文件::函数 部分，校验测试文件存在 + 函数节点存在
            test_path = ref.split("::", 1)[0]
            if not (BACKEND / test_path).exists():
                errors.append(f"{sid}: 悬空测试文件 {test_path}")
                continue
            if "::" in ref and not _node_exists(ref, real_nodes):
                errors.append(f"{sid}: 悬空测试节点 {ref}")
        for uref in sc.get("uat", []):
            if not _uat_step_exists(uref):
                errors.append(f"{sid}: 悬空 UAT 步骤 {uref}")
        if sid in NEW_SCENARIOS and not nodes and not sc.get("uat"):
            errors.append(f"{sid}: 新增场景必须有节点或 UAT 脚本覆盖")

    # 6. 上线门禁（gates）：Q173-Q178/Q186 必须映射到真实测试/UAT/命令/产物
    gates = data.get("gates", [])
    missing_gates = [g for g in ("Q173", "Q174", "Q175", "Q176", "Q177", "Q178", "Q186")
                     if g not in {x.get("id") for x in gates}]
    if missing_gates:
        errors.append(f"缺少门禁项: {missing_gates}")
    for gate in gates:
        gid = gate.get("id")
        if not str(gate.get("rule", "")).strip():
            errors.append(f"{gid}: rule 为空")
        has_mapping = False
        for ref in gate.get("nodes", []):
            has_mapping = True
            test_path = ref.split("::", 1)[0]
            if not (BACKEND / test_path).exists():
                errors.append(f"{gid}: 悬空测试文件 {test_path}")
            elif "::" in ref and not _node_exists(ref, real_nodes):
                errors.append(f"{gid}: 悬空测试节点 {ref}")
        for uref in gate.get("uat", []):
            has_mapping = True
            if not _uat_step_exists(uref):
                errors.append(f"{gid}: 悬空 UAT 步骤 {uref}")
        for cmd in gate.get("commands", []):
            has_mapping = True
            # commands 内引用的仓库脚本必须真实存在
            for part in [p for p in cmd.split() if p.endswith(".py")]:
                if not (ROOT / part).exists():
                    errors.append(f"{gid}: 命令引用不存在的脚本 {part}")
        for art in gate.get("artifacts", []):
            if not (ROOT / art).exists():
                errors.append(f"{gid}: 声明产物不存在 {art}")
        if not has_mapping:
            errors.append(f"{gid}: 门禁项必须有测试/UAT/命令映射")

    if errors:
        print("ACCEPTANCE MATRIX FAILED:")
        for e in errors:
            print("  -", e)
        return 1

    total_nodes = sum(len(s.get("nodes", [])) for s in scenarios) + sum(
        len(g.get("nodes", [])) for g in gates
    )
    print(
        f"ACCEPTANCE MATRIX OK: 28 场景 + {len(gates)} 个上线门禁全部映射，"
        f"{total_nodes} 个测试节点引用，无重复/缺失/悬空。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

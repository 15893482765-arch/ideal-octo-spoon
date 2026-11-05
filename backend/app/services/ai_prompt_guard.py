"""提示注入防护（T26 / PRD §5.4 第 5/6 条、§5.2、§9.1、Q148/Q149）。

外部备注、合同条款、附件 OCR、图片 OCR 一律视为**不可信数据**：
- 其中任何「忽略审批 / 不要确认 / 提权 / 改角色 / 扩数据范围 / 直接执行写操作」
  之类指令，只做**检测 + 剥离 + 审计标记**，绝不改变工具白名单、角色权限、
  确认级别与审批流（这些只由服务端矩阵决定）；
- 检测是确定性的关键词/模式匹配（首版不做模型判别，避免被模型绕过）；
- 命中不直接报错阻断正常业务数据，而是：剥离指令片段 → 标记 injection_blocked
  → 由调用方照常按服务端权限执行（外部文本无权扩大也无权缩小权限）。

注意：本函数只负责"发现并清洗"。真正的权限裁决仍在 gateway/矩阵；本模块
无法授予任何能力。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 越权/扩权/绕过确认类指令模式（中英）。命中即视为提示注入。
_NL = r"[^。\n]{0,20}"
_DIRECTIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # 绕过审批 / 管控
    ("IGNORE_APPROVAL", re.compile(
        r"忽略(?:以上|之前|所有)?(?:的)?(?:审批|审核|批准|管控|限制|规则|安全策略|系统提示)")),
    ("IGNORE_APPROVAL", re.compile(
        r"(?i)\b(ignore|disregard|bypass|skip)\b" + _NL
        + r"\b(approval|approve|review|confirm|policy|instructions?|rules?|guardrails?)\b")),
    # 提权 / 改角色
    ("ESCALATE_ROLE", re.compile(
        r"(?:把|将|给我|授予|提升|赋予)[^。\n]{0,12}"
        r"(?:管理员|超级?权限|管理员权限|财务|主管|超管|admin|root)")),
    ("ESCALATE_ROLE", re.compile(
        r"(?i)\b(you are now|act as|become|pretend to be)\b" + _NL
        + r"\b(admin|root|superuser|manager|finance|supervisor)\b")),
    # 扩大数据范围 / 越权读取
    ("WIDEN_SCOPE", re.compile(
        r"(?:查看|读取|导出|访问|显示|拉取)[^。\n]{0,10}"
        r"(?:全部|所有|全量|任何|其它|其他)(?:片区|品牌|商家|数据|订单|客户)")),
    ("WIDEN_SCOPE", re.compile(
        r"(?i)\b(all|every|any|other)\b[^。\n]{0,15}"
        r"\b(regions?|brands?|merchants?|orders?|data|records?)\b[^。\n]{0,15}"
        r"\b(show|export|read|access|list)\b")),
    # 免确认直接执行写操作
    ("SKIP_CONFIRMATION", re.compile(
        r"(?:无需|不用|免去|跳过|不要)[^。\n]{0,6}(?:二次确认|确认|令牌|token)",
        re.IGNORECASE)),
    ("SKIP_CONFIRMATION", re.compile(
        r"(?i)\b(without|no|skip)\b[^。\n]{0,15}\b(confirmation|confirm|token|approval)\b"
        r"[^。\n]{0,15}\b(execute|submit|write|create|publish|approve)\b")),
    # 直接命令执行写操作（来自数据域而非用户受控指令）
    ("FORCE_WRITE", re.compile(
        r"(?i)\b(execute|run|call|invoke)\s+"
        r"(delete|drop|update|insert|approve|publish|transfer)\b")),
    # 泄露系统提示/凭据
    ("LEAK_PROMPT", re.compile(
        r"(?i)(reveal|show|print|repeat)\s+(your|the|system)\s+"
        r"(prompt|instructions?|rules?|secret|api[_ ]?key)")),
)


@dataclass
class GuardResult:
    """外部内容过检结果。"""

    cleaned_text: str
    injection_detected: bool = False
    blocked_directives: list[str] = field(default_factory=list)
    matched_snippets: list[str] = field(default_factory=list)

    def to_audit(self) -> tuple[bool, tuple[str, ...]]:
        return self.injection_detected, tuple(sorted(set(self.blocked_directives)))


def inspect_external_text(text: object) -> GuardResult:
    """检测并剥离不可信外部文本中的越权指令。

    返回清洗后的文本（命中片段替换为中性占位），以及命中的指令类型/原文片段。
    无论命中与否，都不抛异常——业务数据继续流转，仅审计标记。
    """
    if text is None:
        return GuardResult(cleaned_text="")
    s = str(text)
    directives: list[str] = []
    snippets: list[str] = []
    cleaned = s
    for code, pattern in _DIRECTIVE_PATTERNS:
        for m in pattern.finditer(cleaned):
            snippet = m.group(0)
            directives.append(code)
            snippets.append(snippet[:80])
            cleaned = cleaned.replace(snippet, "<blocked_directive>")
    return GuardResult(
        cleaned_text=cleaned,
        injection_detected=bool(directives),
        blocked_directives=directives,
        matched_snippets=snippets,
    )

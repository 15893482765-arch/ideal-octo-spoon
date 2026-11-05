"""命令权限与确认等级（PRD §5.2）、工具种类（T22）。

| 等级 | 能力 | 执行要求 |
| --- | --- | --- |
| A0 | 只读查询/解释 | 直接返回受权限过滤后的数据 |
| A1 | 生成草稿 | 返回可编辑草稿，不写业务表 |
| A2 | 发起普通单据 | 字段预览 + 用户二次确认后提交 |
| A3 | 发起关键流程 | 执行预览 + 有权人审批；AI 不可跳过审批 |
| A4 | 高风险财务/配置 | 双人复核或指定审批；AI 不可自动执行 |
"""

from __future__ import annotations

from enum import StrEnum


class ConfirmLevel(StrEnum):
    """AI 命令确认等级（PRD §5.2）。"""

    A0 = "A0"  # 只读查询/解释
    A1 = "A1"  # 生成草稿（不写业务表）
    A2 = "A2"  # 发起普通单据
    A3 = "A3"  # 发起关键流程
    A4 = "A4"  # 高风险财务/配置

    @property
    def is_write(self) -> bool:
        """A2 及以上为「写工具」（须幂等键 + 结构化确认）。"""
        return self in (ConfirmLevel.A2, ConfirmLevel.A3, ConfirmLevel.A4)

    @property
    def requires_approver(self) -> bool:
        """A3/A4 必须存在独立审批人（AI 不可跳过审批）。"""
        return self in (ConfirmLevel.A3, ConfirmLevel.A4)

    @property
    def dual_review(self) -> bool:
        """A4 需双人复核或指定审批。"""
        return self is ConfirmLevel.A4


class ToolKind(StrEnum):
    """工具性质：决定是否写库、是否需要幂等键与结构化确认。"""

    READ = "READ"  # A0：只读
    DRAFT = "DRAFT"  # A1：草稿，不写正式业务表
    WRITE = "WRITE"  # A2+：写业务表，需幂等键

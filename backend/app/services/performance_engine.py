"""PerformanceEngine：绩效预计算的**纯函数**（T19/T35 / PRD §4.10、§6.5）。

T35 口径精简后：个人绩效 = 新签合同奖励 + 活动达成奖励。
有效巡访奖励、确认销量提成、扣减、调整**恒为 0**，且不再产出对应依据行
（模型列保留以保证历史快照可读，历史数据不回算）。

设计红线：
- **纯函数**：不读库、不写库、不看时间、不发请求；输入不可变事实 + 规则参数，
  输出金额（整数分）与逐项依据。事实采集（生效合同/活动达成/调拨日归属）由
  PerformanceSnapshotService 完成后传入；
- **金额精确到分**：金额一律 ``int`` 分；箱数 Decimal；全程 Decimal 计算，
  **禁止 float**；
- 只吃已审核事实：契约上要求传入的事实已经过服务层过滤（仅生效合同、仅已复盘
  活动）。引擎对关键字段再做防御性校验。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

# 金额量化口径
_FEN = Decimal("1")              # 金额精确到分（整数）
_ZERO = Decimal("0")


# ============================== 输入事实 ==============================


@dataclass(frozen=True)
class EngineFact:
    """一条已审核绩效事实（服务层按调拨日 owner_at 归属后喂入）。"""

    object_type: str          # 事实单据类型：CONTRACT / VISIT_RECORD / ...
    object_id: str            # 事实单据 ID（str 化的 UUID）
    fact_date: Any            # datetime.date
    amount_fen: int = 0       # 与金额相关事实的单据金额（如确认销售额，分）
    quantity: Decimal = _ZERO  # 与数量相关事实（如确认销量箱）；非负
    merchant_id: str | None = None


@dataclass(frozen=True)
class ApprovedItem:
    """已审批的扣减/调整（服务层已校验审批单 APPROVED 且同数据集）。"""

    object_type: str
    object_id: str
    amount_fen: int           # 扣减为负分，调整为非负分
    fact_date: Any
    reason: str = ""
    merchant_id: str | None = None


@dataclass(frozen=True)
class RuleParams:
    """规则版本公式参数。金额单位分；阶梯比率为基点（0.01%）。

    - new_contract_award_fen：每单新签合同提成（分）；
    - valid_visit_award_fen：每次有效巡访奖励（分）；
    - sales_tiers：确认销量**销售额**累进阶梯，元素 (上限分(None=∞), 比率bp)；
      例 [(1000000, 200), (None, 300)]：1 万元(含)以内 2.00%，超出部分 3.00%；
    - activity_award_fen：每个已复盘（达成）活动奖励（分）。
    """

    new_contract_award_fen: int
    valid_visit_award_fen: int
    sales_tiers: tuple[tuple[int | None, int], ...]
    activity_award_fen: int

    def __post_init__(self) -> None:
        if self.new_contract_award_fen < 0:
            raise ValueError("新签合同提成不能为负")
        if self.valid_visit_award_fen < 0:
            raise ValueError("有效巡访奖励不能为负")
        if self.activity_award_fen < 0:
            raise ValueError("活动达成奖励不能为负")
        if not self.sales_tiers:
            raise ValueError("确认销量阶梯至少配置一档")
        prev = -1
        for cap, bp in self.sales_tiers:
            if bp < 0 or bp > 10000:
                raise ValueError("销量阶梯比率基点必须在 [0,10000]")
            if cap is not None and cap <= prev:
                raise ValueError("销量阶梯上限必须严格递增，最后一档为 None")
            if cap is not None:
                prev = cap
        # 最后一档必须是无上限封顶档
        if self.sales_tiers[-1][0] is not None:
            raise ValueError("销量阶梯最后一档必须无上限（cap=None）")


# ============================== 输出依据 ==============================


@dataclass(frozen=True)
class LineBreakdown:
    """单个事实/阶梯的逐项金额依据（Q108 可反查）。"""

    line_type: str
    source_object_type: str
    source_object_id: str
    amount_fen: int
    basis: dict[str, Any]
    quantity: Decimal = _ZERO
    merchant_id: str | None = None
    fact_date: Any = None


@dataclass(frozen=True)
class EngineResult:
    """绩效预计算结果：五类金额（整数分）+ 净额 + 逐项依据。"""

    new_contract_fen: int
    visit_bonus_fen: int
    sales_commission_fen: int
    activity_bonus_fen: int
    deduction_fen: int          # <= 0
    adjustment_fen: int         # >= 0
    net_fen: int
    lines: tuple[LineBreakdown, ...] = field(default_factory=tuple)

    def as_category_map(self) -> dict[str, int]:
        return {
            "new_contract_fen": self.new_contract_fen,
            "visit_bonus_fen": self.visit_bonus_fen,
            "sales_commission_fen": self.sales_commission_fen,
            "activity_bonus_fen": self.activity_bonus_fen,
            "deduction_fen": self.deduction_fen,
            "adjustment_fen": self.adjustment_fen,
            "net_fen": self.net_fen,
        }


# ============================== 纯计算 ==============================


def _to_fen(value: Decimal) -> int:
    """Decimal → 整数分（银行外四舍五入 ROUND_HALF_UP，业务口径）。"""
    return int(value.quantize(_FEN, rounding=ROUND_HALF_UP))


class PerformanceEngine:
    """无状态纯计算器：``compute(...)`` 返回金额与逐项依据。"""

    # 事实单据类型 → 内部分类
    TYPE_NEW_CONTRACT = "NEW_CONTRACT"
    TYPE_VALID_VISIT = "VALID_VISIT"
    TYPE_CONFIRMED_SALES = "CONFIRMED_SALES"
    TYPE_ACTIVITY = "ACTIVITY_ACHIEVED"

    def compute(
        self,
        *,
        params: RuleParams,
        new_contracts: Sequence[EngineFact] = (),
        activities: Sequence[EngineFact] = (),
        # T35：以下入参仅为兼容既有调用方保留，一律不参与计算
        valid_visits: Sequence[EngineFact] = (),
        confirmed_sales: Sequence[EngineFact] = (),
        deductions: Sequence[ApprovedItem] = (),
        adjustments: Sequence[ApprovedItem] = (),
    ) -> EngineResult:
        del valid_visits, confirmed_sales, deductions, adjustments
        lines: list[LineBreakdown] = []

        # 1) 新签合同奖励（仅生效合同；按单定额）
        new_contract_total = 0
        for f in new_contracts:
            amt = int(params.new_contract_award_fen)
            new_contract_total += amt
            lines.append(LineBreakdown(
                line_type=self.TYPE_NEW_CONTRACT,
                source_object_type=f.object_type,
                source_object_id=f.object_id,
                amount_fen=amt,
                quantity=Decimal(1),
                merchant_id=f.merchant_id,
                fact_date=f.fact_date,
                basis={"award_per_contract_fen": amt},
            ))

        # 2) 活动达成奖励（仅已复盘 REVIEWED/CLOSED 活动；参与人按个定额）
        activity_total = 0
        for f in activities:
            amt = int(params.activity_award_fen)
            activity_total += amt
            lines.append(LineBreakdown(
                line_type=self.TYPE_ACTIVITY,
                source_object_type=f.object_type,
                source_object_id=f.object_id,
                amount_fen=amt,
                quantity=Decimal(1),
                merchant_id=f.merchant_id,
                fact_date=f.fact_date,
                basis={"award_per_activity_fen": amt},
            ))

        # T35：有效巡访奖励 / 确认销量提成 / 扣减 / 调整一律为 0，不再产出依据行
        net = new_contract_total + activity_total

        return EngineResult(
            new_contract_fen=new_contract_total,
            visit_bonus_fen=0,
            sales_commission_fen=0,
            activity_bonus_fen=activity_total,
            deduction_fen=0,
            adjustment_fen=0,
            net_fen=net,
            lines=tuple(lines),
        )

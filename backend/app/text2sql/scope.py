"""执行上下文：数据集隔离 + 行级范围。

与现有 ``report_scope.MerchantScope`` 对齐：行级范围只能由服务端
按 actor 角色解析后注入，QueryPlan 本身不携带任何范围信息——
这是口径红线（禁止模型/客户端指定数据范围）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class RowScope:
    """行级范围：三字段均为 None 表示全局（内部/管理员）。"""

    region_ids: tuple[UUID, ...] | None = None
    brand_ids: tuple[UUID, ...] | None = None
    merchant_ids: tuple[UUID, ...] | None = None

    @property
    def is_global(self) -> bool:
        return (
            self.region_ids is None
            and self.brand_ids is None
            and self.merchant_ids is None
        )

    @staticmethod
    def from_merchant_scope(scope: Any) -> "RowScope":
        """从 report_scope.MerchantScope 转换（鸭子类型，避免硬依赖）。"""
        if getattr(scope, "is_global", False):
            return RowScope()
        # 空集合必须保留（= 永假条件，「有范围维度但无任何授权」），
        # 若塌缩成 None 会被当作「该维度不限」而放大可见范围。
        return RowScope(
            region_ids=(
                tuple(scope.region_ids) if scope.region_ids is not None else None
            ),
            merchant_ids=(
                tuple(scope.merchant_ids) if scope.merchant_ids is not None else None
            ),
        )


@dataclass(frozen=True)
class QueryContext:
    """一次查询的执行上下文（由调用方/助手链路注入）。"""

    dataset_id: UUID
    scope: RowScope | None = None
    # 相对时间换算基准（业务时区当当日）；None = 执行当天
    today: date | None = None
    extra: dict[str, Any] = field(default_factory=dict)

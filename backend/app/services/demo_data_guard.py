"""演示数据域守卫（T06 / PRD §6 R18、§8、§10.2、§10.4）。

统一规则：
- 关系创建时校验两端 `dataset_type + dataset_id` 完全一致；
  DEMO + PRODUCTION 混连即拒（含 `PRODUCTION ↔ DEMO` 任一方向）；
- 经营指标、绩效、工资、采购、同步/异步导出必须显式 `DataScope.PRODUCTION`；
  `DataScope.ALL` / `DataScope.DEMO` 都不进入正式口径（PRD R34、T06 Q030）；
- 生产环境硬锁：environment=production 时任何 `dataset_type=DEMO` 批次拒绝进入
  （T06 Q031）；本服务在应用启动期与运行期都可用，由 SeedCheckService / 业务
  Service / CLI 显式调用。

**T06 范围**：仅给出"数据域"二元判断与状态机读口；具体的指标/导出业务卡
（T08/T11/T19/T23 等）按本服务的 read_scope 与 assert_* 入口接入。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from app.core.config import get_settings
from app.core.errors import DomainError


class DataScope(StrEnum):
    """显式数据域（PRD R34 / §10.2）。"""

    PRODUCTION = "PRODUCTION"  # 正式口径
    DEMO = "DEMO"  # 演示口径
    ALL = "ALL"  # 显式同时查询（仅供 UI 调试；不进入正式统计/导出）

    @classmethod
    def parse(cls, value: str | None) -> DataScope:
        if value is None:
            return cls.ALL
        try:
            return cls(value.upper())
        except ValueError as exc:
            raise DomainError(
                code="DATA_SCOPE_INVALID",
                message="数据域参数非法",
                details={"value": value},
            ) from exc


def same_dataset(*, a_type: str, a_id: Any, b_type: str, b_id: Any) -> bool:
    """两端的 (dataset_type, dataset_id) 是否完全一致。"""
    return str(a_type) == str(b_type) and str(a_id) == str(b_id)


def assert_same_dataset(
    *,
    parent_type: str,
    parent_id: Any,
    child_type: str,
    child_id: Any,
    relation: str,
) -> None:
    """关系创建/校验入口：两端 (dataset_type, dataset_id) 必须一致。"""
    if not same_dataset(
        a_type=parent_type, a_id=parent_id, b_type=child_type, b_id=child_id
    ):
        raise DomainError(
            code="DATASET_MIXED",
            message="演示数据与生产数据不得建立关联",
            details={
                "relation": relation,
                "parent": {"dataset_type": parent_type, "dataset_id": str(parent_id)},
                "child": {"dataset_type": child_type, "dataset_id": str(child_id)},
            },
        )


def assert_demo_disabled_in_production(*, dataset_type: str) -> None:
    """生产环境拒绝 dataset_type=DEMO 批次（T06 Q031 / PRD §10.2）。"""
    settings = get_settings()
    if (
        settings.environment == "production"
        and settings.production_dataset_lock
        and dataset_type == "DEMO"
    ):
        raise DomainError(
            code="DEMO_BLOCKED_IN_PRODUCTION",
            message="生产环境禁止导入演示数据",
            details={"dataset_type": dataset_type},
        )


def assert_production_scope(*, scope: DataScope, where: str) -> None:
    """正式口径（绩效/工资/采购/导出/看板）必须显式 PRODUCTION（PRD R34）。"""
    if scope is not DataScope.PRODUCTION:
        raise DomainError(
            code="DATA_SCOPE_NOT_PRODUCTION",
            message="正式口径仅接受 PRODUCTION 数据域",
            details={"where": where, "scope": scope.value},
        )


def dataset_uuid(value: Any) -> str:
    """安全地渲染 dataset_id：UUID → str，否则原样返回（便于日志/负例报告）。"""
    if isinstance(value, UUID):
        return str(value)
    return str(value)

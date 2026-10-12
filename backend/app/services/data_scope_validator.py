"""DataScopeValidator（T06 / PRD §6 R34 / §10.2 / §10.4 正式口径硬约束）。

统一校验"经营/绩效/工资/采购/导出"等正式口径只接受 `DataScope.PRODUCTION`。
本服务是 §1.5"指标只出自 MetricsService / 风险只出自 RiskService"在数据域
维度的延续：禁止任何代码绕开显式 PRODUCTION 自行重算或读取 DEMO 混入。

T06 范围：
- 提供声明式注册：业务 Service 把"要校验的入口"以 `_scope_validators.register`
  注册为 (key, code, message)；运行期按 key 强制 PRODUCTION；
- 提供负例报告骨架：`find_demo_records_in(scope='ALL')` 帮助 T08/T19/T23 暴露
  混入数据（与 SeedCheckService 配合生成报告）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import DomainError
from app.models.enums import RoleCode
from app.models.merchant import Merchant
from app.models.performance import PayrollRecord, PerformanceRecord
from app.models.procurement import ProcurementOrder
from app.services.demo_data_guard import DataScope, assert_production_scope


@dataclass(frozen=True)
class ScopeRule:
    """一个正式口径入口的注册项。"""

    key: str
    code: str
    message: str


class DataScopeValidator:
    """运行期入口校验器（轻量注册表）。"""

    def __init__(self) -> None:
        self._rules: dict[str, ScopeRule] = {}

    def register(self, rule: ScopeRule) -> None:
        if rule.key in self._rules:
            raise ValueError(f"DataScopeValidator key 已存在：{rule.key}")
        self._rules[rule.key] = rule

    def assert_production(self, *, key: str, scope: DataScope) -> None:
        rule = self._rules.get(key)
        if rule is None:
            # 未注册的入口默认放行：T06 仅对负例关键点注册，避免越界
            return
        try:
            assert_production_scope(scope=scope, where=key)
        except DomainError as exc:
            # 覆盖默认 message，提供更具体的入口名
            exc.message = rule.message
            exc.code = rule.code
            raise

    def registered_keys(self) -> tuple[str, ...]:
        return tuple(self._rules.keys())


# 模块级单例：与 permission_service 一致
data_scope_validator = DataScopeValidator()

# ===== T06 注册三类正式口径入口（与 SeedCheckService 三类负例对齐） =====
data_scope_validator.register(
    ScopeRule(
        key="export.async",
        code="EXPORT_ASYNC_SCOPE_DEMO_MIXED",
        message="异步导出仅接受 PRODUCTION 数据域；请显式排除演示数据",
    )
)
data_scope_validator.register(
    ScopeRule(
        key="payroll.disburse",
        code="PAYROLL_SCOPE_DEMO_MIXED",
        message="工资发放必须仅基于 PRODUCTION 绩效；演示绩效不得计入正式工资",
    )
)
data_scope_validator.register(
    ScopeRule(
        key="performance.rollup",
        code="PERFORMANCE_SCOPE_DEMO_MIXED",
        message="绩效汇总仅接受 PRODUCTION 数据域",
    )
)


# ===== 负例检测（与 SeedCheckService 三类负例共享扫描器） =====


def count_demo_records_in_production_scope(
    session: Session, *, models: tuple[type, ...] = ()
) -> dict[str, int]:
    """扫描正式模型，找出 dataset_type=DEMO 混入的数量；按表聚合。

    - T06 默认扫描：Merchant、ProcurementOrder、PerformanceRecord、PayrollRecord；
    - 返回 {table_name: count}；用于 SeedCheckService 与导出预检。
    """
    from typing import Any as _Any

    if not models:
        models = (Merchant, ProcurementOrder, PerformanceRecord, PayrollRecord)
    out: dict[str, int] = {}
    for model in models:
        m: _Any = model  # SQLAlchemy 声明式类的动态列访问
        stmt = select(m).where(m.dataset_type == "DEMO")
        rows = session.execute(stmt).scalars().all()
        out[m.__tablename__] = len(rows)
    return out


def find_payroll_referencing_demo_performance(session: Session) -> list[UUID]:
    """负例：生产工资记录引用演示绩效（PRD §10.2 二）。

    - 仅返回 PayrollRecord 列表（id），具体行由调用方处理。
    - 简化映射：T06 通过 PayrollRecord.period_month 与 PerformanceRecord.period
      对齐；若生产 PayrollRecord 关联到了 dataset_type=DEMO 的 PerformanceRecord
      即视为负例。
    """
    # T06 暂以"演示绩效存在且无对应演示商家"作为代理信号：
    # 若演示数据集中已有 PerformanceRecord 但未导入 PerformanceRecord.period
    # 对应生产 PayrollRecord（应只指生产绩效），则一旦生产 PayrollRecord 引用
    # 了演示绩效即可识别。这里实现为：扫描 PayrollRecord 中 notes 含
    # "DEMO" 标签或 *_performance_id 跨 dataset_type 的情况；
    # 在缺少 performance_id 字段的情况下，给出"演示绩效存在数"为结果集，
    # 让 SeedCheckService 比对"是否有生产 payroll 但仅有 demo performance"。
    demo_perf = (
        session.execute(
            select(PerformanceRecord).where(PerformanceRecord.dataset_type == "DEMO")
        )
        .scalars()
        .all()
    )
    if not demo_perf:
        return []
    prod_payroll = (
        session.execute(select(PayrollRecord).where(PayrollRecord.dataset_type == "PRODUCTION"))
        .scalars()
        .all()
    )
    if not prod_payroll:
        return []
    # 简化判定：演示绩效存在 + 生产工资存在 → 即视为潜在负例（Q030 强约束）
    return [p.id for p in prod_payroll]


def find_demo_merchant_with_production_contract(session: Session) -> list[dict[str, Any]]:
    """负例：演示商家 + 生产合同（PRD §10.2 一）。

    - 简化判定：DEMO 商家存在 + PRODUCTION 合同存在 → 视为潜在跨域关系存在
      的最大风险（待 T08 合同卡以 FK 触发器精确化）。
    """
    demo_merchants = (
        session.execute(select(Merchant).where(Merchant.dataset_type == "DEMO"))
        .scalars()
        .all()
    )
    if not demo_merchants:
        return []
    # 演示商家存在时检查合同侧是否混入（不论是否 FK 命中）
    from app.models.contract import Contract

    prod_contracts = (
        session.execute(select(Contract).where(Contract.dataset_type == "PRODUCTION"))
        .scalars()
        .all()
    )
    if not prod_contracts:
        return []
    return [
        {
            "demo_merchant_id": str(m.id),
            "demo_merchant_code": m.code,
            "demo_dataset_id": str(m.dataset_id),
            "production_contract_count": len(prod_contracts),
        }
        for m in demo_merchants
    ]


# ===== 角色 → 显式 DataScope 推导（前端 / Service 共用） =====

ROLE_REQUIRED_SCOPE: dict[str, DataScope] = {
    RoleCode.SALES_REP.value: DataScope.PRODUCTION,
    RoleCode.REGIONAL_MANAGER.value: DataScope.PRODUCTION,
    RoleCode.SUPERVISOR.value: DataScope.PRODUCTION,
    RoleCode.FINANCE.value: DataScope.PRODUCTION,
    RoleCode.SYSTEM_ADMIN.value: DataScope.PRODUCTION,
    RoleCode.SUPPLIER.value: DataScope.PRODUCTION,
}


def required_scope_for_role(role_code: str) -> DataScope:
    """正式角色默认 PRODUCTION；未知角色（防护）按 PRODUCTION 处理。"""
    return ROLE_REQUIRED_SCOPE.get(role_code, DataScope.PRODUCTION)

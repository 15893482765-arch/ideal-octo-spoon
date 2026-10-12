"""状态机内核（T05 / PRD §4.5/§4.6/§4.8/§4.9/§4.10；R24）。

设计原则：
- **域内显式迁移图**：每个业务域定义"合法 from→to 集合"，未列入集合的跳转
  视为非法，业务 Service 抛 `DomainError(STATE_TRANSITION_INVALID)`；
- **同事务审计**：所有合法迁移由 `BusinessTransitionService` 统一调用
  `AuditService.record_state_transition` 留痕；
- **不替代业务规则**：本服务只校验"是否合法迁移"；具体业务规则（如 RK-1 风
  险、营销排他、补货承诺日校验）由对应 Service 在迁移前后处理；
- **不修改 ORM 列约束**：数据库 CHECK 仍由模型/T02 迁移保持；本服务是 Service
  层第二道门禁。

7 个业务域（与 PRD §4.5/4.6/4.8/4.9/4.10 对齐；T02 既有枚举值；§4.5 文本
中"RESERVED"在 T02 已落地为 IN_TRANSIT，以 T02 实际为准）：

- 合同   CONTRACT       DRAFT/PENDING_APPROVAL/ACTIVE/EXPIRED/TERMINATED
- 冰柜   FREEZER        IN_STOCK/IN_TRANSIT/DEPLOYED/UNDER_MAINTENANCE/SCRAPPED
- 补货   REPLENISHMENT  REQUESTED/CONFIRMED/IN_TRANSIT/DELIVERED/OVERDUE/CANCELLED
- 整改   SUPERVISION_ISSUE  OPEN/IN_PROGRESS/RESOLVED/OVERDUE（§4.8 PRD 文本
        描述 OPEN→IN_PROGRESS→SUBMITTED→VERIFIED→CLOSED 与 T02 实际枚举不一致，
        以 T02 实际为准；VERIFIED/CLOSED 留待后续按业务细则扩展，留 TODO）
- 采购   PROCUREMENT    REQUESTED/APPROVED/ORDERED/RECEIVED/CANCELLED
- 绩效   PERFORMANCE     DRAFT/REVIEWED/APPROVED
- 工资   PAYROLL         DRAFT/REVIEWED/APPROVED/PAID
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.errors import DomainError

# 业务域标识（与审计 object_type 一致，便于跨卡复用）
DOMAIN_CONTRACT = "CONTRACT"
DOMAIN_FREEZER = "FREEZER"
DOMAIN_REPLENISHMENT = "REPLENISHMENT"
DOMAIN_SUPERVISION_ISSUE = "SUPERVISION_ISSUE"
DOMAIN_PROCUREMENT = "PROCUREMENT"
DOMAIN_PERFORMANCE = "PERFORMANCE"
DOMAIN_PAYROLL = "PAYROLL"


@dataclass(frozen=True)
class _Transition:
    from_state: str
    to_state: str


class _DomainGraph:
    """业务域状态迁移图（不可变）。"""

    __slots__ = ("name", "states", "transitions")

    def __init__(
        self, name: str, states: frozenset[str], transitions: frozenset[_Transition]
    ) -> None:
        if not states:
            raise ValueError(f"业务域 {name} 状态集合不能为空")
        unknown = {t.from_state for t in transitions} | {t.to_state for t in transitions}
        if unknown - states:
            raise ValueError(f"业务域 {name} 迁移引用了未知状态：{unknown - states}")
        self.name = name
        self.states = states
        self.transitions = transitions

    def is_legal(self, frm: str, to: str) -> bool:
        return _Transition(frm, to) in self.transitions


# ===== 各域合法迁移图 =====

_CONTRACT = _DomainGraph(
    name=DOMAIN_CONTRACT,
    states=frozenset({"DRAFT", "PENDING_APPROVAL", "ACTIVE", "EXPIRED", "TERMINATED"}),
    transitions=frozenset(
        {
            _Transition("DRAFT", "PENDING_APPROVAL"),  # 提交审批
            _Transition("DRAFT", "TERMINATED"),        # 起草中直接废止
            _Transition("PENDING_APPROVAL", "ACTIVE"), # 审批通过（合同生效）
            _Transition("PENDING_APPROVAL", "DRAFT"),  # 退回补充材料
            _Transition("PENDING_APPROVAL", "TERMINATED"),  # 审批驳回（直接终止）
            _Transition("ACTIVE", "EXPIRED"),          # 到期自动失效
            _Transition("ACTIVE", "TERMINATED"),       # 主动终止
        }
    ),
)

_FREEZER = _DomainGraph(
    name=DOMAIN_FREEZER,
    states=frozenset({"IN_STOCK", "IN_TRANSIT", "DEPLOYED", "UNDER_MAINTENANCE", "SCRAPPED"}),
    transitions=frozenset(
        {
            _Transition("IN_STOCK", "IN_TRANSIT"),         # 出库在途
            _Transition("IN_STOCK", "UNDER_MAINTENANCE"),  # 库存即送修
            _Transition("IN_STOCK", "SCRAPPED"),           # 库存报废
            _Transition("IN_TRANSIT", "DEPLOYED"),         # 投放完成
            _Transition("IN_TRANSIT", "IN_STOCK"),         # 撤柜在途回到在库
            _Transition("IN_TRANSIT", "UNDER_MAINTENANCE"),# 撤柜后送修
            _Transition("DEPLOYED", "IN_TRANSIT"),         # 撤柜
            _Transition("DEPLOYED", "UNDER_MAINTENANCE"),  # 在投期间送修
            _Transition("DEPLOYED", "SCRAPPED"),           # 在投期间报废
            _Transition("UNDER_MAINTENANCE", "IN_STOCK"),  # 修好归库
            _Transition("UNDER_MAINTENANCE", "SCRAPPED"),  # 维修报废
        }
    ),
)

_REPLENISHMENT = _DomainGraph(
    name=DOMAIN_REPLENISHMENT,
    states=frozenset(
        {"REQUESTED", "CONFIRMED", "IN_TRANSIT", "DELIVERED", "OVERDUE", "CANCELLED"}
    ),
    transitions=frozenset(
        {
            _Transition("REQUESTED", "CONFIRMED"),
            _Transition("REQUESTED", "CANCELLED"),
            _Transition("CONFIRMED", "IN_TRANSIT"),
            _Transition("CONFIRMED", "CANCELLED"),
            _Transition("IN_TRANSIT", "DELIVERED"),
            _Transition("IN_TRANSIT", "OVERDUE"),  # 到货逾期
            _Transition("OVERDUE", "DELIVERED"),
            _Transition("OVERDUE", "CANCELLED"),
        }
    ),
)

_SUPERVISION_ISSUE = _DomainGraph(
    name=DOMAIN_SUPERVISION_ISSUE,
    states=frozenset({"OPEN", "IN_PROGRESS", "RESOLVED", "OVERDUE"}),
    transitions=frozenset(
        {
            _Transition("OPEN", "IN_PROGRESS"),
            _Transition("OPEN", "OVERDUE"),
            _Transition("OPEN", "RESOLVED"),  # 直接整改完成（无中间过程）
            _Transition("IN_PROGRESS", "RESOLVED"),
            _Transition("IN_PROGRESS", "OVERDUE"),
            _Transition("OVERDUE", "IN_PROGRESS"),
            _Transition("OVERDUE", "RESOLVED"),
        }
    ),
)

_PROCUREMENT = _DomainGraph(
    name=DOMAIN_PROCUREMENT,
    states=frozenset({"REQUESTED", "APPROVED", "ORDERED", "RECEIVED", "CANCELLED"}),
    transitions=frozenset(
        {
            _Transition("REQUESTED", "APPROVED"),
            _Transition("REQUESTED", "CANCELLED"),
            _Transition("APPROVED", "ORDERED"),
            _Transition("APPROVED", "CANCELLED"),
            _Transition("ORDERED", "RECEIVED"),
            _Transition("ORDERED", "CANCELLED"),
        }
    ),
)

_PERFORMANCE = _DomainGraph(
    name=DOMAIN_PERFORMANCE,
    states=frozenset({"DRAFT", "REVIEWED", "APPROVED"}),
    transitions=frozenset(
        {
            _Transition("DRAFT", "REVIEWED"),
            _Transition("REVIEWED", "APPROVED"),
            _Transition("REVIEWED", "DRAFT"),  # 复核退回
        }
    ),
)

_PAYROLL = _DomainGraph(
    name=DOMAIN_PAYROLL,
    states=frozenset({"DRAFT", "REVIEWED", "APPROVED", "PAID"}),
    transitions=frozenset(
        {
            _Transition("DRAFT", "REVIEWED"),
            _Transition("REVIEWED", "APPROVED"),
            _Transition("REVIEWED", "DRAFT"),  # 复核退回
            _Transition("APPROVED", "PAID"),
        }
    ),
)


_GRAPH_BY_DOMAIN: dict[str, _DomainGraph] = {
    DOMAIN_CONTRACT: _CONTRACT,
    DOMAIN_FREEZER: _FREEZER,
    DOMAIN_REPLENISHMENT: _REPLENISHMENT,
    DOMAIN_SUPERVISION_ISSUE: _SUPERVISION_ISSUE,
    DOMAIN_PROCUREMENT: _PROCUREMENT,
    DOMAIN_PERFORMANCE: _PERFORMANCE,
    DOMAIN_PAYROLL: _PAYROLL,
}


def get_graph(domain: str) -> _DomainGraph:
    """按业务域返回迁移图（域名非法时抛 STATE_TRANSITION_INVALID）。"""
    g = _GRAPH_BY_DOMAIN.get(domain)
    if g is None:
        raise DomainError(
            code="STATE_TRANSITION_INVALID",
            message="未知业务域，无法校验状态迁移",
            details={"domain": domain},
        )
    return g


def assert_legal_transition(domain: str, frm: str, to: str) -> None:
    """校验 (from→to) 是否合法。非法即抛 DomainError。"""
    g = get_graph(domain)
    if frm not in g.states:
        raise DomainError(
            code="STATE_TRANSITION_INVALID",
            message="起始状态不在业务域合法状态集合内",
            details={"domain": domain, "from": frm, "allowed": sorted(g.states)},
        )
    if to not in g.states:
        raise DomainError(
            code="STATE_TRANSITION_INVALID",
            message="目标状态不在业务域合法状态集合内",
            details={"domain": domain, "to": to, "allowed": sorted(g.states)},
        )
    if not g.is_legal(frm, to):
        raise DomainError(
            code="STATE_TRANSITION_INVALID",
            message="该业务域不允许此状态跳转",
            details={"domain": domain, "from": frm, "to": to},
        )


def legal_targets(domain: str, frm: str) -> frozenset[str]:
    """返回指定起始状态可跳转的目标集合（供 UI/服务调用方展示）。"""
    g = get_graph(domain)
    if frm not in g.states:
        return frozenset()
    return frozenset(t.to_state for t in g.transitions if t.from_state == frm)

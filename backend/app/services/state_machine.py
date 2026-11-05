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
- 冰柜   FREEZER        IN_STOCK/RESERVED/DEPLOYED/PENDING_PICKUP/
                        PENDING_ACCEPTANCE/REPAIRING/SCRAPPED
                        （T10 v1.2；另保留 IN_TRANSIT/UNDER_MAINTENANCE 旧值兼容）
- 补货   REPLENISHMENT  T17 起 DRAFT/SUBMITTED/CONFIRMED/PARTIALLY_FULFILLED/
         FULFILLED/CLOSED/CANCELLED（PRD §4.9）；旧 REQUESTED/IN_TRANSIT/
         DELIVERED/OVERDUE 保留兼容历史台账
- 旧整改 SUPERVISION_ISSUE  OPEN/IN_PROGRESS/RESOLVED/OVERDUE（T03 预留表，首版
        无 service 写入；保留兼容）
- 整改   RECTIFICATION OPEN→IN_PROGRESS→SUBMITTED→VERIFIED→CLOSED（T16 / §4.8
        正式五态；VERIFIED/CLOSED 必须由非整改提交人完成，职责分离在 Service 强制）
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
# T16（PRD §4.8）：整改单五态正式状态机
DOMAIN_RECTIFICATION = "RECTIFICATION"
DOMAIN_PROCUREMENT = "PROCUREMENT"
# T18：采购单/厂商供货正式状态机（旧 DOMAIN_PROCUREMENT 图为 v12 预留台账词，
# 新业务一律使用 DOMAIN_PURCHASE）
DOMAIN_PURCHASE = "PURCHASE"
DOMAIN_PERFORMANCE = "PERFORMANCE"
# T19：月度绩效快照头独立状态图（旧 performance_records 用 DOMAIN_PERFORMANCE）
DOMAIN_PERFORMANCE_SNAPSHOT = "PERFORMANCE_SNAPSHOT"
DOMAIN_PAYROLL = "PAYROLL"
# T20：费用单独立状态图（提交→审批通过/驳回；未审批不进损益）
DOMAIN_EXPENSE = "EXPENSE"
# T20：工资单头复用 PAYROLL 图；为语义清晰保留别名
DOMAIN_PAYROLL_STATEMENT = DOMAIN_PAYROLL
# T21：风险项状态图（OPEN→ACKNOWLEDGED→CLOSED；允许 OPEN 直接 CLOSED）
DOMAIN_RISK = "RISK"


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
    # T09（PRD §4.4/§4.6）扩展为申请-审批-版本生命周期；
    # 保留旧 ACTIVE/EXPIRED 兼容 T03 台账语义。
    name=DOMAIN_CONTRACT,
    states=frozenset(
        {
            "DRAFT",
            "PENDING_APPROVAL",
            "RETURNED",
            "REJECTED",
            "APPROVED",
            "EFFECTIVE",
            "TERMINATION_PENDING",
            "TERMINATED",
            "ACTIVE",
            "EXPIRED",
        }
    ),
    transitions=frozenset(
        {
            _Transition("DRAFT", "PENDING_APPROVAL"),        # 提交审批
            _Transition("DRAFT", "TERMINATED"),              # 起草中直接废止
            _Transition("PENDING_APPROVAL", "APPROVED"),     # 审批通过
            _Transition("PENDING_APPROVAL", "RETURNED"),     # 退回补充材料
            _Transition("PENDING_APPROVAL", "REJECTED"),     # 审批拒绝
            _Transition("PENDING_APPROVAL", "TERMINATED"),   # 兼容旧口径：驳回直接终止
            _Transition("RETURNED", "PENDING_APPROVAL"),     # 补充后重新提交
            _Transition("RETURNED", "DRAFT"),                # 退回后转草稿编辑
            _Transition("REJECTED", "DRAFT"),                # 拒绝后可改草稿重新申请
            _Transition("APPROVED", "EFFECTIVE"),            # 批准后生效（固化 v1）
            _Transition("PENDING_APPROVAL", "EFFECTIVE"),    # 审批通过即生效
            _Transition("EFFECTIVE", "TERMINATION_PENDING"), # 发起终止申请
            _Transition("TERMINATION_PENDING", "TERMINATED"),# 终止批准（记录保留）
            _Transition("TERMINATION_PENDING", "EFFECTIVE"), # 终止驳回，恢复生效
            _Transition("ACTIVE", "EXPIRED"),                # 到期自动失效
            _Transition("ACTIVE", "TERMINATED"),             # 主动终止
        }
    ),
)

_FREEZER = _DomainGraph(
    # T10（PRD §4.5 v1.2）：
    #   IN_STOCK → RESERVED → DEPLOYED → PENDING_PICKUP → PENDING_ACCEPTANCE
    #   → IN_STOCK（验收入库，可用库存 +1）
    #                         ↘ REPAIRING → IN_STOCK / SCRAPPED（报废不可逆）
    # 另保留 IN_TRANSIT / UNDER_MAINTENANCE 旧状态及其迁移，兼容 T02 台账历史数据；
    # 新业务流程只写 v1.2 状态（夹具导入时历史词经映射转换）。
    name=DOMAIN_FREEZER,
    states=frozenset(
        {
            "IN_STOCK",
            "RESERVED",
            "DEPLOYED",
            "PENDING_PICKUP",
            "PENDING_ACCEPTANCE",
            "REPAIRING",
            "SCRAPPED",
            "IN_TRANSIT",            # 旧台账词，新流程不写
            "UNDER_MAINTENANCE",     # 旧台账词，新流程不写
        }
    ),
    transitions=frozenset(
        {
            # —— v1.2 主链路 ——
            _Transition("IN_STOCK", "RESERVED"),            # 建投放单/出库前预留
            _Transition("RESERVED", "DEPLOYED"),            # 现场投放完成
            _Transition("RESERVED", "IN_STOCK"),            # 取消预留
            _Transition("DEPLOYED", "PENDING_PICKUP"),      # 终止审批→建撤柜任务
            _Transition("PENDING_PICKUP", "PENDING_ACCEPTANCE"),  # 现场回收（≠在库）
            _Transition("PENDING_ACCEPTANCE", "IN_STOCK"),  # 验收入库（库存 +1）
            _Transition("PENDING_ACCEPTANCE", "REPAIRING"), # 验收判故障→维修
            _Transition("PENDING_ACCEPTANCE", "SCRAPPED"),  # 验收判报废（不可逆）
            _Transition("IN_STOCK", "REPAIRING"),           # 在库发现故障送修
            _Transition("REPAIRING", "IN_STOCK"),           # 修复归库
            _Transition("REPAIRING", "SCRAPPED"),           # 维修无果报废
            # —— T02 旧台账兼容（历史数据/旧测试；新 Service 不主动使用）——
            _Transition("IN_STOCK", "IN_TRANSIT"),
            _Transition("IN_STOCK", "UNDER_MAINTENANCE"),
            _Transition("IN_TRANSIT", "DEPLOYED"),
            _Transition("IN_TRANSIT", "IN_STOCK"),
            _Transition("IN_TRANSIT", "UNDER_MAINTENANCE"),
            _Transition("DEPLOYED", "IN_TRANSIT"),
            _Transition("DEPLOYED", "UNDER_MAINTENANCE"),
            _Transition("DEPLOYED", "SCRAPPED"),
            _Transition("UNDER_MAINTENANCE", "IN_STOCK"),
            _Transition("UNDER_MAINTENANCE", "SCRAPPED"),
        }
    ),
)

_REPLENISHMENT = _DomainGraph(
    # T17 起以 PRD §4.9 为准：
    # DRAFT → SUBMITTED → CONFIRMED → PARTIALLY_FULFILLED / FULFILLED → CLOSED；
    # SUBMITTED/CONFIRMED/PARTIALLY_FULFILLED 可 CANCELLED（取消必须留原因，Q093，
    # 由 Service 强制）。REQUESTED/IN_TRANSIT/DELIVERED/OVERDUE 为旧台账词，保留在
    # states 中兼容历史数据，新流程不再迁入。
    name=DOMAIN_REPLENISHMENT,
    states=frozenset(
        {
            "DRAFT", "SUBMITTED", "CONFIRMED",
            "PARTIALLY_FULFILLED", "FULFILLED", "CLOSED", "CANCELLED",
            "REQUESTED", "IN_TRANSIT", "DELIVERED", "OVERDUE",
        }
    ),
    transitions=frozenset(
        {
            _Transition("DRAFT", "SUBMITTED"),
            _Transition("SUBMITTED", "CONFIRMED"),
            _Transition("SUBMITTED", "CANCELLED"),
            _Transition("CONFIRMED", "PARTIALLY_FULFILLED"),
            _Transition("CONFIRMED", "FULFILLED"),
            _Transition("CONFIRMED", "CANCELLED"),
            _Transition("PARTIALLY_FULFILLED", "PARTIALLY_FULFILLED"),  # 多次部分履约
            _Transition("PARTIALLY_FULFILLED", "FULFILLED"),
            _Transition("PARTIALLY_FULFILLED", "CANCELLED"),
            _Transition("FULFILLED", "CLOSED"),
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

# T16（PRD §4.8）：整改单五态严格顺序，不允许跳级/回退
_RECTIFICATION = _DomainGraph(
    name=DOMAIN_RECTIFICATION,
    states=frozenset({"OPEN", "IN_PROGRESS", "SUBMITTED", "VERIFIED", "CLOSED"}),
    transitions=frozenset(
        {
            _Transition("OPEN", "IN_PROGRESS"),
            _Transition("IN_PROGRESS", "SUBMITTED"),
            _Transition("SUBMITTED", "VERIFIED"),
            _Transition("VERIFIED", "CLOSED"),
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

_PURCHASE = _DomainGraph(
    # T18（PRD §4.9 / §5.1）：内部 DRAFT→CONFIRMED；厂商对已确认单
    # SUPPLIER_CONFIRMED（供货确认，非付款/非入库）或 SUPPLY_REJECTED（驳回）；
    # 内部收货 RECEIVED；DRAFT/CONFIRMED/SUPPLIER_CONFIRMED 可 CANCELLED（留原因）。
    name=DOMAIN_PURCHASE,
    states=frozenset(
        {
            "DRAFT", "CONFIRMED", "SUPPLIER_CONFIRMED",
            "SUPPLY_REJECTED", "RECEIVED", "CANCELLED",
        }
    ),
    transitions=frozenset(
        {
            _Transition("DRAFT", "CONFIRMED"),
            _Transition("DRAFT", "CANCELLED"),
            _Transition("CONFIRMED", "SUPPLIER_CONFIRMED"),
            _Transition("CONFIRMED", "SUPPLY_REJECTED"),
            _Transition("CONFIRMED", "CANCELLED"),
            # 厂商重新确认（先驳回后再协商）：仅允许从驳回态重新确认
            _Transition("SUPPLY_REJECTED", "SUPPLIER_CONFIRMED"),
            _Transition("SUPPLIER_CONFIRMED", "RECEIVED"),
            _Transition("SUPPLIER_CONFIRMED", "CANCELLED"),
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

# T19 月度绩效快照：月末冻结(DRAFT)→经理确认(CONFIRMED)→财务复核(REVIEWED)；
# 复核可退回经理确认；确认/复核后原行冻结，只能追加更正单。
_PERFORMANCE_SNAPSHOT = _DomainGraph(
    name=DOMAIN_PERFORMANCE_SNAPSHOT,
    states=frozenset({"DRAFT", "CONFIRMED", "REVIEWED"}),
    transitions=frozenset({
        _Transition("DRAFT", "CONFIRMED"),
        _Transition("CONFIRMED", "REVIEWED"),
        _Transition("REVIEWED", "CONFIRMED"),   # 财务退回经理补充依据
    }),
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

# T20 费用：提交待审批 → 通过 / 驳回（无中间编辑；REJECTED 终态，需重新录入）
_EXPENSE = _DomainGraph(
    name=DOMAIN_EXPENSE,
    states=frozenset({"PENDING_APPROVAL", "APPROVED", "REJECTED"}),
    transitions=frozenset(
        {
            _Transition("PENDING_APPROVAL", "APPROVED"),
            _Transition("PENDING_APPROVAL", "REJECTED"),
        }
    ),
)


# T21 风险项（PRD §4.11）：新生成 OPEN；经理确认 ACKNOWLEDGED；处置完成 CLOSED。
# 关闭必须填写处理结果（在 RiskService 强制），记录不物理删除。
_RISK = _DomainGraph(
    name=DOMAIN_RISK,
    states=frozenset({"OPEN", "ACKNOWLEDGED", "CLOSED"}),
    transitions=frozenset(
        {
            _Transition("OPEN", "ACKNOWLEDGED"),
            _Transition("OPEN", "CLOSED"),          # 直接处置完成
            _Transition("ACKNOWLEDGED", "CLOSED"),
        }
    ),
)


_GRAPH_BY_DOMAIN: dict[str, _DomainGraph] = {
    DOMAIN_CONTRACT: _CONTRACT,
    DOMAIN_FREEZER: _FREEZER,
    DOMAIN_REPLENISHMENT: _REPLENISHMENT,
    DOMAIN_SUPERVISION_ISSUE: _SUPERVISION_ISSUE,
    DOMAIN_RECTIFICATION: _RECTIFICATION,
    DOMAIN_PROCUREMENT: _PROCUREMENT,
    DOMAIN_PURCHASE: _PURCHASE,
    DOMAIN_PERFORMANCE: _PERFORMANCE,
    DOMAIN_PERFORMANCE_SNAPSHOT: _PERFORMANCE_SNAPSHOT,
    DOMAIN_PAYROLL: _PAYROLL,
    DOMAIN_EXPENSE: _EXPENSE,
    DOMAIN_RISK: _RISK,
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

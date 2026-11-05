"""冰柜投放、撤回、验收与维修服务（PRD §4.4 / §4.5 / §6.3；R2、R11、R12、R13、R37）。

职责（业务规则、状态机、事务边界都在此；Router 不裁决，Repository 只持久化）：
- 冰柜台账购置入库；建投放单（在库→已预留）→ 现场投放（→已投放，收缴押金）；
- 合同终止审批通过后，为在店冰柜创建撤柜任务（已投放→待撤回）；
- 现场回收只到“待验收”，**回收完成不等于在库**；仓库验收为“在库”才 +1（R11）；
- 验收可转维修/报废；维修中可修复归库或报废；**报废不可逆**（R13）；
- 每次状态迁移写资产流水；**一台冰柜任一时刻只绑一个当前商家**（R12，行级锁）；
- 押金收/退写流水并关联投放单/验收单，生成“退押金评估”财务待办；
  押金是往来款，**不写 finance_transactions、不进经营损益**（R37）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.core.errors import ConflictError, DomainError, PermissionDeniedError
from app.core.permissions import (
    MODULE_FREEZER,
    ActorContext,
    permission_service,
)
from app.models import Brand, Contract, FreezerAsset, FreezerMovement, Merchant
from app.models.enums import FreezerAssetStatus
from app.repositories.freezer_repository import FreezerRepository
from app.schemas.freezer import (
    FreezerAcceptanceRequest,
    FreezerDeployConfirmRequest,
    FreezerDeployRequest,
    FreezerDepositRefundRequest,
    FreezerMovementView,
    FreezerPickupConfirmRequest,
    FreezerPurchaseRequest,
    FreezerRecallDecisionRequest,
    FreezerRecallRequestCreate,
    FreezerRecallRescheduleRequest,
    FreezerRepairRequest,
    FreezerScrapRequest,
    FreezerView,
)
from app.services.business_transition_service import BusinessTransitionService
from app.services.report_scope import resolve_merchant_scope
from app.services.state_machine import DOMAIN_FREEZER
from app.services.task_service import (
    TASK_KIND_FREEZER_DEPOSIT_REFUND,
    TASK_KIND_FREEZER_RECALL,
    TaskService,
)

# 状态别名（集中口径，禁止在别处重算）
S_IN_STOCK = FreezerAssetStatus.IN_STOCK.value
S_RESERVED = FreezerAssetStatus.RESERVED.value
S_DEPLOYED = FreezerAssetStatus.DEPLOYED.value
S_PENDING_PICKUP = FreezerAssetStatus.PENDING_PICKUP.value
S_PENDING_ACCEPTANCE = FreezerAssetStatus.PENDING_ACCEPTANCE.value
S_REPAIRING = FreezerAssetStatus.REPAIRING.value
S_SCRAPPED = FreezerAssetStatus.SCRAPPED.value

M_PURCHASE = "PURCHASE"
M_ORDER = "DEPLOYMENT_ORDER"
M_DEPLOY = "DEPLOY"
M_RECALL = "RECALL"
M_RECALL_REQUEST = "RECALL_REQUEST"
M_RECALL_REJECT = "RECALL_REJECT"
M_RECALL_RESCHEDULE = "RECALL_RESCHEDULE"
M_PICKUP = "PICKUP"
M_ACCEPT = "ACCEPT"
M_REPAIR_START = "REPAIR_START"
M_REPAIR_END = "REPAIR_END"
M_SCRAP = "SCRAP"
M_DEPOSIT_COLLECTED = "DEPOSIT_COLLECTED"
M_DEPOSIT_REFUNDED = "DEPOSIT_REFUNDED"

# 可做投放/回收现场动作的角色；验收/报废/退款由更窄角色执行（见各方法）
_FIELD_ROLES = {"SALES_REP", "REGIONAL_MANAGER", "SYSTEM_ADMIN", "SUPERVISOR"}
_WAREHOUSE_ROLES = {"SUPERVISOR", "SYSTEM_ADMIN", "REGIONAL_MANAGER"}
_FINANCE_ROLES = {"FINANCE", "SYSTEM_ADMIN"}
# 冰柜资产台账是**现场实物视图**：财务购置、片区经理/督导看管并投放。
# 系统管理员不参与实物管理，因此不开放台账（列表与详情同一口径）。
_LEDGER_DENIED_ROLES = {"SYSTEM_ADMIN"}
# 撤回申请审批人：片区经理（不可自审）
_RECALL_APPROVER_ROLES = {"REGIONAL_MANAGER"}

# 撤回申请状态
R_PENDING = "PENDING"
R_APPROVED = "APPROVED"
R_REJECTED = "REJECTED"

_DOC_PREFIX = {
    M_ORDER: "FDO",
    M_DEPLOY: "FDP",
    M_RECALL: "FRC",
    M_RECALL_REQUEST: "FRQ",
    M_RECALL_REJECT: "FRJ",
    M_RECALL_RESCHEDULE: "FRS",
    M_PICKUP: "FPK",
    M_ACCEPT: "FAC",
    M_DEPOSIT_COLLECTED: "FDC",
    M_DEPOSIT_REFUNDED: "FDR",
}


class FreezerService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = FreezerRepository(session)
        self._tasks = TaskService(session)

    # ---------------- 鉴权 ----------------
    @staticmethod
    def _require(actor: ActorContext) -> None:
        permission_service.require_ability(actor, MODULE_FREEZER)

    def _require_role(self, actor: ActorContext, roles: set[str], message: str) -> None:
        self._require(actor)
        if actor.role_code not in roles:
            raise PermissionDeniedError(code="FREEZER_FORBIDDEN", message=message)

    def _get_locked(self, asset_id: UUID) -> FreezerAsset:
        asset = self.repo.get(asset_id, for_update=True)
        if asset is None:
            raise DomainError(code="FREEZER_NOT_FOUND", message="冰柜不存在或已不可见")
        return asset

    def _assert_ledger_allowed(self, actor: ActorContext) -> None:
        """台账入口守卫：系统管理员不参与冰柜实物管理（现场资产视图）。

        列表、详情、投放三个入口同一口径，避免"看不到列表却能盲投"。
        """
        if actor.role_code in _LEDGER_DENIED_ROLES:
            raise PermissionDeniedError(
                code="FREEZER_LEDGER_FORBIDDEN",
                message="冰柜资产台账由片区经理/督导与财务维护，当前角色不可见",
            )

    def _assert_asset_readable(self, actor: ActorContext, asset: FreezerAsset) -> None:
        """台账行级可见性（与 `search_assets` 同一口径，防止绕过列表直连详情）。

        - 系统管理员：不参与实物管理 → 不可见；
        - 全局角色（财务）：可见；
        - 片区角色（经理/督导）：本片区已投放资产 + **未绑定商家的可投放池**；
        - 业务员：仅本人负责商家；未投放资产不可见（由片区角色投放）。
        """
        self._assert_ledger_allowed(actor)
        scope = resolve_merchant_scope(self.session, actor)
        if scope.is_global:
            return
        merchant_id = asset.current_merchant_id
        if merchant_id is None:
            # 未投放（在库/维修中/待验收）：属于片区可投放池
            if actor.profile.row_scope == "region":
                return
            raise PermissionDeniedError(
                code="FREEZER_NOT_VISIBLE",
                message="未投放冰柜由片区经理/督导管理与投放",
            )
        if scope.merchant_ids is not None:
            if merchant_id not in set(scope.merchant_ids):
                raise PermissionDeniedError(
                    code="FREEZER_NOT_VISIBLE", message="只能查看本人负责商家的冰柜"
                )
            return
        merchant = self.session.get(Merchant, merchant_id)
        if merchant is None or merchant.region_id not in (scope.region_ids or ()):
            raise PermissionDeniedError(
                code="FREEZER_REGION_FORBIDDEN", message="不能查看其他片区的冰柜资产"
            )

    @staticmethod
    def _doc_no(prefix: str) -> str:
        return f"{prefix}-{uuid4().hex[:16].upper()}"

    # ---------------- 台账：购置入库 ----------------
    def purchase(
        self, actor: ActorContext, payload: FreezerPurchaseRequest
    ) -> FreezerAsset:
        self._require_role(actor, _WAREHOUSE_ROLES | {"FINANCE"}, "无权登记冰柜资产")
        if self.repo.get_by_code(payload.asset_code) is not None:
            raise ConflictError(
                code="FREEZER_CODE_DUPLICATE", message="冰柜资产编号必须唯一"
            )
        # 数据域必须随品牌（服务端推导），不能落到 DatasetMixin 的随机默认值上，
        # 否则资产数据域与品牌/组织错位，退押金等待办可能派给其他数据域的用户。
        brand = self.session.get(Brand, payload.brand_id)
        asset = FreezerAsset(
            asset_code=payload.asset_code.strip(),
            brand_id=payload.brand_id,
            model=payload.model.strip(),
            purchase_date=payload.purchase_date,
            purchase_price_fen=payload.purchase_price_fen,
            deposit_fen=payload.deposit_fen,
            status=S_IN_STOCK,
            dataset_type=brand.dataset_type if brand is not None else "PRODUCTION",
            dataset_id=brand.dataset_id if brand is not None else uuid4(),
        )
        self.repo.add_asset(asset)
        self._write_movement(
            asset,
            M_PURCHASE,
            actor.user_id,
            amount_fen=payload.purchase_price_fen,
            to_status=S_IN_STOCK,
            note=payload.note or "购置入库",
        )
        self.session.flush()

        # T35：冰柜购置即登记资产支出流水（只记账不支付；幂等键=资产单）
        from app.models.finance import SOURCE_EXPENSE_ASSET
        from app.services.finance_ledger_service import FinanceLedgerService

        FinanceLedgerService(self.session).record_expense(
            actor,
            biz_date=asset.purchase_date,
            amount_fen=int(asset.purchase_price_fen),
            source_kind=SOURCE_EXPENSE_ASSET,
            related_type="FREEZER_ASSET",
            related_id=asset.id,
            note=f"冰柜 {asset.asset_code} 购置入库",
            dataset_type=asset.dataset_type,
            dataset_id=asset.dataset_id,
        )
        return asset

    # ---------------- 投放单：在库 → 已预留 ----------------
    def create_deployment(
        self,
        actor: ActorContext,
        asset_id: UUID,
        payload: FreezerDeployRequest,
    ) -> FreezerMovement:
        """创建投放单（出库前预留）。投柜前置合同必须生效（R2，Q048）。"""
        self._assert_ledger_allowed(actor)
        self._require_role(actor, _FIELD_ROLES, "无权创建投放单")
        # R2：只有生效合同才能投放（复用合同守卫，不允许绕过）
        from app.services.contract_service import ContractService

        contract = ContractService(self.session).assert_can_install_freezer(
            actor, payload.contract_id
        )
        asset = self._get_locked(asset_id)
        if asset.status != S_IN_STOCK:
            raise ConflictError(
                code="FREEZER_NOT_AVAILABLE",
                message="只有在库冰柜可创建投放单",
                details={"asset_id": str(asset.id), "status": asset.status},
            )

        deposit = (
            payload.deposit_fen
            if payload.deposit_fen is not None
            else asset.deposit_fen
        )
        if deposit is not None:
            asset.deposit_fen = deposit  # 固化本次投放押金口径到台账
        BusinessTransitionService(self.session).transition(
            entity=asset,
            domain=DOMAIN_FREEZER,
            to_state=S_RESERVED,
            actor_id=actor.user_id,
            reason=f"创建投放单（合同 {contract.contract_no}）",
        )
        movement = self._write_movement(
            asset,
            M_ORDER,
            actor.user_id,
            contract_id=contract.id,
            merchant_id=contract.merchant_id,
            from_status=S_IN_STOCK,
            to_status=S_RESERVED,
            note=payload.note or "投放单已创建，等待现场投放",
        )
        # 新投放周期：清空上一轮撤回申请，避免历史 APPROVED/REJECTED 影响本次申请
        self._reset_recall_request(asset)
        self.session.flush()
        return movement

    # ---------------- 现场投放：已预留 → 已投放（收缴押金）----------------
    def confirm_deployment(
        self,
        actor: ActorContext,
        asset_id: UUID,
        payload: FreezerDeployConfirmRequest,
    ) -> tuple[FreezerAsset, FreezerMovement]:
        self._require_role(actor, _FIELD_ROLES, "无权确认现场投放")
        asset = self._get_locked(asset_id)
        if asset.status != S_RESERVED:
            raise ConflictError(
                code="FREEZER_NOT_RESERVED",
                message="只有已预留（已建投放单）的冰柜可现场投放",
                details={"status": asset.status},
            )
        order = self._latest_movement(asset.id, M_ORDER)
        if order is None or order.merchant_id is None or order.contract_id is None:
            raise DomainError(
                code="FREEZER_ORDER_MISSING",
                message="投放单缺失或不完整，无法现场投放",
            )
        contract = self.session.get(Contract, order.contract_id)
        if contract is None or contract.status != "EFFECTIVE":
            # 二次守卫：现场投放瞬间合同已被终止则拒绝
            raise ConflictError(
                code="CONTRACT_NOT_EFFECTIVE_FOR_FREEZER",
                message="合同已非生效状态，禁止投放",
            )
        # R12：此刻绑定唯一当前商家与责任人
        asset.current_merchant_id = order.merchant_id
        asset.responsible_user_id = actor.user_id
        BusinessTransitionService(self.session).transition(
            entity=asset,
            domain=DOMAIN_FREEZER,
            to_state=S_DEPLOYED,
            actor_id=actor.user_id,
            reason="现场投放完成并绑定当前商家",
        )
        asset.last_deployed_at = datetime.now(tz=UTC)
        deploy = self._write_movement(
            asset,
            M_DEPLOY,
            actor.user_id,
            contract_id=order.contract_id,
            merchant_id=order.merchant_id,
            attachment_id=payload.attachment_id,
            from_status=S_RESERVED,
            to_status=S_DEPLOYED,
            note=payload.note or "现场投放完成",
        )
        # 押金收缴流水（关联投放单 DEPLOY，R37）；金额为 0 也记一笔 0 元收缴
        if asset.deposit_fen is not None:
            self._write_movement(
                asset,
                M_DEPOSIT_COLLECTED,
                actor.user_id,
                contract_id=order.contract_id,
                merchant_id=order.merchant_id,
                related=deploy,
                amount_fen=asset.deposit_fen,
                to_status=S_DEPLOYED,
                note="投放时收缴冰柜押金（往来款，不入损益）",
            )
        self.session.flush()
        return asset, deploy

    # ---------------- 撤回申请：业务员发起 → 片区经理审批 → 待撤回 ----------------
    def request_recall(
        self,
        actor: ActorContext,
        asset_id: UUID,
        payload: FreezerRecallRequestCreate,
    ) -> FreezerAsset:
        """提交撤回（撤柜）申请：资产保持「已投放」，等片区经理审批。

        业务员发现商家不再需要/合同将到期时可发起；必须填计划回收日期与原因，
        审批通过后才转「待撤回」并生成回收待办，避免绕过审批直接撤走在店资产。
        """
        self._require_role(actor, _FIELD_ROLES, "无权提交撤回申请")
        asset = self._get_locked(asset_id)
        self._assert_asset_readable(actor, asset)
        if asset.status != S_DEPLOYED:
            raise ConflictError(
                code="FREEZER_NOT_DEPLOYED",
                message="只有在店（已投放）冰柜可申请撤回",
                details={"status": asset.status},
            )
        if asset.recall_status in (R_PENDING, R_APPROVED):
            raise ConflictError(
                code="FREEZER_RECALL_ALREADY_OPEN",
                message="该冰柜已有进行中的撤回申请",
                details={"recall_status": asset.recall_status},
            )
        reason = payload.reason.strip()
        if not reason:
            raise DomainError(
                code="FREEZER_RECALL_REASON_REQUIRED", message="撤回申请必须填写原因"
            )
        asset.recall_status = R_PENDING
        asset.recall_planned_date = payload.planned_pickup_date
        asset.recall_reason = reason
        asset.recall_requested_by = actor.user_id
        asset.recall_requested_at = datetime.now(tz=UTC)
        asset.recall_decided_by = None
        asset.recall_decided_at = None
        asset.recall_decision_note = None
        self._write_movement(
            asset,
            M_RECALL_REQUEST,
            actor.user_id,
            merchant_id=asset.current_merchant_id,
            from_status=asset.status,
            to_status=asset.status,
            note=f"撤回申请：{reason}；计划回收 {payload.planned_pickup_date}",
        )
        self.session.flush()
        return asset

    def decide_recall(
        self,
        actor: ActorContext,
        asset_id: UUID,
        payload: FreezerRecallDecisionRequest,
    ) -> FreezerAsset:
        """片区经理审批撤回申请（不可自审）。

        APPROVE：已投放 → 待撤回，写撤柜单并给业务员派回收待办；
        REJECT：资产状态不变，仅记录驳回原因，业务员可重新申请。
        """
        self._require_role(actor, _RECALL_APPROVER_ROLES, "只有片区经理可审批撤回申请")
        asset = self._get_locked(asset_id)
        self._assert_asset_readable(actor, asset)
        if asset.recall_status != R_PENDING:
            raise ConflictError(
                code="FREEZER_RECALL_NOT_PENDING",
                message="该冰柜没有待审批的撤回申请",
                details={"recall_status": asset.recall_status},
            )
        if asset.recall_requested_by == actor.user_id:
            raise PermissionDeniedError(
                code="FREEZER_RECALL_SELF_APPROVAL",
                message="撤回申请不可自审，请由其他片区经理审批",
            )
        now = datetime.now(tz=UTC)
        asset.recall_decided_by = actor.user_id
        asset.recall_decided_at = now
        asset.recall_decision_note = (payload.note or "").strip() or None
        if payload.planned_pickup_date is not None:
            asset.recall_planned_date = payload.planned_pickup_date

        if payload.decision == "REJECT":
            asset.recall_status = R_REJECTED
            self._write_movement(
                asset,
                M_RECALL_REJECT,
                actor.user_id,
                merchant_id=asset.current_merchant_id,
                from_status=asset.status,
                to_status=asset.status,
                note=f"撤回申请驳回：{asset.recall_decision_note or '未填写原因'}",
            )
            self.session.flush()
            return asset

        # 同意：已投放 → 待撤回（商家绑定保留到现场回收，符合 R12 绑定约束）
        merchant_id = asset.current_merchant_id
        contract_id = self._recall_contract_id(asset.id)
        asset.recall_status = R_APPROVED
        BusinessTransitionService(self.session).transition(
            entity=asset,
            domain=DOMAIN_FREEZER,
            to_state=S_PENDING_PICKUP,
            actor_id=actor.user_id,
            reason=f"撤回申请审批通过；计划回收 {asset.recall_planned_date}",
        )
        asset.last_recalled_at = now
        self._write_movement(
            asset,
            M_RECALL,
            actor.user_id,
            contract_id=contract_id,
            merchant_id=merchant_id,
            from_status=S_DEPLOYED,
            to_status=S_PENDING_PICKUP,
            note="撤回申请审批通过，创建回收待办",
        )
        self._assign_pickup_task(asset)
        self.session.flush()
        return asset

    def reschedule_recall(
        self,
        actor: ActorContext,
        asset_id: UUID,
        payload: FreezerRecallRescheduleRequest,
    ) -> FreezerAsset:
        """修改计划回收日期：申请人本人或片区经理；现场回收确认后锁定。"""
        self._require(actor)
        asset = self._get_locked(asset_id)
        self._assert_asset_readable(actor, asset)
        if asset.recall_status not in (R_PENDING, R_APPROVED):
            raise ConflictError(
                code="FREEZER_RECALL_NOT_OPEN",
                message="该冰柜没有进行中的撤回申请，无法修改计划回收日期",
                details={"recall_status": asset.recall_status},
            )
        if asset.status not in (S_DEPLOYED, S_PENDING_PICKUP):
            raise ConflictError(
                code="FREEZER_RECALL_DATE_LOCKED",
                message="冰柜已进入回收/验收环节，计划回收日期不可再修改",
                details={"status": asset.status},
            )
        if (
            actor.user_id != asset.recall_requested_by
            and actor.role_code not in _RECALL_APPROVER_ROLES
        ):
            raise PermissionDeniedError(
                code="FREEZER_RECALL_RESCHEDULE_FORBIDDEN",
                message="只有申请人本人或片区经理可修改计划回收日期",
            )
        old_date = asset.recall_planned_date
        asset.recall_planned_date = payload.planned_pickup_date
        note = (payload.note or "").strip()
        self._write_movement(
            asset,
            M_RECALL_RESCHEDULE,
            actor.user_id,
            merchant_id=asset.current_merchant_id,
            from_status=asset.status,
            to_status=asset.status,
            note=f"计划回收日期 {old_date} → {payload.planned_pickup_date}"
            + (f"；{note}" if note else ""),
        )
        self.session.flush()
        return asset

    # ---------------- 合同终止联动：已投放 → 待撤回（建撤柜任务）----------------
    def on_contract_terminated(
        self,
        *,
        contract: Contract,
        actor_id: UUID,
        reason: str,
    ) -> list[FreezerMovement]:
        """终止审批通过后调用：为商家当前在店冰柜建撤柜任务（幂等）。

        - 仅 DEPLOYED 的冰柜转 PENDING_PICKUP；RESERVED（未现场投放）直接取消预留回库；
        - 每台冰柜写 RECALL 流水；为责任人幂等创建 FREEZER_RECALL 待办；
        - 不经 HTTP，故不做能力校验；事务归调用方。
        """
        movements: list[FreezerMovement] = []
        for asset in self.repo.deployed_for_merchant(contract.merchant_id):
            locked = self.repo.get(asset.id, for_update=True)
            if locked is None:
                continue
            if locked.status == S_DEPLOYED:
                BusinessTransitionService(self.session).transition(
                    entity=locked,
                    domain=DOMAIN_FREEZER,
                    to_state=S_PENDING_PICKUP,
                    actor_id=actor_id,
                    reason=f"合同 {contract.contract_no} 终止：{reason}",
                )
                locked.last_recalled_at = datetime.now(tz=UTC)
                mov = self._write_movement(
                    locked,
                    M_RECALL,
                    actor_id,
                    contract_id=contract.id,
                    merchant_id=locked.current_merchant_id,
                    from_status=S_DEPLOYED,
                    to_status=S_PENDING_PICKUP,
                    note="终止审批通过，创建撤柜任务",
                )
                movements.append(mov)
                self._safe_assign_recall_task(locked, contract)
            elif locked.status == S_RESERVED:
                # 建了投放单但未现场投放：取消预留，直接回库，无需撤柜
                BusinessTransitionService(self.session).transition(
                    entity=locked,
                    domain=DOMAIN_FREEZER,
                    to_state=S_IN_STOCK,
                    actor_id=actor_id,
                    reason="合同终止，取消未执行的投放预留",
                )
        self.session.flush()
        return movements

    @staticmethod
    def _reset_recall_request(asset: FreezerAsset) -> None:
        """清空撤回申请（新一轮投放周期开始时调用）。"""
        asset.recall_status = None
        asset.recall_planned_date = None
        asset.recall_reason = None
        asset.recall_requested_by = None
        asset.recall_requested_at = None
        asset.recall_decided_by = None
        asset.recall_decided_at = None
        asset.recall_decision_note = None

    def _assign_pickup_task(self, asset: FreezerAsset) -> None:
        """撤回申请审批通过后，给责任人派「撤柜/现场回收」待办（幂等）。"""
        assignee = asset.responsible_user_id or asset.recall_requested_by
        if assignee is None:
            fallback = self.repo.first_active_user_with_role(
                "SALES_REP", dataset_id=asset.dataset_id
            )
            assignee = fallback.id if fallback is not None else None
        if assignee is None:
            return
        planned = asset.recall_planned_date
        try:
            self._tasks.assign(
                title=f"撤柜任务：冰柜 {asset.asset_code}",
                assignee_id=assignee,
                task_kind=TASK_KIND_FREEZER_RECALL,
                related_object_type="FREEZER",
                related_object_id=asset.id,
                description=(
                    f"撤回申请已审批通过，请于 {planned} 前预约现场回收并拍照记录外观/故障"
                    if planned
                    else "撤回申请已审批通过，请预约现场回收并拍照记录外观/故障"
                ),
            )
        except ConflictError:
            # 已有未关闭撤柜任务：幂等跳过（R27）
            pass

    def _safe_assign_recall_task(self, asset: FreezerAsset, contract: Contract) -> None:
        assignee = asset.responsible_user_id or contract.sales_rep_user_id
        if assignee is None:
            fallback = self.repo.first_active_user_with_role(
                "SALES_REP", dataset_id=asset.dataset_id
            )
            assignee = fallback.id if fallback is not None else None
        if assignee is None:
            # 无可用责任人时不阻断终止；记审计由状态迁移保证，待办留待分派
            return
        try:
            self._tasks.assign(
                title=f"撤柜任务：冰柜 {asset.asset_code}（合同 {contract.contract_no}）",
                assignee_id=assignee,
                task_kind=TASK_KIND_FREEZER_RECALL,
                related_object_type="FREEZER",
                related_object_id=asset.id,
                description="合同已终止，请预约现场回收并拍照记录外观/故障",
            )
        except ConflictError:
            # 已有未关闭撤柜任务：幂等跳过（R27）
            pass

    # ---------------- 现场回收：待撤回 → 待验收（R11：不是在库）----------------
    def confirm_pickup(
        self,
        actor: ActorContext,
        asset_id: UUID,
        payload: FreezerPickupConfirmRequest,
    ) -> FreezerMovement:
        self._require_role(actor, _FIELD_ROLES, "无权确认现场回收")
        asset = self._get_locked(asset_id)
        if asset.status != S_PENDING_PICKUP:
            raise ConflictError(
                code="FREEZER_NOT_PENDING_PICKUP",
                message="只有待撤回冰柜可做现场回收；回收后进入待验收，尚未增加库存",
                details={"status": asset.status},
            )
        merchant_id = asset.current_merchant_id
        contract_id = self._recall_contract_id(asset.id)
        # 先解绑当前商家（CHECK 要求 PENDING_ACCEPTANCE 无商家绑定），再做状态迁移
        asset.current_merchant_id = None
        BusinessTransitionService(self.session).transition(
            entity=asset,
            domain=DOMAIN_FREEZER,
            to_state=S_PENDING_ACCEPTANCE,
            actor_id=actor.user_id,
            reason="现场回收完成，进入仓库待验收（库存尚未增加）",
        )
        # 责任人保留至验收完成（归属历史保留在流水）
        mov = self._write_movement(
            asset,
            M_PICKUP,
            actor.user_id,
            contract_id=contract_id,
            merchant_id=merchant_id,
            attachment_id=payload.attachment_id,
            from_status=S_PENDING_PICKUP,
            to_status=S_PENDING_ACCEPTANCE,
            note=payload.note or "现场回收，待仓库验收",
        )
        self.session.flush()
        return mov

    # ---------------- 仓库验收：待验收 → 在库/维修/报废（R11）----------------
    def accept(
        self,
        actor: ActorContext,
        asset_id: UUID,
        payload: FreezerAcceptanceRequest,
    ) -> tuple[FreezerAsset, FreezerMovement]:
        self._require_role(actor, _WAREHOUSE_ROLES, "无权做仓库验收")
        asset = self._get_locked(asset_id)
        if asset.status != S_PENDING_ACCEPTANCE:
            raise ConflictError(
                code="FREEZER_NOT_PENDING_ACCEPTANCE",
                message="只有待验收冰柜可做仓库验收",
                details={"status": asset.status},
            )
        contract_id = self._recall_contract_id(asset.id)
        if payload.result == "IN_STOCK":
            target = S_IN_STOCK
        elif payload.result == "REPAIR":
            target = S_REPAIRING
        else:
            target = S_SCRAPPED

        BusinessTransitionService(self.session).transition(
            entity=asset,
            domain=DOMAIN_FREEZER,
            to_state=target,
            actor_id=actor.user_id,
            reason=f"仓库验收结论：{payload.result}",
        )
        if target == S_REPAIRING:
            asset.repair_count += 1
        accept_mov = self._write_movement(
            asset,
            M_ACCEPT,
            actor.user_id,
            contract_id=contract_id,
            attachment_id=payload.attachment_id,
            from_status=S_PENDING_ACCEPTANCE,
            to_status=target,
            note=payload.note or f"验收：{payload.result}",
        )
        # R11：只有验收为在库才可用库存 +1（count_in_stock 仅统计 IN_STOCK）。
        # R37：撤柜验收为在库/维修/报废后，自动生成“退押金评估”财务待办。
        self._create_deposit_refund_task(actor, asset, accept_mov)
        self.session.flush()
        return asset, accept_mov

    # ---------------- 维修 / 报废 ----------------
    def start_repair(
        self, actor: ActorContext, asset_id: UUID, payload: FreezerRepairRequest
    ) -> FreezerMovement:
        """在库发现故障 → 维修中（Q052）。"""
        self._require_role(actor, _WAREHOUSE_ROLES, "无权登记维修")
        asset = self._get_locked(asset_id)
        BusinessTransitionService(self.session).transition(
            entity=asset,
            domain=DOMAIN_FREEZER,
            to_state=S_REPAIRING,
            actor_id=actor.user_id,
            reason="故障送修",
        )
        asset.repair_count += 1
        mov = self._write_movement(
            asset,
            M_REPAIR_START,
            actor.user_id,
            attachment_id=payload.attachment_id,
            from_status=S_IN_STOCK,
            to_status=S_REPAIRING,
            note=payload.note or "故障转维修",
        )
        self.session.flush()
        return mov

    def complete_repair(
        self, actor: ActorContext, asset_id: UUID, payload: FreezerRepairRequest
    ) -> FreezerMovement:
        """维修结束 → 在库。"""
        self._require_role(actor, _WAREHOUSE_ROLES, "无权完成维修")
        asset = self._get_locked(asset_id)
        BusinessTransitionService(self.session).transition(
            entity=asset,
            domain=DOMAIN_FREEZER,
            to_state=S_IN_STOCK,
            actor_id=actor.user_id,
            reason="维修完成归库",
        )
        mov = self._write_movement(
            asset,
            M_REPAIR_END,
            actor.user_id,
            attachment_id=payload.attachment_id,
            from_status=S_REPAIRING,
            to_status=S_IN_STOCK,
            note=payload.note or "修复归库",
        )
        self.session.flush()
        return mov

    def scrap(
        self, actor: ActorContext, asset_id: UUID, payload: FreezerScrapRequest
    ) -> FreezerMovement:
        """维修中报废（不可逆，R13/Q053）。报废后任何正向迁移都被状态机拒绝。"""
        self._require_role(actor, _WAREHOUSE_ROLES, "无权报废冰柜")
        asset = self._get_locked(asset_id)
        BusinessTransitionService(self.session).transition(
            entity=asset,
            domain=DOMAIN_FREEZER,
            to_state=S_SCRAPPED,
            actor_id=actor.user_id,
            reason=payload.reason,
        )
        mov = self._write_movement(
            asset,
            M_SCRAP,
            actor.user_id,
            attachment_id=payload.attachment_id,
            from_status=S_REPAIRING,
            to_status=S_SCRAPPED,
            note=payload.reason,
        )
        self.session.flush()
        return mov

    # ---------------- 财务确认退押金（R37，不入损益）----------------
    def confirm_deposit_refund(
        self,
        actor: ActorContext,
        asset_id: UUID,
        payload: FreezerDepositRefundRequest,
    ) -> FreezerMovement:
        """财务确认实际退款：写 DEPOSIT_REFUNDED 流水，关联验收单与原收缴。

        - 金额精确到分，默认全额；不写 finance_transactions、不进损益；
        - 一笔收缴只能退一次（幂等）；退款须凭证（attachment 可选但建议上传）。
        """
        self._require_role(actor, _FINANCE_ROLES, "只有财务可确认押金退还")
        asset = self._get_locked(asset_id)
        collected = self.repo.latest_open_deposit(asset.id)
        if collected is None:
            raise ConflictError(
                code="FREEZER_DEPOSIT_NOT_REFUNDABLE",
                message="没有待退还的冰柜押金（未收缴或已退还）",
            )
        amount = (
            payload.amount_fen if payload.amount_fen is not None else collected.amount_fen or 0
        )
        if amount > (collected.amount_fen or 0):
            raise DomainError(
                code="FREEZER_DEPOSIT_REFUND_EXCEEDS",
                message="退款金额不能超过收缴押金",
                details={"collected_fen": collected.amount_fen, "refund_fen": amount},
            )
        # 退款流水直接关联原押金收缴单；收缴单又关联投放单（可一路追溯到合同/验收）。
        accept_mov = self._latest_movement(asset.id, M_ACCEPT)
        refund = self._write_movement(
            asset,
            M_DEPOSIT_REFUNDED,
            actor.user_id,
            contract_id=collected.contract_id,
            merchant_id=collected.merchant_id,
            related=collected,
            amount_fen=amount,
            attachment_id=payload.attachment_id,
            to_status=asset.status,
            note=payload.note or "财务确认退还冰柜押金（往来款，不入损益）",
        )
        # 关闭由验收单触发的“退押金评估”财务待办（单据动作关闭，Q023 模式）
        self._close_refund_task(actor, asset, accept_mov)
        self.session.flush()
        return refund

    # ---------------- 查询 ----------------
    def get_detail(
        self, actor: ActorContext, asset_id: UUID
    ) -> tuple[FreezerView, list]:
        self._require(actor)
        asset = self.repo.get(asset_id)
        if asset is None:
            raise DomainError(code="FREEZER_NOT_FOUND", message="冰柜不存在或已不可见")
        self._assert_asset_readable(actor, asset)
        movements = [self.to_movement_view(m) for m in self.repo.list_movements(asset.id)]
        return self.to_view(asset), movements

    def available_stock_count(self, actor: ActorContext) -> int:
        """可用库存口径：仅 IN_STOCK（R11）。指标口径唯一，调用方不得另算。"""
        self._require(actor)
        return self.repo.count_in_stock()

    def search_assets(
        self,
        actor: ActorContext,
        *,
        asset_code: str | None = None,
        status: str | None = None,
        merchant_id: UUID | None = None,
        limit: int = 50,
    ) -> list[FreezerView]:
        """冰柜台账只读查询（T23 `freezer.search`）。

        行级范围只由服务端 actor 解析（PRD §2.4）：
        - 业务员 = 本人负责商家（未投放资产不可见，投放由片区角色执行）；
        - 片区角色（片区经理/督导）= 本片区已投放资产 + **未绑定商家的可投放池**，
          否则财务购置入库的冰柜对经理/督导永远不可见，也就无法创建投放单；
        - 财务（受控全局）= 不额外裁剪；系统管理员不参与实物管理，返回空集。
        成本字段（购置价/押金）由响应层按角色字段级脱敏，服务不重算。
        """
        self._require(actor)
        if actor.role_code in _LEDGER_DENIED_ROLES:
            # 台账是现场实物视图：系统管理员不参与实物管理，不开放
            return []
        scope = resolve_merchant_scope(self.session, actor)
        merchant_ids: list[UUID] | None = None
        region_ids: list[UUID] | None = None
        if not scope.is_global:
            if scope.merchant_ids is not None:
                merchant_ids = list(scope.merchant_ids)
            else:
                region_ids = list(scope.region_ids or ())
        if merchant_id is not None:
            if merchant_ids is not None and merchant_id not in merchant_ids:
                return []  # 越出自本人负责范围：无权，返回空集而非他人资产
            merchant_ids = None  # 已精确到单个商家
        assets = self.repo.search(
            asset_code=asset_code,
            status=status,
            merchant_id=merchant_id,
            merchant_ids=merchant_ids,
            region_ids=region_ids,
            # 片区角色的可投放池：未绑定商家的资产也要可见（否则财务购置的在库
            # 冰柜对经理/督导永远消失，无法创建投放单）
            include_unassigned=actor.profile.row_scope == "region",
            limit=limit,
        )
        return [self.to_view(a) for a in assets]

    @staticmethod
    def to_movement_view(m: FreezerMovement) -> FreezerMovementView:
        return FreezerMovementView(
            id=m.id,
            asset_id=m.asset_id,
            movement_type=m.movement_type,
            doc_no=m.doc_no,
            merchant_id=m.merchant_id,
            contract_id=m.contract_id,
            related_movement_id=m.related_movement_id,
            amount_fen=m.amount_fen,
            from_status=m.from_status,
            to_status=m.to_status,
            occurred_at=m.occurred_at,
            attachment_id=m.attachment_id,
            operator_id=m.operator_id,
            note=m.note,
        )

    @staticmethod
    def to_view(a: FreezerAsset) -> FreezerView:
        return FreezerView(
            id=a.id,
            asset_code=a.asset_code,
            brand_id=a.brand_id,
            model=a.model,
            purchase_date=a.purchase_date,
            purchase_price_fen=int(a.purchase_price_fen),
            deposit_fen=int(a.deposit_fen) if a.deposit_fen is not None else None,
            status=a.status,
            current_merchant_id=a.current_merchant_id,
            responsible_user_id=a.responsible_user_id,
            last_deployed_at=a.last_deployed_at,
            last_recalled_at=a.last_recalled_at,
            repair_count=a.repair_count,
            is_active=a.is_active,
            recall_status=a.recall_status,
            recall_planned_date=a.recall_planned_date,
            recall_reason=a.recall_reason,
            recall_requested_by=a.recall_requested_by,
            recall_requested_at=a.recall_requested_at,
            recall_decided_by=a.recall_decided_by,
            recall_decided_at=a.recall_decided_at,
            recall_decision_note=a.recall_decision_note,
        )

    # ---------------- 内部辅助 ----------------
    def _write_movement(
        self,
        asset: FreezerAsset,
        movement_type: str,
        operator_id: UUID,
        *,
        merchant_id: UUID | None = None,
        contract_id: UUID | None = None,
        related: FreezerMovement | None = None,
        amount_fen: int | None = None,
        attachment_id: UUID | None = None,
        from_status: str | None = None,
        to_status: str | None = None,
        note: str | None = None,
    ) -> FreezerMovement:
        prefix = _DOC_PREFIX.get(movement_type)
        mov = FreezerMovement(
            asset_id=asset.id,
            movement_type=movement_type,
            doc_no=self._doc_no(prefix) if prefix else None,
            merchant_id=merchant_id,
            contract_id=contract_id,
            related_movement_id=related.id if related is not None else None,
            amount_fen=amount_fen,
            from_status=from_status,
            to_status=to_status,
            attachment_id=attachment_id,
            operator_id=operator_id,
            note=note,
        )
        return self.repo.add_movement(mov)

    def _latest_movement(
        self, asset_id: UUID, movement_type: str
    ) -> FreezerMovement | None:
        for m in reversed(self.repo.list_movements(asset_id)):
            if m.movement_type == movement_type:
                return m
        return None

    def _recall_contract_id(self, asset_id: UUID) -> UUID | None:
        for m_type in (M_RECALL, M_ORDER, M_DEPLOY):
            m = self._latest_movement(asset_id, m_type)
            if m is not None and m.contract_id is not None:
                return m.contract_id
        return None

    def _create_deposit_refund_task(
        self,
        actor: ActorContext,
        asset: FreezerAsset,
        accept_mov: FreezerMovement,
    ) -> None:
        """验收归库/维修/报废后自动生成“退押金评估”财务待办（R37）。

        仅当存在尚未退还的押金收缴时生成；幂等（同冰柜同类型未关闭不重复）。
        """
        collected = self.repo.latest_open_deposit(asset.id)
        if collected is None:
            return
        finance = self.repo.first_active_user_with_role(
            "FINANCE", dataset_id=asset.dataset_id
        )
        assignee = finance.id if finance is not None else actor.user_id
        try:
            self._tasks.assign(
                title=f"退押金评估：冰柜 {asset.asset_code}",
                assignee_id=assignee,
                task_kind=TASK_KIND_FREEZER_DEPOSIT_REFUND,
                related_object_type="FREEZER",
                related_object_id=asset.id,
                description=(
                    f"撤柜验收已完成（验收单 {accept_mov.doc_no}），"
                    f"待退押金 {collected.amount_fen} 分，请核对凭证后确认退款；"
                    "押金为往来款，不计入经营损益"
                ),
            )
        except ConflictError:
            pass

    def _close_refund_task(
        self,
        actor: ActorContext,
        asset: FreezerAsset,
        accept_mov: FreezerMovement | None,
    ) -> None:
        from sqlalchemy import select

        from app.models import TaskItem

        task = self.session.scalars(
            select(TaskItem)
            .where(
                TaskItem.related_object_type == "FREEZER",
                TaskItem.related_object_id == asset.id,
                TaskItem.task_kind == TASK_KIND_FREEZER_DEPOSIT_REFUND,
                TaskItem.status.in_(["OPEN", "IN_PROGRESS"]),
            )
            .limit(1)
        ).first()
        if task is None:
            return
        self._tasks.close_by_document(
            task_id=task.id,
            actor_id=actor.user_id,
            document_object_type="FREEZER",
            document_object_id=asset.id,
            outcome="DEPOSIT_REFUNDED",
            reason=self._refund_close_reason(accept_mov),
        )

    @staticmethod
    def _refund_close_reason(accept_mov: FreezerMovement | None) -> str:
        if accept_mov is not None and accept_mov.doc_no:
            return f"财务已确认押金退还（验收单 {accept_mov.doc_no}）"
        return "财务已确认押金退还"

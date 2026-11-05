"""合同申请、审批与版本服务（PRD §4.4 / §4.6 / R38）。

职责（业务规则、状态机、事务边界都在此；Router 不裁决，Repository 只持久化）：
- 草稿 → 申请 → 退回/批准 → 生效 → 变更（新版本）→ 终止申请/终止；
- 固化陈列阈值、提成规则版本、生效/到期日；生效后历史版本不可覆盖；
- 只有 EFFECTIVE 合同可投柜、计新签、进入常规巡访（Q043 投柜前置守卫）；
- 业务员不能自审（Q044）；附件仅留痕，其文字内容不参与任何放行判断；
- 审批通过后创建相关任务（投柜/陈列核查）；
- 所有写操作走 BusinessTransitionService 状态机 + AuditService 同事务审计。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.errors import (
    ConflictError,
    DomainError,
    PermissionDeniedError,
)
from app.core.permissions import (
    MODULE_CONTRACT,
    ActorContext,
    permission_service,
)
from app.models import Contract, ContractVersion, FileObject
from app.models.enums import ContractStatus
from app.repositories.contract_repository import ContractRepository
from app.schemas.contract import (
    ContractChangeRequest,
    ContractDecisionRequest,
    ContractDraftRequest,
    ContractRenewalRequest,
    ContractRenewalView,
    ContractTerminateRequest,
    ContractVersionView,
    ContractView,
)
from app.services.audit_service import EVENT_CREATE, EVENT_UPDATE, AuditService
from app.services.business_transition_service import BusinessTransitionService
from app.services.contract_template import render_contract_body
from app.services.state_machine import DOMAIN_CONTRACT
from app.services.task_service import (
    TASK_KIND_FREEZER_ACCEPTANCE,
    TaskService,
)

# 可审批合同的角色（业务员不可自审）
_APPROVER_ROLES = {"REGIONAL_MANAGER", "SYSTEM_ADMIN"}
# 可起草/申请合同的角色
_DRAFT_ROLES = {"SALES_REP", "REGIONAL_MANAGER", "SYSTEM_ADMIN"}
# 可编辑条款的状态：草稿，以及"退回补充材料"后待重新提交的合同
_EDITABLE_STATUSES = {ContractStatus.DRAFT.value, ContractStatus.RETURNED.value}
# 续约申请已终结的状态：不再阻塞同一旧合同再次发起续约
_CLOSED_RENEWAL_STATUSES = {
    ContractStatus.REJECTED.value,
    ContractStatus.TERMINATED.value,
    ContractStatus.EXPIRED.value,
}


class ContractService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = ContractRepository(session)
        self._tasks = TaskService(session)

    # ---------------- 鉴权 ----------------
    @staticmethod
    def _require_ability(actor: ActorContext) -> None:
        try:
            permission_service.require_ability(actor, MODULE_CONTRACT)
        except PermissionDeniedError:
            raise
        except Exception as exc:  # 权限层通用异常 → 403
            raise PermissionDeniedError(
                code="PERMISSION_DENIED", message="当前角色无权访问合同中心"
            ) from exc

    def _assert_readable(self, actor: ActorContext, contract: Contract) -> None:
        """行级范围只由服务端 actor 的角色画像决定（PRD §2.4、§5.3）。

        经理/督导=本片区（按商家片区裁剪）；业务员=本人负责；财务/管理员=全局。
        """
        scope = actor.profile.row_scope
        if scope in ("global", "global_readonly"):
            return
        if scope == "region":
            merchant = self.repo.get_merchant(contract.merchant_id)
            if merchant is None or merchant.region_id not in actor.region_ids:
                raise PermissionDeniedError(
                    code="CONTRACT_REGION_FORBIDDEN",
                    message="只能查看本人负责片区的合同",
                )
            return
        if contract.sales_rep_user_id == actor.user_id:
            return
        raise PermissionDeniedError(
            code="CONTRACT_NOT_ASSIGNED",
            message="只能查看本人负责的合同",
        )

    # ---------------- 附件 ----------------
    def _bind_attachment(
        self,
        actor: ActorContext,
        contract: Contract,
        *,
        attachment_id: UUID | None,
        filename: str | None = None,
        content_type: str | None = None,
        size_bytes: int | None = None,
    ) -> UUID | None:
        """绑定已上传的合同附件（上传在 Router 经 FileService.save_upload 完成）。

        这里只校验附件存在、属合同业务类型、未被隔离；**不读取附件正文，
        文件名/文字内容绝不参与审批放行**（PRD §4.4 红线）。
        filename/content_type/size_bytes 仅为兼容 API 入参，服务端以已落库的
        FileObject 元数据为准，不采信客户端传入值。
        """
        if attachment_id is None:
            return contract.attachment_id
        file_obj = self.session.get(FileObject, attachment_id)
        if file_obj is None:
            raise DomainError(
                code="CONTRACT_ATTACHMENT_NOT_FOUND",
                message="合同附件不存在，请先上传",
            )
        if file_obj.scan_status == "INFECTED":
            raise DomainError(
                code="CONTRACT_ATTACHION_QUARANTINED",
                message="附件已被安全隔离，不能用于合同",
            )
        # 审计只记录文件引用，不记录正文
        AuditService(self.session).record(
            object_type="contract",
            object_id=contract.id,
            actor_id=actor.user_id,
            event_type=EVENT_UPDATE,
            after={"attachment_id": str(file_obj.id), "biz_type": file_obj.biz_type},
            reason="绑定合同附件",
        )
        return file_obj.id

    # ---------------- 草稿 / 申请 ----------------
    def create_draft(
        self, actor: ActorContext, payload: ContractDraftRequest
    ) -> Contract:
        self._require_ability(actor)
        if actor.role_code not in _DRAFT_ROLES:
            raise PermissionDeniedError(
                code="CONTRACT_DRAFT_FORBIDDEN", message="无权创建合同"
            )
        merchant = self.repo.get_merchant(payload.merchant_id)
        if merchant is None:
            raise DomainError(
                code="MERCHANT_NOT_FOUND", message="合同关联的商家不存在"
            )
        if self.repo.get_by_no(payload.contract_no) is not None:
            raise ConflictError(
                code="CONTRACT_NO_DUPLICATE",
                message="合同编号已存在，必须全局唯一",
            )
        self._validate_dates(payload.effective_from, payload.expire_at)

        contract = Contract(
            merchant_id=payload.merchant_id,
            contract_no=payload.contract_no,
            title=payload.title,
            party_name=payload.party_name,
            template_version=payload.template_version,
            status=ContractStatus.DRAFT.value,
            effective_from=payload.effective_from,
            expire_at=payload.expire_at,
            commission_rate=(
                Decimal(str(payload.commission_rate))
                if payload.commission_rate is not None
                else None
            ),
            display_subsidy_amount=payload.display_subsidy_amount,
            # 阈值固化（列 NOT NULL，默认 50）
            display_threshold_pct=(
                payload.display_threshold_pct
                if payload.display_threshold_pct is not None
                else 50
            ),
            commission_rule_version=payload.commission_rule_version,
            # 店长姓名：未提供时自动取商家主联系人，便于直接生成纸质合同
            store_manager_name=(
                payload.store_manager_name or merchant.primary_contact_name
            ),
            sales_rep_user_id=(
                actor.user_id if actor.role_code == "SALES_REP" else None
            ),
            manager_user_id=(
                actor.user_id if actor.role_code == "REGIONAL_MANAGER" else None
            ),
            current_version=1,
            dataset_type="PRODUCTION",
            dataset_id=merchant.dataset_id,
        )
        # 纸质合同正文：草稿创建即生成（可手工修订后保存覆盖）
        contract.body_text = payload.body_text or self._render_body(contract)
        self.session.add(contract)
        self.session.flush()

        if payload.attachment_id is not None:
            contract.attachment_id = self._bind_attachment(
                actor,
                contract,
                attachment_id=payload.attachment_id,
                filename=payload.attachment_filename,
                content_type=payload.attachment_content_type,
                size_bytes=payload.attachment_size_bytes,
            )

        AuditService(self.session).record(
            object_type="contract",
            object_id=contract.id,
            actor_id=actor.user_id,
            event_type=EVENT_CREATE,
            after={"status": contract.status, "contract_no": contract.contract_no},
            reason=payload.reason,
        )
        self.session.flush()
        return contract

    def update_draft(
        self, actor: ActorContext, contract_id: UUID, payload: ContractDraftRequest
    ) -> Contract:
        self._require_ability(actor)
        contract = self._get_or_404(contract_id)
        self._assert_owner_or_manager(actor, contract)
        # 草稿可编辑；RETURNED 按"退回补充材料"语义同样可补齐条款后重新提交，
        # 否则被退回的合同无法修正，形成不可恢复的死锁。
        if contract.status not in _EDITABLE_STATUSES:
            raise DomainError(
                code="CONTRACT_NOT_EDITABLE",
                message="仅草稿或被退回状态可编辑；生效后变更需走变更申请生成新版本",
            )
        self._validate_dates(payload.effective_from, payload.expire_at)
        existing = self.repo.get_by_no(payload.contract_no)
        if existing is not None and existing.id != contract.id:
            raise ConflictError(
                code="CONTRACT_NO_DUPLICATE", message="合同编号已被其他合同占用"
            )
        contract.contract_no = payload.contract_no
        contract.title = payload.title
        contract.party_name = payload.party_name
        contract.template_version = payload.template_version
        contract.effective_from = payload.effective_from
        contract.expire_at = payload.expire_at
        contract.commission_rate = (
            Decimal(str(payload.commission_rate))
            if payload.commission_rate is not None
            else None
        )
        contract.display_subsidy_amount = payload.display_subsidy_amount
        contract.display_threshold_pct = payload.display_threshold_pct
        contract.commission_rule_version = payload.commission_rule_version
        contract.store_manager_name = payload.store_manager_name
        # 正文：显式提交则保留手工修订；否则按最新条款重渲染，保持正文与字段同步
        contract.body_text = (
            payload.body_text
            if payload.body_text is not None
            else self._render_body(contract)
        )
        if payload.attachment_id is not None:
            contract.attachment_id = self._bind_attachment(
                actor,
                contract,
                attachment_id=payload.attachment_id,
                filename=payload.attachment_filename,
                content_type=payload.attachment_content_type,
                size_bytes=payload.attachment_size_bytes,
            )
        AuditService(self.session).record(
            object_type="contract",
            object_id=contract.id,
            actor_id=actor.user_id,
            event_type=EVENT_UPDATE,
            after={"title": contract.title},
            reason=payload.reason,
        )
        self.session.flush()
        return contract

    def submit(self, actor: ActorContext, contract_id: UUID, reason: str) -> Contract:
        """草稿 → 待审。提交前校验固化口径完整。"""
        self._require_ability(actor)
        contract = self._get_or_404(contract_id)
        self._assert_owner_or_manager(actor, contract)
        self._assert_fixed_terms_ready(contract)
        BusinessTransitionService(self.session).transition(
            entity=contract,
            domain=DOMAIN_CONTRACT,
            to_state=ContractStatus.PENDING_APPROVAL.value,
            actor_id=actor.user_id,
            reason=reason,
            extra_after={"submitted_by": str(actor.user_id)},
        )
        return contract

    # ---------------- 续约（新合同接续旧合同，审批后生效）----------------
    def create_renewal(
        self, actor: ActorContext, contract_id: UUID, payload: ContractRenewalRequest
    ) -> Contract:
        """基于生效合同追加一份接续合同（DRAFT），审批通过后按新生效日生效。

        - 只有 EFFECTIVE 合同可续约；业务员只能续约本人负责的合同（经理限本片区）；
        - 新生效日必须晚于旧合同到期日：两份合同有效期不重叠，避免"双份生效"；
        - 旧合同不改状态（到期自然失效），因此不需要"先终止再重签"；
        - 同一旧合同同时只允许存在一份未终结的续约申请（幂等防重）；
        - 条款缺省沿用旧合同（提成率/补贴/陈列阈值/规则版本/责任人）。
        """
        self._require_ability(actor)
        if actor.role_code not in _DRAFT_ROLES:
            raise PermissionDeniedError(
                code="CONTRACT_RENEWAL_FORBIDDEN", message="无权发起合同续约"
            )
        old = self._get_or_404(contract_id)
        self._assert_owner_or_manager(actor, old)
        if old.status != ContractStatus.EFFECTIVE.value:
            raise DomainError(
                code="CONTRACT_NOT_EFFECTIVE",
                message="只有生效合同可以续约",
                details={"status": old.status},
            )
        if old.expire_at is not None and payload.effective_from <= old.expire_at:
            raise DomainError(
                code="CONTRACT_RENEWAL_OVERLAP",
                message="新生效日必须晚于原合同到期日，避免两份合同有效期重叠",
                details={"old_expire_at": old.expire_at.isoformat()},
            )
        self._validate_dates(payload.effective_from, payload.expire_at)

        blocking = [
            r
            for r in self.repo.list_renewals(old.id)
            if r.status not in _CLOSED_RENEWAL_STATUSES
        ]
        if blocking:
            raise ConflictError(
                code="CONTRACT_RENEWAL_EXISTS",
                message="该合同已有进行中的续约申请，请先处理后再发起",
                details={
                    "renewal_id": str(blocking[0].id),
                    "contract_no": blocking[0].contract_no,
                    "status": blocking[0].status,
                },
            )

        contract_no = payload.contract_no or self._generate_renewal_no(old)
        if self.repo.get_by_no(contract_no) is not None:
            raise ConflictError(
                code="CONTRACT_NO_DUPLICATE",
                message="合同编号已存在，必须全局唯一",
            )

        renewal = Contract(
            merchant_id=old.merchant_id,
            contract_no=contract_no,
            title=payload.title or old.title,
            party_name=payload.party_name or old.party_name,
            template_version=old.template_version,
            status=ContractStatus.DRAFT.value,
            effective_from=payload.effective_from,
            expire_at=payload.expire_at,
            commission_rate=(
                Decimal(str(payload.commission_rate))
                if payload.commission_rate is not None
                else old.commission_rate
            ),
            display_subsidy_amount=(
                payload.display_subsidy_amount
                if payload.display_subsidy_amount is not None
                else old.display_subsidy_amount
            ),
            display_threshold_pct=(
                payload.display_threshold_pct
                if payload.display_threshold_pct is not None
                else old.display_threshold_pct
            ),
            commission_rule_version=(
                payload.commission_rule_version or old.commission_rule_version
            ),
            store_manager_name=old.store_manager_name,
            sales_rep_user_id=old.sales_rep_user_id,
            manager_user_id=old.manager_user_id,
            renewed_from_contract_id=old.id,
            current_version=1,
            dataset_type=old.dataset_type,
            dataset_id=old.dataset_id,
        )
        # 续约草稿同步生成纸质正文（编号/期限已变更，须重渲染而非沿用旧文）
        renewal.body_text = self._render_body(renewal)
        self.session.add(renewal)
        self.session.flush()

        AuditService(self.session).record(
            object_type="contract",
            object_id=renewal.id,
            actor_id=actor.user_id,
            event_type=EVENT_CREATE,
            after={
                "status": renewal.status,
                "contract_no": renewal.contract_no,
                "renewed_from_contract_id": str(old.id),
                "renewed_from_contract_no": old.contract_no,
                "effective_from": renewal.effective_from.isoformat()
                if renewal.effective_from
                else None,
                "expire_at": renewal.expire_at.isoformat() if renewal.expire_at else None,
            },
            reason=payload.reason,
        )
        self.session.flush()
        return renewal

    def create_application_from_ai(
        self,
        actor: ActorContext,
        *,
        merchant_id: UUID,
        title: str,
        effective_from: date,
        expire_at: date,
        commission_rate: float | None = None,
        reason: str = "AI 助手代拟合同申请",
    ) -> Contract:
        """MCP 受控入口：代拟并直接提交合同申请（DRAFT → PENDING_APPROVAL，T24）。

        合同编号由服务端生成（模型不能指定），甲方法人取商家名称；固化条款缺失
        仍由 submit 的 `_assert_fixed_terms_ready` 裁决，执行器不绕过业务规则。
        """
        merchant = self.repo.get_merchant(merchant_id)
        if merchant is None:
            raise DomainError(
                code="MERCHANT_NOT_FOUND", message="合同关联的商家不存在"
            )
        today = date.today()
        payload = ContractDraftRequest(
            merchant_id=merchant_id,
            contract_no=f"AI-{today.strftime('%Y%m%d')}-{merchant_id.hex[:8]}",
            title=title,
            party_name=merchant.name,
            effective_from=effective_from,
            expire_at=expire_at,
            commission_rate=commission_rate,
            reason="AI 助手代拟合同草稿",
        )
        contract = self.create_draft(actor, payload)
        return self.submit(actor, contract_id=contract.id, reason=reason)

    # ---------------- 审批 ----------------
    def decide(
        self,
        actor: ActorContext,
        contract_id: UUID,
        payload: ContractDecisionRequest,
    ) -> Contract:
        self._require_ability(actor)
        contract = self._get_or_404(contract_id)
        # 片区行级：经理只能审批本片区合同
        if actor.role_code == "REGIONAL_MANAGER":
            merchant = self.repo.get_merchant(contract.merchant_id)
            if merchant is not None and merchant.region_id not in actor.region_ids:
                raise PermissionDeniedError(
                    code="CONTRACT_REGION_FORBIDDEN",
                    message="不能审批其他片区的合同",
                )

        # Q044：业务员不能自审（即使是本人提交的合同）
        if actor.role_code not in _APPROVER_ROLES:
            raise PermissionDeniedError(
                code="CONTRACT_SELF_APPROVAL_FORBIDDEN",
                message="业务员不能审批合同，更不能自审",
            )
        if (
            contract.sales_rep_user_id == actor.user_id
            or contract.created_by_id == actor.user_id
        ):
            raise PermissionDeniedError(
                code="CONTRACT_SELF_APPROVAL_FORBIDDEN",
                message="提交人与审批人不能为同一人",
            )

        if payload.decision == "APPROVE":
            return self._approve(actor, contract, payload.comment)
        if payload.decision == "RETURN":
            return self._return(actor, contract, payload.comment)
        return self._reject(actor, contract, payload.comment)

    def _approve(
        self, actor: ActorContext, contract: Contract, comment: str
    ) -> Contract:
        # 待审 → 批准
        BusinessTransitionService(self.session).transition(
            entity=contract,
            domain=DOMAIN_CONTRACT,
            to_state=ContractStatus.APPROVED.value,
            actor_id=actor.user_id,
            reason=f"审批通过：{comment or '同意'}",
        )
        # 记录审批中的经理，作为后续续签责任人
        if contract.manager_user_id is None and actor.role_code == "REGIONAL_MANAGER":
            contract.manager_user_id = actor.user_id
        # 批准 → 生效（首版固化，Q045）
        BusinessTransitionService(self.session).transition(
            entity=contract,
            domain=DOMAIN_CONTRACT,
            to_state=ContractStatus.EFFECTIVE.value,
            actor_id=actor.user_id,
            reason="审批通过后合同生效",
            extra_after={
                "version": contract.current_version,
                "display_threshold_pct": contract.display_threshold_pct,
                "commission_rule_version": contract.commission_rule_version,
            },
        )
        contract.signed_at = contract.signed_at or date.today()
        # 生成不可变版本快照 v1
        self._snapshot_version(contract, change_type="INIT", actor_id=actor.user_id)
        # 审批后创建相关任务（PRD §4.4：投柜与陈列核查）
        self._create_post_approval_tasks(actor, contract)
        # 续约合同生效：关闭旧合同的续签评估待办并留痕（旧合同状态不变，到期自然失效）
        if contract.renewed_from_contract_id is not None:
            self._on_renewal_effective(actor, contract)
        self.session.flush()
        return contract

    def _on_renewal_effective(self, actor: ActorContext, renewal: Contract) -> None:
        """续约合同审批生效后的联动：旧合同待办收口 + 审计，不改旧合同状态。"""
        if renewal.renewed_from_contract_id is None:
            return
        old = self.repo.get_by_id(renewal.renewed_from_contract_id)
        if old is None:
            return
        for task in self.repo.open_renewal_tasks(old.id):
            try:
                self._tasks.close_by_document(
                    task_id=task.id,
                    actor_id=actor.user_id,
                    document_object_type="contract",
                    document_object_id=old.id,
                    outcome="RENEWED",
                    reason=f"续约合同 {renewal.contract_no} 已审批生效",
                )
            except ConflictError:
                pass
        AuditService(self.session).record(
            object_type="contract",
            object_id=old.id,
            actor_id=actor.user_id,
            event_type=EVENT_UPDATE,
            after={
                "renewed_by_contract_id": str(renewal.id),
                "renewed_by_contract_no": renewal.contract_no,
            },
            reason=f"续约合同 {renewal.contract_no} 生效，旧合同到期自然失效",
        )

    def _return(
        self, actor: ActorContext, contract: Contract, comment: str
    ) -> Contract:
        BusinessTransitionService(self.session).transition(
            entity=contract,
            domain=DOMAIN_CONTRACT,
            to_state=ContractStatus.RETURNED.value,
            actor_id=actor.user_id,
            reason=f"退回补充：{comment or '材料不完整'}",
        )
        return contract

    def _reject(
        self, actor: ActorContext, contract: Contract, comment: str
    ) -> Contract:
        BusinessTransitionService(self.session).transition(
            entity=contract,
            domain=DOMAIN_CONTRACT,
            to_state=ContractStatus.REJECTED.value,
            actor_id=actor.user_id,
            reason=f"审批拒绝：{comment or '不符合要求'}",
        )
        return contract

    # ---------------- 变更（新版本，Q046）----------------
    def change(
        self,
        actor: ActorContext,
        contract_id: UUID,
        payload: ContractChangeRequest,
    ) -> Contract:
        self._require_ability(actor)
        if actor.role_code not in _APPROVER_ROLES:
            raise PermissionDeniedError(
                code="CONTRACT_CHANGE_FORBIDDEN", message="无权变更合同"
            )
        contract = self._get_or_404(contract_id)
        if contract.status != ContractStatus.EFFECTIVE.value:
            raise DomainError(
                code="CONTRACT_NOT_EFFECTIVE",
                message="只有生效合同可以变更（变更生成新版本）",
            )
        old_expire_at = contract.expire_at
        new_effective = payload.effective_from or contract.effective_from
        new_expire = payload.expire_at or contract.expire_at
        self._validate_dates(new_effective, new_expire)
        expire_changed = (
            payload.expire_at is not None and payload.expire_at != old_expire_at
        )

        # 保留旧版本（已在 contract_versions 中），仅更新主表为新口径
        if payload.title is not None:
            contract.title = payload.title
        if payload.party_name is not None:
            contract.party_name = payload.party_name
        if payload.template_version is not None:
            contract.template_version = payload.template_version
        contract.effective_from = new_effective
        contract.expire_at = new_expire
        if payload.commission_rate is not None:
            contract.commission_rate = Decimal(str(payload.commission_rate))
        if payload.display_subsidy_amount is not None:
            contract.display_subsidy_amount = payload.display_subsidy_amount
        if payload.display_threshold_pct is not None:
            contract.display_threshold_pct = payload.display_threshold_pct
        if payload.commission_rule_version is not None:
            contract.commission_rule_version = payload.commission_rule_version
        if payload.attachment_id is not None:
            contract.attachment_id = self._bind_attachment(
                actor,
                contract,
                attachment_id=payload.attachment_id,
                filename=payload.attachment_filename,
                content_type=payload.attachment_content_type,
                size_bytes=payload.attachment_size_bytes,
            )

        contract.current_version += 1
        # 到期日变化 → 新一轮续签评估周期：关闭旧到期周期的未关闭续签待办，
        # 并重置幂等标记（R38：同一新到期周期可重新为业务员/经理生成各一条）。
        if expire_changed:
            for old_task in self.repo.open_renewal_tasks(contract.id):
                try:
                    self._tasks.close_by_document(
                        task_id=old_task.id,
                        actor_id=actor.user_id,
                        document_object_type="contract",
                        document_object_id=contract.id,
                        outcome="SUPERSEDED",
                        reason="合同到期日变更，旧续签评估待办作废",
                    )
                except ConflictError:
                    pass
        contract.renewal_generated_for_expire_at = None
        self.session.flush()
        self._snapshot_version(
            contract, change_type="CHANGE", actor_id=actor.user_id
        )
        AuditService(self.session).record(
            object_type="contract",
            object_id=contract.id,
            actor_id=actor.user_id,
            event_type=EVENT_UPDATE,
            after={"version": contract.current_version},
            reason=payload.reason,
        )
        self.session.flush()
        return contract

    # ---------------- 终止（不是删除，Q047）----------------
    def request_termination(
        self,
        actor: ActorContext,
        contract_id: UUID,
        payload: ContractTerminateRequest,
    ) -> Contract:
        """生效合同 → 终止待审。终止前状态保持 EFFECTIVE（Q047）。"""
        self._require_ability(actor)
        contract = self._get_or_404(contract_id)
        self._assert_owner_or_manager(actor, contract)
        BusinessTransitionService(self.session).transition(
            entity=contract,
            domain=DOMAIN_CONTRACT,
            to_state=ContractStatus.TERMINATION_PENDING.value,
            actor_id=actor.user_id,
            reason=payload.reason,
            extra_after={"terminate_date": str(payload.terminate_date or "")},
        )
        return contract

    def approve_termination(
        self, actor: ActorContext, contract_id: UUID, reason: str
    ) -> Contract:
        """终止待审 → 终止。合同记录保留不删除。"""
        self._require_ability(actor)
        if actor.role_code not in _APPROVER_ROLES:
            raise PermissionDeniedError(
                code="CONTRACT_TERMINATE_FORBIDDEN", message="无权审批终止"
            )
        contract = self._get_or_404(contract_id)
        BusinessTransitionService(self.session).transition(
            entity=contract,
            domain=DOMAIN_CONTRACT,
            to_state=ContractStatus.TERMINATED.value,
            actor_id=actor.user_id,
            reason=reason,
        )
        self._snapshot_version(
            contract, change_type="TERMINATE", actor_id=actor.user_id
        )
        # PRD §4.4/§4.5：终止审批通过后，为在店冰柜创建撤柜任务（同一事务）
        from app.services.freezer_service import FreezerService

        FreezerService(self.session).on_contract_terminated(
            contract=contract, actor_id=actor.user_id, reason=reason
        )
        return contract

    # ---------------- 投柜守卫（Q043）----------------
    def assert_can_install_freezer(
        self, actor: ActorContext, contract_id: UUID | None
    ) -> Contract:
        """投柜前置：只有生效合同可投柜；否则 409。

        供 T10 冰柜投放服务调用，不允许绕过（PRD §4.4）。
        """
        self._require_ability(actor)
        if contract_id is None:
            raise DomainError(
                code="CONTRACT_REQUIRED_FOR_FREEZER",
                message="投柜必须关联一份已生效合同",
            )
        contract = self._get_or_404(contract_id)
        if contract.status != ContractStatus.EFFECTIVE.value:
            raise ConflictError(
                code="CONTRACT_NOT_EFFECTIVE_FOR_FREEZER",
                message="合同未生效，禁止投柜；待合同审批生效后再投放",
                details={"contract_id": str(contract.id), "status": contract.status},
            )
        if contract.expire_at is not None and contract.expire_at < date.today():
            raise ConflictError(
                code="CONTRACT_EXPIRED_FOR_FREEZER",
                message="合同已到期，禁止投柜，请先续签",
            )
        return contract

    # ---------------- 查询 ----------------
    def list_contracts(
        self,
        actor: ActorContext,
        *,
        status: str | None = None,
        keyword: str | None = None,
    ) -> list[ContractView]:
        self._require_ability(actor)
        # 行级范围按角色画像：全局角色不限；片区角色（经理/督导）限本片区。
        scope = actor.profile.row_scope
        region_ids = None if scope in ("global", "global_readonly") else list(
            actor.region_ids
        )
        rows = self.repo.list_for_actor(
            region_ids=region_ids,
            owner_user_id=actor.user_id,
            role_code=actor.role_code,
            status=status,
            keyword=keyword,
        )
        return [self._to_view(c) for c in rows]

    def get_detail(
        self, actor: ActorContext, contract_id: UUID
    ) -> tuple[ContractView, list[ContractVersionView], list[ContractRenewalView]]:
        self._require_ability(actor)
        contract = self._get_or_404(contract_id)
        self._assert_readable(actor, contract)
        versions = [
            ContractVersionView.model_validate(
                {
                    "id": v.id,
                    "contract_id": v.contract_id,
                    "version": v.version,
                    "change_type": v.change_type,
                    "title": v.title,
                    "party_name": v.party_name,
                    "effective_from": v.effective_from,
                    "expire_at": v.expire_at,
                    "commission_rate": float(v.commission_rate)
                    if v.commission_rate is not None
                    else None,
                    "display_subsidy_amount": int(v.display_subsidy_amount)
                    if v.display_subsidy_amount is not None
                    else None,
                    "display_threshold_pct": v.display_threshold_pct,
                    "commission_rule_version": v.commission_rule_version,
                    "attachment_id": v.attachment_id,
                    "created_at": v.created_at,
                }
            )
            for v in self.repo.list_versions(contract_id)
        ]
        renewals = [
            ContractRenewalView(
                id=r.id,
                contract_no=r.contract_no,
                status=r.status,
                effective_from=r.effective_from,
                expire_at=r.expire_at,
            )
            for r in self.repo.list_renewals(contract_id)
        ]
        return self._to_view(contract), versions, renewals

    # ---------------- 内部辅助 ----------------
    def _get_or_404(self, contract_id: UUID) -> Contract:
        contract = self.repo.get_by_id(contract_id)
        if contract is None:
            raise DomainError(
                code="CONTRACT_NOT_FOUND", message="合同不存在或已不可见"
            )
        return contract

    def _assert_owner_or_manager(
        self, actor: ActorContext, contract: Contract
    ) -> None:
        if actor.role_code in {"REGIONAL_MANAGER", "SYSTEM_ADMIN"}:
            if actor.role_code == "REGIONAL_MANAGER":
                merchant = self.repo.get_merchant(contract.merchant_id)
                if merchant is not None and merchant.region_id not in actor.region_ids:
                    raise PermissionDeniedError(
                        code="CONTRACT_REGION_FORBIDDEN",
                        message="不能操作其他片区的合同",
                    )
            return
        if actor.role_code == "SALES_REP":
            if contract.sales_rep_user_id not in (None, actor.user_id):
                raise PermissionDeniedError(
                    code="CONTRACT_NOT_ASSIGNED",
                    message="只能操作本人负责的合同",
                )
            # 草稿期业务员可作为责任人；首次保存时绑定
            if contract.sales_rep_user_id is None:
                contract.sales_rep_user_id = actor.user_id
            return
        raise PermissionDeniedError(
            code="CONTRACT_FORBIDDEN", message="无权操作该合同"
        )

    @staticmethod
    def _validate_dates(
        effective_from: date | None, expire_at: date | None
    ) -> None:
        if (
            effective_from is not None
            and expire_at is not None
            and expire_at < effective_from
        ):
            raise DomainError(
                code="CONTRACT_DATE_INVALID",
                message="合同到期日不能早于生效日",
            )

    @staticmethod
    def _assert_fixed_terms_ready(contract: Contract) -> None:
        """提交审批前必须固化关键口径（PRD §4.4）。"""
        missing: list[str] = []
        if contract.effective_from is None:
            missing.append("生效日")
        if contract.expire_at is None:
            missing.append("到期日")
        # display_threshold_pct 列 NOT NULL 默认 50，始终已固化
        if not (0 <= contract.display_threshold_pct <= 100):
            missing.append("陈列阈值(0-100)")
        if contract.commission_rule_version is None:
            missing.append("提成规则版本")
        if missing:
            raise DomainError(
                code="CONTRACT_TERMS_INCOMPLETE",
                message="提交审批前必须固化：" + "、".join(missing),
                details={"missing": missing},
            )

    def _generate_renewal_no(self, old: Contract) -> str:
        """续约编号：`<原编号>-R<n>`（全局唯一，长度按 contract_no 上限截断）。"""
        base = f"{old.contract_no}-R"
        n = 1
        while self.repo.get_by_no(f"{base}{n}"[:32]) is not None:
            n += 1
        return f"{base}{n}"[:32]

    def _snapshot_version(
        self, contract: Contract, *, change_type: str, actor_id: UUID
    ) -> ContractVersion:
        existing = self.repo.get_version(contract.id, contract.current_version)
        if existing is not None:
            # 同版本已快照（幂等保护），不重复生成
            return existing
        cv = ContractVersion(
            contract_id=contract.id,
            version=contract.current_version,
            change_type=change_type,
            title=contract.title,
            party_name=contract.party_name,
            template_version=contract.template_version,
            signed_at=contract.signed_at,
            effective_from=contract.effective_from,
            expire_at=contract.expire_at,
            commission_rate=contract.commission_rate,
            display_subsidy_amount=contract.display_subsidy_amount,
            display_threshold_pct=contract.display_threshold_pct,
            commission_rule_version=contract.commission_rule_version,
            store_manager_name=contract.store_manager_name,
            body_text=contract.body_text,
            attachment_id=contract.attachment_id,
            snapshot={
                "contract_no": contract.contract_no,
                "merchant_id": str(contract.merchant_id),
            },
            dataset_type=contract.dataset_type,
            dataset_id=contract.dataset_id,
            created_by_id=actor_id,
            updated_by_id=actor_id,
        )
        return self.repo.add_version(cv)

    def _create_post_approval_tasks(
        self, actor: ActorContext, contract: Contract
    ) -> None:
        """审批通过后创建投柜/陈列核查任务（PRD §4.4）。

        R27 幂等：同合同同任务类型同责任人未关闭则不重复；重复触发跳过即可。
        """
        assignee = contract.sales_rep_user_id or actor.user_id
        try:
            self._tasks.assign(
                title=f"合同 {contract.contract_no} 生效：投柜与陈列核查",
                assignee_id=assignee,
                task_kind=TASK_KIND_FREEZER_ACCEPTANCE,
                related_object_type="contract",
                related_object_id=contract.id,
                description=(
                    f"陈列达标阈值 {contract.display_threshold_pct}%；"
                    "合同已生效，请安排投柜与陈列核查"
                ),
            )
        except ConflictError:
            # 同责任人已有未关闭投柜任务：幂等跳过，不影响合同生效
            pass

    @staticmethod
    def _render_body(contract: Contract) -> str:
        """按当前条款渲染纸质合同正文（商家店名/店长姓名/当前日期自动填入）。"""
        return render_contract_body(
            contract_no=contract.contract_no,
            party_name=contract.party_name,
            store_manager_name=contract.store_manager_name,
            effective_from=contract.effective_from,
            expire_at=contract.expire_at,
            display_threshold_pct=contract.display_threshold_pct,
            commission_rate=contract.commission_rate,
            commission_rule_version=contract.commission_rule_version,
        )

    @staticmethod
    def _to_view(c: Contract) -> ContractView:
        return ContractView(
            id=c.id,
            merchant_id=c.merchant_id,
            contract_no=c.contract_no,
            title=c.title,
            party_name=c.party_name,
            status=c.status,
            version=c.current_version,
            template_version=c.template_version,
            signed_at=c.signed_at,
            effective_from=c.effective_from,
            expire_at=c.expire_at,
            commission_rate=float(c.commission_rate) if c.commission_rate is not None else None,
            display_subsidy_amount=int(c.display_subsidy_amount)
            if c.display_subsidy_amount is not None
            else None,
            display_threshold_pct=c.display_threshold_pct,
            commission_rule_version=c.commission_rule_version,
            store_manager_name=c.store_manager_name,
            body_text=c.body_text,
            sales_rep_user_id=c.sales_rep_user_id,
            manager_user_id=c.manager_user_id,
            attachment_id=c.attachment_id,
            has_attachment=c.attachment_id is not None,
            renewal_generated_for_expire_at=c.renewal_generated_for_expire_at,
            renewed_from_contract_id=c.renewed_from_contract_id,
            created_at=c.created_at,
        )

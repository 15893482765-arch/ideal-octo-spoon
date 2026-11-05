"""商家中心业务服务（PRD §4.3 / §4.6 / R35）。

职责（业务规则、状态机、事务边界都在此，Router 不做裁决）：
- 列表/地图/详情的行级范围裁剪（业务员仅本人/临时授权；片区角色限本片区）
- 建档全局唯一编号校验 + 同片区店名/地址非阻断重复提示
- 责任归属走 MerchantAssignment 生效区间，主责任区间不得重叠
- 联系人/精确坐标按角色脱敏
- 等级 A/B/C 仅片区经理可维护，留审计且只影响生效日之后（不回溯历史）
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from difflib import SequenceMatcher
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.errors import (
    ConflictError,
    DomainError,
    PermissionDeniedError,
)
from app.core.permissions import (
    MODULE_MERCHANT,
    ActorContext,
    permission_service,
)
from app.core.redaction import mask_phone
from app.models import (
    Merchant,
    MerchantAssignment,
    MerchantGradeChange,
    User,
)
from app.models.enums import MerchantStatus, RoleCode
from app.repositories.contract_repository import ContractRepository
from app.repositories.merchant_repository import WIDE_SCOPE_ROLES, MerchantRepository
from app.schemas.merchant import (
    AssignmentCreateRequest,
    AssignmentView,
    GradeChangeRequest,
    GradeChangeView,
    MerchantCreateRequest,
    MerchantListItem,
    MerchantListQuery,
    MerchantStatusChangeRequest,
)
from app.services.audit_service import EVENT_CREATE, EVENT_UPDATE, AuditService

# 近似判定阈值（店名/地址相似度）；Python 侧确定性实现，便于 Q038 复现
_SIMILAR_THRESHOLD = 0.6
# 可看精确坐标/联系方式的角色
_SENSITIVE_COORD_ROLES = {
    "SALES_REP",
    "REGIONAL_MANAGER",
    "SUPERVISOR",
    "SYSTEM_ADMIN",
}
_CONTACT_VISIBLE_ROLES = {
    "SALES_REP",
    "REGIONAL_MANAGER",
    "SUPERVISOR",
    "SYSTEM_ADMIN",
    "FINANCE",
}
# 可维护等级/归属的角色
_GRADE_MANAGER_ROLES = {"REGIONAL_MANAGER"}
_ASSIGNMENT_MANAGER_ROLES = {"REGIONAL_MANAGER", "SYSTEM_ADMIN"}

# 商家状态的可写角色（PRD §4.3）：
# - 负责业务员改本人/临时授权商家（主路径）；
# - 片区经理在本片区内兜底（业务员离职、账号停用时的唯一出口）；
# - 督导/财务/系统管理员/厂商只读。
_STATUS_WRITABLE_ROLES = {"SALES_REP", "REGIONAL_MANAGER"}
_STATUS_MANAGER_ROLES = {"REGIONAL_MANAGER"}

# PRD §4.3:222 状态机：LEAD → NEGOTIATING → ACTIVE → PAUSED → TERMINATED。
# 业务员可推进并允许相邻回退（洽谈不成退回线索、暂停后恢复合作）；
# TERMINATED 不在业务员可直改集合内，必须走「取消合作申请 → 经理审批 → 撤柜」
# （PRD §6.3:455-460 / R2），由 change_status 显式拒绝。
_SALES_REP_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    MerchantStatus.LEAD.value: frozenset({MerchantStatus.NEGOTIATING.value}),
    MerchantStatus.NEGOTIATING.value: frozenset(
        {MerchantStatus.LEAD.value, MerchantStatus.ACTIVE.value}
    ),
    MerchantStatus.ACTIVE.value: frozenset({MerchantStatus.PAUSED.value}),
    MerchantStatus.PAUSED.value: frozenset({MerchantStatus.ACTIVE.value}),
    MerchantStatus.TERMINATED.value: frozenset(),
}


def allowed_status_transitions(status: str) -> frozenset[str]:
    """负责业务员可从 status 变更到的目标状态（前端提示用；裁决仍在 Service）。"""
    return _SALES_REP_STATUS_TRANSITIONS.get(status, frozenset())

UTC = UTC


def _shanghai_now() -> datetime:
    # 展示/业务周按 Asia/Shanghai；存储转 UTC
    return datetime.now(timezone(timedelta(hours=8))).astimezone(UTC)


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a.strip().lower(), b.strip().lower()).ratio()


class MerchantService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = MerchantRepository(session)
        # 「合作中」状态前置校验需要判断商家是否有生效合同（合同域只读查询）
        self.contract_repo = ContractRepository(session)

    # ---------------- 行级范围 ----------------
    def _scope_region_ids(self, actor: ActorContext) -> list[UUID] | None:
        """返回该 actor 允许读取的片区；全局角色返回 None 表示不限。"""
        if actor.role_code in WIDE_SCOPE_ROLES:
            return None
        if actor.profile.row_scope == "region":
            return list(actor.region_ids)
        if actor.profile.row_scope == "self":
            return list(actor.region_ids)
        # brand_region 等：厂商不看商家明细
        return []

    def _owned_merchant_ids(self, actor: ActorContext, at: datetime) -> list[UUID]:
        return self.repo.merchant_ids_for_owner(actor.user_id, at)

    def _assert_can_read_merchant(self, actor: ActorContext, merchant: Merchant) -> None:
        if actor.role_code in WIDE_SCOPE_ROLES:
            return
        if actor.profile.row_scope == "region":
            if merchant.region_id not in actor.region_ids:
                raise PermissionDeniedError(
                    code="MERCHANT_REGION_FORBIDDEN",
                    message="该商家不在你的负责片区内",
                    details={"merchant_id": str(merchant.id)},
                )
            return
        if actor.profile.row_scope == "self":
            owned = set(self._owned_merchant_ids(actor, _shanghai_now()))
            if merchant.id not in owned:
                raise PermissionDeniedError(
                    code="MERCHANT_NOT_ASSIGNED",
                    message="只能操作本人或临时授权的商家",
                    details={"merchant_id": str(merchant.id)},
                )
            return
        raise PermissionDeniedError(
            code="MERCHANT_SCOPE_FORBIDDEN",
            message="当前角色无权查看商家明细",
        )

    def assert_can_operate_merchant(
        self, actor: ActorContext, merchant: Merchant
    ) -> None:
        """写操作：业务员只能操作本人/临时授权商家（任务卡红线）。"""
        if actor.role_code == "SALES_REP":
            owned = set(self._owned_merchant_ids(actor, _shanghai_now()))
            if merchant.id not in owned:
                raise PermissionDeniedError(
                    code="MERCHANT_NOT_ASSIGNED",
                    message="业务员只能操作本人或临时授权的商家",
                    details={"merchant_id": str(merchant.id)},
                )
            return
        # 其余写角色走片区校验
        self._assert_can_read_merchant(actor, merchant)

    @staticmethod
    def _require_merchant_ability(actor: ActorContext) -> None:
        try:
            permission_service.require_ability(actor, MODULE_MERCHANT)
        except Exception as exc:  # PermissionError_ → 统一 403 域异常
            raise PermissionDeniedError(
                code="PERMISSION_DENIED",
                message="当前角色无权访问商家中心",
            ) from exc

    # ---------------- 列表 / URL 筛选 ----------------
    def list_merchants(
        self, actor: ActorContext, query: MerchantListQuery
    ) -> tuple[list[MerchantListItem], int, dict]:
        self._require_merchant_ability(actor)

        region_ids = self._scope_region_ids(actor)
        merchant_ids: list[UUID] | None = None
        if actor.role_code == "SALES_REP":
            merchant_ids = self._owned_merchant_ids(actor, _shanghai_now())

        rows, total = self.repo.search(
            region_ids=region_ids,
            merchant_ids=merchant_ids,
            status=query.status,
            grade=query.grade,
            keyword=query.keyword,
            order=query.order,
            region_id=query.region_id,
            page=query.page,
            page_size=query.page_size,
        )

        ids = [m.id for (m, _lat, _lng) in rows]
        active = {
            a.merchant_id: a
            for a in self.repo.active_assignments(ids, _shanghai_now())
            if a.assignment_type == "PRIMARY"
        }
        items = [
            self._serialize_list_item(actor, m, lat, lng, active.get(m.id))
            for (m, lat, lng) in rows
        ]
        applied = {
            "status": (query.status or "").upper() or None,
            "grade": (query.grade or "").upper() or None,
            "region_id": str(query.region_id) if query.region_id else None,
            "keyword": query.keyword,
            "order": query.order,
            "page": query.page,
            "page_size": query.page_size,
        }
        return items, total, applied

    def supplier_redacted_summary(self, actor: ActorContext) -> dict:
        """厂商（SUPPLIER）脱敏汇总（T23 `merchant.search`，PRD §5.3）。

        厂商不看商家明细：只返回其**授权片区**内的商家数量与片区数，不含任何
        可识别字段（名称/编号/联系方式/坐标）。厂商门户关闭时由其能力矩阵兜底。
        """
        if actor.role_code != RoleCode.SUPPLIER.value:
            raise PermissionDeniedError(
                code="MERCHANT_SCOPE_FORBIDDEN",
                message="脱敏汇总仅用于厂商角色",
            )
        regions = sorted(actor.supplier_region_ids or actor.region_ids)
        _, total = self.repo.search(region_ids=list(regions), page=1, page_size=1)
        return {
            "merchant_count": int(total),
            "region_count": len(regions),
            "masked": True,
        }

    def _serialize_list_item(
        self,
        actor: ActorContext,
        m: Merchant,
        lat: float | None,
        lng: float | None,
        primary: MerchantAssignment | None,
    ) -> MerchantListItem:
        contact, lat, lng, _, _ = self.redact_view_for_role(
            actor.role_code, m.contact_phone, lat, lng
        )
        return MerchantListItem(
            id=m.id,
            code=m.code,
            name=m.name,
            status=m.status,
            grade=m.grade,
            region_id=m.region_id,
            brand_id=m.brand_id,
            address=m.address,
            contact_phone=contact,
            primary_contact_name=m.primary_contact_name,
            latitude=lat,
            longitude=lng,
            current_owner_id=primary.owner_user_id if primary else None,
            created_at=m.created_at,
        )

    # ---------------- 建档 + 重复提示 ----------------
    def create_merchant(
        self,         actor: ActorContext, payload: MerchantCreateRequest
    ) -> tuple[Merchant, list[dict]]:
        self._require_merchant_ability(actor)

        # 编号全局唯一（生产/演示 dataset 内唯一，DB 亦有约束兜底）
        if self.repo.get_by_code(payload.code) is not None:
            raise ConflictError(
                code="MERCHANT_CODE_DUPLICATE",
                message="商家编号已存在，商家编号必须全局唯一",
                details={"code": payload.code},
            )

        # 行级：业务员不可在非授权片区建档；片区角色须在本片区
        if actor.profile.row_scope == "region":
            if payload.region_id not in actor.region_ids:
                raise PermissionDeniedError(
                    code="MERCHANT_REGION_FORBIDDEN",
                    message="不能在你负责片区之外建档",
                )

        location = None
        if payload.latitude is not None and payload.longitude is not None:
            location = self._make_point(payload.longitude, payload.latitude)

        merchant = Merchant(
            code=payload.code,
            name=payload.name,
            status=MerchantStatus.LEAD.value,
            region_id=payload.region_id,
            brand_id=payload.brand_id,
            address=payload.address,
            contact_phone=payload.contact_phone,
            primary_contact_name=payload.primary_contact_name,
            business_hours=payload.business_hours,
            location=location,
        )
        self.repo.add(merchant)

        # 建档即指派主负责人（生效区间，open-end 起自指定/现在）
        if payload.owner_user_id is not None:
            start = payload.assignment_start or _shanghai_now()
            assignment = MerchantAssignment(
                merchant_id=merchant.id,
                owner_user_id=payload.owner_user_id,
                assignment_type="PRIMARY",
                valid_range=self._range(start, None),
                reason="建档指派主负责人",
                # 数据域必须与商家一致：绩效归属（PerformanceRepository.primary_owner_at）
                # 按 dataset_id 命中区间，漏传会落成 PRODUCTION+随机 UUID，导致该商家的
                # 新签/巡访/销量绩效全部归不到人（PRD §8 关联链数据域一致）。
                dataset_type=merchant.dataset_type,
                dataset_id=merchant.dataset_id,
            )
            self.repo.add_assignment(assignment)

        warnings = self._find_duplicates(
            merchant, payload.name, payload.address
        )
        AuditService(self.session).record(
            object_type="MERCHANT",
            object_id=merchant.id,
            event_type=EVENT_CREATE,
            actor_id=actor.user_id,
            after={
                "code": merchant.code,
                "name": merchant.name,
                "region_id": str(merchant.region_id),
                "owner_user_id": (
                    str(payload.owner_user_id) if payload.owner_user_id else None
                ),
            },
            reason="商家建档",
        )
        return merchant, warnings

    def _find_duplicates(
        self, merchant: Merchant, name: str, address: str | None
    ) -> list[dict]:
        """同片区内店名或地址近似（非阻断提示，PRD §4.3）。"""
        warnings: list[dict] = []
        for cand in self.repo.same_region_candidates(
            merchant.region_id, exclude_id=merchant.id
        ):
            name_sim = _similar(name, cand.name)
            addr_sim = (
                _similar(address, cand.address)
                if address and cand.address
                else 0.0
            )
            if name_sim >= _SIMILAR_THRESHOLD or addr_sim >= _SIMILAR_THRESHOLD:
                warnings.append(
                    {
                        "merchant_id": str(cand.id),
                        "code": cand.code,
                        "name": cand.name,
                        "address": cand.address,
                        "name_similarity": round(name_sim, 3),
                        "address_similarity": round(addr_sim, 3),
                    }
                )
        return warnings

    # ---------------- 详情聚合 ----------------
    def get_merchant_detail(self, actor: ActorContext, merchant_id: UUID    ) -> dict:
        self._require_merchant_ability(actor)
        merchant = self.repo.get_by_id(merchant_id)
        if merchant is None:
            raise DomainError(
                code="MERCHANT_NOT_FOUND",
                message="商家不存在或已被删除",
                details={"merchant_id": str(merchant_id)},
            )
        self._assert_can_read_merchant(actor, merchant)

        agg = self.repo.aggregate_detail(merchant_id)
        assignments = self.repo.assignments_for_merchant(merchant_id)
        grade_history = self.repo.grade_changes_for_merchant(merchant_id)

        current_primary = next(
            (a for a in assignments if a.assignment_type == "PRIMARY"), None
        )
        lat, lng = self.repo.coords_of(merchant_id)

        contact, lat, lng, contact_masked, location_masked = self.redact_view_for_role(
            actor.role_code, merchant.contact_phone, lat, lng
        )

        return {
            "id": merchant.id,
            "code": merchant.code,
            "name": merchant.name,
            "status": merchant.status,
            "grade": merchant.grade,
            "region_id": merchant.region_id,
            "brand_id": merchant.brand_id,
            "address": merchant.address,
            "contact_phone": contact,
            "primary_contact_name": merchant.primary_contact_name,
            "business_hours": merchant.business_hours,
            "latitude": lat,
            "longitude": lng,
            "contact_masked": contact_masked,
            "location_masked": location_masked,
            "current_owner": (
                self._assignment_view(current_primary)
                if current_primary
                else None
            ),
            "assignments": [self._assignment_view(a) for a in assignments],
            "grade_history": [self._grade_view(g) for g in grade_history],
            "contracts": agg["contracts"],
            "freezers": agg["freezers"],
            "visits": agg["visit_count"],
            "replenishments": agg["replenishments"],
            "activities": agg["activities"],
            "issues": agg["issues"],
            "audits": [
                {
                    "event_type": a.event_type,
                    "object_type": a.object_type,
                    "reason": a.reason,
                    "occurred_at": (
                        a.occurred_at.isoformat() if a.occurred_at else None
                    ),
                }
                for a in agg["audits"]
            ],
        }

    # ---------------- 责任归属区间 ----------------
    def assign(
        self,
        actor: ActorContext,
        merchant_id: UUID,
        payload: AssignmentCreateRequest,
    ) -> MerchantAssignment:
        self._require_merchant_ability(actor)
        if actor.role_code not in _ASSIGNMENT_MANAGER_ROLES:
            raise PermissionDeniedError(
                code="MERCHANT_ASSIGN_FORBIDDEN",
                message="只有片区经理可调整商家责任归属",
            )
        merchant = self.repo.get_by_id(merchant_id)
        if merchant is None:
            raise DomainError(
                code="MERCHANT_NOT_FOUND",
                message="商家不存在",
                details={"merchant_id": str(merchant_id)},
            )
        self._assert_can_read_merchant(actor, merchant)

        if self.session.get(User, payload.owner_user_id) is None:
            raise DomainError(
                code="USER_NOT_FOUND",
                message="被指派人账号不存在",
                details={"user_id": str(payload.owner_user_id)},
            )
        if payload.end_at is not None and payload.end_at <= payload.start_at:
            raise DomainError(
                code="ASSIGNMENT_RANGE_INVALID",
                message="归属结束时间必须晚于开始时间",
            )

        # 主责任区间不得重叠（Q040）：Service 主动判定 + DB EXCLUDE 兜底
        if payload.assignment_type == "PRIMARY":
            overlap = self.repo.overlapping_primary(
                merchant_id, payload.start_at, payload.end_at
            )
            if overlap:
                raise ConflictError(
                    code="ASSIGNMENT_OVERLAP",
                    message="该商家在指定时段已存在主负责人，责任区间不得重叠",
                    details={
                        "merchant_id": str(merchant_id),
                        "conflict_assignment_ids": [str(a.id) for a in overlap],
                    },
                )

        assignment = MerchantAssignment(
            merchant_id=merchant_id,
            owner_user_id=payload.owner_user_id,
            assignment_type=payload.assignment_type,
            valid_range=self._range(payload.start_at, payload.end_at),
            reason=payload.reason,
            # 同建档：归属区间数据域与商家保持一致，否则绩效归不到人
            dataset_type=merchant.dataset_type,
            dataset_id=merchant.dataset_id,
        )
        self.repo.add_assignment(assignment)
        AuditService(self.session).record(
            object_type="MERCHANT_ASSIGNMENT",
            object_id=assignment.id,
            event_type=EVENT_CREATE,
            actor_id=actor.user_id,
            after={
                "merchant_id": str(merchant_id),
                "owner_user_id": str(payload.owner_user_id),
                "assignment_type": payload.assignment_type,
                "start_at": payload.start_at.isoformat(),
                "end_at": payload.end_at.isoformat() if payload.end_at else None,
            },
            reason=payload.reason or "责任归属调整",
        )
        return assignment

    # ---------------- 状态维护（仅负责业务员，PRD §4.3） ----------------
    def change_status(
        self,
        actor: ActorContext,
        merchant_id: UUID,
        payload: MerchantStatusChangeRequest,
    ) -> tuple[Merchant, str]:
        """变更商家状态；返回 (商家, 原状态)。

        红线：
        - 负责业务员只能改本人/临时授权商家（assert_can_operate_merchant）；
        - 片区经理可在本片区内兜底（业务员离职/账号停用时的唯一出口，按行级范围限本片区）；
        - 督导、财务、系统管理员、厂商一律只读；
        - 只能沿 PRD §4.3 状态机推进/相邻回退；TERMINATED 必须走取消合作审批链路；
        - 置为 ACTIVE（合作中）前必须有「已生效且未到期」的合同（PRD R2/§4.3:224 巡访口径）。
        """
        self._require_merchant_ability(actor)
        if actor.role_code not in _STATUS_WRITABLE_ROLES:
            raise PermissionDeniedError(
                code="MERCHANT_STATUS_FORBIDDEN",
                message="商家状态由负责业务员维护、片区经理兜底，其他角色只读",
            )
        merchant = self.repo.get_by_id(merchant_id)
        if merchant is None:
            raise DomainError(
                code="MERCHANT_NOT_FOUND",
                message="商家不存在",
                details={"merchant_id": str(merchant_id)},
            )
        if actor.role_code in _STATUS_MANAGER_ROLES:
            # 经理兜底：不限负责人，但受行级范围约束（越区 403）
            self._assert_can_read_merchant(actor, merchant)
        else:
            # 业务员：必须是当前主负责人/临时授权人，而非仅同片区可见
            self.assert_can_operate_merchant(actor, merchant)

        new_status = payload.new_status
        old_status = merchant.status
        if new_status == MerchantStatus.TERMINATED.value:
            raise DomainError(
                code="MERCHANT_TERMINATION_REQUIRES_APPROVAL",
                message="终止合作须发起「取消合作申请」并经经理审批，不能直接改状态",
                details={"merchant_id": str(merchant.id)},
            )
        if new_status == old_status:
            raise ConflictError(
                code="MERCHANT_STATUS_UNCHANGED",
                message="新状态与当前状态相同，无需变更",
                details={"status": old_status},
            )
        allowed = _SALES_REP_STATUS_TRANSITIONS.get(old_status, frozenset())
        if new_status not in allowed:
            raise DomainError(
                code="MERCHANT_STATUS_TRANSITION_INVALID",
                message=f"商家状态不能从 {old_status} 直接变更为 {new_status}",
                details={
                    "from": old_status,
                    "to": new_status,
                    "allowed": sorted(allowed),
                },
            )
        # ACTIVE（合作中）前置：必须有已生效且未到期的合同。
        # 依据 PRD §4.3:224「仅 ACTIVE 商家进入常规巡访覆盖率」与 R5「ACTIVE 商家默认每 7 天
        # 至少一次有效巡访」：无合同即置 ACTIVE 会把未签约商家计入应巡访分母并触发无效任务。
        if new_status == MerchantStatus.ACTIVE.value and not (
            self.contract_repo.has_effective_contract(merchant.id)
        ):
            raise DomainError(
                code="MERCHANT_ACTIVE_REQUIRES_EFFECTIVE_CONTRACT",
                message="置为「合作中」前需已有已生效且未到期的合同；请先提交合同申请并审批生效",
                details={"merchant_id": str(merchant.id), "from": old_status},
            )

        merchant.status = new_status
        self.session.flush()
        AuditService(self.session).record(
            object_type="MERCHANT",
            object_id=merchant.id,
            event_type=EVENT_UPDATE,
            actor_id=actor.user_id,
            before={"status": old_status},
            after={"status": new_status},
            reason=payload.reason,
        )
        return merchant, old_status

    # ---------------- 等级维护（仅片区经理） ----------------
    def change_grade(
        self,
        actor: ActorContext,
        merchant_id: UUID,
        payload: GradeChangeRequest,
    ) -> MerchantGradeChange:
        self._require_merchant_ability(actor)
        if actor.role_code not in _GRADE_MANAGER_ROLES:
            raise PermissionDeniedError(
                code="GRADE_CHANGE_FORBIDDEN",
                message="商家等级只能由片区经理维护",
            )
        merchant = self.repo.get_by_id(merchant_id)
        if merchant is None:
            raise DomainError(
                code="MERCHANT_NOT_FOUND",
                message="商家不存在",
                details={"merchant_id": str(merchant_id)},
            )
        self._assert_can_read_merchant(actor, merchant)

        if merchant.grade == payload.new_grade:
            raise ConflictError(
                code="GRADE_UNCHANGED",
                message="新等级与当前等级相同，无需调整",
                details={"grade": payload.new_grade},
            )
        if payload.effective_from < date.today():
            raise DomainError(
                code="GRADE_EFFECTIVE_PAST",
                message="等级生效日不得早于今天（历史不回溯，R35）",
            )

        change = MerchantGradeChange(
            merchant_id=merchant_id,
            old_grade=merchant.grade,
            new_grade=payload.new_grade,
            effective_from=payload.effective_from,
            reason=payload.reason,
        )
        self.repo.add_grade_change(change)

        # 仅当生效日=今天时更新当前快照；未来生效的由后续作业在生效日应用，
        # 历史覆盖率/绩效按发生时快照计算，绝不回溯（PRD §4.6/R35）。
        if payload.effective_from == date.today():
            merchant.grade = payload.new_grade
        AuditService(self.session).record(
            object_type="MERCHANT",
            object_id=merchant_id,
            event_type=EVENT_UPDATE,
            actor_id=actor.user_id,
            before={"grade": change.old_grade},
            after={
                "grade": change.new_grade,
                "effective_from": change.effective_from.isoformat(),
                "applied_now": payload.effective_from == date.today(),
            },
            reason=payload.reason,
        )
        return change

    # ---------------- 公共工具 ----------------
    @staticmethod
    def _range(start: datetime, end: datetime | None):
        from sqlalchemy import func

        return func.tstzrange(
            func.to_timestamp(start.timestamp()),
            func.to_timestamp(end.timestamp()) if end else None,
            "[)",
        )

    @staticmethod
    def _make_point(lng: float, lat: float):
        from geoalchemy2 import functions as gfunc
        from sqlalchemy import literal

        # ST_MakePoint 只接 x,y（第三参会被当作 Z）；SRID 用 ST_SetSRID 指定
        return gfunc.ST_SetSRID(gfunc.ST_MakePoint(literal(lng), literal(lat)), 4326)

    @staticmethod
    def can_view_contact(role_code: str) -> bool:
        return role_code in _CONTACT_VISIBLE_ROLES

    @staticmethod
    def can_view_precise_location(role_code: str) -> bool:
        # 厂商门户隐藏字段集（权限层 SENSITIVE_FIELDS_SUPPLIER）语义一致：
        # 厂商/财务不得见精确坐标
        return role_code in _SENSITIVE_COORD_ROLES

    @classmethod
    def redact_view_for_role(
        cls,
        role_code: str,
        phone: str | None,
        lat: float | None,
        lng: float | None,
    ) -> tuple[str | None, float | None, float | None, bool, bool]:
        """按角色裁剪联系人与精确坐标（PRD §4.3）。

        返回 (phone, lat, lng, contact_masked, location_masked)。
        可独立于模块鉴权测试：厂商/财务等经聚合入口看到商家时同样适用。
        """
        contact_masked = bool(phone) and not cls.can_view_contact(role_code)
        if contact_masked:
            phone = mask_phone(phone) if phone else phone

        location_masked = not cls.can_view_precise_location(role_code)
        if location_masked:
            lat, lng = cls._blur_coordinates(lat, lng)
        return phone, lat, lng, contact_masked, location_masked

    @staticmethod
    def _blur_coordinates(
        lat: float | None, lng: float | None
    ) -> tuple[float | None, float | None]:
        if lat is None or lng is None:
            return None, None
        # 截到约 11km 网格，避免精确坐标泄露给无权限角色
        blur = Decimal("0.1")
        return (
            float(Decimal(str(lat)).quantize(blur)),
            float(Decimal(str(lng)).quantize(blur)),
        )

    @staticmethod
    def _assignment_view(a: MerchantAssignment) -> AssignmentView:
        return AssignmentView(
            id=a.id,
            owner_user_id=a.owner_user_id,
            assignment_type=a.assignment_type,
            start_at=a.valid_range.lower,  # type: ignore[attr-defined]
            end_at=a.valid_range.upper,  # type: ignore[attr-defined]
            reason=a.reason,
        )

    @staticmethod
    def _grade_view(g: MerchantGradeChange) -> GradeChangeView:
        return GradeChangeView(
            id=g.id,
            old_grade=g.old_grade,
            new_grade=g.new_grade,
            effective_from=g.effective_from,
            reason=g.reason,
        )

"""督导跟访与整改闭环服务（PRD §4.8 / R14 / R15 / 验收 A10，T16）。

边界与红线：
- 督导跟访是**独立事实**：只写 SupervisionVisit / Rectification，绝不写业务员的
  VisitTask / VisitRecord，因此不替代、也不计入业务员有效巡访与覆盖率（R14）；
- 整改五态状态机 OPEN→IN_PROGRESS→SUBMITTED→VERIFIED→CLOSED，状态合法性只走
  state_machine（BusinessTransitionService）；
- **验证人不能是整改提交人**：SUBMITTED→VERIFIED 在 Service 强制 verifier_id !=
  submitter_id（DB CHECK 兜底）；
- 每周覆盖口径（R15/A10，唯一裁决处）：某督导在某业务周（上海周一为起点）内，
  跟访次数 ≥2 **且** 随访到的不同业务员 ≥2 名才算达标；“同一业务员两次”次数达标
  但覆盖人数仅 1，判不达标；周末工作台据此预警；
- 已审核事实不物理删除，只状态迁移并留原因；写操作与审计同事务；流转方法幂等。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import DomainError, PermissionDeniedError
from app.core.permissions import (
    MODULE_SUPERVISION,
    MODULE_VISIT,
    ActorContext,
    PermissionService,
)
from app.models import Merchant, Rectification, SupervisionVisit
from app.models.enums import (
    IssueCategory,
    RectificationStatus,
    RoleCode,
    SupervisionVisitStatus,
)
from app.repositories.supervision_repository import SupervisionRepository
from app.services.audit_service import AuditService
from app.services.business_transition_service import BusinessTransitionService
from app.services.state_machine import DOMAIN_RECTIFICATION
from app.services.task_service import TASK_KIND_SUPERVISION_RECTIFICATION, TaskService

# R15：每周最低跟访次数与最低覆盖业务员人数
MIN_WEEKLY_VISITS = 2
MIN_WEEKLY_DISTINCT_REPS = 2

_CATEGORIES = frozenset(c.value for c in IssueCategory)


@dataclass(frozen=True)
class WeeklyCoverage:
    """某督导在某业务周的跟访覆盖结果（周末工作台预警依据）。"""

    region_id: UUID
    supervisor_id: UUID
    week_start: date
    visit_count: int
    distinct_rep_count: int
    compliant: bool
    warnings: tuple[str, ...]

    @property
    def needs_warning(self) -> bool:
        return not self.compliant


class SupervisionService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = SupervisionRepository(session)
        self.permissions = PermissionService()
        self.audit = AuditService(session)
        self.transition = BusinessTransitionService(session)
        self.tasks = TaskService(session)

    # ============================ 跟访计划 ============================

    def plan_visit(
        self,
        actor: ActorContext,
        *,
        region_id: UUID,
        shadowed_user_id: UUID,
        merchant_id: UUID,
        planned_date: date,
    ) -> SupervisionVisit:
        """创建每周跟访计划（PLANNED）。仅督导/片区经理。"""
        self._require_supervision_ability(actor)
        region = self.repo.get_region(region_id)
        if region is None:
            raise DomainError(code="REGION_NOT_FOUND", message="片区不存在")
        self._assert_region(actor, region_id)

        self._merchant_in_region(merchant_id, region_id, region.dataset_id)
        rep = self.repo.get_active_user(shadowed_user_id, region.dataset_id)
        if rep is None or not self._is_sales_rep(rep.id):
            raise DomainError(
                code="SUPERVISION_SHADOW_REP_INVALID",
                message="随访业务员不存在/非在职/非业务员角色/数据范围不一致",
            )
        if not self._user_in_region(shadowed_user_id, region_id):
            raise PermissionDeniedError(
                code="REGION_FORBIDDEN",
                message="随访业务员不属于该片区",
            )
        # 督导本人不能是被随访业务员
        if shadowed_user_id == actor.user_id:
            raise DomainError(
                code="SUPERVISION_SELF_SHADOW",
                message="督导不能把自己作为被随访业务员",
            )

        visit = SupervisionVisit(
            region_id=region_id,
            status=SupervisionVisitStatus.PLANNED.value,
            supervisor_id=actor.user_id,
            shadowed_user_id=shadowed_user_id,
            merchant_id=merchant_id,
            planned_date=planned_date,
            dataset_type="PRODUCTION",
            dataset_id=region.dataset_id,
        )
        self.repo.add_visit(visit)
        self.audit.record(
            object_type="SUPERVISION_VISIT", object_id=visit.id,
            event_type="CREATE", actor_id=actor.user_id,
            after={"status": "PLANNED", "shadowed_user_id": str(shadowed_user_id),
                   "planned_date": planned_date.isoformat()},
            reason="创建督导跟访计划",
        )
        return visit

    # ============================ 现场记录提交 ============================

    def submit_visit(
        self,
        actor: ActorContext,
        *,
        visit_id: UUID,
        action_score: int,
        photo_count: int,
        visited_at: datetime,
        display_issue: str | None = None,
        service_issue: str | None = None,
        equipment_issue: str | None = None,
        note: str | None = None,
        issues: list[dict] | None = None,
    ) -> SupervisionVisit:
        """现场提交：评分/照片/陈列·服务·设备问题，并为问题生成整改单。

        幂等：已 SUBMITTED 的跟访直接返回当前态（不重复生成整改/待办）。
        """
        self._require_supervision_ability(actor)
        visit = self._visit_or_404(visit_id)
        self._assert_region(actor, visit.region_id)
        if visit.status == SupervisionVisitStatus.SUBMITTED.value:
            return visit  # 幂等
        if not 0 <= action_score <= 100:
            raise DomainError(
                code="SUPERVISION_SCORE_INVALID",
                message="业务动作评分必须在 0-100 之间",
            )
        if photo_count < 0:
            raise DomainError(
                code="SUPERVISION_PHOTO_INVALID",
                message="现场照片数不能为负",
            )

        visit.action_score = action_score
        visit.photo_count = photo_count
        visit.visited_at = visited_at
        visit.display_issue = display_issue
        visit.service_issue = service_issue
        visit.equipment_issue = equipment_issue
        visit.note = note
        visit.submitted_at = datetime.now(UTC)
        visit.status = SupervisionVisitStatus.SUBMITTED.value
        self.session.flush()

        # 生成整改单 + 责任人待办
        created: list[Rectification] = []
        for raw in issues or []:
            created.append(self._create_rectification(actor, visit, raw))

        self.audit.record_state_transition(
            object_type="SUPERVISION_VISIT", object_id=visit.id,
            actor_id=actor.user_id,
            before={"status": "PLANNED"},
            after={"status": "SUBMITTED", "action_score": action_score,
                   "rectifications": len(created)},
            reason="提交督导跟访现场记录",
        )
        return visit

    def submit_by_merchant_plan(
        self,
        actor: ActorContext,
        *,
        merchant_id: UUID,
        planned_date: date,
        findings: str = "",
        action_score: int = 80,
    ) -> SupervisionVisit:
        """MCP 受控入口：按商家+计划日定位本人 PLANNED 跟访并现场提交（T24）。

        - 仅定位发起人（督导）自己在该商家、该日的 PLANNED 跟访，模型不能指定
          他人的 visit_id 越权提交（行级范围仍由 submit_visit 再次校验）；
        - findings 写入现场说明 note；评分默认合格，可由结构化参数覆盖；
        - 问题整改单仍由 submit_visit 的既有规则生成，整改非同人校验不受影响。
        """
        visits = self.repo.find_planned_visits_for_merchant(
            merchant_id, planned_date, supervisor_id=actor.user_id
        )
        if not visits:
            raise DomainError(
                code="SUPERVISION_PLAN_NOT_FOUND",
                message="该商家在指定日期没有由你创建的待提交督导跟访计划",
                details={"merchant_id": str(merchant_id),
                         "planned_date": str(planned_date)},
            )
        return self.submit_visit(
            actor,
            visit_id=visits[0].id,
            action_score=action_score,
            photo_count=0,
            visited_at=datetime.now(UTC),
            note=findings or None,
        )

    def _create_rectification(
        self, actor: ActorContext, visit: SupervisionVisit, raw: dict
    ) -> Rectification:
        category = raw["category"]
        description = (raw.get("description") or "").strip()
        owner_user_id = raw["owner_user_id"]
        due_date = raw["due_date"]
        if category not in _CATEGORIES:
            raise DomainError(
                code="RECTIFICATION_CATEGORY_INVALID",
                message="整改问题分类必须是 DISPLAY/SERVICE/EQUIPMENT/OTHER",
            )
        if not description:
            raise DomainError(
                code="RECTIFICATION_DESCRIPTION_REQUIRED",
                message="整改问题描述不能为空",
            )
        if not isinstance(due_date, date):
            raise DomainError(
                code="RECTIFICATION_DUE_INVALID",
                message="整改截止日无效",
            )
        owner = self.repo.get_active_user(owner_user_id, visit.dataset_id)
        if owner is None:
            raise DomainError(
                code="RECTIFICATION_OWNER_INVALID",
                message="整改责任人不存在/非在职/数据范围不一致",
            )
        rect = Rectification(
            region_id=visit.region_id,
            supervision_visit_id=visit.id,
            merchant_id=visit.merchant_id,
            category=category,
            description=description,
            owner_user_id=owner_user_id,
            due_date=due_date,
            status=RectificationStatus.OPEN.value,
            dataset_type="PRODUCTION",
            dataset_id=visit.dataset_id,
        )
        self.repo.add_rectification(rect)
        self.audit.record(
            object_type="RECTIFICATION", object_id=rect.id,
            event_type="CREATE", actor_id=actor.user_id,
            after={"status": "OPEN", "category": category,
                   "owner_user_id": str(owner_user_id),
                   "merchant_id": str(visit.merchant_id)},
            reason="督导跟访发现问题，生成整改项",
        )
        # 责任人整改待办（幂等：同整改单未关闭不重复）
        try:
            self.tasks.assign(
                title=f"督导整改：{category}（截止 {due_date.isoformat()}）",
                assignee_id=owner_user_id,
                task_kind=TASK_KIND_SUPERVISION_RECTIFICATION,
                related_object_type="RECTIFICATION",
                related_object_id=rect.id,
                description=description,
            )
        except DomainError:
            pass  # 幂等：已有未关闭同类型待办
        return rect

    # ============================ 整改状态机 ============================

    def start_rectification(
        self, actor: ActorContext, *, rectification_id: UUID
    ) -> Rectification:
        """责任人开始整改：OPEN → IN_PROGRESS。"""
        rect = self._locked_rect_for_owner(actor, rectification_id)
        if rect.status == RectificationStatus.IN_PROGRESS.value:
            return rect  # 幂等
        self.transition.transition(
            entity=rect, domain=DOMAIN_RECTIFICATION,
            to_state=RectificationStatus.IN_PROGRESS.value,
            actor_id=actor.user_id, reason="责任人开始整改",
        )
        return rect

    def submit_rectification(
        self, actor: ActorContext, *, rectification_id: UUID, note: str
    ) -> Rectification:
        """责任人提交整改结果：IN_PROGRESS → SUBMITTED。记录提交人。"""
        if not note or not note.strip():
            raise DomainError(
                code="RECTIFICATION_SUBMIT_NOTE_REQUIRED",
                message="提交整改必须填写整改说明",
            )
        rect = self._locked_rect_for_owner(actor, rectification_id)
        if rect.status == RectificationStatus.SUBMITTED.value:
            return rect  # 幂等
        self.transition.transition(
            entity=rect, domain=DOMAIN_RECTIFICATION,
            to_state=RectificationStatus.SUBMITTED.value,
            actor_id=actor.user_id, reason="责任人提交整改结果",
            extra_after={"submitter_id": str(actor.user_id)},
        )
        rect.submitter_id = actor.user_id
        rect.submit_note = note.strip()
        rect.submitted_at = datetime.now(UTC)
        self.session.flush()
        return rect

    def verify_rectification(
        self,
        actor: ActorContext,
        *,
        rectification_id: UUID,
        passed: bool,
        note: str | None = None,
    ) -> Rectification:
        """督导/经理验证：SUBMITTED → VERIFIED（passed=True）。

        红线：**验证人不能是整改提交人**；passed=False 首版不做退回态（附录 A 未要求），
        保持 SUBMITTED 并留审计说明，由责任人补充后重新提交。
        """
        self._require_supervision_ability(actor)
        rect = self.repo.lock_rectification(rectification_id)
        if rect is None:
            raise DomainError(code="RECTIFICATION_NOT_FOUND", message="整改单不存在")
        self._assert_region(actor, rect.region_id)

        if not passed:
            self.audit.record(
                object_type="RECTIFICATION", object_id=rect.id,
                event_type="UPDATE", actor_id=actor.user_id,
                before={"status": rect.status},
                after={"status": rect.status, "verify_result": "NOT_PASSED"},
                reason=note or "验证未通过，责任人补充后重新提交",
            )
            return rect

        if rect.status == RectificationStatus.VERIFIED.value:
            return rect  # 幂等
        # 职责分离硬门禁
        if rect.submitter_id is not None and rect.submitter_id == actor.user_id:
            raise PermissionDeniedError(
                code="RECTIFICATION_VERIFIER_IS_SUBMITTER",
                message="验证人不能是整改提交人",
            )
        if rect.submitter_id is None:
            raise DomainError(
                code="RECTIFICATION_NOT_SUBMITTED",
                message="整改尚未提交，无法验证",
            )
        self.transition.transition(
            entity=rect, domain=DOMAIN_RECTIFICATION,
            to_state=RectificationStatus.VERIFIED.value,
            actor_id=actor.user_id, reason="非提交人验证整改通过",
            extra_after={"verifier_id": str(actor.user_id)},
        )
        rect.verifier_id = actor.user_id
        rect.verify_note = note
        rect.verified_at = datetime.now(UTC)
        self.session.flush()
        return rect

    def close_rectification(
        self, actor: ActorContext, *, rectification_id: UUID, note: str | None = None
    ) -> Rectification:
        """验证通过后关闭：VERIFIED → CLOSED（关闭人同样不得为提交人，沿用职责分离）。"""
        self._require_supervision_ability(actor)
        rect = self.repo.lock_rectification(rectification_id)
        if rect is None:
            raise DomainError(code="RECTIFICATION_NOT_FOUND", message="整改单不存在")
        self._assert_region(actor, rect.region_id)
        if rect.status == RectificationStatus.CLOSED.value:
            return rect  # 幂等
        if rect.submitter_id is not None and rect.submitter_id == actor.user_id:
            raise PermissionDeniedError(
                code="RECTIFICATION_CLOSER_IS_SUBMITTER",
                message="整改关闭人不能是整改提交人",
            )
        self.transition.transition(
            entity=rect, domain=DOMAIN_RECTIFICATION,
            to_state=RectificationStatus.CLOSED.value,
            actor_id=actor.user_id, reason=note or "验证通过，关闭整改",
        )
        rect.closed_at = datetime.now(UTC)
        self.session.flush()
        return rect

    # ============================ 每周覆盖（R15 唯一裁决） ============================

    def weekly_coverage(
        self,
        actor: ActorContext,
        *,
        region_id: UUID,
        week_start: date,
        supervisor_id: UUID | None = None,
    ) -> list[WeeklyCoverage]:
        """统计业务周（上海周一 00:00 起）内各督导的跟访次数与覆盖业务员数。

        只统计 SUBMITTED 跟访（计划未执行不计）。达标：次数 ≥2 且不同业务员 ≥2。
        督导可查本人；经理可查本片区。
        """
        self._require_supervision_ability(actor)
        self._assert_region(actor, region_id)
        monday = week_start - timedelta(days=week_start.weekday())
        week_end_exclusive = monday + timedelta(days=7)

        if self.repo.get_region(region_id) is None:
            raise DomainError(code="REGION_NOT_FOUND", message="片区不存在")
        visits = self.repo.list_visits_in_week(
            region_id=region_id,
            week_start=monday,
            week_end_exclusive=week_end_exclusive,
            supervisor_id=supervisor_id,
        )
        visits = [
            v for v in visits if v.status == SupervisionVisitStatus.SUBMITTED.value
        ]

        by_sup: dict[UUID, list[SupervisionVisit]] = {}
        for v in visits:
            by_sup.setdefault(v.supervisor_id, []).append(v)

        # 若某督导整周无记录，也需要预警；显式给定 supervisor 时补零条目
        target_supervisors = set(by_sup.keys())
        if supervisor_id is not None:
            target_supervisors.add(supervisor_id)

        results: list[WeeklyCoverage] = []
        for sup_id in sorted(target_supervisors, key=lambda x: str(x)):
            rows = by_sup.get(sup_id, [])
            distinct = {v.shadowed_user_id for v in rows}
            warnings: list[str] = []
            if len(rows) < MIN_WEEKLY_VISITS:
                warnings.append(
                    f"本周跟访 {len(rows)} 次，少于最低 {MIN_WEEKLY_VISITS} 次"
                )
            if len(distinct) < MIN_WEEKLY_DISTINCT_REPS:
                warnings.append(
                    f"仅覆盖 {len(distinct)} 名业务员，少于最低 "
                    f"{MIN_WEEKLY_DISTINCT_REPS} 名（同一业务员多次不满足覆盖两人）"
                )
            compliant = (
                len(rows) >= MIN_WEEKLY_VISITS
                and len(distinct) >= MIN_WEEKLY_DISTINCT_REPS
            )
            results.append(
                WeeklyCoverage(
                    region_id=region_id,
                    supervisor_id=sup_id,
                    week_start=monday,
                    visit_count=len(rows),
                    distinct_rep_count=len(distinct),
                    compliant=compliant,
                    warnings=tuple(warnings),
                )
            )
        return results

    # ============================ 只读 ============================

    def get_visit_detail(self, actor: ActorContext, *, visit_id: UUID) -> dict:
        self._require_supervision_ability(actor)
        visit = self._visit_or_404(visit_id)
        self._assert_region(actor, visit.region_id)
        return {
            "visit": visit,
            "rectifications": self.repo.list_rectifications_for_visit(visit_id),
        }

    def list_visits(
        self,
        actor: ActorContext,
        *,
        region_id: UUID | None = None,
        status: str | None = None,
        supervisor_id: UUID | None = None,
        shadowed_user_id: UUID | None = None,
        planned_from: date | None = None,
        planned_to: date | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict], int, dict]:
        """跟访计划/记录分页列表（督导/经理，含草稿态 PLANNED）。

        行级范围只来自身份上下文：非系统管理员强制限定在其授权片区；显式传
        `region_id` 且不在授权内直接 403（与 weekly-coverage 同口径）。
        """
        self._require_supervision_ability(actor)
        if status is not None and status not in {s.value for s in SupervisionVisitStatus}:
            raise DomainError(
                code="SUPERVISION_STATUS_UNKNOWN",
                message="跟访状态取值非法",
                details={"status": status},
            )
        scope = self._list_region_scope(actor, region_id)
        rows, total = self.repo.list_visits(
            region_ids=scope,
            status=status,
            supervisor_id=supervisor_id,
            shadowed_user_id=shadowed_user_id,
            planned_from=planned_from,
            planned_to=planned_to,
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        counts = self.repo.rectification_status_counts(v.id for v, *_ in rows)
        items = [
            {
                "visit": visit,
                "region_name": region.name,
                "supervisor_name": supervisor.display_name if supervisor else None,
                "shadowed_user_name": shadowed.display_name if shadowed else None,
                "merchant_name": merchant.name,
                "rectification_count": counts.get(visit.id, (0, 0))[0],
                "pending_rectification_count": counts.get(visit.id, (0, 0))[1],
            }
            for visit, region, supervisor, shadowed, merchant in rows
        ]
        applied = {
            "region_id": str(region_id) if region_id is not None else None,
            "status": status,
            "supervisor_id": str(supervisor_id) if supervisor_id is not None else None,
            "shadowed_user_id": (
                str(shadowed_user_id) if shadowed_user_id is not None else None
            ),
            "planned_from": planned_from.isoformat() if planned_from else None,
            "planned_to": planned_to.isoformat() if planned_to else None,
            "page": page,
            "page_size": page_size,
        }
        return items, total, applied

    def list_rectifications(
        self,
        actor: ActorContext,
        *,
        region_id: UUID | None = None,
        status: str | None = None,
        owner_user_id: UUID | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict], int, dict]:
        """整改单分页列表（督导/经理视角；业务员查自己的走 /my-rectifications）。

        逾期口径：已过截止日（业务时区）且未闭环（VERIFIED/CLOSED 不计）。
        """
        self._require_supervision_ability(actor)
        if status is not None and status not in {s.value for s in RectificationStatus}:
            raise DomainError(
                code="RECTIFICATION_STATUS_UNKNOWN",
                message="整改状态取值非法",
                details={"status": status},
            )
        scope = self._list_region_scope(actor, region_id)
        rows, total = self.repo.list_rectifications(
            region_ids=scope,
            status=status,
            owner_user_id=owner_user_id,
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        today = self._today()
        closed_states = {
            RectificationStatus.VERIFIED.value,
            RectificationStatus.CLOSED.value,
        }
        items = [
            {
                "rectification": rect,
                "region_name": region.name,
                "merchant_name": merchant.name,
                "owner_name": owner.display_name if owner else None,
                "overdue": rect.due_date < today and rect.status not in closed_states,
            }
            for rect, region, merchant, owner in rows
        ]
        applied = {
            "region_id": str(region_id) if region_id is not None else None,
            "status": status,
            "owner_user_id": (
                str(owner_user_id) if owner_user_id is not None else None
            ),
            "page": page,
            "page_size": page_size,
        }
        return items, total, applied

    def list_user_options(
        self, actor: ActorContext, *, region_id: UUID, role_code: str
    ) -> list:
        """跟访计划的人员下拉：该片区内指定角色（SALES_REP 被跟访人 / SUPERVISOR 督导）。

        只返回已授权该片区且在职业用户，避免选人后在建计划时才失败。
        """
        self._require_supervision_ability(actor)
        region = self.repo.get_region(region_id)
        if region is None:
            raise DomainError(code="REGION_NOT_FOUND", message="片区不存在")
        self._assert_region(actor, region_id)
        if role_code not in (RoleCode.SALES_REP.value, RoleCode.SUPERVISOR.value):
            raise DomainError(
                code="SUPERVISION_ROLE_UNSUPPORTED",
                message="人员下拉仅支持业务员/督导两类角色",
                details={"role_code": role_code},
            )
        return self.repo.list_region_users_by_role(
            region.dataset_id, region_id, role_code
        )

    def list_my_rectifications(
        self, actor: ActorContext, *, status: str | None = None
    ) -> list[Rectification]:
        """业务员查自己名下整改（走 MODULE_VISIT 能力，不要求督导能力）。"""
        self.permissions.require_ability(actor, MODULE_VISIT)
        stmt = select(Rectification).where(
            Rectification.owner_user_id == actor.user_id
        )
        if status is not None:
            stmt = stmt.where(Rectification.status == status)
        stmt = stmt.order_by(Rectification.due_date.asc())
        return list(self.session.scalars(stmt).all())

    # ============================ 内部 ============================

    def _locked_rect_for_owner(
        self, actor: ActorContext, rect_id: UUID
    ) -> Rectification:
        """业务员整改动作：需 MODULE_VISIT，且只能操作自己名下整改。"""
        self.permissions.require_ability(actor, MODULE_VISIT)
        rect = self.repo.lock_rectification(rect_id)
        if rect is None:
            raise DomainError(code="RECTIFICATION_NOT_FOUND", message="整改单不存在")
        if rect.owner_user_id != actor.user_id:
            raise PermissionDeniedError(
                code="RECTIFICATION_NOT_OWNER",
                message="只能处理自己名下的整改单",
            )
        return rect

    def _is_sales_rep(self, user_id: UUID) -> bool:
        from app.models import Role, User

        row = self.session.execute(
            select(Role.role_code)
            .join(User, User.role_id == Role.id)
            .where(User.id == user_id)
        ).first()
        return row is not None and row[0] == RoleCode.SALES_REP.value

    def _user_in_region(self, user_id: UUID, region_id: UUID) -> bool:
        from app.models import UserRegionBrandAuth

        return self.session.scalar(
            select(UserRegionBrandAuth.id)
            .where(UserRegionBrandAuth.user_id == user_id)
            .where(UserRegionBrandAuth.region_id == region_id)
            .limit(1)
        ) is not None


    def _merchant_in_region(
        self, merchant_id: UUID, region_id: UUID, dataset_id: UUID
    ) -> Merchant:
        merchant = self.repo.get_merchant(merchant_id)
        if (
            merchant is None
            or merchant.region_id != region_id
            or merchant.dataset_id != dataset_id
        ):
            raise DomainError(
                code="SUPERVISION_MERCHANT_INVALID",
                message="商家不存在或不属于该片区/数据范围",
            )
        return merchant

    def _visit_or_404(self, visit_id: UUID) -> SupervisionVisit:
        visit = self.repo.get_visit(visit_id)
        if visit is None:
            raise DomainError(code="SUPERVISION_VISIT_NOT_FOUND", message="跟访不存在")
        return visit

    def _require_supervision_ability(self, actor: ActorContext) -> None:
        """督导模块能力校验的统一出口（HTTP 层映射见 core.handlers）。

        督导路由未挂 router 级能力依赖（`/my-rectifications` 走 MODULE_VISIT，不能被
        MODULE_SUPERVISION 门禁整体挡掉），因此模块级校验只能在服务层逐入口声明。
        """
        self.permissions.require_ability(actor, MODULE_SUPERVISION)

    @staticmethod
    def _assert_region(actor: ActorContext, region_id: UUID) -> None:
        if actor.role_code == RoleCode.SYSTEM_ADMIN.value:
            return
        if region_id not in (actor.region_ids or set()):
            raise PermissionDeniedError(
                code="REGION_FORBIDDEN", message="无权操作该片区的督导跟访"
            )

    def _list_region_scope(
        self, actor: ActorContext, region_id: UUID | None
    ) -> set[UUID] | None:
        """列表用的片区范围（None=不限制）。

        督导模块能力只授予督导/片区经理（系统管理员也不含该模块），故这里不限制的
        分支实践中不可达，仍保留以便权限矩阵调整后行为一致。
        """
        if actor.role_code == RoleCode.SYSTEM_ADMIN.value and not actor.region_ids:
            return {region_id} if region_id is not None else None
        allowed = set(actor.region_ids or set())
        if region_id is not None:
            if region_id not in allowed:
                raise PermissionDeniedError(
                    code="REGION_FORBIDDEN", message="无权查看该片区的督导跟访数据"
                )
            return {region_id}
        return allowed

    @staticmethod
    def _today() -> date:
        """业务时区（Asia/Shanghai）的今天，用于逾期口径。"""
        from zoneinfo import ZoneInfo

        return datetime.now(UTC).astimezone(ZoneInfo("Asia/Shanghai")).date()

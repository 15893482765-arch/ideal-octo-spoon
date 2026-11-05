"""周排班服务：建议、周视图/地图列表、批量草稿、全有或全无发布（PRD §4.7）。

边界：
- 冲突唯一口径来自 ScheduleConflictService（本服务不自算重叠）；
- 风险只出自 RiskService（T21 落地）。T13 只预留风险优先级接缝（默认空），
  T21 注入后 RK-1 未巡访商家自动置建议列表首位（Q184 跨卡回归，PRD §4.11）；
- 建议≠自动发布：suggest 只返回排序建议，绝不写库；publish 才原子落任务+占用；
- 发布全有或全无：检测到任意一条冲突即整体拒绝、零写入；
- 并发：(region, 业务周) 事务级咨询锁串行化 + 已发布批次部分唯一索引 +
  ResourceReservation GiST 排他约束三重兜底，保证并发发布只成功一次。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import TSTZRANGE
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, DomainError, PermissionDeniedError
from app.core.permissions import MODULE_SCHEDULE, ActorContext, PermissionService
from app.models import (
    Merchant,
    Region,
    ResourceReservation,
    SchedulePlanBatch,
    SchedulePlanEntry,
    ScheduleTemplate,
    User,
    VisitTask,
)
from app.models.enums import ScheduleStatus, VisitSource, VisitTaskStatus
from app.repositories.schedule_repository import (
    ACTIVE_RESERVATION,
    SOURCE_OBJECT_BATCH,
    SOURCE_TYPE_SCHEDULE,
    ScheduleRepository,
)
from app.repositories.transfer_repository import OPEN_VISIT_STATUSES
from app.services.audit_service import (
    EVENT_CREATE,
    EVENT_UPDATE,
    AuditService,
)
from app.services.schedule_conflict_service import (
    Conflict,
    Occupancy,
    ScheduleConflictService,
)

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_TSTZRANGE = TSTZRANGE()


def tstzrange(start: datetime, end: datetime):
    """构造半开区间 [start,end) 的 Postgres tstzrange 值（由 DB 绑定参数处理）。"""
    return func.tstzrange(start, end, "[)", type_=_TSTZRANGE)

# 无等级频次配置时的兜底：7 天至少一次（PRD：ACTIVE 商家 7 天至少一次）
DEFAULT_FREQUENCY_DAYS = 7
# 默认单场巡访时长（占用区间），当调用方未给 end_at 时使用
DEFAULT_VISIT_DURATION = timedelta(minutes=30)

# 建议排序的等级权重（数值越小越优先）
_GRADE_RANK = {"A": 0, "B": 1, "C": 2}

# 风险优先级提供程序签名：(merchant_ids, dataset_id) -> 被标记 RK-1 的商家集合（有序）
RiskPriorityProvider = Callable[[list[UUID], UUID], list[UUID]]


def business_week_bounds(week_start: date) -> tuple[datetime, datetime]:
    """业务周半开区间 [周一 00:00, 下周一 00:00)（Asia/Shanghai），转 UTC 存储。"""
    if week_start.weekday() != 0:
        raise DomainError(
            code="SCHEDULE_WEEK_NOT_MONDAY",
            message="week_start 必须是 Asia/Shanghai 的周一",
        )
    start_local = datetime.combine(week_start, time(0, 0), tzinfo=SHANGHAI_TZ)
    end_local = start_local + timedelta(days=7)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


@dataclass
class Suggestion:
    merchant: Merchant
    assignee_id: UUID | None
    overdue_days: int
    grade: str | None
    has_pending_replenishment: bool
    distance_meters: float | None
    risk_pinned: bool = False
    # T25：主负责人在建议周被活动（CAMPAIGN）占用，该商家不能进「可编辑建议」，
    # 只能降级到 priority_list（优先级清单）并保留原因，绝不自动改派/发布。
    activity_blocked: bool = False
    reasons: list[str] = field(default_factory=list)

    def sort_key(self):
        # 风险钉死最前（RK-1 未巡访优先位，T21 提供）；其后逾期、等级、补货、距离
        return (
            0 if self.risk_pinned else 1,
            -self.overdue_days,
            _GRADE_RANK.get(self.grade or "", 3),
            0 if self.has_pending_replenishment else 1,
            self.distance_meters if self.distance_meters is not None else float("inf"),
            str(self.merchant.id),  # 稳定兜底
        )


@dataclass
class SuggestionPlan:
    """排班建议结果（T25 / PRD §5.3 降级矩阵）。

    - ``editable``：无活动占用的**可编辑建议**（仍不自动发布）；
    - ``priority_list``：因活动占用无法直接排入的商家，降级为优先级清单，
      只给优先级与原因，供人工改期/改派后再走受控发布（冲突只产草稿，不自动发布）；
    - 建议只读：本结果不产生任何批次/任务/占用。
    """

    editable: list[Suggestion] = field(default_factory=list)
    priority_list: list[Suggestion] = field(default_factory=list)
    degraded: bool = False
    degraded_reason: str | None = None

    def to_dict(self) -> dict:
        def _row(s: Suggestion) -> dict:
            return {
                "merchant_id": str(s.merchant.id),
                "merchant_name": s.merchant.name,
                "assignee_id": str(s.assignee_id) if s.assignee_id else None,
                "overdue_days": s.overdue_days,
                "grade": s.grade,
                "has_pending_replenishment": s.has_pending_replenishment,
                "distance_meters": s.distance_meters,
                "risk_pinned": s.risk_pinned,
                "activity_blocked": s.activity_blocked,
                "reasons": list(s.reasons),
            }

        return {
            "editable": [_row(s) for s in self.editable],
            "priority_list": [_row(s) for s in self.priority_list],
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
        }


@dataclass
class CoverageGap:
    total_active: int
    covered: int
    gap: int
    rate: Decimal
    missing_merchant_ids: list[UUID]


@dataclass(frozen=True)
class ProjectedEntry:
    """未发布周的模板预排条目（只读；``id`` 复用模板 id，不产生任务/占用）。"""

    id: UUID
    merchant_id: UUID
    assigned_user_id: UUID
    planned_date: date
    start_at: datetime
    end_at: datetime
    priority: int


class ScheduleService:
    def __init__(
        self,
        session: Session,
        risk_provider: RiskPriorityProvider | None = None,
    ) -> None:
        self.session = session
        self.repo = ScheduleRepository(session)
        self.permissions = PermissionService()
        self.conflicts_svc = ScheduleConflictService(session)
        self.audit = AuditService(session)
        # T21 RiskService 落地后注入；T13 默认不提供（不自行判定风险）
        self._risk_provider = risk_provider

    # ============================ 建议（只读，绝不自动发布）============================

    def suggest(
        self,
        actor: ActorContext,
        *,
        region_id: UUID,
        week_start: date,
        lng: float | None = None,
        lat: float | None = None,
        limit: int = 50,
    ) -> list[Suggestion]:
        """返回本周建议巡访商家（排序：风险钉首 → 逾期 → 等级 → 补货 → 距离）。

        仅返回建议，不写任何排班（红线：不把路线建议当自动发布）。
        兼容 T13：返回「可编辑建议 + 活动占用降级清单」的有序并集，均不发布。
        """
        plan = self.suggest_plan(
            actor, region_id=region_id, week_start=week_start, lng=lng, lat=lat,
            limit=limit,
        )
        return plan.editable + plan.priority_list

    def suggest_plan(
        self,
        actor: ActorContext,
        *,
        region_id: UUID,
        week_start: date,
        lng: float | None = None,
        lat: float | None = None,
        limit: int = 50,
    ) -> SuggestionPlan:
        """T25：返回可编辑建议与活动占用降级清单（PRD §5.3/§5.5）。

        - 主负责人本周被 CAMPAIGN 占用的商家进 ``priority_list``（只给优先级/原因，
          供人工改期改派后再发布），其余进 ``editable``；
        - 仅建议，不写任何批次/任务/占用（冲突只产草稿，不自动发布）。
        """
        self.permissions.require_ability(actor, MODULE_SCHEDULE)
        region = self._region_or_404(region_id)
        self._assert_region(actor, region_id)
        start_utc, end_utc = business_week_bounds(week_start)
        today = datetime.now(SHANGHAI_TZ).date()
        dataset_id = region.dataset_id

        merchants = [m for m in self.repo.list_active_merchants([region_id], dataset_id)
                     if m.region_id == region_id]
        merchant_ids = [m.id for m in merchants]

        assignees = self.repo.primary_assignees(merchant_ids)
        last_visit = self.repo.latest_valid_visit_at(merchant_ids)
        scheduled = self.repo.merchants_scheduled_in_week(
            region_id, week_start, dataset_id
        )
        visited_in_week = self.repo.merchants_with_valid_visit_in_week(
            region_id, start_utc, end_utc, dataset_id
        )
        pending_repl = self.repo.pending_replenishment_merchants(merchant_ids)
        distances = (
            self.repo.merchant_distances(merchants, lng, lat)
            if lng is not None and lat is not None else {}
        )

        # 活动避让：本周被 CAMPAIGN 占用的业务员（占用唯一来源 ResourceReservation）。
        assigned_user_ids = {u for u in assignees.values() if u is not None}
        campaign_users = self.repo.campaign_occupied_user_ids(
            list(assigned_user_ids), start_utc, end_utc, dataset_id
        )

        suggestions: list[Suggestion] = []
        for m in merchants:
            if m.id in scheduled or m.id in visited_in_week:
                continue  # 本周已排或已有效巡访，不再建议
            last_at = last_visit.get(m.id)
            frequency = self._frequency_days(m.grade)
            overdue = self._overdue_days(last_at, frequency, today)
            if overdue < 0 and last_at is not None:
                # 未到下次应巡日且历史上有巡访：不作为缺口建议（但从未巡访的仍要排）
                continue
            reasons = []
            if last_at is None:
                reasons.append("从未有效巡访")
            elif overdue > 0:
                reasons.append(f"逾期 {overdue} 天")
            if m.id in pending_repl:
                reasons.append("有待处理补货")
            assignee_id = assignees.get(m.id)
            blocked = assignee_id in campaign_users if assignee_id else False
            if blocked:
                # 冲突项不进可编辑建议，只在优先级清单标注活动占用（冲突只产草稿）
                reasons.insert(0, "主负责人本周被活动占用，需改期或改派")
            suggestions.append(
                Suggestion(
                    merchant=m,
                    assignee_id=assignee_id,
                    overdue_days=max(overdue, 0),
                    grade=m.grade,
                    has_pending_replenishment=m.id in pending_repl,
                    distance_meters=distances.get(m.id),
                    activity_blocked=blocked,
                    reasons=reasons,
                )
            )

        # RK-1 未巡访商家钉首（风险只出自 RiskService；T21 注入 provider 后生效）
        if self._risk_provider is not None:
            pinned = self._risk_provider(
                [s.merchant.id for s in suggestions], dataset_id
            )
            pin_order = {mid: i for i, mid in enumerate(pinned)}
            for s in suggestions:
                if s.merchant.id in pin_order:
                    s.risk_pinned = True
                    s.reasons.insert(0, "RK-1 未巡访风险")

        suggestions.sort(key=lambda s: s.sort_key())
        chosen = suggestions[:limit]
        editable = [s for s in chosen if not s.activity_blocked]
        blocked_list = [s for s in chosen if s.activity_blocked]
        plan = SuggestionPlan(
            editable=editable,
            priority_list=blocked_list,
            degraded=bool(blocked_list),
            degraded_reason=(
                "ACTIVITY_CONFLICT:部分商家主负责人被活动占用，已降级为优先级清单"
                if blocked_list else None
            ),
        )
        return plan

    # ============================ 周视图 / 地图列表 ============================

    def week_view(self, actor: ActorContext, *, week_start: date,
                  region_id: UUID | None = None, user_id: UUID | None = None):
        """周视图。

        行级范围：
        - 业务员只看到排给自己的条目（经理排给谁就只看谁），避免看到全片区排班；
        - 片区经理/系统管理员可看全部，也可用 ``user_id`` 按业务员筛选。
        """
        self.permissions.require_ability(actor, MODULE_SCHEDULE)
        business_week_bounds(week_start)  # 校验周一
        region_filter, dataset_id = self._week_scope(actor, region_id)
        assigned_user_id = (
            actor.user_id
            if actor.role_code == "SALES_REP"
            else user_id
        )
        return self.repo.week_view(
            region_filter, week_start, dataset_id, assigned_user_id=assigned_user_id
        )

    def week_has_published(
        self, actor: ActorContext, *, week_start: date, region_id: UUID | None = None
    ) -> bool:
        """该业务周在可见范围内是否已发布排班（决定能否展示模板预排）。"""
        self.permissions.require_ability(actor, MODULE_SCHEDULE)
        region_filter, dataset_id = self._week_scope(actor, region_id)
        if dataset_id is None:
            # 无片区上下文（系统管理员）时不按数据集过滤，只看是否有发布
            region_ids = set(region_filter) if region_filter else None
            return self.repo.has_published_week(region_ids, week_start, None)
        return self.repo.has_published_week(
            set(region_filter) if region_filter else None, week_start, dataset_id
        )

    def projected_week(
        self, actor: ActorContext, *, week_start: date,
        region_id: UUID | None = None, user_id: UUID | None = None,
    ) -> list[tuple[ProjectedEntry, Merchant, User]]:
        """未发布周的“模板预排”：把每周循环模板换算成本周日期后只读展示。

        下周依旧沿用同一套模板，除非经理继续添加/调整；本方法**不写库**，
        真正生成巡访任务仍走 ``templates/apply``（原子发布）。

        红线：模板预排不是排班——该周已有已发布排班时不得再展示预排，
        且已排入本周的商家要从预排里剔除，避免"看到有排班却没有巡访任务"。
        """
        self.permissions.require_ability(actor, MODULE_SCHEDULE)
        business_week_bounds(week_start)
        region_filter, dataset_id = self._week_scope(actor, region_id)
        if dataset_id is None:
            # 模板按 dataset 归档；没有片区上下文（系统管理员看全部）时不做模板预排。
            return []
        if self.repo.has_published_week(
            set(region_filter) if region_filter else None, week_start, dataset_id
        ):
            return []
        assigned_user_id = actor.user_id if actor.role_code == "SALES_REP" else user_id
        already_scheduled = self.repo.scheduled_merchant_ids(
            set(region_filter) if region_filter else None, week_start, dataset_id
        )

        rows = self.repo.list_templates_in_regions(region_filter, dataset_id)
        out: list[tuple[ProjectedEntry, Merchant, User]] = []
        for template, merchant, user in rows:
            if merchant.id in already_scheduled:
                continue
            if assigned_user_id is not None and user.id != assigned_user_id:
                continue
            planned = week_start + timedelta(days=template.weekday - 1)
            start_local = datetime.combine(planned, template.start_time, tzinfo=SHANGHAI_TZ)
            end_local = datetime.combine(planned, template.end_time, tzinfo=SHANGHAI_TZ)
            out.append((
                ProjectedEntry(
                    id=template.id,
                    merchant_id=merchant.id,
                    assigned_user_id=user.id,
                    planned_date=planned,
                    start_at=start_local.astimezone(UTC),
                    end_at=end_local.astimezone(UTC),
                    priority=template.priority,
                ),
                merchant,
                user,
            ))
        return out

    def map_list(self, actor: ActorContext, *, week_start: date,
                 region_id: UUID | None = None, user_id: UUID | None = None):
        """地图列表：周视图条目 + 商家坐标（供前端打点/排序）。"""
        return self.week_view(
            actor, week_start=week_start, region_id=region_id, user_id=user_id
        )

    def list_merchant_candidates(
        self,
        actor: ActorContext,
        *,
        region_id: UUID | None = None,
        week_start: date | None = None,
    ) -> list[tuple[Merchant, str, bool, UUID | None]]:
        """排班候选商家：可见片区内全部启用商家 + 本周是否已排。

        草稿页之前只查“当前选中片区”，且不区分已排/未排，导致看起来“只有 6 家”；
        这里一次返回所有可见片区的启用商家，并用 ``scheduled`` 标记本周已排的，
        新启用的商家下次查询即出现。
        """
        self.permissions.require_ability(actor, MODULE_SCHEDULE)
        if region_id is not None:
            self._assert_region(actor, region_id)
            region_filter: list[UUID] | None = [region_id]
            dataset_id = self._region_or_404(region_id).dataset_id
        else:
            region_filter, dataset_id = self._week_scope(actor, None)

        rows = self.repo.list_active_merchants_with_region(region_filter, dataset_id)
        scheduled = (
            self.repo.scheduled_merchant_ids(region_filter, week_start, dataset_id)
            if week_start is not None
            else set()
        )
        owners = self.repo.primary_owner_map({m.id for m, _n in rows})
        return [
            (merchant, region_name, merchant.id in scheduled, owners.get(merchant.id))
            for merchant, region_name in rows
        ]

    def list_assignee_options(
        self, actor: ActorContext, *, region_id: UUID | None = None
    ) -> list[tuple[User, list[str]]]:
        """排班可选的业务员：经理可见片区内的全部在职业务员。

        之前直接复用督导的“当前片区已授权用户”下拉，导致只出现该片区的 2 个人；
        排班的行级范围实际按 manager 的可见片区校验，所以这里放宽到可见片区内
        所有在职 SALES_REP，并回传其所属片区名，避免跨片区误排。
        """
        self._require_manage(actor)
        current: str | None = None
        if region_id is not None:
            region = self._region_or_404(region_id)
            self._assert_region(actor, region_id)
            scope = (
                None if actor.role_code == "SYSTEM_ADMIN"
                else set(actor.region_ids or {region_id})
            )
            dataset_id = region.dataset_id
            current = region.name
        else:
            _region_filter, dataset_id = self._week_scope(actor, None)
            scope = None if actor.role_code == "SYSTEM_ADMIN" else set(actor.region_ids or ())
        rows = self.repo.list_sales_reps(dataset_id, scope)

        merged: dict[UUID, tuple[User, list[str]]] = {}
        for user, region_name in rows:
            _user_obj, names = merged.setdefault(user.id, (user, []))
            if region_name and region_name not in names:
                names.append(region_name)
        return sorted(
            merged.values(),
            key=lambda item: (
                0 if current and current in item[1] else 1,
                item[0].display_name or "",
            ),
        )

    def _week_scope(
        self, actor: ActorContext, region_id: UUID | None
    ) -> tuple[list[UUID] | None, UUID | None]:
        if region_id is not None:
            self._assert_region(actor, region_id)
            return [region_id], self._region_or_404(region_id).dataset_id
        readable = self._readable_region_ids(actor)
        # 管理员（不限片区）：region=None、dataset=None；无授权者给不可能命中的 id
        region_filter = (
            None if not readable and actor.role_code == "SYSTEM_ADMIN" else readable
        )
        dataset_id = None
        if region_filter:
            first = self.repo.get_region(region_filter[0])
            dataset_id = first.dataset_id if first is not None else None
        return region_filter, dataset_id

    def coverage_gap(
        self, actor: ActorContext, *, region_id: UUID | None, week_start: date
    ) -> CoverageGap:
        """周覆盖缺口：ACTIVE 商家中本周未获有效巡访/未排的数量（PRD §3 覆盖率）。

        ``region_id`` 为空时按经理可见片区汇总（周视图默认“全部可见片区”）。
        """
        self.permissions.require_ability(actor, MODULE_SCHEDULE)
        if region_id is None:
            if actor.role_code == "SYSTEM_ADMIN" and not actor.region_ids:
                region_ids = list(self.session.scalars(select(Region.id)).all())
            else:
                region_ids = list(actor.region_ids or ())
            total_active = covered = 0
            missing: list[UUID] = []
            for rid in region_ids:
                part = self.coverage_gap(actor, region_id=rid, week_start=week_start)
                total_active += part.total_active
                covered += part.covered
                missing.extend(part.missing_merchant_ids)
            rate = (
                (Decimal(covered) / Decimal(total_active)).quantize(Decimal("0.0001"))
                if total_active else Decimal("1")
            )
            return CoverageGap(
                total_active=total_active,
                covered=covered,
                gap=len(missing),
                rate=rate,
                missing_merchant_ids=missing,
            )

        region = self._region_or_404(region_id)
        self._assert_region(actor, region_id)
        start_utc, end_utc = business_week_bounds(week_start)
        dataset_id = region.dataset_id
        merchants = [
            m for m in self.repo.list_active_merchants([region_id], dataset_id)
            if m.region_id == region_id
        ]
        visited = self.repo.merchants_with_valid_visit_in_week(
            region_id, start_utc, end_utc, dataset_id
        )
        scheduled = self.repo.merchants_scheduled_in_week(
            region_id, week_start, dataset_id
        )
        covered = {m.id for m in merchants if m.id in visited or m.id in scheduled}
        missing = [m.id for m in merchants if m.id not in covered]
        total = len(merchants)
        rate = (
            (Decimal(len(covered)) / Decimal(total)).quantize(Decimal("0.0001"))
            if total else Decimal("1")
        )
        return CoverageGap(
            total_active=total,
            covered=len(covered),
            gap=len(missing),
            rate=rate,
            missing_merchant_ids=missing,
        )

    # ============================ 批量草稿 ============================

    def create_draft(
        self,
        actor: ActorContext,
        *,
        region_id: UUID,
        week_start: date,
        entries: list[dict],
        idempotency_key: str | None = None,
    ) -> SchedulePlanBatch:
        """创建排班草稿批次（不生成任务/占用，允许存在冲突，发布时才裁决）。"""
        self._require_manage(actor)
        region = self._region_or_404(region_id)
        self._assert_region(actor, region_id)
        start_utc, end_utc = business_week_bounds(week_start)
        if not entries:
            raise DomainError(code="SCHEDULE_EMPTY", message="排班条目不能为空")

        dataset_id = region.dataset_id
        self._assert_entries_in_scope(entries, region_id, dataset_id)
        batch = SchedulePlanBatch(
            week_start_date=week_start,
            week_range=tstzrange(start_utc, end_utc),
            region_id=region_id,
            status=ScheduleStatus.DRAFT.value,
            idempotency_key=idempotency_key,
            dataset_type="PRODUCTION",
            dataset_id=dataset_id,
        )
        self.repo.add_batch(batch)

        seen_merchants: set[UUID] = set()
        for e in entries:
            if e["merchant_id"] in seen_merchants:
                raise DomainError(
                    code="SCHEDULE_DUPLICATE_MERCHANT_IN_BATCH",
                    message="同一批次中商家只能出现一次",
                )
            seen_merchants.add(e["merchant_id"])
            planned_date = e["planned_date"]
            if not (week_start <= planned_date < week_start + timedelta(days=7)):
                raise DomainError(
                    code="SCHEDULE_DATE_OUT_OF_WEEK",
                    message="计划日期必须落在该业务周内",
                )
            start_at, end_at = self._normalize_window(e, planned_date)
            self.session.add(
                SchedulePlanEntry(
                    batch_id=batch.id,
                    merchant_id=e["merchant_id"],
                    assigned_user_id=e["assigned_user_id"],
                    planned_date=planned_date,
                    start_at=start_at,
                    end_at=end_at,
                    priority=e.get("priority", 2),
                    dataset_type="PRODUCTION",
                    dataset_id=dataset_id,
                )
            )
        self.session.flush()
        self.audit.record(
            object_type="SCHEDULE_PLAN_BATCH", object_id=batch.id,
            event_type=EVENT_CREATE, actor_id=actor.user_id,
            after={"status": batch.status, "entry_count": len(entries)},
        )
        return batch

    # ============================ 全有或全无发布 ============================

    def publish(
        self, actor: ActorContext, *, batch_id: UUID
    ) -> tuple[SchedulePlanBatch, list[Conflict]]:
        """发布批次：有任意冲突则整体拒绝零写入；成功则原子生成任务+占用。

        返回 (batch, [])；冲突时抛 ScheduleConflictError（事务由 Router 回滚）。
        """
        self._require_manage(actor)
        batch = self.repo.get_batch(batch_id)
        if batch is None:
            raise DomainError(code="SCHEDULE_BATCH_NOT_FOUND", message="排班批次不存在")
        region = self.repo.get_region(batch.region_id)
        if region is None or batch.dataset_id != region.dataset_id:
            raise DomainError(code="SCHEDULE_BATCH_SCOPE_BROKEN",
                             message="排班批次与片区数据范围不一致")
        self._assert_region(actor, batch.region_id)

        # 并发第一重：(region, 周) 咨询锁串行化并发发布
        self.repo.acquire_region_week_lock(batch.region_id, batch.week_start_date)

        # 幂等：本批已发布则直接返回（重复点击/重试不重复生成）
        if batch.status == "PUBLISHED":
            return batch, []

        # 第二重：锁定本批条目（防止发布中途被改）
        entries = self.repo.lock_entries(batch.id)
        if not entries:
            raise DomainError(code="SCHEDULE_EMPTY", message="排班批次为空，无法发布")

        # 冲突唯一裁决（优先于同周唯一检查）：候选 vs 既有 ACTIVE 占用 + 批内彼此。
        # 只给到“天”的条目在发布时按业务员+日期自动分时段（09:00 起每 30 分钟一格），
        # 因此同一天排多家不同商家不会被误判为时间冲突。
        start_utc, end_utc = business_week_bounds(batch.week_start_date)
        candidates = self._plan_occupancies(
            entries, dataset_id=batch.dataset_id,
            week_start=start_utc, week_end=end_utc,
        )
        conflicts = self.conflicts_svc.detect(
            candidates, dataset_id=batch.dataset_id,
            window_start=start_utc, window_end=end_utc,
        )
        if conflicts:
            # 红线：一条冲突即整批拒绝，零写入（外层 Router 回滚；此处不落任何任务/占用）
            raise ScheduleConflictError(conflicts)

        # 冲突通过后，再判同周是否已被别的批次发布（并发兜底，咨询锁内读到权威状态）
        existing = self.repo.find_published_batch(
            batch.region_id, batch.week_start_date, batch.dataset_id
        )
        if existing is not None and existing.id != batch.id:
            raise ConflictError(
                code="SCHEDULE_WEEK_ALREADY_PUBLISHED",
                message="该片区本周已存在已发布排班",
            )

        # 无冲突：同事务原子生成 VisitTask + ResourceReservation + 回链
        now = datetime.now(UTC)
        for entry, occ in zip(entries, candidates, strict=True):
            task = VisitTask(
                merchant_id=entry.merchant_id,
                assigned_user_id=entry.assigned_user_id,
                planned_date=entry.planned_date,
                window_start_at=entry.start_at,
                window_end_at=entry.end_at,
                priority=entry.priority,
                source=VisitSource.WEEKLY_SCHEDULE.value,
                status=VisitTaskStatus.PENDING.value,
                dataset_type="PRODUCTION",
                dataset_id=batch.dataset_id,
            )
            self.repo.add_visit_task(task)

            reservation = ResourceReservation(
                resource_user_id=entry.assigned_user_id,
                start_at=occ.start_at,
                end_at=occ.end_at,
                occupied_range=tstzrange(occ.start_at, occ.end_at),
                source_type=SOURCE_TYPE_SCHEDULE,
                status=ACTIVE_RESERVATION,
                merchant_id=entry.merchant_id,
                source_object_type=SOURCE_OBJECT_BATCH,
                source_object_id=batch.id,
                dataset_type="PRODUCTION",
                dataset_id=batch.dataset_id,
            )
            self.repo.add_reservation(reservation)

            entry.visit_task_id = task.id
            entry.reservation_id = reservation.id

        batch.status = ScheduleStatus.PUBLISHED.value
        batch.published_at = now
        batch.published_by_id = actor.user_id

        # 每周循环：把本次排班沉淀为模板，下周自动沿用同一套排班（除非经理继续添加/调整）。
        # 已存在模板的商家不覆盖，避免一次临时排期改动影响长期循环。
        new_templates = self._upsert_templates_from_entries(entries, region)
        self.session.flush()
        if new_templates:
            self.audit.record(
                object_type="SCHEDULE_TEMPLATE",
                object_id=new_templates[0].id,
                event_type=EVENT_CREATE,
                actor_id=actor.user_id,
                after={
                    "region_id": str(region.id),
                    "count": len(new_templates),
                    "reason": "发布周排班时自动沉淀为每周循环模板",
                },
            )

        self.audit.record_state_transition(
            object_type="SCHEDULE_PLAN_BATCH", object_id=batch.id,
            actor_id=actor.user_id,
            before={"status": "DRAFT"},
            after={"status": "PUBLISHED", "entries": len(entries)},
            reason="经理发布周排班",
        )
        return batch, []

    def append_entries(
        self,
        actor: ActorContext,
        *,
        batch_id: UUID,
        entries: list[dict],
    ) -> tuple[SchedulePlanBatch, list[SchedulePlanEntry]]:
        """已发布周增量追加：在保留既有条目/任务/占用的前提下再排几家商家。

        - 只允许追加到 PUBLISHED 批次（草稿请直接用原批次编辑）；
        - 已有商家不能重复追加（同周同一商家只排一次）；
        - 与发布同一套口径：并发锁、自动分时段、冲突整体拒绝（零写入）；
        - 追加成功后同步沉淀每周模板，下周自动带上新增商家。
        """
        self._require_manage(actor)
        batch = self.repo.get_batch(batch_id)
        if batch is None:
            raise DomainError(code="SCHEDULE_BATCH_NOT_FOUND", message="排班批次不存在")
        region = self.repo.get_region(batch.region_id)
        if region is None or batch.dataset_id != region.dataset_id:
            raise DomainError(
                code="SCHEDULE_BATCH_SCOPE_BROKEN",
                message="排班批次与片区数据范围不一致",
            )
        if batch.status != ScheduleStatus.PUBLISHED.value:
            raise DomainError(
                code="SCHEDULE_BATCH_NOT_PUBLISHED",
                message="该批次还未发布：请直接在草稿里补充后一次发布",
            )
        self._assert_region(actor, batch.region_id)
        if not entries:
            raise DomainError(code="SCHEDULE_EMPTY", message="追加条目不能为空")

        self.repo.acquire_region_week_lock(batch.region_id, batch.week_start_date)

        week_start = batch.week_start_date
        existing_entries = self.repo.lock_entries(batch.id)
        existing_merchants = {e.merchant_id for e in existing_entries}
        payload_merchants: set[UUID] = set()
        for e in entries:
            if e["merchant_id"] in payload_merchants:
                raise DomainError(
                    code="SCHEDULE_DUPLICATE_MERCHANT_IN_BATCH",
                    message="同一批次中商家只能出现一次",
                )
            payload_merchants.add(e["merchant_id"])
            if e["merchant_id"] in existing_merchants:
                raise ConflictError(
                    code="SCHEDULE_MERCHANT_ALREADY_SCHEDULED",
                    message="该商家本周已排班，不能重复追加",
                    details={"merchant_id": str(e["merchant_id"])},
                )
            planned_date = e["planned_date"]
            if not (week_start <= planned_date < week_start + timedelta(days=7)):
                raise DomainError(
                    code="SCHEDULE_DATE_OUT_OF_WEEK",
                    message="计划日期必须落在该业务周内",
                )

        self._assert_entries_in_scope(entries, batch.region_id, batch.dataset_id)

        created: list[SchedulePlanEntry] = []
        for e in entries:
            planned_date = e["planned_date"]
            start_at, end_at = self._normalize_window(e, planned_date)
            entry = SchedulePlanEntry(
                batch_id=batch.id,
                merchant_id=e["merchant_id"],
                assigned_user_id=e["assigned_user_id"],
                planned_date=planned_date,
                start_at=start_at,
                end_at=end_at,
                priority=e.get("priority", 2),
                dataset_type="PRODUCTION",
                dataset_id=batch.dataset_id,
            )
            self.session.add(entry)
            created.append(entry)
        self.session.flush()

        start_utc, end_utc = business_week_bounds(week_start)
        candidates = self._plan_occupancies(
            created, dataset_id=batch.dataset_id,
            week_start=start_utc, week_end=end_utc,
        )
        conflicts = self.conflicts_svc.detect(
            candidates, dataset_id=batch.dataset_id,
            window_start=start_utc, window_end=end_utc,
        )
        if conflicts:
            # 与发布同一条红线：有冲突则这次追加零写入（外层事务整体回滚）
            raise ScheduleConflictError(conflicts)

        for entry, occ in zip(created, candidates, strict=True):
            task = VisitTask(
                merchant_id=entry.merchant_id,
                assigned_user_id=entry.assigned_user_id,
                planned_date=entry.planned_date,
                window_start_at=occ.start_at,
                window_end_at=occ.end_at,
                priority=entry.priority,
                source=VisitSource.WEEKLY_SCHEDULE.value,
                status=VisitTaskStatus.PENDING.value,
                dataset_type="PRODUCTION",
                dataset_id=batch.dataset_id,
            )
            self.repo.add_visit_task(task)

            reservation = ResourceReservation(
                resource_user_id=entry.assigned_user_id,
                start_at=occ.start_at,
                end_at=occ.end_at,
                occupied_range=tstzrange(occ.start_at, occ.end_at),
                source_type=SOURCE_TYPE_SCHEDULE,
                status=ACTIVE_RESERVATION,
                merchant_id=entry.merchant_id,
                source_object_type=SOURCE_OBJECT_BATCH,
                source_object_id=batch.id,
                dataset_type="PRODUCTION",
                dataset_id=batch.dataset_id,
            )
            self.repo.add_reservation(reservation)

            entry.visit_task_id = task.id
            entry.reservation_id = reservation.id

        self._upsert_templates_from_entries(created, region)
        self.session.flush()

        self.audit.record(
            object_type="SCHEDULE_PLAN_BATCH",
            object_id=batch.id,
            event_type=EVENT_UPDATE,
            actor_id=actor.user_id,
            after={
                "status": batch.status,
                "appended_entry_count": len(created),
                "total_entry_count": len(existing_entries) + len(created),
            },
            reason="已发布周增量追加商家",
        )
        return batch, created

    def reset_week_batch(self, actor: ActorContext, *, batch_id: UUID) -> None:
        """撤销整周已发布排班（条目 + 巡访任务 + 资源占用），供「按模板重排本周」。

        安全边界：只允许清理**尚未开始执行**的周——批内任一巡访任务已进入
        IN_PROGRESS/COMPLETED/ABNORMAL 时整体拒绝，避免把现场已跑的排班抹掉。
        """
        self._require_manage(actor)
        batch = self.repo.get_batch(batch_id)
        if batch is None:
            raise DomainError(code="SCHEDULE_BATCH_NOT_FOUND", message="排班批次不存在")
        if batch.status != ScheduleStatus.PUBLISHED.value:
            raise DomainError(
                code="SCHEDULE_BATCH_NOT_PUBLISHED",
                message="该批次还未发布，无需撤销",
            )
        self._assert_region(actor, batch.region_id)
        self.repo.acquire_region_week_lock(batch.region_id, batch.week_start_date)

        entries = self.repo.lock_entries(batch.id)
        task_ids = [e.visit_task_id for e in entries if e.visit_task_id is not None]
        tasks = (
            list(self.session.scalars(select(VisitTask).where(VisitTask.id.in_(task_ids))).all())
            if task_ids
            else []
        )
        locked = [
            t for t in tasks
            if t.status not in (VisitTaskStatus.PENDING.value, VisitTaskStatus.INVALIDATED.value)
        ]
        if locked:
            raise ConflictError(
                code="SCHEDULE_WEEK_LOCKED",
                message=(
                    f"本周已有 {len(locked)} 个巡访任务开始执行，不能按模板重排；"
                    "请改用「继续追加」或逐条改期"
                ),
                details={"locked_tasks": len(locked)},
            )

        before = {
            "status": ScheduleStatus.PUBLISHED.value,
            "entry_count": len(entries),
            "week_start": batch.week_start_date.isoformat(),
            "task_count": len(tasks),
        }
        self.session.execute(
            delete(ResourceReservation).where(
                ResourceReservation.source_object_type == SOURCE_OBJECT_BATCH,
                ResourceReservation.source_object_id == batch.id,
            )
        )
        if tasks:
            self.session.execute(delete(VisitTask).where(VisitTask.id.in_(task_ids)))
        self.session.execute(
            delete(SchedulePlanEntry).where(SchedulePlanEntry.batch_id == batch.id)
        )
        self.session.delete(batch)
        self.session.flush()
        self.audit.record(
            object_type="SCHEDULE_PLAN_BATCH",
            object_id=batch_id,
            event_type=EVENT_UPDATE,
            actor_id=actor.user_id,
            before=before,
            reason="按每周模板重排：撤销该周已发布排班",
        )

    def publish_week_draft(
        self,
        actor: ActorContext,
        *,
        week_start: date,
        entry_ids: list[UUID] | None = None,
    ) -> tuple[SchedulePlanBatch, list]:
        """MCP 受控入口：按业务周定位发布人范围内的 DRAFT 批次并发布（T24）。

        - 不接受模型指定 region（行级范围由 actor 注入）；只在发布人有权管理的
          片区草稿批次中定位；
        - 可选 entry_ids 仅用于核对批内条目（防止发布错批），不改变冲突裁决；
        - 冲突/状态机/同周唯一仍由 publish() 唯一裁决（全有或全无）。
        """
        self._require_manage(actor)
        scope_regions = set(actor.region_ids)
        from sqlalchemy import select

        rows = list(self.session.scalars(
            select(SchedulePlanBatch)
            .where(
                SchedulePlanBatch.week_start_date == week_start,
                SchedulePlanBatch.status == ScheduleStatus.DRAFT.value,
            )
            .order_by(SchedulePlanBatch.created_at.desc())
        ).all())
        batch = next(
            (
                b
                for b in rows
                if not scope_regions or b.region_id in scope_regions
            ),
            None,
        )
        if batch is None:
            raise DomainError(
                code="SCHEDULE_WEEK_DRAFT_NOT_FOUND",
                message="该业务周在你可管理的片区内没有可发布的排班草稿",
                details={"week_start": str(week_start)},
            )
        if entry_ids:
            existing = {e.id for e in self.repo.list_entries(batch.id)}
            missing = [str(x) for x in entry_ids if x not in existing]
            if missing:
                raise DomainError(
                    code="SCHEDULE_ENTRY_NOT_IN_BATCH",
                    message="指定条目不属于该周草稿批次，请重新核对",
                    details={"missing": missing},
                )
        return self.publish(actor, batch_id=batch.id)

    # ============================ 调拨改派 ============================

    def reassign_merchant_schedule(
        self,
        *,
        merchant_id: UUID,
        from_user_id: UUID,
        to_user_id: UUID,
        effective_at: datetime,
        actor_id: UUID | None = None,
    ) -> dict:
        """商家调拨生效时把今后的排班一并改派给新负责人。

        背景（缺陷）：调拨只转移 VisitTask，不改 SchedulePlanEntry/占用/模板，
        于是原负责人在周视图仍"有排班"，巡访计划里却已归新负责人——
        表现为"业务员当日有排班但巡访计划看不到"。

        口径：
        - 只处理生效日（含）之后、任务尚未开始的条目；已执行任务保持原负责人；
        - 保留原时段；若新负责人该时段已被占用，则顺延到当天第一个空档；
          顺延失败（当天排满）时跳过该条并回报，不阻断调拨本身；
        - 同步改派每周模板，避免下周又排回原负责人。
        """
        effective_day = effective_at.astimezone(SHANGHAI_TZ).date()
        entries = self.repo.entries_for_merchant_after(
            merchant_id, from_user_id, effective_day
        )
        moved = 0
        moved_reservations = 0
        skipped: list[dict] = []

        for entry in entries:
            task = (
                self.repo.get_visit_task(entry.visit_task_id)
                if entry.visit_task_id
                else None
            )
            # 与 TransferService 的“未完成任务”口径保持一致：任务转给谁，排班就改派给谁，
            # 否则又会出现"排班归 A、任务归 B"。已完成/已失效任务保持原负责人。
            if task is not None and task.status not in OPEN_VISIT_STATUSES:
                skipped.append(
                    {"entry_id": str(entry.id), "reason": "TASK_FINISHED"}
                )
                continue

            start_at, end_at = entry.start_at, entry.end_at
            if start_at is None or end_at is None:
                start_at, end_at = self._default_window(entry.planned_date)

            busy = self._busy_for_reassign(to_user_id, entry, exclude=entry.reservation_id)
            if self._overlaps_any(start_at, end_at, busy.get(
                (to_user_id, entry.planned_date), []
            )):
                try:
                    start_at, end_at = self._next_free_slot(
                        to_user_id, entry.planned_date, busy
                    )
                except DomainError:
                    skipped.append(
                        {"entry_id": str(entry.id), "reason": "DAY_FULL"}
                    )
                    continue
            self._remember_busy(busy, to_user_id, start_at, end_at)

            entry.assigned_user_id = to_user_id
            entry.start_at, entry.end_at = start_at, end_at
            moved += 1

            if task is not None:
                task.assigned_user_id = to_user_id
                task.window_start_at = start_at
                task.window_end_at = end_at

            reservation = (
                self.repo.get_reservation(entry.reservation_id)
                if entry.reservation_id
                else None
            )
            if reservation is not None:
                reservation.resource_user_id = to_user_id
                reservation.start_at = start_at
                reservation.end_at = end_at
                reservation.occupied_range = tstzrange(start_at, end_at)
                moved_reservations += 1

        templates = self.repo.reassign_templates(
            merchant_id, from_user_id, to_user_id
        )
        self.session.flush()

        if moved or templates:
            self.audit.record(
                object_type="MERCHANT_SCHEDULE_REASSIGN",
                object_id=merchant_id,
                event_type=EVENT_UPDATE,
                actor_id=actor_id,
                after={
                    "from_user_id": str(from_user_id),
                    "to_user_id": str(to_user_id),
                    "effective_day": effective_day.isoformat(),
                    "entries": moved,
                    "reservations": moved_reservations,
                    "templates": templates,
                    "skipped": skipped,
                },
                reason="商家调拨生效：今后的排班与模板一并改派新负责人",
            )
        return {
            "entries": moved,
            "reservations": moved_reservations,
            "templates": templates,
            "skipped": skipped,
        }

    def _busy_for_reassign(
        self, to_user_id: UUID, entry: SchedulePlanEntry, *, exclude: UUID | None
    ) -> dict[tuple[UUID, date], list[tuple[datetime, datetime]]]:
        """新负责人在该条目当天（及目标周）已有的 ACTIVE 占用。"""
        day_start = datetime.combine(entry.planned_date, time(0, 0), tzinfo=SHANGHAI_TZ)
        rows = self.repo.active_reservations_for_users(
            {to_user_id},
            (day_start - timedelta(days=1)).astimezone(UTC),
            (day_start + timedelta(days=2)).astimezone(UTC),
            entry.dataset_id,
        )
        busy: dict[tuple[UUID, date], list[tuple[datetime, datetime]]] = {}
        for r in rows:
            if exclude is not None and r.id == exclude:
                continue
            self._remember_busy(busy, r.resource_user_id, r.start_at, r.end_at)
        return busy

    @staticmethod
    def _overlaps_any(
        start_at: datetime, end_at: datetime,
        taken: list[tuple[datetime, datetime]],
    ) -> bool:
        from app.services.schedule_conflict_service import overlaps_half_open

        return any(
            overlaps_half_open(start_at, end_at, b_start, b_end)
            for b_start, b_end in taken
        )

    def reconcile_schedule_assignees(self, *, apply: bool = True) -> dict:
        """修复历史不一致：让排班条目/占用跟随其巡访任务的当前负责人。

        调拨曾只转任务不改排班，存量数据里会出现"排班归 A、任务归 B"：
        业务员在排班页看到自己当天有排班，巡访计划里却完全没有。
        以任务的 `assigned_user_id` 为准回填条目与占用（时段不变）；
        新负责人该时段已被占用（占用排他约束冲突）时跳过并上报，交人工改期。
        """
        rows = self.session.execute(
            select(SchedulePlanEntry, VisitTask)
            .join(
                SchedulePlanBatch,
                SchedulePlanBatch.id == SchedulePlanEntry.batch_id,
            )
            .join(VisitTask, VisitTask.id == SchedulePlanEntry.visit_task_id)
            .where(SchedulePlanBatch.status == "PUBLISHED")
            .where(VisitTask.assigned_user_id != SchedulePlanEntry.assigned_user_id)
        ).all()

        fixed = 0
        conflicts: list[str] = []
        for entry, task in rows:
            if not apply:
                fixed += 1
                continue
            savepoint = self.session.begin_nested()
            try:
                entry.assigned_user_id = task.assigned_user_id
                reservation = (
                    self.repo.get_reservation(entry.reservation_id)
                    if entry.reservation_id
                    else None
                )
                if reservation is not None:
                    reservation.resource_user_id = task.assigned_user_id
                self.session.flush()
                savepoint.commit()
                fixed += 1
            except IntegrityError:
                savepoint.rollback()
                conflicts.append(str(entry.id))

        if fixed and apply:
            self.session.flush()
        return {
            "mismatched": len(rows),
            "fixed": fixed,
            "conflicts": conflicts,
            "dry_run": not apply,
        }

    # ============================ 内部 ============================

    def _to_occupancy(self, e: SchedulePlanEntry) -> Occupancy:
        if e.start_at is None or e.end_at is None:
            start_at, end_at = self._default_window(e.planned_date)
        else:
            start_at, end_at = e.start_at, e.end_at
        return Occupancy(
            key=str(e.id),
            resource_user_id=e.assigned_user_id,
            start_at=start_at,
            end_at=end_at,
            merchant_id=e.merchant_id,
            planned_date=e.planned_date,
        )

    @staticmethod
    def _default_window(planned_date: date) -> tuple[datetime, datetime]:
        start_local = datetime.combine(planned_date, time(9, 0), tzinfo=SHANGHAI_TZ)
        return (
            start_local.astimezone(UTC),
            (start_local + DEFAULT_VISIT_DURATION).astimezone(UTC),
        )

    # ---------------- 一天多家商家：自动分时段 ----------------

    def _plan_occupancies(
        self,
        entries: list[SchedulePlanEntry],
        *,
        dataset_id: UUID,
        week_start: datetime,
        week_end: datetime,
    ) -> list[Occupancy]:
        """把条目换成待校验占用；只给到“天”的条目按业务员自动分时段。

        PRD 的冲突口径不变（同一业务员的占用区间不得重叠），但“一天排多家商家”
        本身不是冲突：没有显式时间窗时，从 09:00 起按 30 分钟一格顺延，
        自动避开既有占用与同批已分配时段。
        """
        existing = self.repo.active_reservations_for_users(
            {e.assigned_user_id for e in entries}, week_start, week_end, dataset_id
        )
        busy: dict[tuple[UUID, date], list[tuple[datetime, datetime]]] = {}
        for r in existing:
            self._remember_busy(busy, r.resource_user_id, r.start_at, r.end_at)
        # 显式时间窗先占位，避免自动分时段撞上同批的固定时段
        for e in entries:
            if e.start_at is not None and e.end_at is not None:
                self._remember_busy(busy, e.assigned_user_id, e.start_at, e.end_at)

        candidates: list[Occupancy] = []
        for e in entries:
            if e.start_at is None or e.end_at is None:
                start_at, end_at = self._next_free_slot(
                    e.assigned_user_id, e.planned_date, busy
                )
                # 落库：周视图/改期/任务时间窗都以实际时段为准
                e.start_at, e.end_at = start_at, end_at
                self._remember_busy(busy, e.assigned_user_id, start_at, end_at)
            candidates.append(
                Occupancy(
                    key=str(e.id),
                    resource_user_id=e.assigned_user_id,
                    start_at=e.start_at,
                    end_at=e.end_at,
                    merchant_id=e.merchant_id,
                    planned_date=e.planned_date,
                )
            )
        return candidates

    @staticmethod
    def _remember_busy(
        busy: dict[tuple[UUID, date], list[tuple[datetime, datetime]]],
        user_id: UUID,
        start_at: datetime,
        end_at: datetime,
    ) -> None:
        day = start_at.astimezone(SHANGHAI_TZ).date()
        busy.setdefault((user_id, day), []).append((start_at, end_at))

    def _next_free_slot(
        self,
        user_id: UUID,
        day: date,
        busy: dict[tuple[UUID, date], list[tuple[datetime, datetime]]],
    ) -> tuple[datetime, datetime]:
        """当天从 09:00 起找第一个 30 分钟空档（最晚到 18:00）。"""
        from app.services.schedule_conflict_service import overlaps_half_open

        cursor = datetime.combine(day, time(9, 0), tzinfo=SHANGHAI_TZ)
        deadline = datetime.combine(day, time(18, 0), tzinfo=SHANGHAI_TZ)
        taken = busy.get((user_id, day), [])
        while cursor + DEFAULT_VISIT_DURATION <= deadline:
            start_at = cursor.astimezone(UTC)
            end_at = (cursor + DEFAULT_VISIT_DURATION).astimezone(UTC)
            if not any(
                overlaps_half_open(start_at, end_at, b_start, b_end)
                for b_start, b_end in taken
            ):
                return start_at, end_at
            cursor += DEFAULT_VISIT_DURATION
        raise DomainError(
            code="SCHEDULE_DAY_FULL",
            message="该业务员当天时段已排满（09:00–18:00），请换一天或先改期",
        )

    # ---------------- 每周循环：发布即沉淀模板 ----------------

    def _upsert_templates_from_entries(
        self, entries: list[SchedulePlanEntry], region: Region
    ) -> list[ScheduleTemplate]:
        """发布后把条目沉淀为每周模板（已存在模板的商家不覆盖）。"""
        existing = {
            mid
            for (mid,) in self.session.execute(
                select(ScheduleTemplate.merchant_id)
                .where(ScheduleTemplate.region_id == region.id)
                .where(ScheduleTemplate.dataset_id == region.dataset_id)
            ).all()
        }
        created: list[ScheduleTemplate] = []
        for e in entries:
            if e.merchant_id in existing:
                continue
            start_local = (
                e.start_at.astimezone(SHANGHAI_TZ) if e.start_at
                else datetime.combine(e.planned_date, time(9, 0), tzinfo=SHANGHAI_TZ)
            )
            end_local = (
                e.end_at.astimezone(SHANGHAI_TZ) if e.end_at
                else start_local + DEFAULT_VISIT_DURATION
            )
            template = ScheduleTemplate(
                region_id=region.id,
                merchant_id=e.merchant_id,
                assigned_user_id=e.assigned_user_id,
                weekday=e.planned_date.isoweekday(),
                start_time=start_local.time().replace(tzinfo=None),
                end_time=end_local.time().replace(tzinfo=None),
                priority=e.priority,
                is_active=True,
                dataset_type=region.dataset_type,
                dataset_id=region.dataset_id,
            )
            self.session.add(template)
            existing.add(e.merchant_id)
            created.append(template)
        return created

    # ---------------- 改期：把当天的商家调到其他天 ----------------

    def move_entry(
        self,
        actor: ActorContext,
        *,
        entry_id: UUID,
        planned_date: date,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> SchedulePlanEntry:
        """把一条已发布排班改到同周另一天（同步改巡访任务与人员占用）。

        典型场景：该业务员当天要安排地推，经理把当天需拜访的商家整体挪到其他天。
        改期同样走冲突唯一裁决（排除条目自身占用），冲突则整条拒绝、零写入。
        """
        self._require_manage(actor)
        entry = self.repo.get_entry(entry_id)
        if entry is None:
            raise DomainError(
                code="SCHEDULE_ENTRY_NOT_FOUND", message="排班条目不存在"
            )
        batch = self.repo.get_batch(entry.batch_id)
        if batch is None or batch.dataset_id != entry.dataset_id:
            raise DomainError(
                code="SCHEDULE_BATCH_SCOPE_BROKEN", message="排班条目与批次数据范围不一致"
            )
        if batch.status != ScheduleStatus.PUBLISHED.value:
            raise DomainError(
                code="SCHEDULE_ENTRY_NOT_PUBLISHED",
                message="只有已发布的排班才能改期；草稿请直接调整批次",
            )
        self._assert_region(actor, batch.region_id)
        week_start = batch.week_start_date
        if not (week_start <= planned_date < week_start + timedelta(days=7)):
            raise DomainError(
                code="SCHEDULE_DATE_OUT_OF_WEEK",
                message="目标日期必须落在同一业务周内",
            )

        if (start_at is None) != (end_at is None):
            raise DomainError(
                code="SCHEDULE_WINDOW_INCOMPLETE",
                message="时间窗必须同时提供开始与结束，或都留空（默认沿用原时段）",
            )

        exclude = {entry.reservation_id} if entry.reservation_id else None
        if start_at is not None and end_at is not None:
            new_start = self._as_utc(start_at)
            new_end = self._as_utc(end_at)
            if new_end <= new_start:
                raise DomainError(
                    code="SCHEDULE_WINDOW_ORDER",
                    message="结束时间必须晚于开始时间（半开区间）",
                )
            candidate = self._move_candidate(entry, planned_date, new_start, new_end)
            conflicts = self._detect_for_move(
                entry, candidate, dataset_id=batch.dataset_id, exclude=exclude
            )
            if conflicts:
                raise ScheduleConflictError(conflicts)
        else:
            # 默认沿用原时间段的“时分”，落在目标日期上
            old_local = (
                entry.start_at.astimezone(SHANGHAI_TZ) if entry.start_at else None
            )
            duration = (
                entry.end_at - entry.start_at
                if entry.start_at and entry.end_at else DEFAULT_VISIT_DURATION
            )
            if old_local is None:
                new_start, new_end = self._next_free_slot(
                    entry.assigned_user_id, planned_date, self._busy_for_move(entry, exclude)
                )
                conflicts = self._detect_for_move(
                    entry,
                    self._move_candidate(entry, planned_date, new_start, new_end),
                    dataset_id=batch.dataset_id,
                    exclude=exclude,
                )
                if conflicts:
                    raise ScheduleConflictError(conflicts)
            else:
                keep_start = datetime.combine(
                    planned_date, old_local.time(), tzinfo=SHANGHAI_TZ
                )
                new_start = keep_start.astimezone(UTC)
                new_end = new_start + duration
                conflicts = self._detect_for_move(
                    entry,
                    self._move_candidate(entry, planned_date, new_start, new_end),
                    dataset_id=batch.dataset_id,
                    exclude=exclude,
                )
                if conflicts:
                    # 目标日同时段已被占用：自动顺延到当天第一个空档
                    new_start, new_end = self._next_free_slot(
                        entry.assigned_user_id, planned_date,
                        self._busy_for_move(entry, exclude),
                    )
                    conflicts = self._detect_for_move(
                        entry,
                        self._move_candidate(entry, planned_date, new_start, new_end),
                        dataset_id=batch.dataset_id,
                        exclude=exclude,
                    )
                    if conflicts:
                        raise ScheduleConflictError(conflicts)

        before = {
            "planned_date": entry.planned_date.isoformat(),
            "start_at": entry.start_at.isoformat() if entry.start_at else None,
            "end_at": entry.end_at.isoformat() if entry.end_at else None,
        }
        entry.planned_date = planned_date
        entry.start_at = new_start
        entry.end_at = new_end

        if entry.visit_task_id:
            task = self.repo.get_visit_task(entry.visit_task_id)
            if task is not None:
                task.planned_date = planned_date
                task.window_start_at = new_start
                task.window_end_at = new_end

        if entry.reservation_id:
            reservation = self.repo.get_reservation(entry.reservation_id)
            if reservation is not None:
                reservation.start_at = new_start
                reservation.end_at = new_end
                reservation.occupied_range = tstzrange(new_start, new_end)

        self.session.flush()
        self.audit.record(
            object_type="SCHEDULE_PLAN_ENTRY",
            object_id=entry.id,
            event_type=EVENT_UPDATE,
            actor_id=actor.user_id,
            before=before,
            after={
                "planned_date": planned_date.isoformat(),
                "start_at": new_start.isoformat(),
                "end_at": new_end.isoformat(),
            },
            reason="经理改期（避让地推/活动占用）",
        )
        return entry

    def _move_candidate(
        self,
        entry: SchedulePlanEntry,
        planned_date: date,
        start_at: datetime,
        end_at: datetime,
    ) -> Occupancy:
        return Occupancy(
            key=str(entry.id),
            resource_user_id=entry.assigned_user_id,
            start_at=start_at,
            end_at=end_at,
            merchant_id=entry.merchant_id,
            planned_date=planned_date,
        )

    def _detect_for_move(
        self,
        entry: SchedulePlanEntry,
        candidate: Occupancy,
        *,
        dataset_id: UUID,
        exclude: set[UUID] | None,
    ) -> list[Conflict]:
        day_local = candidate.planned_date
        day_start = datetime.combine(day_local, time(0, 0), tzinfo=SHANGHAI_TZ)
        day_end = day_start + timedelta(days=1)
        return self.conflicts_svc.detect(
            [candidate],
            dataset_id=dataset_id,
            window_start=day_start.astimezone(UTC),
            window_end=day_end.astimezone(UTC),
            exclude_reservation_ids=exclude,
        )

    def _busy_for_move(
        self, entry: SchedulePlanEntry, exclude: set[UUID] | None
    ) -> dict[tuple[UUID, date], list[tuple[datetime, datetime]]]:
        day_start = datetime.combine(entry.planned_date, time(0, 0), tzinfo=SHANGHAI_TZ)
        rows = self.repo.active_reservations_for_users(
            {entry.assigned_user_id},
            (day_start - timedelta(days=7)).astimezone(UTC),
            (day_start + timedelta(days=14)).astimezone(UTC),
            entry.dataset_id,
        )
        busy: dict[tuple[UUID, date], list[tuple[datetime, datetime]]] = {}
        for r in rows:
            if exclude and r.id in exclude:
                continue
            self._remember_busy(busy, r.resource_user_id, r.start_at, r.end_at)
        return busy

    def _normalize_window(self, e: dict, planned_date: date):
        start_at = e.get("start_at")
        end_at = e.get("end_at")
        if start_at is None and end_at is None:
            return None, None  # 仅指定业务日，发布时用默认时段生成占用
        if start_at is None or end_at is None:
            raise DomainError(
                code="SCHEDULE_WINDOW_INCOMPLETE",
                message="时间窗必须同时提供开始与结束，或都留空",
            )
        start_at = self._as_utc(start_at)
        end_at = self._as_utc(end_at)
        if end_at <= start_at:
            raise DomainError(
                code="SCHEDULE_WINDOW_ORDER", message="结束时间必须晚于开始时间（半开区间）"
            )
        return start_at, end_at

    @staticmethod
    def _as_utc(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            # 无时区视为 Asia/Shanghai 本地时间
            dt = dt.replace(tzinfo=SHANGHAI_TZ)
        return dt.astimezone(UTC)

    def _frequency_days(self, grade: str | None) -> int:
        from app.models import MerchantGradeFrequency
        today = datetime.now(SHANGHAI_TZ).date()
        row = self.session.scalars(
            select(MerchantGradeFrequency)
            .where(MerchantGradeFrequency.grade == (grade or "C"))
            .where(MerchantGradeFrequency.start_date <= today)
            .where(
                (MerchantGradeFrequency.end_date.is_(None))
                | (MerchantGradeFrequency.end_date >= today)
            )
            .order_by(MerchantGradeFrequency.start_date.desc())
        ).first()
        return row.frequency_days if row else DEFAULT_FREQUENCY_DAYS

    @staticmethod
    def _overdue_days(last_at: datetime | None, frequency_days: int,
                      today: date) -> int:
        """正=逾期天数；负=距下次应巡还有几天；从未巡访给较大正值以保证进入建议。"""
        if last_at is None:
            return frequency_days  # 从未巡访：视为已到期（至少要排一次）
        last_local = last_at.astimezone(SHANGHAI_TZ).date()
        due = last_local + timedelta(days=frequency_days)
        return (today - due).days

    def _assert_entries_in_scope(
        self, entries: list[dict], region_id: UUID, dataset_id: UUID
    ) -> None:
        """条目的商家与负责人必须属于同片区/同数据范围（服务端行级范围，红线）。"""
        merchant_ids = [e["merchant_id"] for e in entries]
        user_ids = {e["assigned_user_id"] for e in entries}
        merchants = self.repo.list_active_merchants(None, dataset_id)
        m_map = {m.id: m for m in merchants}
        for mid in merchant_ids:
            m = m_map.get(mid)
            if m is None:
                raise DomainError(
                    code="SCHEDULE_MERCHANT_NOT_FOUND",
                    message=f"商家不存在或非 ACTIVE: {mid}",
                )
            if m.region_id != region_id:
                raise PermissionDeniedError(
                    code="SCHEDULE_MERCHANT_REGION",
                    message="商家不属于目标片区",
                )
        # 负责人必须存在、在职、且属于本数据范围
        from app.models import User
        users = self.session.scalars(
            select(User).where(User.id.in_(user_ids))
        ).all()
        u_map = {u.id: u for u in users}
        for uid in user_ids:
            u = u_map.get(uid)
            if u is None or u.status != "ACTIVE" or u.dataset_id != dataset_id:
                raise DomainError(
                    code="SCHEDULE_ASSIGNEE_INVALID",
                    message="负责人不存在/在职状态异常/数据范围不一致",
                )

    def _region_or_404(self, region_id: UUID) -> Region:
        region = self.repo.get_region(region_id)
        if region is None:
            raise DomainError(code="REGION_NOT_FOUND", message="片区不存在")
        return region

    def _readable_region_ids(self, actor: ActorContext) -> list[UUID]:
        if actor.role_code == "SYSTEM_ADMIN":
            return []  # 空列表语义见 repo：不过滤
        if not actor.region_ids:
            return [uuid4()]  # 无授权：给不可能命中的 id
        return list(actor.region_ids)

    def _assert_region(self, actor: ActorContext, region_id: UUID) -> None:
        if actor.role_code == "SYSTEM_ADMIN":
            return
        if region_id not in (actor.region_ids or set()):
            raise PermissionDeniedError(
                code="REGION_FORBIDDEN", message="无权操作该片区的排班"
            )

    def _require_manage(self, actor: ActorContext) -> None:
        # 发布/草稿只允许片区经理及以上；业务员只读
        self.permissions.require_ability(actor, MODULE_SCHEDULE)
        if actor.role_code not in ("REGIONAL_MANAGER", "SYSTEM_ADMIN"):
            raise PermissionDeniedError(
                code="SCHEDULE_MANAGE_FORBIDDEN",
                message="只有片区经理及以上可创建/发布排班",
            )


class ScheduleConflictError(DomainError):
    """发布时检测到一条或多条冲突；整批拒绝（不部分写入）。"""

    def __init__(self, conflicts: list[Conflict]) -> None:
        super().__init__(
            code="SCHEDULE_CONFLICT",
            message=f"排班冲突 {len(conflicts)} 条，整批未发布",
        )
        self.conflicts = conflicts

    def to_dict(self) -> dict:
        base = super().to_dict()
        base["conflicts"] = [c.to_dict() for c in self.conflicts]
        return base

"""地推活动服务（PRD §4.7 / R39，T15）。

边界与红线：
- 人员时间冲突**只**调用 T13 的 ScheduleConflictService（通用 ResourceReservation 占用源），
  本服务绝不自行重算区间重叠；发布时为每个参与人写 CAMPAIGN 占用，任意冲突即整体失败、
  零写入（全有或全无），由外层事务回滚；
- 取消必须填原因，并只释放**未来时段**占用（已经开始/过去的占用保留事实）；
- 样品只做领用/消耗/归还/缺失台账（首版不做完整 WMS），样品成本计入活动费用
  sample_cost_fen，**不进确认销量与库存**；
- 复盘时按 SKU 校验“领用 = 消耗 + 归还 + 缺失”，样品未归还/缺失则为每个参与人各生成
  一条幂等提醒待办（复用 T05 TaskService）；
- 已审核事实不物理删除，只做状态迁移并留原因；审计与写操作同事务；提交幂等（重复发布/
  取消/复盘直接返回当前态）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.errors import DomainError, PermissionDeniedError
from app.core.permissions import (
    MODULE_ACTIVITY,
    ActorContext,
    PermissionService,
)
from app.models import (
    Activity,
    ActivityParticipant,
    ActivitySampleLedger,
    Region,
    ResourceReservation,
    Sku,
)
from app.models.enums import ActivityStatus
from app.repositories.activity_repository import (
    ACTIVE_RESERVATION,
    RELEASED_RESERVATION,
    SOURCE_OBJECT_ACTIVITY,
    SOURCE_TYPE_CAMPAIGN,
    STAGE_CONSUMED,
    STAGE_MISSING,
    STAGE_RETURNED,
    STAGE_TAKEN_OUT,
    ActivityRepository,
)
from app.services.audit_service import AuditService
from app.services.schedule_conflict_service import (
    Conflict,
    Occupancy,
    ScheduleConflictService,
)
from app.services.schedule_service import tstzrange
from app.services.task_service import TASK_KIND_CAMPAIGN_SAMPLE_RETURN, TaskService

_MANAGE_ROLES = ("REGIONAL_MANAGER", "SYSTEM_ADMIN")


class ActivityConflictError(DomainError):
    """活动发布检测到人员时间冲突；整体拒绝（不部分写入）。"""

    def __init__(self, conflicts: list[Conflict]) -> None:
        super().__init__(
            code="ACTIVITY_CONFLICT",
            message=f"活动人员时间冲突 {len(conflicts)} 条，活动未发布",
        )
        self.conflicts = conflicts

    def to_dict(self) -> dict:
        base = super().to_dict()
        base["conflicts"] = [c.to_dict() for c in self.conflicts]
        return base


class ActivityService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = ActivityRepository(session)
        self.permissions = PermissionService()
        self.conflicts_svc = ScheduleConflictService(session)
        self.audit = AuditService(session)
        self.tasks = TaskService(session)

    # ============================ 草稿 / 发布 ============================

    def create_draft(
        self,
        actor: ActorContext,
        *,
        region_id: UUID,
        name: str,
        start_at: datetime,
        end_at: datetime,
        location: str,
        owner_user_id: UUID,
        participant_user_ids: list[UUID],
        budget_fen: int = 0,
        notes: str | None = None,
        samples: list[dict] | None = None,
    ) -> Activity:
        """创建活动草稿（含参与人、样品领用台账）；草稿不写人员占用，允许存在冲突。"""
        self._require_manage(actor)
        region = self._region_or_404(region_id)
        self._assert_region(actor, region_id)
        start_at, end_at = self._normalize_window(start_at, end_at)
        dataset_id = region.dataset_id

        owner = self.repo.get_active_users([owner_user_id], dataset_id)
        if len(owner) != 1:
            raise DomainError(
                code="ACTIVITY_OWNER_INVALID",
                message="活动负责人不存在/非在职/数据范围不一致",
            )

        participant_ids = self._validate_participants(participant_user_ids, dataset_id)
        if owner_user_id not in participant_ids:
            participant_ids.add(owner_user_id)  # 负责人默认也是占用参与人

        activity = Activity(
            name=name,
            start_at=start_at,
            end_at=end_at,
            location=location,
            owner_user_id=owner_user_id,
            budget_fen=budget_fen,
            status=ActivityStatus.DRAFT.value,
            notes=notes,
            region_id=region_id,
            dataset_type="PRODUCTION",
            dataset_id=dataset_id,
        )
        self.repo.add_activity(activity)
        for uid in sorted(participant_ids, key=lambda x: str(x)):
            self.repo.add_participant(ActivityParticipant(
                activity_id=activity.id, user_id=uid,
                dataset_type="PRODUCTION", dataset_id=dataset_id,
            ))

        # 样品领用登记（草稿即登记 TAKEN_OUT，发布后才对人员产生时间占用）
        self._record_sample_lines(
            activity, samples or [], STAGE_TAKEN_OUT, owner_user_id, dataset_id
        )

        self.audit.record(
            object_type="CAMPAIGN", object_id=activity.id,
            event_type="CREATE", actor_id=actor.user_id,
            after={"status": "DRAFT", "participants": len(participant_ids)},
            reason="创建地推活动草稿",
        )
        self.session.flush()
        return activity

    def publish(self, actor: ActorContext, *, activity_id: UUID) -> Activity:
        """发布活动：冲突唯一裁决 → 为每个参与人写 CAMPAIGN 占用（全有或全无）。"""
        self._require_manage(actor)
        activity = self._activity_or_404(activity_id)
        self._assert_region(actor, activity.region_id)

        # 幂等：已发布及之后状态直接返回当前态（重复点击/重试不重复生成占用）
        if activity.status != ActivityStatus.DRAFT.value:
            return activity

        participant_ids = self.repo.participant_user_ids(activity_id)
        if not participant_ids:
            raise DomainError(code="ACTIVITY_EMPTY", message="活动没有参与人，无法发布")

        # 红线：冲突只来自 ScheduleConflictService；候选彼此（批内）+ 与既有 ACTIVE 占用
        candidates = [
            Occupancy(
                key=str(uid),
                resource_user_id=uid,
                start_at=activity.start_at,
                end_at=activity.end_at,
            )
            for uid in sorted(participant_ids, key=lambda x: str(x))
        ]
        conflicts = self.conflicts_svc.detect(
            candidates,
            dataset_id=activity.dataset_id,
            window_start=activity.start_at,
            window_end=activity.end_at,
        )
        if conflicts:
            raise ActivityConflictError(conflicts)

        # 无冲突：为每个参与人生成一条 CAMPAIGN 占用
        for occ in candidates:
            self.repo.add_reservation(ResourceReservation(
                resource_user_id=occ.resource_user_id,
                start_at=occ.start_at,
                end_at=occ.end_at,
                occupied_range=tstzrange(occ.start_at, occ.end_at),
                source_type=SOURCE_TYPE_CAMPAIGN,
                status=ACTIVE_RESERVATION,
                merchant_id=None,
                source_object_type=SOURCE_OBJECT_ACTIVITY,
                source_object_id=activity.id,
                dataset_type="PRODUCTION",
                dataset_id=activity.dataset_id,
            ))

        before = activity.status
        activity.status = ActivityStatus.PUBLISHED.value
        self.session.flush()
        self.audit.record_state_transition(
            object_type="CAMPAIGN", object_id=activity.id,
            actor_id=actor.user_id,
            before={"status": before}, after={"status": "PUBLISHED"},
            reason="发布地推活动，登记参与人员时间占用",
        )
        return activity

    # ============================ 执行 / 取消 ============================

    def start(self, actor: ActorContext, *, activity_id: UUID) -> Activity:
        """标记活动执行中（PUBLISHED → IN_PROGRESS）。"""
        self._require_manage(actor)
        activity = self._activity_or_404(activity_id)
        self._assert_region(actor, activity.region_id)
        if activity.status == ActivityStatus.IN_PROGRESS.value:
            return activity  # 幂等
        self._require_status(activity, (ActivityStatus.PUBLISHED.value,), "开始执行")
        before = activity.status
        activity.status = ActivityStatus.IN_PROGRESS.value
        self.session.flush()
        self.audit.record_state_transition(
            object_type="CAMPAIGN", object_id=activity.id, actor_id=actor.user_id,
            before={"status": before}, after={"status": "IN_PROGRESS"},
            reason="活动开始执行",
        )
        return activity

    def cancel(self, actor: ActorContext, *, activity_id: UUID, reason: str) -> Activity:
        """取消活动（必须填原因）：释放本活动**未来时段**的 ACTIVE 占用。"""
        self._require_manage(actor)
        if not reason or not reason.strip():
            raise DomainError(
                code="ACTIVITY_CANCEL_REASON_REQUIRED",
                message="取消活动必须填写原因",
            )
        activity = self._activity_or_404(activity_id)
        self._assert_region(actor, activity.region_id)
        if activity.status == ActivityStatus.CANCELLED.value:
            return activity  # 幂等
        self._require_status(
            activity,
            (ActivityStatus.PUBLISHED.value, ActivityStatus.IN_PROGRESS.value),
            "取消",
        )

        # 加行锁串行化，只释放尚未开始（start_at 在当前时刻之后）的占用
        now = datetime.now(UTC)
        reservations = self.repo.lock_campaign_reservations(activity_id)
        released = 0
        for r in reservations:
            if r.start_at > now:
                r.status = RELEASED_RESERVATION
                released += 1

        before = activity.status
        activity.status = ActivityStatus.CANCELLED.value
        activity.cancel_reason = reason.strip()
        activity.cancelled_at = now
        activity.cancelled_by_id = actor.user_id
        self.session.flush()
        self.audit.record_state_transition(
            object_type="CAMPAIGN", object_id=activity.id, actor_id=actor.user_id,
            before={"status": before},
            after={"status": "CANCELLED", "released_future_reservations": released},
            reason=reason.strip(),
        )
        return activity

    # ============================ 复盘 ============================

    def review(
        self,
        actor: ActorContext,
        *,
        activity_id: UUID,
        sample_results: list[dict],
    ) -> Activity:
        """复盘（IN_PROGRESS/PUBLISHED → REVIEWED）。

        - 登记样品实际消耗/归还/缺失台账，校验按 SKU 平衡（领用 = 消耗+归还+缺失）；
        - 样品成本按实耗（消耗+缺失）× 参考进价计入活动费用，不进确认销量与库存；
        - 存在未归还（消耗/缺失）样品时，为每个参与人各生成一条幂等提醒待办。
        """
        self._require_manage(actor)
        activity = self._activity_or_404(activity_id)
        self._assert_region(actor, activity.region_id)
        if activity.status == ActivityStatus.REVIEWED.value:
            return activity  # 幂等
        self._require_status(
            activity,
            (ActivityStatus.PUBLISHED.value, ActivityStatus.IN_PROGRESS.value),
            "复盘",
        )

        dataset_id = activity.dataset_id
        # 1) 登记复盘结果台账
        for line in sample_results:
            self._record_one_sample(
                activity, line, operator_id=actor.user_id, dataset_id=dataset_id
            )

        # 2) 按 SKU 平衡校验
        ledger = self.repo.list_sample_ledger(activity_id)
        balance = self._check_sample_balance(ledger)

        # 3) 样品成本 = Σ(消耗+缺失箱数) × SKU 参考进价（分）
        cost_fen = self._sample_cost_fen(ledger)
        activity.sample_cost_fen = cost_fen

        # 4) 未归还/缺失 → 为每个参与人各生成提醒待办（幂等）
        unreturned_skus = sorted(
            sid for sid, q in balance.items()
            if q["CONSUMED"] + q["MISSING"] > 0
        )
        if unreturned_skus:
            participant_ids = sorted(
                self.repo.participant_user_ids(activity_id), key=lambda x: str(x)
            )
            price_map = self.repo.sku_price_map(unreturned_skus)
            for uid in participant_ids:
                self._assign_return_reminder(activity, uid, balance, price_map)

        before = activity.status
        now = datetime.now(UTC)
        activity.status = ActivityStatus.REVIEWED.value
        activity.reviewed_at = now
        activity.reviewed_by_id = actor.user_id
        self.session.flush()
        self.audit.record_state_transition(
            object_type="CAMPAIGN", object_id=activity.id, actor_id=actor.user_id,
            before={"status": before},
            after={"status": "REVIEWED", "sample_cost_fen": cost_fen,
                   "unreturned_sku_count": len(unreturned_skus)},
            reason="地推活动复盘，登记样品实际消耗/归还",
        )
        return activity

    # ============================ 列表 / 下拉选项 ============================

    def list_activities(
        self,
        actor: ActorContext,
        *,
        region_id: UUID | None = None,
        status: str | None = None,
        keyword: str | None = None,
        start_from: date | None = None,
        start_to: date | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict], int, dict]:
        """活动分页列表（含草稿）。

        - 行级范围只来自身份上下文：非系统管理员强制限定在其授权片区；显式传
          `region_id` 且不在授权内直接 403（与 detail/publish 同口径）；
        - `status` 只接受受控状态值，非法值直接拒绝；
        - 日期区间按业务时区（Asia/Shanghai）的自然日解释，转 UTC 后比较 start_at。
        """
        self.permissions.require_ability(actor, MODULE_ACTIVITY)
        if status is not None and status not in {s.value for s in ActivityStatus}:
            raise DomainError(
                code="ACTIVITY_STATUS_UNKNOWN",
                message="活动状态取值非法",
                details={"status": status},
            )
        scope = self._list_region_scope(actor, region_id)
        rows, total = self.repo.list_activities(
            region_ids=scope,
            status=status,
            keyword=(keyword or "").strip() or None,
            start_from=self._shanghai_day_start(start_from) if start_from else None,
            start_to=self._shanghai_day_end(start_to) if start_to else None,
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        ids = [a.id for a, _region, _owner in rows]
        participants = self.repo.participant_counts(ids)
        reservations = self.repo.active_reservation_counts(ids)
        items = [
            {
                "activity": activity,
                "region_name": region.name,
                "owner_name": owner.display_name if owner is not None else None,
                "participant_count": participants.get(activity.id, 0),
                "active_reservation_count": reservations.get(activity.id, 0),
            }
            for activity, region, owner in rows
        ]
        applied = {
            "region_id": str(region_id) if region_id is not None else None,
            "status": status,
            "keyword": (keyword or "").strip() or None,
            "start_from": start_from.isoformat() if start_from else None,
            "start_to": start_to.isoformat() if start_to else None,
            "page": page,
            "page_size": page_size,
        }
        return items, total, applied

    def list_user_options(
        self, actor: ActorContext, *, region_id: UUID
    ) -> list[User]:
        """可选活动人员（负责人/参与人下拉）。

        统一按目标片区解析数据范围：非系统管理员需已授权该片区，避免跨片区选人后
        在创建时才失败；系统管理员取该数据集全部在职用户。
        """
        self.permissions.require_ability(actor, MODULE_ACTIVITY)
        region = self._region_or_404(region_id)
        self._assert_region(actor, region_id)
        is_admin = actor.role_code == "SYSTEM_ADMIN"
        return self.repo.list_region_users(
            region.dataset_id, region_id, all_in_dataset=is_admin
        )

    def list_sku_options(self, actor: ActorContext, *, region_id: UUID) -> list[Sku]:
        """可选样品 SKU（与片区数据范围一致；未导入主数据时为空列表）。"""
        self.permissions.require_ability(actor, MODULE_ACTIVITY)
        region = self._region_or_404(region_id)
        self._assert_region(actor, region_id)
        return self.repo.list_dataset_skus(region.dataset_id)

    # ============================ 只读 ============================

    def get_detail(self, actor: ActorContext, *, activity_id: UUID) -> dict:
        self.permissions.require_ability(actor, MODULE_ACTIVITY)
        activity = self._activity_or_404(activity_id)
        self._assert_region(actor, activity.region_id)
        participants = self.repo.list_participants(activity_id)
        reservations = self.repo.active_campaign_reservations(activity_id)
        ledger = self.repo.list_sample_ledger(activity_id)
        return {
            "activity": activity,
            "participant_user_ids": [p.user_id for p in participants],
            "reservation_ids": [r.id for r in reservations],
            "ledger": ledger,
        }

    # ============================ 内部 ============================

    def _record_sample_lines(
        self,
        activity: Activity,
        samples: list[dict],
        stage: str,
        operator_id: UUID,
        dataset_id: UUID,
    ) -> None:
        for line in samples:
            self._record_one_sample(
                activity,
                {"sku_id": line["sku_id"], "stage": stage,
                 "boxes": line["boxes"], "note": line.get("note")},
                operator_id=operator_id,
                dataset_id=dataset_id,
            )

    def _record_one_sample(
        self,
        activity: Activity,
        line: dict,
        *,
        operator_id: UUID,
        dataset_id: UUID,
    ) -> ActivitySampleLedger:
        sku_id = line["sku_id"]
        stage = line["stage"]
        boxes = line["boxes"]
        if stage not in (STAGE_TAKEN_OUT, STAGE_CONSUMED, STAGE_RETURNED, STAGE_MISSING):
            raise DomainError(
                code="ACTIVITY_SAMPLE_STAGE_INVALID",
                message="样品台账阶段必须是 TAKEN_OUT/CONSUMED/RETURNED/MISSING",
            )
        boxes = Decimal(str(boxes))
        if boxes < 0:
            raise DomainError(
                code="ACTIVITY_SAMPLE_BOXES_INVALID",
                message="样品箱数不能为负",
            )
        if boxes == 0:
            raise DomainError(
                code="ACTIVITY_SAMPLE_BOXES_EMPTY",
                message="样品箱数必须大于 0",
            )
        sku = self.session.get(Sku, sku_id)
        if sku is None or sku.dataset_id != dataset_id:
            raise DomainError(
                code="ACTIVITY_SAMPLE_SKU_INVALID",
                message="样品 SKU 不存在或数据范围不一致",
            )
        row = ActivitySampleLedger(
            activity_id=activity.id,
            sku_id=sku_id,
            stage=stage,
            boxes=boxes,
            operator_id=operator_id,
            note=line.get("note"),
            dataset_type="PRODUCTION",
            dataset_id=dataset_id,
        )
        return self.repo.add_sample_ledger(row)

    def _check_sample_balance(self, ledger: list[ActivitySampleLedger]) -> dict:
        """按 SKU 汇总各阶段箱数并校验：领用 == 消耗 + 归还 + 缺失。"""
        agg: dict[UUID, dict[str, Decimal]] = {}
        for row in ledger:
            slot = agg.setdefault(
                row.sku_id,
                {"TAKEN_OUT": Decimal("0"), "CONSUMED": Decimal("0"),
                 "RETURNED": Decimal("0"), "MISSING": Decimal("0")},
            )
            slot[row.stage] += row.boxes
        for sku_id, q in agg.items():
            taken = q["TAKEN_OUT"]
            settled = q["CONSUMED"] + q["RETURNED"] + q["MISSING"]
            if taken != settled:
                raise DomainError(
                    code="ACTIVITY_SAMPLE_UNBALANCED",
                    message="样品台账不平：领用箱数必须等于消耗+归还+缺失",
                    details={
                        "sku_id": str(sku_id),
                        "taken_out": str(taken),
                        "settled": str(settled),
                    },
                )
        return agg

    def _sample_cost_fen(self, ledger: list[ActivitySampleLedger]) -> int:
        """样品成本（分）：Σ(消耗+缺失箱数) × 箱规 × SKU 参考进价（分/瓶）。

        参考进价缺失按 0、箱规未维护按 1；样品成本只计入活动费用，
        不进确认销量与库存（R39）。
        """
        used: dict[UUID, Decimal] = {}
        for row in ledger:
            if row.stage in (STAGE_CONSUMED, STAGE_MISSING):
                used[row.sku_id] = used.get(row.sku_id, Decimal("0")) + row.boxes
        costs = self.repo.sku_case_cost_map(used.keys())
        total = Decimal("0")
        for sku_id, boxes in used.items():
            unit_price_fen, bottles_per_case = costs.get(sku_id, (0, Decimal("1")))
            total += boxes * bottles_per_case * Decimal(unit_price_fen)
        return int(total.quantize(Decimal("1")))

    def _assign_return_reminder(
        self,
        activity: Activity,
        assignee_id: UUID,
        balance: dict,
        price_map: dict[UUID, int],
    ) -> None:
        lines = [
            f"SKU {sid}: 消耗 {q['CONSUMED']} / 缺失 {q['MISSING']} 箱"
            for sid, q in sorted(balance.items())
            if q["CONSUMED"] + q["MISSING"] > 0
        ]
        try:
            self.tasks.assign(
                title=f"活动「{activity.name}」样品未归还/缺失跟进",
                assignee_id=assignee_id,
                task_kind=TASK_KIND_CAMPAIGN_SAMPLE_RETURN,
                related_object_type=SOURCE_OBJECT_ACTIVITY,
                related_object_id=activity.id,
                description="；".join(lines),
            )
        except DomainError:
            # 幂等：同活动同责任人已有未关闭提醒则不重复生成（R27）
            pass

    def _validate_participants(
        self, participant_user_ids: list[UUID], dataset_id: UUID
    ) -> set[UUID]:
        ids = set(participant_user_ids or [])
        if not ids:
            raise DomainError(
                code="ACTIVITY_PARTICIPANTS_EMPTY",
                message="活动参与人不能为空",
            )
        users = self.repo.get_active_users(ids, dataset_id)
        valid = {u.id for u in users}
        invalid = ids - valid
        if invalid:
            raise DomainError(
                code="ACTIVITY_PARTICIPANT_INVALID",
                message="参与人不存在/非在职/数据范围不一致",
                details={"invalid_user_ids": [str(x) for x in invalid]},
            )
        return ids

    @staticmethod
    def _normalize_window(start_at: datetime, end_at: datetime) -> tuple[datetime, datetime]:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Asia/Shanghai")

        def as_utc(dt: datetime) -> datetime:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
            return dt.astimezone(UTC)

        start_at, end_at = as_utc(start_at), as_utc(end_at)
        if end_at <= start_at:
            raise DomainError(
                code="ACTIVITY_WINDOW_ORDER",
                message="活动结束时间必须晚于开始时间（半开区间）",
            )
        return start_at, end_at

    def _activity_or_404(self, activity_id: UUID) -> Activity:
        activity = self.repo.get_activity(activity_id)
        if activity is None:
            raise DomainError(code="ACTIVITY_NOT_FOUND", message="活动不存在")
        return activity

    def _region_or_404(self, region_id: UUID) -> Region:
        region = self.session.get(Region, region_id)
        if region is None:
            raise DomainError(code="REGION_NOT_FOUND", message="片区不存在")
        return region

    def _assert_region(self, actor: ActorContext, region_id: UUID) -> None:
        if actor.role_code == "SYSTEM_ADMIN":
            return
        if region_id not in (actor.region_ids or set()):
            raise PermissionDeniedError(
                code="REGION_FORBIDDEN", message="无权操作该片区的地推活动"
            )

    def _list_region_scope(
        self, actor: ActorContext, region_id: UUID | None
    ) -> set[UUID] | None:
        """列表用的片区范围（None=不限制，仅系统管理员可全局）。

        非管理员显式传未授权片区 → 403；未传则限定其全部授权片区，授权为空集时
        返回空集（列表为空，而不是越权看到全量）。
        """
        if actor.role_code == "SYSTEM_ADMIN":
            return {region_id} if region_id is not None else None
        allowed = set(actor.region_ids or set())
        if region_id is not None:
            if region_id not in allowed:
                raise PermissionDeniedError(
                    code="REGION_FORBIDDEN", message="无权查看该片区的地推活动"
                )
            return {region_id}
        return allowed

    @staticmethod
    def _shanghai_day_start(day: date) -> datetime:
        from zoneinfo import ZoneInfo

        return datetime(
            day.year, day.month, day.day, tzinfo=ZoneInfo("Asia/Shanghai")
        ).astimezone(UTC)

    @staticmethod
    def _shanghai_day_end(day: date) -> datetime:
        from datetime import timedelta

        return ActivityService._shanghai_day_start(
            day + timedelta(days=1)
        ) - timedelta(microseconds=1)

    def _require_manage(self, actor: ActorContext) -> None:
        self.permissions.require_ability(actor, MODULE_ACTIVITY)
        if actor.role_code not in _MANAGE_ROLES:
            raise PermissionDeniedError(
                code="ACTIVITY_MANAGE_FORBIDDEN",
                message="只有片区经理及以上可管理地推活动",
            )

    @staticmethod
    def _require_status(activity: Activity, allowed: tuple[str, ...], action: str) -> None:
        if activity.status not in allowed:
            raise DomainError(
                code="ACTIVITY_STATUS_INVALID",
                message=f"活动当前状态 {activity.status} 不允许{action}",
                details={"current_status": activity.status, "allowed": list(allowed)},
            )

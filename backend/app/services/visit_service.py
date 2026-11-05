"""巡访业务服务（PRD §4.6 / §4.8 / T11）。

职责：建任务（周排班/临时/整改/督导）、移动端时间窗、打卡提交、照片先传后绑、
异常经理判定、补充修订追加。有效性只委托 VisitValidityService；距离只信服务端
PostGIS 重算；原始证据一经提交不可覆盖。

事务/审计：所有写操作与审计同事务；提交按 idempotency_key / 任务唯一正式记录幂等。
错误映射：权限→PermissionDeniedError(403)；冲突→ConflictError(409)；其余业务→DomainError(422)。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ConflictError, DomainError, PermissionDeniedError
from app.core.logging import get_logger
from app.core.permissions import MODULE_VISIT, ActorContext, PermissionService
from app.models import (
    FileObject,
    Merchant,
    ReplenishmentOrder,
    Sku,
    User,
    VisitPhoto,
    VisitRecord,
    VisitRecordStatus,
    VisitRevision,
    VisitSalesLine,
    VisitSource,
    VisitTask,
    VisitTaskStatus,
)
from app.models.enums import ReplenishmentStatus
from app.models.merchant_assignment import MerchantAssignment
from app.repositories.visit_repository import VisitRepository, point_geography
from app.services.audit_service import (
    EVENT_CREATE,
    EVENT_STATE_TRANSITION,
    EVENT_UPDATE,
    AuditService,
)
from app.services.task_service import (
    TASK_KIND_PROBLEM_RECTIFICATION,
    TASK_KIND_REPLENISHMENT_REVIEW,
    TaskService,
)
from app.services.visit_validity_service import VisitValidityService

logger = get_logger(__name__)

# 无生效合同时陈列阈值的平台默认值（PRD §3 指标定义：默认 50%）
DEFAULT_DISPLAY_THRESHOLD_PCT = 50

VALID = "VALID"
ABNORMAL_PENDING = "ABNORMAL_PENDING"
INVALID = "INVALID"

_JUDGE_ROLES = frozenset({"REGIONAL_MANAGER", "SYSTEM_ADMIN"})
_SUPERVISION_ROLES = frozenset({"SUPERVISOR", "REGIONAL_MANAGER", "SYSTEM_ADMIN"})


class VisitService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = VisitRepository(session)
        self.permissions = PermissionService()
        self.validity = VisitValidityService()
        self.audit = AuditService(session)
        self.tasks = TaskService(session)

    # ================= 任务 =================

    def create_task(self, actor: ActorContext, payload) -> VisitTask:
        """创建巡访任务。责任人由服务端决定：业务员只能给自己排班（行级范围）。"""
        self.permissions.require_ability(actor, MODULE_VISIT)

        merchant = self._merchant_or_404(payload.merchant_id)
        self._assert_merchant_in_scope(actor, merchant)

        if payload.source == VisitSource.SUPERVISION and actor.role_code not in _SUPERVISION_ROLES:
            raise PermissionDeniedError(
                code="VISIT_SUPERVISION_FORBIDDEN", message="仅督导/经理可安排督导跟访"
            )
        if (
            payload.window_start_at
            and payload.window_end_at
            and payload.window_end_at < payload.window_start_at
        ):
            raise DomainError(
                code="VISIT_WINDOW_INVALID", message="计划结束时间不能早于开始时间"
            )

        assignee_id = self._resolve_assignee(
            actor, payload.assigned_user_id, payload.source
        )
        if actor.profile.row_scope == "self" and not self._is_primary_owner(
            merchant.id, actor.user_id, payload.planned_date
        ):
            raise PermissionDeniedError(
                code="DATA_SCOPE_VIOLATION", message="只能为自己负责的商家安排巡访"
            )
        assignee = self.session.get(User, assignee_id)
        if assignee is None or assignee.dataset_id != merchant.dataset_id:
            raise DomainError(
                code="VISIT_ASSIGNEE_INVALID", message="责任人不存在或与商家数据范围不一致"
            )

        task = VisitTask(
            merchant_id=merchant.id,
            assigned_user_id=assignee_id,
            planned_date=payload.planned_date,
            window_start_at=payload.window_start_at,
            window_end_at=payload.window_end_at,
            priority=payload.priority,
            source=payload.source.value,
            reason=payload.reason,
            status=VisitTaskStatus.PENDING.value,
            dataset_id=merchant.dataset_id,
            dataset_type=merchant.dataset_type,
        )
        self.repo.add_task(task)
        self._audit(
            actor, "VISIT_TASK", task.id, EVENT_CREATE,
            after={
                "source": task.source,
                "merchant_id": str(merchant.id),
                "assigned_user_id": str(task.assigned_user_id),
                "planned_date": task.planned_date.isoformat(),
            },
        )
        return task

    def list_my_tasks(self, actor: ActorContext, on_date: date | None = None) -> list[VisitTask]:
        """责任人工作台任务列表：业务员看自己的；经理/督导看本片区。"""
        self.permissions.require_ability(actor, MODULE_VISIT)
        scope = actor.profile.row_scope
        region_ids: frozenset[UUID] | None = (
            actor.region_ids if scope == "region" else None
        )
        return self.repo.list_tasks_for_actor(
            user_id=actor.user_id, on_date=on_date, region_ids=region_ids
        )

    def get_task_detail(self, actor: ActorContext, task_id: UUID) -> tuple[VisitTask, Merchant]:
        self.permissions.require_ability(actor, MODULE_VISIT)
        task = self.repo.get_task(task_id)
        if task is None:
            raise DomainError(code="VISIT_TASK_NOT_FOUND", message="巡访任务不存在")
        merchant = self._merchant_or_404(task.merchant_id)
        self._assert_can_read(actor, task, merchant)
        return task, merchant

    def list_sku_options(self, actor: ActorContext, *, merchant_id: UUID) -> list[Sku]:
        """补货/销量行可选 SKU：同品牌 + 同数据范围（无主数据时返回空列表）。"""
        self.permissions.require_ability(actor, MODULE_VISIT)
        merchant = self._merchant_or_404(merchant_id)
        self._assert_merchant_in_scope(actor, merchant)
        return self.repo.list_skus_for_brand(merchant.brand_id, merchant.dataset_id)

    # ================= 提交打卡 =================

    def submit(self, actor: ActorContext, task_id: UUID, payload) -> VisitRecord:
        """业务员/督导提交一次巡访（visit.submit，§5.3.1）。

        红线：仅任务责任人可提交（非责任人 403）；一个任务只允许一条正式记录；
        距离服务端重算；有效性由 VisitValidityService 唯一裁决。
        """
        self.permissions.require_ability(actor, MODULE_VISIT)

        # 幂等：同 idempotency_key 直接返回既有正式记录
        if payload.idempotency_key:
            existing = self._find_by_idempotency(payload.idempotency_key)
            if existing is not None:
                return existing

        task = self.repo.get_task(task_id, for_update=True)
        if task is None:
            raise DomainError(code="VISIT_TASK_NOT_FOUND", message="巡访任务不存在")
        merchant = self._merchant_or_404(task.merchant_id)

        # 责任授权：提交者必须是任务责任人（行级范围由服务端注入，不接受客户端指定）
        if task.assigned_user_id != actor.user_id:
            raise PermissionDeniedError(
                code="VISIT_NOT_ASSIGNEE", message="只有该巡访任务的责任人可提交"
            )
        # 唯一正式记录（幂等，重复提交拒绝而非覆盖）
        if self.repo.official_record_for_task(task.id) is not None:
            raise ConflictError(
                code="VISIT_ALREADY_SUBMITTED", message="该巡访任务已提交，不可重复提交"
            )

        now = datetime.now(UTC)
        # 服务端权威距离：PostGIS ST_Distance（前端距离不作数）
        distance = self.repo.distance_to_merchant_meters(merchant, payload.lng, payload.lat)

        # 陈列阈值：从生效合同固化快照（R3），无生效合同回退平台默认 50%；后改合同不影响历史
        on_date = now.date()
        contract = self.repo.effective_contract(merchant.id, on_date)
        threshold_pct = (
            contract.display_threshold_pct if contract is not None
            else DEFAULT_DISPLAY_THRESHOLD_PCT
        )
        display_met = (
            payload.display_pct >= threshold_pct if payload.display_pct is not None else None
        )

        # 提交前校验：销量/补货行引用的 SKU 必须存在且属本数据范围（任一失败整体回滚）
        sku_ids = {ln.sku_id for ln in payload.sales_lines} | {
            ln.sku_id for ln in payload.replenishment_lines
        }
        sku_map = self.repo.get_skus(sku_ids)
        self._assert_skus_in_scope(sku_ids, sku_map, task.dataset_id)

        record = VisitRecord(
            visit_task_id=task.id,
            merchant_id=task.merchant_id,
            user_id=actor.user_id,
            source=task.source,
            location=point_geography(payload.lng, payload.lat),
            checkin_at=now,                                # 服务端接收时刻（UTC）
            checkout_at=payload.checkout_at,
            device_checkin_at=payload.device_checkin_at,    # 设备时间仅取证
            accuracy_meters=payload.accuracy_meters,
            distance_meters=distance,
            display_pct=payload.display_pct,
            freezer_status=payload.freezer_status.value,
            device_issue=payload.device_issue,
            conclusion=payload.conclusion.value,
            display_threshold_pct=threshold_pct,
            display_met=display_met,
            note=payload.note,
            photo_count=len(payload.photos),
            is_official=True,  # 一任务至多一条正式记录（部分唯一索引）
            submitted_at=now,
            idempotency_key=payload.idempotency_key,
            dataset_id=task.dataset_id,
            dataset_type=task.dataset_type,
        )

        # 先裁决再落状态（唯一裁决口径，跨卡钩子：不复制 T11 规则）
        verdict = self.validity.evaluate(
            task=task,
            location_present=True,
            checkin_at=now,
            photo_count=len(payload.photos),
            distance_meters=distance,
            threshold_meters=get_settings().visit_geofence_meters,
        )
        record.validity = verdict.validity
        if verdict.is_valid:
            record.status = VisitRecordStatus.RECOGNIZED.value
            task.status = VisitTaskStatus.COMPLETED.value
        elif verdict.needs_manager_judgement:
            # 定位超阈值/缺照片 → 异常待经理判定，判定前不计绩效/覆盖率
            record.status = VisitRecordStatus.ABNORMAL_PENDING.value
            task.status = VisitTaskStatus.ABNORMAL.value
        else:
            record.status = VisitRecordStatus.REJECTED.value
            record.validity = INVALID
            task.status = VisitTaskStatus.ABNORMAL.value
        self.repo.add_record(record)

        # 照片：先上传后绑定；只增不改；校验 file 属本数据范围且为就绪图片
        for ph in payload.photos:
            self._bind_photo(actor, record, ph.file_id, ph.photo_kind)

        # ---- T12：一次提交同事务联动（任一失败整体回滚）----
        # 1) 上报销量行：经理确认前不进正式口径（只落 visit_sales_lines，不写 SalesConfirmation）
        for ln in payload.sales_lines:
            self.repo.add_sales_line(
                VisitSalesLine(
                    visit_record_id=record.id,
                    sku_id=ln.sku_id,
                    reported_boxes=Decimal(ln.reported_boxes),
                    feedback=ln.feedback,
                    out_of_stock=ln.out_of_stock,
                    dataset_id=task.dataset_id,
                    dataset_type=task.dataset_type,
                )
            )

        # 2) 补货行 → 补货 DRAFT（PRD §4.9：DRAFT 先行，经理确认后才进正式补货池/采购）
        replenishment_ids: list[UUID] = []
        for ln in payload.replenishment_lines:
            order = self._create_replenishment_draft(
                actor=actor, record=record, merchant=merchant, line=ln, on_date=on_date
            )
            replenishment_ids.append(order.id)

        # 3) 待办联动：设备异常/陈列不达标/结论需整改 → 整改待办；补货草稿 → 经理复核待办
        self._create_followup_tasks(
            actor=actor,
            merchant=merchant,
            record=record,
            payload=payload,
            display_met=display_met,
            has_replenishment=bool(replenishment_ids),
        )

        self._audit(
            actor, "VISIT_RECORD", record.id, EVENT_CREATE,
            after={
                "validity": record.validity,
                "status": record.status,
                "distance_meters": distance,
                "reasons": list(verdict.reasons),
                "photo_count": record.photo_count,
                "source": record.source,
                "freezer_status": record.freezer_status,
                "display_pct": record.display_pct,
                "display_threshold_pct": threshold_pct,
                "display_met": display_met,
                "conclusion": record.conclusion,
                "sales_line_count": len(payload.sales_lines),
                "replenishment_ids": [str(i) for i in replenishment_ids],
            },
        )
        # T35：任务完成（有效巡访）即自动重算该业务员当月绩效快照
        if task.status == VisitTaskStatus.COMPLETED.value:
            self._refresh_performance(user_id=record.user_id,
                                      biz_date=on_date)
        return record

    # ================= 经理判定 =================

    def judge(self, actor: ActorContext, record_id: UUID, payload) -> VisitRecord:
        """经理判定异常巡访：认可计有效或判异常不计（留审计，§4.6）。"""
        self.permissions.require_ability(actor, MODULE_VISIT)
        if actor.role_code not in _JUDGE_ROLES:
            raise PermissionDeniedError(
                code="VISIT_JUDGE_FORBIDDEN", message="仅片区经理可判定巡访异常"
            )

        record = self.repo.get_record(record_id, for_update=True)
        if record is None:
            raise DomainError(code="VISIT_RECORD_NOT_FOUND", message="巡访记录不存在")
        merchant = self._merchant_or_404(record.merchant_id)
        self._assert_merchant_in_scope(actor, merchant)

        if record.status != VisitRecordStatus.ABNORMAL_PENDING.value:
            raise ConflictError(
                code="VISIT_NOT_PENDING_JUDGEMENT", message="仅待判定的异常巡访可判定"
            )

        now = datetime.now(UTC)
        if payload.recognize:
            record.status = VisitRecordStatus.RECOGNIZED.value
            record.validity = VALID
            new_task_status = VisitTaskStatus.COMPLETED.value
        else:
            record.status = VisitRecordStatus.ABNORMAL.value
            record.validity = INVALID
            new_task_status = VisitTaskStatus.ABNORMAL.value
        record.judged_by_id = actor.user_id
        record.judged_at = now
        record.judge_reason = payload.reason

        task = self.repo.get_task(record.visit_task_id)
        if task is not None:
            task.status = new_task_status

        self._audit(
            actor, "VISIT_RECORD", record.id, EVENT_STATE_TRANSITION,
            before={"status": VisitRecordStatus.ABNORMAL_PENDING.value},
            after={
                "status": record.status,
                "validity": record.validity,
                "recognize": payload.recognize,
            },
            reason=payload.reason,
        )
        # T35：判定为有效（任务完成）→ 自动重算该业务员当月绩效快照
        if new_task_status == VisitTaskStatus.COMPLETED.value:
            self._refresh_performance(
                user_id=record.user_id,
                biz_date=now.astimezone(ZoneInfo("Asia/Shanghai")).date(),
            )
        return record

    # ================= 绩效自动重算（T35） =================

    def _refresh_performance(self, *, user_id: UUID, biz_date: date) -> None:
        """巡访完成时重算当月绩效快照（同事务）。

        红线：绩效重算失败只记告警，绝不阻断现场打卡/判定（业务事实优先）。
        """
        from app.services.performance_snapshot_service import (
            PerformanceSnapshotService,
        )

        try:
            # 用 SAVEPOINT 隔离：绩效侧异常只回滚这一段，不影响巡访事实落库
            with self.session.begin_nested():
                PerformanceSnapshotService(self.session).refresh_month(
                    user_id, biz_date
                )
        except Exception:  # noqa: BLE001 - 绩效异常不得阻断巡访提交
            logger.warning(
                "performance auto refresh failed: user=%s date=%s",
                user_id, biz_date, exc_info=True,
            )

    # ================= 修订追加（原始证据不可覆盖） =================

    def add_revision(self, actor: ActorContext, record_id: UUID, payload) -> VisitRevision:
        self.permissions.require_ability(actor, MODULE_VISIT)
        record = self.repo.get_record(record_id)
        if record is None:
            raise DomainError(code="VISIT_RECORD_NOT_FOUND", message="巡访记录不存在")
        # 责任人本人、经理或督导可补充
        if actor.user_id != record.user_id and actor.role_code not in (
            "REGIONAL_MANAGER", "SYSTEM_ADMIN", "SUPERVISOR"
        ):
            raise PermissionDeniedError(
                code="VISIT_REVISION_FORBIDDEN", message="无权补充该巡访记录"
            )

        revision = VisitRevision(
            visit_record_id=record.id,
            author_id=actor.user_id,
            supplement=payload.supplement,
            dataset_id=record.dataset_id,
            dataset_type=record.dataset_type,
        )
        self.repo.add_revision(revision)
        self._audit(
            actor, "VISIT_RECORD", record.id, EVENT_UPDATE,
            after={"revision_id": str(revision.id), "supplement": payload.supplement},
        )
        return revision

    # ================= 读取 =================

    def get_record_detail(self, actor: ActorContext, record_id: UUID):
        self.permissions.require_ability(actor, MODULE_VISIT)
        record = self.repo.get_record(record_id)
        if record is None:
            raise DomainError(code="VISIT_RECORD_NOT_FOUND", message="巡访记录不存在")
        task = self.repo.get_task(record.visit_task_id)
        merchant = self._merchant_or_404(record.merchant_id)
        self._assert_can_read(actor, task, merchant)
        photos = self.repo.list_photos(record.id)
        revisions = self.repo.list_revisions(record.id)
        sales_lines = self.repo.list_sales_lines(record.id)
        replenishments = self.repo.list_replenishments_for_record(record.id)
        # 业务员/经理看打卡点（服务端记录的唯一坐标，供地图回溯）
        record_lng_lat = self.repo.record_lng_lat(record)
        return (
            record, photos, revisions, record_lng_lat, sales_lines, replenishments
        )

    # ================= 内部 =================

    def _assert_skus_in_scope(
        self, sku_ids: set[UUID], sku_map: dict, dataset_id: UUID
    ) -> None:
        """销量/补货行引用的 SKU 必须存在且与巡访同数据范围（演示隔离红线）。"""
        for sid in sku_ids:
            sku = sku_map.get(sid)
            if sku is None:
                raise DomainError(
                    code="VISIT_SKU_NOT_FOUND", message=f"SKU 不存在: {sid}"
                )
            if sku.dataset_id != dataset_id:
                raise PermissionDeniedError(
                    code="DATA_SCOPE_VIOLATION", message="SKU 与巡访数据范围不一致"
                )

    def _create_replenishment_draft(
        self,
        *,
        actor: ActorContext,
        record: VisitRecord,
        merchant: Merchant,
        line,
        on_date: date,
    ) -> ReplenishmentOrder:
        """巡访补货行 → 补货 DRAFT（PRD §4.9 状态链起点）。

        - status=DRAFT，committed_boxes=0（经理确认前承诺量为 0，不进正式补货口径）；
        - 回链 source_visit_record_id；承诺到货日不得早于需求日（DB 约束兜底）。
        """
        if (
            line.expected_delivery_date is not None
            and line.expected_delivery_date < on_date
        ):
            raise DomainError(
                code="VISIT_REPLENISHMENT_DATE_INVALID",
                message="期望到货日不能早于巡访日期",
            )
        order = ReplenishmentOrder(
            code=f"RD-{on_date.strftime('%Y%m%d')}-{uuid4().hex[:10]}",
            merchant_id=merchant.id,
            sku_id=line.sku_id,
            requested_boxes=Decimal(line.requested_boxes),
            committed_boxes=Decimal("0"),
            request_date=on_date,
            promised_delivery_date=line.expected_delivery_date,
            status=ReplenishmentStatus.DRAFT.value,
            source_visit_record_id=record.id,
            dataset_id=record.dataset_id,
            dataset_type=record.dataset_type,
        )
        self.repo.add_replenishment(order)
        return order

    def _create_followup_tasks(
        self,
        *,
        actor: ActorContext,
        merchant: Merchant,
        record: VisitRecord,
        payload,
        display_met: bool | None,
        has_replenishment: bool,
    ) -> None:
        """同事务生成异常/整改待办与补货经理复核待办（幂等：同记录同类型不重复）。

        - 设备异常 / 陈列低于合同阈值 / 结论需整改 → 责任业务员整改待办；
        - 存在补货草稿 → 片区经理补货复核待办（业务员不能确认自己的补货）。
        """
        needs_rectification = bool(
            (payload.device_issue and payload.device_issue.strip())
            or display_met is False
            or payload.conclusion.value == "NEEDS_RECTIFICATION"
        )
        if needs_rectification:
            self.tasks.assign(
                title=f"巡访整改：{merchant.name}",
                assignee_id=record.user_id,
                task_kind=TASK_KIND_PROBLEM_RECTIFICATION,
                related_object_type="VISIT_RECORD",
                related_object_id=record.id,
                description=self._rectification_description(payload, display_met),
            )

        if has_replenishment:
            manager = self.repo.regional_manager(
                merchant.region_id, record.dataset_id, merchant.brand_id
            )
            if manager is not None:
                self.tasks.assign(
                    title=f"补货复核：{merchant.name}",
                    assignee_id=manager.id,
                    task_kind=TASK_KIND_REPLENISHMENT_REVIEW,
                    related_object_type="VISIT_RECORD",
                    related_object_id=record.id,
                    description="巡访提报补货需求，待经理确认后进入正式补货池",
                )

    @staticmethod
    def _rectification_description(payload, display_met: bool | None) -> str:
        reasons: list[str] = []
        if payload.device_issue and payload.device_issue.strip():
            reasons.append(f"设备异常：{payload.device_issue.strip()}")
        if display_met is False:
            reasons.append("本品牌陈列比例低于合同阈值")
        return "；".join(reasons) or "巡访现场需整改"

    def _bind_photo(
        self, actor: ActorContext, record: VisitRecord, file_id: UUID, kind: str
    ) -> None:
        fobj: FileObject | None = self.repo.get_file(file_id)
        if fobj is None:
            raise DomainError(
                code="VISIT_PHOTO_NOT_FOUND", message=f"照片文件不存在: {file_id}"
            )
        if fobj.dataset_id != record.dataset_id:
            raise PermissionDeniedError(
                code="VISIT_PHOTO_SCOPE", message="照片与巡访数据范围不一致"
            )
        # 文件须为图片且已就绪（未扫描或已隔离的不得绑定）
        if not fobj.content_type.startswith("image/") or fobj.scan_status not in (
            "CLEAN", "SKIPPED"
        ):
            raise DomainError(
                code="VISIT_PHOTO_NOT_READY", message="仅可绑定已就绪的图片文件"
            )
        self.repo.add_photo(
            VisitPhoto(
                visit_record_id=record.id,
                file_id=file_id,
                photo_kind=kind,
                dataset_id=record.dataset_id,
                dataset_type=record.dataset_type,
            )
        )

    def _find_by_idempotency(self, key: str) -> VisitRecord | None:
        return self.session.scalars(
            select(VisitRecord).where(VisitRecord.idempotency_key == key)
        ).first()

    def _resolve_assignee(
        self, actor: ActorContext, assigned: UUID | None, source: VisitSource
    ) -> UUID:
        if actor.profile.row_scope == "self":
            # 业务员只能给自己排（忽略客户端指定，服务端注入）
            return actor.user_id
        if assigned is None and (
            source == VisitSource.SUPERVISION or actor.role_code == "SUPERVISOR"
        ):
            # 督导跟访默认由发起督导本人执行（不计业务员覆盖率）
            return actor.user_id
        # 经理/管理员指派他人：必须显式指定责任人
        if assigned is None:
            raise DomainError(
                code="VISIT_ASSIGNEE_REQUIRED", message="请指定巡访责任人"
            )
        return assigned

    def _merchant_or_404(self, merchant_id: UUID) -> Merchant:
        m = self.repo.get_merchant(merchant_id)
        if m is None:
            raise DomainError(code="MERCHANT_NOT_FOUND", message="商家不存在")
        return m

    def _assert_merchant_in_scope(self, actor: ActorContext, merchant: Merchant) -> None:
        scope = actor.profile.row_scope
        if scope in ("global", "global_readonly"):
            return
        if scope == "region":
            if merchant.region_id not in actor.region_ids:
                raise PermissionDeniedError(
                    code="DATA_SCOPE_VIOLATION", message="无权操作该片区的商家"
                )
            return
        # self（业务员）：责任人相关操作由任务责任人 / 主负责人校验兜底

    def _assert_can_read(
        self, actor: ActorContext, task: VisitTask | None, merchant: Merchant
    ) -> None:
        # 业务员只读自己的任务；其余走行级范围（片区/督导）
        if actor.profile.row_scope == "self" and task is not None:
            if task.assigned_user_id != actor.user_id:
                raise PermissionDeniedError(
                    code="DATA_SCOPE_VIOLATION", message="无权查看该巡访"
                )
            return
        self._assert_merchant_in_scope(actor, merchant)

    def _is_primary_owner(self, merchant_id: UUID, user_id: UUID, on_date: date) -> bool:
        """该用户在该日期是否为商家的主负责人（生效区间内，PRD 行级范围）。"""
        at = datetime.combine(on_date, time.min, tzinfo=UTC)
        point = func.tstzrange(
            func.to_timestamp(at.timestamp()),
            func.to_timestamp(at.timestamp()),
            "[]",
        )
        stmt = select(func.count()).select_from(MerchantAssignment).where(
            MerchantAssignment.merchant_id == merchant_id,
            MerchantAssignment.owner_user_id == user_id,
            MerchantAssignment.assignment_type == "PRIMARY",
            MerchantAssignment.valid_range.op("@>")(func.lower(point)),
        )
        return bool(self.session.scalar(stmt))

    def _audit(
        self,
        actor: ActorContext,
        object_type: str,
        object_id: UUID,
        event_type: str,
        *,
        before: dict | None = None,
        after: dict | None = None,
        reason: str | None = None,
    ) -> None:
        """写操作与审计同事务（§5/§8 红线）；审计只增不改。"""
        self.audit.record(
            object_type=object_type,
            object_id=object_id,
            event_type=event_type,
            actor_id=actor.user_id,
            before=before,
            after=after,
            reason=reason,
        )

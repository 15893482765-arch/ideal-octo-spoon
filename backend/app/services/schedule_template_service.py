"""每周循环排班模板（用户新增需求）。

与 T13 周排班的关系：
- 模板只描述"排给谁 + 商家 + 周几 + 时段"，不绑定日期，每周自动复用；
- 同一天可以有多家商家（不同商家），一周内同一商家只排一次；
- 生成任务时把 weekday 换算成目标业务周的具体日期，仍复用
  ScheduleService.create_draft/publish 的冲突裁决与全有或全无发布；
- 冲突、占用、幂等仍由 T13 唯一实现，本服务不重算。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.errors import ConflictError, DomainError, PermissionDeniedError
from app.core.permissions import MODULE_SCHEDULE, ActorContext, PermissionService
from app.models import Merchant, SchedulePlanBatch, ScheduleTemplate, User
from app.repositories.schedule_repository import ScheduleRepository
from app.services.audit_service import EVENT_CREATE, EVENT_UPDATE, AuditService
from app.services.schedule_service import SHANGHAI_TZ, ScheduleService

_MANAGE_ROLES = ("REGIONAL_MANAGER", "SYSTEM_ADMIN")


class ScheduleTemplateService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = ScheduleRepository(session)
        self.permissions = PermissionService()
        self.schedule = ScheduleService(session)
        self.audit = AuditService(session)

    # ----------------------------- 查询 -----------------------------

    def list_templates(
        self, actor: ActorContext, *, region_id: UUID | None = None
    ) -> list[tuple[ScheduleTemplate, Merchant, User, str | None]]:
        """模板列表；``region_id`` 为空时返回经理可见片区的全部模板（带片区名）。

        页面顶部默认只选中第一个片区，之前会把其他片区的模板“藏起来”
        （例如金水片区 3 条看不到），所以“全部可见片区”必须走这条分支。
        """
        self.permissions.require_ability(actor, MODULE_SCHEDULE)
        if region_id is not None:
            self._assert_region(actor, region_id)
            region = self._region_or_404(region_id)
            rows = self.repo.list_templates(region_id, region.dataset_id)
            return [(tpl, merchant, user, region.name) for tpl, merchant, user in rows]

        if actor.role_code == "SYSTEM_ADMIN":
            return [
                (tpl, merchant, user, region_name)
                for tpl, merchant, user, region_name
                in self.repo.list_templates_detail(None)
            ]
        return [
            (tpl, merchant, user, region_name)
            for tpl, merchant, user, region_name
            in self.repo.list_templates_detail(set(actor.region_ids or ()))
        ]

    # ----------------------------- 新增 -----------------------------

    def create_entries(
        self, actor: ActorContext, *, region_id: UUID, entries: list[dict]
    ) -> list[ScheduleTemplate]:
        """按天批量添加：同一天可多家商家，一周内同一商家只排一次。"""
        self._require_manage(actor)
        self._assert_region(actor, region_id)
        region = self._region_or_404(region_id)

        created: list[ScheduleTemplate] = []
        for item in entries:
            merchant = self.session.get(Merchant, item["merchant_id"])
            if (
                merchant is None
                or merchant.region_id != region_id
                or merchant.dataset_id != region.dataset_id
            ):
                raise DomainError(
                    code="SCHEDULE_MERCHANT_INVALID",
                    message="商家不存在或不属于该片区",
                )
            assignee = self.session.get(User, item["assigned_user_id"])
            if (
                assignee is None
                or assignee.status != "ACTIVE"
                or assignee.dataset_id != region.dataset_id
            ):
                raise DomainError(
                    code="SCHEDULE_ASSIGNEE_INVALID",
                    message="负责人不存在/在职状态异常/数据范围不一致",
                )
            if self.repo.find_template_by_merchant(region_id, merchant.id) is not None:
                raise ConflictError(
                    code="SCHEDULE_TEMPLATE_EXISTS",
                    message="该商家已在每周模板中；请先删除原模板再调整",
                    details={"merchant_id": str(merchant.id)},
                )
            if item["start_time"] >= item["end_time"]:
                raise DomainError(
                    code="SCHEDULE_TEMPLATE_WINDOW_INVALID",
                    message="结束时间必须晚于开始时间",
                )
            template = ScheduleTemplate(
                region_id=region_id,
                merchant_id=merchant.id,
                assigned_user_id=assignee.id,
                weekday=item["weekday"],
                start_time=item["start_time"],
                end_time=item["end_time"],
                priority=item.get("priority", 2),
                is_active=True,
                dataset_type=region.dataset_type,
                dataset_id=region.dataset_id,
            )
            self.repo.add_template(template)
            created.append(template)

        self.audit.record(
            object_type="SCHEDULE_TEMPLATE",
            object_id=created[0].id,
            event_type=EVENT_CREATE,
            actor_id=actor.user_id,
            after={
                "region_id": str(region_id),
                "count": len(created),
                "entries": [
                    {
                        "merchant_id": str(t.merchant_id),
                        "weekday": t.weekday,
                        "start_time": t.start_time.isoformat(),
                        "end_time": t.end_time.isoformat(),
                    }
                    for t in created
                ],
            },
            reason="新增每周循环排班模板",
        )
        return created

    # ----------------------------- 删除 -----------------------------

    def delete_entry(self, actor: ActorContext, template_id: UUID) -> None:
        self._require_manage(actor)
        template = self.repo.get_template(template_id)
        if template is None:
            raise DomainError(
                code="SCHEDULE_TEMPLATE_NOT_FOUND", message="排班模板不存在"
            )
        self._assert_region(actor, template.region_id)
        self.session.delete(template)
        self.session.flush()
        self.audit.record(
            object_type="SCHEDULE_TEMPLATE",
            object_id=template_id,
            event_type=EVENT_UPDATE,
            actor_id=actor.user_id,
            before={
                "merchant_id": str(template.merchant_id),
                "weekday": template.weekday,
            },
            reason="删除每周循环排班模板",
        )

    # ----------------------------- 历史数据回填 -----------------------------

    def sync_from_published(
        self,
        actor: ActorContext,
        *,
        region_id: UUID,
        overwrite: bool = False,
        week_start: date | None = None,
    ) -> dict:
        """把「已发布排班」回填成每周模板（每个商家取一条已发布条目）。

        - 模板 = 整周内所有业务员、所有商家的「商家 + 业务员 + 周几 + 时段」集合，
          所以历史排班过的商家/业务员需要一次性补齐，否则下周预排会缺人缺店；
        - 默认只补缺失的商家（不覆盖已手工调整过的模板）；
        - ``overwrite=True`` 时用已发布排班覆盖已有模板的业务员/周几/时段；
        - ``week_start`` 指定源业务周（页面「用本周排班覆盖模板」），为空则取历史上
          最新一周——注意最新一周可能是**未来周**（已提前生成下周排班），会与用户
          当前查看的这周不一致。
        """
        self._require_manage(actor)
        self._assert_region(actor, region_id)
        region = self._region_or_404(region_id)
        entries = self.repo.latest_published_entries(
            region_id, region.dataset_id, week_start=week_start
        )
        existing = {
            t.merchant_id: t
            for t in self.repo.list_templates_raw(region_id, region.dataset_id)
        }

        created = updated = skipped = 0
        first_touched = None
        for entry in entries:
            start_local = (
                entry.start_at.astimezone(SHANGHAI_TZ) if entry.start_at
                else datetime.combine(
                    entry.planned_date, time(9, 0), tzinfo=SHANGHAI_TZ
                )
            )
            end_local = (
                entry.end_at.astimezone(SHANGHAI_TZ) if entry.end_at
                else start_local + timedelta(minutes=30)
            )
            if end_local <= start_local:
                end_local = start_local + timedelta(minutes=30)
            start_time = start_local.time().replace(tzinfo=None)
            end_time = end_local.time().replace(tzinfo=None)
            weekday = entry.planned_date.isoweekday()

            template = existing.get(entry.merchant_id)
            if template is None:
                template = ScheduleTemplate(
                    region_id=region_id,
                    merchant_id=entry.merchant_id,
                    assigned_user_id=entry.assigned_user_id,
                    weekday=weekday,
                    start_time=start_time,
                    end_time=end_time,
                    priority=entry.priority,
                    is_active=True,
                    dataset_type=region.dataset_type,
                    dataset_id=region.dataset_id,
                )
                self.session.add(template)
                existing[entry.merchant_id] = template
                created += 1
                first_touched = first_touched or template
            elif overwrite:
                template.assigned_user_id = entry.assigned_user_id
                template.weekday = weekday
                template.start_time = start_time
                template.end_time = end_time
                template.priority = entry.priority
                template.is_active = True
                updated += 1
                first_touched = first_touched or template
            else:
                skipped += 1

        self.session.flush()
        if first_touched is not None:
            self.audit.record(
                object_type="SCHEDULE_TEMPLATE",
                object_id=first_touched.id,
                event_type=EVENT_UPDATE,
                actor_id=actor.user_id,
                after={
                    "region_id": str(region_id),
                    "created": created,
                    "updated": updated,
                    "skipped": skipped,
                    "source_entries": len(entries),
                    "overwrite": overwrite,
                },
                reason="从已发布排班回填每周模板",
            )
        return {
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "source_entries": len(entries),
        }

    # ----------------------------- 生成任务 -----------------------------

    def apply(
        self,
        actor: ActorContext,
        *,
        region_id: UUID,
        week_start: date,
        replace: bool = False,
    ) -> tuple[SchedulePlanBatch, int, int]:
        """按模板生成目标业务周的草稿并原子发布（冲突整体拒绝、零写入）。

        返回值 ``(batch, created_count, total_count)``：

        - 该周还没排班：按整份模板生成，``created_count`` = 生成条目数；
        - 该周已发布且 ``replace=False``：**增量补齐**模板里还未排入的商家，
          已排的不动（不会因为重复点击而"看起来没反应"）；
        - 该周已发布且 ``replace=True``：撤销该周已发布排班后按模板整周重排，
          若已有巡访任务开始执行则整体拒绝（SCHEDULE_WEEK_LOCKED）。
        """
        self._require_manage(actor)
        self._assert_region(actor, region_id)
        region = self._region_or_404(region_id)
        rows = self.repo.list_templates(region_id, region.dataset_id)
        if not rows:
            raise DomainError(
                code="SCHEDULE_TEMPLATE_EMPTY",
                message="该片区还没有每周排班模板，请先添加",
            )

        published = self.repo.find_published_batch(
            region_id, week_start, region.dataset_id
        )
        if published is not None:
            if not replace:
                # 增量补齐：模板里有、本周还没排的商家
                existing_merchants = {
                    e.merchant_id for e in self.repo.list_entries(published.id)
                }
                pending_rows = [
                    r for r in rows if r[0].merchant_id not in existing_merchants
                ]
                if not pending_rows:
                    return published, 0, len(existing_merchants)
                appended = self._build_entries(pending_rows, week_start)
                _batch, created = self.schedule.append_entries(
                    actor, batch_id=published.id, entries=appended
                )
                return (
                    published,
                    len(created),
                    len(existing_merchants) + len(created),
                )
            # 整周重排：先撤销（内部处理"任务已开始"的保护）
            self.schedule.reset_week_batch(actor, batch_id=published.id)

        entries = self._build_entries(rows, week_start)
        batch = self.schedule.create_draft(
            actor,
            region_id=region_id,
            week_start=week_start,
            entries=entries,
            idempotency_key=f"template-{region_id}-{week_start.isoformat()}",
        )
        published, _conflicts = self.schedule.publish(actor, batch_id=batch.id)
        return published, len(entries), len(entries)

    # ----------------------------- 内部 -----------------------------

    @staticmethod
    def _build_entries(
        rows: list[tuple[ScheduleTemplate, Merchant, User]], week_start: date
    ) -> list[dict]:
        """模板 → 目标周条目：同一业务员同一天多家商家时按 30 分钟顺延，避免自相冲突。"""
        entries: list[dict] = []
        slot_index: dict[tuple, int] = {}
        for template, _merchant, _user in rows:
            planned_date = week_start + timedelta(days=template.weekday - 1)
            key = (template.assigned_user_id, template.weekday)
            index = slot_index.get(key, 0)
            slot_index[key] = index + 1
            duration = max(
                datetime.combine(planned_date, template.end_time)
                - datetime.combine(planned_date, template.start_time),
                timedelta(minutes=30),
            )
            start_local = datetime.combine(
                planned_date, template.start_time, tzinfo=SHANGHAI_TZ
            ) + index * timedelta(minutes=30)
            end_local = start_local + duration
            entries.append(
                {
                    "merchant_id": template.merchant_id,
                    "assigned_user_id": template.assigned_user_id,
                    "planned_date": planned_date,
                    "start_at": start_local.astimezone(UTC),
                    "end_at": end_local.astimezone(UTC),
                    "priority": template.priority,
                }
            )
        return entries

    def _region_or_404(self, region_id: UUID):
        region = self.repo.get_region(region_id)
        if region is None:
            raise DomainError(code="REGION_NOT_FOUND", message="片区不存在")
        return region

    def _assert_region(self, actor: ActorContext, region_id: UUID) -> None:
        if actor.role_code == "SYSTEM_ADMIN":
            return
        if region_id not in (actor.region_ids or set()):
            raise PermissionDeniedError(
                code="REGION_FORBIDDEN", message="无权操作该片区的排班"
            )

    def _require_manage(self, actor: ActorContext) -> None:
        self.permissions.require_ability(actor, MODULE_SCHEDULE)
        if actor.role_code not in _MANAGE_ROLES:
            raise PermissionDeniedError(
                code="SCHEDULE_MANAGE_FORBIDDEN",
                message="排班模板仅片区经理/系统管理员可维护",
            )

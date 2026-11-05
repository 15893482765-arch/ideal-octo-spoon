"""排班/活动占用冲突唯一裁决服务（PRD §4.7 R6）。

这是**唯一**定义人员时间重叠与重复商家冲突口径的地方（指标只出 MetricsService、
风险只出 RiskService 同理）：任何 Service 不得自行重算区间重叠。

- 时间占用采用半开区间 ``[start_at, end_at)``：上一场 09:00–10:00 与下一场 10:00–11:00
  **不冲突**（端点相接）；
- 返回**全部**冲突（不做 fail-fast），供批量发布一次性提示；
- T13 只消费通用占用源 ResourceReservation（SCHEDULE）；T15 地推活动发布时写入
  CAMPAIGN 占用，本服务无需改动即可对活动避让。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ResourceReservation

ConflictType = Literal["TIME_OVERLAP", "DUPLICATE_MERCHANT"]


@dataclass(frozen=True)
class Occupancy:
    """一次待校验的人员占用（候选排班项）。"""

    key: str  # 候选标识（通常是条目 id 或临时序号），回传给调用方定位
    resource_user_id: UUID
    start_at: datetime
    end_at: datetime
    merchant_id: UUID | None = None
    planned_date: object | None = None  # 仅重复商家按日匹配时使用（date）


@dataclass(frozen=True)
class Conflict:
    """一条冲突。left/right 为候选 key；与既有占用冲突时 right 为既有占用 id。"""

    type: ConflictType
    left_key: str
    right_key: str
    resource_user_id: UUID
    message: str

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "left_key": self.left_key,
            "right_key": self.right_key,
            "resource_user_id": str(self.resource_user_id),
            "message": self.message,
        }


def overlaps_half_open(a_start, a_end, b_start, b_end) -> bool:
    """半开区间 [a_start,a_end) 与 [b_start,b_end) 是否重叠。

    端点相接（a_end == b_start）返回 False。
    """
    return a_start < b_end and b_start < a_end


class ScheduleConflictService:
    """候选占用 vs 彼此 / 既有 ResourceReservation 的冲突检测。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def detect(
        self,
        candidates: list[Occupancy],
        *,
        dataset_id: UUID,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        exclude_reservation_ids: set[UUID] | None = None,
    ) -> list[Conflict]:
        """返回全部冲突（候选项两两冲突 + 候选与既有占用冲突）。

        :param window_start/window_end: 查询既有占用的时间窗（半开）；默认取候选并集。
        :param exclude_reservation_ids: 改期时排除条目自身的那条占用，避免自己和自己冲突。
        """
        conflicts: list[Conflict] = []
        if not candidates:
            return conflicts

        # 1) 候选项彼此之间（批内）：时间重叠 + 同人同商家同日
        conflicts.extend(self._conflicts_within_batch(candidates))

        # 2) 候选项与既有 ACTIVE 占用：时间重叠 + 同人同商家同日
        existing = self._load_existing(
            candidates, dataset_id, window_start, window_end,
            exclude_reservation_ids=exclude_reservation_ids,
        )
        conflicts.extend(self._conflicts_against_existing(candidates, existing))

        # 去重（同一对 key + 类型可能被两个候选各触发一次），保序
        seen: set[tuple] = set()
        unique: list[Conflict] = []
        for c in conflicts:
            sig = (c.type, tuple(sorted((c.left_key, c.right_key))), c.resource_user_id)
            if sig not in seen:
                seen.add(sig)
                unique.append(c)
        return unique

    # ---------------- 内部 ----------------

    def _conflicts_within_batch(self, candidates: list[Occupancy]) -> list[Conflict]:
        out: list[Conflict] = []
        for i in range(len(candidates)):
            for j in range(i + 1, len(candidates)):
                a, b = candidates[i], candidates[j]
                if a.resource_user_id != b.resource_user_id:
                    continue
                if overlaps_half_open(a.start_at, a.end_at, b.start_at, b.end_at):
                    out.append(
                        Conflict(
                            type="TIME_OVERLAP",
                            left_key=a.key, right_key=b.key,
                            resource_user_id=a.resource_user_id,
                            message="同一业务员时间占用重叠（半开区间）",
                        )
                    )
                if self._same_merchant_day(a, b):
                    out.append(
                        Conflict(
                            type="DUPLICATE_MERCHANT",
                            left_key=a.key, right_key=b.key,
                            resource_user_id=a.resource_user_id,
                            message="同一业务员在同一天被重复安排同一商家",
                        )
                    )
        return out

    def _conflicts_against_existing(
        self, candidates: list[Occupancy], existing: list[ResourceReservation]
    ) -> list[Conflict]:
        out: list[Conflict] = []
        for c in candidates:
            for r in existing:
                if c.resource_user_id != r.resource_user_id:
                    continue
                if overlaps_half_open(c.start_at, c.end_at, r.start_at, r.end_at):
                    out.append(
                        Conflict(
                            type="TIME_OVERLAP",
                            left_key=c.key, right_key=str(r.id),
                            resource_user_id=c.resource_user_id,
                            message=f"与既有占用 {r.source_type.lower()} 时间重叠",
                        )
                    )
                if self._same_merchant_day(
                    c,
                    Occupancy(
                        key=str(r.id),
                        resource_user_id=r.resource_user_id,
                        start_at=r.start_at,
                        end_at=r.end_at,
                        merchant_id=r.merchant_id,
                        planned_date=r.start_at.date(),
                    ),
                ):
                    out.append(
                        Conflict(
                            type="DUPLICATE_MERCHANT",
                            left_key=c.key, right_key=str(r.id),
                            resource_user_id=c.resource_user_id,
                            message="与既有占用在同一天重复安排同一商家",
                        )
                    )
        return out

    @staticmethod
    def _same_merchant_day(a: Occupancy, b: Occupancy) -> bool:
        if not a.merchant_id or not b.merchant_id:
            return False
        if a.merchant_id != b.merchant_id:
            return False
        # 优先按显式 planned_date（业务日，Asia/Shanghai），否则退化到区间起点日期
        a_day = a.planned_date or a.start_at.date()
        b_day = b.planned_date or b.start_at.date()
        return a_day == b_day

    def _load_existing(
        self,
        candidates: list[Occupancy],
        dataset_id: UUID,
        window_start: datetime | None,
        window_end: datetime | None,
        *,
        exclude_reservation_ids: set[UUID] | None = None,
    ) -> list[ResourceReservation]:
        user_ids = {c.resource_user_id for c in candidates}
        if not user_ids:
            return []
        if window_start is None:
            window_start = min(c.start_at for c in candidates)
        if window_end is None:
            window_end = max(c.end_at for c in candidates)
        stmt = (
            select(ResourceReservation)
            .where(ResourceReservation.dataset_id == dataset_id)
            .where(ResourceReservation.status == "ACTIVE")
            .where(ResourceReservation.resource_user_id.in_(user_ids))
            # 半开区间相交：start_at < window_end AND end_at > window_start
            .where(ResourceReservation.start_at < window_end)
            .where(ResourceReservation.end_at > window_start)
        )
        if exclude_reservation_ids:
            stmt = stmt.where(ResourceReservation.id.notin_(exclude_reservation_ids))
        return list(self.session.scalars(stmt).all())

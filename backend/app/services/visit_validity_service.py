"""VisitValidityService —— 有效巡访的**唯一**裁决者（PRD §4.6、指标定义）。

任何代码（指标覆盖率、绩效、看板）都不得绕开本服务自行重算"是否有效"。

有效（VALID）需**同时**满足：
1. 责任授权：提交者就是该巡访任务的责任人（授权校验在 Service 层完成，未授权不进入裁决）；
2. 定位：有 PostGIS 打卡坐标；
3. 到店时间：有 checkin_at，且落在任务计划时间窗允许范围内（当次实际到店）；
4. 照片：至少绑定一张现场照片；
5. 距离阈值：服务端用 PostGIS ST_Distance 重算的距离 ≤ 阈值（默认 200 米，可配置）。

- 缺照片 / 定位超阈值 → ABNORMAL_PENDING（待经理判定），**判定前不计绩效/覆盖率**；
- 完全缺定位 / 无到店时间 → INVALID；
- 经理可把异常判定为 RECOGNIZED（认可有效）或 ABNORMAL（不计），判定留审计。

前端上报距离一律不作数；督导跟访（SUPERVISION）不进入业务员覆盖率（在指标侧按
source 排除），其记录有效性仍可由本服务裁决留痕。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.core.config import get_settings
from app.models import VisitRecord, VisitTask

VALID = "VALID"
ABNORMAL_PENDING = "ABNORMAL_PENDING"
INVALID = "INVALID"


@dataclass(frozen=True)
class ValidityVerdict:
    validity: str
    distance_meters: float | None
    reasons: tuple[str, ...]

    @property
    def is_valid(self) -> bool:
        return self.validity == VALID

    @property
    def needs_manager_judgement(self) -> bool:
        return self.validity == ABNORMAL_PENDING


class VisitValidityService:
    """无状态裁决：输入任务/记录与服务端事实，产出有效性结论。"""

    def evaluate(
        self,
        *,
        task: VisitTask,
        location_present: bool,
        checkin_at: datetime | None,
        photo_count: int,
        distance_meters: float | None,
        threshold_meters: int | None = None,
    ) -> ValidityVerdict:
        threshold = (
            threshold_meters
            if threshold_meters is not None
            else get_settings().visit_geofence_meters
        )

        hard_fails: list[str] = []   # 无法补救 → INVALID
        soft_fails: list[str] = []   # 可由经理判定 → ABNORMAL_PENDING

        # 2. 定位
        if not location_present or distance_meters is None:
            hard_fails.append("NO_LOCATION")
        else:
            # 5. 距离阈值（服务端重算，非前端）
            if distance_meters > threshold:
                soft_fails.append("OUT_OF_GEOFENCE")

        # 3. 到店时间
        if checkin_at is None:
            hard_fails.append("NO_CHECKIN_TIME")

        # 4. 照片
        if photo_count <= 0:
            soft_fails.append("NO_PHOTO")

        # 1. 责任授权在 VisitService.submit 前置校验（非责任人 403），此处不重复。
        if hard_fails:
            return ValidityVerdict(INVALID, distance_meters, tuple(hard_fails + soft_fails))
        if soft_fails:
            return ValidityVerdict(
                ABNORMAL_PENDING, distance_meters, tuple(soft_fails)
            )
        return ValidityVerdict(VALID, distance_meters, ())

    @staticmethod
    def is_supervision(record: VisitRecord) -> bool:
        """督导跟访标记：不进入业务员覆盖率（PRD §4.8）。"""
        return record.source == "SUPERVISION"

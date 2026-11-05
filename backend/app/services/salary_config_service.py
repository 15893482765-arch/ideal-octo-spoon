"""薪资配置（T35）：按员工维护月薪与生效日期，支持历史版本。

规则：
- 一份配置 = 「员工 + 生效起日 + 底薪（整数分）」；``(user_id, effective_from)``
  唯一，同一起效日重复录入直接 409；
- **历史版本**：只追加新版本，不回写已发工资单；解析一律“取生效日 <= 目标日
  的最新一条”，因此新增版本天然覆盖之后的月份；
- 停用（``is_active=false``）用于录错撤回：停用后该条不再参与解析，但历史工资
  单上已固化的底薪不变（工资单自带 base_salary_fen 快照）；
- 只有具备 ``payroll`` 能力的财务/管理员可维护，系统管理员不因此获得发放权。
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, DomainError, PermissionDeniedError
from app.core.permissions import MODULE_PAYROLL, ActorContext, PermissionService
from app.models import User
from app.models.payroll import SalaryConfig
from app.services.audit_service import EVENT_CREATE, EVENT_UPDATE, AuditService

_DATASET_TYPE = "PRODUCTION"


class SalaryConfigService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.permissions = PermissionService()
        self.audit = AuditService(session)

    # ============================ 维护 ============================

    def create_config(
        self,
        actor: ActorContext,
        *,
        user_id: UUID,
        base_salary_fen: int,
        effective_from: date,
        effective_to: date | None = None,
        note: str | None = None,
    ) -> SalaryConfig:
        self._require_payroll(actor)
        ds_id = self._dataset_id(actor)
        target = self.session.get(User, user_id)
        if target is None or target.dataset_id != ds_id:
            raise DomainError(
                code="SALARY_CONFIG_USER_INVALID",
                message="薪资配置对象不存在或不在当前数据域",
            )
        if base_salary_fen < 0:
            raise DomainError(
                code="SALARY_CONFIG_BASE_INVALID", message="月薪必须为非负整数分"
            )
        if effective_to is not None and effective_to < effective_from:
            raise DomainError(
                code="SALARY_CONFIG_RANGE_INVALID",
                message="生效结束日不得早于生效起始日",
            )
        existed = self.session.scalar(
            select(SalaryConfig).where(
                SalaryConfig.user_id == user_id,
                SalaryConfig.effective_from == effective_from,
            )
        )
        if existed is not None:
            raise ConflictError(
                code="SALARY_CONFIG_DUPLICATED",
                message="该员工在此生效日期已配置月薪，请调整日期或停用旧配置",
                details={"effective_from": effective_from.isoformat()},
            )

        config = SalaryConfig(
            user_id=user_id,
            base_salary_fen=base_salary_fen,
            effective_from=effective_from,
            effective_to=effective_to,
            is_active=True,
            note=note,
            dataset_type=_DATASET_TYPE,
            dataset_id=ds_id,
            created_by_id=actor.user_id,
        )
        self.session.add(config)
        self.session.flush()
        self.audit.record(
            object_type="SALARY_CONFIG",
            object_id=config.id,
            event_type=EVENT_CREATE,
            actor_id=actor.user_id,
            after={
                "user_id": str(user_id),
                "base_salary_fen": base_salary_fen,
                "effective_from": effective_from.isoformat(),
                "effective_to": (
                    effective_to.isoformat() if effective_to is not None else None
                ),
            },
            reason=note,
        )
        return config

    def toggle_active(
        self, actor: ActorContext, config_id: UUID
    ) -> SalaryConfig:
        """停用/启用一条配置（历史工资单不受影响）。"""
        self._require_payroll(actor)
        config = self._load(config_id, self._dataset_id(actor))
        before = config.is_active
        config.is_active = not config.is_active
        self.session.flush()
        self.audit.record(
            object_type="SALARY_CONFIG",
            object_id=config.id,
            event_type=EVENT_UPDATE,
            actor_id=actor.user_id,
            before={"is_active": before},
            after={"is_active": config.is_active},
        )
        return config

    # ============================ 查询 ============================

    def list_configs(
        self,
        actor: ActorContext,
        *,
        user_id: UUID | None = None,
        only_active: bool = False,
        limit: int = 200,
    ) -> list[tuple[SalaryConfig, str | None]]:
        self._require_payroll(actor)
        stmt = select(SalaryConfig, User.display_name).join(
            User, User.id == SalaryConfig.user_id
        ).where(
            SalaryConfig.dataset_type == _DATASET_TYPE,
            SalaryConfig.dataset_id == self._dataset_id(actor),
        )
        if user_id is not None:
            stmt = stmt.where(SalaryConfig.user_id == user_id)
        if only_active:
            stmt = stmt.where(SalaryConfig.is_active.is_(True))
        stmt = stmt.order_by(
            SalaryConfig.effective_from.desc(), SalaryConfig.created_at.desc()
        ).limit(max(1, min(int(limit), 500)))
        return [(row[0], row[1]) for row in self.session.execute(stmt).all()]

    def effective_for_month(
        self, actor: ActorContext, *, user_id: UUID, period_month: str
    ) -> SalaryConfig | None:
        """工资生成表单自动带出：返回该员工该月生效配置（无则 None）。"""
        self._require_payroll(actor)
        ds_id = self._dataset_id(actor)
        target = self.session.get(User, user_id)
        if target is None or target.dataset_id != ds_id:
            raise DomainError(
                code="SALARY_CONFIG_USER_INVALID",
                message="薪资配置对象不存在或不在当前数据域",
            )
        try:
            year, month = (int(part) for part in period_month.split("-"))
            on_date = date(year, month, 1)
        except (ValueError, TypeError) as exc:
            raise DomainError(
                code="SALARY_CONFIG_PERIOD_INVALID",
                message="period_month 必须为 YYYY-MM 格式",
            ) from exc
        return self.resolve_config(user_id, on_date=on_date, dataset_id=ds_id)

    def resolve_config(
        self, user_id: UUID, *, on_date: date, dataset_id: UUID
    ) -> SalaryConfig | None:
        """解析生效配置：effective_from <= on_date 且（无结束日或未过期）的最新一条。"""
        return self.session.scalars(
            select(SalaryConfig)
            .where(
                SalaryConfig.user_id == user_id,
                SalaryConfig.dataset_type == _DATASET_TYPE,
                SalaryConfig.dataset_id == dataset_id,
                SalaryConfig.is_active.is_(True),
                SalaryConfig.effective_from <= on_date,
                (SalaryConfig.effective_to.is_(None))
                | (SalaryConfig.effective_to >= on_date),
            )
            .order_by(SalaryConfig.effective_from.desc())
            .limit(1)
        ).first()

    # ============================ 内部 ============================

    def _load(self, config_id: UUID, ds_id: UUID) -> SalaryConfig:
        config = self.session.get(SalaryConfig, config_id)
        if config is None:
            raise DomainError(
                code="SALARY_CONFIG_NOT_FOUND", message="薪资配置不存在"
            )
        if config.dataset_type != _DATASET_TYPE or config.dataset_id != ds_id:
            raise PermissionDeniedError(
                code="SALARY_CONFIG_DATASET_FORBIDDEN",
                message="无权访问其他数据域的薪资配置",
            )
        return config

    def _require_payroll(self, actor: ActorContext) -> None:
        if not self.permissions.has_ability(actor, MODULE_PAYROLL):
            raise PermissionDeniedError(
                code="SALARY_CONFIG_FORBIDDEN",
                message="仅财务可维护薪资配置",
                details={"actor_id": str(actor.user_id)},
            )

    def _dataset_id(self, actor: ActorContext) -> UUID:
        user = self.session.get(User, actor.user_id)
        if user is None:
            raise DomainError(code="USER_NOT_FOUND", message="登录用户不存在")
        return user.dataset_id

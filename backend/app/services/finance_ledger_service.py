"""财务流水服务（T35：金额只记流水，不做在线支付）。

口径：
- 交易全部线下完成，系统只保留**流水**：收入 / 支出 / 调整（tx_type），
  金额一律整数分，支出以负数入账，净额 = 收入 − 支出（+ 调整代数和）；
- 每笔流水必须能回溯业务单据（related_object_type + related_object_id），
  并由业务服务的状态迁移点在同一事务内写入：业务成功才有账，业务回滚账也没；
- **幂等**：唯一索引 (related_object_type, related_object_id, tx_type,
  source_kind) + 冲突回读，重复点按/重试不会产生第二笔；
- 本服务只记账，不提供任何支付/退款接口：`pay` 仅表示“线下已发放，登记流水”。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import Integer, cast, extract, func, literal, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import DomainError
from app.core.logging import get_logger
from app.core.permissions import MODULE_FINANCE, ActorContext, PermissionService
from app.models import FinanceTransaction, User
from app.models.finance import (
    SOURCE_EXPENSE_ASSET,
    SOURCE_EXPENSE_COMPANY,
    SOURCE_EXPENSE_OPEX,
    SOURCE_EXPENSE_OTHER,
    SOURCE_EXPENSE_PAYROLL,
    SOURCE_EXPENSE_PROCUREMENT,
    SOURCE_INCOME_SALES,
)
from app.services.audit_service import EVENT_CREATE, AuditService

logger = get_logger(__name__)

TX_INCOME = "INCOME"
TX_EXPENSE = "EXPENSE"
TX_ADJUSTMENT = "ADJUSTMENT"

# 来源中文名（前端筛选与列表展示统一取这里，避免两端各写一份文案）
SOURCE_KIND_LABELS: dict[str, str] = {
    SOURCE_INCOME_SALES: "销售收款",
    SOURCE_EXPENSE_PROCUREMENT: "采购进货",
    SOURCE_EXPENSE_PAYROLL: "工资发放",
    SOURCE_EXPENSE_ASSET: "资产购置",
    SOURCE_EXPENSE_COMPANY: "公司物品采购",
    SOURCE_EXPENSE_OPEX: "运营费用",
    SOURCE_EXPENSE_OTHER: "其他支出",
    "RENT": "房租",
    "COMMISSION": "佣金",
}

_MAX_NOTE_LEN = 255


@dataclass(frozen=True)
class LedgerBucket:
    """分桶收支（按月或按年）。"""

    period: str
    income_fen: int
    expense_fen: int
    net_fen: int


@dataclass(frozen=True)
class LedgerSummary:
    """总账汇总：三张指标卡 + 分桶明细。"""

    granularity: str
    year: int
    month: int | None
    income_fen: int
    expense_fen: int
    net_fen: int
    buckets: tuple[LedgerBucket, ...]


class FinanceLedgerService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.permissions = PermissionService()
        self.audit = AuditService(session)

    # ============================ 记账 ============================

    def record_income(
        self,
        actor: ActorContext,
        *,
        biz_date: date,
        amount_fen: int,
        source_kind: str = SOURCE_INCOME_SALES,
        related_type: str,
        related_id: UUID,
        note: str | None = None,
        dataset_type: str | None = None,
        dataset_id: UUID | None = None,
    ) -> FinanceTransaction | None:
        """登记一笔收入（正数入账）；金额为 0 时不记账（无实际流水）。"""
        return self._record(
            actor,
            biz_date=biz_date,
            signed_amount_fen=abs(int(amount_fen)),
            tx_type=TX_INCOME,
            source_kind=source_kind,
            related_type=related_type,
            related_id=related_id,
            note=note,
            dataset_type=dataset_type,
            dataset_id=dataset_id,
        )

    def record_expense(
        self,
        actor: ActorContext,
        *,
        biz_date: date,
        amount_fen: int,
        source_kind: str,
        related_type: str,
        related_id: UUID,
        note: str | None = None,
        dataset_type: str | None = None,
        dataset_id: UUID | None = None,
    ) -> FinanceTransaction | None:
        """登记一笔支出（负数入账，便于流水代数和直接得净额）。"""
        return self._record(
            actor,
            biz_date=biz_date,
            signed_amount_fen=-abs(int(amount_fen)),
            tx_type=TX_EXPENSE,
            source_kind=source_kind,
            related_type=related_type,
            related_id=related_id,
            note=note,
            dataset_type=dataset_type,
            dataset_id=dataset_id,
        )

    def record_adjustment(
        self,
        actor: ActorContext,
        *,
        biz_date: date,
        amount_fen: int,
        source_kind: str,
        related_type: str,
        related_id: UUID,
        note: str | None = None,
        dataset_type: str | None = None,
        dataset_id: UUID | None = None,
    ) -> FinanceTransaction | None:
        """登记一笔调整（金额带符号，不得为 0）。"""
        return self._record(
            actor,
            biz_date=biz_date,
            signed_amount_fen=int(amount_fen),
            tx_type=TX_ADJUSTMENT,
            source_kind=source_kind,
            related_type=related_type,
            related_id=related_id,
            note=note,
            dataset_type=dataset_type,
            dataset_id=dataset_id,
        )

    def _record(
        self,
        actor: ActorContext,
        *,
        biz_date: date,
        signed_amount_fen: int,
        tx_type: str,
        source_kind: str,
        related_type: str,
        related_id: UUID,
        note: str | None,
        dataset_type: str | None,
        dataset_id: UUID | None,
    ) -> FinanceTransaction | None:
        if signed_amount_fen == 0:
            # 0 元流水对总账无意义，且模型有 amount_fen <> 0 约束：直接跳过
            return None
        if not related_type or len(related_type) > 32:
            raise DomainError(
                code="FINANCE_SOURCE_INVALID",
                message="流水必须关联业务单据类型（≤32 字符）",
            )
        note_text = (note or "").strip()[:_MAX_NOTE_LEN] or None

        existing = self._find(source_kind, tx_type, related_type, related_id)
        if existing is not None:
            return existing

        ds_type, ds_id = self._resolve_dataset(actor, dataset_type, dataset_id)
        row = FinanceTransaction(
            biz_date=biz_date,
            tx_type=tx_type,
            source_kind=source_kind,
            amount_fen=signed_amount_fen,
            related_object_type=related_type,
            related_object_id=related_id,
            note=note_text,
            bookkeeper_id=actor.user_id,
            created_by_id=actor.user_id,
            dataset_type=ds_type,
            dataset_id=ds_id,
        )
        try:
            # 唯一索引兜底：并发/重试时用 SAVEPOINT 隔离，冲突只回滚这一笔
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError:
            existing = self._find(source_kind, tx_type, related_type, related_id)
            if existing is None:
                # 非本幂等键的约束错误（FK/CHECK 等）必须抛出，不能静默吞掉
                raise
            logger.info(
                "finance_ledger idempotent hit: source=%s related=%s:%s",
                source_kind,
                related_type,
                related_id,
            )
            return existing

        self.audit.record(
            object_type="FINANCE_TRANSACTION",
            object_id=row.id,
            event_type=EVENT_CREATE,
            actor_id=actor.user_id,
            after={
                "tx_type": tx_type,
                "source_kind": source_kind,
                "amount_fen": signed_amount_fen,
                "biz_date": biz_date.isoformat(),
                "related_object_type": related_type,
                "related_object_id": str(related_id),
            },
            reason=note_text,
        )
        return row

    def _find(
        self, source_kind: str, tx_type: str, related_type: str, related_id: UUID
    ) -> FinanceTransaction | None:
        return self.session.execute(
            select(FinanceTransaction)
            .where(
                FinanceTransaction.source_kind == source_kind,
                FinanceTransaction.tx_type == tx_type,
                FinanceTransaction.related_object_type == related_type,
                FinanceTransaction.related_object_id == related_id,
            )
            .limit(1)
        ).scalar_one_or_none()

    # ============================ 查询 ============================

    def summary(
        self,
        actor: ActorContext,
        *,
        granularity: str = "month",
        year: int,
        month: int | None = None,
    ) -> LedgerSummary:
        """总账汇总：月口径给 12 个月分桶，年口径给最近 5 个年分桶。"""
        self.permissions.require_ability(actor, MODULE_FINANCE)
        if granularity not in ("month", "year"):
            raise DomainError(
                code="FINANCE_GRANULARITY_INVALID",
                message="统计口径只能按月(month)或按年(year)",
            )
        if not 2000 <= year <= 2100:
            raise DomainError(
                code="FINANCE_YEAR_INVALID", message="统计年份超出合理范围"
            )
        if granularity == "month":
            if month is None or not 1 <= month <= 12:
                raise DomainError(
                    code="FINANCE_MONTH_INVALID", message="按月统计必须给出 1-12 的月份"
                )
            start, end = date(year, 1, 1), date(year, 12, 31)
            labels = [f"{year}-{m:02d}" for m in range(1, 13)]
            year_labels = [year] * 12
            month_labels: list[int | None] = list(range(1, 13))
        else:
            start, end = date(year - 4, 1, 1), date(year, 12, 31)
            labels = [str(y) for y in range(year - 4, year + 1)]
            year_labels = list(range(year - 4, year + 1))
            month_labels = [None] * 5

        rows = self._aggregate(actor, start=start, end=end, by_month=granularity == "month")
        # (year, month|None) -> {tx_type: sum}
        index: dict[tuple[int, int | None], dict[str, int]] = {}
        for row_year, row_month, tx_type, total in rows:
            key = (int(row_year), int(row_month) if row_month is not None else None)
            index.setdefault(key, {})[tx_type] = int(total or 0)

        buckets: list[LedgerBucket] = []
        for label, bucket_year, bucket_month in zip(
            labels, year_labels, month_labels, strict=True
        ):
            amounts = index.get((bucket_year, bucket_month), {})
            income = amounts.get(TX_INCOME, 0)
            expense = -amounts.get(TX_EXPENSE, 0)
            adjustment = amounts.get(TX_ADJUSTMENT, 0)
            buckets.append(
                LedgerBucket(
                    period=label,
                    income_fen=income,
                    expense_fen=expense,
                    net_fen=income - expense + adjustment,
                )
            )

        return LedgerSummary(
            granularity=granularity,
            year=year,
            month=month if granularity == "month" else None,
            income_fen=sum(b.income_fen for b in buckets),
            expense_fen=sum(b.expense_fen for b in buckets),
            net_fen=sum(b.net_fen for b in buckets),
            buckets=tuple(buckets),
        )

    def list_transactions(
        self,
        actor: ActorContext,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
        tx_type: str | None = None,
        source_kind: str | None = None,
        limit: int = 200,
    ) -> list[FinanceTransaction]:
        """流水明细（按日期倒序），支持类型/来源/区间筛选。"""
        self.permissions.require_ability(actor, MODULE_FINANCE)
        if date_from is not None and date_to is not None and date_from > date_to:
            raise DomainError(
                code="FINANCE_DATE_RANGE_INVALID", message="开始日期不得晚于结束日期"
            )
        stmt = select(FinanceTransaction).where(
            FinanceTransaction.dataset_id == self._dataset_id(actor)
        )
        if date_from is not None:
            stmt = stmt.where(FinanceTransaction.biz_date >= date_from)
        if date_to is not None:
            stmt = stmt.where(FinanceTransaction.biz_date <= date_to)
        if tx_type:
            stmt = stmt.where(FinanceTransaction.tx_type == tx_type)
        if source_kind:
            stmt = stmt.where(FinanceTransaction.source_kind == source_kind)
        stmt = stmt.order_by(
            FinanceTransaction.biz_date.desc(), FinanceTransaction.created_at.desc()
        ).limit(max(1, min(int(limit), 500)))
        return list(self.session.execute(stmt).scalars().all())

    # ============================ 内部 ============================

    def _aggregate(
        self, actor: ActorContext, *, start: date, end: date, by_month: bool
    ) -> list[Any]:
        year_expr = cast(extract("year", FinanceTransaction.biz_date), Integer)
        month_expr = cast(extract("month", FinanceTransaction.biz_date), Integer)
        group_cols: list[Any] = [year_expr]
        select_cols: list[Any] = [year_expr.label("y")]
        if by_month:
            group_cols.append(month_expr)
            select_cols.append(month_expr.label("m"))
        else:
            select_cols.append(literal(None, Integer).label("m"))
        stmt = (
            select(*select_cols, FinanceTransaction.tx_type,
                   func.sum(FinanceTransaction.amount_fen))
            .where(
                FinanceTransaction.dataset_id == self._dataset_id(actor),
                FinanceTransaction.biz_date >= start,
                FinanceTransaction.biz_date <= end,
            )
            .group_by(*group_cols, FinanceTransaction.tx_type)
        )
        return [tuple(r) for r in self.session.execute(stmt).all()]

    def _resolve_dataset(
        self, actor: ActorContext, dataset_type: str | None, dataset_id: UUID | None
    ) -> tuple[str, UUID]:
        if dataset_type is not None and dataset_id is not None:
            return dataset_type, dataset_id
        if (dataset_type is None) != (dataset_id is None):
            raise DomainError(
                code="FINANCE_DATASET_INCOMPLETE",
                message="数据域类型与 id 必须同时提供或同时为空",
            )
        return "PRODUCTION", self._dataset_id(actor)

    def _dataset_id(self, actor: ActorContext) -> UUID:
        user = self.session.get(User, actor.user_id)
        if user is None:
            raise DomainError(code="USER_NOT_FOUND", message="登录用户不存在")
        return user.dataset_id

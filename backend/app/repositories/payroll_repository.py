"""T20 费用 / 工资单持久化（PRD §4.10、§6.5）。

只做持久化；同数据集隔离、审批依据校验、金额合计全部在 Service。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.approval import ApprovalRequest
from app.models.enums import ApprovalStatus
from app.models.file_object import FileObject
from app.models.payroll import (
    Expense,
    PayrollAdjustment,
    PayrollLine,
    PayrollStatement,
)
from app.models.performance import PerformanceMonthlySnapshot
from app.models.user import User


class PayrollRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ----------------------------- 凭证 -----------------------------

    def get_proof_file(self, file_id: UUID) -> FileObject | None:
        return self.session.get(FileObject, file_id)

    # ----------------------------- 审批依据 -----------------------------

    def get_approval(self, approval_id: UUID) -> ApprovalRequest | None:
        return self.session.get(ApprovalRequest, approval_id)

    def approved_approval(self, approval_id: UUID) -> ApprovalRequest | None:
        ap = self.get_approval(approval_id)
        if ap is None or ap.status != ApprovalStatus.APPROVED.value:
            return None
        return ap

    # ----------------------------- 费用 -----------------------------

    def get_expense(self, expense_id: UUID) -> Expense | None:
        return self.session.get(Expense, expense_id)

    def approved_expenses_in_month(
        self, period_month: str, dataset_type: str, dataset_id: UUID,
    ) -> list[Expense]:
        """已审批费用（进损益口径）：仅 APPROVED + 同数据集 + 归属月。"""
        return list(self.session.scalars(
            select(Expense).where(
                Expense.dataset_type == dataset_type,
                Expense.dataset_id == dataset_id,
                Expense.period_month == period_month,
                Expense.status == "APPROVED",
            ).order_by(Expense.incurred_on, Expense.expense_no)
        ).all())

    # ----------------------------- T19 绩效快照 -----------------------------

    def reviewed_snapshot_for_user(
        self, user_id: UUID, period_month: str, dataset_type: str,
        dataset_id: UUID,
    ) -> PerformanceMonthlySnapshot | None:
        """用户某月**已财务复核**（REVIEWED）且同数据集的 T19 绩效快照。"""
        return self.session.scalar(
            select(PerformanceMonthlySnapshot).where(
                PerformanceMonthlySnapshot.dataset_type == dataset_type,
                PerformanceMonthlySnapshot.dataset_id == dataset_id,
                PerformanceMonthlySnapshot.user_id == user_id,
                PerformanceMonthlySnapshot.period_month == period_month,
                PerformanceMonthlySnapshot.status == "REVIEWED",
            )
        )

    def latest_snapshot_for_user(
        self, user_id: UUID, period_month: str, dataset_type: str,
        dataset_id: UUID,
    ) -> PerformanceMonthlySnapshot | None:
        """用户某月最新绩效快照（T35：含未复核，工资草稿自动取用）。

        同一用户同月只应有一份快照；按冻结时间倒序兜底取最新一份，
        避免历史数据里出现多条时抛 MultipleResultsFound。
        """
        return self.session.scalars(
            select(PerformanceMonthlySnapshot).where(
                PerformanceMonthlySnapshot.dataset_type == dataset_type,
                PerformanceMonthlySnapshot.dataset_id == dataset_id,
                PerformanceMonthlySnapshot.user_id == user_id,
                PerformanceMonthlySnapshot.period_month == period_month,
            ).order_by(
                PerformanceMonthlySnapshot.frozen_at.desc().nullslast(),
                PerformanceMonthlySnapshot.created_at.desc(),
            ).limit(1)
        ).first()

    # ----------------------------- 工资单 -----------------------------

    def get_statement(self, statement_id: UUID) -> PayrollStatement | None:
        return self.session.get(PayrollStatement, statement_id)

    def find_statement(
        self, user_id: UUID, period_month: str, dataset_type: str,
        dataset_id: UUID,
    ) -> PayrollStatement | None:
        return self.session.scalar(
            select(PayrollStatement).where(
                PayrollStatement.dataset_type == dataset_type,
                PayrollStatement.dataset_id == dataset_id,
                PayrollStatement.user_id == user_id,
                PayrollStatement.period_month == period_month,
            )
        )

    def list_statements(
        self,
        *,
        dataset_id: UUID,
        period_month: str | None = None,
        status: str | None = None,
        user_id: UUID | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[tuple[PayrollStatement, str]], int]:
        """工资单列表（含员工姓名），按月份、单号倒序；返回 (rows, total)。"""
        conditions = [PayrollStatement.dataset_id == dataset_id]
        if period_month:
            conditions.append(PayrollStatement.period_month == period_month)
        if status:
            conditions.append(PayrollStatement.status == status)
        if user_id:
            conditions.append(PayrollStatement.user_id == user_id)
        rows = self.session.execute(
            select(PayrollStatement, User.display_name)
            .join(User, User.id == PayrollStatement.user_id)
            .where(*conditions)
            .order_by(
                PayrollStatement.period_month.desc(),
                PayrollStatement.statement_no.desc(),
            )
            .offset(offset)
            .limit(limit)
        ).all()
        total = int(
            self.session.execute(
                select(func.count())
                .select_from(PayrollStatement)
                .where(*conditions)
            ).scalar_one()
        )
        return [(row[0], row[1]) for row in rows], total

    def list_lines(self, statement_id: UUID) -> list[PayrollLine]:
        return list(self.session.scalars(
            select(PayrollLine).where(
                PayrollLine.statement_id == statement_id
            ).order_by(PayrollLine.id)
        ).all())

    def list_adjustments(self, statement_id: UUID) -> list[PayrollAdjustment]:
        return list(self.session.scalars(
            select(PayrollAdjustment).where(
                PayrollAdjustment.statement_id == statement_id
            ).order_by(PayrollAdjustment.id)
        ).all())

    def find_adjustment_by_approval(
        self, statement_id: UUID, approval_id: UUID,
    ) -> PayrollAdjustment | None:
        return self.session.scalar(
            select(PayrollAdjustment).where(
                PayrollAdjustment.statement_id == statement_id,
                PayrollAdjustment.approval_id == approval_id,
            )
        )

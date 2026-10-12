"""SeedCheckService（T06 / PRD §10.2、§10.4、§8）。

T06 范围：
- 仅准备表结构、空批次命令（无主数据/业务数据导入）；
- SeedCheckService 暴露三类跨域负例扫描（PRD §10.2）：
  1) 演示商家 + 生产合同
  2) 生产工资引用演示绩效
  3) 异步导出混入演示数据
- 生产配置拒绝 DEMO 批次（PRD §10.2 / R24）：
  environment=production + dataset_type=DEMO → 拒；
- 空批次（PENDING/0 rows）可运行：run → SUCCEEDED，无负例。

T06 不做：
- 实际解析 Excel / CSV；
- 实际写业务表（主数据 SKU/账号/片区/品牌 + 业务 演示表均未到卡）；
- 任何对生产数据表的内容写入。

行扫描器共享 `data_scope_validator` 模块，避免业务卡与 SeedCheckService
各自实现导致不一致。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, DomainError
from app.models.seed import (
    SEED_STATUS_FAILED,
    SEED_STATUS_PENDING,
    SEED_STATUS_RUNNING,
    SEED_STATUS_SUCCEEDED,
    SEED_TYPE_EMPTY,
    SeedBatch,
)
from app.services.audit_service import AuditService
from app.services.data_scope_validator import (
    count_demo_records_in_production_scope,
    data_scope_validator,
    find_demo_merchant_with_production_contract,
    find_payroll_referencing_demo_performance,
)
from app.services.demo_data_guard import DataScope, assert_demo_disabled_in_production


class SeedCheckService:
    """演示数据批次 + 跨域负例自检。"""

    NEGATIVE_KIND_DEMO_MERCHANT_PROD_CONTRACT = "demo_merchant_production_contract"
    NEGATIVE_KIND_PROD_PAYROLL_DEMO_PERFORMANCE = (
        "production_payroll_referencing_demo_performance"
    )
    NEGATIVE_KIND_EXPORT_DEMO_MIXED = "async_export_demo_mixed"

    def __init__(self, session: Session) -> None:
        self.session = session

    # -------- 空批次：仅占位 / 自检（Q029）--------

    def create_empty_batch(
        self,
        *,
        submitted_by_id: UUID,
        seed_type: str = SEED_TYPE_EMPTY,
        dataset_type: str = "DEMO",
        dataset_id: UUID | None = None,
    ) -> SeedBatch:
        """创建空批次；T06 默认 dataset_type=DEMO（演示自检）。"""
        if dataset_type not in ("PRODUCTION", "DEMO"):
            raise DomainError(
                code="SEED_DATASET_INVALID",
                message="演示批次仅接受 PRODUCTION/DEMO 数据域",
                details={"dataset_type": dataset_type},
            )
        # 生产配置硬锁（Q031）：environment=production 拒绝 DEMO
        assert_demo_disabled_in_production(dataset_type=dataset_type)

        if dataset_id is None and dataset_type == "DEMO":
            # 演示默认 dataset_id 固定串（与 PRD §10.3 一致；T06 仅标识，不导入）
            dataset_id = UUID(int=0)  # 占位；T07 接入工作簿解析时改用真实 hash
        elif dataset_id is None:
            import uuid

            dataset_id = uuid.uuid4()

        batch = SeedBatch(
            dataset_type=dataset_type,
            dataset_id=dataset_id,
            seed_type=seed_type,
            source_filename="",
            file_hash="",
            status=SEED_STATUS_PENDING,
            submitted_by_id=submitted_by_id,
        )
        self.session.add(batch)
        self.session.flush()
        return batch

    def run_check(self, batch: SeedBatch) -> SeedBatch:
        """对批次执行 SeedCheck 自检（无业务表写入）。

        - 空批次 + 0 行 → SUCCEEDED，无负例；
        - 三类跨域负例命中 → FAILED，negative_findings 记录证据；
        - 负例检测结果通过 AuditService 落审计。
        """
        if batch.status == SEED_STATUS_SUCCEEDED:
            raise ConflictError(
                code="SEED_ALREADY_SUCCEEDED",
                message="批次已成功完成；不得重复运行",
                details={"batch_id": str(batch.id)},
            )
        if batch.status == SEED_STATUS_RUNNING:
            raise ConflictError(
                code="SEED_ALREADY_RUNNING",
                message="批次正在运行中",
                details={"batch_id": str(batch.id)},
            )

        before = {"status": batch.status}
        batch.status = SEED_STATUS_RUNNING
        batch.started_at = datetime.now(tz=UTC)
        self.session.flush()

        findings: list[dict[str, Any]] = []

        # 1) 演示商家 + 生产合同
        prod_demo = find_demo_merchant_with_production_contract(self.session)
        if prod_demo:
            findings.append(
                {
                    "kind": self.NEGATIVE_KIND_DEMO_MERCHANT_PROD_CONTRACT,
                    "code": "DEMO_MERCHANT_PRODUCTION_CONTRACT",
                    "message": "演示商家与生产合同跨域并存；关系必须切断",
                    "evidence": prod_demo,
                }
            )

        # 2) 生产工资引用演示绩效
        prod_payroll = find_payroll_referencing_demo_performance(self.session)
        if prod_payroll:
            findings.append(
                {
                    "kind": self.NEGATIVE_KIND_PROD_PAYROLL_DEMO_PERFORMANCE,
                    "code": "PROD_PAYROLL_DEMO_PERFORMANCE",
                    "message": "生产工资记录引用了演示绩效，必须切断",
                    "evidence": [
                        {"payroll_id": str(pid)} for pid in prod_payroll[:20]
                    ],  # 限 20 条防膨胀
                }
            )

        # 3) 异步导出混入演示数据
        if data_scope_validator._rules.get("export.async") is not None:  # noqa: SLF001
            # 扫描正式模型中是否存在 demo 混入
            counts = count_demo_records_in_production_scope(self.session)
            total = sum(counts.values())
            if total > 0:
                findings.append(
                    {
                        "kind": self.NEGATIVE_KIND_EXPORT_DEMO_MIXED,
                        "code": "EXPORT_DEMO_MIXED",
                        "message": "正式模型存在演示数据混入；异步导出必须显式 PRODUCTION",
                        "evidence": counts,
                    }
                )

        batch.negative_findings = {
            "findings": findings,
            "checked_at": datetime.now(tz=UTC).isoformat(),
        }
        if findings:
            batch.status = SEED_STATUS_FAILED
            batch.error_log = "跨域负例命中：\n" + "\n".join(
                f"- {f['code']}: {f['message']}" for f in findings
            )
        else:
            batch.status = SEED_STATUS_SUCCEEDED
            batch.error_log = None
        batch.finished_at = datetime.now(tz=UTC)
        batch.rows_total = 0
        batch.rows_imported = 0
        batch.rows_rejected = len(findings)
        self.session.flush()

        # 落审计（R23 / R30）
        AuditService(self.session).record(
            object_type="SEED_BATCH",
            object_id=batch.id,
            event_type="UPDATE",
            actor_id=batch.submitted_by_id,
            before=before,
            after={
                "status": batch.status,
                "rows_rejected": batch.rows_rejected,
                "negative_findings_count": len(findings),
            },
            reason=f"SeedCheck 完成：{'负例' if findings else '通过'}",
        )
        return batch

    # -------- 查询（路由 / CLI 复用）--------

    def get(self, batch_id: UUID) -> SeedBatch | None:
        return self.session.get(SeedBatch, batch_id)

    def list_batches(
        self, *, status: str | None = None, limit: int = 50
    ) -> list[SeedBatch]:
        stmt = select(SeedBatch).order_by(SeedBatch.created_at.desc()).limit(limit)
        if status is not None:
            stmt = stmt.where(SeedBatch.status == status)
        return list(self.session.execute(stmt).scalars().all())

    # -------- CLI 入口（Q029 / Q030 / Q031）--------

    @staticmethod
    def assert_export_scope(scope: str) -> None:
        """异步导出预检（CLI / 路由共用）。"""
        data_scope_validator.assert_production(
            key="export.async", scope=DataScope.parse(scope)
        )


def build_empty_check_for_environment(
    session: Session,
    *,
    environment: str,
    submitted_by_id: UUID,
) -> SeedBatch:
    """CLI 入口：根据环境创建空批次（dev/test → DEMO；production → 拒）。"""
    if environment == "production":
        # 生产环境仅创建 PRODUCTION 空批次；演示批次被生产配置锁拦截
        return SeedCheckService(session).create_empty_batch(
            submitted_by_id=submitted_by_id,
            dataset_type="PRODUCTION",
        )
    return SeedCheckService(session).create_empty_batch(
        submitted_by_id=submitted_by_id,
        dataset_type="DEMO",
    )

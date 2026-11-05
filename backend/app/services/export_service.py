"""ExportService（T21 / PRD §4.10 经营报表、§5.3 report.export_request、§9.4 导出）。

红线：
- 导出必须显式 `DataScope.PRODUCTION`；缺省/ALL/DEMO 直接拒绝（R34、A24）；
- 异步任务在执行时**重新校验** scope 与发起人权限（防止权限/数据域漂移）；
- 行级范围按发起人角色画像过滤；字段级按 visible/hidden 过滤（R28）；
- 产出走对象存储 + 短期下载链接；链接到期即失效（Q120）；所有导出写审计；
- 报表数值一律复用 MetricsService（R22：导出不得各自重算）。
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import DomainError, PermissionDeniedError
from app.core.permissions import (
    MODULE_DASHBOARD,
    MODULE_REPORT,
    ActorContext,
    get_profile,
    permission_service,
)
from app.models import MerchantAssignment, Role, User, UserRegionBrandAuth
from app.models.enums import ExportFormat, ExportStatus, ReportKey
from app.models.reporting import ExportJob
from app.repositories.reporting_repository import ReportingRepository
from app.services.audit_service import EVENT_CREATE, EVENT_STATE_TRANSITION, AuditService
from app.services.demo_data_guard import DataScope, assert_production_scope
from app.services.metrics_service import (
    M_MONTHLY_NET_PROFIT,
    MetricsService,
    parse_period,
)
from app.services.report_scope import MerchantScope, resolve_merchant_scope
from app.services.storage import get_storage_backend

_FIELD_SEP = "│"
_CONTENT_TYPES = {
    ExportFormat.CSV.value: "text/csv; charset=utf-8",
    ExportFormat.XLSX.value: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ExportFormat.PDF.value: "application/pdf",
}
_REPORT_TITLES = {
    ReportKey.METRICS.value: "经营指标汇总",
    ReportKey.CONTRACTS.value: "合同报表",
    ReportKey.MERCHANTS.value: "商家报表",
    ReportKey.FREEZERS.value: "冰柜报表",
    ReportKey.VISITS.value: "巡访报表",
    ReportKey.SALES.value: "销量报表",
    ReportKey.REPLENISHMENT.value: "补货报表",
    ReportKey.PROCUREMENT.value: "采购报表",
    ReportKey.EXPENSES.value: "费用报表",
    ReportKey.PERFORMANCE.value: "绩效工资报表",
    ReportKey.PROFIT.value: "损益报表",
}

# 空结果时的列定义（保证表头稳定）
_REPORT_COLUMNS = {
    ReportKey.MERCHANTS.value: ["id", "code", "name", "status", "grade",
                                "region_id", "brand_id"],
    ReportKey.CONTRACTS.value: ["id", "contract_no", "title", "status",
                                "merchant_id", "effective_from", "expire_at"],
    ReportKey.SALES.value: ["id", "merchant_id", "sku_id", "biz_date",
                            "confirmed_boxes", "amount_fen"],
    ReportKey.FREEZERS.value: ["id", "asset_code", "model", "status",
                               "purchase_price_fen", "current_merchant_id"],
    ReportKey.REPLENISHMENT.value: ["id", "code", "merchant_id", "status",
                                    "committed_boxes", "fulfilled_boxes",
                                    "promised_delivery_date"],
    ReportKey.PROCUREMENT.value: ["id", "code", "sku_id", "status",
                                  "quantity_cases", "unit_price_fen",
                                  "expected_date"],
    ReportKey.EXPENSES.value: ["id", "expense_no", "type", "title",
                               "amount_fen", "status", "period_month"],
    ReportKey.PERFORMANCE.value: ["id", "statement_no", "user_id", "status",
                                  "base_salary_fen", "performance_fen",
                                  "gross_pay_fen", "period_month"],
    ReportKey.VISITS.value: ["id", "merchant_id", "user_id", "status",
                             "validity", "source", "display_pct",
                             "submitted_at"],
}


def _report_columns(report_key: str) -> list[str]:
    return list(_REPORT_COLUMNS.get(report_key, []))


class ExportService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = ReportingRepository(session)
        self.metrics = MetricsService(session)
        self.audit = AuditService(session)

    # ============================ 申请 ============================

    def request_export(
        self, actor: ActorContext, *, report_key: str, fmt: str,
        filters: dict | None = None, scope: DataScope | None,
    ) -> ExportJob:
        """创建异步导出任务（QUEUED）；scope 缺省/非 PRODUCTION 直接拒绝。"""
        settings = get_settings()
        if not settings.export_enabled:
            raise DomainError(code="EXPORT_DISABLED", message="导出功能当前已关闭")
        self._require_scope(scope, "export.request")
        if not permission_service.has_ability(actor, MODULE_REPORT):
            raise PermissionDeniedError(
                code="EXPORT_FORBIDDEN",
                message="当前角色无权发起导出（仅经理/财务/管理员）",
                details={"role": actor.role_code},
            )
        if report_key not in {k.value for k in ReportKey}:
            raise DomainError(code="EXPORT_REPORT_UNKNOWN",
                              message="未知报表", details={"report_key": report_key})
        if fmt not in {f.value for f in ExportFormat}:
            raise DomainError(code="EXPORT_FORMAT_INVALID",
                              message="导出格式必须为 XLSX/CSV/PDF",
                              details={"format": fmt})
        user = self.session.get(User, actor.user_id)
        if user is None:
            raise DomainError(code="USER_NOT_FOUND", message="登录用户不存在")

        job = ExportJob(
            job_no=f"EXP-{uuid4().hex[:14].upper()}",
            report_key=report_key, format=fmt,
            filters_json=filters or {},
            columns_json=[],
            scope=DataScope.PRODUCTION.value,
            status=ExportStatus.QUEUED.value,
            requested_by_id=actor.user_id,
            requested_role_code=actor.role_code,
            row_scope=get_profile(actor.role_code).row_scope,
            dataset_type="PRODUCTION", dataset_id=user.dataset_id,
        )
        self.session.add(job)
        self.session.flush()
        self.audit.record(
            object_type="EXPORT_JOB", object_id=job.id, event_type=EVENT_CREATE,
            actor_id=actor.user_id,
            after={"job_no": job.job_no, "report_key": report_key, "format": fmt,
                   "scope": job.scope, "filters": filters or {}},
            reason="发起异步导出申请",
        )
        return job

    # ============================ 执行（异步任务入口）============================

    def run_job(self, job_id: UUID) -> ExportJob:
        """执行导出任务；重新校验 scope 与发起人权限（不信任申请期状态）。"""
        job = self.session.get(ExportJob, job_id)
        if job is None:
            raise DomainError(code="EXPORT_JOB_NOT_FOUND", message="导出任务不存在")
        if job.status in (ExportStatus.SUCCEEDED.value, ExportStatus.FAILED.value):
            return job  # 幂等：已终结不重复执行

        job.status = ExportStatus.RUNNING.value
        job.started_at = datetime.now(UTC)
        self.session.flush()
        try:
            # 1) 重新校验数据域（必须 PRODUCTION）
            self._require_scope(DataScope.parse(job.scope), "export.run")
            # 2) 重新校验发起人权限（角色/能力可能已变化）
            actor = self._actor_for_user(job.requested_by_id)
            if actor is None or not permission_service.has_ability(actor, MODULE_REPORT):
                raise PermissionDeniedError(
                    code="EXPORT_FORBIDDEN",
                    message="发起人当前无导出权限；任务拒绝执行",
                )
            # 3) 生成：用 SAVEPOINT 隔离，失败只回滚本次生成，不动已写记录
            with self.session.begin_nested():
                columns, rows = self._build_rows(job, actor)
                data = self._render(job.format, job.report_key, columns, rows)
                object_key = f"EXPORT/{uuid4().hex}/{job.job_no}.{job.format.lower()}"
                get_storage_backend().put(object_key, data)
                job.object_key = object_key
                job.content_type = _CONTENT_TYPES[job.format]
                job.columns_json = columns
                job.row_count = len(rows)
                settings = get_settings()
                job.expires_at = datetime.now(UTC) + timedelta(
                    seconds=settings.export_link_ttl_seconds
                )
            job.status = ExportStatus.SUCCEEDED.value
            job.finished_at = datetime.now(UTC)
            self.session.flush()
            self.audit.record(
                object_type="EXPORT_JOB", object_id=job.id,
                event_type=EVENT_STATE_TRANSITION, actor_id=job.requested_by_id,
                before={"status": ExportStatus.RUNNING.value},
                after={"status": job.status, "row_count": job.row_count,
                       "object_key": job.object_key,
                       "expires_at": job.expires_at.isoformat()},
                reason="异步导出生成完成",
            )
            return job
        except Exception:
            job.status = ExportStatus.FAILED.value
            job.finished_at = datetime.now(UTC)
            self.session.flush()
            self.audit.record(
                object_type="EXPORT_JOB", object_id=job.id,
                event_type=EVENT_STATE_TRANSITION, actor_id=job.requested_by_id,
                after={"status": job.status}, reason="异步导出失败",
            )
            raise

    # ============================ 下载（短期链接）============================

    def get_download(self, actor: ActorContext, job_id: UUID) -> dict:
        """返回短期下载链接；越权/未完成/已过期一律拒绝（Q119/Q120）。"""
        job = self.session.get(ExportJob, job_id)
        if job is None:
            raise DomainError(code="EXPORT_JOB_NOT_FOUND", message="导出任务不存在")
        user = self.session.get(User, actor.user_id)
        if user is None or job.dataset_id != user.dataset_id:
            raise PermissionDeniedError(
                code="EXPORT_DATASET_FORBIDDEN", message="无权访问其他数据域的导出文件"
            )
        if not self._can_read(actor, job):
            raise PermissionDeniedError(
                code="EXPORT_FORBIDDEN", message="无权下载该导出文件"
            )
        if job.status != ExportStatus.SUCCEEDED.value or job.object_key is None:
            raise DomainError(
                code="EXPORT_NOT_READY", message="导出尚未完成",
                details={"status": job.status},
            )
        now = datetime.now(UTC)
        if job.expires_at is not None and now >= job.expires_at:
            raise DomainError(
                code="EXPORT_LINK_EXPIRED", message="下载链接已过期，请重新导出",
                details={"expires_at": job.expires_at.isoformat()},
            )
        settings = get_settings()
        return {
            "job_id": str(job.id),
            "job_no": job.job_no,
            "object_key": job.object_key,
            "content_type": job.content_type,
            "row_count": job.row_count,
            "expires_at": job.expires_at.isoformat() if job.expires_at else None,
            "ttl_seconds": settings.export_link_ttl_seconds,
        }

    def _rep_scope_override(
        self, actor: ActorContext, sales_rep_id: UUID | None,
    ) -> MerchantScope | None:
        """业务员维度：取该业务员在有效期内的负责商家，与 actor 行级范围取交集。"""
        if sales_rep_id is None:
            return None
        base = resolve_merchant_scope(self.session, actor)
        rows = self.session.execute(
            select(MerchantAssignment.merchant_id).where(
                MerchantAssignment.owner_user_id == sales_rep_id,
                MerchantAssignment.valid_range.op("@>")(func.now()),
            )
        ).scalars().all()
        rep_ids = tuple(sorted(set(rows), key=str))
        if base.is_global:
            return MerchantScope(merchant_ids=rep_ids)
        if base.region_ids is not None:
            return MerchantScope(region_ids=base.region_ids, merchant_ids=rep_ids)
        return MerchantScope(
            merchant_ids=tuple(sorted(set(base.merchant_list or []) & set(rep_ids), key=str))
        )

    def list_jobs(self, actor: ActorContext) -> list[ExportJob]:
        user = self.session.get(User, actor.user_id)
        if user is None:
            raise DomainError(code="USER_NOT_FOUND", message="登录用户不存在")
        stmt = select(ExportJob).where(
            ExportJob.dataset_id == user.dataset_id
        ).order_by(ExportJob.created_at.desc())
        jobs = list(self.session.execute(stmt).scalars().all())
        if permission_service.has_ability(actor, MODULE_REPORT) and \
                get_profile(actor.role_code).row_scope in ("global", "global_readonly"):
            return jobs
        return [j for j in jobs if j.requested_by_id == actor.user_id]

    def read_object(self, job: ExportJob) -> bytes:
        if job.object_key is None:
            raise DomainError(code="EXPORT_NOT_READY", message="导出尚未完成")
        return get_storage_backend().get(job.object_key)

    # ============================ 内部 ============================

    def _can_read(self, actor: ActorContext, job: ExportJob) -> bool:
        if job.requested_by_id == actor.user_id:
            return True
        profile = get_profile(actor.role_code)
        # 全局（财务/管理员）可读本数据域内全部导出
        return profile.row_scope in ("global", "global_readonly") and \
            permission_service.has_ability(actor, MODULE_REPORT)

    def _actor_for_user(self, user_id: UUID) -> ActorContext | None:
        user = self.session.get(User, user_id)
        if user is None:
            return None
        role = self.session.get(Role, user.role_id)
        if role is None:
            return None
        rows = self.session.execute(
            select(UserRegionBrandAuth).where(UserRegionBrandAuth.user_id == user.id)
        ).scalars().all()
        region_ids = frozenset(r.region_id for r in rows)
        brand_ids = frozenset(r.brand_id for r in rows)
        return ActorContext(
            user_id=user.id, role_code=role.role_code, region_ids=region_ids,
            brand_ids=brand_ids,
        )

    def _build_rows(
        self, job: ExportJob, actor: ActorContext,
    ) -> tuple[list[str], list[dict]]:
        return self._build_rows_for(
            actor, job.report_key, job.dataset_id, job.filters_json or {}
        )

    def _build_rows_for(
        self, actor: ActorContext, report_key: str, dataset_id: UUID,
        filters: dict, scope_override: MerchantScope | None = None,
    ) -> tuple[list[str], list[dict]]:
        """按报表键生成列 + 行（行级范围 + 字段权限过滤）。"""
        mscope: MerchantScope = scope_override or resolve_merchant_scope(
            self.session, actor)
        period = filters.get("period")
        start = end = None
        if period:
            start, end = parse_period(period)
        # 片区/品牌/SKU 进一步过滤（在行级范围内取交集）
        if filters.get("region_id"):
            mscope = MerchantScope(region_ids=(UUID(str(filters["region_id"])),))
        if report_key == ReportKey.METRICS.value:
            return self._metrics_rows(actor, period)
        if report_key == ReportKey.PROFIT.value:
            return self._profit_rows(actor, period)

        rows = self.repo.report_rows(report_key, dataset_id, mscope, start, end)
        if filters.get("sku_id") and rows and "sku_id" in rows[0]:
            sku = str(filters["sku_id"])
            rows = [r for r in rows if r.get("sku_id") == sku]
        if filters.get("brand_id"):
            brand = str(filters["brand_id"])
            rows = [r for r in rows if r.get("brand_id") == brand]
        columns = list(rows[0].keys()) if rows else _report_columns(report_key)
        return self._apply_field_permissions(actor, columns, rows)

    def report_view(
        self, actor: ActorContext, *, report_key: str, scope: DataScope | None,
        period: str | None = None, region_id: UUID | None = None,
        brand_id: UUID | None = None, sku_id: UUID | None = None,
        sales_rep_id: UUID | None = None,
    ) -> dict:
        """同步聚合报表（单请求返回列+行，避免前端多请求拼装）。

        支持时间（period）/片区（region_id）/品牌（brand_id）/SKU（sku_id）/
        业务员（sales_rep_id）多维筛选。
        """
        self._require_scope(scope, "report.view")
        if not permission_service.has_ability(actor, MODULE_DASHBOARD):
            raise PermissionDeniedError(
                code="REPORT_FORBIDDEN", message="当前角色无权查看经营报表",
                details={"role": actor.role_code},
            )
        if report_key not in {k.value for k in ReportKey}:
            raise DomainError(code="REPORT_UNKNOWN", message="未知报表",
                              details={"report_key": report_key})
        user = self.session.get(User, actor.user_id)
        if user is None:
            raise DomainError(code="USER_NOT_FOUND", message="登录用户不存在")
        filters: dict = {}
        if period:
            filters["period"] = period
        if region_id is not None:
            filters["region_id"] = str(region_id)
        if brand_id is not None:
            filters["brand_id"] = str(brand_id)
        if sku_id is not None:
            filters["sku_id"] = str(sku_id)
        if sales_rep_id is not None:
            filters["sales_rep_id"] = str(sales_rep_id)
        scope_override = self._rep_scope_override(actor, sales_rep_id)
        columns, rows = self._build_rows_for(
            actor, report_key, user.dataset_id, filters, scope_override
        )
        self.audit.record(
            object_type="REPORT_VIEW", object_id=user.dataset_id,
            event_type=EVENT_CREATE, actor_id=actor.user_id,
            after={"report_key": report_key, "filters": filters,
                   "row_count": len(rows)},
            reason="查看经营报表",
        )
        return {"report_key": report_key, "columns": columns, "rows": rows,
                "row_count": len(rows)}

    def require_runner(self, actor: ActorContext, job_id: UUID) -> None:
        """执行导出：仅发起人本人，或全局财务/管理员可触发。"""
        job = self.session.get(ExportJob, job_id)
        if job is None:
            raise DomainError(code="EXPORT_JOB_NOT_FOUND", message="导出任务不存在")
        if job.requested_by_id == actor.user_id:
            return
        profile = get_profile(actor.role_code)
        if profile.row_scope in ("global", "global_readonly") and \
                permission_service.has_ability(actor, MODULE_REPORT):
            return
        raise PermissionDeniedError(
            code="EXPORT_FORBIDDEN", message="无权触发他人导出任务"
        )

    def _metrics_rows(self, actor: ActorContext, period: str | None):
        if not period:
            raise DomainError(code="EXPORT_PERIOD_REQUIRED",
                              message="指标导出必须提供 period 过滤条件")
        results = self.metrics.all(
            actor, scope=DataScope.PRODUCTION, period=period
        )
        columns = ["key", "name", "value", "unit", "definition", "as_of",
                   "data_complete", "note"]
        rows = [
            {"key": r.key, "name": r.name,
             "value": "" if r.value is None else str(r.value),
             "unit": r.unit, "definition": r.definition,
             "as_of": r.as_of.isoformat(), "data_complete": r.data_complete,
             "note": r.note or ""}
            for r in results
        ]
        return self._apply_field_permissions(actor, columns, rows)

    def _profit_rows(self, actor: ActorContext, period: str | None):
        if not period:
            raise DomainError(code="EXPORT_PERIOD_REQUIRED",
                              message="损益导出必须提供 period 过滤条件")
        result = self.metrics.compute(
            actor, M_MONTHLY_NET_PROFIT, scope=DataScope.PRODUCTION, period=period
        )
        columns = ["name", "value", "unit", "definition", "as_of"]
        rows = [{
            "name": result.name,
            "value": "" if result.value is None else str(result.value),
            "unit": result.unit, "definition": result.definition,
            "as_of": result.as_of.isoformat(),
        }]
        return self._apply_field_permissions(actor, columns, rows)

    def _apply_field_permissions(
        self, actor: ActorContext, columns: list[str], rows: list[dict],
    ) -> tuple[list[str], list[dict]]:
        profile = get_profile(actor.role_code)
        cols = list(columns)
        if profile.visible_fields:
            cols = [c for c in cols if c in profile.visible_fields]
        cols = [c for c in cols if c not in profile.hidden_fields]
        filtered = [{c: r.get(c) for c in cols} for r in rows]
        return cols, filtered

    def _render(
        self, fmt: str, report_key: str, columns: list[str], rows: list[dict],
    ) -> bytes:
        limit = get_settings().export_max_rows
        rows = rows[:limit]
        if fmt == ExportFormat.CSV.value:
            return self._render_csv(columns, rows)
        if fmt == ExportFormat.XLSX.value:
            return self._render_xlsx(report_key, columns, rows)
        return self._render_pdf(report_key, columns, rows)

    @staticmethod
    def _render_csv(columns: list[str], rows: list[dict]) -> bytes:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(columns)
        for r in rows:
            writer.writerow([_cell(r.get(c)) for c in columns])
        # BOM 便于 Excel 正确识别 UTF-8
        return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")

    @staticmethod
    def _render_xlsx(report_key: str, columns: list[str], rows: list[dict]) -> bytes:
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.title = _REPORT_TITLES.get(report_key, "报表")[:31] or "报表"
        ws.append(columns)
        for r in rows:
            ws.append([_cell(r.get(c)) for c in columns])
        out = io.BytesIO()
        wb.save(out)
        return out.getvalue()

    @staticmethod
    def _render_pdf(report_key: str, columns: list[str], rows: list[dict]) -> bytes:
        """最小可用 PDF（内置 Helvetica 仅支持 Latin-1；非 ASCII 以 '?' 代替）。"""
        lines = [_REPORT_TITLES.get(report_key, "报表"), _FIELD_SEP.join(columns)]
        for r in rows:
            lines.append(_FIELD_SEP.join(_cell(r.get(c)) for c in columns))
        return _simple_pdf(lines)

    @staticmethod
    def _require_scope(scope: DataScope | None, where: str) -> None:
        if scope is None:
            raise DomainError(
                code="DATA_SCOPE_REQUIRED",
                message="必须显式传入 DataScope.PRODUCTION",
                details={"where": where},
            )
        if not isinstance(scope, DataScope):
            scope = DataScope.parse(scope)
        assert_production_scope(scope=scope, where=where)


def _cell(value) -> str:
    if value is None:
        return ""
    return str(value)


def _simple_pdf(lines: list[str]) -> bytes:
    """生成单页/多页文本 PDF（ASCII-safe）。"""
    per_page = 45
    pages = [lines[i:i + per_page] for i in range(0, len(lines), per_page)] or [[]]
    objects: list[bytes] = []
    # 1: Catalog, 2: Pages, 3..: Page objects then content streams, font last
    page_ids = []
    content_ids = []
    base = 3
    for i in range(len(pages)):
        page_ids.append(base + i * 2)
        content_ids.append(base + i * 2 + 1)
    font_id = base + len(pages) * 2

    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    for i, page_lines in enumerate(pages):
        objects.append(
            (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
             f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
             f"/Contents {content_ids[i]} 0 R >>").encode()
        )
        body = ["BT", "/F1 9 Tf", "40 800 Td", "12 TL"]
        for ln in page_lines:
            body.append(f"({_pdf_escape(ln)}) Tj T*")
        body.append("ET")
        stream = "\n".join(body).encode("latin-1", errors="replace")
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
                       + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for idx, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{idx} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF").encode()
    return bytes(out)


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

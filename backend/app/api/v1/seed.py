"""SeedCheck 路由（T06 / PRD §10.2、§10.4）。

- GET  /api/v1/seed/batches             最近批次列表
- POST /api/v1/seed/batches/empty       创建一个空批次（环境决定 dataset_type）
- POST /api/v1/seed/batches/{id}/run    运行 SeedCheck
- GET  /api/v1/seed/batches/{id}        拉单个批次详情

T06 范围：仅骨架；不实现 Excel 导入。生产环境 POST /empty 在 dataset_type=DEMO
时由 SeedCheckService 内部直接拒。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import error_response_body
from app.core.security import hash_password
from app.db.session import session_scope
from app.models.enums import RoleCode, UserStatus
from app.models.user import Role, User
from app.services.seed_check_service import (
    SeedCheckService,
    build_empty_check_for_environment,
)

router = APIRouter(prefix="/api/v1/seed", tags=["seed"])


class EmptyBatchRequest(BaseModel):
    seed_type: str = Field(default="EMPTY", pattern="^(MASTER|BUSINESS|EMPTY)$")


class BatchResponse(BaseModel):
    id: str
    dataset_type: str
    dataset_id: str
    seed_type: str
    status: str
    rows_total: int
    rows_imported: int
    rows_rejected: int
    negative_findings: dict[str, Any]
    error_log: str | None


def _cli_submitter(session: Session) -> UUID:
    """路由侧的"系统提交者"占位。"""
    role = (
        session.query(Role)
        .filter(Role.role_code == RoleCode.SYSTEM_ADMIN.value)
        .first()
    )
    if role is None:
        role = Role(
            code=f"api-system-admin-{UUID(int=0).hex[:6]}",
            name="API System Admin",
            role_code=RoleCode.SYSTEM_ADMIN.value,
            dataset_type="PRODUCTION",
            dataset_id=UUID(int=0),
        )
        session.add(role)
        session.flush()
    user = session.query(User).filter(User.username == "api-system").first()
    if user is None:
        user = User(
            username="api-system",
            display_name="API System",
            role_id=role.id,
            status=UserStatus.ACTIVE.value,
            must_change_password=False,
            password_hash=hash_password("api-not-for-prod"),
            dataset_type="PRODUCTION",
            dataset_id=role.dataset_id,
        )
        session.add(user)
        session.flush()
    return user.id


@router.get("/batches")
def list_batches(limit: int = 50) -> dict[str, Any]:
    settings = get_settings()
    if not settings.enable_seed_cli:
        raise HTTPException(
            status_code=503,
            detail=error_response_body(
                "SEED_CLI_DISABLED", "Seed CLI 未启用", {}
            ),
        )
    with session_scope() as s:
        rows = SeedCheckService(s).list_batches(limit=limit)
        return {
            "batches": [
                {
                    "id": str(b.id),
                    "dataset_type": b.dataset_type,
                    "dataset_id": str(b.dataset_id),
                    "seed_type": b.seed_type,
                    "status": b.status,
                    "rows_rejected": b.rows_rejected,
                    "created_at": b.created_at.isoformat() if b.created_at else None,
                }
                for b in rows
            ]
        }


@router.post("/batches/empty", response_model=BatchResponse)
def create_empty_batch(_: EmptyBatchRequest) -> BatchResponse:
    settings = get_settings()
    if not settings.enable_seed_cli:
        raise HTTPException(
            status_code=503,
            detail=error_response_body(
                "SEED_CLI_DISABLED", "Seed CLI 未启用", {}
            ),
        )
    with session_scope() as s:
        submitter = _cli_submitter(s)
        s.flush()
        batch = build_empty_check_for_environment(
            s,
            environment=settings.environment,
            submitted_by_id=submitter,
        )
        s.flush()
        return BatchResponse(
            id=str(batch.id),
            dataset_type=batch.dataset_type,
            dataset_id=str(batch.dataset_id),
            seed_type=batch.seed_type,
            status=batch.status,
            rows_total=batch.rows_total,
            rows_imported=batch.rows_imported,
            rows_rejected=batch.rows_rejected,
            negative_findings=batch.negative_findings,
            error_log=batch.error_log,
        )


@router.post("/batches/{batch_id}/run", response_model=BatchResponse)
def run_batch(batch_id: UUID) -> BatchResponse:
    settings = get_settings()
    if not settings.enable_seed_cli:
        raise HTTPException(
            status_code=503,
            detail=error_response_body(
                "SEED_CLI_DISABLED", "Seed CLI 未启用", {}
            ),
        )
    with session_scope() as s:
        svc = SeedCheckService(s)
        batch = svc.get(batch_id)
        if batch is None:
            raise HTTPException(
                status_code=404,
                detail=error_response_body(
                    "SEED_BATCH_NOT_FOUND", "Seed 批次不存在", {"batch_id": str(batch_id)}
                ),
            )
        result = svc.run_check(batch)
        return BatchResponse(
            id=str(result.id),
            dataset_type=result.dataset_type,
            dataset_id=str(result.dataset_id),
            seed_type=result.seed_type,
            status=result.status,
            rows_total=result.rows_total,
            rows_imported=result.rows_imported,
            rows_rejected=result.rows_rejected,
            negative_findings=result.negative_findings,
            error_log=result.error_log,
        )


@router.get("/batches/{batch_id}", response_model=BatchResponse)
def get_batch(batch_id: UUID) -> BatchResponse:
    with session_scope() as s:
        batch = SeedCheckService(s).get(batch_id)
        if batch is None:
            raise HTTPException(
                status_code=404,
                detail=error_response_body(
                    "SEED_BATCH_NOT_FOUND", "Seed 批次不存在", {"batch_id": str(batch_id)}
                ),
            )
        return BatchResponse(
            id=str(batch.id),
            dataset_type=batch.dataset_type,
            dataset_id=str(batch.dataset_id),
            seed_type=batch.seed_type,
            status=batch.status,
            rows_total=batch.rows_total,
            rows_imported=batch.rows_imported,
            rows_rejected=batch.rows_rejected,
            negative_findings=batch.negative_findings,
            error_log=batch.error_log,
        )

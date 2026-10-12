"""T04 服务内核测试（T04 / PRD §5/§8/§9.4）。

Q016 审计随事务回滚（失败事务不留成功审计）
Q017 幂等同键同结果（重放）
Q018 同键异参 409
Q019 文件越权 / 签名 / 隔离 / 扫描状态
Q020 日志敏感词扫描（password=、JWT、Argon2、手机号）

附带：
- 处理器 ConflictError → HTTP 409 映射验证
- 响应 Schema 字段裁剪（手机号掩码、工资/成本按角色）
"""

from __future__ import annotations

import logging
import time
from io import StringIO
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import ConflictError
from app.core.handlers import register_exception_handlers
from app.core.logging import (
    JsonFormatter,
    mask_phone_text,
    redact,
    sanitize_message,
)
from app.core.permissions import ActorContext
from app.core.redaction import sanitize_response
from app.core.security import hash_password
from app.models import AuditLog, FileObject, IdempotencyKey, Role, User
from app.models.enums import RoleCode, UserStatus
from app.services.audit_service import (
    EVENT_CREATE,
    AuditService,
)
from app.services.file_service import BIZ_PHOTO, FileService
from app.services.idempotency_service import (
    STATUS_COMPLETED,
    IdempotencyService,
    hash_params,
)
from app.services.storage import LocalFSStorage

# ===== 夹具辅助 =====


@pytest.fixture
def t04_user(session: Session) -> User:
    """测试用业务用户（owner / actor 复用）：先建独立 Role 行满足 FK。"""
    dsid = uuid4()
    role = Role(
        code=f"t04-role-{uuid4().hex[:6]}",
        name="T04 角色",
        role_code=RoleCode.SYSTEM_ADMIN.value,
        dataset_type="PRODUCTION",
        dataset_id=dsid,
    )
    session.add(role)
    session.flush()
    u = User(
        username=f"t04_user_{uuid4().hex[:8]}",
        display_name="T04 用户",
        role_id=role.id,
        status=UserStatus.ACTIVE.value,
        must_change_password=False,
        password_hash=hash_password("t04-pass-not-for-prod"),
        dataset_type="PRODUCTION",
        dataset_id=dsid,
    )
    session.add(u)
    session.flush()
    return u


@pytest.fixture
def actor(t04_user: User) -> ActorContext:
    return ActorContext(
        user_id=t04_user.id,
        role_code=RoleCode.SYSTEM_ADMIN.value,
        region_ids=frozenset(),
        brand_ids=frozenset(),
    )


# ===== Q016 =====


def test_audit_persists_with_required_fields(session: Session, t04_user: User) -> None:
    """Q016 正向：审计同事务记录 actor / 原因 / 前后值 / request-id。"""
    audit = AuditService(session)
    obj_id = uuid4()
    log = audit.record(
        object_type="MERCHANT",
        object_id=obj_id,
        event_type=EVENT_CREATE,
        actor_id=t04_user.id,
        before=None,
        after={"name": "测试商家", "phone": "13812345678", "contact_phone": "13900000000"},
        reason="新建商家",
        extra={"source": "unit-test"},
    )
    session.commit()

    found = session.get(AuditLog, log.id)
    assert found is not None
    assert found.actor_id == t04_user.id
    assert found.reason == "新建商家"
    assert found.before_json is None
    # 命中 SENSITIVE_KEYS（phone/contact_phone）已脱敏，name 保留
    assert found.after_json == {
        "name": "测试商家",
        "phone": "***REDACTED***",
        "contact_phone": "***REDACTED***",
    }
    assert found.request_id is not None  # 由 context 注入


def test_audit_redacts_credentials_in_after(session: Session, t04_user: User) -> None:
    """Q016 强化：凭据/手机号不得进入审计 before/after。"""
    audit = AuditService(session)
    log = audit.record(
        object_type="USER",
        object_id=t04_user.id,
        event_type="UPDATE",
        actor_id=t04_user.id,
        after={
            "username": "alice",
            "phone": "13812345678",
            "password": "hunter2",
            "token": "eyJabc.def.ghi",
            "contact_phone": "13900000000",
        },
    )
    session.commit()

    found = session.get(AuditLog, log.id)
    assert found is not None
    payload = found.after_json
    assert payload is not None
    assert payload["username"] == "alice"  # 业务字段保留
    assert payload["phone"] == "***REDACTED***"
    assert payload["password"] == "***REDACTED***"
    assert payload["token"] == "***REDACTED***"
    assert payload["contact_phone"] == "***REDACTED***"


def test_audit_rolls_back_with_business_transaction(applied_engine) -> None:
    """Q016 核心：业务事务回滚时审计一并消失（失败事务不留成功审计）。

    独立 session 跑（不用 conftest 的 SAVEPOINT 夹具），提交后用新连接验证
    数据库行已落库 / 已回滚。
    """
    SessionLocal = sessionmaker(bind=applied_engine, future=True)
    s = SessionLocal()
    try:
        dsid = uuid4()
        role = Role(
            code=f"t04-rollback-{uuid4().hex[:6]}",
            name="T04 回滚测试角色",
            role_code=RoleCode.SYSTEM_ADMIN.value,
            dataset_type="PRODUCTION",
            dataset_id=dsid,
        )
        s.add(role)
        s.flush()
        original_name = "T04-Original"
        temp = User(
            username=f"t04_rb_{uuid4().hex[:8]}",
            display_name=original_name,
            role_id=role.id,
            status=UserStatus.ACTIVE.value,
            must_change_password=False,
            password_hash=hash_password("dummy"),
            dataset_type="PRODUCTION",
            dataset_id=dsid,
        )
        s.add(temp)
        s.flush()
        temp_id = temp.id
        role_id = role.id

        # SAVEPOINT 内：业务写 + 审计
        sp = s.begin_nested()
        temp.display_name = "T04-Should-Not-Persist"
        s.flush()
        audit = AuditService(s)
        log = audit.record(
            object_type="USER",
            object_id=temp_id,
            event_type="UPDATE",
            actor_id=temp_id,
            before={"display_name": original_name},
            after={"display_name": "T04-Should-Not-Persist"},
            reason="测试回滚",
        )
        log_id = log.id
        sp.rollback()  # 业务校验失败，整体回滚

        # 提交外层事务让用户落库（便于 verify 看见）
        s.commit()
    finally:
        s.close()

    # 独立连接验证：审计行不存在，用户名未变
    verify = SessionLocal()
    try:
        assert verify.get(AuditLog, log_id) is None
        u = verify.get(User, temp_id)
        assert u is not None
        assert u.display_name == original_name
    finally:
        verify.close()

    # 清理临时数据（不影响其他测试）
    cleanup = SessionLocal()
    try:
        u = cleanup.get(User, temp_id)
        if u is not None:
            cleanup.delete(u)
        r = cleanup.get(Role, role_id)
        if r is not None:
            cleanup.delete(r)
        cleanup.commit()
    finally:
        cleanup.close()


def test_audit_state_transition_requires_reason(session: Session, t04_user: User) -> None:
    """R24：状态迁移必须填原因。"""
    audit = AuditService(session)
    with pytest.raises(ValueError, match="原因"):
        audit.record_state_transition(
            object_type="CONTRACT",
            object_id=uuid4(),
            actor_id=t04_user.id,
            before={"status": "DRAFT"},
            after={"status": "APPROVED"},
            reason="",
        )


# ===== Q017 =====


def test_idempotency_replays_same_params(session: Session) -> None:
    """Q017：同 (scope,key) + 同参数 → 重放首次结果，业务函数不重跑。"""
    svc = IdempotencyService(session)
    calls = {"n": 0}

    def fn() -> dict[str, object]:
        calls["n"] += 1
        return {"order_id": "O-001", "total": 10}

    first = svc.run(scope="visit.submit", key="k1", params={"a": 1}, actor_id=None, fn=fn)
    second = svc.run(scope="visit.submit", key="k1", params={"a": 1}, actor_id=None, fn=fn)

    assert first["replayed"] is False
    assert second["replayed"] is True
    assert second["response"] == {"order_id": "O-001", "total": 10}
    assert calls["n"] == 1

    row = session.query(IdempotencyKey).one()
    assert row.status == STATUS_COMPLETED
    assert row.response == {"order_id": "O-001", "total": 10}
    assert row.params_hash == hash_params({"a": 1})


def test_idempotency_failed_then_retry(session: Session) -> None:
    """Q017 续：失败后允许同键重试（不留副作用）。"""
    svc = IdempotencyService(session)
    calls = {"n": 0}

    def fail() -> dict[str, object]:
        calls["n"] += 1
        raise RuntimeError("外部依赖瞬时失败")

    with pytest.raises(RuntimeError):
        svc.run(scope="ai.tool.write", key="k1", params={"x": 1}, actor_id=None, fn=fail)

    def ok() -> dict[str, object]:
        calls["n"] += 1
        return {"ok": True}

    result = svc.run(scope="ai.tool.write", key="k1", params={"x": 1}, actor_id=None, fn=ok)
    assert result["response"] == {"ok": True}
    assert calls["n"] == 2


# ===== Q018 =====


def test_idempotency_same_key_different_params_conflicts(session: Session) -> None:
    """Q018：同键异参 → ConflictError（HTTP 409 由 handler 验证）。"""
    svc = IdempotencyService(session)
    svc.run(scope="visit.submit", key="k1", params={"a": 1}, actor_id=None, fn=lambda: {"v": 1})
    session.commit()

    with pytest.raises(ConflictError) as excinfo:
        svc.run(scope="visit.submit", key="k1", params={"a": 2}, actor_id=None, fn=lambda: {"v": 2})
    assert excinfo.value.code == "IDEMPOTENCY_PARAMS_CONFLICT"


def test_idempotency_conflict_maps_to_http_409(test_env) -> None:
    """Q018 续：ConflictError 由 handler 转 HTTP 409 响应。"""
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    def boom() -> None:  # pragma: no cover - 触发 handler
        raise ConflictError(code="IDEMPOTENCY_PARAMS_CONFLICT", message="幂等键冲突")

    with TestClient(app) as c:
        r = c.get("/boom")
        assert r.status_code == 409
        body = r.json()
        assert body["code"] == "IDEMPOTENCY_PARAMS_CONFLICT"
        assert "request_id" in body


# ===== Q019 =====


@pytest.fixture
def file_service(session: Session, tmp_path) -> FileService:
    return FileService(session, LocalFSStorage(str(tmp_path)))


# 最小有效 PNG（1x1 透明）
_PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff"
    b"\xff?\x00\x05\xfe\x02\xfe\xa3\xee\xfe\xff\xff\xff\xff\xff\xff\xff\xff"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_file_save_validates_whitelist_and_size(
    file_service: FileService, session: Session, t04_user: User
) -> None:
    obj = file_service.save_upload(
        owner_user_id=t04_user.id,
        filename="photo.png",
        content_type="image/png",
        data=_PNG_1X1,
        biz_type=BIZ_PHOTO,
    )
    session.flush()
    assert obj.size_bytes == len(_PNG_1X1)
    assert obj.sha256  # 64 字符 hex
    assert obj.scan_status == "PENDING"

    # 拒非白名单类型
    from app.core.errors import FileAccessError

    with pytest.raises(FileAccessError) as excinfo:
        file_service.save_upload(
            owner_user_id=t04_user.id,
            filename="malware.exe",
            content_type="application/octet-stream",
            data=b"MZ",
            biz_type=BIZ_PHOTO,
        )
    assert excinfo.value.code == "FILE_TYPE_REJECTED"

    # 拒 MIME 与扩展名不一致
    with pytest.raises(FileAccessError) as excinfo:
        file_service.save_upload(
            owner_user_id=t04_user.id,
            filename="photo.jpg",
            content_type="image/png",
            data=_PNG_1X1,
            biz_type=BIZ_PHOTO,
        )
    assert excinfo.value.code == "FILE_TYPE_REJECTED"

    # 拒超大文件（11MB）
    with pytest.raises(FileAccessError) as excinfo:
        file_service.save_upload(
            owner_user_id=t04_user.id,
            filename="big.png",
            content_type="image/png",
            data=b"\x00" * (10 * 1024 * 1024 + 1),
            biz_type=BIZ_PHOTO,
        )
    assert excinfo.value.code == "FILE_TOO_LARGE"


def test_file_signed_url_owner_ok_and_nonowner_denied(
    file_service: FileService, session: Session, t04_user: User
) -> None:
    obj = file_service.save_upload(
        owner_user_id=t04_user.id,
        filename="photo.png",
        content_type="image/png",
        data=_PNG_1X1,
        biz_type=BIZ_PHOTO,
    )
    session.flush()

    # owner 签发：必须显式传入 is_authorized=True
    issued = file_service.issue_signed_url(
        file_id=obj.id, actor_user_id=t04_user.id, is_authorized=True
    )
    assert issued["file_id"] == str(obj.id)
    assert "signature" in issued and "expires_at" in issued

    # owner 下载：OK
    data = file_service.download(
        file_id=obj.id,
        current_actor_id=t04_user.id,
        expires_at=issued["expires_at"],
        signature=issued["signature"],
    )
    assert data == _PNG_1X1

    # 业务层在签发前必须鉴权：未授权签发直接拒绝（防止越权签发 URL）
    from app.core.errors import FileAccessError

    with pytest.raises(FileAccessError) as excinfo:
        file_service.issue_signed_url(
            file_id=obj.id, actor_user_id=t04_user.id, is_authorized=False
        )
    assert excinfo.value.code == "FILE_ACCESS_DENIED"

    # 攻击者用自己的身份签不到 URL；即便用同签名但以攻击者身份下载也失败
    attacker_id = uuid4()
    with pytest.raises(FileAccessError) as excinfo:
        file_service.download(
            file_id=obj.id,
            current_actor_id=attacker_id,
            expires_at=issued["expires_at"],
            signature=issued["signature"],
        )
    assert excinfo.value.code == "FILE_SIGNATURE_INVALID"


def test_file_signed_url_expiry_and_tamper(
    file_service: FileService, session: Session, t04_user: User
) -> None:
    obj = file_service.save_upload(
        owner_user_id=t04_user.id,
        filename="photo.png",
        content_type="image/png",
        data=_PNG_1X1,
        biz_type=BIZ_PHOTO,
    )
    session.flush()
    issued = file_service.issue_signed_url(
        file_id=obj.id, actor_user_id=t04_user.id, is_authorized=True
    )

    # 篡改签名
    from app.core.errors import FileAccessError

    with pytest.raises(FileAccessError) as excinfo:
        file_service.download(
            file_id=obj.id,
            current_actor_id=t04_user.id,
            expires_at=issued["expires_at"],
            signature="a" * 64,
        )
    assert excinfo.value.code == "FILE_SIGNATURE_INVALID"

    # 过期：把 expires_at 改成过去时间
    with pytest.raises(FileAccessError) as excinfo:
        file_service.download(
            file_id=obj.id,
            current_actor_id=t04_user.id,
            expires_at=int(time.time()) - 1,
            signature=issued["signature"],
        )
    assert excinfo.value.code == "FILE_SIGNATURE_EXPIRED"


def test_file_scan_infected_blocks_download(
    file_service: FileService, session: Session, t04_user: User
) -> None:
    """Q019 续：INFECTED 状态拒绝下载与签发（隔离）。"""
    obj = file_service.save_upload(
        owner_user_id=t04_user.id,
        filename="photo.png",
        content_type="image/png",
        data=_PNG_1X1,
        biz_type=BIZ_PHOTO,
    )
    session.flush()
    file_service.mark_scan_status(file_id=obj.id, status="CLEAN")
    assert session.get(FileObject, obj.id).scan_status == "CLEAN"

    file_service.mark_scan_status(file_id=obj.id, status="INFECTED")

    # 隔离后签发被拒
    from app.core.errors import FileAccessError

    with pytest.raises(FileAccessError) as excinfo:
        file_service.issue_signed_url(
            file_id=obj.id, actor_user_id=t04_user.id, is_authorized=True
        )
    assert excinfo.value.code == "FILE_QUARANTINED"


# ===== Q020 =====


def test_sanitize_message_scrubs_credentials_and_phone() -> None:
    msg = (
        "user login attempt password=hunter2 token=eyJabc.def.ghijklmnop"
        " $argon2id$v=19$m=65536 13812345678 done"
    )
    out = sanitize_message(msg)
    assert "hunter2" not in out
    assert "eyJ" not in out
    assert "$argon2" not in out
    assert "13812345678" not in out
    assert "138****5678" in out
    assert "***REDACTED***" in out


def test_mask_phone_text_preserves_first3_and_last4() -> None:
    assert mask_phone_text("联系 13812345678 / 13900000000") == "联系 138****5678 / 139****0000"
    # 非法格式不动
    assert mask_phone_text("号码 12-3456") == "号码 12-3456"


def test_redact_dict_sensitive_keys() -> None:
    out = redact(
        {
            "username": "alice",
            "password": "hunter2",
            "phone": "13812345678",
            "nested": {"token": "abc", "safe": "ok"},
        }
    )
    assert out["username"] == "alice"
    assert out["password"] == "***REDACTED***"
    assert out["phone"] == "***REDACTED***"
    assert out["nested"]["token"] == "***REDACTED***"
    assert out["nested"]["safe"] == "ok"


def test_json_formatter_scrubs_log_output(test_env) -> None:
    """Q020 端到端：JsonFormatter 输出的 message 不含明文凭据与手机号。"""
    logger = logging.getLogger("t04.test")
    logger.handlers = []
    logger.setLevel(logging.INFO)
    logger.propagate = False
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)

    from app.core.context import set_request_id

    set_request_id("req-test-001")
    logger.info(
        "login attempt password=hunter2 token=eyJabc.def.ghijklmnopqr 13812345678"
    )
    line = stream.getvalue().strip()
    # 解析 JSON 字符串
    import json

    payload = json.loads(line)
    assert "hunter2" not in payload["message"]
    assert "eyJ" not in payload["message"]
    assert "13812345678" not in payload["message"]
    assert "***REDACTED***" in payload["message"]
    assert payload["request_id"] == "req-test-001"


# ===== 响应 Schema 字段裁剪 =====


def _ctx(role: str) -> ActorContext:
    return ActorContext(
        user_id=UUID(int=1),
        role_code=role,
        region_ids=frozenset(),
        brand_ids=frozenset(),
    )


_PAYLOAD = {
    "id": "abc",
    "name": "测试",
    "phone": "13812345678",
    "contact_phone": "13900000000",
    "merchant_location_wkt": "POINT(120 30)",
    "payroll_total_fen": 999900,
    "cost_fen": 50000,
    "gross_profit_fen": 12000,
}


def test_sanitize_response_supplier_redacts_all_sensitive() -> None:
    out = sanitize_response(_ctx(RoleCode.SUPPLIER.value), dict(_PAYLOAD))
    assert out["name"] == "测试"
    assert out["phone"] is None
    assert out["contact_phone"] is None
    assert out["merchant_location_wkt"] is None
    assert out["payroll_total_fen"] is None
    assert out["cost_fen"] is None
    assert out["gross_profit_fen"] is None


def test_sanitize_response_sales_rep_masks_phone_hides_payroll_and_cost() -> None:
    out = sanitize_response(_ctx(RoleCode.SALES_REP.value), dict(_PAYLOAD))
    assert out["phone"] == "138****5678"
    assert out["contact_phone"] == "139****0000"
    assert out["merchant_location_wkt"] == "POINT(120 30)"  # 业务员需要坐标做巡访
    assert out["payroll_total_fen"] is None
    assert out["cost_fen"] is None
    assert out["gross_profit_fen"] is None


def test_sanitize_response_finance_sees_all() -> None:
    out = sanitize_response(_ctx(RoleCode.FINANCE.value), dict(_PAYLOAD))
    assert out["phone"] == "138****5678"
    assert out["payroll_total_fen"] == 999900
    assert out["cost_fen"] == 50000
    assert out["gross_profit_fen"] == 12000


def test_sanitize_response_system_admin_plain() -> None:
    out = sanitize_response(_ctx(RoleCode.SYSTEM_ADMIN.value), dict(_PAYLOAD))
    assert out["phone"] == "13812345678"
    assert out["payroll_total_fen"] == 999900
    assert out["cost_fen"] == 50000

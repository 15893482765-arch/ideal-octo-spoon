"""T29 Q168 签名 URL 过期与对象存储隔离；Q171 备份恢复；Q172 故障时核心业务可用。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.services.backup_service import (
    BackupRecord,
    BackupService,
    RunnerResult,
    build_dump_command,
    evaluate_rpo,
    evaluate_rto,
    select_backups_to_delete,
    verify_archive,
)
from app.services.file_service import BIZ_PHOTO, FileAccessError, FileService
from app.services.storage import (
    LocalFSStorage,
    S3Storage,
    object_key_for,
)

# ---------------------------------------------------------------------------
# Q168 签名 URL 过期 / 数据集隔离 / 对象键租户隔离 / S3 预签名过期
# ---------------------------------------------------------------------------


class _FakeS3Client:
    def __init__(self) -> None:
        self.presign_calls: list[dict] = []

    def generate_presigned_url(self, op: str, Params: dict, ExpiresIn: int) -> str:  # noqa: N803
        self.presign_calls.append({"op": op, "params": Params, "expires": ExpiresIn})
        key = Params["Key"]
        return f"https://s3.example/{Params['Bucket']}/{key}?ExpiresIn={ExpiresIn}&sig=abc"

    # 其余方法本测试不触发，提供最小桩
    def put_object(self, *a, **k):  # noqa: ANN002, ANN003, ARG002
        return None

    def get_object(self, *a, **k):  # noqa: ANN002, ANN003, ARG002
        return {"Body": None}

    def delete_object(self, *a, **k):  # noqa: ANN002, ANN003, ARG002
        return None


def test_q168_s3_presigned_url_is_short_lived_and_scoped():
    client = _FakeS3Client()
    storage = S3Storage(client, bucket="qinshuang-prod")
    url = storage.presign_get("PRODUCTION/ds1/PHOTO/u/f.jpg", expires_in=300)
    assert "ExpiresIn=300" in url
    call = client.presign_calls[0]
    assert call["expires"] == 300  # 5 分钟短期
    assert call["params"]["Bucket"] == "qinshuang-prod"


def test_q168_object_key_tenant_isolation_segregates_demo_and_production():
    demo_key = object_key_for("DEMO", "ds-demo", "PHOTO", "uuid-1", "door.jpg")
    prod_key = object_key_for("PRODUCTION", "ds-prod", "PHOTO", "uuid-2", "door.jpg")
    assert demo_key.startswith("DEMO/ds-demo/photo/")
    assert prod_key.startswith("PRODUCTION/ds-prod/photo/")
    # 物理前缀隔离：演示数据不会落到生产前缀
    assert not demo_key.startswith("PRODUCTION/")
    assert demo_key.split("/")[0] != prod_key.split("/")[0]


def test_q168_signed_download_expired_rejected(
    file_service: FileService, session: Session, t29_user
) -> None:
    obj = file_service.save_upload(
        owner_user_id=t29_user.id,
        filename="p.png",
        content_type="image/png",
        data=_PNG,
        biz_type=BIZ_PHOTO,
        dataset_type="PRODUCTION",
    )
    issued = file_service.issue_signed_url(
        file_id=obj.id, actor_user_id=t29_user.id, is_authorized=True
    )
    # 过期之后下载 → 拒绝（expires_at 之后 1 秒）
    future = int(issued["expires_at"]) + 1
    with pytest.raises(FileAccessError) as exc:
        file_service.download(
            file_id=obj.id,
            current_actor_id=t29_user.id,
            expires_at=issued["expires_at"],
            signature=issued["signature"],
            now_seconds=future,
        )
    assert exc.value.code == "FILE_SIGNATURE_EXPIRED"


def test_q168_dataset_whitelist_blocks_demo_file_from_production_path(
    file_service: FileService, session: Session, t29_user
) -> None:
    demo = file_service.save_upload(
        owner_user_id=t29_user.id,
        filename="d.png",
        content_type="image/png",
        data=_PNG,
        biz_type=BIZ_PHOTO,
        dataset_type="DEMO",
    )
    issued = file_service.issue_signed_url(
        file_id=demo.id, actor_user_id=t29_user.id, is_authorized=True
    )
    # 正式下载/导出口径只允许 PRODUCTION：DEMO 文件被拒（红线：演示数据不进正式口径）
    with pytest.raises(FileAccessError) as exc:
        file_service.download(
            file_id=demo.id,
            current_actor_id=t29_user.id,
            expires_at=issued["expires_at"],
            signature=issued["signature"],
            allowed_dataset_types={"PRODUCTION"},
        )
    assert exc.value.code == "FILE_DATASET_FORBIDDEN"


# ---------------------------------------------------------------------------
# Q171 备份 / 恢复 / 保留 / RPO / RTO
# ---------------------------------------------------------------------------


class _FakeRunner:
    """内存假执行器：dump 产出可解压 SQL；restore 校验输入能被解压。"""

    def __init__(self, *, dump_code: int = 0, restore_code: int = 0) -> None:
        self.dump_code = dump_code
        self.restore_code = restore_code
        self.restored: list[bytes] = []
        self.env_seen: list[dict] = []

    def dump(self, argv, env):  # noqa: ANN001
        self.env_seen.append(dict(env))
        if self.dump_code != 0:
            return RunnerResult(self.dump_code, b"", b"boom")
        return RunnerResult(0, b"-- fake sql dump\nCREATE TABLE t(x int);\n", b"")

    def restore(self, argv, env, archive_bytes):  # noqa: ANN001
        import gzip

        self.env_seen.append(dict(env))
        try:
            self.restored.append(gzip.decompress(archive_bytes))
        except OSError:
            return RunnerResult(1, b"", b"bad archive")
        return RunnerResult(self.restore_code, b"", b"")


def _service(tmp_path: Path, runner: _FakeRunner, now=None) -> BackupService:
    return BackupService(
        database_url="postgresql://app:secret@db:5432/qinshuang",
        backup_dir=tmp_path / "backups",
        runner=runner,
        clock=now or (lambda: datetime(2026, 10, 15, 18, 0, tzinfo=UTC)),
    )


def test_q171_dump_command_never_puts_password_in_argv():
    argv, env = build_dump_command("postgresql://app:p@ss@db:5432/qinshuang")
    joined = " ".join(argv)
    assert "p@ss" not in joined  # 密码不进命令行（进程列表不可见）
    assert env["PGPASSWORD"] == "p@ss"  # 只走环境变量


def test_q171_backup_then_restore_roundtrip_and_manifest(tmp_path):
    runner = _FakeRunner()
    svc = _service(tmp_path, runner)
    record = svc.create_backup()
    assert record.size_bytes > 0
    assert (tmp_path / "backups" / record.filename).exists()

    # 恢复到目标库（演练），返回计时
    result = svc.restore_latest(target_database_url="postgresql://app:secret@dr:5432/qinshuang_dr")
    assert result["verified"] is True
    assert b"fake sql dump" in runner.restored[0]
    assert result["elapsed_seconds"] >= 0

    # 清单持久化
    again = BackupService(
        database_url="x", backup_dir=tmp_path / "backups", runner=runner,
    )
    assert again.load_set().latest().sha256 == record.sha256


def test_q171_retention_prunes_backups_older_than_30_days(tmp_path):
    runner = _FakeRunner()
    base = datetime(2026, 10, 15, tzinfo=UTC)
    clock = {"now": base}
    svc = _service(tmp_path, runner, now=lambda: clock["now"])

    # 31 天前的备份
    clock["now"] = base - timedelta(days=31)
    old = svc.create_backup()
    # 今天的备份
    clock["now"] = base
    new = svc.create_backup()

    deleted = svc.apply_retention(retention_days=30)
    assert old.filename in deleted
    assert new.filename not in deleted
    assert not (tmp_path / "backups" / old.filename).exists()
    assert (tmp_path / "backups" / new.filename).exists()


def test_q171_rpo_and_rto_evaluation():
    now = datetime(2026, 10, 15, tzinfo=UTC)
    fresh = BackupRecord("f.sql.gz", now - timedelta(hours=20), 1, "a", "db")
    stale = BackupRecord("s.sql.gz", now - timedelta(hours=30), 1, "b", "db")
    assert evaluate_rpo(fresh, now=now, rpo_hours=24) is True
    assert evaluate_rpo(stale, now=now, rpo_hours=24) is False
    assert evaluate_rpo(None, now=now, rpo_hours=24) is False
    # RTO 4h
    assert evaluate_rto(3 * 3600 + 59 * 60, rto_hours=4) is True
    assert evaluate_rto(4 * 3600 + 1, rto_hours=4) is False


def test_q171_corrupt_archive_blocks_restore(tmp_path):
    runner = _FakeRunner()
    svc = _service(tmp_path, runner)
    record = svc.create_backup()
    archive = tmp_path / "backups" / record.filename
    archive.write_bytes(b"not a gzip")  # 人为损坏
    with pytest.raises(Exception) as exc:  # noqa: PT011
        svc.restore_latest(target_database_url="postgresql://x")
    assert exc.value.code == "BACKUP_CORRUPT"
    assert verify_archive(archive, record.sha256) is False


def test_q171_select_retention_pure_logic():
    now = datetime(2026, 10, 15, tzinfo=UTC)
    records = [
        BackupRecord("new.gz", now - timedelta(days=2), 1, "n", "db"),
        BackupRecord("old.gz", now - timedelta(days=40), 1, "o", "db"),
    ]
    doomed = select_backups_to_delete(records, now=now, retention_days=30)
    assert doomed == {"old.gz"}


# ---------------------------------------------------------------------------
# Q172 故障时核心业务可用：备份失败不污染、依赖故障降级、恢复失败明确报错
# ---------------------------------------------------------------------------


def test_q172_failed_dump_raises_and_leaves_no_file(tmp_path):
    runner = _FakeRunner(dump_code=1)
    svc = _service(tmp_path, runner)
    with pytest.raises(Exception) as exc:  # noqa: PT011
        svc.create_backup()
    assert exc.value.code == "BACKUP_FAILED"
    # 不留半成品归档/清单
    assert list((tmp_path / "backups").glob("*.sql.gz")) == []
    assert svc.load_set().backups == []


def test_q172_restore_failure_raises_without_silent_success(tmp_path):
    runner = _FakeRunner(restore_code=1)
    svc = _service(tmp_path, runner)
    svc.create_backup()
    with pytest.raises(Exception) as exc:  # noqa: PT011
        svc.restore_latest(target_database_url="postgresql://x")
    assert exc.value.code == "RESTORE_FAILED"


def test_q172_metrics_recording_failure_does_not_break_request():
    """指标注册表即使遇到坏标签也不外抛（观测旁路不拖垮核心请求）。"""
    from app.core.metrics import registry as reg

    # 正常 inc 可用
    reg.counter("app_t29_probe_total", "", ()).inc()
    # safe_inc 走安全路径不抛
    reg.safe_inc("app_t29_probe_total", 1)
    assert reg.counter("app_t29_probe_total", "").value() == 2
    # 渲染不抛
    assert "app_t29_probe_total" in reg.render_prometheus()


# 最小 1x1 PNG
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6360000100000500010d0a2db40000000049454e44ae426082"
)


# ---------------------------------------------------------------------------
# fixtures（本文件专用，复用全局 session）
# ---------------------------------------------------------------------------


@pytest.fixture
def t29_user(session: Session):
    from uuid import uuid4

    from app.core.security import hash_password
    from app.models import Role, User, UserStatus
    from app.models.enums import RoleCode

    dsid = uuid4()
    role = Role(
        code=f"t29-role-{uuid4().hex[:6]}",
        name="T29 角色",
        role_code=RoleCode.SYSTEM_ADMIN.value,
        dataset_type="PRODUCTION",
        dataset_id=dsid,
    )
    session.add(role)
    session.flush()
    u = User(
        username=f"t29_user_{uuid4().hex[:8]}",
        display_name="T29 用户",
        role_id=role.id,
        status=UserStatus.ACTIVE.value,
        must_change_password=False,
        password_hash=hash_password("t29-pass-not-for-prod"),
        dataset_type="PRODUCTION",
        dataset_id=dsid,
    )
    session.add(u)
    session.flush()
    return u


@pytest.fixture
def file_service(session: Session, tmp_path) -> FileService:
    return FileService(session, LocalFSStorage(str(tmp_path)))

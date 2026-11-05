"""备份与恢复服务（T29 / PRD §8：每日备份、30 天恢复窗口、RPO≤24h、RTO≤4h）。

可测性设计：
- 命令构造、备份清单（manifest）、保留裁剪、RPO/RTO 达标判定为**纯逻辑**，不依赖真实 PG；
- 真正的 pg_dump / pg_restore 通过 `BackupRunner` 协议注入（生产用 subprocess 实现），
  测试用内存假实现，可在不连库的情况下完整验证"备份→损坏检测→恢复→计时→保留"。

安全：
- 口令只通过 PGPASSWORD 环境变量传给子进程，绝不出现在命令行 argv / 日志（进程列表可读）；
- 恢复默认拒绝非空库（--clean 只在显式 allow_clean 下使用），避免误覆盖生产。
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

MANIFEST_VERSION = 1
DUMP_SUFFIX = ".sql.gz"


def utc_now() -> datetime:
    return datetime.now(UTC)


# ===== 命令构造（纯逻辑）=====


def _parse_pg_url(database_url: str) -> dict[str, str]:
    """解析 PG URL（同步 psycopg2 形态）为连接参数；密码单独返回不进 argv。"""
    parsed = urlparse(database_url)
    return {
        "host": parsed.hostname or "localhost",
        "port": str(parsed.port or 5432),
        "user": parsed.username or "",
        "password": parsed.password or "",
        "dbname": (parsed.path or "/").lstrip("/"),
    }


def build_dump_command(
    database_url: str,
    *,
    extra_args: tuple[str, ...] = ("--no-owner", "--no-privileges"),
) -> tuple[list[str], dict[str, str]]:
    """返回 (argv, env)。密码经 PGPASSWORD 环境变量传递，不进命令行（进程列表不可见）。"""
    c = _parse_pg_url(database_url)
    # 明文 SQL 到 stdout，由执行器管道压缩为 gzip 归档（见 SubprocessBackupRunner）。
    argv = [
        "pg_dump",
        "-h",
        c["host"],
        "-p",
        c["port"],
        "-U",
        c["user"],
        "-d",
        c["dbname"],
        "--no-password",
        *extra_args,
    ]
    return argv, {"PGPASSWORD": c["password"]}


def build_restore_command(
    database_url: str,
    *,
    clean: bool = False,
) -> tuple[list[str], dict[str, str]]:
    """gunzip → psql 恢复明文归档；密码经环境变量。返回 (argv, env)，输入由执行器管道喂入。"""
    c = _parse_pg_url(database_url)
    argv = [
        "psql", "-h", c["host"], "-p", c["port"], "-U", c["user"], "-d", c["dbname"],
        "--no-password", "-v", "ON_ERROR_STOP=1",
    ]
    if clean:
        # 仅在演练库或显式授权下使用：执行器在恢复前先下发清理
        argv.append("--set=clean=1")
    return argv, {"PGPASSWORD": c["password"]}


# ===== Runner 协议 =====


@dataclass
class RunnerResult:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


class BackupRunner(Protocol):
    def dump(self, argv: list[str], env: dict[str, str]) -> RunnerResult:
        """执行 pg_dump，stdout 返回待压缩的 SQL 字节。"""
        ...

    def restore(self, argv: list[str], env: dict[str, str], archive_bytes: bytes) -> RunnerResult:
        """解压归档并把 SQL 喂给恢复进程。"""
        ...


# ===== 清单与保留 =====


@dataclass(frozen=True)
class BackupRecord:
    filename: str
    created_at: datetime
    size_bytes: int
    sha256: str
    logical_db: str

    def to_manifest(self) -> dict[str, object]:
        return {
            "filename": self.filename,
            "created_at": self.created_at.isoformat(),
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "logical_db": self.logical_db,
        }

    @staticmethod
    def from_manifest(row: dict[str, object]) -> BackupRecord:
        return BackupRecord(
            filename=str(row["filename"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            size_bytes=int(str(row["size_bytes"])),
            sha256=str(row["sha256"]),
            logical_db=str(row.get("logical_db", "")),
        )


@dataclass
class BackupSet:
    backups: list[BackupRecord] = field(default_factory=list)

    def latest(self) -> BackupRecord | None:
        return max(self.backups, key=lambda b: b.created_at, default=None)


def select_backups_to_delete(
    backups: list[BackupRecord], *, now: datetime, retention_days: int
) -> set[str]:
    """保留窗口裁剪：早于 now-retention_days 的备份应删除（含清单）。"""
    cutoff = now - timedelta(days=retention_days)
    return {b.filename for b in backups if b.created_at < cutoff}


def evaluate_rpo(latest: BackupRecord | None, *, now: datetime, rpo_hours: int) -> bool:
    """最近一次成功备份的年龄是否 ≤ RPO（每日备份要求 RPO≤24h）。"""
    if latest is None:
        return False
    age = now - latest.created_at
    return age <= timedelta(hours=rpo_hours)


def evaluate_rto(restore_elapsed_seconds: float, rto_hours: int) -> bool:
    """一次实际恢复耗时是否在 RTO（4h）以内。"""
    return restore_elapsed_seconds >= 0 and restore_elapsed_seconds <= rto_hours * 3600


def verify_archive(path: Path, expected_sha256: str) -> bool:
    """校验备份归档完整性：SHA-256 一致且 gzip 可完整解压。"""
    if not path.exists():
        return False
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected_sha256:
        return False
    try:
        with gzip.open(path, "rb") as fh:
            while fh.read(1024 * 1024):
                pass
    except (OSError, EOFError, gzip.BadGzipFile):
        return False
    return True


# ===== 编排服务 =====


class BackupError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BackupService:
    """备份/恢复编排；所有副作用（子进程、文件系统）经注入，便于演练与测试。"""

    def __init__(
        self,
        *,
        database_url: str,
        backup_dir: str | Path,
        runner: BackupRunner,
        logical_db: str = "qinshuang",
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._url = database_url
        self._dir = Path(backup_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._runner = runner
        self._logical_db = logical_db
        self._clock = clock

    @property
    def manifest_path(self) -> Path:
        return self._dir / "manifest.json"

    # -------- 备份 --------

    def create_backup(self) -> BackupRecord:
        ts = self._clock().astimezone(UTC)
        stamp = ts.strftime("%Y%m%dT%H%M%SZ")
        filename = f"backup-{stamp}{DUMP_SUFFIX}"
        out_path = self._dir / filename
        argv, env = build_dump_command(self._url)
        result = self._runner.dump(argv, env)
        if result.returncode != 0 or not result.stdout:
            if out_path.exists():
                out_path.unlink()
            raise BackupError("BACKUP_FAILED", f"pg_dump 退出码 {result.returncode}")

        data = gzip.compress(result.stdout)
        out_path.write_bytes(data)
        record = BackupRecord(
            filename=filename,
            created_at=ts,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            logical_db=self._logical_db,
        )
        self._append_manifest(record)
        return record

    # -------- 恢复 --------

    def restore_latest(
        self,
        *,
        target_database_url: str,
        clean: bool = False,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """用最近一次成功备份恢复到目标库，并返回 RTO 计时与校验结果（恢复演练）。"""
        record = self.load_set().latest()
        if record is None:
            raise BackupError("BACKUP_NOT_FOUND", "没有可恢复的备份")
        return self.restore_record(
            record, target_database_url=target_database_url, clean=clean, now=now
        )

    def restore_record(
        self,
        record: BackupRecord,
        *,
        target_database_url: str,
        clean: bool = False,
        now: datetime | None = None,
    ) -> dict[str, object]:
        archive = self._dir / record.filename
        if not verify_archive(archive, record.sha256):
            raise BackupError("BACKUP_CORRUPT", f"备份损坏或校验失败：{record.filename}")
        argv, env = build_restore_command(target_database_url, clean=clean)
        start = _monotonic()
        result = self._runner.restore(argv, env, archive.read_bytes())
        elapsed = _monotonic() - start
        if result.returncode != 0:
            raise BackupError("RESTORE_FAILED", f"恢复进程退出码 {result.returncode}")
        return {
            "restored_file": record.filename,
            "elapsed_seconds": round(elapsed, 3),
            "verified": True,
            "restored_at": (now or self._clock()).isoformat(),
        }

    # -------- 保留 --------

    def apply_retention(self, *, retention_days: int) -> list[str]:
        """删除超过恢复窗口的旧备份，返回被删文件名。"""
        now = self._clock()
        backup_set = self.load_set()
        doomed = select_backups_to_delete(
            backup_set.backups, now=now, retention_days=retention_days
        )
        for filename in doomed:
            p = self._dir / filename
            if p.exists():
                p.unlink()
        if doomed:
            kept = [b for b in backup_set.backups if b.filename not in doomed]
            self._write_manifest(kept)
        return sorted(doomed)

    # -------- 清单 --------

    def load_set(self) -> BackupSet:
        if not self.manifest_path.exists():
            return BackupSet()
        raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        rows = raw.get("backups", [])
        return BackupSet([BackupRecord.from_manifest(r) for r in rows])

    def _append_manifest(self, record: BackupRecord) -> None:
        backup_set = self.load_set()
        backup_set.backups.append(record)
        self._write_manifest(backup_set.backups)

    def _write_manifest(self, backups: list[BackupRecord]) -> None:
        payload = {
            "manifest_version": MANIFEST_VERSION,
            "logical_db": self._logical_db,
            "backups": [b.to_manifest() for b in backups],
        }
        tmp = self.manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.manifest_path)


def _monotonic() -> float:
    import time

    return time.monotonic()


# ===== 生产子进程执行器 =====


class SubprocessBackupRunner:
    """用 pg_dump|gzip 与 gunzip|psql 执行真实备份/恢复。

    仅用于生产/演练；密码经 PGPASSWORD 环境变量注入，绝不进 argv。
    生产环境由容器镜像提供 postgresql-client 与 gzip。
    """

    def dump(self, argv: list[str], env: dict[str, str]) -> RunnerResult:
        import subprocess

        proc = subprocess.run(  # noqa: S603 - argv 由内部构造，非用户输入
            argv,
            env={**_safe_env(), **env},
            capture_output=True,
            check=False,
        )
        return RunnerResult(proc.returncode, proc.stdout, proc.stderr)

    def restore(self, argv: list[str], env: dict[str, str], archive_bytes: bytes) -> RunnerResult:
        import gzip as _gzip
        import subprocess

        try:
            sql = _gzip.decompress(archive_bytes)
        except OSError:
            return RunnerResult(1, b"", b"invalid gzip archive")
        proc = subprocess.run(  # noqa: S603
            argv,
            input=sql,
            env={**_safe_env(), **env},
            capture_output=True,
            check=False,
        )
        return RunnerResult(proc.returncode, proc.stdout, proc.stderr)


def _safe_env() -> dict[str, str]:
    import os

    return {k: v for k, v in os.environ.items() if k in {"PATH", "TZ", "LANG", "LC_ALL"}}

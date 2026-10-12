"""对象存储后端（T04 / PRD §8：S3 兼容对象存储）。

- `StorageBackend` 协议：put / get / delete；
- `LocalFSStorage`：本地文件系统实现（开发与测试默认后端）；
- `S3Storage`：MinIO/云 OSS 实现（生产后端；本地无 MinIO 依赖环境，
  真实连接验证随 T09 合同附件 / T11 巡访照片接入时进行）。

红线：object_key 由 FileService 生成（含 UUID，永不覆盖），后端只认 key 读写；
不提供"按文件名覆盖"接口。
"""

from __future__ import annotations

from typing import Protocol

from app.core.errors import DomainError

_SAFE_KEY_RE_CHARS = "A-Za-z0-9._-"


def _check_key(object_key: str) -> None:
    """路径安全：禁止 ..、绝对路径、反斜杠与控制字符。"""
    if (
        not object_key
        or ".." in object_key
        or object_key.startswith("/")
        or "\\" in object_key
        or any(ord(c) < 32 for c in object_key)
        or "/" not in object_key
    ):
        raise DomainError(code="FILE_KEY_INVALID", message="对象键非法")


class StorageBackend(Protocol):
    def put(self, object_key: str, data: bytes) -> None: ...
    def get(self, object_key: str) -> bytes: ...
    def delete(self, object_key: str) -> None: ...


class LocalFSStorage:
    """本地文件系统后端：{root}/{object_key}。"""

    def __init__(self, root_dir: str) -> None:
        import pathlib

        self.root = pathlib.Path(root_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, object_key: str):

        _check_key(object_key)
        path = (self.root / object_key).resolve()
        if not str(path).startswith(str(self.root)):
            raise DomainError(code="FILE_KEY_INVALID", message="对象键非法")
        return path

    def put(self, object_key: str, data: bytes) -> None:
        path = self._path(object_key)
        if path.exists():
            raise DomainError(
                code="FILE_KEY_EXISTS", message="对象键已存在，禁止覆盖"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, object_key: str) -> bytes:
        path = self._path(object_key)
        if not path.exists():
            raise DomainError(code="FILE_NOT_FOUND", message="文件不存在")
        return path.read_bytes()

    def delete(self, object_key: str) -> None:
        path = self._path(object_key)
        if path.exists():
            path.unlink()


class S3Storage:
    """S3 兼容后端（MinIO/云 OSS）。

    生产后端；本地开发环境未起 MinIO，本实现的连接级验证推迟到
    T09（合同附件）/ T11（巡访照片）接入时以真实容器完成。
    """

    def __init__(self) -> None:
        import boto3  # 延迟导入：仅生产环境需要

        from app.core.config import get_settings

        settings = get_settings()
        self._bucket = settings.s3_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key.get_secret_value(),
            aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
        )

    def _check(self, object_key: str) -> None:
        _check_key(object_key)

    def put(self, object_key: str, data: bytes) -> None:
        self._check(object_key)
        self._client.put_object(Bucket=self._bucket, Key=object_key, Body=data)

    def get(self, object_key: str) -> bytes:
        self._check(object_key)
        resp = self._client.get_object(Bucket=self._bucket, Key=object_key)
        return resp["Body"].read()

    def delete(self, object_key: str) -> None:
        self._check(object_key)
        self._client.delete_object(Bucket=self._bucket, Key=object_key)


def get_storage_backend() -> StorageBackend:
    """按配置选择存储后端（local / s3）。"""
    from app.core.config import get_settings

    backend = get_settings().storage_backend
    if backend == "s3":
        return S3Storage()
    return LocalFSStorage(get_settings().local_storage_dir)

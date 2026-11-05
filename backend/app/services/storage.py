"""对象存储后端（T04 / PRD §8：S3 兼容对象存储）。

- `StorageBackend` 协议：put / get / delete；
- `LocalFSStorage`：本地文件系统实现（开发与测试默认后端）；
- `S3Storage`：MinIO/云 OSS 实现（生产后端；本地无 MinIO 依赖环境，
  真实连接验证随 T09 合同附件 / T11 巡访照片接入时进行）。

红线：object_key 由 FileService 生成（含 UUID，永不覆盖），后端只认 key 读写；
不提供"按文件名覆盖"接口。
"""

from __future__ import annotations

from typing import Any, Protocol

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

    def presign_get(self, object_key: str, expires_in: int) -> str:
        """生成短期预签名下载 URL。本地后端无法预签名，抛 NotImplementedError。"""
        ...


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

    def presign_get(self, object_key: str, expires_in: int) -> str:
        # 本地后端通过 FileService 的 HMAC 签名下载端点访问，不生成云厂商预签名。
        raise NotImplementedError("local storage does not support presigned URLs")


class S3Storage:
    """S3 兼容后端（MinIO/云 OSS）。

    生产后端；本地开发环境未起 MinIO，本实现的连接级验证推迟到
    T09（合同附件）/ T11（巡访照片）接入时以真实容器完成。
    """

    def __init__(self, client: Any | None = None, *, bucket: str | None = None) -> None:
        from app.core.config import get_settings

        settings = get_settings()
        self._bucket = bucket or settings.s3_bucket
        if client is not None:
            # 测试 / 注入式客户端
            self._client = client
            return
        import boto3  # 延迟导入：仅生产环境需要

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

    def presign_get(self, object_key: str, expires_in: int) -> str:
        """生成 S3 预签名 GET URL（短期、只读）。

        过期后对象存储直接拒绝（与 FileService 的 HMAC 下载签名双重短期化）。
        """
        self._check(object_key)
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": object_key},
            ExpiresIn=expires_in,
        )


def object_key_for(
    dataset_type: str, dataset_id: str, biz_type: str, file_uuid: str, filename: str
) -> str:
    """生成带**租户/数据集隔离前缀**的对象键（Q168 对象存储隔离）。

    key 形如 `{dataset_type}/{dataset_id}/{biz_type}/{uuid}/{file}`，
    使不同数据集（DEMO/PRODUCTION、不同企业）物理路径隔离；
    下游可据此在 S3/MinIO 配置 bucket 前缀策略，DEMO 数据永不与生产混用。
    """
    import re

    safe_ds_type = re.sub(r"[^A-Za-z0-9._-]", "", dataset_type).upper() or "PRODUCTION"
    safe_biz = re.sub(r"[^A-Za-z0-9._-]", "", biz_type).lower() or "other"
    safe_name = re.sub(r"[^A-Za-z0-9._\u4e00-\u9fff-]+", "_", filename)[:120] or "file"
    return f"{safe_ds_type}/{dataset_id}/{safe_biz}/{file_uuid}/{safe_name}"


def get_storage_backend() -> StorageBackend:
    """按配置选择存储后端（local / s3）。"""
    from app.core.config import get_settings

    backend = get_settings().storage_backend
    if backend == "s3":
        return S3Storage()
    return LocalFSStorage(get_settings().local_storage_dir)

"""FileService（T04 / PRD §8、T04 卡红线）。

能力边界：
- 只接受白名单 MIME 类型 + 大小上限；
- 落存前计算 SHA-256（防篡改、可校验）；
- 下载走**短期签名 URL**（HMAC 绑定 file_id + 授权人 + 过期时间）；
- **访问时再次鉴权**：签名校验通过后仍要重查访问人身份与文件归属；
- 预留病毒扫描状态（scan_status）；INFECTED 一律拒绝下载（隔离）。

红线：原始定位/照片不可覆盖 —— 本服务不提供覆盖/改名接口，
object_key 含 UUID 且 `StorageBackend.put` 拒绝已存在的 key。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import FileAccessError
from app.models import FileObject
from app.services.storage import StorageBackend

BIZ_CONTRACT_ATTACHMENT = "CONTRACT_ATTACHMENT"
BIZ_PHOTO = "PHOTO"
BIZ_PROOF_DOC = "PROOF_DOC"
BIZ_OTHER = "OTHER"

_ALLOWED_BIZ_TYPES = frozenset({BIZ_CONTRACT_ATTACHMENT, BIZ_PHOTO, BIZ_PROOF_DOC, BIZ_OTHER})

_FILENAME_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._\u4e00-\u9fff-]+")

# 扩展名 → MIME 允许映射（白名单之外的扩展名即使伪造 MIME 也拒绝）
_EXT_ALLOWED = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}


def _sign(secret: bytes, message: str) -> str:
    return hmac.new(secret, message.encode("utf-8"), hashlib.sha256).hexdigest()


class FileService:
    def __init__(self, session: Session, storage: StorageBackend) -> None:
        self.session = session
        self.storage = storage

    # -------- 上传 --------

    def save_upload(
        self,
        *,
        owner_user_id: UUID,
        filename: str,
        content_type: str,
        data: bytes,
        biz_type: str,
        dataset_type: str = "PRODUCTION",
        dataset_id: UUID | None = None,
    ) -> FileObject:
        """校验 + 落存 + 登记元数据。

        - MIME/扩展名双白名单；大小上限；
        - SHA-256 落列；object_key = {biz_type}/{uuid}/{安全文件名}；
        - scan_status 由调用方决定初始值：未启用扫描的环境直接置 SKIPPED，
          启用后保持 PENDING 等扫描回调（附录 A：真实扫描不在首版）。
        """
        settings = get_settings()
        whitelist = {t.strip().lower() for t in settings.file_upload_whitelist.split(",")}
        ct = (content_type or "").split(";")[0].strip().lower()
        if ct not in whitelist:
            raise FileAccessError(
                code="FILE_TYPE_REJECTED",
                message="文件类型不在允许范围",
                details={"allowed": sorted(whitelist)},
            )
        ext = (
            "." + _FILENAME_SANITIZE_RE.sub("", filename.rsplit(".", 1)[-1].lower())
            if "." in filename
            else ""
        )
        if ext not in _EXT_ALLOWED:
            raise FileAccessError(
                code="FILE_TYPE_REJECTED",
                message="文件扩展名不在允许范围",
                details={"allowed": sorted(_EXT_ALLOWED)},
            )
        # MIME 与扩展名必须一致，防止伪造头绕过白名单
        if _EXT_ALLOWED[ext] != ct:
            raise FileAccessError(
                code="FILE_TYPE_REJECTED",
                message="文件类型与扩展名不一致",
            )
        if len(data) > settings.file_max_size_bytes:
            raise FileAccessError(
                code="FILE_TOO_LARGE",
                message="文件超过大小上限",
                details={"max_bytes": settings.file_max_size_bytes},
            )
        if biz_type not in _ALLOWED_BIZ_TYPES:
            raise FileAccessError(code="FILE_BIZ_TYPE_INVALID", message="业务类型非法")
        if not data:
            raise FileAccessError(code="FILE_EMPTY", message="文件内容为空")

        safe_name = _FILENAME_SANITIZE_RE.sub("_", filename)[:180] or "file"
        object_key = f"{biz_type.lower()}/{uuid4()}/{safe_name}"
        sha256 = hashlib.sha256(data).hexdigest()

        self.storage.put(object_key, data)
        obj = FileObject(
            object_key=object_key,
            original_filename=filename[:255],
            content_type=ct,
            size_bytes=len(data),
            sha256=sha256,
            biz_type=biz_type,
            owner_user_id=owner_user_id,
            scan_status="PENDING",
            dataset_type=dataset_type,
            dataset_id=dataset_id or uuid4(),
        )
        self.session.add(obj)
        self.session.flush()
        return obj

    # -------- 病毒扫描状态（预留）--------

    def mark_scan_status(self, *, file_id: UUID, status: str) -> None:
        """扫描器回调入口：PENDING → CLEAN / INFECTED；未启用扫描时置 SKIPPED。"""
        if status not in {"PENDING", "CLEAN", "INFECTED", "SKIPPED"}:
            raise FileAccessError(code="FILE_SCAN_STATUS_INVALID", message="扫描状态非法")
        obj = self.session.get(FileObject, file_id)
        if obj is None:
            raise FileAccessError(code="FILE_NOT_FOUND", message="文件不存在")
        obj.scan_status = status
        self.session.flush()

    # -------- 签名 URL --------

    def issue_signed_url(
        self,
        *,
        file_id: UUID,
        actor_user_id: UUID,
        is_authorized: bool,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        """为**已通过权限校验**的访问者签发短期下载 URL。

        调用方（Service 层）必须先完成业务级行级鉴权并把结果传入 is_authorized；
        本方法只对文件本体做存在性/隔离校验。返回结构供路由拼装：
        {"file_id", "actor_user_id", "expires_at"(epoch 秒), "signature", "url_path"}
        """
        if not is_authorized:
            raise FileAccessError(code="FILE_ACCESS_DENIED", message="无权访问该文件")
        obj = self.session.get(FileObject, file_id)
        if obj is None:
            raise FileAccessError(code="FILE_NOT_FOUND", message="文件不存在")
        if obj.scan_status == "INFECTED":
            raise FileAccessError(code="FILE_QUARANTINED", message="文件已被隔离，禁止下载")

        settings = get_settings()
        ttl = ttl_seconds or settings.file_signed_url_ttl_seconds
        expires_at = int(time.time()) + ttl
        signature = _sign(
            settings.secret_key.get_secret_value().encode(),
            f"file:{file_id}:actor:{actor_user_id}:exp:{expires_at}",
        )
        path = (
            f"/api/files/{file_id}/download"
            f"?actor_id={actor_user_id}&expires_at={expires_at}&signature={signature}"
        )
        return {
            "file_id": str(file_id),
            "actor_user_id": str(actor_user_id),
            "expires_at": expires_at,
            "signature": signature,
            "url_path": path,
        }

    # -------- 下载（访问时再次鉴权）--------

    def download(
        self,
        *,
        file_id: UUID,
        current_actor_id: UUID,
        expires_at: int,
        signature: str,
        owner_check: Any = None,
        allowed_dataset_types: Any = None,
        now_seconds: int | None = None,
    ) -> bytes:
        """凭签名 URL 下载：签名 + 有效期 + 访问人身份 + 数据集隔离 + 文件状态 多重校验。

        - 签名绑定的 actor 必须与当前访问人一致（防止 URL 转借越权）；
        - owner_check：可选回调 `Callable(actor_id, file) -> bool`，
          由业务层注入行级规则（如"照片拍摄者或片区经理"）；返回 False 即拒绝；
        - allowed_dataset_types：可选可迭代数据集白名单（如生产口只允许 PRODUCTION，
          DEMO 数据不得进入正式下载/导出口径）；不传则不按数据集限制；
        - INFECTED 拒绝；内容取回后校验 SHA-256 一致性。
        """
        settings = get_settings()
        current_ts = int(now_seconds if now_seconds is not None else time.time())
        # 先看过期（用户可读到的更具体原因），再验签名
        if current_ts > expires_at:
            raise FileAccessError(code="FILE_SIGNATURE_EXPIRED", message="下载链接已过期")
        expected = _sign(
            settings.secret_key.get_secret_value().encode(),
            f"file:{file_id}:actor:{current_actor_id}:exp:{expires_at}",
        )
        if not hmac.compare_digest(expected, signature or ""):
            raise FileAccessError(
                code="FILE_SIGNATURE_INVALID", message="下载签名无效"
            )

        obj = self.session.get(FileObject, file_id)
        if obj is None:
            raise FileAccessError(code="FILE_NOT_FOUND", message="文件不存在")
        allowed = None if allowed_dataset_types is None else set(allowed_dataset_types)
        if allowed is not None and obj.dataset_type not in allowed:
            # 数据集隔离：演示数据不得经正式下载/导出路径流出
            raise FileAccessError(
                code="FILE_DATASET_FORBIDDEN", message="该文件不属于允许的数据范围"
            )
        if obj.owner_user_id != current_actor_id and owner_check is None:
            raise FileAccessError(code="FILE_ACCESS_DENIED", message="无权访问该文件")
        if owner_check is not None and not owner_check(current_actor_id, obj):
            raise FileAccessError(code="FILE_ACCESS_DENIED", message="无权访问该文件")
        if obj.scan_status == "INFECTED":
            raise FileAccessError(code="FILE_QUARANTINED", message="文件已被隔离，禁止下载")

        data = self.storage.get(obj.object_key)
        if hashlib.sha256(data).hexdigest() != obj.sha256:
            raise FileAccessError(
                code="FILE_INTEGRITY_BROKEN", message="文件内容校验失败"
            )
        return data

    # -------- 摘要（供日志/审计引用；已脱敏）--------

    @staticmethod
    def describe(obj: FileObject) -> dict[str, Any]:
        return {
            "file_id": str(obj.id),
            "biz_type": obj.biz_type,
            "content_type": obj.content_type,
            "size_bytes": obj.size_bytes,
            "sha256": obj.sha256[:12],
            "scan_status": obj.scan_status,
        }

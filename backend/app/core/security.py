"""密码哈希：Argon2id。

PRD §9.4 红线：明文密码不得入数据库、日志、错误、审计、导出、种子或 AI 上下文。
本模块只接受/返回哈希串与布尔结果；任何调用方不得把明文落地或输出。
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

# 16 MiB 内存、3 次迭代、1 个并行；与 OWASP 2024 推荐一致
_hasher = PasswordHasher(memory_cost=2**14, time_cost=3, parallelism=1)


def hash_password(plaintext: str) -> str:
    """对明文做 Argon2id 哈希。调用方负责把 `plaintext` 用完即弃。"""
    return _hasher.hash(plaintext)


def verify_password(plaintext: str, password_hash: str) -> bool:
    """校验明文与哈希是否一致。

    不抛错给上游：调用方拿到 False 后统一走"登录失败"分支并写审计。
    唯一抛出 InvalidHashError 的场景是库内数据被破坏——仍返回 False 以避免泄露。
    """
    try:
        return _hasher.verify(password_hash, plaintext)
    except (VerifyMismatchError, InvalidHashError):
        return False
    except Exception:
        # 防御性兜底：任何意外错误都不应让上游误判为"通过"
        return False


def needs_rehash(password_hash: str) -> bool:
    """哈希参数升级时强制重哈希，避免历史弱参数账号长期挂账。"""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except Exception:
        return True

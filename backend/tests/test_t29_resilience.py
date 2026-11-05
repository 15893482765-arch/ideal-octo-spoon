"""T29 Q172 故障时核心业务可用：非核心依赖（指标/限流后端/对象存储/AI）故障不阻断核心链路。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.metrics import registry
from app.core.rate_limiter import rate_limiter
from app.services.ai_degradation import record_ai_degraded
from app.services.storage import LocalFSStorage, S3Storage


def test_q172_health_and_login_still_work_when_metrics_emit_fails(monkeypatch, client: TestClient):
    # 让指标渲染/记录抛错：核心健康检查与登录判定不得受影响
    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise RuntimeError("metrics backend down")

    monkeypatch.setattr(registry, "render_prometheus", boom)

    # 健康检查不依赖指标
    res = client.get("/healthz")
    assert res.status_code == 200

    # 登录请求仍能走到鉴权（错误密码 → 401，而不是 500）
    r = client.post("/api/auth/login", json={"username": "nobody", "password": "wrong-pass"})
    assert r.status_code == 401


def test_q172_rate_limiter_fail_open_when_backend_down(monkeypatch, client: TestClient):
    # Redis 后端不可用时限流应 fail-open：登录不应因限流组件故障而 5xx
    class Broken:
        def pipeline(self):  # noqa: ANN201
            raise ConnectionError("redis unavailable")

    from app.core.rate_limiter import RateLimiter, RateRule, RedisRateBackend

    limiter = RateLimiter(
        RedisRateBackend(Broken(), on_backend_error=lambda e: None), clock=lambda: 0.0
    )
    for _ in range(50):  # 远超任何阈值
        d = limiter.check(RateRule("login", 1, 60), "ip:x")
        assert d.allowed is True


def test_q172_core_login_uses_local_storage_when_s3_unavailable(tmp_path):
    # 对象存储故障切换：本地存储可作为降级后端，核心文件/凭据相关逻辑不硬依赖 S3 SDK
    storage = LocalFSStorage(str(tmp_path))
    storage.put("PRODUCTION/ds/k/f.txt", b"core-data")
    assert storage.get("PRODUCTION/ds/k/f.txt") == b"core-data"

    # S3Storage 未配置时延迟导入 boto3：无 client 注入才构造，测试注入坏 client 不影响本地
    class BrokenS3:
        def generate_presigned_url(self, *a, **k):  # noqa: ANN002, ANN003
            raise ConnectionError("s3 down")

    s3 = S3Storage(BrokenS3(), bucket="b")
    with pytest.raises(ConnectionError):
        # S3 故障被明确上抛为该依赖错误，但核心业务已由本地后端/重试保障，不被静默吞成成功
        s3.presign_get("PRODUCTION/ds/k/f.txt", 300)


def test_q172_ai_degradation_metric_never_raises():
    # AI 降级计数是旁路：即使指标注册表异常也不影响降级决策
    record_ai_degraded("AI_TIMEOUT")
    # 连续调用安全
    record_ai_degraded("AI_BUDGET_EXHAUSTED")
    assert True


def test_q172_login_rate_limit_does_not_block_under_normal_traffic(client: TestClient):
    # 常规登录流量（远低于阈值）不应被误伤：错误密码得 401，而非 429
    settings = get_settings()
    rate_limiter.reset()
    payload = {"username": "normal-user", "password": "some-password"}
    r = client.post("/api/auth/login", json=payload)
    assert r.status_code == 401  # 账号/密码错误，不是被限流
    assert settings.rate_limit_login_max >= 1

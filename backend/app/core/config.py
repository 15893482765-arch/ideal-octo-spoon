"""应用配置。

安全约束（PRD §9.4 / T01 红线 / T03 红线）：
- 密钥一律 SecretStr，repr/日志/JSON 序列化只显示掩码，不出现明文；
- 必填配置缺失时启动即失败（fail-fast），且错误信息不包含任何密钥值；
- 认证域参数（TTL、限流、厂商开关）集中在本文件，禁止散落在路由/Service。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """环境变量驱动的应用配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "沁爽饮料分销管理平台"
    app_version: str = "0.1.0"
    environment: Literal["dev", "test", "production"]

    # 认证密钥：只允许 SecretStr，任何输出路径都不得出现明文
    secret_key: SecretStr

    # 数据层（PostgreSQL 16 + PostGIS）
    database_url: str

    # 缓存/幂等/分布式锁
    redis_url: str

    # S3 兼容对象存储（本地 MinIO）
    s3_endpoint_url: str
    s3_access_key: SecretStr
    s3_secret_key: SecretStr
    s3_bucket: str = "qinshuang"

    # 演示数据总开关：生产环境必须为 false（PRD §8 存储约束 / R24）
    demo_data_enabled: bool = False
    # 演示账号工作簿（《品牌与账号数据.xlsx》）：非生产环境下启动时把「账号数据」表
    # 播种为可直接登录的账号（幂等）。相对路径按 backend/ 解析；留空则跳过播种。
    demo_account_workbook: str | None = None
    # 生产数据集硬锁：当 environment=="production" 时，启动期硬性要求
    # `demo_data_enabled=False` 且 `production_dataset_lock=True`，禁止任何
    # `dataset_type=DEMO` 批次进入（PRD §10.2、T06 Q031）。
    production_dataset_lock: bool = True
    # 显式启用 `python -m app.cli.db seed-check` 类 CLI：默认 dev/test 可用
    enable_seed_cli: bool = True

    # 同源托管前端：当 `frontend/dist` 已构建时，由后端在根路径提供前端并做 SPA
    # 回退（/api 前缀仍保持 JSON 404，不被前端接管）。这样只需访问后端端口即可
    # 同时拿到页面与接口，避免"前端静态服务器未转发 /api → 登录 404"与
    # "刷新 /workspace 被静态服务器 404"这两类问题（PRD §3.1 全局框架）。
    # 测试环境强制关闭，避免改变既有 404 断言。
    serve_frontend: bool = True

    # 厂商协作方总开关（PRD §2.3）：默认关闭，开启后厂商可登录
    supplier_portal_enabled: bool = False

    log_level: str = "INFO"

    # ===== 认证域（T03）=====
    # access token 寿命：短期，便于被踢
    access_token_ttl_seconds: int = 900
    # refresh token 寿命：较长（30 天），但被踢/改密后立即失效
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 30
    # 连续登录失败上限；达到后锁定账号
    max_login_failures: int = 5
    # 锁定持续时间（秒）
    login_lockout_seconds: int = 900
    # 一次性激活凭据寿命（秒）：默认 7 天
    activation_ttl_seconds: int = 60 * 60 * 24 * 7
    # 一次性密码重置凭据寿命（秒）：默认 24 小时
    password_reset_ttl_seconds: int = 60 * 60 * 24

    # ===== 一次性 bootstrap（首次初始化管理员）=====
    # 仅当环境中存在下列 3 个变量时启用，且仅在没有系统管理员时执行一次：
    # INITIAL_ADMIN_USERNAME / INITIAL_ADMIN_PASSWORD / INITIAL_ADMIN_DISPLAY_NAME
    # 生产部署后应立即从 env 移除 INITIAL_ADMIN_PASSWORD。
    initial_admin_username: str | None = None
    initial_admin_password: SecretStr | None = None
    initial_admin_display_name: str | None = None
    initial_admin_bootstrap: bool = False

    # ===== 文件域（T04 / PRD §8：S3 兼容存储 + 短期签名 URL）=====
    # 上传 MIME 白名单（逗号分隔）；不在名单内的类型直接拒绝
    file_upload_whitelist: str = "image/jpeg,image/png,image/webp,application/pdf"
    # 单文件大小上限（字节）：默认 10MB
    file_max_size_bytes: int = 10 * 1024 * 1024
    # 签名下载 URL 有效期（秒）：短期，默认 5 分钟
    file_signed_url_ttl_seconds: int = 300
    # 存储后端：local（本地文件系统，开发/测试）/ s3（MinIO 等 S3 兼容）
    storage_backend: Literal["local", "s3"] = "local"
    local_storage_dir: str = "./.data/storage"
    # 病毒扫描开关（附录 A：真实扫描不在首版）。关闭时上传的文件直接置 SKIPPED，
    # 否则保持 PENDING 等扫描器回调；只有 CLEAN/SKIPPED 的文件才可被业务绑定。
    file_scan_enabled: bool = False

    # T09 / R38：生效合同到期前 N 天自动生成“续签评估”待办
    contract_renewal_remind_days: int = 30

    # T11 / PRD §4.6：巡访打卡定位距商家坐标阈值（米，默认 200，管理员可配置）。
    # 距离一律由服务端用 PostGIS ST_Distance 重算；前端上报距离不作数（红线）。
    visit_geofence_meters: int = 200

    # T20 / PRD §4.10：工资发放财务双人复核开关。
    # 开启后“财务复核（APPROVED）”与“财务发放（PAID）”不得由同一财务完成；
    # 生产环境建议置 true，dev/test 默认 false。
    payroll_dual_finance_review: bool = False

    # ===== T21 风险域（PRD §4.11，阈值由管理员配置）=====
    # RK-1：ACTIVE 商家超过巡访频次未产生有效巡访（无等级频次配置时的兜底天数）
    risk_no_visit_days: int = 7
    # RK-3：连续两次陈列低于合同阈值即触发（固定次数，PRD 明确“连续两次”）
    risk_display_consecutive: int = 2
    # RK-4：连续两周确认销量为零即触发
    risk_zero_sales_weeks: int = 2
    # RK-7：冰柜维修（REPAIR_START）超过该小时数未归库即触发
    risk_freezer_repair_limit_hours: int = 72

    # ===== T21 导出域（PRD §4.10 / §9.4 导出）=====
    # 导出总开关（PRD §5.3 EXPORT_ENABLED）
    export_enabled: bool = True
    # 导出文件短期下载链接有效期（秒），默认 15 分钟
    export_link_ttl_seconds: int = 900
    # 单次导出行数上限（防止超大导出拖垮内存）
    export_max_rows: int = 50_000

    # ===== 幂等域（T04 / PRD §5.4）=====
    # 幂等键保留期（小时）：过期后后台清理，窗口外同键视为新请求
    idempotency_retention_hours: int = 24 * 7

    # ===== T24 / PRD §5.4：MCP 写工具结构化确认 token =====
    # 执行预览签发的确认 token 有效期（秒），默认 10 分钟；过期需重新预览。
    mcp_confirmation_ttl_seconds: int = 600

    # ===== T25 / PRD §5.2、§9.1：AI 建议与经营解释 Provider =====
    # 经营解释/建议的语言层：首版只内置 mock（确定性、可复现、无外部依赖）。
    # LLM 仅负责组织语言与标注为「假设」的原因，绝不重算指标/金额（PRD §9.1）。
    ai_provider: Literal["mock"] = "mock"
    # mock Provider 确定性种子；同输入 + 同种子必须产出相同文本（Q146 可复现）。
    ai_mock_seed: int = 20261001
    # 对话模型（OpenAI 兼容）：只用于「选只读工具 + 组织回答」，不参与取数与权限判断；
    # 与经营解释的 ai_provider 分离，避免对话模型影响指标口径输出。
    # 供应商只决定「是否启用」；任意 OpenAI 兼容端点都走同一客户端实现。
    ai_chat_provider: Literal["mock", "moonshot", "openai"] = "mock"
    ai_model: str = "deepseek-flash"
    ai_base_url: str = "https://api.deepseek.com/v1"
    ai_api_key: SecretStr | None = None
    # 单次对话模型调用时限（比 AiGateway 的 15s 略宽，覆盖首包延迟）
    ai_chat_timeout_seconds: float = 25.0

    # ===== RAG 知识库 / Embedding（任务卡 1）=====
    # 知识块向量化：Moonshot 无公开 embedding API，首版本地 bge-small-zh-v1.5
    # （sentence-transformers，CPU 可跑）；openai_compatible 保留给兼容端点
    # （如托管 bge-m3），切换时无需改代码。
    embedding_enabled: bool = True
    embedding_provider: Literal["local", "openai_compatible"] = "local"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dim: int = 512
    embedding_batch_size: int = 32
    embedding_device: str = "cpu"
    # 本地模型缓存目录（sentence-transformers cache_folder）；空串用 HF 默认缓存
    embedding_model_dir: str = ""
    # openai_compatible 端点（local 模式留空）
    embedding_base_url: str = ""
    embedding_api_key: SecretStr | None = None

    # ===== Qdrant 语义检索（Text2SQL L2 检索层）=====
    # 语义层索引的向量存储：双 collection（实体表粒度 + 列/指标细粒度）。
    # 关闭时检索器降级为 catalog 关键词匹配（低精度兜底，不阻断助手）。
    qdrant_enabled: bool = False
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: SecretStr | None = None
    qdrant_timeout_seconds: float = 10.0
    semantic_index_entity_collection: str = "semantic_entities"
    semantic_index_column_collection: str = "semantic_columns"
    # 检索返回规模：实体 top-k / 细粒度（列+指标）top-k
    retrieval_top_k_entities: int = 6
    retrieval_top_k_columns: int = 12
    # ContextBuilder 的 token 预算（按字符近似估算）
    retrieval_context_token_budget: int = 3000

    # ===== Text2SQL 只读执行器 =====
    text2sql_statement_timeout_ms: int = 15000
    text2sql_max_rows: int = 1000

    # ===== T26 / PRD §9.3、§5.5、§9.4：模型韧性 =====
    # 模型超时 15 秒；超时/失败最多重试 1 次（共最多 2 次尝试）。
    ai_timeout_seconds: float = 15.0
    ai_max_attempts: int = 2
    # 断路器：连续失败达到阈值即打开；打开期内直接走逐能力降级（不调用模型）；
    # 打开后经过冷却秒数进入半开，放一次试探，成功则关闭。
    ai_circuit_failure_threshold: int = 5
    ai_circuit_open_cooldown_seconds: float = 30.0

    # ===== T22 / PRD §5.3.1：MCP 写工具功能开关（审批矩阵「功能开关」列）=====
    # 关闭后对应工具调用一律被 MCP 网关拒绝（MCP_FEATURE_DISABLED）；
    # SUPPLIER_PORTAL_ENABLED / EXPORT_ENABLED 复用上方同名开关。
    merchant_enabled: bool = True
    visit_enabled: bool = True
    replenishment_enabled: bool = True
    contract_enabled: bool = True
    schedule_enabled: bool = True
    merchant_transfer_enabled: bool = True
    campaign_enabled: bool = True
    supervision_enabled: bool = True
    approval_enabled: bool = True

    # ===== T29 / PRD §9.4：安全（CORS / 限流）=====
    # 允许的跨域来源，逗号分隔；默认空（不放行任意 Origin）。生产必须显式配置前端域名。
    cors_allowed_origins: str = ""
    # 登录接口限流：同一身份每窗口最大尝试次数与窗口秒数（与账号锁定互补，防爆破）
    rate_limit_login_max: int = 10
    rate_limit_login_window_seconds: int = 60
    # 通用 API 限流：每身份每窗口最大请求数
    rate_limit_api_max: int = 600
    rate_limit_api_window_seconds: int = 60
    # 限流后端：memory（单进程/测试）/ redis（多实例共享）
    rate_limit_backend: Literal["memory", "redis"] = "memory"
    # 生产环境是否暴露 /metrics（默认 false，由受控网络/采集器开启）
    metrics_enabled: bool = False

    # ===== T29 / PRD §8：备份与恢复（RPO≤24h、RTO≤4h、30 天恢复窗口）=====
    backup_enabled: bool = False
    # 每日备份时刻（cron 表达式，容器/系统 cron 实际调度；默认 UTC 18:00 = 北京 02:00）
    backup_schedule_cron: str = "0 18 * * *"
    backup_dir: str = "./.data/backups"
    # 恢复窗口：保留最近 N 天每日备份（PRD 明确至少 30 天）
    backup_retention_days: int = 30
    # 恢复目标：用于演练校验与文档/告警；恢复流程必须在此时长内完成
    backup_rpo_hours: int = 24
    backup_rto_hours: int = 4
    # 绩效/工资长期保留（月）：月度绩效与工资快照长期留存策略（PRD §8，企业策略可配置）
    payroll_retention_months: int = 120  # 10 年
    performance_retention_months: int = 120

    @property
    def cors_origins_set(self) -> set[str]:
        return {o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()}

    @property
    def database_url_sync(self) -> str:
        """Alembic 与同步测试使用的 URL：把 asyncpg 驱动替换为 psycopg2。"""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")


@lru_cache
def get_settings() -> Settings:
    """返回进程级单例配置；缺失必填项时在首次调用处直接抛错。"""
    # pydantic-settings 从环境变量/.env 注入必填字段，mypy 无法静态推断
    return Settings()  # type: ignore[call-arg]

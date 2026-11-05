"""RAG 知识库切块存储（知识问答任务卡 1）。

设计决策：
- **全员可见**：知识内容为公司公开制度文档，表上不设角色/范围列；
  访问控制统一在 MCP 工具层（knowledge.search 的白名单）完成；
- **向量列用 ARRAY(Float) 而非 pgvector**：本地开发/测试库未安装
  vector 扩展（pg_available_extensions 无记录）。知识块规模为几十到几百条，
  在 Python 侧做余弦相似度足够；后续上量时可平移 pgvector（列类型迁移 +
  检索 SQL 改写，表结构其余字段不变）；
- **幂等摄入**：`(doc_id, section_path)` 唯一索引为自然键，
  `content_hash`（sha256）用于摄入时跳过未变更切块，支持重复跑摄入脚本；
- **embedding 可空**：允许先入库后补向量（embedding 服务降级时不阻塞内容更新）。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Float, Index, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, pk_uuid

# 向量维度由 config.embedding_dim 决定（随 provider 而变：智谱 embedding-3 为 2048，
# 本地 bge-small-zh 为 512）。列类型是维度无关的 ARRAY(Float)，换模型无需迁移。
EMBEDDING_DIM = 512


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        # 摄入幂等键：同一文档同一小节重复摄入走更新而非插重
        Index(
            "ux_knowledge_chunks_doc_section",
            "doc_id",
            "section_path",
            unique=True,
        ),
    )

    id: Mapped[UUID] = pk_uuid()

    # 文档编号（如 QS-POL-FREEZER-003），回答引用来源时展示
    doc_id: Mapped[str] = mapped_column(String(40), nullable=False)
    doc_title: Mapped[str] = mapped_column(String(200), nullable=False)
    # 小节标题路径（如 "5. 报修与维修流程"），切块与引用定位用
    section_path: Mapped[str] = mapped_column(String(300), nullable=False)

    content: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # 向量（bge-small-zh 512 维）；可空：embedding 降级时允许只更新文本
    embedding: Mapped[list[float] | None] = mapped_column(
        ARRAY(Float), nullable=True
    )

    # sha256(content)：摄入时比对跳过未变更切块
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

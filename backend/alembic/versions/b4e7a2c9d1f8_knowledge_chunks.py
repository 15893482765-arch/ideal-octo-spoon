"""RAG 知识库切块表：knowledge_chunks。

- 全员可见的公司制度文档切块（无角色/范围列，访问控制在 MCP 工具层）；
- 向量列用 ARRAY(Float)：本地库无 pgvector 扩展，块规模小走 Python 余弦；
  后续可平移 pgvector；
- (doc_id, section_path) 唯一索引为摄入幂等键，content_hash 比对跳过未变更。

Revision ID: b4e7a2c9d1f8
Revises: d8f3b02e4a16
Create Date: 2026-10-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b4e7a2c9d1f8"
down_revision = "d8f3b02e4a16"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_chunks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("doc_id", sa.String(length=40), nullable=False),
        sa.Column("doc_title", sa.String(length=200), nullable=False),
        sa.Column("section_path", sa.String(length=300), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("embedding", postgresql.ARRAY(sa.Float()), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ux_knowledge_chunks_doc_section",
        "knowledge_chunks",
        ["doc_id", "section_path"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_knowledge_chunks_doc_section", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")

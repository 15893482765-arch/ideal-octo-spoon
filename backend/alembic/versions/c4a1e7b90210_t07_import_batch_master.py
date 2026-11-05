"""t07_import_batch_master

T07 沁爽主数据 Excel 导入：扩展 import_batches 以承载四步导入
（上传/预检/确认/执行）所需的文件哈希、数据集标识、批次类别与结构化预览。

Revision ID: c4a1e7b90210
Revises: 26bceae5250f
Create Date: 2026-10-12 16:05:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c4a1e7b90210'
down_revision: str | None = '26bceae5250f'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 全部带 server_default，保证历史行可回填；payload_json 禁止持久化任何凭据。
    op.add_column(
        'import_batches',
        sa.Column('dataset_id', sa.UUID(), nullable=True),
    )
    op.add_column(
        'import_batches',
        sa.Column(
            'file_hash',
            sa.String(length=64),
            server_default=sa.text("''"),
            nullable=False,
        ),
    )
    op.add_column(
        'import_batches',
        sa.Column(
            'seed_kind',
            sa.String(length=16),
            server_default=sa.text("'MASTER'"),
            nullable=False,
        ),
    )
    op.add_column(
        'import_batches',
        sa.Column(
            'payload_json',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    # 同 (source_filename, file_hash) 唯一：重复文件只显示既有批次（Q034）。
    # file_hash 非空批次之间唯一；空串（历史/自检批次）不参与，用部分唯一索引。
    op.create_index(
        'uq_import_batches_source_hash',
        'import_batches',
        ['source_filename', 'file_hash'],
        unique=True,
        postgresql_where=sa.text("file_hash <> ''"),
    )
    op.create_index(
        'ix_import_batches_dataset',
        'import_batches',
        ['dataset_type', 'dataset_id'],
        unique=False,
    )


    # T07：skus 增补主数据展示列（均可空，不改变既有非空/校验口径）。
    op.add_column('skus', sa.Column('purchase_price_fen', sa.BigInteger(), nullable=True))
    op.add_column('skus', sa.Column('series_name', sa.String(length=64), nullable=True))
    op.add_column('skus', sa.Column('category_name', sa.String(length=64), nullable=True))
    op.add_column('skus', sa.Column('spec_text', sa.String(length=64), nullable=True))
    op.add_column('skus', sa.Column('selling_points', sa.String(length=255), nullable=True))
    op.add_column('skus', sa.Column('shelf_life_text', sa.String(length=32), nullable=True))
    op.add_column('skus', sa.Column('target_channel', sa.String(length=128), nullable=True))

    # 修正既有口径缺陷：freezer_assets.status 原为 String(16)，
    # 无法容纳受控枚举值 UNDER_MAINTENANCE（17 字符）；加宽到 24。
    op.alter_column(
        'freezer_assets',
        'status',
        existing_type=sa.String(length=16),
        type_=sa.String(length=24),
        existing_server_default=sa.text("'IN_STOCK'"),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        'freezer_assets',
        'status',
        existing_type=sa.String(length=24),
        type_=sa.String(length=16),
        existing_server_default=sa.text("'IN_STOCK'"),
        existing_nullable=False,
    )
    op.drop_column('skus', 'target_channel')
    op.drop_column('skus', 'shelf_life_text')
    op.drop_column('skus', 'selling_points')
    op.drop_column('skus', 'spec_text')
    op.drop_column('skus', 'category_name')
    op.drop_column('skus', 'series_name')
    op.drop_column('skus', 'purchase_price_fen')
    op.drop_index('ix_import_batches_dataset', table_name='import_batches')
    op.drop_index('uq_import_batches_source_hash', table_name='import_batches')
    op.drop_column('import_batches', 'payload_json')
    op.drop_column('import_batches', 'seed_kind')
    op.drop_column('import_batches', 'file_hash')
    op.drop_column('import_batches', 'dataset_id')

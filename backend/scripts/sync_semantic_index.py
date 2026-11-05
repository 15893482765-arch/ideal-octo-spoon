"""语义索引同步脚本（Text2SQL L2）：语义层 YAML → 双 collection 全量重建。

用法（backend 根目录）：
    python scripts/sync_semantic_index.py            # 全量重建（默认即幂等）
    python scripts/sync_semantic_index.py --dry-run  # 只构建文档预览，不调模型不写库

注意：本脚本是语义索引的唯一写入路径；运行期检索只读。语义层 YAML 变更后
必须重跑本脚本。本地首次运行会自动下载 bge-small-zh-v1.5（约 100MB）。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# 直接 `python scripts/sync_semantic_index.py` 运行时脚本目录会占 sys.path[0]，
# 自举 backend 根目录以保证 `app.*` 可导入（pytest 经 pythonpath=["."] 不受影响）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402
from app.retrieval.store import QdrantSemanticStore  # noqa: E402
from app.retrieval.sync import sync_semantic_index  # noqa: E402
from app.semantic import get_default_catalog  # noqa: E402
from app.services.ai_embedding import get_embedding_client  # noqa: E402

logger = logging.getLogger("sync_semantic_index")


def main() -> int:
    parser = argparse.ArgumentParser(description="语义索引同步（双 collection 全量重建）")
    parser.add_argument("--dry-run", action="store_true", help="只预览文档，不写库")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    settings = get_settings()
    catalog = get_default_catalog(refresh=True)
    summary = catalog.summary()
    logger.info(
        "语义层已加载：%d 实体 / %d 指标 / %d 维度 / %d 术语",
        summary["entities"], summary["metrics"], summary["dimensions"], summary["glossary"],
    )

    if args.dry_run:
        report = sync_semantic_index(catalog, embedding=None, store=None, dry_run=True)
        logger.info(
            "dry-run：将构建 %d 条实体文档 + %d 条细粒度文档",
            report.entity_documents, report.column_documents,
        )
        for text in report.sample_texts:
            logger.info("样例文档：%s", text[:120])
        return 0

    embedding = get_embedding_client()
    if embedding is None:
        logger.error("embedding 不可用（检查 embedding_enabled / provider 配置）")
        return 1
    store = QdrantSemanticStore(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None,
        timeout_seconds=settings.qdrant_timeout_seconds,
        entity_collection=settings.semantic_index_entity_collection,
        column_collection=settings.semantic_index_column_collection,
        vector_size=settings.embedding_dim,
    )
    if not store.ping():
        logger.error("Qdrant 不可达：%s（先启动 infra/docker-compose.yml 的 qdrant 服务）", settings.qdrant_url)
        return 1

    logger.info("开始 embedding + 重建 collection（向量维度 %d）…", embedding.dim)
    report = sync_semantic_index(catalog, embedding=embedding, store=store)
    logger.info(
        "同步完成：%d 条实体文档 + %d 条细粒度文档，共写入 %d 条（dim=%d）",
        report.entity_documents, report.column_documents, report.written, report.vector_dim,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

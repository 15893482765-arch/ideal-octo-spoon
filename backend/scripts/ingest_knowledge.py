"""知识库摄入管线（任务卡 3）：解析 knowledge/*.md → 切块 → embedding → 幂等入库。

用法（backend 根目录）：
    python scripts/ingest_knowledge.py              # 增量：只嵌入新增/变更切块
    python scripts/ingest_knowledge.py --rebuild    # 全量重建（切换 embedding 模型后必须）
    python scripts/ingest_knowledge.py --dry-run    # 只解析预览，不调模型不写库

切块规则（确定性，无 LLM）：
- `# 标题` → doc_title；头部 `- 文档编号：XXX` → doc_id；
- `## N. xxx` 为小节边界，`###` 子节并入所属小节（块保持短小）；
- 小节标题含「FAQ」时按 `**Q：…**` / `A：…` 问答对逐条整块
  （FAQ 与员工真实提问表述最接近，整块保留命中率最高）；
- 幂等：唯一键 (doc_id, section_path)；content_hash 一致且已有向量 → 跳过；
  hash 变更 → 更新；文档中删除的小节 → 同步删行；embedding 为 NULL 的行
  即使 hash 未变也会补嵌（降级后恢复的自愈路径）。
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

# 直接 `python scripts/ingest_knowledge.py` 运行时脚本目录会占 sys.path[0]，
# 自举 backend 根目录以保证 `app.*` 可导入（pytest 经 pythonpath=["."] 不受影响）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.session import get_session_factory  # noqa: E402
from app.models import KnowledgeChunk  # noqa: E402
from app.services.ai_embedding import EmbeddingClient, get_embedding_client  # noqa: E402

logger = logging.getLogger(__name__)

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"

_DOC_ID_RE = re.compile(r"文档编号[:：]\s*([A-Za-z0-9-]+)")
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")
_FAQ_Q_RE = re.compile(r"^\*\*Q[:：]\s*(.+?)\s*\*+\s*$")
_FAQ_A_RE = re.compile(r"^A[:：]\s*(.*)$")


@dataclass
class Chunk:
    section_path: str
    content: str
    content_hash: str


@dataclass
class ParsedDoc:
    doc_id: str
    doc_title: str
    chunks: list[Chunk] = field(default_factory=list)


@dataclass
class IngestStats:
    added: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize(text: str) -> str:
    lines = [line.rstrip() for line in text.strip().splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _split_faq(section_title: str, body: str) -> list[Chunk]:
    """FAQ 小节按问答对切块：`**Q：…**` 起新块，`A：` 及续行并入。"""
    pairs: list[tuple[str, list[str]]] = []
    current_q: str | None = None
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        q_match = _FAQ_Q_RE.match(line)
        if q_match:
            current_q = q_match.group(1)
            pairs.append((current_q, []))
            continue
        if current_q is None:
            continue
        a_match = _FAQ_A_RE.match(line)
        pairs[-1][1].append(a_match.group(1) if a_match else line)
    chunks = []
    for question, answer_lines in pairs:
        answer = _normalize("\n".join(answer_lines))
        content = _normalize(f"Q：{question}\nA：{answer}")
        chunks.append(
            Chunk(
                section_path=f"{section_title}｜{question}"[:300],
                content=content,
                content_hash=_hash(content),
            )
        )
    return chunks


def parse_markdown(path: Path) -> ParsedDoc:
    """解析单份制度 markdown 为切块集合（不触 DB、不调模型）。"""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    doc_title = ""
    doc_id = ""
    sections: list[tuple[str, list[str]]] = []
    for line in lines:
        heading = _HEADING_RE.match(line)
        if heading:
            level, title = len(heading.group(1)), heading.group(2).strip()
            if level == 1 and not doc_title:
                doc_title = title
                continue
            if level == 2:
                sections.append((title, []))
                continue
            # `###` 子节并入所属小节，保留小标题行文
            if sections:
                sections[-1][1].append(title)
            continue
        id_match = _DOC_ID_RE.search(line)
        if id_match and not doc_id:
            doc_id = id_match.group(1)
        if sections:
            sections[-1][1].append(line)

    if not doc_title or not doc_id:
        raise ValueError(f"{path.name} 缺少 `# 标题` 或 `- 文档编号：XXX` 头")
    if not sections:
        raise ValueError(f"{path.name} 没有任何 `## ` 小节")

    parsed = ParsedDoc(doc_id=doc_id, doc_title=doc_title)
    for section_title, body_lines in sections:
        body = _normalize("\n".join(body_lines))
        if "FAQ" in section_title:
            parsed.chunks.extend(_split_faq(section_title, body))
            continue
        content = _normalize(f"{section_title}\n{body}")
        parsed.chunks.append(
            Chunk(
                section_path=section_title[:300],
                content=content,
                content_hash=_hash(content),
            )
        )
    return parsed


def ingest_document(
    session: Session,
    path: Path,
    client: EmbeddingClient,
    *,
    rebuild: bool = False,
) -> IngestStats:
    """摄入单份文档：按自然键 diff，仅对新增/变更内容调用 embedding。"""
    parsed = parse_markdown(path)
    stats = IngestStats()

    if rebuild:
        session.execute(
            delete(KnowledgeChunk).where(KnowledgeChunk.doc_id == parsed.doc_id)
        )

    existing = {
        row.section_path: row
        for row in session.scalars(
            select(KnowledgeChunk).where(KnowledgeChunk.doc_id == parsed.doc_id)
        )
    }
    parsed_paths = {chunk.section_path for chunk in parsed.chunks}

    pending: list[tuple[Chunk, KnowledgeChunk | None]] = []
    for chunk in parsed.chunks:
        row = existing.get(chunk.section_path)
        if (
            row is not None
            and row.content_hash == chunk.content_hash
            and row.embedding is not None  # 向量缺失的行走补嵌，不自欺跳过
        ):
            stats.skipped += 1
            continue
        pending.append((chunk, row))

    if pending:
        vectors = client.embed_documents([chunk.content for chunk, _ in pending])
        for (chunk, row), vector in zip(pending, vectors, strict=True):
            if row is None:
                session.add(
                    KnowledgeChunk(
                        doc_id=parsed.doc_id,
                        doc_title=parsed.doc_title,
                        section_path=chunk.section_path,
                        content=chunk.content,
                        embedding=list(vector),
                        content_hash=chunk.content_hash,
                    )
                )
                stats.added += 1
            else:
                row.doc_title = parsed.doc_title
                row.content = chunk.content
                row.content_hash = chunk.content_hash
                row.embedding = list(vector)
                row.updated_at = datetime.now(UTC)
                stats.updated += 1

    for section_path, row in existing.items():
        if section_path not in parsed_paths:
            session.delete(row)
            stats.deleted += 1

    session.commit()
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="知识库摄入：markdown → 切块 → embedding → 入库"
    )
    parser.add_argument("--dir", type=Path, default=KNOWLEDGE_DIR, help="markdown 目录")
    parser.add_argument("--rebuild", action="store_true", help="先清空对应文档再全量重建")
    parser.add_argument("--dry-run", action="store_true", help="只解析预览，不调模型不写库")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    files = sorted(args.dir.glob("*.md"))
    if not files:
        logger.error("未找到 markdown：%s", args.dir)
        return 1

    if args.dry_run:
        for path in files:
            parsed = parse_markdown(path)
            print(
                f"\n{path.name}  →  {parsed.doc_id}《{parsed.doc_title}》"
                f" 共 {len(parsed.chunks)} 块"
            )
            for chunk in parsed.chunks:
                print(f"  [{len(chunk.content):4d} 字] {chunk.section_path}")
        return 0

    client = get_embedding_client()
    if client is None:
        logger.error("embedding 不可用（EMBEDDING_ENABLED=false 或配置缺失），中止摄入")
        return 1

    session = get_session_factory()()
    try:
        total = IngestStats()
        for path in files:
            stats = ingest_document(session, path, client, rebuild=args.rebuild)
            total.added += stats.added
            total.updated += stats.updated
            total.deleted += stats.deleted
            total.skipped += stats.skipped
            print(
                f"{path.name}: +{stats.added} 新增 / ~{stats.updated} 更新 / "
                f"-{stats.deleted} 删除 / ={stats.skipped} 跳过"
            )
        print(
            f"\n合计：+{total.added} / ~{total.updated} / -{total.deleted} / ={total.skipped}"
        )
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

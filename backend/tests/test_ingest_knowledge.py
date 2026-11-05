"""摄入管线测试（任务卡 3）：解析切块 + 幂等 diff，全程假 embedding 客户端。

覆盖：
- 解析：doc_id/标题抽取、### 子节并入父节、FAQ 按问答对整块、hash 稳定；
- 摄入：首跑全新增、复跑全跳过（不调模型）、内容变更只更一条、
  小节删除同步删行、--rebuild 全量重建、向量缺失行自动补嵌。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from scripts.ingest_knowledge import ingest_document, parse_markdown
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import KnowledgeChunk

FIXTURE_MD = """# 沁爽测试 制度文档

- 文档编号：QS-TEST-001
- 版本：V1.0
---

## 1. 总则

第一条 内容甲。

## 2. 流程

### 2.1 子节

子节内容并入父节。

## 3. 常见问题（FAQ）

**Q：问题一是什么？**
A：答案一。

**Q：问题二怎么算？**
A：答案二第一行
答案二第二行。
"""


class FakeEmbedding:
    """假 embedding 客户端：记录调用，返回确定性 8 维向量。"""

    dim = 8

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_documents(self, texts: Any) -> list[list[float]]:
        batch = list(texts)
        self.calls.append(batch)
        return [[float(i + 1)] * self.dim for i, _ in enumerate(batch)]

    def embed_query(self, text: str) -> list[float]:
        return [1.0] * self.dim


@pytest.fixture
def md_file(tmp_path: Path) -> Path:
    path = tmp_path / "test-doc.md"
    path.write_text(FIXTURE_MD, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def test_parse_extracts_header_and_chunks(md_file: Path) -> None:
    parsed = parse_markdown(md_file)
    assert parsed.doc_id == "QS-TEST-001"
    assert parsed.doc_title == "沁爽测试 制度文档"
    # 2 个普通小节 + 2 条 FAQ
    assert len(parsed.chunks) == 4
    paths = [c.section_path for c in parsed.chunks]
    assert "1. 总则" in paths
    assert "2. 流程" in paths
    assert any("FAQ" in p and "问题一是什么" in p for p in paths)


def test_parse_merges_subsection_into_parent(md_file: Path) -> None:
    parsed = parse_markdown(md_file)
    section2 = next(c for c in parsed.chunks if c.section_path == "2. 流程")
    assert "子节内容并入父节" in section2.content
    # 子节标题以行文形式保留，不单独成块
    assert not any(c.section_path.startswith("2.1") for c in parsed.chunks)


def test_parse_faq_chunk_keeps_qa_together(md_file: Path) -> None:
    parsed = parse_markdown(md_file)
    faq = [c for c in parsed.chunks if "FAQ" in c.section_path]
    assert len(faq) == 2
    q2 = next(c for c in faq if "问题二" in c.section_path)
    assert q2.content.startswith("Q：问题二怎么算？")
    assert "A：答案二第一行" in q2.content and "答案二第二行" in q2.content
    assert "**" not in q2.content  # 粗体标记已剥离


def test_parse_hash_stable_and_content_sensitive(md_file: Path) -> None:
    first = parse_markdown(md_file)
    second = parse_markdown(md_file)
    assert [c.content_hash for c in first.chunks] == [c.content_hash for c in second.chunks]
    md_file.write_text(FIXTURE_MD.replace("内容甲", "内容乙"), encoding="utf-8")
    changed = parse_markdown(md_file)
    old_hashes = {x.content_hash for x in first.chunks}
    diff = [c for c in changed.chunks if c.content_hash not in old_hashes]
    assert len(diff) == 1 and diff[0].section_path == "1. 总则"


def test_parse_missing_header_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.md"
    bad.write_text("# 无编号文档\n\n## 1. 节\n内容\n", encoding="utf-8")
    with pytest.raises(ValueError, match="文档编号"):
        parse_markdown(bad)


# ---------------------------------------------------------------------------
# 摄入（DB）
# ---------------------------------------------------------------------------


def _rows(session: Session) -> list[KnowledgeChunk]:
    return list(
      session.scalars(
          select(KnowledgeChunk)
          .where(KnowledgeChunk.doc_id == "QS-TEST-001")
          .order_by(KnowledgeChunk.section_path)
      )
    )


def test_ingest_first_run_adds_all(
    test_env: None, isolated_session: Session, md_file: Path
) -> None:
    fake = FakeEmbedding()
    stats = ingest_document(isolated_session, md_file, fake)
    assert (stats.added, stats.updated, stats.deleted, stats.skipped) == (4, 0, 0, 0)
    rows = _rows(isolated_session)
    assert len(rows) == 4
    assert all(r.doc_title == "沁爽测试 制度文档" for r in rows)
    assert all(r.embedding is not None and len(r.embedding) == 8 for r in rows)
    assert all(len(r.content_hash) == 64 for r in rows)


def test_ingest_second_run_skips_without_model_call(
    test_env: None, isolated_session: Session, md_file: Path
) -> None:
    ingest_document(isolated_session, md_file, FakeEmbedding())
    ids_before = {r.section_path: r.id for r in _rows(isolated_session)}

    fake = FakeEmbedding()
    stats = ingest_document(isolated_session, md_file, fake)
    assert (stats.added, stats.updated, stats.deleted) == (0, 0, 0)
    assert stats.skipped == 4
    assert fake.calls == []  # 未变更不调用模型
    assert {r.section_path: r.id for r in _rows(isolated_session)} == ids_before


def test_ingest_updates_only_changed_section(
    test_env: None, isolated_session: Session, md_file: Path
) -> None:
    ingest_document(isolated_session, md_file, FakeEmbedding())
    before = {r.section_path: r.content_hash for r in _rows(isolated_session)}

    md_file.write_text(FIXTURE_MD.replace("内容甲", "内容乙"), encoding="utf-8")
    fake = FakeEmbedding()
    stats = ingest_document(isolated_session, md_file, fake)
    assert (stats.added, stats.updated, stats.deleted, stats.skipped) == (0, 1, 0, 3)
    assert fake.calls == [["1. 总则\n第一条 内容乙。"]]  # 只嵌变更块

    after = {r.section_path: r.content_hash for r in _rows(isolated_session)}
    assert after["1. 总则"] != before["1. 总则"]
    unchanged = set(after) - {"1. 总则"}
    assert all(after[p] == before[p] for p in unchanged)


def test_ingest_deletes_removed_section(
    test_env: None, isolated_session: Session, md_file: Path
) -> None:
    ingest_document(isolated_session, md_file, FakeEmbedding())
    md_file.write_text(
        FIXTURE_MD.replace("## 2. 流程\n\n### 2.1 子节\n\n子节内容并入父节。\n\n", ""),
        encoding="utf-8",
    )
    stats = ingest_document(isolated_session, md_file, FakeEmbedding())
    assert stats.deleted == 1
    assert {r.section_path for r in _rows(isolated_session)} == {
        "1. 总则",
        "3. 常见问题（FAQ）｜问题一是什么？",
        "3. 常见问题（FAQ）｜问题二怎么算？",
    }


def test_ingest_rebuild_replaces_rows(
    test_env: None, isolated_session: Session, md_file: Path
) -> None:
    ingest_document(isolated_session, md_file, FakeEmbedding())
    ids_before = {r.id for r in _rows(isolated_session)}
    stats = ingest_document(isolated_session, md_file, FakeEmbedding(), rebuild=True)
    assert stats.added == 4
    ids_after = {r.id for r in _rows(isolated_session)}
    assert len(ids_after) == 4
    assert ids_before.isdisjoint(ids_after)  # 全部换血


def test_ingest_backfills_missing_embedding(
    test_env: None, isolated_session: Session, md_file: Path
) -> None:
    ingest_document(isolated_session, md_file, FakeEmbedding())
    row = _rows(isolated_session)[0]
    row.embedding = None  # 模拟降级期入库的缺向量行
    isolated_session.flush()

    fake = FakeEmbedding()
    stats = ingest_document(isolated_session, md_file, fake)
    # hash 未变但向量为 NULL → 走补嵌而非跳过
    assert stats.updated == 1 and stats.skipped == 3
    refreshed = _rows(isolated_session)[0]
    assert refreshed.embedding is not None

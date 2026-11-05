"""knowledge.search 工具测试（任务卡 4）。

覆盖：
- 服务级：向量排序与阈值过滤、降级关键词回退、推理失败回退、
  缺向量行的两种模式行为、top_k 截断；
- 工具级：六类角色全员放行、命中返回来源四要素、空结果 NO_RECORDS 文案、
  降级模式在信封上明示、注册表与白名单一致性。
全程假 embedding 客户端，不触模型。
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

import mcp
from app.core.permissions import ActorContext
from app.models import KnowledgeChunk
from app.services.ai_embedding import EmbeddingUnavailableError
from app.services.knowledge_search import MIN_VECTOR_SCORE, search_knowledge
from mcp.approval_matrix import (
    R_ADMIN,
    R_FINANCE,
    R_MANAGER,
    R_SALES,
    R_SUPERVISOR,
    R_SUPPLIER,
)
from mcp.catalog import ALL_TOOL_SPECS, PRD_TOOL_CATALOG
from mcp.gateway import McpGateway
from mcp.identity import McpContext

ALL_ROLES = (R_SALES, R_MANAGER, R_SUPERVISOR, R_FINANCE, R_ADMIN, R_SUPPLIER)

FREEZER_VEC = [1.0, 0.0, 0.0, 0.0]
CONTRACT_VEC = [0.0, 1.0, 0.0, 0.0]


class FakeEmbedding:
    """假 embedding 客户端：embed_query 返回固定向量。"""

    def __init__(self, query_vec: list[float]) -> None:
        self._query_vec = query_vec

    def embed_query(self, text: str) -> list[float]:
        return self._query_vec


class BrokenEmbedding:
    def embed_query(self, text: str) -> list[float]:
        raise EmbeddingUnavailableError("模拟推理失败")


def _chunk(
    session: Session,
    doc_id: str,
    section: str,
    content: str,
    embedding: list[float] | None,
) -> KnowledgeChunk:
    row = KnowledgeChunk(
        doc_id=doc_id,
        doc_title=f"《{doc_id} 测试文档》",
        section_path=section,
        content=content,
        embedding=embedding,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def seeded(session: Session) -> None:
    _chunk(session, "QS-POL-FREEZER-003", "5. 报修与维修流程",
           "冰柜不制冷时填破损故障并立即报修", FREEZER_VEC)
    _chunk(session, "QS-POL-CONTRACT-001", "5. 续签管理",
           "合同到期前 30 天触发续签评估", CONTRACT_VEC)


# ---------------------------------------------------------------------------
# 服务级
# ---------------------------------------------------------------------------


def test_vector_ranks_and_filters(test_env: None, session: Session, seeded: None) -> None:
    hits, mode = search_knowledge(session, "冰柜坏了怎么报修", client=FakeEmbedding(FREEZER_VEC))
    assert mode == "vector"
    assert len(hits) == 1
    assert hits[0].doc_id == "QS-POL-FREEZER-003"
    assert hits[0].section_path == "5. 报修与维修流程"
    assert hits[0].score >= MIN_VECTOR_SCORE


def test_vector_below_threshold_returns_empty(
    test_env: None, session: Session, seeded: None
) -> None:
    # 与两个块都正交的查询向量：相似度 0，低于阈值
    hits, mode = search_knowledge(session, "无关问题", client=FakeEmbedding([0.0, 0.0, 1.0, 0.0]))
    assert hits == [] and mode == "vector"


def test_keyword_fallback_when_client_none(
    test_env: None, session: Session, seeded: None
) -> None:
    hits, mode = search_knowledge(session, "冰柜报修", client=None)
    assert mode == "keyword"
    assert hits[0].doc_id == "QS-POL-FREEZER-003"


def test_vector_failure_falls_back_to_keyword(
    test_env: None, session: Session, seeded: None
) -> None:
    hits, mode = search_knowledge(session, "冰柜报修", client=BrokenEmbedding())
    assert mode == "keyword"
    assert hits and hits[0].doc_id == "QS-POL-FREEZER-003"


def test_rows_without_embedding_skipped_in_vector_but_found_in_keyword(
    test_env: None, session: Session
) -> None:
    _chunk(session, "QS-POL-HR-002", "6. 绩效管理", "绩效按巡访覆盖率核算", None)
    hits, _ = search_knowledge(session, "绩效", client=FakeEmbedding(FREEZER_VEC))
    assert all(h.doc_id != "QS-POL-HR-002" for h in hits)
    hits_kw, mode = search_knowledge(session, "绩效", client=None)
    assert mode == "keyword" and hits_kw[0].doc_id == "QS-POL-HR-002"


def test_top_k_truncates(test_env: None, session: Session) -> None:
    for i in range(4):
        vec = [0.0] * 4
        vec[0] = 1.0 - i * 0.01  # 全部近似的向量，确保都被命中
        vec[1] = i * 0.01
        _chunk(session, "QS-T", f"节{i}", f"内容{i}", vec)
    hits, _ = search_knowledge(session, "内容", top_k=2, client=FakeEmbedding(FREEZER_VEC))
    assert len(hits) == 2
    assert hits[0].score >= hits[1].score


def test_empty_query_returns_nothing(test_env: None, session: Session, seeded: None) -> None:
    assert search_knowledge(session, "   ", client=FakeEmbedding(FREEZER_VEC))[0] == []


# ---------------------------------------------------------------------------
# 工具级（经网关 + MockMcpClient）
# ---------------------------------------------------------------------------


def _client(session: Session, role_code: str) -> mcp.MockMcpClient:
    actor = ActorContext(
        user_id=uuid4(), role_code=role_code, region_ids=frozenset(), brand_ids=frozenset()
    )
    registry = mcp.bind_read_handlers(mcp.build_default_registry())
    context = McpContext.from_actor(
        actor, session=session, request_id="t-kb", session_id="t-kb-session"
    )
    return mcp.MockMcpClient(McpGateway(registry), context)


@pytest.fixture
def force_keyword(monkeypatch: pytest.MonkeyPatch) -> None:
    """全局 embedding 单例降级为 None → 工具走关键词模式。"""
    monkeypatch.setattr(
        "app.services.knowledge_search.get_embedding_client", lambda: None
    )


def test_spec_registered_with_all_roles() -> None:
    assert "knowledge.search" in PRD_TOOL_CATALOG
    spec = next(s for s in ALL_TOOL_SPECS if s.name == "knowledge.search")
    assert frozenset(ALL_ROLES) == spec.allowed_roles
    assert "query" in spec.input_schema["required"]


@pytest.mark.parametrize("role", ALL_ROLES)
def test_tool_allows_every_role(
    test_env: None, session: Session, seeded: None, force_keyword: None, role: str
) -> None:
    result = _client(session, role).call("knowledge.search", query="冰柜报修")["result"]
    assert result["result_state"] == "OK", f"角色 {role} 应可访问知识库"


def test_tool_hit_returns_source_fields(
    test_env: None, session: Session, seeded: None, force_keyword: None
) -> None:
    result = _client(session, R_SALES).call("knowledge.search", query="冰柜报修")["result"]
    item = result["items"][0]
    assert item["doc_id"] == "QS-POL-FREEZER-003"
    assert item["section_path"] == "5. 报修与维修流程"
    assert "报修" in item["content"]
    assert float(item["score"]) > 0  # 信封 jsonable 红线：数值一律字符串化
    # 降级模式必须在信封上明示
    assert result["retrieval_mode"] == "keyword"
    assert "关键词匹配" in result["note"]


def test_tool_vector_mode_via_gateway(
    test_env: None, session: Session, seeded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.services.knowledge_search.get_embedding_client",
        lambda: FakeEmbedding(FREEZER_VEC),
    )
    result = _client(session, R_MANAGER).call("knowledge.search", query="冰柜坏了")["result"]
    assert result["retrieval_mode"] == "vector"
    assert result["items"][0]["doc_id"] == "QS-POL-FREEZER-003"
    assert result.get("note") is None


def test_tool_no_match_returns_no_records(
    test_env: None, session: Session, seeded: None, force_keyword: None
) -> None:
    result = _client(session, R_SUPPLIER).call(
        "knowledge.search", query="完全无关的词汇组合"
    )["result"]
    assert result["result_state"] == "NO_RECORDS"
    assert result["reason_code"] == "KNOWLEDGE_NO_MATCH"
    assert "未在制度知识库中找到" in result["note"]

"""FallbackVectorStore 打分等价性与边界测试。

批量化余弦是纯优化，**不允许改变结果**：同样的候选集必须给出同样的排序与分数
（与逐行 `cosine_similarity` 一致）。另外把两个坑钉死：
零向量行的余弦必须是 0（而不是 NaN），维度不一致时退回逐行实现而不是崩掉。
"""

from __future__ import annotations

import pytest

from app.models.knowledge import DocumentChunk
from app.rag.vectorstore import FallbackVectorStore
from app.services.model_gateway import cosine_similarity

TENANT = "t-vector"
KB = "kb-vector"


def _chunk(session, index: int, embedding, *, level: str = "internal", kb: str = KB) -> DocumentChunk:
    row = DocumentChunk(
        tenant_id=TENANT,
        document_id="doc-1",
        kb_id="kb-id",
        kb_code=kb,
        chunk_index=index,
        content=f"切片 {index}",
        security_level=level,
        embedding=embedding,
    )
    session.add(row)
    session.flush()
    return row


def test_批量余弦与逐行余弦结果一致(session):
    query = [0.3, -0.5, 0.8, 0.1]
    vectors = [
        [0.2, 0.1, 0.9, 0.0],
        [-0.7, 0.4, 0.2, 0.5],
        [0.9, 0.9, 0.9, 0.9],
        [0.0, 0.0, 0.0, 0.0],  # 零向量：余弦定义为 0
        [0.1, -0.2, 0.3, 0.4],
    ]
    rows = [_chunk(session, i, vector) for i, vector in enumerate(vectors)]
    # 期望值用真实 id + 逐行 cosine_similarity 复算（批量实现必须与它逐位一致）
    expected = sorted(
        ((row.id, cosine_similarity(query, vector)) for row, vector in zip(rows, vectors)),
        key=lambda item: item[1],
        reverse=True,
    )

    got = FallbackVectorStore().search(
        session,
        tenant_id=TENANT,
        kb_codes=[KB],
        query_vector=query,
        top_k=5,
        security_levels=["internal"],
    )

    assert [chunk_id for chunk_id, _ in got] == [chunk_id for chunk_id, _ in expected]
    for (got_id, got_score), (want_id, want_score) in zip(got, expected, strict=True):
        assert got_id == want_id
        assert got_score == pytest.approx(want_score, abs=1e-6)


def test_零向量不产生_nan(session):
    _chunk(session, 0, [0.0, 0.0, 0.0])
    _chunk(session, 1, [1.0, 0.0, 0.0])

    got = FallbackVectorStore().search(
        session,
        tenant_id=TENANT,
        kb_codes=[KB],
        query_vector=[1.0, 0.0, 0.0],
        top_k=5,
        security_levels=["internal"],
    )

    scores = {chunk_id: score for chunk_id, score in got}
    assert all(score == score for score in scores.values()), "分数不能是 NaN"
    assert min(scores.values()) == 0.0
    assert max(scores.values()) == pytest.approx(1.0)


def test_维度不一致的脏行按零分处理而不是整体失败(session):
    """换过 embedding 模型但没重建索引时，多余的老向量不能把整个知识库检索搞崩。"""
    good = _chunk(session, 0, [0.1, 0.2, 0.3])
    _chunk(session, 1, [0.1, 0.2])  # 维度不一致（老数据）

    got = FallbackVectorStore().search(
        session,
        tenant_id=TENANT,
        kb_codes=[KB],
        query_vector=[0.1, 0.2, 0.3],
        top_k=5,
        security_levels=["internal"],
    )

    scores = {chunk_id: score for chunk_id, score in got}
    assert len(got) == 2
    assert scores[good.id] == pytest.approx(1.0, abs=1e-6)
    assert scores[[cid for cid, _ in got if cid != good.id][0]] == 0.0, "脏行应为 0 分（会被 min_score 过滤）"


def test_只召回指定知识库与密级(session):
    _chunk(session, 0, [1.0, 0.0])
    _chunk(session, 1, [1.0, 0.0], level="confidential")
    _chunk(session, 2, [1.0, 0.0], kb="other_kb")

    got = FallbackVectorStore().search(
        session,
        tenant_id=TENANT,
        kb_codes=[KB],
        query_vector=[1.0, 0.0],
        top_k=5,
        security_levels=["internal"],
    )

    assert len(got) == 1


def test_没有候选时返回空列表(session):
    got = FallbackVectorStore().search(
        session,
        tenant_id=TENANT,
        kb_codes=[KB],
        query_vector=[1.0, 0.0],
        top_k=5,
        security_levels=["internal"],
    )

    assert got == []


def test_top_k_截断(session):
    for index in range(6):
        _chunk(session, index, [1.0, float(index) / 10])

    got = FallbackVectorStore().search(
        session,
        tenant_id=TENANT,
        kb_codes=[KB],
        query_vector=[1.0, 0.0],
        top_k=3,
        security_levels=["internal"],
    )

    assert len(got) == 3

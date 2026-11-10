"""pgvector 政策排除集的回归测试。

背景（2026-11-08 接手审计发现）：`PgVectorStore.search` 在排除集非空时
在 `chunk_vectors` 上引用不存在的 `document_id` 列；构造好的参数没有传给
`execute`；异常回退到 Python 余弦检索时又没透传排除集。三个缺陷叠加的结果是：

- 生产 PostgreSQL + pgvector 且存在过期/未生效政策时，语句必然报错；
- 报错被 catch 后静默降级，且回退路径**不排除**这些政策；
- 评测里的 `expired_policy_leak_rate=0` 只在 SQLite 回退路径成立，测不到生产路径。

这里用两个不依赖真库的单元测试锁住 SQL 形状与回退透传；真实数据行为见
`tests/integration/test_pgvector_policy_exclusion.py`（需显式提供 PGVECTOR_TEST_DATABASE_URL）。
"""

from __future__ import annotations

import pytest

from app.rag.vectorstore import FallbackVectorStore, PgVectorStore


class _Result:
    def __init__(self, rows: list[tuple[str, float]]) -> None:
        self._rows = rows

    def fetchall(self):
        return self._rows


class _CaptureSession:
    """记录 search 发出的 SQL 与绑定参数。"""

    def __init__(self, *, rows: list[tuple[str, float]] | None = None, fail: bool = False) -> None:
        self.rows = rows or []
        self.fail = fail
        self.sql = ""
        self.params: dict = {}
        self.rolled_back = False

    def execute(self, statement, params=None):
        self.sql = str(statement)
        self.params = dict(params or {})
        if self.fail:
            raise RuntimeError("simulated pgvector failure")
        return _Result(self.rows)

    def rollback(self) -> None:
        self.rolled_back = True


def _store() -> PgVectorStore:
    store = PgVectorStore()
    store.table = "chunk_vectors"
    return store


def test_无排除集时不引入_document_chunks_join():
    session = _CaptureSession(rows=[("c1", 0.9)])
    rows = _store().search(
        session,  # type: ignore[arg-type]
        tenant_id="t1",
        kb_codes=["policy_kb"],
        query_vector=[1.0, 0.0, 0.0],
        top_k=5,
        security_levels=["public"],
    )

    assert rows == [("c1", 0.9)]
    assert "FROM chunk_vectors cv" in session.sql
    assert "JOIN document_chunks" not in session.sql
    assert "excluded" not in session.params


def test_排除集走_document_chunks_真值列且参数真的传下去():
    session = _CaptureSession(rows=[("c2", 0.8)])
    rows = _store().search(
        session,  # type: ignore[arg-type]
        tenant_id="t1",
        kb_codes=["policy_kb"],
        query_vector=[1.0, 0.0, 0.0],
        top_k=5,
        security_levels=["public"],
        exclude_document_ids=["doc-expired"],
    )

    assert rows == [("c2", 0.8)]
    # 1) 排除条件必须落在 document_chunks.document_id（chunk_vectors 没有该列）
    assert "JOIN document_chunks dc ON dc.id = cv.chunk_id" in session.sql
    assert "dc.document_id <> ALL(:excluded)" in session.sql
    assert "cv.document_id" not in session.sql
    # 2) 绑定参数必须真的包含 excluded（原实现构造后丢弃）
    assert session.params["excluded"] == ["doc-expired"]
    # 3) 不应触发降级
    assert session.rolled_back is False


def test_检索异常回退时仍透传政策排除集(monkeypatch):
    captured: dict = {}

    def fake_fallback_search(self, session, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(FallbackVectorStore, "search", fake_fallback_search)
    session = _CaptureSession(fail=True)

    rows = _store().search(
        session,  # type: ignore[arg-type]
        tenant_id="t1",
        kb_codes=["policy_kb"],
        query_vector=[1.0, 0.0, 0.0],
        top_k=5,
        security_levels=["public"],
        exclude_document_ids=["doc-expired"],
    )

    assert rows == []
    assert session.rolled_back is True
    assert captured["exclude_document_ids"] == ["doc-expired"], (
        "回退路径丢排除集等于让失效政策重新可见"
    )

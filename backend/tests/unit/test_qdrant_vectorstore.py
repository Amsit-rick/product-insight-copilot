"""Qdrant 向量档的单元测试（用假客户端，**不需要真服务**；真机验证见 scripts/verify_qdrant.py）。

重点不是"能调通"，而是三条不可让步的性质：

1. **权限过滤必须下推到向量库**：tenant_id / kb_code / security_level 三个条件一个都不能少，
   而且要在 query filter 里 —— 绝不能"先召回再在应用层过滤"（那等于越权数据已被召回，
   会进日志与缓存，安全性依赖调用方自觉）；
2. **检索失败要降级而不是把诊断搞挂**：与 pgvector 档同款行为（退回 Python 余弦）；
3. **换向量后端必须让旧缓存失效**：否则会出现"换了 Qdrant、页面还在吃 Python 余弦的结果"。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.models.knowledge import DocumentChunk
from app.rag import vectorstore as vs
from app.rag.vectorstore import (
    FallbackVectorStore,
    PgVectorStore,
    QdrantVectorStore,
    _point_id,
    get_vector_store,
    reset_vector_store,
)


@pytest.fixture(autouse=True)
def _reset_store():
    reset_vector_store()
    yield
    reset_vector_store()


class FakeQdrantClient:
    """只记录调用参数，不做真实网络请求。"""

    def __init__(self, *, existing: bool = True, fail_search: bool = False) -> None:
        self.existing = existing
        self.fail_search = fail_search
        self.created: list[dict] = []
        self.points: list = []
        self.deleted: list = []
        self.queries: list[dict] = []

    def collection_exists(self, name: str) -> bool:
        return self.existing

    def create_collection(self, *, collection_name, vectors_config):
        self.created.append({"name": collection_name, "config": vectors_config})

    def upsert(self, *, collection_name, points, wait=True):
        self.points.extend(points)

    def query_points(self, **kwargs):
        self.queries.append(kwargs)
        if self.fail_search:
            raise RuntimeError("qdrant down")
        limit = kwargs.get("limit", 10)
        limit = limit if isinstance(limit, int) else 10
        return SimpleNamespace(
            points=[
                SimpleNamespace(id=str(uuid.uuid4()), score=0.9 - index * 0.1, payload={"chunk_id": f"c-{index}"})
                for index in range(min(limit, 3))
            ]
        )

    def delete(self, *, collection_name, points_selector, wait=True):
        self.deleted.append(points_selector)


def qdrant_settings(**overrides) -> Settings:
    base = {
        "qdrant_url": "http://127.0.0.1:6333",
        "qdrant_collection": "pic_test",
        "embedding_dim": 8,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def make_store(monkeypatch, client: FakeQdrantClient, **settings_overrides) -> QdrantVectorStore:
    settings = qdrant_settings(**settings_overrides)
    monkeypatch.setattr("app.rag.vectorstore.get_settings", lambda: settings)
    store = QdrantVectorStore()
    monkeypatch.setattr(store, "client", lambda: client)
    return store


def add_chunk(session, *, chunk_id: str, tenant: str = "t1", kb: str = "product_kb", level: str = "internal"):
    session.add(
        DocumentChunk(
            id=chunk_id,
            tenant_id=tenant,
            document_id="doc-1",
            kb_id="kb-1",
            kb_code=kb,
            content="次日留存口径",
            chunk_index=0,
            document_version="v1",
            security_level=level,
        )
    )
    session.flush()


# --------------------------------------------------------------------- 过滤下推
def test_过滤三个条件全部下推(session, monkeypatch):
    client = FakeQdrantClient()
    store = make_store(monkeypatch, client)

    store.search(
        session,
        tenant_id="t1",
        kb_codes=["product_kb", "feedback_kb"],
        query_vector=[0.1] * 8,
        top_k=5,
        security_levels=["public", "internal"],
    )

    assert client.queries, "应当真的发起了查询"
    query_filter = client.queries[0]["query_filter"]
    conditions = {condition.key: condition for condition in query_filter.must}
    assert set(conditions) == {"tenant_id", "kb_code", "security_level"}, (
        "三个过滤条件必须全部下推 —— 少一个就是越权面（" + str(sorted(conditions)) + "）"
    )
    assert conditions["tenant_id"].match.value == "t1"
    assert sorted(conditions["kb_code"].match.any) == ["feedback_kb", "product_kb"]
    assert sorted(conditions["security_level"].match.any) == ["internal", "public"]


def test_检索结果按_payload_还原原始_chunk_id(session, monkeypatch):
    client = FakeQdrantClient()
    store = make_store(monkeypatch, client)

    hits = store.search(
        session, tenant_id="t1", kb_codes=["product_kb"], query_vector=[0.1] * 8, top_k=3,
        security_levels=["internal"],
    )
    assert [chunk_id for chunk_id, _ in hits] == ["c-0", "c-1", "c-2"]
    assert all(isinstance(score, float) for _, score in hits)


# --------------------------------------------------------------------- 写入
def test_upsert_带齐过滤与删除所需的_payload(session, monkeypatch):
    client = FakeQdrantClient()
    store = make_store(monkeypatch, client)
    chunk_id = str(uuid.uuid4())
    add_chunk(session, chunk_id=chunk_id)

    store.upsert(session, [chunk_id], [[0.2] * 8])

    assert len(client.points) == 1
    payload = client.points[0].payload
    # tenant/kb/security 是检索过滤要用的；document_id 是"删文档"要用的
    assert payload["tenant_id"] == "t1"
    assert payload["kb_code"] == "product_kb"
    assert payload["security_level"] == "internal"
    assert payload["document_id"] == "doc-1"
    assert payload["chunk_id"] == chunk_id, "原始 id 必须进 payload，检索时才能还原"


def test_非_uuid_的_chunk_id_被确定性映射(session, monkeypatch):
    """Qdrant 只接受 UUID/整数点 id；测试数据里常有 `chunk-1` 这种 id，不能让整条链路报错。"""
    client = FakeQdrantClient()
    store = make_store(monkeypatch, client)
    add_chunk(session, chunk_id="chunk-1")

    store.upsert(session, ["chunk-1"], [[0.3] * 8])

    point_id = client.points[0].id
    assert point_id == _point_id("chunk-1")
    assert uuid.UUID(point_id)  # 必须是合法 UUID
    assert _point_id("chunk-1") == _point_id("chunk-1"), "映射必须确定性（同 id 同点）"


# --------------------------------------------------------------------- 删除
def test_删除文档时按_id_与_document_id_双保险(session, monkeypatch):
    client = FakeQdrantClient()
    store = make_store(monkeypatch, client)
    add_chunk(session, chunk_id=str(uuid.uuid4()))

    store.delete_document(session, "doc-1")

    assert len(client.deleted) == 2, "既要按现存切片 id 删，也要按 payload.document_id 删"
    # 第二次是按 document_id 的过滤删除：切片行已经被删掉时，只有这条路能清掉孤儿向量
    second = client.deleted[1]
    assert second.filter.must[0].key == "document_id"
    assert second.filter.must[0].match.value == "doc-1"


def test_切片行已不存在时仍能按_document_id_清理(session, monkeypatch):
    """场景：先删了切片行（重建索引），再删文档 —— 此时库里查不到 chunk_id，
    只靠 id 删除会留下孤儿向量，那是**越权召回**。"""
    client = FakeQdrantClient()
    store = make_store(monkeypatch, client)

    store.delete_document(session, "doc-gone")

    assert len(client.deleted) == 1
    assert client.deleted[0].filter.must[0].match.value == "doc-gone"


# --------------------------------------------------------------------- 降级
def test_检索失败时降级到_python_余弦而不是抛异常(session, monkeypatch):
    client = FakeQdrantClient(fail_search=True)
    store = make_store(monkeypatch, client)
    add_chunk(session, chunk_id=str(uuid.uuid4()))

    hits = store.search(
        session, tenant_id="t1", kb_codes=["product_kb"], query_vector=[0.5] * 8, top_k=3,
        security_levels=["internal"],
    )
    assert isinstance(hits, list), "向量库故障不应让检索直接失败"


def test_未配置_qdrant_时不建连接(session, monkeypatch):
    settings = Settings(_env_file=None, qdrant_url="")
    monkeypatch.setattr("app.rag.vectorstore.get_settings", lambda: settings)
    store = QdrantVectorStore()
    assert store.configured is False


# --------------------------------------------------------------------- 选择逻辑
def test_auto_在配了_qdrant_时选_qdrant(session, monkeypatch):
    client = FakeQdrantClient(existing=False)
    # 注意顺序：**先**打补丁再构造 store —— QdrantVectorStore.__init__ 会读 get_settings()，
    # 反过来写的话它拿到的是真实配置（集合名会变成默认的 pic_chunks）。
    monkeypatch.setattr("app.rag.vectorstore.get_settings", lambda: qdrant_settings(vector_backend="auto"))
    store = QdrantVectorStore()
    monkeypatch.setattr(store, "client", lambda: client)
    monkeypatch.setattr("app.rag.vectorstore.QdrantVectorStore", lambda: store)

    chosen = get_vector_store(session)
    assert chosen.name == "qdrant"
    assert client.created and client.created[0]["name"] == "pic_test", "集合不存在时应自动创建"


def test_显式_qdrant_但连不上时降级而不是崩(session, monkeypatch):
    """显式指定 qdrant 也降级：检索退化成 Python 余弦仍然可用，比整个诊断起不来合理，
    前提是 /health 如实上报实际生效的后端。"""
    monkeypatch.setattr("app.rag.vectorstore.get_settings", lambda: qdrant_settings(vector_backend="qdrant"))

    def boom(self):
        raise ConnectionError("qdrant unreachable")

    monkeypatch.setattr(QdrantVectorStore, "ensure_schema", boom)
    chosen = get_vector_store(session)
    assert isinstance(chosen, FallbackVectorStore)

    from app.rag.vectorstore import describe_vector_store

    described = describe_vector_store()
    assert described["configured"] == "qdrant"
    assert described["effective"] == "fallback", "配置值与实际生效值必须分开上报"


def test_未配_qdrant_时_auto_仍按数据库方言(session, monkeypatch):
    settings = Settings(_env_file=None, qdrant_url="", vector_backend="auto")
    monkeypatch.setattr("app.rag.vectorstore.get_settings", lambda: settings)
    chosen = get_vector_store(session)
    assert isinstance(chosen, FallbackVectorStore)  # 临时库是 SQLite


# --------------------------------------------------------------------- 缓存键
def test_换向量后端会让检索缓存失效(session, monkeypatch):
    """回归：缓存键里必须带向量后端身份，否则换了 Qdrant 还在吃旧缓存。

    这里直接把选定好的 store 放进 `_store`（而不是让它去连真服务）——
    单元测试不该依赖任何外部服务。
    """
    from app.rag.retrieval import RetrievalService

    monkeypatch.setattr("app.rag.vectorstore.get_settings", lambda: qdrant_settings())
    service = RetrievalService()

    monkeypatch.setattr(vs, "_store", QdrantVectorStore())
    assert service._store_identity(session) == "qdrant:pic_test@8"

    monkeypatch.setattr(
        "app.rag.vectorstore.get_settings", lambda: qdrant_settings(qdrant_collection="另一个集合")
    )
    monkeypatch.setattr(vs, "_store", QdrantVectorStore())
    assert service._store_identity(session) == "qdrant:另一个集合@8", "换集合也要让缓存失效"

    monkeypatch.setattr(vs, "_store", PgVectorStore())
    assert service._store_identity(session).startswith("pgvector:")

    monkeypatch.setattr(vs, "_store", FallbackVectorStore())
    assert service._store_identity(session) == "fallback"

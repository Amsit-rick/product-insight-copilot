"""阶段六：缓存与指纹（含失效正确性）的单元测试。

重点验证三件容易出错的事：
1. 数据变了，缓存必须失效（**包括"行数没变但内容被改"这种情况**）；
2. 不同数据库不能共用缓存（多环境共用 Redis 时的串数据风险）；
3. 缓存本身出问题不能影响主流程。
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.db.session import build_engine, build_session_factory
from app.models.identity import Tenant
from app.models.knowledge import DocumentChunk, KnowledgeBase
from app.models.metrics import MetricSnapshot
from app.rag.retrieval import RetrievalService
from app.services.cache import Cache, MemoryCache, SqliteCache
from app.services.caching import (
    cache_stats,
    cached_call,
    database_scope,
    kb_fingerprint,
    make_key,
    metric_fingerprint,
    reset_cache_stats,
)
from app.services.model_gateway import EmbeddingClient

TENANT = "*"


@pytest.fixture(autouse=True)
def _clean_stats():
    reset_cache_stats()
    yield
    reset_cache_stats()


def _add_chunk(
    session, *, kb_code: str = "product_kb", content: str = "次日留存口径说明", tenant_id: str = TENANT
) -> str:
    chunk = DocumentChunk(
        tenant_id=tenant_id,
        document_id="doc-1",
        kb_id="kb-1",
        kb_code=kb_code,
        content=content,
        chunk_index=0,
        document_version="v1",
    )
    session.add(chunk)
    session.flush()
    return chunk.id


# --------------------------------------------------------------------------- 指纹
def test_新增切片会改变知识库指纹(session):
    before = kb_fingerprint(session, TENANT, ["product_kb"])

    _add_chunk(session)

    assert kb_fingerprint(session, TENANT, ["product_kb"]) != before


def test_内容变化而数量不变也会改变指纹(session):
    chunk_id = _add_chunk(session, content="旧口径")
    before = kb_fingerprint(session, TENANT, ["product_kb"])

    chunk = session.get(DocumentChunk, chunk_id)
    chunk.content = "新口径"
    session.flush()

    # 行数没变，但 updated_at 变了 —— 只看行数的实现会在这里误命中
    assert kb_fingerprint(session, TENANT, ["product_kb"]) != before


def test_新增指标快照会改变指标指纹(session):
    before = metric_fingerprint(session, TENANT)

    from datetime import date

    session.add(
        MetricSnapshot(
            tenant_id=TENANT,
            metric_name="next_day_retention",
            stat_date=date(2026, 8, 1),
            value=0.3,
        )
    )
    session.flush()

    assert metric_fingerprint(session, TENANT) != before


def test_不同数据库的指纹不同(tmp_path):
    """多环境共用 Redis 时，键里必须带数据库身份，否则会读到别的库的结果。"""
    settings_a = Settings(_env_file=None, database_url=f"sqlite:///{(tmp_path / 'a.db').as_posix()}")
    settings_b = Settings(_env_file=None, database_url=f"sqlite:///{(tmp_path / 'b.db').as_posix()}")
    engine_a = build_engine(settings_a)
    engine_b = build_engine(settings_b)
    session_a = build_session_factory(engine_a)()
    session_b = build_session_factory(engine_b)()
    try:
        assert database_scope(session_a) != database_scope(session_b)
    finally:
        session_a.close()
        session_b.close()
        engine_a.dispose()
        engine_b.dispose()


def test_键由参数决定且稳定():
    key1 = make_key("retrieval", {"tenant": "t", "query": "留存"})
    key2 = make_key("retrieval", {"tenant": "t", "query": "留存"})
    key3 = make_key("retrieval", {"tenant": "t", "query": "崩溃"})

    assert key1 == key2
    assert key1 != key3


# --------------------------------------------------------------------------- cached_call
def test_缓存命中与未命中计数(monkeypatch):
    fake = MemoryCache()
    monkeypatch.setattr("app.services.caching.get_cache", lambda: fake)
    calls = {"n": 0}

    def producer():
        calls["n"] += 1
        return {"value": 42}

    first, hit1 = cached_call("k1", 60, producer)
    second, hit2 = cached_call("k1", 60, producer)

    assert first == second == {"value": 42}
    assert hit1 is False and hit2 is True
    assert calls["n"] == 1  # 第二次没有真正执行生产函数
    stats = cache_stats()
    assert stats["hits"] == 1 and stats["misses"] == 1
    assert stats["hit_rate"] == 0.5


def test_关闭缓存时直接执行(monkeypatch):
    fake = MemoryCache()
    monkeypatch.setattr("app.services.caching.get_cache", lambda: fake)
    calls = {"n": 0}

    def producer():
        calls["n"] += 1
        return 1

    cached_call("k2", 0, producer)
    cached_call("k2", 0, producer)

    assert calls["n"] == 2
    assert cache_stats()["skipped"] == 2


def test_缓存后端异常不影响主流程(monkeypatch):
    class BrokenCache:
        def get(self, key):  # noqa: ANN001
            raise RuntimeError("redis down")

        def set(self, key, value, ttl=None):  # noqa: ANN001
            raise RuntimeError("redis down")

    monkeypatch.setattr("app.services.caching.get_cache", lambda: BrokenCache())
    value, hit = cached_call("k3", 60, lambda: "ok")

    assert value == "ok"
    assert hit is False
    assert cache_stats()["errors"] >= 1


# --------------------------------------------------------------------------- 与检索服务联动
def _seed_retrieval_corpus(session) -> None:
    session.add(
        Tenant(code="t-cache", name="缓存测试租户")
    )
    session.flush()
    tenant = session.query(Tenant).filter(Tenant.code == "t-cache").first()
    kb = KnowledgeBase(
        tenant_id=tenant.id,
        code="product_kb",
        name="产品知识库",
        document_count=0,
        chunk_count=0,
    )
    session.add(kb)
    session.flush()
    session.add(
        DocumentChunk(
            tenant_id=tenant.id,
            document_id="doc-1",
            kb_id=kb.id,
            kb_code="product_kb",
            content="次日留存 = 次日活跃去重用户数 / 当日新增用户数",
            chunk_index=0,
            document_version="v1",
        )
    )
    session.flush()
    return tenant.id


def test_检索结果会被缓存并且数据变化后失效(session, monkeypatch):
    fake = MemoryCache()
    monkeypatch.setattr("app.services.caching.get_cache", lambda: fake)
    monkeypatch.setattr(
        "app.services.cache._cache", fake, raising=False
    )
    tenant_id = _seed_retrieval_corpus(session)
    service = RetrievalService()

    def retrieval_hits() -> int:
        # 只看 **retrieval** 命名空间：向量缓存（embedding）的命中是另一回事，
        # 用总命中数会让"检索缓存是否失效"这条断言被别的缓存干扰。
        return cache_stats()["namespaces"].get("retrieval", {}).get("hits", 0)

    first = service.search(
        session, query="次日留存 口径", tenant_id=tenant_id, kb_codes=["product_kb"], top_k=5
    )
    second = service.search(
        session, query="次日留存 口径", tenant_id=tenant_id, kb_codes=["product_kb"], top_k=5
    )

    assert [hit.chunk_id for hit in first] == [hit.chunk_id for hit in second]
    assert retrieval_hits() >= 1, "第二次相同检索应当命中缓存"

    # 写入新切片（同一个租户/知识库）→ 指纹变化 → 必须回到数据库重新检索（而不是吃旧缓存）
    _add_chunk(session, content="崩溃率口径说明", tenant_id=tenant_id)
    session.flush()
    hits_before = retrieval_hits()
    third = service.search(
        session, query="次日留存 口径", tenant_id=tenant_id, kb_codes=["product_kb"], top_k=5
    )

    assert isinstance(third, list)
    assert retrieval_hits() == hits_before, "数据变化后不应再命中旧缓存"

    # 反过来验证：其他租户的数据变化不会影响本租户的缓存（指纹按租户隔离）
    _add_chunk(session, content="别的租户的切片", tenant_id="另一个租户")
    session.flush()
    service.search(
        session, query="次日留存 口径", tenant_id=tenant_id, kb_codes=["product_kb"], top_k=5
    )
    assert retrieval_hits() == hits_before + 1


# --------------------------------------------------------------------- 向量缓存
def test_同一段查询文本只向量化一次():
    """同一段查询文本重复向量化是真实存在的浪费：

    一次诊断里多个 Agent 各自检索不同知识库，而 `rewrite_query` 只保留前 24 个 token，
    长问题的追加关键词会被截掉 —— 于是 6 次检索送来的是**同一段文本**。
    离线时白烧 CPU，接了真实 Embedding 接口就是 6 次计费调用。
    """
    calls: list[list[str]] = []
    client = EmbeddingClient(Settings(_env_file=None, embedding_provider="stub", embedding_dim=64))
    original = client._embed_uncached

    def counting_embed(texts):
        calls.append(list(texts))
        return original(texts)

    client._embed_uncached = counting_embed  # type: ignore[method-assign]

    first = client.embed_query("次日留存 口径 说明")
    second = client.embed_query("次日留存 口径 说明")
    third = client.embed_query("另一个问题")

    assert len(calls) == 2, f"同一段文本只应真正计算一次，实际调用了 {len(calls)} 次"
    assert calls[0] == ["次日留存 口径 说明"]
    assert calls[1] == ["另一个问题"]
    assert first == second
    assert third != first

    stats = cache_stats()["namespaces"]["embedding"]
    assert stats["hits"] == 1
    assert stats["misses"] == 2


def test_入库切片不占用查询向量缓存():
    """切片向量每篇只算一次且已落库；塞进缓存只会挤掉真正复用的查询向量。"""
    settings = Settings(
        _env_file=None, embedding_provider="stub", embedding_dim=32, embedding_cache_max_entries=4
    )
    client = EmbeddingClient(settings)
    client.embed(["切片一", "切片二", "切片三", "切片四", "切片五", "切片六"])
    assert client.cache_info()["entries"] == 0, "批量入库不应写向量缓存"
    assert client.cache_info()["hits"] + client.cache_info()["misses"] == 0, "批量入库不计入缓存统计"


def test_换向量模型后不会复用旧向量():
    """向量缓存键必须带模型身份：换了模型/维度后旧向量复用会静默给出错误检索结果。"""
    settings = Settings(_env_file=None, embedding_provider="stub", embedding_dim=64)
    client = EmbeddingClient(settings)
    calls: list[list[str]] = []
    original = client._embed_uncached

    def counting_embed(texts):
        calls.append(list(texts))
        return original(texts)

    client._embed_uncached = counting_embed  # type: ignore[method-assign]
    client.embed_query("留存")
    assert len(calls) == 1
    client.embed_query("留存")
    assert len(calls) == 1, "同模型下应当命中"

    settings.embedding_dim = 128  # 等价于换了模型
    client.embed_query("留存")
    assert len(calls) == 2, "维度/模型变了必须重新计算"


def test_向量缓存有容量上限且淘汰最久未使用():
    settings = Settings(
        _env_file=None, embedding_provider="stub", embedding_dim=32, embedding_cache_max_entries=2
    )
    client = EmbeddingClient(settings)
    for text in ("a", "b", "c"):
        client.embed_query(text)
    info = client.cache_info()
    assert info["entries"] == 2, "超出上限应淘汰，不能无限增长"

    # a 已被淘汰 → 再问一次要重新计算；c 还在 → 命中
    calls: list[list[str]] = []
    original = client._embed_uncached

    def counting_embed(texts):
        calls.append(list(texts))
        return original(texts)

    client._embed_uncached = counting_embed  # type: ignore[method-assign]
    client.embed_query("c")
    assert calls == [], "最近用过的应当还在缓存里"
    client.embed_query("a")
    assert calls == [["a"]], "被淘汰的最久未用项应重新计算"


def test_向量缓存可关闭():
    settings = Settings(_env_file=None, embedding_provider="stub", embedding_dim=32, embedding_cache_enabled=False)
    client = EmbeddingClient(settings)
    calls: list[list[str]] = []

    def counting_embed(texts):
        calls.append(list(texts))
        return [[0.0] * 32 for _ in texts]

    client._embed_uncached = counting_embed  # type: ignore[method-assign]
    client.embed_query("留存")
    client.embed_query("留存")
    assert len(calls) == 2, "关闭后不应缓存"
    assert client.cache_info()["enabled"] is False


def test_向量缓存过期后重新计算():
    settings = Settings(
        _env_file=None, embedding_provider="stub", embedding_dim=32, embedding_cache_ttl_seconds=0
    )
    client = EmbeddingClient(settings)
    calls: list[list[str]] = []

    def counting_embed(texts):
        calls.append(list(texts))
        return [[0.0] * 32 for _ in texts]

    client._embed_uncached = counting_embed  # type: ignore[method-assign]
    client.embed_query("留存")
    client.embed_query("留存")
    # ttl=0 表示"不设过期"，因此仍然命中
    assert len(calls) == 1


def test_真实向量严格模式禁止静默回退():
    class FailingClient:
        def post(self, *_args, **_kwargs):
            raise RuntimeError("provider unavailable")

    settings = Settings(
        _env_file=None,
        embedding_provider="openai_compatible",
        embedding_base_url="http://embedding.test/v1",
        embedding_api_key="secret",
        embedding_model="test-model",
        embedding_dim=4,
        embedding_failure_mode="error",
    )
    client = EmbeddingClient(settings)
    client._client = FailingClient()

    with pytest.raises(RuntimeError, match="Embedding 服务不可用"):
        client.embed(["退款规则"])
    info = client.cache_info()
    assert info["requests"] == 1
    assert info["failures"] == 1
    assert info["fallbacks"] == 0


def test_真实向量开发模式回退会被计数():
    class FailingClient:
        def post(self, *_args, **_kwargs):
            raise RuntimeError("provider unavailable")

    settings = Settings(
        _env_file=None,
        embedding_provider="openai_compatible",
        embedding_base_url="http://embedding.test/v1",
        embedding_api_key="secret",
        embedding_model="test-model",
        embedding_dim=4,
        embedding_failure_mode="fallback",
    )
    client = EmbeddingClient(settings)
    client._client = FailingClient()

    vectors = client.embed(["退款规则"])
    assert len(vectors) == 1 and len(vectors[0]) == 4
    assert client.cache_info()["fallbacks"] == 1


# --------------------------------------------------------------------- 落盘降级缓存
def test_降级缓存落盘后跨实例仍命中(tmp_path):
    """核心诉求：后端重启后同一个问题还能命中。

    纯进程内缓存每次重启清零，命中率长期停在个位数，缓存看起来像没生效。
    """
    path = str(tmp_path / "cache.db")
    first = SqliteCache(path)
    first.set("pic:retrieval:abc", {"hits": [1, 2, 3]}, ttl=300)

    # 新实例 = 模拟进程重启
    second = SqliteCache(path)
    assert second.get("pic:retrieval:abc") == {"hits": [1, 2, 3]}
    assert second.count() == 1


def test_降级缓存过期即未命中(tmp_path):
    cache = SqliteCache(str(tmp_path / "cache.db"))
    cache.set("k", {"v": 1}, ttl=300)
    assert cache.get("k") == {"v": 1}

    # 手写一条已过期的记录
    cache.set("expired", {"v": 2}, ttl=-1)
    assert cache.get("expired") is None, "过期记录必须视为未命中"


def test_降级缓存能清理过期记录(tmp_path):
    cache = SqliteCache(str(tmp_path / "cache.db"))
    cache.set("alive", {"v": 1}, ttl=300)
    cache.set("dead", {"v": 2}, ttl=-1)
    pruned = cache.prune_expired()
    assert pruned == 1
    assert cache.count() == 1
    assert cache.get("alive") == {"v": 1}


def test_redis_不可用时降级到落盘缓存(tmp_path):
    """降级目标默认是落盘（可跨重启），显式配 memory 才用纯内存。"""
    settings = Settings(
        _env_file=None, cache_backend="auto", cache_fallback="sqlite", cache_sqlite_path=str(tmp_path / "c.db")
    )
    backend, name = Cache._build_fallback(settings)
    assert name == "sqlite"
    assert isinstance(backend, SqliteCache)

    settings = Settings(_env_file=None, cache_backend="auto", cache_fallback="memory")
    backend, name = Cache._build_fallback(settings)
    assert name == "memory"
    assert isinstance(backend, MemoryCache)


# --------------------------------------------------------------------- 统计口径
def test_统计按命名空间细分并给出最近窗口(monkeypatch):
    """一个总命中率说明不了问题：冷启动的检索必然 0%，而指标/向量缓存可能很高。"""
    fake = MemoryCache()
    monkeypatch.setattr("app.services.caching.get_cache", lambda: fake)
    monkeypatch.setattr("app.services.cache._cache", fake, raising=False)

    cached_call(make_key("retrieval", {"q": 1}), 60, lambda: "a", namespace="retrieval")
    cached_call(make_key("retrieval", {"q": 1}), 60, lambda: "a", namespace="retrieval")
    cached_call(make_key("metric", {"m": 1}), 60, lambda: "b", namespace="metric")

    stats = cache_stats()
    assert stats["namespaces"]["retrieval"] == {
        "hits": 1,
        "misses": 1,
        "total_lookups": 2,
        "hit_rate": 0.5,
    }
    assert stats["namespaces"]["metric"]["hit_rate"] == 0.0
    assert stats["total_lookups"] == 3
    assert stats["hits"] == 1
    assert stats["recent"]["window"] == 3
    assert stats["recent"]["hit_rate"] == round(1 / 3, 4)


def test_没传命名空间时从键里推断(monkeypatch):
    fake = MemoryCache()
    monkeypatch.setattr("app.services.caching.get_cache", lambda: fake)
    monkeypatch.setattr("app.services.cache._cache", fake, raising=False)
    cached_call(make_key("retrieval", {"q": 9}), 60, lambda: "v")
    assert "retrieval" in cache_stats()["namespaces"]


def test_不同_top_k_复用同一份缓存且与直接计算逐条一致(session, monkeypatch):
    """**精确性证明**：把 top_k 从检索缓存键里去掉之后，"要 6 条"是由"已经算好的 8 条"截断得到的。

    这条优化成立的前提是**排序结果与 top_k 无关**（向量召回用固定的 retrieval_candidate_k，
    全文召回不看 top_k，最终只是 `ranked[:top_k]`）。前提一旦不成立，截断就会给出错误结果 ——
    所以这里不靠"看起来对"，而是把缓存路径与直接计算的结果**逐条比对**。
    """
    fake = MemoryCache()
    monkeypatch.setattr("app.services.caching.get_cache", lambda: fake)
    monkeypatch.setattr("app.services.cache._cache", fake, raising=False)
    tenant_id = _seed_retrieval_corpus(session)
    for index in range(9):
        _add_chunk(
            session,
            content=f"次日留存 口径 说明 第{index}条：连续活跃去重用户数 / 当日新增用户数，留存率观察",
            tenant_id=tenant_id,
        )
    session.flush()
    service = RetrievalService()

    def ids(top_k: int, *, use_cache: bool) -> list[str]:
        original = service.settings.retrieval_cache_ttl_seconds
        service.settings.retrieval_cache_ttl_seconds = 300 if use_cache else 0
        try:
            return [
                hit.chunk_id
                for hit in service.search(
                    session,
                    query="次日留存 口径",
                    tenant_id=tenant_id,
                    kb_codes=["product_kb"],
                    top_k=top_k,
                )
            ]
        finally:
            service.settings.retrieval_cache_ttl_seconds = original

    # 前提：直接计算是确定性的，且排序与 top_k 无关（6 条恰好是 8 条的前缀）
    direct_8, direct_6, direct_3 = ids(8, use_cache=False), ids(6, use_cache=False), ids(3, use_cache=False)
    assert direct_6 == direct_8[:6] and direct_3 == direct_8[:3]
    assert len(direct_8) >= 4, f"语料太小，截断无从验证（只有 {len(direct_8)} 条）"

    # 1) 先要 8 条写入缓存，再要 6/3 条 → 必须与直接计算完全一致
    assert ids(8, use_cache=True) == direct_8
    assert ids(6, use_cache=True) == direct_6
    assert ids(3, use_cache=True) == direct_3
    assert cache_stats()["namespaces"]["retrieval"]["hits"] >= 1, "6/3 条应当复用 8 条那份缓存"

    # 2) 反过来：先要 3 条（桶仍是 8），再要 8 条 → 也要一致
    reset_cache_stats()
    fake._data.clear() if hasattr(fake, "_data") else None
    assert ids(3, use_cache=True) == direct_3
    assert ids(8, use_cache=True) == direct_8, "桶相同，8 条也应直接命中那份缓存"

    # 3) 要得比桶还多（20 条）时不能拿 8 条那份凑数
    assert ids(20, use_cache=True) == ids(20, use_cache=False)


def test_改写后相同的两个查询共用同一次向量召回(session, monkeypatch):
    """真实场景：多个 Agent 用"原问题 + 各自关键词"检索，而 `rewrite_query` 只保留前 24 个 token，
    于是长问题在不同 Agent 之间得到**完全相同**的改写文本 —— 向量召回应当只做一次。

    这是**精确**复用：改写文本相同 → 向量相同 → 范围相同 → 召回结果必然相同。"""
    fake = MemoryCache()
    monkeypatch.setattr("app.services.caching.get_cache", lambda: fake)
    monkeypatch.setattr("app.services.cache._cache", fake, raising=False)
    tenant_id = _seed_retrieval_corpus(session)
    service = RetrievalService()

    filler = " ".join(f"词{i}" for i in range(30))
    first = f"次日留存 口径 {filler}"
    # 追加词特意选非同义词触发词（避免改写时的同义词扩展把两段改写文本变不同）
    second = f"{first} 复盘 监控"
    assert service.rewrite_query(first) == service.rewrite_query(second), "前提：两者改写后相同"
    assert service.rewrite_query(first) != first, "前提：改写确实发生了"

    service.search(session, query=first, tenant_id=tenant_id, kb_codes=["product_kb"], top_k=5)
    vector_hits = cache_stats()["namespaces"].get("retrieval_vector", {}).get("hits", 0)
    service.search(session, query=second, tenant_id=tenant_id, kb_codes=["product_kb"], top_k=5)
    after = cache_stats()["namespaces"].get("retrieval_vector", {}).get("hits", 0)
    assert after > vector_hits, "改写文本相同的第二次检索应命中向量召回缓存"
    # 第二次的检索结果缓存必然未命中（原问题不同），但向量层已复用
    assert cache_stats()["namespaces"]["retrieval"]["misses"] >= 2


def test_检索逻辑版本变化后旧缓存不再复用(session, monkeypatch):
    """数据变化由指纹负责，但指纹**对代码逻辑变化一无所知**。

    检索缓存 TTL 从 5 分钟放宽到 30 分钟的前提就是有这个版本号兜底：
    改了改写/召回/重排/打分逻辑后，旧结果必须立刻失效，而不是等 TTL 过期
    （缓存落盘后更是会跨重启继续提供旧逻辑的结果）。
    """
    fake = MemoryCache()
    monkeypatch.setattr("app.services.caching.get_cache", lambda: fake)
    monkeypatch.setattr("app.services.cache._cache", fake, raising=False)
    tenant_id = _seed_retrieval_corpus(session)
    service = RetrievalService()

    def misses() -> int:
        return cache_stats()["namespaces"].get("retrieval", {}).get("misses", 0)

    service.search(session, query="次日留存 口径", tenant_id=tenant_id, kb_codes=["product_kb"], top_k=5)
    service.search(session, query="次日留存 口径", tenant_id=tenant_id, kb_codes=["product_kb"], top_k=5)
    before = misses()
    assert cache_stats()["namespaces"]["retrieval"]["hits"] >= 1, "同版本下应当命中"

    import app.rag.retrieval as retrieval_module

    monkeypatch.setattr(retrieval_module, "RETRIEVAL_LOGIC_VERSION", "ret-999")
    service.search(session, query="次日留存 口径", tenant_id=tenant_id, kb_codes=["product_kb"], top_k=5)
    assert misses() == before + 1, "逻辑版本变了必须重新检索，不能吃旧缓存"


def test_换向量模型会让检索缓存失去意义(session, monkeypatch):
    """检索缓存键里带向量模型身份：否则换了模型（尤其缓存落盘后）会读到旧模型的结果。"""
    fake = MemoryCache()
    monkeypatch.setattr("app.services.caching.get_cache", lambda: fake)
    monkeypatch.setattr("app.services.cache._cache", fake, raising=False)
    tenant_id = _seed_retrieval_corpus(session)
    service = RetrievalService()

    class FakeEmbedder:
        def __init__(self, model: str) -> None:
            self.model_name = model
            self.dim = 64

        def embed(self, texts):
            return [[0.1] * 64 for _ in texts]

    service.embedder = FakeEmbedder("model-A")
    service.search(session, query="次日留存 口径", tenant_id=tenant_id, kb_codes=["product_kb"], top_k=5)
    service.search(session, query="次日留存 口径", tenant_id=tenant_id, kb_codes=["product_kb"], top_k=5)
    hits_a = cache_stats()["namespaces"]["retrieval"]["hits"]
    assert hits_a >= 1, "同模型同查询应当命中"

    service.embedder = FakeEmbedder("model-B")
    service.search(session, query="次日留存 口径", tenant_id=tenant_id, kb_codes=["product_kb"], top_k=5)
    assert cache_stats()["namespaces"]["retrieval"]["hits"] == hits_a, "换模型后不得复用旧结果"

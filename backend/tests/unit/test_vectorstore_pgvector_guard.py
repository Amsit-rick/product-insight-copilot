"""pgvector 初始化失败时的"体面降级"——这个 bug 只在真 Postgres 上会暴露。

背景（2026-09-15 真机验证发现）：Postgres 里**任何**语句失败都会把当前事务置为 aborted 状态，
之后同一事务里的语句一律报 `InFailedSqlTransaction`。原来的写法是

    try:
        session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))   # 失败
    except Exception:
        logger.warning("pgvector 初始化失败，回退 fallback")              # 看起来没事
        store = FallbackVectorStore()

在 SQLite 上完全正常（SQLite 没有这个语义），但换到没有 pgvector 扩展的 Postgres 上，
"回退成功"之后**调用方后续的查询全炸**，而且报错信息指向别处，非常难查。

两条防线，各有一个测试：
1. 先查 `pg_available_extensions` 再建扩展 —— 不该靠"发一条注定失败的语句"来判断；
2. 用 SAVEPOINT 包住初始化 —— 即使真失败了，也只回滚到保存点，不连累外层事务。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text as sql_text

from app.rag.vectorstore import (
    FallbackVectorStore,
    PgVectorStore,
    VectorStoreUnavailable,
    get_vector_store,
    reset_vector_store,
)


class _FakeResult:
    def __init__(self, row) -> None:
        self._row = row

    def first(self):
        return self._row


class _FakeSession:
    """只记录执行过哪些 SQL，用来断言"没有发出注定失败的语句"。"""

    def __init__(self, *, extension_available: bool) -> None:
        self.extension_available = extension_available
        self.statements: list[str] = []

    def execute(self, statement, *_args, **_kwargs):
        sql = str(statement)
        self.statements.append(sql)
        if "pg_available_extensions" in sql:
            return _FakeResult((1,) if self.extension_available else None)
        return _FakeResult(None)

    def commit(self) -> None:  # ensure_schema 成功后会提交
        self.statements.append("COMMIT")


def test_扩展不可用时直接给出明确错误且不发失败语句():
    session = _FakeSession(extension_available=False)

    with pytest.raises(VectorStoreUnavailable) as excinfo:
        PgVectorStore().ensure_schema(session)  # type: ignore[arg-type]

    assert "pgvector" in str(excinfo.value)
    executed = " ".join(session.statements)
    assert "pg_available_extensions" in executed, "应当先探测扩展是否可用"
    assert "CREATE EXTENSION" not in executed, (
        "不该靠「发一条注定失败的 CREATE EXTENSION」来判断可用性 —— "
        "这正是会把调用方事务搞成 aborted 的根源"
    )


def test_扩展可用时才建表():
    session = _FakeSession(extension_available=True)

    PgVectorStore().ensure_schema(session)  # type: ignore[arg-type]

    executed = " ".join(session.statements)
    assert "CREATE EXTENSION" in executed
    assert "CREATE TABLE" in executed, "扩展可用时应继续建向量表"


def test_初始化失败不会污染调用方事务(session, monkeypatch):
    """核心回归：pgvector 初始化必须包在 SAVEPOINT 里。

    这里在 SQLite 上跑（没有 Postgres 的 aborted-transaction 语义，所以断言的是"有没有用保存点"
    这个实现约束）；真正的行为验证在真 Postgres 上做过（见 README §7.4 的多副本实测）。
    """
    reset_vector_store()
    nested_calls: list[bool] = []
    original_begin_nested = session.begin_nested

    def spy_begin_nested():
        nested_calls.append(True)
        return original_begin_nested()

    monkeypatch.setattr(session, "begin_nested", spy_begin_nested)

    def boom(_self, _session):
        raise RuntimeError("extension \"vector\" is not available")

    monkeypatch.setattr(PgVectorStore, "ensure_schema", boom)
    monkeypatch.setattr("app.rag.vectorstore.get_settings", _postgres_settings)

    store = get_vector_store(session)

    assert isinstance(store, FallbackVectorStore), "初始化失败必须回退到 fallback"
    assert nested_calls, "初始化必须包在 SAVEPOINT（begin_nested）里"
    assert not session.in_nested_transaction(), "保存点应当已经结束"
    # 最关键的一条：调用方的会话仍然可用（在真 Postgres 上就是"不报 InFailedSqlTransaction"）
    assert session.execute(sql_text("SELECT 1")).scalar() == 1
    reset_vector_store()


def test_回退只发生一次且被缓存(session, monkeypatch):
    """失败一次之后应缓存 fallback，避免每次检索都重试一遍注定失败的初始化。"""
    reset_vector_store()
    attempts: list[int] = []

    def boom(_self, _session):
        attempts.append(1)
        raise RuntimeError("nope")

    monkeypatch.setattr(PgVectorStore, "ensure_schema", boom)
    monkeypatch.setattr("app.rag.vectorstore.get_settings", _postgres_settings)

    first = get_vector_store(session)
    second = get_vector_store(session)
    assert isinstance(first, FallbackVectorStore) and first is second
    assert len(attempts) == 1, f"不应重复尝试初始化，实际尝试了 {len(attempts)} 次"
    reset_vector_store()


def _postgres_settings():
    """把配置伪装成"方言是 Postgres、向量后端 auto"，以触发 pgvector 分支。"""
    from app.config import Settings

    settings = Settings(_env_file=None, vector_backend="pgvector")
    return settings

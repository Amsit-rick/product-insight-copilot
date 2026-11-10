"""真实 PostgreSQL + pgvector 下的政策排除集回归。

单元测试只能验证 SQL 形状；这个用例验证"数据真的被排掉了"，因为该缺陷在
SQLite / Python 余弦路径上完全看不出来（原评测里的 expired_policy_leak_rate=0
就只覆盖了回退路径）。

默认跳过；显式提供隔离测试库后运行：

    $env:PGVECTOR_TEST_DATABASE_URL = "postgresql+psycopg://postgres:postgres@127.0.0.1:55432/postgres"
    ..\\.venv\\Scripts\\python.exe -m pytest tests/integration/test_pgvector_policy_exclusion.py -q

用例只在自己的随机 schema 内建表，结束后 DROP SCHEMA CASCADE，不触碰业务表。
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.rag.vectorstore import PgVectorStore

PG_URL = os.getenv("PGVECTOR_TEST_DATABASE_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="需要显式提供 PGVECTOR_TEST_DATABASE_URL（隔离的 PostgreSQL + pgvector）",
)


def test_pgvector_排除过期政策文档():
    engine = create_engine(PG_URL, future=True)
    if engine.dialect.name != "postgresql":
        engine.dispose()
        pytest.skip("PGVECTOR_TEST_DATABASE_URL 不是 PostgreSQL")

    schema = f"pgvector_excl_{uuid.uuid4().hex[:12]}"
    table = "chunk_vectors_excl_test"
    connection = engine.connect()
    try:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        # 必须保留 public：vector 扩展默认装在 public，漏掉它会让 vector(3) 解析失败。
        connection.execute(text(f'SET search_path TO "{schema}", public'))
        # 只建本用例需要的最小列；业务表结构见 migrations/001_init.sql。
        connection.execute(
            text(
                "CREATE TABLE document_chunks ("
                " id TEXT PRIMARY KEY,"
                " document_id TEXT NOT NULL)"
            )
        )
        connection.execute(
            text(
                f"CREATE TABLE {table} ("
                " chunk_id TEXT PRIMARY KEY,"
                " tenant_id TEXT NOT NULL,"
                " kb_code TEXT NOT NULL,"
                " security_level TEXT NOT NULL,"
                " embedding vector(3))"
            )
        )
        connection.execute(
            text(
                "INSERT INTO document_chunks (id, document_id) VALUES "
                "('c-active', 'doc-active'),"
                "('c-expired', 'doc-expired')"
            )
        )
        # c-expired 与查询向量更接近：如果排除失效，它一定会排第一。
        connection.execute(
            text(
                f"INSERT INTO {table} "
                "(chunk_id, tenant_id, kb_code, security_level, embedding) VALUES "
                "('c-active', 't1', 'policy_kb', 'public', '[0.8,0.1,0]'),"
                "('c-expired', 't1', 'policy_kb', 'public', '[1,0,0]')"
            )
        )
        connection.commit()

        session = Session(bind=connection)
        store = PgVectorStore()
        store.table = table
        try:
            hits = store.search(
                session,
                tenant_id="t1",
                kb_codes=["policy_kb"],
                query_vector=[1.0, 0.0, 0.0],
                top_k=5,
                security_levels=["public"],
                exclude_document_ids=["doc-expired"],
            )
            ids = [chunk_id for chunk_id, _score in hits]
            assert "c-expired" not in ids, "失效政策文档仍被召回，排除集没有生效"
            assert ids == ["c-active"], f"应只返回有效政策切片，实际 {ids}"

            # 不传排除集时两条都应返回，证明上一条不是"表里没数据"导致的假阴性。
            all_hits = store.search(
                session,
                tenant_id="t1",
                kb_codes=["policy_kb"],
                query_vector=[1.0, 0.0, 0.0],
                top_k=5,
                security_levels=["public"],
            )
            assert {chunk_id for chunk_id, _score in all_hits} == {"c-active", "c-expired"}
        finally:
            session.close()
    finally:
        try:
            # 前置语句失败时事务处于 aborted，必须先 rollback 否则清理语句也会被拒。
            connection.rollback()
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            connection.commit()
        finally:
            connection.close()
            engine.dispose()

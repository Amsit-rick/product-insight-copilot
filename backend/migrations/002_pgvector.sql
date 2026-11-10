-- =============================================================================
-- 产品智能诊断平台 / Product Insight Copilot -- 向量表迁移（002_pgvector）
-- =============================================================================
--
-- 【适用环境】PostgreSQL + pgvector（deploy/docker-compose.yml 中使用的
--   pgvector/pgvector:pg16 镜像已内置该扩展）。
--   执行：python -m app.db.migrate apply
--
-- 【本地 / 测试环境】SQLite 下没有 pgvector，向量检索退化为
--   FallbackVectorStore（读取 document_chunks.embedding 的 JSON 列，在 Python 侧
--   做余弦相似度），因此本地初始化请用 python -m app.db.seed，不要执行本文件。
--
-- 【与代码的对应关系】本文件与 app/rag/vectorstore.py 中 PgVectorStore.ensure_schema()
--   完全一致；后者是运行时的幂等兜底（CREATE ... IF NOT EXISTS），本文件保证
--   "迁移先行"的部署路径同样可用，两者执行顺序无论先后都不会冲突。
--
-- 【维度】embedding vector(1024) 中的 1024 必须与配置项 EMBEDDING_DIM
--   （app/config.py: embedding_dim，默认 1024）保持一致；
--   表名 chunk_vectors 对应 VECTOR_TABLE_NAME（默认 chunk_vectors），
--   若修改了表名，请同步修改本文件的表名与索引名。
--   更换 embedding 模型导致维度变化时：需要先 DROP 向量表，再按新维度重建并重新灌入向量。
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS chunk_vectors (
    chunk_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    kb_code TEXT NOT NULL,
    security_level TEXT NOT NULL,
    embedding vector(1024)
);

-- 过滤维度索引：检索总是按 tenant_id + kb_code 过滤（密级在 SQL 里再过滤）。
CREATE INDEX IF NOT EXISTS ix_chunk_vectors_kb ON chunk_vectors (tenant_id, kb_code);

-- -----------------------------------------------------------------------------
-- 近邻索引示例（默认注释掉，按需启用）
--
-- 重要：近邻索引必须在**表中已有数据之后再创建**。
--   - IVFFlat 需要用现有数据训练聚类中心，空表上建索引会导致召回率极低；
--   - HNSW 同样建议先导入数据再建索引，构建更快且参数更容易调优。
-- 因此这里只给出示例，正式环境请在向量灌入完成后再执行。
-- 查询使用 `embedding <=> query`（余弦距离，见 PgVectorStore.search），
-- 所以索引必须使用 vector_cosine_ops 对应的距离算子类。
--
-- 经验参数：行数 < 100 万时 lists ≈ rows / 1000；HNSW 的 m/ef_construction 越大越准也越慢。
-- -----------------------------------------------------------------------------
-- CREATE INDEX IF NOT EXISTS ix_chunk_vectors_embedding_ivfflat
--     ON chunk_vectors USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
-- ANALYZE chunk_vectors;
--
-- CREATE INDEX IF NOT EXISTS ix_chunk_vectors_embedding_hnsw
--     ON chunk_vectors USING hnsw (embedding vector_cosine_ops)
--     WITH (m = 16, ef_construction = 64);

"""向量存储抽象（设计文档 9.5 节）。

- `PgVectorStore`：Postgres + pgvector，使用独立的 chunk_vectors 表。
- `FallbackVectorStore`：读取 document_chunks.embedding（JSON），在 Python 侧做余弦检索，
  用于 SQLite 或没有 pgvector 扩展的环境（本地演示 / 测试）。**注意它是 O(n) 扫描**，
  实测容量与选型阈值见 `scripts/benchmark_retrieval.py` 与 README §7.2。

业务代码只依赖 `VectorStore` 接口，后续可替换为 Qdrant / Milvus。
"""

from __future__ import annotations

import uuid
from typing import Protocol

import numpy as np
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.logging_utils import get_logger
from app.models.knowledge import DocumentChunk
from app.services.model_gateway import cosine_similarity

logger = get_logger(__name__)


class VectorStoreUnavailable(RuntimeError):
    """向量库不可用（pgvector 扩展没装、Qdrant 连不上、维度不匹配……）。

    单独一个异常类型，是为了让"降级到 fallback"这件事在日志与测试里都能被明确识别，
    而不是混在一堆 Exception 里看不出到底为什么降级。
    """


_POINT_NAMESPACE = uuid.UUID("6f1d8d3e-2f4b-4a6c-9d7e-5b8c1a2f3e40")


def _point_id(chunk_id: str) -> str:
    """Qdrant 的点 id 必须是 UUID 或整数；我们的 chunk_id 是 UUID 字符串，直接用。

    但测试与早期数据里可能出现非 UUID 的 id（例如 `chunk-1`），那会让 Qdrant 直接报错。
    这里对非 UUID 做**确定性**的 UUID5 映射（同一个 id 永远映射到同一个点），
    并把原始 id 存进 payload，检索回来时还原 —— 既不丢信息，也不会因 id 形状而失败。
    """
    try:
        return str(uuid.UUID(str(chunk_id)))
    except (ValueError, AttributeError, TypeError):
        return str(uuid.uuid5(_POINT_NAMESPACE, str(chunk_id)))


class VectorStore(Protocol):
    name: str
    # 向量是否落在关系库里：决定"换后端要不要重建索引"（只有 fallback 是 True）
    persists_in_database: bool

    def identity(self) -> str:
        """缓存键用的身份标识：换了向量后端/集合/维度后，旧缓存必须失效。"""
        ...

    def upsert(self, session: Session, chunk_ids: list[str], vectors: list[list[float]]) -> None: ...

    def search(
        self,
        session: Session,
        *,
        tenant_id: str,
        kb_codes: list[str],
        query_vector: list[float],
        top_k: int,
        security_levels: list[str],
    ) -> list[tuple[str, float]]: ...

    def delete_document(self, session: Session, document_id: str) -> None: ...


class PgVectorStore:
    name = "pgvector"

    def identity(self) -> str:
        return f"pgvector:{get_settings().vector_table_name}@{get_settings().embedding_dim}"

    @property
    def persists_in_database(self) -> bool:
        # 向量在独立的 chunk_vectors 表里，切片表本身不留向量
        return False

    def __init__(self) -> None:
        self.settings = get_settings()
        self.table = self.settings.vector_table_name

    def ensure_schema(self, session: Session) -> None:
        # 先问"扩展装没装"，而不是直接 CREATE EXTENSION 试一下。
        # 为什么重要：Postgres 里**任何**语句失败都会把当前事务置为 aborted 状态，
        # 之后同一事务里的语句一律报 InFailedSqlTransaction —— 也就是说
        # "试一下失败了再回退"这种写法会顺手把调用方的事务毁掉。
        # （SQLite 没有这个语义，所以这个问题在 SQLite 上一条测试都测不出来，
        #   只有在真 Postgres 上才会暴露：2026-09-15 真机验证时踩到。）
        available = session.execute(
            sql_text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
        ).first()
        if available is None:
            raise VectorStoreUnavailable(
                "PostgreSQL 未安装 pgvector 扩展（pg_available_extensions 里没有 vector）："
                "请使用 pgvector/pgvector:pg16 镜像，或改用 VECTOR_BACKEND=fallback"
            )
        session.execute(sql_text("CREATE EXTENSION IF NOT EXISTS vector"))
        session.execute(
            sql_text(
                f"""
                CREATE TABLE IF NOT EXISTS {self.table} (
                    chunk_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    kb_code TEXT NOT NULL,
                    security_level TEXT NOT NULL,
                    embedding vector({self.settings.embedding_dim})
                )
                """
            )
        )
        session.execute(
            sql_text(
                f"CREATE INDEX IF NOT EXISTS ix_{self.table}_kb "
                f"ON {self.table} (tenant_id, kb_code)"
            )
        )
        session.commit()

    def upsert(self, session: Session, chunk_ids: list[str], vectors: list[list[float]]) -> None:
        if not chunk_ids:
            return
        rows = session.query(DocumentChunk).filter(DocumentChunk.id.in_(chunk_ids)).all()
        meta = {r.id: r for r in rows}
        for chunk_id, vector in zip(chunk_ids, vectors, strict=False):
            chunk = meta.get(chunk_id)
            if not chunk:
                continue
            session.execute(
                sql_text(
                    f"""
                    INSERT INTO {self.table} (chunk_id, tenant_id, kb_code, security_level, embedding)
                    VALUES (:chunk_id, :tenant_id, :kb_code, :security_level, CAST(:embedding AS vector))
                    ON CONFLICT (chunk_id) DO UPDATE SET embedding = EXCLUDED.embedding
                    """
                ),
                {
                    "chunk_id": chunk_id,
                    "tenant_id": chunk.tenant_id,
                    "kb_code": chunk.kb_code,
                    "security_level": chunk.security_level,
                    "embedding": "[" + ",".join(f"{v:.6f}" for v in vector) + "]",
                },
            )

    def search(
        self,
        session: Session,
        *,
        tenant_id: str,
        kb_codes: list[str],
        query_vector: list[float],
        top_k: int,
        security_levels: list[str],
        exclude_document_ids: list[str] | None = None,
    ) -> list[tuple[str, float]]:
        literal = "[" + ",".join(f"{v:.6f}" for v in query_vector) + "]"
        params = {
            "qv": literal,
            "tenant_id": tenant_id,
            "kb_codes": list(kb_codes),
            "levels": list(security_levels),
            "top_k": top_k,
        }
        # 政策排除集的真值在 document_chunks.document_id，而 chunk_vectors 只存
        # chunk_id/tenant_id/kb_code/security_level/embedding（见 ensure_schema）。
        # 原实现直接在 chunk_vectors 上写 `document_id <> ALL(:excluded)`：
        #   ① 该列不存在 → 语句报错；
        #   ② 构造好的 params 没传给 execute，`excluded` 也没带上；
        #   ③ 异常回退 fallback 时又没透传排除集。
        # 结果是"生产 pgvector 路径静默降级，并可能召回已失效政策"。这里改为
        # JOIN document_chunks 后过滤，避免加列迁移，且对存量数据立即生效。
        join_clause = ""
        excluded_clause = ""
        if exclude_document_ids:
            join_clause = " JOIN document_chunks dc ON dc.id = cv.chunk_id"
            excluded_clause = " AND dc.document_id <> ALL(:excluded)"
            params["excluded"] = list(exclude_document_ids)
        stmt = sql_text(
            f"""
            SELECT cv.chunk_id, 1 - (cv.embedding <=> CAST(:qv AS vector)) AS score
            FROM {self.table} cv{join_clause}
            WHERE cv.tenant_id = :tenant_id
              AND cv.kb_code = ANY(:kb_codes)
              AND cv.security_level = ANY(:levels){excluded_clause}
            ORDER BY cv.embedding <=> CAST(:qv AS vector)
            LIMIT :top_k
            """
        )
        try:
            rows = session.execute(stmt, params).fetchall()
        except Exception as exc:
            logger.warning("pgvector 检索失败，回退到 Python 余弦检索: %s", exc)
            session.rollback()
            return FallbackVectorStore().search(
                session,
                tenant_id=tenant_id,
                kb_codes=kb_codes,
                query_vector=query_vector,
                top_k=top_k,
                security_levels=security_levels,
                # 回退路径必须继续排除失效政策：否则"降级"会变成"合规失效"。
                exclude_document_ids=exclude_document_ids,
            )
        return [(row[0], float(row[1])) for row in rows]

    def delete_document(self, session: Session, document_id: str) -> None:
        chunk_ids = [
            row[0]
            for row in session.query(DocumentChunk.id)
            .filter(DocumentChunk.document_id == document_id)
            .all()
        ]
        if not chunk_ids:
            return
        session.execute(
            sql_text(f"DELETE FROM {self.table} WHERE chunk_id = ANY(:ids)"),
            {"ids": chunk_ids},
        )


class QdrantVectorStore:
    """Qdrant 向量库（服务端模式）：把 ANN 检索与**权限过滤**一起下推到向量库。

    为什么值得有这一档：
    - `FallbackVectorStore` 是 O(n) 全扫描（2026-11-09 实测 512 维、1000 条切片
      p50 ≈ 0.28s，10000 条 ≈ 2.01s）；`PgVectorStore` 把扫描下推数据库，Qdrant 是独立服务，
      ANN + **filterable HNSW**（带 payload 过滤仍走索引）+ 量化省内存，多副本天然共享索引。

    安全口径与另外两档**严格一致**（这是不可让步的部分）：
    - `tenant_id` / `kb_code` / `security_level` 三个条件**必须进 Qdrant 的 query filter**，
      绝不允许"先召回再在应用层过滤" —— 那等于越权数据已经被召回（会进日志、缓存与调用方），
      安全性依赖调用方自觉，正是本项目一直在避免的形态；
    - 版本 / 日期过滤仍由上层 `_pass_filters` 做（与 pgvector 档保持同一契约，避免行为分叉）。

    已知差异（如实记录，见 README §7.2）：Qdrant 默认是**近似**检索（HNSW），
    与另外两档的精确穷举在极端情况下 top-k 可能不同；`QDRANT_SEARCH_EF` 可调精度，
    小数据集上等价性由 `scripts/verify_qdrant.py` 实测对比。
    """

    name = "qdrant"

    @property
    def persists_in_database(self) -> bool:
        # 向量在 Qdrant 集合里，关系库不留
        return False

    def identity(self) -> str:
        """缓存键身份：集合名 + 维度 —— 换集合/换维度都要让检索缓存失效。"""
        return f"qdrant:{self.settings.qdrant_collection}@{self.settings.embedding_dim}"

    def __init__(self) -> None:
        self.settings = get_settings()
        self.collection = self.settings.qdrant_collection
        self._client = None

    # ------------------------------------------------------------------ 连接
    @property
    def configured(self) -> bool:
        return bool((self.settings.qdrant_url or "").strip())

    def client(self):
        """懒建客户端：没配 Qdrant 的环境（含 CI）不该因为 import 就失败。"""
        if self._client is None:
            from qdrant_client import QdrantClient  # 延迟导入

            self._client = QdrantClient(
                url=self.settings.qdrant_url,
                api_key=self.settings.qdrant_api_key or None,
                timeout=self.settings.qdrant_timeout_seconds,
                prefer_grpc=self.settings.qdrant_prefer_grpc,
            )
        return self._client

    def ensure_schema(self) -> None:
        """建集合（幂等）。连不上就抛异常，由 `get_vector_store` 决定降级到哪一档。"""
        from qdrant_client import models

        client = self.client()
        if not client.collection_exists(self.collection):
            client.create_collection(
                collection_name=self.collection,
                vectors_config=models.VectorParams(
                    size=self.settings.embedding_dim, distance=models.Distance.COSINE
                ),
            )

    # ------------------------------------------------------------------ 写入
    def upsert(self, session: Session, chunk_ids: list[str], vectors: list[list[float]]) -> None:
        from qdrant_client import models

        if not chunk_ids:
            return
        rows = session.query(DocumentChunk).filter(DocumentChunk.id.in_(chunk_ids)).all()
        meta = {row.id: row for row in rows}
        points = []
        for chunk_id, vector in zip(chunk_ids, vectors, strict=False):
            chunk = meta.get(chunk_id)
            if not chunk or not vector:
                continue
            points.append(
                models.PointStruct(
                    id=_point_id(chunk_id),
                    vector=list(vector),
                    payload={
                        # 原始 chunk_id 存进 payload：点 id 可能被改成 UUID5（见 _point_id），
                        # 检索回来必须还原成库里的 id，否则上层按 id 取切片会取不到。
                        "chunk_id": chunk_id,
                        "tenant_id": chunk.tenant_id,
                        "kb_code": chunk.kb_code,
                        "security_level": chunk.security_level,
                        # document_id 进 payload 是为了"删文档"能按过滤条件清理，
                        # 不依赖库里还留着那些切片行（行可能已经被删掉了）。
                        "document_id": chunk.document_id,
                    },
                )
            )
        if points:
            self.client().upsert(collection_name=self.collection, points=points, wait=True)

    # ------------------------------------------------------------------ 检索
    def _query_filter(
        self,
        *,
        tenant_id: str,
        kb_codes: list[str],
        security_levels: list[str],
        exclude_document_ids: list[str] | None = None,
    ):
        from qdrant_client import models

        # 三个条件全部作为 must 下推：少一个都是越权面
        must = [
            models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant_id)),
            models.FieldCondition(key="kb_code", match=models.MatchAny(any=list(kb_codes))),
            models.FieldCondition(
                key="security_level", match=models.MatchAny(any=list(security_levels))
            ),
        ]
        must_not = []
        if exclude_document_ids:
            # 过期/未生效政策在 ANN 查询阶段就排除（不做事后过滤，避免它们占满 top_k）
            must_not.append(
                models.FieldCondition(
                    key="document_id", match=models.MatchAny(any=list(exclude_document_ids))
                )
            )
        return models.Filter(must=must, must_not=must_not or None)

    def search(
        self,
        session: Session,
        *,
        tenant_id: str,
        kb_codes: list[str],
        query_vector: list[float],
        top_k: int,
        security_levels: list[str],
        exclude_document_ids: list[str] | None = None,
    ) -> list[tuple[str, float]]:
        from qdrant_client import models

        try:
            response = self.client().query_points(
                collection_name=self.collection,
                query=list(query_vector),
                query_filter=self._query_filter(
                    tenant_id=tenant_id,
                    kb_codes=kb_codes,
                    security_levels=security_levels,
                    exclude_document_ids=exclude_document_ids,
                ),
                limit=top_k,
                search_params=models.SearchParams(hnsw_ef=self.settings.qdrant_search_ef),
                with_payload=True,
            )
        except Exception as exc:
            # 与 pgvector 档同款降级：向量库单次检索失败不该让整个诊断挂掉
            logger.warning("Qdrant 检索失败，回退到 Python 余弦检索: %s", exc)
            return FallbackVectorStore().search(
                session,
                tenant_id=tenant_id,
                kb_codes=kb_codes,
                query_vector=query_vector,
                top_k=top_k,
                security_levels=security_levels,
            )
        results: list[tuple[str, float]] = []
        for point in response.points:
            payload = point.payload or {}
            chunk_id = str(payload.get("chunk_id") or point.id)
            results.append((chunk_id, float(point.score)))
        return results

    # ------------------------------------------------------------------ 删除
    def delete_document(self, session: Session, document_id: str) -> None:
        from qdrant_client import models

        client = self.client()
        # 双保险：① 按库里现存的切片 id 删；② 再按 payload.document_id 删一遍。
        # 只做 ① 的话，切片行先被删掉（例如重建索引）就会留下孤儿向量 —— 那是越权召回。
        chunk_ids = [
            row[0]
            for row in session.query(DocumentChunk.id)
            .filter(DocumentChunk.document_id == document_id)
            .all()
        ]
        if chunk_ids:
            client.delete(
                collection_name=self.collection,
                points_selector=models.PointIdsList(points=[_point_id(cid) for cid in chunk_ids]),
                wait=True,
            )
        client.delete(
            collection_name=self.collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="document_id", match=models.MatchValue(value=document_id)
                        )
                    ]
                )
            ),
            wait=True,
        )


class FallbackVectorStore:
    name = "fallback"

    @property
    def persists_in_database(self) -> bool:
        # 向量就写在 document_chunks.embedding 上（这是它与其他两档最大的区别）
        return True

    def identity(self) -> str:
        return "fallback"

    """无 pgvector 时的退路：库内候选集 + Python 余弦。

    复杂度是 **O(n)**：每次查询把该租户/知识库下的全部切片读回来算余弦。
    实测（`scripts/benchmark_retrieval.py`，512 维、本机 SQLite、并发 1）：
    1000 条切片 p50 ≈ 0.28s、4000 条 ≈ 0.88s、10000 条 ≈ 2.01s，近似线性增长；
    而且**加并发不提吞吐**（并发 4 的 QPS 与并发 1 基本相同，只是 P95 约放大 4 倍）。
    结论：**只适合演示与百级切片、单副本**；上千条切片或多副本请切 pgvector / Qdrant / Milvus
    （阈值与迁移路径见 README §7.2）。这里把余弦改成矩阵一次算，是为了让"算"的部分不再成为瓶颈，
    同时把真实瓶颈（向量随行存取）如实暴露出来，而不是靠调参掩盖。
    """

    name = "fallback"

    def upsert(self, session: Session, chunk_ids: list[str], vectors: list[list[float]]) -> None:
        rows = session.query(DocumentChunk).filter(DocumentChunk.id.in_(chunk_ids)).all()
        by_id = {r.id: r for r in rows}
        for chunk_id, vector in zip(chunk_ids, vectors, strict=False):
            chunk = by_id.get(chunk_id)
            if chunk is not None:
                chunk.embedding = vector
                chunk.embedding_version = get_settings().embedding_version

    def search(
        self,
        session: Session,
        *,
        tenant_id: str,
        kb_codes: list[str],
        query_vector: list[float],
        top_k: int,
        security_levels: list[str],
        exclude_document_ids: list[str] | None = None,
    ) -> list[tuple[str, float]]:
        query = (
            session.query(
                DocumentChunk.id,
                DocumentChunk.embedding,
                DocumentChunk.tenant_id,
                DocumentChunk.kb_code,
                DocumentChunk.security_level,
            )
            .filter(DocumentChunk.tenant_id == tenant_id)
            .filter(DocumentChunk.kb_code.in_(kb_codes))
            .filter(DocumentChunk.security_level.in_(security_levels))
            .filter(DocumentChunk.embedding.isnot(None))
        )
        if exclude_document_ids:
            # 排除在 SQL 里做（不是读出来再筛）：过期政策若占满候选，会把有效政策挤出 top_k
            query = query.filter(DocumentChunk.document_id.notin_(exclude_document_ids))
        rows = query.all()
        if not rows:
            return []

        chunk_ids: list[str] = []
        vectors: list[list[float]] = []
        for chunk_id, embedding, *_ in rows:
            if not embedding:
                continue
            chunk_ids.append(chunk_id)
            vectors.append(embedding)
        if not chunk_ids:
            return []

        scores = self._batch_cosine(query_vector, vectors)
        scored = list(zip(chunk_ids, scores, strict=True))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_k]

    @staticmethod
    def _batch_cosine(query_vector: list[float], vectors: list[list[float]]) -> list[float]:
        """一次矩阵乘法算完全部余弦，替代逐行 `cosine_similarity`。

        等价性由 `tests/unit/test_vectorstore_scoring.py` 断言（同样的排序与分数），
        批量化只是把 1000 次小的 numpy 构造/归一化合并成一次。

        容错：库里混入**维度不一致**的向量（换过 embedding 模型、老数据没重建索引）时，
        这些行按"余弦=0"处理并告警——不能因为几行脏数据让整个知识库检索失败。
        """
        try:
            matrix = np.asarray(vectors, dtype=np.float32)
            query = np.asarray(query_vector, dtype=np.float32)
        except (TypeError, ValueError):
            return FallbackVectorStore._per_row_cosine(query_vector, vectors)
        if matrix.ndim != 2 or matrix.size == 0 or query.ndim != 1:
            return FallbackVectorStore._per_row_cosine(query_vector, vectors)
        if matrix.shape[1] != query.shape[0]:
            return FallbackVectorStore._per_row_cosine(query_vector, vectors)

        norms = np.linalg.norm(matrix, axis=1)
        query_norm = float(np.linalg.norm(query))
        if query_norm == 0:
            return [0.0] * len(vectors)
        safe = np.where(norms == 0, 1.0, norms)
        similarities = (matrix @ query) / (safe * query_norm)
        # 零向量的余弦定义为 0（与 cosine_similarity 保持一致）
        similarities = np.where(norms == 0, 0.0, similarities)
        return [float(value) for value in similarities]

    @staticmethod
    def _per_row_cosine(query_vector: list[float], vectors: list[list[float]]) -> list[float]:
        """逐行兜底：维度不一致的行记 0 分并告警，其余照常计算。"""
        scores: list[float] = []
        mismatched = 0
        for item in vectors:
            try:
                if not item or len(item) != len(query_vector):
                    raise ValueError("dimension_mismatch")
                scores.append(cosine_similarity(query_vector, item))
            except Exception:
                mismatched += 1
                scores.append(0.0)
        if mismatched:
            logger.warning(
                "有 %s 条切片的向量维度与当前 embedding 不一致，已按 0 分处理；"
                "建议重建索引（POST /api/v1/knowledge-bases/{code}/reindex）",
                mismatched,
            )
        return scores

    def delete_document(self, session: Session, document_id: str) -> None:
        session.query(DocumentChunk).filter(DocumentChunk.document_id == document_id).delete(
            synchronize_session=False
        )


def _normalize(vectors: list[list[float]]) -> list[list[float]]:
    out = []
    for vector in vectors:
        arr = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(arr))
        out.append((arr / norm).tolist() if norm else arr.tolist())
    return out


_store: VectorStore | None = None


def describe_vector_store(settings: Settings | None = None) -> dict:
    """把"配的是什么"与"实际生效的是哪个"分开上报。

    为什么需要：`vector_backend=auto` 时 `/health` 过去只回显配置值 `auto`，
    看不出实际用的是 pgvector 还是 Python 余弦 —— 排查检索性能问题时这是第一条要看的信息。
    """
    settings = settings or get_settings()
    return {
        "configured": settings.vector_backend,
        "effective": _store.name if _store is not None else None,
        "identity": _store.identity() if _store is not None else None,
        "qdrant_url": settings.qdrant_url or None,
        "qdrant_collection": settings.qdrant_collection if (settings.qdrant_url or "").strip() else None,
        "embedding_dim": settings.embedding_dim,
    }


def get_vector_store(session: Session | None = None) -> VectorStore:
    """按配置与可用性选择实现。

    `auto` 的顺序：**配了 QDRANT_URL → qdrant**；否则数据库是 Postgres → pgvector；否则 fallback。
    每一档初始化失败都逐级降级（Qdrant 连不上 → pgvector/fallback），并在日志里说清原因。
    """
    global _store
    if _store is not None:
        return _store
    settings = get_settings()
    backend = settings.vector_backend

    want_qdrant = backend == "qdrant" or (
        backend == "auto" and bool((settings.qdrant_url or "").strip())
    )
    want_pgvector = backend == "pgvector" or (backend == "auto" and settings.is_postgres)

    store: VectorStore | None = None
    if want_qdrant:
        candidate = QdrantVectorStore()
        try:
            candidate.ensure_schema()  # 幂等建集合；连不上会抛
            store = candidate
            logger.info(
                "向量后端：Qdrant（%s，集合 %s）", settings.qdrant_url, settings.qdrant_collection
            )
        except Exception as exc:
            logger.warning("Qdrant 不可用，尝试下一档: %s", exc)
            if backend == "qdrant":
                # 显式指定 qdrant 时也降级而不是崩：检索退化为 Python 余弦仍然可用，
                # 只是慢；比起"整个诊断跑不起来"，降级是更合理的选择（/health 会如实上报）。
                store = None

    if store is None and want_pgvector:
        candidate_pg = PgVectorStore()
        if session is not None:
            try:
                # SAVEPOINT 内的失败只回滚到保存点，不会连累外层事务。
                # 没有这一层的话，任何一次 pgvector 初始化失败（扩展缺失、没权限建扩展、
                # 向量维度不匹配……）都会让调用方后续的查询全部炸掉 —— 而"回退 fallback"
                # 看上去还是成功的，故障因此变得非常难查。
                with session.begin_nested():
                    candidate_pg.ensure_schema(session)  # type: ignore[attr-defined]
                store = candidate_pg
            except Exception as exc:
                logger.warning("pgvector 初始化失败，回退 fallback: %s", exc)
        else:
            store = candidate_pg

    if store is None:
        store = FallbackVectorStore()
    _store = store
    return store


def reset_vector_store() -> None:
    global _store
    _store = None

"""检索服务（RetrievalService）。

流程（设计文档 9.3 节）：
    问题改写 -> 领域路由 -> 权限过滤 -> 向量召回 -> 全文召回 -> 去重与重排 -> 时间/版本过滤 -> 构建引用证据

权限过滤是强制步骤：任何一次召回都必须带 tenant + 密级 + 知识库白名单条件。
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime

import httpx
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import get_settings
from app.logging_utils import get_logger
from app.models.base import utcnow
from app.models.knowledge import Document, DocumentChunk
from app.services.model_gateway import get_embedding_client

logger = get_logger(__name__)

# 检索逻辑版本：**改动改写 / 召回 / 重排 / 过滤 / 打分逻辑时必须 +1**。
# 它进缓存键，作用是"代码变了，旧缓存自动失效" —— 数据变化由指纹负责，
# 但指纹对"代码逻辑变化"一无所知。有了它，检索缓存 TTL 才可以放长
# （否则改了打分逻辑还要等 TTL 过期，或者读到旧逻辑的结果）。
RETRIEVAL_LOGIC_VERSION = "ret-4"  # ret-4：分离召回绝对相关度与 reranker 排序 logits

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_WORD_RE = re.compile(r"[A-Za-z0-9_\.]+")


@dataclass
class RetrievalHit:
    chunk_id: str
    document_id: str
    kb_code: str
    content: str
    source_name: str
    page: int | None
    document_version: str
    updated_at: str | None
    relevance_score: float
    permission_checked: bool = True
    match_type: str = "hybrid"
    app_version: str | None = None
    section_title: str | None = None
    scores: dict = field(default_factory=dict)

    def as_evidence(self) -> dict:
        return {
            "document_id": self.document_id,
            "chunk_id": self.chunk_id,
            "knowledge_base": self.kb_code,
            "content": self.content,
            "source_name": self.source_name,
            "page": self.page,
            "document_version": self.document_version,
            "updated_at": self.updated_at,
            "relevance_score": round(self.relevance_score, 4),
            "permission_checked": self.permission_checked,
            "match_type": self.match_type,
        }

    def to_cache(self) -> dict:
        """缓存载荷：必须能 JSON 往返，且重建后信息不丢。"""
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "kb_code": self.kb_code,
            "content": self.content,
            "source_name": self.source_name,
            "page": self.page,
            "document_version": self.document_version,
            "updated_at": self.updated_at,
            "relevance_score": self.relevance_score,
            "permission_checked": self.permission_checked,
            "match_type": self.match_type,
            "app_version": self.app_version,
            "section_title": self.section_title,
            "scores": self.scores,
        }

    @classmethod
    def from_cache(cls, payload: dict) -> "RetrievalHit":
        return cls(
            chunk_id=payload.get("chunk_id", ""),
            document_id=payload.get("document_id", ""),
            kb_code=payload.get("kb_code", ""),
            content=payload.get("content", ""),
            source_name=payload.get("source_name", ""),
            page=payload.get("page"),
            document_version=payload.get("document_version") or "v1",
            updated_at=payload.get("updated_at"),
            relevance_score=float(payload.get("relevance_score") or 0.0),
            permission_checked=bool(payload.get("permission_checked", True)),
            match_type=payload.get("match_type") or "hybrid",
            app_version=payload.get("app_version"),
            section_title=payload.get("section_title"),
            scores=payload.get("scores") or {},
        )


def tokenize(text: str) -> list[str]:
    """中英混排分词：英文数字词 + 中文字符 + 中文二元组。"""
    words = [w.lower() for w in _WORD_RE.findall(text or "")]
    cjk = _CJK_RE.findall(text or "")
    bigrams = [cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)]
    return list(dict.fromkeys(words + cjk + bigrams))


def allowed_security_levels(max_rank: int) -> list[str]:
    from app.models.enums import SecurityLevel

    return [level.value for level in SecurityLevel if level.rank <= max_rank]


class RetrievalService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.embedder = get_embedding_client()

    # ------------------------------------------------------------------ 主入口
    def search(
        self,
        session: Session,
        *,
        query: str,
        tenant_id: str,
        kb_codes: list[str],
        max_security_rank: int = 1,
        top_k: int | None = None,
        app_version: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        department: str | None = None,
        # 政策生效期过滤（需求书 §4.3）：过期/未生效/范围不符的政策必须排除。
        # 默认按"现在"过滤 —— 这是**默认安全**：任何调用方都不可能"忘了传"而引用过期政策。
        policy_at: datetime | None = None,
        shop_id: str | None = None,
        platform: str | None = None,
        member_level: str | None = None,
    ) -> list[RetrievalHit]:
        if not kb_codes:
            return []
        top_k = top_k or self.settings.retrieval_top_k
        policy_at = policy_at or utcnow()
        excluded_documents = self._policy_exclusions(
            session,
            tenant_id=tenant_id,
            at=policy_at,
            shop_id=shop_id,
            platform=platform,
            member_level=member_level,
        )

        # 缓存键带上"知识库数据指纹"：入库/重建索引/删除文档后指纹变化，旧缓存自然失效，
        # 因此不需要维护"谁该清哪块缓存"的失效矩阵（那种代码最容易漏清理）。
        from app.services.caching import cached_call, kb_fingerprint, make_key

        cache_ttl = self.settings.retrieval_cache_ttl_seconds
        if cache_ttl > 0:
            # 按"够用的条数桶"缓存，而不是按调用方要的 top_k：
            # 排序结果**与 top_k 无关**（向量召回用固定的 retrieval_candidate_k，
            # 全文召回压根不看 top_k，最终只是 ranked[:top_k] 截断），
            # 所以"要 6 条"完全可以由"算好的 8 条"截断得到 —— 这是**精确**复用，不是近似。
            # 实测多 Agent 会分别要 6 与 8 条，不去掉这一维就白白多算一次全量检索。
            bucket = max(top_k, self.settings.retrieval_top_k)
            fingerprint = kb_fingerprint(session, tenant_id, kb_codes)
            key = make_key(
                "retrieval",
                {
                    "tenant": tenant_id,
                    "query": query,
                    "kbs": sorted(kb_codes),
                    "rank": max_security_rank,
                    "bucket": bucket,
                    "version": app_version,
                    "from": date_from.isoformat() if date_from else None,
                    "to": date_to.isoformat() if date_to else None,
                    "dept": department,
                    # 向量模型身份：换了 embedding 模型/维度后，旧结果绝不能复用。
                    # 缓存落盘之后这一条尤其重要——否则换模型重启后仍会读到旧模型的检索结果。
                    "embed": self._embed_identity,
                    # 向量后端身份：换了检索后端（fallback ↔ pgvector ↔ qdrant）或集合名后同样必须失效，
                    # 否则会出现"换了 Qdrant、页面却还在吃 Python 余弦算出来的结果"，看起来像没生效。
                    "store": self._store_identity(session),
                    # 检索逻辑版本：改了改写/召回/重排/打分逻辑后旧缓存必须失效
                    "logic": RETRIEVAL_LOGIC_VERSION,
                    "fp": fingerprint,
                    # 政策排除集：政策修订/生效/失效后，知识库指纹（切片）不变，
                    # 但可用政策变了 —— 不带上这一维就会拿到"引用过期政策"的旧结果。
                    "pol": hashlib.sha1(
                        ("|".join(excluded_documents)).encode("utf-8")
                    ).hexdigest()[:12]
                    if excluded_documents
                    else "none",
                    "pol_at": policy_at.date().isoformat(),
                },
            )

            def producer() -> list[dict]:
                return [
                    hit.to_cache()
                    for hit in self._search_uncached(
                        session,
                        query=query,
                        tenant_id=tenant_id,
                        kb_codes=kb_codes,
                        max_security_rank=max_security_rank,
                        top_k=bucket,
                        app_version=app_version,
                        date_from=date_from,
                        date_to=date_to,
                        department=department,
                        fingerprint=fingerprint,
                        exclude_document_ids=excluded_documents,
                    )
                ]

            payload, _cached = cached_call(key, cache_ttl, producer, namespace="retrieval")
            # 截断到调用方要的条数：ranked 是有序前缀，取前 top_k 与直接算 top_k 完全等价
            return [RetrievalHit.from_cache(item) for item in payload[:top_k]]

        return self._search_uncached(
            session,
            query=query,
            tenant_id=tenant_id,
            kb_codes=kb_codes,
            max_security_rank=max_security_rank,
            top_k=top_k,
            app_version=app_version,
            date_from=date_from,
            date_to=date_to,
            department=department,
            exclude_document_ids=excluded_documents,
        )

    def _policy_exclusions(
        self,
        session: Session,
        *,
        tenant_id: str,
        at: datetime,
        shop_id: str | None,
        platform: str | None,
        member_level: str | None,
    ) -> list[str]:
        """当前不可引用的政策文档（过期 / 未生效 / 范围不符 / 已归档）。

        为什么要单独一个方法而不是内联：这个集合同时进**查询条件**与**缓存键**，
        两处必须完全一致，否则会出现"缓存里存的是上一版政策的检索结果"。
        """
        from app.services.policy_service import PolicyService

        try:
            return PolicyService(session).unusable_document_ids(
                tenant_id=tenant_id,
                at=at,
                shop_id=shop_id,
                platform=platform,
                member_level=member_level,
            )
        except Exception as exc:  # pragma: no cover - 政策层异常不应让检索整体失败
            logger.warning("政策生效期过滤失败，按不过滤处理（需排查）: %s", exc)
            return []

    @property
    def _embed_identity(self) -> str:
        """向量模型身份：进缓存键，换模型/换维度后旧缓存自动失效。"""
        return f"{self.embedder.model_name}@{self.embedder.dim}"

    def _store_identity(self, session: Session) -> str:
        """向量后端身份（fallback / pgvector:表@维度 / qdrant:集合@维度）：同样进缓存键。"""
        from app.rag.vectorstore import get_vector_store

        try:
            return get_vector_store(session).identity()
        except Exception:  # pragma: no cover - 拿不到实现时退化为固定值，不能让检索挂掉
            return "unknown"

    def _search_uncached(
        self,
        session: Session,
        *,
        query: str,
        tenant_id: str,
        kb_codes: list[str],
        max_security_rank: int,
        top_k: int,
        app_version: str | None,
        date_from: date | None,
        date_to: date | None,
        department: str | None,
        fingerprint: str | None = None,
        exclude_document_ids: list[str] | None = None,
    ) -> list[RetrievalHit]:
        levels = allowed_security_levels(max_security_rank)

        rewritten = self.rewrite_query(query)
        vector_hits = self._vector_recall_cached(
            session,
            query=rewritten,
            tenant_id=tenant_id,
            kb_codes=kb_codes,
            levels=levels,
            app_version=app_version,
            date_from=date_from,
            date_to=date_to,
            fingerprint=fingerprint,
            exclude_document_ids=exclude_document_ids,
        )
        keyword_hits = self._keyword_recall(
            session,
            rewritten,
            tenant_id,
            kb_codes,
            levels,
            app_version,
            date_from,
            date_to,
            exclude_document_ids=exclude_document_ids,
        )

        merged: dict[str, RetrievalHit] = {}
        for hit in vector_hits + keyword_hits:
            existing = merged.get(hit.chunk_id)
            if existing is None:
                merged[hit.chunk_id] = hit
            else:
                existing.relevance_score = max(
                    existing.relevance_score, hit.relevance_score
                )
                existing.scores.update(hit.scores)
                if existing.match_type != hit.match_type:
                    existing.match_type = "hybrid"

        ranked = self._rerank(query, list(merged.values()))
        if department:
            ranked = [h for h in ranked if not h.section_title or department in (h.section_title or "")]
        # 绝对相关度下限：混合检索总会有 top_k 命中，没有下限就会出现
        # "任何问题都能拿到证据"的假象，进而让系统对无答案问题硬答。
        floor = self.settings.retrieval_min_score
        ranked = [h for h in ranked if h.relevance_score >= floor]
        return ranked[:top_k]

    # ------------------------------------------------------ 问题改写（规则 + 可选模型）
    def rewrite_query(self, query: str) -> str:
        """第一版使用规则改写：抽取关键词并补齐同义表达，避免引入模型延迟。"""
        tokens = tokenize(query)
        if not tokens:
            return query
        extra = []
        joined = query.lower()
        synonyms = {
            "留存": ["retention", "次日留存", "留存率"],
            "崩溃": ["crash", "闪退", "崩溃率"],
            "版本": ["release", "发版", "changelog"],
            "反馈": ["feedback", "工单", "评论"],
            "转化": ["conversion", "转化率"],
            "卡顿": ["性能", "帧率", "耗时"],
        }
        for key, values in synonyms.items():
            if key in joined:
                extra.extend(values)
        return " ".join(dict.fromkeys(tokens[:24] + extra))

    def _vector_recall_cached(
        self,
        session: Session,
        *,
        query: str,
        tenant_id: str,
        kb_codes: list[str],
        levels: list[str],
        app_version: str | None,
        date_from: date | None,
        date_to: date | None,
        fingerprint: str | None = None,
        exclude_document_ids: list[str] | None = None,
    ) -> list[RetrievalHit]:
        """向量召回（**只依赖"改写后的查询文本 + 范围"**）单独缓存一层。

        为什么值得单独一层：向量召回是这个系统里最贵的检索步骤（无 pgvector 时是
        把候选切片整体读回 Python 算余弦，实测 1000 条切片约 350ms 花在"取回+解码"上），
        而它只取决于改写后的文本、范围、密级与版本过滤 —— **与原问题的措辞无关**。

        关键性质：多个 Agent 的查询是"原问题 + 各自关键词"，而 `rewrite_query` 只保留
        前 24 个 token，于是长问题在不同 Agent 之间会得到**完全相同**的改写文本。
        按"原问题"做键时它们是 6 次独立的全量扫描，按"改写文本 + 范围"做键就能收敛。
        这是**精确**复用（向量相同、范围相同 → 召回结果必然相同），不含任何近似。
        """
        from app.services.caching import cached_call, make_key

        cache_ttl = self.settings.retrieval_cache_ttl_seconds
        if cache_ttl <= 0:
            return self._vector_recall(
                session,
                query,
                tenant_id,
                kb_codes,
                levels,
                app_version,
                date_from,
                date_to,
                exclude_document_ids=exclude_document_ids,
            )

        from app.services.caching import kb_fingerprint

        key = make_key(
            "retrieval_vector",
            {
                "tenant": tenant_id,
                "rewritten": query,
                "kbs": sorted(kb_codes),
                "levels": sorted(levels),
                "version": app_version,
                "from": date_from.isoformat() if date_from else None,
                "to": date_to.isoformat() if date_to else None,
                "embed": self._embed_identity,
                "store": self._store_identity(session),
                "logic": RETRIEVAL_LOGIC_VERSION,
                "fp": fingerprint or kb_fingerprint(session, tenant_id, kb_codes),
                # 政策排除集同样进这一层缓存键：向量召回也要随政策生效变化而失效
                "pol": hashlib.sha1(
                    ("|".join(exclude_document_ids or [])).encode("utf-8")
                ).hexdigest()[:12]
                if exclude_document_ids
                else "none",
            },
        )
        payload, _cached = cached_call(
            key,
            cache_ttl,
            lambda: [
                hit.to_cache()
                for hit in self._vector_recall(
                    session,
                    query,
                    tenant_id,
                    kb_codes,
                    levels,
                    app_version,
                    date_from,
                    date_to,
                    exclude_document_ids=exclude_document_ids,
                )
            ],
            namespace="retrieval_vector",
        )
        return [RetrievalHit.from_cache(item) for item in payload]

    # ------------------------------------------------------------------ 向量召回
    def _vector_recall(
        self,
        session: Session,
        query: str,
        tenant_id: str,
        kb_codes: list[str],
        levels: list[str],
        app_version: str | None,
        date_from: date | None,
        date_to: date | None,
        exclude_document_ids: list[str] | None = None,
    ) -> list[RetrievalHit]:
        from app.rag.vectorstore import get_vector_store

        try:
            # 查询向量走缓存：同一次诊断里多个 Agent 会用同一段改写后的文本检索不同知识库
            query_vector = self.embedder.embed_query(query)
        except Exception as exc:
            if self.settings.embedding_failure_mode == "error":
                raise RuntimeError("严格模式下向量化失败，禁止退化为纯关键词检索") from exc
            logger.warning("向量化失败，跳过向量召回: %s", exc)
            return []
        if not query_vector:
            return []
        store = get_vector_store(session)
        try:
            pairs = store.search(
                session,
                tenant_id=tenant_id,
                kb_codes=kb_codes,
                query_vector=query_vector,
                top_k=self.settings.retrieval_candidate_k,
                security_levels=levels,
                exclude_document_ids=exclude_document_ids,
            )
        except Exception as exc:
            logger.warning("向量检索失败: %s", exc)
            return []
        if not pairs:
            return []
        chunk_ids = [cid for cid, _ in pairs]
        chunks = {
            c.id: c
            for c in session.query(DocumentChunk)
            .filter(DocumentChunk.id.in_(chunk_ids))
            .all()
        }
        score_map = dict(pairs)
        hits: list[RetrievalHit] = []
        for chunk_id, score in pairs:
            chunk = chunks.get(chunk_id)
            if not chunk or not self._pass_filters(chunk, app_version, date_from, date_to):
                continue
            hits.append(self._to_hit(session, chunk, score * 0.6, {"vector": score}))
        return hits

    # ------------------------------------------------------------------ 全文召回
    def _keyword_recall(
        self,
        session: Session,
        query: str,
        tenant_id: str,
        kb_codes: list[str],
        levels: list[str],
        app_version: str | None,
        date_from: date | None,
        date_to: date | None,
        exclude_document_ids: list[str] | None = None,
    ) -> list[RetrievalHit]:
        terms = [t for t in tokenize(query) if len(t) >= 2][:12]
        if not terms:
            return []
        filters = [
            DocumentChunk.tenant_id == tenant_id,
            DocumentChunk.kb_code.in_(kb_codes),
            DocumentChunk.security_level.in_(levels),
        ]
        if exclude_document_ids:
            # 全文召回同样要排除不可引用的政策（与向量召回保持同一口径）
            filters.append(DocumentChunk.document_id.notin_(exclude_document_ids))
        if app_version:
            filters.append(
                or_(DocumentChunk.app_version == app_version, DocumentChunk.app_version.is_(None))
            )
        if date_from:
            filters.append(
                or_(DocumentChunk.doc_updated_at >= date_from, DocumentChunk.doc_updated_at.is_(None))
            )
        if date_to:
            filters.append(
                or_(DocumentChunk.doc_updated_at <= date_to, DocumentChunk.doc_updated_at.is_(None))
            )
        rows = (
            session.query(DocumentChunk)
            .filter(*filters)
            .limit(self.settings.retrieval_candidate_k * 4)
            .all()
        )
        hits: list[RetrievalHit] = []
        for chunk in rows:
            content = chunk.content or ""
            matched = sum(1 for t in terms if t in content.lower())
            if not matched:
                continue
            # 关键词覆盖度归一，权重 0.4
            score = (matched / len(terms)) * 0.4
            hits.append(self._to_hit(session, chunk, score, {"keyword": matched / len(terms)}))
        hits.sort(key=lambda h: h.relevance_score, reverse=True)
        return hits[: self.settings.retrieval_candidate_k]

    # ------------------------------------------------------------------ 重排
    def _rerank(self, query: str, hits: list[RetrievalHit]) -> list[RetrievalHit]:
        """可选 BGE-Reranker HTTP 服务；未配置时使用启发式加权。"""
        endpoint = self.settings.reranker_endpoint
        if endpoint and hits:
            try:
                resp = httpx.post(
                    endpoint,
                    json={
                        "query": query,
                        "documents": [h.content for h in hits],
                        "top_n": len(hits),
                    },
                    headers=(
                        {"Authorization": f"Bearer {self.settings.reranker_api_key}"}
                        if self.settings.reranker_api_key
                        else None
                    ),
                    timeout=self.settings.reranker_timeout_seconds,
                )
                resp.raise_for_status()
                data = resp.json()
                results = data.get("results")
                if not isinstance(results, list) or len(results) != len(hits):
                    raise ValueError("reranker result count mismatch")
                seen: set[int] = set()
                for item in results:
                    idx = item.get("index")
                    score = item.get("score")
                    if (
                        not isinstance(idx, int)
                        or isinstance(idx, bool)
                        or not 0 <= idx < len(hits)
                        or idx in seen
                        or not isinstance(score, (int, float))
                        or isinstance(score, bool)
                        or not math.isfinite(float(score))
                    ):
                        raise ValueError("reranker result shape mismatch")
                    seen.add(idx)
                    # Cross-encoder 常返回未校准 logits（BGE 可能全为负数）。
                    # 它只负责候选间排序，不能覆盖召回阶段的绝对相关度，否则后面的
                    # retrieval_min_score 会把“相关但 logit<0”的证据全部误删。
                    hits[idx].scores["rerank"] = float(score)
                hits.sort(key=lambda h: h.scores["rerank"], reverse=True)
                return hits
            except Exception as exc:
                if self.settings.reranker_failure_mode == "error":
                    raise RuntimeError("Reranker 服务不可用或响应不符合契约") from exc
                logger.warning("重排服务不可用，使用启发式排序: %s", exc)

        tokens = set(tokenize(query))
        for hit in hits:
            content = (hit.content or "").lower()
            overlap = sum(1 for t in tokens if t in content) / max(1, len(tokens))
            hit.scores["overlap"] = overlap
            hit.relevance_score = round(hit.relevance_score + overlap * 0.15, 4)
        hits.sort(key=lambda h: h.relevance_score, reverse=True)
        return hits

    # ------------------------------------------------------------------ 工具方法
    @staticmethod
    def _pass_filters(
        chunk: DocumentChunk, app_version: str | None, date_from: date | None, date_to: date | None
    ) -> bool:
        if app_version and chunk.app_version and chunk.app_version != app_version:
            return False
        if date_from and chunk.doc_updated_at and chunk.doc_updated_at < date_from:
            return False
        if date_to and chunk.doc_updated_at and chunk.doc_updated_at > date_to:
            return False
        return True

    @staticmethod
    def _to_hit(
        session: Session, chunk: DocumentChunk, score: float, scores: dict
    ) -> RetrievalHit:
        doc = session.get(Document, chunk.document_id)
        return RetrievalHit(
            chunk_id=chunk.id,
            document_id=chunk.document_id,
            kb_code=chunk.kb_code,
            content=chunk.content or "",
            source_name=(doc.source_name if doc else None) or chunk.source_name or "",
            page=chunk.page,
            document_version=chunk.document_version or "v1",
            updated_at=chunk.doc_updated_at.isoformat() if chunk.doc_updated_at else None,
            relevance_score=round(score, 4),
            app_version=chunk.app_version,
            section_title=chunk.section_title,
            scores=scores,
        )


_retrieval: RetrievalService | None = None


def get_retrieval_service() -> RetrievalService:
    global _retrieval
    if _retrieval is None:
        _retrieval = RetrievalService()
    return _retrieval

"""真机验证 Qdrant 档：把 demo 切片灌进真 Qdrant，与 Python 余弦档**逐条对比**。

为什么必须真跑：单元测试用的是假客户端，只能证明"我们发的过滤条件对不对"；
真机上才验证得了：集合能建、1024 维向量能写、**过滤真的生效**（跨租户/跨密级不串）、
`<=>` 语义与 Qdrant 的 COSINE 分数是否一致、以及"Qdrant 挂掉能不能降级"。

对比口径（与另外两档保持同一契约）：
- 同一批切片、同一个查询向量；
- 对比 top-k 的 **id 列表重合度**（Qdrant 默认 HNSW 是近似检索，小数据集上应当完全一致）；
- 对比分数（Qdrant COSINE 分数 = 余弦相似度，与 FallbackVectorStore 的口径相同）。

用法（先起 Qdrant，见 README §7.2）：

    docker run -d --name pic-qdrant -p 56333:6333 qdrant/qdrant:v1.13.2
    ..\\.venv\\Scripts\\python.exe scripts\\verify_qdrant.py

环境变量：MULTI_QDRANT（默认 http://127.0.0.1:56333）
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

QDRANT_URL = os.environ.get("MULTI_QDRANT", "http://127.0.0.1:56333")

# 用临时库 + stub 模型：不碰开发库、不花钱、不依赖真实 embedding
os.environ["DATABASE_URL"] = f"sqlite:///{(BACKEND / '.tmp' / 'qdrant_verify.db').as_posix()}"
os.environ["AUTO_INIT_DB"] = "true"
os.environ["AUTO_SEED"] = "true"
os.environ["LLM_PROVIDER"] = "stub"
os.environ["EMBEDDING_PROVIDER"] = "stub"
os.environ["LLM_API_KEY"] = ""
os.environ["AGENT_MODEL_OVERRIDES"] = "{}"
# 关键：**先用 fallback 入库**，让向量落在关系库里 —— 这样才有对比基线，
# 也才能复现"把向量搬到另一个后端"（= reindex）的真实动作。
# 直接以 VECTOR_BACKEND=qdrant 入库的话，关系库里根本不留向量，Python 余弦档就无从对比。
os.environ["VECTOR_BACKEND"] = "fallback"
os.environ["QDRANT_URL"] = QDRANT_URL
os.environ["QDRANT_COLLECTION"] = "pic_verify"
os.environ["CACHE_BACKEND"] = "memory"

for suffix in ("", "-wal", "-shm"):
    stale = Path(os.environ["DATABASE_URL"].replace("sqlite:///", "") + suffix)
    if stale.exists():
        stale.unlink()

from sqlalchemy import create_engine  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.seed import init_db, seed_all  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.knowledge import DocumentChunk  # noqa: E402
from app.rag.vectorstore import (  # noqa: E402
    FallbackVectorStore,
    QdrantVectorStore,
    describe_vector_store,
    reset_vector_store,
)
from app.services.model_gateway import get_embedding_client  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
lines: list[str] = []


def log(message: str = "") -> None:
    lines.append(message)
    print(message)


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    log(f"  [{'PASS' if ok else 'FAIL'}] {name}{' — ' + detail if detail else ''}")


def main() -> int:
    settings = get_settings()
    embedder = get_embedding_client()

    log("=== 0. 环境 ===")
    log(f"  数据库    : {settings.database_url}")
    log(f"  向量后端  : {settings.vector_backend}")
    log(f"  Qdrant    : {settings.qdrant_url}（集合 {settings.qdrant_collection}）")
    log(f"  embedding : {embedder.model_name}（{embedder.dim} 维）\n")

    init_db()
    seed_all()

    with SessionLocal() as session:
        # 租户 id 必须从库里取：seed 返回的 'demo' 是租户**编码**，切片的 tenant_id 是租户的 UUID。
        # 写死 "demo" 的话 Qdrant 过滤会命中 0 条（我第一次就是这么错的，表现为"查询 200 但没结果"）。
        from app.models.identity import Tenant

        tenant_id = session.query(Tenant).first().id
        all_chunks = session.query(DocumentChunk).all()
        with_vectors = [chunk for chunk in all_chunks if chunk.embedding]

        log("\n=== 1. 准备数据（向量必须先在关系库里，才能当对比基线）===")
        log(f"  租户 id = {tenant_id}")
        log(f"  切片总数 = {len(all_chunks)}，关系库里有向量的 = {len(with_vectors)}")
        log("  说明：向量写在哪里**由 store 决定** —— fallback 写关系库的 JSON 列，")
        log("        而 pgvector / qdrant 只写各自的向量库、关系库**不留**向量。")
        log("        所以'把后端换成 Qdrant'之后必须【重建索引】，第 2 步做的就是这件事。")
        if not with_vectors:
            check("关系库里存在向量（可作对比基线）", False,
                  "没有向量：请先用 VECTOR_BACKEND=fallback 入库，或跑 scripts/reindex_vectors.py")
            return 1
        check("关系库里存在向量（可作对比基线）", True, f"{len(with_vectors)} 条")

        # ------------------------------------------------------------ 建集合 + 灌向量
        store = QdrantVectorStore()
        log("\n=== 2. 建集合 + 灌向量（等价于 reindex 到 Qdrant）===")
        try:
            store.ensure_schema()
            check("集合可用（不存在则自动创建）", True, f"集合={store.collection}")
        except Exception as exc:
            check("集合可用（不存在则自动创建）", False, f"{type(exc).__name__}: {exc}")
            log("\n[中止] Qdrant 不可用，请先起服务（docker run -d -p 56333:6333 qdrant/qdrant:v1.13.2）")
            return 1

        chunks = with_vectors
        chunk_ids = [chunk.id for chunk in chunks]
        vectors = [list(chunk.embedding) for chunk in chunks]
        started = time.perf_counter()
        store.upsert(session, chunk_ids, vectors)
        elapsed = time.perf_counter() - started
        count = store.client().count(collection_name=store.collection, exact=True).count
        check("向量写入 Qdrant 且点数一致", count == len(chunk_ids),
              f"Qdrant={count} 切片={len(chunk_ids)}，耗时 {elapsed:.2f}s")

        # ------------------------------------------------------------ 检索对比
        log("\n=== 3. 与 Python 余弦档逐条对比（同一切片集、同一查询向量）===")
        queries = [
            "次日留存下降 版本变更 相关记录",
            "闪退 崩溃 工单 反馈",
            "灰度 回滚 发布记录",
            "指标口径 留存 定义",
        ]
        fallback = FallbackVectorStore()
        identical = 0
        score_gaps: list[float] = []
        for text in queries:
            vector = embedder.embed_query(text)
            qdrant_hits = store.search(
                session, tenant_id=tenant_id, kb_codes=["product_kb", "feedback_kb", "engineering_kb", "operation_kb"],
                query_vector=vector, top_k=5, security_levels=["public", "internal", "confidential"],
            )
            baseline = fallback.search(
                session, tenant_id=tenant_id, kb_codes=["product_kb", "feedback_kb", "engineering_kb", "operation_kb"],
                query_vector=vector, top_k=5, security_levels=["public", "internal", "confidential"],
            )
            same_ids = [cid for cid, _ in qdrant_hits] == [cid for cid, _ in baseline]
            identical += 1 if same_ids else 0
            if qdrant_hits and baseline:
                score_gaps.append(abs(qdrant_hits[0][1] - baseline[0][1]))
            log(f"  「{text[:18]}…」一致={same_ids} "
                f"Qdrant top1={qdrant_hits[0][1]:.4f} 余弦 top1={baseline[0][1]:.4f}")
        check("四组查询的 top-5 id 列表与 Python 余弦完全一致", identical == len(queries),
              f"{identical}/{len(queries)} 一致（Qdrant 默认 HNSW 近似；不一致说明 ef 需要调大）")
        check("首位分数与余弦口径一致（差 < 1e-4）",
              all(gap < 1e-4 for gap in score_gaps), f"最大差 {max(score_gaps):.6f}" if score_gaps else "无样本")

        # ------------------------------------------------------------ 过滤隔离
        log("\n=== 4. 权限过滤真的生效（安全不变量）===")
        vector = embedder.embed_query("留存")
        other_tenant = store.search(
            session, tenant_id="不存在的租户", kb_codes=["product_kb"], query_vector=vector, top_k=5,
            security_levels=["internal"],
        )
        check("换租户 → 召回为空（不串租户）", other_tenant == [], f"{len(other_tenant)} 条")

        wrong_kb = store.search(
            session, tenant_id=tenant_id, kb_codes=["not_a_kb"], query_vector=vector, top_k=5,
            security_levels=["internal"],
        )
        check("换知识库 → 召回为空", wrong_kb == [], f"{len(wrong_kb)} 条")

        high_only = store.search(
            session, tenant_id=tenant_id, kb_codes=["product_kb"], query_vector=vector, top_k=5,
            security_levels=["confidential"],
        )
        check("只放开 confidential 时，internal 切片不会被召回",
              all(item_id not in {c.id for c in chunks if c.security_level == "internal"} for item_id, _ in high_only),
              f"{len(high_only)} 条")

        # 低密级主体拿不到高密级切片（正向对照：放开全部时应当拿得到）
        all_levels = store.search(
            session, tenant_id=tenant_id, kb_codes=["product_kb"], query_vector=vector, top_k=5,
            security_levels=["public", "internal", "confidential"],
        )
        check("放开全部密级时能正常召回（正向对照）", len(all_levels) > 0, f"{len(all_levels)} 条")

        # ------------------------------------------------------------ 延迟对比
        log("\n=== 5. 延迟对比（同一查询各跑 10 次取中位数）===")

        def median_ms(fn, rounds: int = 10) -> float:
            timings = []
            for _ in range(rounds):
                started = time.perf_counter()
                fn()
                timings.append((time.perf_counter() - started) * 1000)
            timings.sort()
            return timings[len(timings) // 2]

        vector = embedder.embed_query("次日留存下降 版本变更 相关记录")
        qdrant_ms = median_ms(
            lambda: store.search(
                session, tenant_id=tenant_id,
                kb_codes=["product_kb", "feedback_kb", "engineering_kb", "operation_kb"],
                query_vector=vector, top_k=8, security_levels=["public", "internal", "confidential"],
            )
        )
        cosine_ms = median_ms(
            lambda: fallback.search(
                session, tenant_id=tenant_id,
                kb_codes=["product_kb", "feedback_kb", "engineering_kb", "operation_kb"],
                query_vector=vector, top_k=8, security_levels=["public", "internal", "confidential"],
            )
        )
        log(f"  切片数 {len(chunk_ids)}：Qdrant p50 {qdrant_ms:.1f}ms  vs  Python 余弦 p50 {cosine_ms:.1f}ms")
        log("  （注意：切片量级越大差距越明显；59 条时两者都在毫秒级，差距主要在"
            "'是否把向量读回 Python'这个结构性成本上）")

        # ------------------------------------------------------------ 删除
        log("\n=== 6. 删除文档时向量被一并清掉 ===")
        target = chunks[0]
        before = store.client().count(collection_name=store.collection, exact=True).count
        store.delete_document(session, target.document_id)
        after = store.client().count(collection_name=store.collection, exact=True).count
        check("按 document_id 清掉了该文档的全部向量", after < before, f"{before} → {after}")
        leftover = store.search(
            session, tenant_id=tenant_id, kb_codes=[target.kb_code], query_vector=list(target.embedding),
            top_k=5, security_levels=["public", "internal", "confidential"],
        )
        check("已删除文档的切片不再被召回",
              all(cid != target.id for cid, _ in leftover), f"{len(leftover)} 条")

    # ---------------------------------------------------------------- 降级
    log("\n=== 7. Qdrant 不可用时自动降级（不能把诊断搞挂）===")
    os.environ["VECTOR_BACKEND"] = "qdrant"  # 显式指定 qdrant
    os.environ["QDRANT_URL"] = "http://127.0.0.1:1"  # 打不通的地址
    get_settings.cache_clear()
    import app.config as config_module

    config_module.settings = config_module.get_settings()
    reset_vector_store()
    with SessionLocal() as session:
        from app.rag.vectorstore import get_vector_store

        chosen = get_vector_store(session)
        described = describe_vector_store()
        check("连不上 Qdrant 时降级到 Python 余弦（而不是让诊断起不来）",
              chosen.name == "fallback", f"实际生效={chosen.name}")
        check("配置值与实际生效值分开上报（便于排查）",
              described["configured"] == "qdrant" and described["effective"] == "fallback",
              f"{described['configured']} → {described['effective']}")

    failed = [item for item in RESULTS if not item[1]]
    log(f"\n=== 汇总：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ===")
    if failed:
        log("失败项：" + "、".join(item[0] for item in failed))

    out = BACKEND / ".tmp" / "qdrant_verify_out.txt"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwrote {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

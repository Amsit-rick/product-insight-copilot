"""把向量（重新）灌进当前配置的向量后端 —— **切换向量后端时必需的工具**。

为什么必需：向量写在哪里由 `VectorStore.upsert()` 决定 ——
`FallbackVectorStore` 把向量写在 `document_chunks.embedding`（JSON 列），
而 `pgvector` / `qdrant` 写进各自的向量库，**关系库里就不留向量了**。
因此"把后端从 fallback 换成 qdrant"之后，Qdrant 里是空的；反之换回 fallback 又读不到 DB 里的向量。
（这也是"换后端必须重建索引"这条运维要求的由来。）

两个数据来源，按可用性自动选择：

1. **关系库里已有向量**（当前后端是 fallback，或刚从 fallback 切过来）→ 直接搬过去，不重算，最快；
2. **关系库里没有向量**（当前后端是 pgvector/qdrant）→ 只能用 embedding 模型**重新计算**
   （stub 哈希向量是确定性的，结果与原来一致；真实模型下如果模型没变也一致）。

用法::

    # 先确认要灌进哪个后端（VECTOR_BACKEND / QDRANT_URL 等）
    ..\\.venv\\Scripts\\python.exe scripts\\reindex_vectors.py --dry-run
    ..\\.venv\\Scripts\\python.exe scripts\\reindex_vectors.py

幂等：可以反复执行（同 id 覆盖写）。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from sqlalchemy import func  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402
from app.models.knowledge import DocumentChunk  # noqa: E402
from app.rag.vectorstore import describe_vector_store, get_vector_store, reset_vector_store  # noqa: E402
from app.services.model_gateway import get_embedding_client  # noqa: E402

BATCH = 32


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重建向量索引（把向量灌进当前向量后端）")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写入")
    parser.add_argument("--batch", type=int, default=BATCH, help="每批条数")
    parser.add_argument("--kb", default=None, help="只处理某个知识库")
    args = parser.parse_args(argv)

    reset_vector_store()
    with SessionLocal() as session:
        store = get_vector_store(session)
        described = describe_vector_store()
        print("=== 目标向量后端 ===")
        print(f"  配置={described['configured']}  实际生效={store.name}  身份={store.identity()}")
        if described.get("qdrant_url"):
            print(f"  Qdrant: {described['qdrant_url']} 集合={described['qdrant_collection']}")

        query = session.query(DocumentChunk).filter(DocumentChunk.embedding.isnot(None))
        if args.kb:
            query = query.filter(DocumentChunk.kb_code == args.kb)
        chunks = query.all()
        total = session.query(func.count(DocumentChunk.id)).scalar() or 0

        print("\n=== 数据来源 ===")
        print(f"  切片总数={total}  关系库里有向量的={len(chunks)}")

        if not chunks:
            print("  关系库里没有向量 → 只能重新计算 embedding（用当前 embedding 模型）")
            if args.dry_run:
                print("\n[dry-run] 结束（未写入）")
                return 0
            all_chunks = session.query(DocumentChunk)
            if args.kb:
                all_chunks = all_chunks.filter(DocumentChunk.kb_code == args.kb)
            all_chunks = all_chunks.all()
            if not all_chunks:
                print("  没有切片可处理，结束")
                return 0
            embedder = get_embedding_client()
            started = time.perf_counter()
            written = 0
            for start in range(0, len(all_chunks), args.batch):
                batch = all_chunks[start : start + args.batch]
                vectors = embedder.embed([item.content for item in batch])
                store.upsert(session, [item.id for item in batch], vectors)
                written += len(batch)
                print(f"  已写入 {written}/{len(all_chunks)}")
            session.flush()
            print(f"\n完成：重新计算并写入 {written} 条，耗时 {time.perf_counter() - started:.1f}s")
            return 0

        if args.dry_run:
            print(f"\n[dry-run] 将把 {len(chunks)} 条已有向量搬进 {store.name}（未写入）")
            return 0

        started = time.perf_counter()
        for start in range(0, len(chunks), args.batch):
            batch = chunks[start : start + args.batch]
            store.upsert(session, [item.id for item in batch], [list(item.embedding) for item in batch])
            print(f"  已写入 {min(start + len(batch), len(chunks))}/{len(chunks)}")
        session.flush()
        print(f"\n完成：把 {len(chunks)} 条向量写入 {store.name}，耗时 {time.perf_counter() - started:.1f}s")
        print("提示：换后端后旧后端的向量**不会**自动清理；如需清理请用 reindex 接口或手动删除旧集合/表。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

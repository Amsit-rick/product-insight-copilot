"""重算知识库的统计字段（`document_count` / `chunk_count` / `index_status`）。

**为什么需要它**：这几个字段是为了"知识库列表不必每次 COUNT"而冗余存储的。正常写入
路径（上传 / 删除 / 重建索引）都会顺手刷新，但只要有一次刷新发生在状态落库**之前**，
冗余值就会和真实数据漂移 —— 典型症状是"文档删掉了、列表里没了，但知识库概览的数字
还是旧的"（`_refresh_kb_stats` 在 `autoflush=False` 的会话里先统计后 flush，2026-09-15
清理误传文档时踩到）。当时修了刷新逻辑，但**已经写坏的值**需要这个脚本重算一遍。

脚本是幂等的、可反复执行，只改冗余统计字段，不动文档/切片本身。

用法（在 `backend` 目录下）::

    ..\\.venv\\Scripts\\python.exe scripts\\repair_kb_stats.py --dry-run   # 只看差异
    ..\\.venv\\Scripts\\python.exe scripts\\repair_kb_stats.py             # 实际重算

退出码：0 = 正常（无论有没有需要修的值）；1 = 执行出错。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from sqlalchemy import func  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402
from app.models.enums import DocumentStatus  # noqa: E402
from app.models.knowledge import Document, DocumentChunk, KnowledgeBase  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重算知识库冗余统计字段")
    parser.add_argument("--dry-run", action="store_true", help="只打印差异，不写库")
    args = parser.parse_args(argv)

    changed = 0
    with SessionLocal() as session:
        kbs = session.query(KnowledgeBase).order_by(KnowledgeBase.code).all()
        if not kbs:
            print("没有知识库（库是空的？先跑 python -m app.db.seed）")
            return 0

        print(f"{'知识库':<18} {'文档(旧→新)':<16} {'切片(旧→新)':<16} 状态")
        print("-" * 72)
        for kb in kbs:
            # 与 DocumentService._refresh_kb_stats 保持同一口径：已软删除的文档不计入
            real_docs = int(
                session.query(func.count(Document.id))
                .filter(Document.kb_id == kb.id)
                .filter(Document.status != DocumentStatus.DELETED.value)
                .scalar()
                or 0
            )
            real_chunks = int(
                session.query(func.count(DocumentChunk.id))
                .filter(DocumentChunk.kb_id == kb.id)
                .scalar()
                or 0
            )
            real_status = "indexed" if real_chunks else "empty"

            old_docs, old_chunks = int(kb.document_count or 0), int(kb.chunk_count or 0)
            drifted = (old_docs, old_chunks, kb.index_status) != (real_docs, real_chunks, real_status)
            mark = "  ← 需修正" if drifted else ""
            print(
                f"{kb.code:<18} {old_docs:>4} → {real_docs:<9} {old_chunks:>4} → {real_chunks:<9} "
                f"{kb.index_status} → {real_status}{mark}"
            )

            if drifted and not args.dry_run:
                kb.document_count = real_docs
                kb.chunk_count = real_chunks
                kb.index_status = real_status
                changed += 1

        if args.dry_run:
            print("\n[dry-run] 未写库。去掉 --dry-run 才会实际修正。")
        else:
            session.commit()
            print(f"\n已重算，修正 {changed} 个知识库。")
            if changed:
                print("提示：知识库列表与工作台概览现在应与真实文档数一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

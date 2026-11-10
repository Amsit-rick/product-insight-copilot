"""切片：按标题、表格和语义切分（设计文档 9.2 节）。

- 表格整块保留，避免切断行列关系。
- 段落按标题聚合后进行滑窗切分，保留重叠以维持上下文。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import get_settings
from app.rag.parsers import Block


@dataclass
class ChunkDraft:
    content: str
    chunk_index: int
    block_type: str
    page: int | None
    section_title: str | None
    token_count: int


def _approx_tokens(text: str) -> int:
    """中英混排的近似 token 估算：中文按字符、英文按 4 字符折算。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    others = len(text) - cjk
    return cjk + max(1, others // 4)


def _split_long(text: str, size: int, overlap: int) -> list[str]:
    if len(text) <= size:
        return [text]
    pieces: list[str] = []
    start = 0
    step = max(1, size - overlap)
    while start < len(text):
        pieces.append(text[start : start + size])
        start += step
    return pieces


def build_chunks(blocks: list[Block], chunk_size: int | None = None, overlap: int | None = None) -> list[ChunkDraft]:
    settings = get_settings()
    size = chunk_size or settings.chunk_size
    ov = overlap or settings.chunk_overlap

    chunks: list[ChunkDraft] = []
    index = 0

    # 1) 表格与标题块单独成块
    pending: list[tuple[str, Block]] = []
    for block in blocks:
        text = (block.text or "").strip()
        if not text:
            continue
        if block.block_type == "table" or len(text) > size * 2:
            for piece in _split_long(text, size, ov):
                chunks.append(
                    ChunkDraft(
                        content=piece,
                        chunk_index=index,
                        block_type=block.block_type,
                        page=block.page,
                        section_title=block.section_title,
                        token_count=_approx_tokens(piece),
                    )
                )
                index += 1
            continue
        pending.append((text, block))

    # 2) 其余段落按同一标题聚合后滑窗合并
    buffer: list[str] = []
    buffer_len = 0
    meta: Block | None = None

    def flush() -> None:
        nonlocal buffer, buffer_len, index
        if not buffer:
            return
        joined = "\n".join(buffer)
        chunks.append(
            ChunkDraft(
                content=joined,
                chunk_index=index,
                block_type=meta.block_type if meta else "paragraph",
                page=meta.page if meta else None,
                section_title=meta.section_title if meta else None,
                token_count=_approx_tokens(joined),
            )
        )
        index += 1
        buffer, buffer_len = [], 0

    for text, block in pending:
        if meta is None:
            meta = block
        if meta.section_title != block.section_title:
            flush()
            meta = block
        if buffer_len + len(text) > size:
            flush()
            meta = block
        buffer.append(text)
        buffer_len += len(text)
    flush()

    return chunks

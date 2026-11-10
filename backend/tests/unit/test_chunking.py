"""切片（`app.rag.chunking`）单元测试。

关注三条真实约束：
- 表格块整块保留，绝不按字符切断行列关系；
- 超长文本按窗口切分且**保留重叠**，避免上下文在切点处断掉；
- 每个块都要有连续递增的 `chunk_index` 与可用的 token 估算。
"""

from __future__ import annotations

from app.rag.chunking import _approx_tokens, build_chunks
from app.rag.parsers import Block

SIZE = 50
OVERLAP = 10


def _blocks(*blocks: Block) -> list[Block]:
    return list(blocks)


def _rebuild(chunks, size: int, overlap: int) -> str:
    """按 size/step 布局把切片拼回原文。

    只有落在窗口内的字符会被保留，因此能同时验证"不丢字"与"重叠区完全一致"。
    """
    step = size - overlap
    merged = ""
    for index, chunk in enumerate(chunks):
        start = index * step
        if index == 0:
            merged = chunk.content
            continue
        covered = max(0, len(merged) - start)
        assert chunk.content[:covered] == merged[start:], "重叠区内容不一致"
        merged += chunk.content[covered:]
    return merged


def test_表格块整块保留不切分():
    """表格行数多但总长短于窗口时，必须原样成为一个切片。"""
    table_text = "平台 | 留存\nAndroid | 42%\niOS | 38%"
    chunks = build_chunks(
        _blocks(Block(text=table_text, block_type="table", page=3, section_title="留存")),
        chunk_size=SIZE,
        overlap=OVERLAP,
    )

    assert len(chunks) == 1
    assert chunks[0].content == table_text
    assert chunks[0].block_type == "table"
    assert chunks[0].page == 3
    assert chunks[0].section_title == "留存"
    assert chunks[0].chunk_index == 0


def test_超长表格按窗口切分但保留全部内容():
    """超过两倍窗口的表格仍要切分，且拼回后内容不丢字。"""
    table_text = "".join(f"行{i} | 数值{i}\n" for i in range(30))
    chunks = build_chunks(
        _blocks(Block(text=table_text, block_type="table")),
        chunk_size=SIZE,
        overlap=OVERLAP,
    )

    assert len(chunks) > 1
    assert all(chunk.block_type == "table" for chunk in chunks)
    assert all(len(chunk.content) <= SIZE for chunk in chunks)
    # 步长 = size - overlap，逐片拼接可无损还原原始文本（块首尾空白会被 strip）
    assert _rebuild(chunks, SIZE, OVERLAP) == table_text.strip()


def test_标题块自成一组并作为段落切片的起始内容():
    """同一标题下的段落聚合为一块；换标题即开新块。"""
    blocks = _blocks(
        Block(text="第一章 留存分析", block_type="heading", section_title="第一章 留存分析"),
        Block(text="次日留存下降 5 个百分点。", section_title="第一章 留存分析"),
        Block(text="第二章 崩溃分析", block_type="heading", section_title="第二章 崩溃分析"),
        Block(text="崩溃率上升 12%。", section_title="第二章 崩溃分析"),
    )
    chunks = build_chunks(blocks, chunk_size=200, overlap=OVERLAP)

    assert [c.content for c in chunks] == [
        "第一章 留存分析\n次日留存下降 5 个百分点。",
        "第二章 崩溃分析\n崩溃率上升 12%。",
    ]
    # 标题块作为聚合块的类型与标题来源
    assert chunks[0].block_type == "heading"
    assert chunks[0].section_title == "第一章 留存分析"
    assert chunks[1].section_title == "第二章 崩溃分析"


def test_长文本滑窗切分保留重叠():
    """长文本按 size 切片、步长 size-overlap，相邻两片必须有 overlap 个字符重叠。"""
    text = "".join(f"第{i:03d}句。" for i in range(20))  # 每个片段 6 字符，共 120 字符
    chunks = build_chunks(
        _blocks(Block(text=text, block_type="paragraph")),
        chunk_size=SIZE,
        overlap=OVERLAP,
    )

    assert len(chunks) > 2
    for previous, current in zip(chunks, chunks[1:]):
        assert previous.content[-OVERLAP:] == current.content[:OVERLAP]
    assert chunks[0].content == text[:SIZE]
    assert chunks[1].content == text[SIZE - OVERLAP : 2 * SIZE - OVERLAP]
    # 滑窗切分不得丢字：按步长重排后与原文一致
    assert _rebuild(chunks, SIZE, OVERLAP) == text


def test_切片索引连续且token估算为正():
    """chunk_index 从 0 开始连续递增；非空切片 token_count 必须 > 0。"""
    blocks = _blocks(
        Block(text="表格A | 表格B", block_type="table"),
        Block(text="A" * 300, section_title="长文"),
        Block(text="短段落", section_title="长文"),
    )
    chunks = build_chunks(blocks, chunk_size=SIZE, overlap=OVERLAP)

    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    assert all(c.token_count > 0 for c in chunks)
    assert all(c.content.strip() for c in chunks)
    # 中文按字计、非中文按 4 字符折算
    assert chunks[0].token_count == _approx_tokens(chunks[0].content)


def test_空白块被跳过():
    """纯空白块不产生切片，也不占索引。"""
    chunks = build_chunks(
        _blocks(
            Block(text="   \n  "),
            Block(text="有内容的段落"),
            Block(text="", block_type="table"),
        ),
        chunk_size=SIZE,
        overlap=OVERLAP,
    )

    assert [c.content for c in chunks] == ["有内容的段落"]
    assert chunks[0].chunk_index == 0


def test_空输入返回空列表():
    assert build_chunks([], chunk_size=SIZE, overlap=OVERLAP) == []


def test_默认窗口取配置值():
    """不传参数时使用 settings.chunk_size，超长文本按配置窗口切分。"""
    from app.config import get_settings

    size = get_settings().chunk_size
    text = "X" * (size * 2 + 50)
    chunks = build_chunks(_blocks(Block(text=text)))

    assert len(chunks) >= 2
    assert chunks[0].content == "X" * size

"""文档解析：PDF / Word / Excel / CSV / Markdown / JSON / TXT。

输出统一的 Block 结构，保留页码、标题与表格信息，供切片阶段使用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.logging_utils import get_logger

logger = get_logger(__name__)


@dataclass
class Block:
    """解析出的最小内容块。"""

    text: str
    block_type: str = "paragraph"  # paragraph | heading | table | list
    page: int | None = None
    section_title: str | None = None
    meta: dict = field(default_factory=dict)


class ParseError(Exception):
    pass


def parse_file(path: Path, suffix: str | None = None) -> list[Block]:
    suffix = (suffix or path.suffix).lower()
    if suffix == ".pdf":
        return _parse_pdf(path)
    if suffix == ".docx":
        return _parse_docx(path)
    if suffix in {".xlsx", ".xlsm"}:
        return _parse_xlsx(path)
    if suffix == ".csv":
        return _parse_csv(path)
    if suffix in {".md", ".markdown", ".txt"}:
        return _parse_text(path, markdown=suffix != ".txt")
    if suffix == ".json":
        return _parse_json(path)
    raise ParseError(f"不支持的文件类型: {suffix}")


def _clean(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def _parse_pdf(path: Path) -> list[Block]:
    """优先 pypdf；未安装时退回 PyMuPDF；两者都没有则明确报错（不静默返回空）。"""
    reader_error: str | None = None
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        blocks: list[Block] = []
        for index, page in enumerate(reader.pages, start=1):
            text = _clean(page.extract_text() or "")
            if not text:
                continue
            blocks.append(Block(text=text, page=index, block_type="paragraph"))
        if blocks:
            return blocks
        reader_error = "pypdf 未提取到文本"
    except ImportError:
        reader_error = "pypdf 未安装"
    except Exception as exc:
        reader_error = f"pypdf 解析失败: {exc}"

    try:
        import pymupdf  # type: ignore

        document = pymupdf.open(str(path))
        blocks = []
        for index, page in enumerate(document, start=1):
            text = _clean(page.get_text() or "")
            if text:
                blocks.append(Block(text=text, page=index, block_type="paragraph"))
        document.close()
        if blocks:
            return blocks
    except Exception as exc:
        raise ParseError(
            f"PDF 未提取到文本（{reader_error}；PyMuPDF 回退也失败: {exc}）。"
            "扫描件需要 OCR，请先转成文本再上传。"
        ) from exc

    raise ParseError(f"PDF 未提取到文本（{reader_error}），可能是扫描件，需要 OCR")


def _parse_docx(path: Path) -> list[Block]:
    """优先 python-docx；失败时使用标准库后备解析（`rag.office`）。"""
    primary_error: Exception | None = None
    try:
        import docx  # python-docx

        document = docx.Document(str(path))
        blocks: list[Block] = []
        current_title: str | None = None

        for para in document.paragraphs:
            text = _clean(para.text)
            if not text:
                continue
            style = (para.style.name or "").lower() if para.style else ""
            if style.startswith("heading") or style.startswith("标题"):
                current_title = text
                blocks.append(Block(text=text, block_type="heading", section_title=text))
            else:
                blocks.append(
                    Block(
                        text=text,
                        block_type="list" if style.startswith("list") else "paragraph",
                        section_title=current_title,
                    )
                )

        for table in document.tables:
            rows = []
            for row in table.rows:
                rows.append(" | ".join(_clean(cell.text) for cell in row.cells))
            content = "\n".join(r for r in rows if r)
            if content:
                blocks.append(Block(text=content, block_type="table", section_title=current_title))

        return blocks or [Block(text="")]
    except ImportError:
        pass
    except Exception as exc:
        # 部分由 WPS/旧版 Office 导出的文件会包含失效的外部关系，
        # python-docx 会在加载关系时整体失败，但正文 XML 仍然完整可读。
        # 后备解析只读取 word/document.xml，可在不修改源文件的前提下
        # 恢复正文；若后备解析也失败，下面会同时保留两条错误线索。
        primary_error = exc
        logger.warning("python-docx 解析失败，尝试标准库后备解析: %s", exc)

    from app.rag.office import OfficeParseError, extract_docx

    try:
        extracted = extract_docx(path)
    except OfficeParseError as exc:
        detail = f"；python-docx: {primary_error}" if primary_error else ""
        raise ParseError(f"{exc}{detail}") from exc

    blocks = []
    current_title = None
    for text, block_type, _style in extracted:
        if block_type == "heading":
            current_title = text
            blocks.append(Block(text=text, block_type="heading", section_title=text))
        else:
            blocks.append(Block(text=text, block_type="paragraph", section_title=current_title))
    return blocks or [Block(text="")]


def _parse_xlsx(path: Path) -> list[Block]:
    """优先 openpyxl；未安装时使用标准库后备解析（`rag.office`）。"""
    try:
        from openpyxl import load_workbook

        workbook = load_workbook(str(path), data_only=True, read_only=True)
        blocks: list[Block] = []
        for sheet in workbook.worksheets:
            rows: list[str] = []
            for row in sheet.iter_rows(values_only=True):
                cells = ["" if c is None else str(c) for c in row]
                line = " | ".join(cells).strip(" |")
                if line:
                    rows.append(line)
            if rows:
                blocks.append(
                    Block(
                        text="\n".join(rows),
                        block_type="table",
                        section_title=sheet.title,
                        meta={"sheet": sheet.title},
                    )
                )
        workbook.close()
        return blocks or [Block(text="")]
    except ImportError:
        pass
    except Exception as exc:
        raise ParseError(f"xlsx 解析失败: {exc}") from exc

    from app.rag.office import OfficeParseError, extract_xlsx

    try:
        sheets = extract_xlsx(path)
    except OfficeParseError as exc:
        raise ParseError(str(exc)) from exc

    blocks = []
    for title, rows in sheets.items():
        lines = [" | ".join(cell for cell in row).strip(" |") for row in rows]
        content = "\n".join(line for line in lines if line)
        if content:
            blocks.append(
                Block(text=content, block_type="table", section_title=title, meta={"sheet": title})
            )
    return blocks or [Block(text="")]


def _parse_csv(path: Path) -> list[Block]:
    import csv

    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        rows = [" | ".join(row) for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        return [Block(text="")]
    header = rows[0]
    return [Block(text="\n".join(rows), block_type="table", section_title=header)]


def _parse_text(path: Path, markdown: bool = True) -> list[Block]:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    if not markdown:
        return [Block(text=_clean(raw))] if raw.strip() else [Block(text="")]

    blocks: list[Block] = []
    current_title: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            text = _clean("\n".join(buffer))
            if text:
                blocks.append(Block(text=text, block_type="paragraph", section_title=current_title))
            buffer.clear()

    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            flush()
            current_title = stripped.lstrip("#").strip()
            blocks.append(
                Block(text=current_title, block_type="heading", section_title=current_title)
            )
        elif not stripped:
            flush()
        else:
            buffer.append(line)
    flush()
    return blocks or [Block(text=_clean(raw))]


def _parse_json(path: Path) -> list[Block]:
    import json

    raw = path.read_text(encoding="utf-8", errors="ignore")
    try:
        data = json.loads(raw)
    except Exception as exc:
        raise ParseError(f"JSON 解析失败: {exc}") from exc

    def walk(node, title: str, out: list[Block]) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{title}.{key}" if title else str(key), out)
        elif isinstance(node, list):
            lines = [json.dumps(item, ensure_ascii=False) for item in node]
            out.append(
                Block(text="\n".join(lines), block_type="table", section_title=title or "items")
            )
        else:
            out.append(Block(text=f"{title}: {node}", block_type="paragraph", section_title=title))

    blocks: list[Block] = []
    walk(data, "", blocks)
    return blocks or [Block(text=raw[:5000])]

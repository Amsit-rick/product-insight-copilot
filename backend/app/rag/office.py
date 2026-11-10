"""Office 文件的零依赖解析后备实现。

设计动机（"零依赖本地可跑"）：openpyxl / python-docx 在生产镜像里会安装，
但在离线或最小化环境里不应因为缺一个库就完全无法入库。docx 与 xlsx 本质都是
zip + XML，这里用标准库做**只读文本抽取**，能力有限但足以支撑检索：

- docx：抽取 `word/document.xml` 的段落文本与标题样式；
- xlsx：抽取 `xl/worksheets/*.xml` 的单元格值（共享字符串表），按行输出。

两者都只做"取文本"，不做样式、公式、图表的完整还原；
如果装了 openpyxl / python-docx，解析器会优先使用它们。
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

HEADING_RE = re.compile(r"heading|标题", re.I)


class OfficeParseError(Exception):
    pass


# --------------------------------------------------------------------------- docx
def extract_docx(path: Path) -> list[tuple[str, str, str | None]]:
    """返回 [(文本, 块类型, 样式名)]，块类型为 heading / paragraph。"""
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml")
    except Exception as exc:
        raise OfficeParseError(f"docx 解析失败: {exc}") from exc

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise OfficeParseError(f"docx XML 解析失败: {exc}") from exc

    blocks: list[tuple[str, str, str | None]] = []
    for para in root.iter(f"{W}p"):
        style = None
        style_node = para.find(f"{W}pPr/{W}pStyle")
        if style_node is not None:
            style = style_node.get(f"{W}val")
        texts = [node.text or "" for node in para.iter(f"{W}t")]
        text = "".join(texts).strip()
        if not text:
            continue
        block_type = "heading" if style and HEADING_RE.search(style) else "paragraph"
        blocks.append((text, block_type, style))
    if not blocks:
        raise OfficeParseError("docx 未提取到任何文本")
    return blocks


# --------------------------------------------------------------------------- xlsx
def extract_xlsx(path: Path) -> dict[str, list[list[str]]]:
    """返回 {工作表名: [[单元格字符串, ...], ...]}。"""
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            shared = _shared_strings(zf) if "xl/sharedStrings.xml" in names else []
            sheet_titles = _sheet_titles(zf) if "xl/workbook.xml" in names else {}
            sheets: dict[str, list[list[str]]] = {}
            for name in sorted(n for n in names if n.startswith("xl/worksheets/") and n.endswith(".xml")):
                rows = _sheet_rows(zf.read(name), shared)
                if rows:
                    sheets[sheet_titles.get(name, name.split("/")[-1].replace(".xml", ""))] = rows
    except zipfile.BadZipFile as exc:
        raise OfficeParseError(f"xlsx 不是合法的 Office 文件: {exc}") from exc
    if not sheets:
        raise OfficeParseError("xlsx 未提取到任何工作表数据")
    return sheets


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    values: list[str] = []
    for si in root.iter(f"{S}si"):
        values.append("".join(node.text or "" for node in si.iter(f"{S}t")))
    return values


def _sheet_titles(zf: zipfile.ZipFile) -> dict[str, str]:
    root = ET.fromstring(zf.read("xl/workbook.xml"))
    rels_root = None
    if "xl/_rels/workbook.xml.rels" in zf.namelist():
        rels_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rel_map: dict[str, str] = {}
    if rels_root is not None:
        for rel in rels_root:
            rel_map[rel.get("Id")] = rel.get("Target", "")

    titles: dict[str, str] = {}
    r_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    for sheet in root.iter(f"{S}sheet"):
        rid = sheet.get(f"{r_ns}id")
        target = rel_map.get(rid or "", "")
        if target:
            normalized = target.lstrip("/")
            if not normalized.startswith("xl/"):
                normalized = "xl/" + normalized
            titles[normalized] = sheet.get("name") or normalized
    return titles


def _sheet_rows(xml_bytes: bytes, shared: list[str]) -> list[list[str]]:
    root = ET.fromstring(xml_bytes)
    rows: list[list[str]] = []
    for row in root.iter(f"{S}row"):
        cells: dict[int, str] = {}
        for cell in row.iter(f"{S}c"):
            ref = cell.get("r") or ""
            index = _column_index(ref)
            cell_type = cell.get("t")
            value = cell.find(f"{S}v")
            inline = cell.find(f"{S}is")
            if cell_type == "s" and value is not None and value.text is not None:
                try:
                    text = shared[int(value.text)]
                except (ValueError, IndexError):
                    text = ""
            elif cell_type == "inlineStr" and inline is not None:
                text = "".join(node.text or "" for node in inline.iter(f"{S}t"))
            else:
                text = value.text if value is not None and value.text else ""
            cells[index] = (text or "").strip()
        if cells:
            width = max(cells) + 1
            rows.append([cells.get(i, "") for i in range(width)])
    return [row for row in rows if any(cell for cell in row)]


def _column_index(ref: str) -> int:
    """把 A1 / BC12 形式的引用转成 0 基列号。"""
    letters = "".join(ch for ch in ref if ch.isalpha())
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch.upper()) - ord("A") + 1)
    return max(0, index - 1)

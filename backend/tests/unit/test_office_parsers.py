"""Office 零依赖解析后备实现（`app.rag.office`）单元测试。

用例中的数据文件都是**测试时现造**的最小合法 OOXML：
本机没有 python-docx / openpyxl，验证的正是标准库 zip + XML 这条后备链路。
"""

from __future__ import annotations

import builtins
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from app.rag.office import OfficeParseError, extract_docx, extract_xlsx
from app.rag.parsers import ParseError, parse_file

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _write_zip(path: Path, entries: dict[str, str | bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return path


def _block_import(monkeypatch, package: str) -> None:
    """只在当前用例阻断优先解析器，稳定覆盖标准库后备链路。"""
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == package or name.startswith(f"{package}."):
            raise ImportError(f"blocked by test: {package}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)


def _docx_bytes() -> bytes:
    """两段正文（标题样式 + 普通段落）与一个空段落。"""
    document = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W_NS}">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>版本 3.2.0 发布说明</w:t></w:r></w:p>
    <w:p><w:r><w:t>崩溃率</w:t></w:r><w:r><w:t>下降 12%</w:t></w:r></w:p>
    <w:p><w:r><w:t>  </w:t></w:r></w:p>
    <w:p><w:pPr><w:pStyle w:val="标题2"/></w:pPr>
      <w:r><w:t>附：留存口径</w:t></w:r></w:p>
  </w:body>
</w:document>
"""
    return document.encode("utf-8")


def _xlsx_bytes() -> bytes:
    """一个工作表：共享字符串表 + 共享串单元格 + 内联字符串 + 数值 + 稀疏行。"""
    workbook = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="{S_NS}" xmlns:r="{R_NS}">
  <sheets><sheet name="留存明细" sheetId="1" r:id="rId1"/></sheets>
</workbook>
"""
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Target="worksheets/sheet1.xml"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>
</Relationships>
"""
    shared = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="{S_NS}" count="5" uniqueCount="5">
  <si><t>指标名</t></si>
  <si><t>日期</t></si>
  <si><t>指标值</t></si>
  <si><t>retention_d1</t></si>
  <si><t>2024-03-02</t></si>
</sst>
"""
    sheet = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="{S_NS}">
  <sheetData>
    <row r="1">
      <c r="A1" t="s"><v>0</v></c>
      <c r="B1" t="s"><v>1</v></c>
      <c r="C1" t="s"><v>2</v></c>
    </row>
    <row r="2">
      <c r="A2" t="s"><v>3</v></c>
      <c r="B2" t="inlineStr"><is><t>2024-03-01</t></is></c>
      <c r="C2"><v>0.42</v></c>
    </row>
    <row r="3">
      <c r="A3" t="s"><v>3</v></c>
      <c r="B3" t="s"><v>4</v></c>
      <c r="C3"><v>0.35</v></c>
    </row>
    <row r="4">
      <c r="C4"><v>0.5</v></c>
    </row>
    <row r="5"><c r="A5"><v></v></c></row>
  </sheetData>
</worksheet>
"""
    return _build_xlsx_zip({"xl/workbook.xml": workbook, "xl/_rels/workbook.xml.rels": rels,
                            "xl/sharedStrings.xml": shared, "xl/worksheets/sheet1.xml": sheet})


def _build_xlsx_zip(entries: dict[str, str]) -> bytes:
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buffer.getvalue()


@pytest.fixture
def docx_path(tmp_path: Path) -> Path:
    return _write_zip(tmp_path / "release.docx", {"word/document.xml": _docx_bytes()})


@pytest.fixture
def xlsx_path(tmp_path: Path) -> Path:
    path = tmp_path / "retention.xlsx"
    path.write_bytes(_xlsx_bytes())
    return path


# --------------------------------------------------------------------------- docx
def test_extract_docx_提取段落与标题样式(docx_path: Path):
    blocks = extract_docx(docx_path)

    assert blocks == [
        ("版本 3.2.0 发布说明", "heading", "Heading1"),
        ("崩溃率下降 12%", "paragraph", None),
        ("附：留存口径", "heading", "标题2"),
    ]


def test_parse_file_docx_走标准库后备(docx_path: Path, monkeypatch):
    _block_import(monkeypatch, "docx")
    blocks = parse_file(docx_path)

    assert [b.block_type for b in blocks] == ["heading", "paragraph", "heading"]
    assert blocks[0].text == "版本 3.2.0 发布说明"
    assert blocks[0].section_title == "版本 3.2.0 发布说明"
    # 段落继承最近的标题作为 section_title，切片阶段才能按标题聚合
    assert blocks[1].text == "崩溃率下降 12%"
    assert blocks[1].section_title == "版本 3.2.0 发布说明"
    assert blocks[2].section_title == "附：留存口径"


def test_parse_file_docx在python_docx读取失败时走标准库后备(docx_path: Path, monkeypatch):
    import docx

    def fail_to_open(_path):
        raise KeyError("missing relationship target")

    monkeypatch.setattr(docx, "Document", fail_to_open)

    blocks = parse_file(docx_path)

    assert [block.text for block in blocks] == [
        "版本 3.2.0 发布说明",
        "崩溃率下降 12%",
        "附：留存口径",
    ]


def test_extract_docx_缺少部件时报错(tmp_path: Path):
    path = _write_zip(tmp_path / "empty.docx", {"docProps/core.xml": "<x/>"})

    with pytest.raises(OfficeParseError, match="docx 解析失败"):
        extract_docx(path)


def test_extract_docx_无文本时报错(tmp_path: Path):
    document = f'<?xml version="1.0"?><w:document xmlns:w="{W_NS}"><w:body><w:p/></w:body></w:document>'
    path = _write_zip(tmp_path / "blank.docx", {"word/document.xml": document})

    with pytest.raises(OfficeParseError, match="未提取到任何文本"):
        extract_docx(path)


# --------------------------------------------------------------------------- xlsx
def test_extract_xlsx_共享字符串与稀疏单元格(xlsx_path: Path):
    sheets = extract_xlsx(xlsx_path)

    assert list(sheets) == ["留存明细"]
    assert sheets["留存明细"] == [
        ["指标名", "日期", "指标值"],
        ["retention_d1", "2024-03-01", "0.42"],
        ["retention_d1", "2024-03-02", "0.35"],
        # 稀疏行：只写了 C4，前面的空列要补齐
        ["", "", "0.5"],
    ]


def test_extract_xlsx_工作表名来自workbook(xlsx_path: Path):
    """工作表标题取自 workbook.xml + rels，而不是文件名。"""
    assert "sheet1" not in extract_xlsx(xlsx_path)


def test_parse_file_xlsx_走标准库后备(xlsx_path: Path, monkeypatch):
    _block_import(monkeypatch, "openpyxl")
    blocks = parse_file(xlsx_path)

    assert len(blocks) == 1
    assert blocks[0].block_type == "table"
    assert blocks[0].section_title == "留存明细"
    assert blocks[0].meta == {"sheet": "留存明细"}
    # 行以 " | " 连接，且首尾空单元格被 strip 掉
    assert blocks[0].text.splitlines()[1] == "retention_d1 | 2024-03-01 | 0.42"
    assert blocks[0].text.splitlines()[3] == "0.5"


def test_extract_xlsx_非zip文件报错(tmp_path: Path):
    path = tmp_path / "broken.xlsx"
    path.write_bytes(b"this is not a zip archive")

    with pytest.raises(OfficeParseError, match="不是合法的 Office 文件"):
        extract_xlsx(path)


def test_extract_xlsx_无工作表数据报错(tmp_path: Path):
    workbook = f'<?xml version="1.0"?><workbook xmlns="{S_NS}"><sheets/></workbook>'
    path = _write_zip(tmp_path / "no_sheet.xlsx", {"xl/workbook.xml": workbook})

    with pytest.raises(OfficeParseError, match="未提取到任何工作表数据"):
        extract_xlsx(path)


# --------------------------------------------------------------------------- 统一入口
@pytest.mark.parametrize("name", ["broken.docx", "broken.xlsx"])
def test_parse_file_损坏压缩包抛ParseError(tmp_path: Path, name: str):
    path = tmp_path / name
    path.write_bytes(b"PK\x03\x04 broken zip payload")

    with pytest.raises(ParseError):
        parse_file(path)


def test_parse_file_不支持的类型():
    with pytest.raises(ParseError, match="不支持的文件类型"):
        parse_file(Path("whatever.exe"))


def test_parse_file_损坏docx的XML报错(tmp_path: Path, monkeypatch):
    """zip 合法但 document.xml 不是合法 XML：必须显式报错，不能静默返回空。"""
    _block_import(monkeypatch, "docx")
    path = _write_zip(tmp_path / "bad_xml.docx", {"word/document.xml": "<w:document>未闭合"})

    with pytest.raises(ParseError, match="XML 解析失败"):
        parse_file(path)


def test_xml命名空间常量与OOXML一致():
    """防止命名空间写错导致"能跑但永远解析不到内容"。"""
    root = ET.fromstring(_docx_bytes())
    assert root.tag == f"{{{W_NS}}}document"

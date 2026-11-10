"""上传安全检查（设计文档 11 节）。

检查项：扩展名白名单、大小上限、文件头魔数、Office 宏、文档内恶意指令。
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from app.config import get_settings
from app.services.policy_engine import get_policy_engine

# 常见文件头魔数
MAGIC: dict[str, list[bytes]] = {
    ".pdf": [b"%PDF"],
    ".docx": [b"PK\x03\x04"],
    ".xlsx": [b"PK\x03\x04"],
    ".csv": [],
    ".md": [],
    ".txt": [],
    ".json": [],
}

MACRO_NAMES = {"word/vbaProject.bin", "xl/vbaProject.bin", "ppt/vbaProject.bin"}


@dataclass
class UploadCheckResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class UploadSecurityError(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def check_upload(filename: str, data: bytes) -> UploadCheckResult:
    settings = get_settings()
    errors: list[str] = []
    warnings: list[str] = []

    suffix = Path(filename).suffix.lower()
    if suffix not in settings.allowed_extensions:
        errors.append(f"不支持的文件类型: {suffix}")

    max_bytes = settings.max_upload_mb * 1024 * 1024
    if len(data) > max_bytes:
        errors.append(f"文件超过大小上限 {settings.max_upload_mb}MB")

    if not data:
        errors.append("文件内容为空")

    expected_magics = MAGIC.get(suffix, [])
    if expected_magics and not any(data.startswith(m) for m in expected_magics):
        errors.append("文件头与扩展名不匹配，疑似伪装文件")

    if suffix in {".docx", ".xlsx"}:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = set(zf.namelist())
                macros = names & MACRO_NAMES
                if macros:
                    errors.append("文档包含宏（vbaProject.bin），已拒绝")
                if any(n.startswith("..") or n.startswith("/") for n in names):
                    errors.append("文档包含可疑路径条目")
        except Exception:
            errors.append("Office 文档解析失败，可能已损坏")

    text_preview = _extract_text_preview(suffix, data)
    hit = get_policy_engine().detect_injection(text_preview)
    if hit:
        warnings.append(f"文档内容包含可疑指令片段: {hit}")

    return UploadCheckResult(ok=not errors, errors=errors, warnings=warnings)


def _extract_text_preview(suffix: str, data: bytes, limit: int = 20000) -> str:
    if suffix in {".md", ".txt", ".csv", ".json"}:
        return data[:limit].decode("utf-8", errors="ignore")
    if suffix == ".pdf":
        return data[:limit].decode("latin-1", errors="ignore")
    if suffix in {".docx", ".xlsx"}:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                chunks = []
                for name in zf.namelist():
                    if name.endswith(".xml"):
                        chunks.append(zf.read(name)[:5000].decode("utf-8", errors="ignore"))
                return "\n".join(chunks)[:limit]
        except Exception:
            return ""
    return ""


def assert_upload_safe(filename: str, data: bytes) -> UploadCheckResult:
    result = check_upload(filename, data)
    if not result.ok:
        raise UploadSecurityError(result.errors)
    return result

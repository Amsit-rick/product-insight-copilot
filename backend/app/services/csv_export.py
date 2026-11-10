"""Helpers for producing spreadsheet-safe CSV exports."""

from __future__ import annotations

import re


_FORMULA_PREFIX = re.compile(r"^[\t\r\n \x00-\x1f\x7f\ufeff]*[=+\-@]")


def spreadsheet_safe_text(value: object) -> str:
    """Prefix formula-like cells so spreadsheet programs treat them as text."""
    text = "" if value is None else str(value)
    return f"'{text}" if _FORMULA_PREFIX.match(text) else text

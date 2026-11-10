import pytest

from app.services.csv_export import spreadsheet_safe_text


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("=1+1", "'=1+1"),
        ("+SUM(A1:A2)", "'+SUM(A1:A2)"),
        ("-2+3", "'-2+3"),
        ("@SUM(A1:A2)", "'@SUM(A1:A2)"),
        (" \t=HYPERLINK(\"https://example.invalid\")", "' \t=HYPERLINK(\"https://example.invalid\")"),
        ("\r\n@SUM(A1)", "'\r\n@SUM(A1)"),
        ("ordinary text", "ordinary text"),
        ("", ""),
        (None, ""),
        (42, "42"),
    ],
)
def test_spreadsheet_safe_text(value, expected):
    assert spreadsheet_safe_text(value) == expected

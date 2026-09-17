"""Tests for OneDrive/SharePoint name sanitization."""
from __future__ import annotations

from migrator.transform.paths import sanitize_path, sanitize_segment


def test_illegal_chars_replaced() -> None:
    assert sanitize_segment('a<b>c:d"e?f*g') == "a_b_c_d_e_f_g"


def test_trailing_dots_and_spaces_stripped() -> None:
    assert sanitize_segment(" report. ") == "report"


def test_reserved_names_escaped_with_and_without_extension() -> None:
    assert sanitize_segment("CON") == "CON_"          # extensionless (was bypassed)
    assert sanitize_segment("NUL.txt") == "NUL_.txt"
    assert sanitize_segment("con") == "con_"          # case-insensitive
    assert sanitize_segment("COM0") == "COM0_"        # SharePoint rejects 0 too
    assert sanitize_segment("LPT0.log") == "LPT0_.log"
    assert sanitize_segment("CONTRACT") == "CONTRACT"  # only exact stems match


def test_forbidden_names() -> None:
    assert sanitize_segment("desktop.ini") == "desktop_.ini"
    assert sanitize_segment("Desktop.INI") == "Desktop_.INI"
    assert sanitize_segment("a_vti_b") == "a_vti-b"


def test_empty_becomes_unnamed() -> None:
    assert sanitize_segment("...") == "_unnamed"


def test_long_name_truncation_preserves_extension() -> None:
    got = sanitize_segment("x" * 300 + ".pdf")
    assert len(got) <= 128
    assert got.endswith(".pdf")


def test_sanitize_path_joins_and_truncates() -> None:
    assert sanitize_path("a\\b/c") == "a/b/c"
    long = "/".join(["seg" + "x" * 60] * 12) + "/file.txt"
    got = sanitize_path(long)
    assert len(got) <= 400
    assert got.endswith("/file.txt")

"""The bytes of a page become text, and the page tells its language. No network."""

# This file reads Persian text on purpose. Its letters are no Latin look-alikes.
# ruff: noqa: RUF001

import codecs

import pytest

from unshelve.extract.html import decode_html, language_of

# With the Arabic yeh of old Persian pages, and a zero-width non-joiner after "نزديک".
PERSIAN = "نزديک‌ترين همسايه‌ها"


def _page(meta: str = "", body: str = PERSIAN) -> str:
    return f"<html><head>{meta}<title>t</title></head><body><p>{body}</p></body></html>"


def test_utf8_without_a_declaration_is_read_as_utf8() -> None:
    assert PERSIAN in decode_html(_page().encode("utf-8"), None)


def test_the_charset_of_the_header_is_used() -> None:
    page = _page().encode("cp1256")

    assert PERSIAN in decode_html(page, "text/html; charset=windows-1256")


@pytest.mark.parametrize(
    "meta",
    [
        '<meta charset="windows-1256">',
        "<meta charset=windows-1256>",
        '<meta http-equiv="Content-Type" content="text/html; charset=windows-1256">',
        "<META HTTP-EQUIV='content-type' CONTENT='text/html;charset=Windows-1256'>",
    ],
)
def test_the_charset_of_a_meta_tag_is_used_when_the_header_has_none(meta: str) -> None:
    assert PERSIAN in decode_html(_page(meta).encode("cp1256"), "text/html")


def test_the_header_wins_over_a_meta_tag() -> None:
    # A page that moved to a new server often keeps the meta tag of the old one.
    page = _page('<meta charset="windows-1256">').encode("utf-8")

    assert PERSIAN in decode_html(page, "text/html; charset=utf-8")


def test_a_byte_order_mark_wins_over_each_declaration() -> None:
    page = codecs.BOM_UTF8 + _page('<meta charset="windows-1256">').encode("utf-8")

    assert PERSIAN in decode_html(page, "text/html; charset=windows-1256")


def test_a_meta_tag_that_names_utf16_means_utf8() -> None:
    # The tag was found by reading the page as ASCII, so the page cannot be UTF-16.
    page = _page('<meta charset="utf-16">').encode("utf-8")

    assert PERSIAN in decode_html(page, "text/html")


def test_latin1_is_read_as_windows_1252_as_browsers_do() -> None:
    # In windows-1252 the bytes 0x93 and 0x94 are quotation marks. In latin-1 they are
    # control characters.
    page = "<p>“quoted” café</p>".encode("cp1252")

    assert "“quoted” café" in decode_html(page, "text/html; charset=iso-8859-1")


def test_an_unknown_charset_name_is_left_out() -> None:
    page = _page().encode("utf-8")

    assert PERSIAN in decode_html(page, "text/html; charset=no-such-charset")


@pytest.mark.parametrize("label", ["undefined", "unicode-escape", "base64", "utf-7", "idna"])
def test_a_page_cannot_choose_a_codec_that_is_no_page_encoding(label: str) -> None:
    # "undefined" always fails, "unicode-escape" reads "\\u0041" as "A", "base64" gives no
    # text. Such a name is left out, and the next rule reads the page.
    page = (_page() + "\\u0041").encode("utf-8")

    text = decode_html(page, f"text/html; charset={label}")

    assert PERSIAN in text
    assert text.endswith("</html>\\u0041")


def test_a_page_that_is_not_utf8_and_names_no_charset_is_guessed() -> None:
    text = "اين يک متن فارسي است که نام رمزگذاري خود را در هيچ جا نمي‌گويد. " * 20
    page = f"<html><body><p>{text}</p></body></html>".encode("cp1256")

    assert "يک متن فارسي" in decode_html(page, "text/html")


def test_bytes_that_the_charset_cannot_read_do_not_stop_the_reading() -> None:
    text = decode_html(b"<p>\xff\xfe\xfd broken</p>", "text/html; charset=utf-8")

    assert text == "<p>��� broken</p>"


@pytest.mark.parametrize(
    ("start", "language"),
    [
        ('<html lang="fa">', "fa"),
        ('<html lang="fa-IR" dir="rtl">', "fa"),
        ("<html lang=EN-us>", "en"),
        ('<html xml:lang="de" lang="de">', "de"),
        ('<!doctype html>\n<HTML LANG="ar">', "ar"),
        ("<html>", None),
        ('<html lang="">', None),
        ('<html lang="x-klingon">', None),
        ('<html data-lang="fr">', None),
        ('<html lang="english">', None),  # no language code
    ],
)
def test_the_language_comes_from_the_html_element(start: str, language: str | None) -> None:
    assert language_of(f"{start}<head></head><body><p lang='es'>hola</p></body></html>") == (
        language
    )

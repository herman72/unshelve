"""The bytes of a web page become text, and the page tells its language. Pure: no network.

The rules come in the order of the HTML standard: a byte order mark first, then the charset
of the HTTP header, then a meta tag near the start of the page, then UTF-8, and a guess at
the end.

Why it matters: an old Persian page in windows-1256, read with a wrong encoding, becomes
letters of a different alphabet. trafilatura guesses the encoding when it gets bytes, and
its guess can be wrong. So it gets text from here.
"""

import codecs
import re

import charset_normalizer

_BYTE_ORDER_MARKS = (
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)
_HEADER_CHARSET = re.compile(r"charset\s*=\s*[\"']?([\w.:-]+)", re.IGNORECASE)
# <meta charset="x">, and <meta http-equiv="Content-Type" content="text/html; charset=x">
_META_CHARSET = re.compile(rb"<meta[^>]*?charset\s*=\s*[\"']?\s*([\w.:-]+)", re.IGNORECASE)
_START = 4096  # the meta tag and the language are near the start of a page
# Browsers read these labels as windows-1252, which has the same letters and more.
_WINDOWS_1252_LABELS = frozenset({"ascii", "iso8859-1"})
# Python knows these codecs, but they are no encodings of a web page. "undefined" always
# fails, and "unicode-escape" reads a backslash as a command.
_NOT_FOR_PAGES = frozenset(
    {"undefined", "unicode-escape", "raw-unicode-escape", "idna", "punycode", "utf-7"}
)
_LANGUAGE = re.compile(r"<html\b[^>]*?\slang\s*=\s*[\"']?([a-z]{2,3})(?![a-z])", re.IGNORECASE)


def decode_html(body: bytes, content_type: str | None) -> str:
    """The text of a page. Bytes that the encoding cannot read become U+FFFD: nothing fails."""
    for mark, encoding in _BYTE_ORDER_MARKS:
        if body.startswith(mark):
            return body[len(mark) :].decode(encoding, errors="replace")
    header = _HEADER_CHARSET.search(content_type or "")
    meta = _META_CHARSET.search(body[:_START])
    for label in (header.group(1) if header else None, _meta_label(meta)):
        text = _decoded(body, label)
        if text is not None:
            return text
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        guess = charset_normalizer.from_bytes(body[:65_536]).best()
        text = _decoded(body, guess.encoding if guess is not None else None)
        return text if text is not None else body.decode("cp1252", errors="replace")


def language_of(html: str) -> str | None:
    """The language that the page names in ``<html lang="...">``, such as "fa" for "fa-IR"."""
    found = _LANGUAGE.search(html[:_START])
    return found.group(1).lower() if found else None


def _meta_label(meta: re.Match[bytes] | None) -> str | None:
    if meta is None:
        return None
    label = meta.group(1).decode("ascii", errors="replace")
    # The page was read as ASCII to find the tag, so it cannot be UTF-16 (HTML standard).
    return "utf-8" if label.lower().startswith("utf-16") else label


def _decoded(body: bytes, label: str | None) -> str | None:
    """``body`` in the encoding ``label``, or None when the label is no encoding of a page."""
    if not label:
        return None
    try:
        name = codecs.lookup(label).name
        if name in _NOT_FOR_PAGES:
            return None
        if name in _WINDOWS_1252_LABELS:
            name = "cp1252"
        return body.decode(name, errors="replace")
    except LookupError:
        # An unknown label, or a codec such as "base64" that does not give text. With
        # errors="replace", no other codec of Python fails: a test of each one showed it.
        return None

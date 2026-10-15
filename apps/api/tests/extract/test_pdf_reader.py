"""The reader of PDF files, which runs in a child process. Here most tests call it directly;
one test starts it as the worker does. The fixtures are made-up texts, printed by Chrome."""

import json
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Any

import pytest
from pypdf import PageObject

from unshelve.extract.pdf_reader import CPU_SECONDS, MEMORY_BYTES, read

FIXTURES = Path(__file__).parent / "fixtures" / "pdf"


def _read(name: str, *, max_pages: int = 300, max_chars: int = 300_000) -> dict[str, Any]:
    return read((FIXTURES / name).read_bytes(), max_pages=max_pages, max_chars=max_chars)


def _one_page(content: bytes, filters: bytes = b"") -> bytes:
    """A PDF of one page with this content stream, and a correct table of its objects."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d%s >>\nstream\n%s\nendstream" % (len(content), filters, content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.7\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    table = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        table,
    )
    return bytes(out)


# --- Text and facts ----------------------------------------------------------------------


def test_a_paper_in_two_columns_gives_its_pages_in_the_order_of_reading() -> None:
    found = _read("paper.pdf")

    assert (found["pages"], found["read"], len(found["texts"])) == (3, 3, 3)
    text = "\n".join(found["texts"])
    assert text.startswith("Leases for Job Queues in Postgres\nA. Made-up and B. Example")
    sections = ["Abstract", "1 Introduction", "2 Leases", "4 Results", "6 Conclusion", "References"]
    places = [text.index(section) for section in sections]
    assert places == sorted(places)  # the left column first, then the right one


def test_the_facts_are_kept_as_the_file_states_them() -> None:
    paper = _read("paper.pdf")
    slides = _read("slides.pdf")

    assert (paper["title"], paper["author"]) == ("Leases for Job Queues in Postgres", None)
    assert paper["created"] == "D:20261015191205+00'00'"
    # The worker decides which of them it trusts.
    assert slides["title"] == "Microsoft PowerPoint - queues-talk.pptx"
    assert slides["author"] == "Made-up Speaker"


def test_the_links_of_the_annotations_are_kept() -> None:
    # In the text the link is "our data page"; only the annotation has the address.
    assert _read("paper.pdf")["links"] == ["https://example.org/queue-data"]


def test_a_scan_has_a_page_without_text() -> None:
    found = _read("scanned.pdf")

    assert (found["pages"], found["texts"]) == (1, [""])


# --- Limits ------------------------------------------------------------------------------


def test_reading_stops_after_the_pages_that_are_allowed() -> None:
    found = _read("paper.pdf", max_pages=1)

    assert (found["pages"], found["read"]) == (3, 1)


def test_reading_stops_after_the_characters_that_are_allowed() -> None:
    found = _read("paper.pdf", max_chars=100)

    assert (found["pages"], found["read"]) == (3, 1)
    assert len(found["texts"][0]) == 100


def _text_then_spaces(megabytes: int) -> bytes:
    """A deflated content stream: one line of text, then many megabytes of spaces."""
    packer = zlib.compressobj(9)
    parts = [packer.compress(b"BT /F1 12 Tf 20 100 Td (A made-up line) Tj ET\n")]
    parts += [packer.compress(b" " * 1_000_000) for _ in range(megabytes)]
    return b"".join(parts) + packer.flush()


def test_a_page_with_a_deflated_stream_of_a_normal_size_is_read() -> None:
    page = _one_page(_text_then_spaces(1), b" /Filter /FlateDecode")

    assert read(page, max_pages=300, max_chars=300_000)["texts"] == ["A made-up line"]


def test_a_page_that_expands_past_the_limit_of_pypdf_gives_no_text() -> None:
    # 100 MB of spaces are 100 KB when deflated: a small file that fills the memory. pypdf
    # stops at 75 MB, and the page gives no text.
    bomb = _text_then_spaces(100)
    assert len(bomb) < 200_000

    found = read(_one_page(bomb, b" /Filter /FlateDecode"), max_pages=300, max_chars=300_000)

    assert (found["pages"], found["texts"]) == (1, [""])


# --- Files that cannot be read -----------------------------------------------------------


def test_a_file_with_a_password_gives_an_error_of_its_own() -> None:
    assert _read("locked.pdf") == {"error": "password"}


def test_a_file_that_limits_only_printing_is_read_without_a_password() -> None:
    found = _read("owner_password.pdf")

    assert found["texts"][0].startswith("Queues in practice")


@pytest.mark.parametrize(
    "data",
    [
        (FIXTURES / "cut.pdf").read_bytes(),
        b"<html><body>A page, not a PDF.</body></html>",
        b"",
    ],
    ids=["cut", "html", "empty"],
)
def test_a_file_that_is_no_whole_pdf_is_broken(data: bytes) -> None:
    found = read(data, max_pages=300, max_chars=300_000)

    assert found["error"] == "broken"
    assert found["detail"]


def test_a_page_that_cannot_be_read_gives_no_text_and_the_next_pages_are_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = PageObject.extract_text
    calls: list[int] = []

    def broken_second_page(self: PageObject, *args: Any, **kwargs: Any) -> str:
        calls.append(1)
        if len(calls) == 2:
            raise ValueError("a made-up broken page")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(PageObject, "extract_text", broken_second_page)

    found = _read("paper.pdf")

    assert [bool(text) for text in found["texts"]] == [True, False, True]


def test_a_half_character_becomes_a_question_mark(monkeypatch: pytest.MonkeyPatch) -> None:
    # JSON cannot hold half of a character, and the worker would refuse the whole answer.
    def half_character(*_: object, **__: object) -> str:
        return "a" + chr(0xD800) + "b"

    monkeypatch.setattr(PageObject, "extract_text", half_character)

    assert _read("scanned.pdf")["texts"] == ["a?b"]


# --- As a child process ------------------------------------------------------------------


def test_the_child_process_lowers_its_own_limits() -> None:
    # The limits are those of the process that runs main(), so a second process checks them.
    code = (
        "import io, resource, sys\n"
        "from unshelve.extract import pdf_reader\n"
        "sys.argv = ['pdf_reader', '1', '100']\n"
        "sys.stdin = io.TextIOWrapper(io.BytesIO(b''))\n"
        "pdf_reader.main()\n"
        "cpu, memory = resource.RLIMIT_CPU, resource.RLIMIT_AS\n"
        "print(resource.getrlimit(cpu)[0], resource.getrlimit(memory)[0], file=sys.stderr)\n"
    )
    done = subprocess.run(  # noqa: S603 - the code is a constant of this test
        [sys.executable, "-I", "-c", code], capture_output=True, check=True, timeout=60
    )

    cpu, memory = (int(value) for value in done.stderr.split())
    assert cpu == CPU_SECONDS
    if sys.platform == "linux":  # macOS does not keep a limit of memory
        assert memory == MEMORY_BYTES


def test_the_module_answers_in_json_as_a_child_process() -> None:
    # The same command as the worker uses: isolated (-I), with the limits as arguments.
    done = subprocess.run(
        [sys.executable, "-I", "-m", "unshelve.extract.pdf_reader", "2", "300000"],
        input=(FIXTURES / "paper.pdf").read_bytes(),
        capture_output=True,
        check=True,
        timeout=60,
    )

    found = json.loads(done.stdout)
    assert (found["pages"], found["read"]) == (3, 2)
    assert found["title"] == "Leases for Job Queues in Postgres"

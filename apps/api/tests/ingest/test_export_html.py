"""The reader of an HTML export. Each test name is one rule. No database is used here."""

from datetime import datetime
from pathlib import Path

import pytest

from tests.ingest.support import DESKTOP, MACOS, by_id, made_up
from unshelve.ingest import export_html
from unshelve.ingest.export_html import (
    ExportedFile,
    ExportFormatError,
    export_pages,
    parse_clock_time,
    read_export,
)

# --- The two real exports -----------------------------------------------------------------


def test_a_real_export_gives_its_five_posts() -> None:
    chat = read_export(DESKTOP)

    assert chat.title == "Unshelve test"
    assert [message.message_id for message in chat.messages] == [2, 3, 4, 5, 6]
    link = by_id(chat)[2]
    assert link.sent_at == datetime(2026, 10, 13, 22, 40, 37)  # noqa: DTZ001 - a clock time
    assert link.text == "https://example.com/blog/scaling-storage/"
    assert link.urls == ("https://example.com/blog/scaling-storage/",)
    assert link.files == ()
    # A line with a date and "channel created" are in the page. Only the second is a message.
    assert chat.left_out == {export_html.SERVICE_MESSAGE: 1}


def test_a_forwarded_post_gives_the_name_and_the_first_date_of_its_source() -> None:
    forwarded = by_id(read_export(DESKTOP))[4]

    assert forwarded.forwarded_from == "Systems Notes | Queues & Agents 🚀"
    assert forwarded.forwarded_at == datetime(2026, 6, 20, 15, 56, 2)  # noqa: DTZ001
    # The post ends with a mention. A mention is a link in the page, and not a link of the post.
    assert forwarded.text.endswith("\n\n@systemsnotes")
    assert forwarded.urls == ("https://video.example.com/watch?v=x9y8z7w6v5u",)


def test_the_line_breaks_of_a_post_stay_and_the_line_breaks_of_the_page_go() -> None:
    note = by_id(read_export(DESKTOP))[5]

    assert note.text == (
        "یادداشت کوتاه دربارهٔ یادگیری.\n"
        "\n"
        "۱- توضیح ساده بخواهید.\n"  # noqa: RUF001 - Persian text
        '"Explain it in simple words"\n'
        "\n"
        "۲- تصویر بخواهید."
    )


def test_a_document_gives_its_path_in_the_export_and_its_name() -> None:
    document = by_id(read_export(DESKTOP))[6]

    assert document.text == ""
    assert document.files == (
        ExportedFile(
            kind="document",
            path="files/a-guide-to-job-queues.pdf",
            name="a-guide-to-job-queues.pdf",
        ),
    )


def test_the_two_apps_give_the_same_posts() -> None:
    desktop, macos = read_export(DESKTOP), read_export(MACOS)

    def seen(chat: export_html.ExportedChat) -> list[tuple[object, ...]]:
        return [
            (
                message.message_id,
                message.sent_at,
                message.text,
                message.urls,
                message.forwarded_from,
                [(file.kind, file.name) for file in message.files],
            )
            for message in chat.messages
        ]

    assert macos.title == desktop.title
    assert seen(macos) == seen(desktop)
    # Telegram for macOS shows a preview under a link. A preview is not content of the post.
    assert macos.left_out == {export_html.SERVICE_MESSAGE: 1}
    # The two apps give a file two different paths, and only one app gives the first date.
    assert by_id(macos)[6].files[0].path == "files/6_a-guide-to-job-queues.pdf"
    assert by_id(macos)[4].forwarded_at is None


# --- The made-up export: kinds that the code of Telegram Desktop writes --------------------


def test_the_text_of_a_post_has_no_marks_of_the_page() -> None:
    post = by_id(made_up())[12]

    assert post.text == (
        "Read this first: the guide\n"
        "uv run pytest\n"
        "make up\n"
        "Then claim() & #queues, ask @someone or me@example.com.\n"
        "The guide again: https://example.com/guide?a=1&b=2"
    )


def test_only_web_links_count_and_each_counts_one_time() -> None:
    post = by_id(made_up())[12]

    # The post has the same link two times, a hashtag, a mention and a mail address.
    assert post.urls == ("https://example.com/guide?a=1&b=2",)


def test_a_link_with_a_text_that_starts_with_an_at_sign_is_a_link() -> None:
    assert not export_html._is_mention("https://example.com/people", "@team page")  # pyright: ignore[reportPrivateUsage]
    assert export_html._is_mention("https://t.me/someone", "@someone")  # pyright: ignore[reportPrivateUsage]


def test_spaces_at_the_start_of_a_line_stay() -> None:
    note = by_id(made_up())[26]

    expected = "یادداشت: صف‌ها را بخوان.\n\n  خط دوم با دو فاصله شروع می‌شود."  # noqa: RUF001
    assert note.text == expected


@pytest.mark.parametrize(
    ("message_id", "expected"),
    [
        (
            7,
            ExportedFile(
                "photo", "photos/photo_1@13-10-2026_23-25-17.jpg", "photo_1@13-10-2026_23-25-17.jpg"
            ),
        ),
        (9, ExportedFile("photo", None, None)),  # the export did not include the file
        (13, ExportedFile("video", "video_files/talk.mp4", "talk.mp4")),
        (14, ExportedFile("video", None, None)),
        (16, ExportedFile("sticker", None, None)),  # the box of a photo, with the title "Sticker"
        (
            20,
            ExportedFile(
                "voice",
                "voice_messages/audio_1@13-10-2026_23-37-00.ogg",
                "audio_1@13-10-2026_23-37-00.ogg",
            ),
        ),
        # The title of an audio file is the name of the song, so the name comes from the path.
        (21, ExportedFile("audio", "files/a-podcast-episode.mp3", "a-podcast-episode.mp3")),
        (23, ExportedFile("document", "files/part-one.pdf", "part-one.pdf")),
        (24, ExportedFile("document", None, "part-two.pdf")),  # not included: the name stays
    ],
)
def test_each_kind_of_file_is_read(message_id: int, expected: ExportedFile) -> None:
    assert by_id(made_up())[message_id].files == (expected,)


def test_a_file_can_have_a_caption() -> None:
    video = by_id(made_up())[13]

    assert video.text == "A talk about queues"
    assert len(video.files) == 1


def test_a_post_that_cannot_be_read_is_counted_and_left_out() -> None:
    chat = made_up()

    assert chat.left_out == {
        "poll": 1,  # message 15
        export_html.RICH_MESSAGE: 1,  # message 19
        export_html.SERVICE_MESSAGE: 1,  # message 22: "pinned this message"
        "location": 1,  # message 25
    }
    assert not {15, 19, 22, 25} & set(by_id(chat))


def test_posts_of_one_person_that_were_forwarded_together_have_the_same_source() -> None:
    posts = by_id(made_up())

    # Telegram Desktop writes the name of the person on the first post only.
    assert posts[17].forwarded_from == "Sara M"
    assert posts[17].forwarded_at == datetime(2026, 10, 12, 9, 1, 0)  # noqa: DTZ001
    assert posts[18].forwarded_from == "Sara M"
    assert posts[18].forwarded_at is None
    assert posts[18].urls == ("https://example.com/second",)


def test_the_posts_of_all_pages_come_back_in_the_order_of_their_ids() -> None:
    ids = [message.message_id for message in made_up().messages]

    assert ids == [7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 18, 20, 21, 23, 24, 26]


# --- Pages ---------------------------------------------------------------------------------


def test_the_pages_are_read_in_the_order_of_their_numbers(tmp_path: Path) -> None:
    for name in (
        "messages10.html",
        "messages2.html",
        "messages.html",
        "notes.html",
        "messages.txt",
    ):
        (tmp_path / name).write_text("")

    names = [page.name for page in export_pages(tmp_path)]

    # A sort by name puts "messages10" before "messages2".
    assert names == ["messages.html", "messages2.html", "messages10.html"]


def test_a_folder_without_a_page_is_not_an_export(tmp_path: Path) -> None:
    (tmp_path / "result.json").write_text("{}")

    with pytest.raises(ExportFormatError, match=r"has no file messages\.html"):
        read_export(tmp_path)


# --- Clock times ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("13.10.2026 22:40:37 UTC+02:00", datetime(2026, 10, 13, 22, 40, 37)),  # noqa: DTZ001
        # The app wrote a wrong zone (docs/adr/0009). The reader does not use this part.
        ("13.10.2026 22:40:37 UTC+03:00", datetime(2026, 10, 13, 22, 40, 37)),  # noqa: DTZ001
        ("13.10.2026 22:40:37", datetime(2026, 10, 13, 22, 40, 37)),  # noqa: DTZ001
        ("13 October 2026, 22:40:37", datetime(2026, 10, 13, 22, 40, 37)),  # noqa: DTZ001
        ("1 January 2027, 00:00:05", datetime(2027, 1, 1, 0, 0, 5)),  # noqa: DTZ001
        ("  03 may 2025, 07:08:09 ", datetime(2025, 5, 3, 7, 8, 9)),  # noqa: DTZ001
    ],
)
def test_a_clock_time_is_read_in_the_form_of_each_app(text: str, expected: datetime) -> None:
    assert parse_clock_time(text) == expected
    assert parse_clock_time(text).tzinfo is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "yesterday",
        "2026-10-13 22:40:37",
        "13 Oktober 2026, 22:40:37",  # the name of a month in a language that is not English
        "13/10/2026 22:40:37",
        "13.10.2026 22:40",
    ],
)
def test_a_date_in_a_form_that_is_not_known_is_an_error(text: str) -> None:
    with pytest.raises(ExportFormatError, match="a date in a form that the importer does not know"):
        parse_clock_time(text)


def test_a_date_that_does_not_exist_is_an_error() -> None:
    with pytest.raises(ExportFormatError, match="a date in a form that the importer does not know"):
        parse_clock_time("31.02.2026 10:00:00")


# --- Pages that the reader does not know ---------------------------------------------------

DATE = '<div class="pull_right date details" title="13.10.2026 22:40:37 UTC+02:00">22:40</div>'


def _export(tmp_path: Path, history: str, *, header: str | None = None) -> Path:
    """An export of one page, with these elements in its history."""
    if header is None:
        header = (
            '<div class="page_header"><div class="content">'
            '<div class="text bold">A chat</div></div></div>'
        )
    (tmp_path / "messages.html").write_text(
        f'<html><body><div class="page_wrap">{header}'
        f'<div class="page_body chat_page"><div class="history">{history}</div></div>'
        "</div></body></html>",
        encoding="utf-8",
    )
    return tmp_path


def _post(message_id: int, body: str) -> str:
    return (
        f'<div class="message default clearfix" id="message{message_id}">'
        f'<div class="body">{body}</div></div>'
    )


def test_a_post_without_an_id_stops_the_import(tmp_path: Path) -> None:
    page = (
        '<div class="message default clearfix"><div class="body">'
        '<div class="text">x</div></div></div>'
    )

    with pytest.raises(ExportFormatError, match=r"messages\.html: a message without a message id"):
        read_export(_export(tmp_path, page))


def test_a_post_without_a_date_stops_the_import(tmp_path: Path) -> None:
    with pytest.raises(ExportFormatError, match=r"messages\.html: message 5 has no date"):
        read_export(_export(tmp_path, _post(5, '<div class="text">x</div>')))


def test_a_post_with_a_date_that_is_not_known_stops_the_import(tmp_path: Path) -> None:
    body = (
        '<div class="pull_right date details" title="last week">22:40</div>'
        '<div class="text">x</div>'
    )

    with pytest.raises(ExportFormatError, match="message 5 has a date in a form that the importer"):
        read_export(_export(tmp_path, _post(5, body)))


def test_a_message_of_a_kind_that_is_not_known_stops_the_import(tmp_path: Path) -> None:
    page = '<div class="message story clearfix" id="message5"><div class="body">x</div></div>'

    with pytest.raises(
        ExportFormatError, match="a message of a kind that is not known: clearfix message story"
    ):
        read_export(_export(tmp_path, page))


def test_a_page_without_the_title_of_the_chat_stops_the_import(tmp_path: Path) -> None:
    with pytest.raises(ExportFormatError, match="the page has no chat title"):
        read_export(_export(tmp_path, _post(5, DATE + '<div class="text">x</div>'), header=""))


NAMELESS_FORWARD = DATE + '<div class="forwarded body"><div class="text">again</div></div>'
NAMED_FORWARD = DATE + (
    '<div class="forwarded body"><div class="from_name">Sara M '
    '<span class="date details" title="12.10.2026 09:01:00 UTC+02:00"> 12.10.2026 09:01:00</span>'
    '</div><div class="text">first</div></div>'
)


def test_a_forwarded_post_without_a_source_stops_the_import(tmp_path: Path) -> None:
    with pytest.raises(
        ExportFormatError, match="message 5 has a forwarded post without the name of its source"
    ):
        read_export(_export(tmp_path, _post(5, NAMELESS_FORWARD)))


def test_the_source_of_the_post_before_is_not_used_after_a_service_message(tmp_path: Path) -> None:
    pinned = (
        '<div class="message service" id="message6"><div class="body details">pinned</div></div>'
    )

    with pytest.raises(ExportFormatError, match="message 7 has a forwarded post without the name"):
        read_export(
            _export(tmp_path, _post(5, NAMED_FORWARD) + pinned + _post(7, NAMELESS_FORWARD))
        )


def test_the_source_of_the_post_before_is_not_used_after_a_post_that_was_not_forwarded(
    tmp_path: Path,
) -> None:
    own = _post(6, DATE + '<div class="text">my own note</div>')

    with pytest.raises(ExportFormatError, match="message 7 has a forwarded post without the name"):
        read_export(_export(tmp_path, _post(5, NAMED_FORWARD) + own + _post(7, NAMELESS_FORWARD)))


def test_the_source_of_the_post_before_is_not_used_on_the_next_page(tmp_path: Path) -> None:
    folder = _export(tmp_path, _post(5, NAMED_FORWARD))
    first_page = (folder / "messages.html").read_text(encoding="utf-8")
    (folder / "messages2.html").write_text(
        first_page.replace(_post(5, NAMED_FORWARD), _post(6, NAMELESS_FORWARD)), encoding="utf-8"
    )

    with pytest.raises(ExportFormatError, match=r"messages2\.html: message 6 has a forwarded post"):
        read_export(folder)


def test_a_post_that_was_left_out_still_gives_its_source_to_the_next_post(tmp_path: Path) -> None:
    # The first post of the person is a poll. A poll is left out, and the next post is read.
    poll = DATE + (
        '<div class="forwarded body"><div class="from_name">Sara M</div>'
        '<div class="media_wrap clearfix"><div class="media_poll">A question</div></div></div>'
    )

    chat = read_export(_export(tmp_path, _post(5, poll) + _post(6, NAMELESS_FORWARD)))

    assert [(m.message_id, m.forwarded_from) for m in chat.messages] == [(6, "Sara M")]
    assert chat.left_out == {"poll": 1}


def test_content_of_a_kind_that_is_not_known_is_counted(tmp_path: Path) -> None:
    new_kind = (
        '<div class="media_wrap clearfix"><div class="media clearfix media_hologram">?</div></div>'
    )
    with_text = _post(5, DATE + new_kind + '<div class="text">look at this</div>')
    alone = _post(6, DATE + new_kind)

    chat = read_export(_export(tmp_path, with_text + alone))

    # The post with a text is read. The post that has only the new content is left out.
    assert [message.message_id for message in chat.messages] == [5]
    assert chat.left_out == {export_html.UNKNOWN_CONTENT: 2}


def test_a_post_with_nothing_to_read_is_counted(tmp_path: Path) -> None:
    chat = read_export(_export(tmp_path, _post(5, DATE)))

    assert chat.messages == []
    assert chat.left_out == {export_html.NO_CONTENT: 1}


def test_a_line_break_in_a_block_of_code_stays(tmp_path: Path) -> None:
    # Telegram Desktop writes <br> in a block of code. In HTML a line break of the page also
    # counts there, so the reader keeps it.
    body = DATE + '<div class="text">\nRun:<br><pre>make db\nmake test</pre>\n</div>'

    chat = read_export(_export(tmp_path, _post(5, body)))

    assert chat.messages[0].text == "Run:\nmake db\nmake test"

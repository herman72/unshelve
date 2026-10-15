"""The pieces of a text for the embedder. Each test name is one rule."""

# Persian words are in the test texts.
# ruff: noqa: RUF001

import re
from itertools import pairwise

from hypothesis import given
from hypothesis import strategies as st

from unshelve.embed.chunks import (
    LONGEST_PIECE_CHARS,
    MAX_PIECES,
    MIN_BODY_CHARS,
    PIECE_CHARS,
    Piece,
    card_text,
    pieces_of,
)

# 64 characters, with the space at its end.
SENTENCE = "Postgres keeps each job in one row, and a worker locks the row. "
# "I want to read this text?" The second word has a zero-width non-joiner (ZWNJ) in it.
PERSIAN = "من می\u200cخواهم این متن را بخوانم؟ "


def _paragraphs(count: int, sentences: int) -> str:
    return "\n\n".join(f"Part {number}. " + SENTENCE * sentences for number in range(count))


def _without_spaces(text: str) -> str:
    return re.sub(r"\s+", "", text)


# --- The card ----------------------------------------------------------------------------


def test_the_card_is_the_title_the_summary_and_the_key_points() -> None:
    card = card_text("Queues in Postgres", "How SKIP LOCKED works.", ["One row.", "A lease."])

    assert card == "Queues in Postgres\nHow SKIP LOCKED works.\nOne row.\nA lease."


def test_an_empty_part_is_left_out_of_the_card() -> None:
    assert card_text(None, " ", ["", "  One row. "]) == "One row."
    assert card_text(None, None, []) == ""


# --- Which text gets pieces --------------------------------------------------------------


def test_a_short_text_gets_no_piece_because_its_card_is_enough() -> None:
    assert pieces_of("") == []
    assert pieces_of("x" * MIN_BODY_CHARS) == []
    assert len(pieces_of("x" * (MIN_BODY_CHARS + 1))) == 1
    assert pieces_of(f"  {'x' * MIN_BODY_CHARS}\n\n") == []  # the spaces around do not count


def test_a_text_that_fits_in_one_piece_is_one_piece() -> None:
    text = f"  {SENTENCE * 20}\n"

    assert pieces_of(text) == [Piece(start=2, end=2 + 64 * 20 - 1, text=text.strip())]


# --- Where a piece ends ------------------------------------------------------------------


def test_a_piece_ends_after_a_paragraph_when_it_can() -> None:
    # Each paragraph has about 525 characters: two fit in one piece, three do not.
    found = pieces_of(_paragraphs(10, sentences=8))

    assert [piece.text.count("Part ") for piece in found] == [2, 2, 2, 2, 2]
    for piece in found:
        assert piece.text.startswith("Part ")
        assert piece.text.endswith("the row.")


def test_a_long_paragraph_is_cut_after_a_sentence() -> None:
    found = pieces_of(SENTENCE * 50)  # one paragraph of 3,200 characters

    assert [piece.text.count("row.") for piece in found] == [23, 23, 4]
    assert all(piece.text.endswith("the row.") for piece in found)


def test_an_empty_line_with_spaces_also_ends_a_paragraph() -> None:
    paragraphs = [f"Part {number}. " + SENTENCE * 8 for number in range(4)]

    found = pieces_of("\n \t\n".join(paragraphs))

    assert [piece.text.count("Part ") for piece in found] == [2, 2]


def test_a_long_paragraph_of_lines_is_cut_at_the_end_of_a_line() -> None:
    line = "a line of a list without a full stop at its end\n"  # 49 characters

    found = pieces_of(line * 80)

    assert len(found) > 1
    assert all(piece.text.endswith("at its end") for piece in found)


def test_a_sentence_end_near_the_start_does_not_make_a_short_piece() -> None:
    # The only full stop is after 7 characters. A piece of 7 characters would waste a vector,
    # so the piece ends after the last word that fits.
    found = pieces_of("Short. " + "word " * 400)

    assert len(found[0].text) > PIECE_CHARS - 10


def test_a_paragraph_without_a_sentence_end_is_cut_after_a_word() -> None:
    found = pieces_of("vector " * 500)  # 3,500 characters, and no full stop

    assert [piece.text.count("vector") for piece in found] == [214, 214, 72]
    for piece in found:
        assert piece.text.startswith("vector")
        assert piece.text.endswith("vector")


def test_a_word_longer_than_a_piece_is_cut_inside_it() -> None:
    found = pieces_of("a" * 4_000)

    assert [len(piece.text) for piece in found] == [PIECE_CHARS, PIECE_CHARS, 1_000]


def test_a_persian_sentence_ends_with_its_own_question_mark() -> None:
    found = pieces_of(PERSIAN * 100)

    assert len(found) > 1
    for piece in found:
        assert piece.text.startswith("من ")
        assert piece.text.endswith("بخوانم؟")


def test_a_zero_width_non_joiner_is_not_a_space() -> None:
    # A word that is written with a ZWNJ stays whole: a piece is never cut at the ZWNJ.
    word = "کتاب\u200cها"  # "books"
    found = pieces_of(f"{word} " * 600)

    assert len(found) > 1
    for piece in found:
        assert piece.text.startswith(word)
        assert piece.text.endswith(word)


# --- A long text -------------------------------------------------------------------------


def test_a_long_text_gets_longer_pieces_and_at_most_40() -> None:
    text = _paragraphs(100, sentences=15)  # about 97,000 characters

    found = pieces_of(text)

    assert len(found) <= MAX_PIECES
    assert max(len(piece.text) for piece in found) > PIECE_CHARS
    assert all(len(piece.text) <= LONGEST_PIECE_CHARS for piece in found)
    assert _without_spaces("".join(piece.text for piece in found)) == _without_spaces(text)


def test_a_piece_never_grows_past_the_longest_piece() -> None:
    # 36 paragraphs of 4,100 characters: each one is longer than the longest piece. The
    # pieces grow up to 4,000 characters and no further, so each paragraph is cut in two.
    paragraph = "word " * 820

    found = pieces_of("\n\n".join([paragraph] * 36))

    assert len(found) == MAX_PIECES
    assert max(len(piece.text) for piece in found) <= LONGEST_PIECE_CHARS


def test_a_very_long_text_gets_40_pieces_from_its_start() -> None:
    text = _paragraphs(400, sentences=15)  # about 388,000 characters

    found = pieces_of(text)

    assert len(found) == MAX_PIECES
    assert found[0].start == 0
    assert found[-1].end < len(text) // 2
    assert all(len(piece.text) <= LONGEST_PIECE_CHARS for piece in found)


# --- Rules for each text -----------------------------------------------------------------

_WORDS = st.sampled_from(
    ["vector", "search", "Postgres.", "row,", "می\u200cخواهم", "کتاب؟", "a" * 50, "x" * 2_000]
)
_GAPS = st.sampled_from([" ", " ", " ", "\n", "\n\n", "\t", "  \n \n "])
_TEXTS = st.lists(st.tuples(_WORDS, _GAPS), max_size=300).map(
    lambda pairs: "".join(word + gap for word, gap in pairs)
)


@given(_TEXTS)
def test_each_piece_is_the_text_between_its_start_and_its_end(text: str) -> None:
    found = pieces_of(text)

    for piece in found:
        assert piece.text == text[piece.start : piece.end]
        assert piece.text
        assert piece.text == piece.text.strip()
    for before, after in pairwise(found):
        assert before.end <= after.start


@given(_TEXTS)
def test_the_pieces_keep_to_the_limits(text: str) -> None:
    found = pieces_of(text)

    assert len(found) <= MAX_PIECES
    assert all(len(piece.text) <= LONGEST_PIECE_CHARS for piece in found)


@given(_TEXTS)
def test_only_the_end_of_a_text_that_needs_more_than_40_pieces_is_left_out(text: str) -> None:
    found = pieces_of(text)

    in_pieces = _without_spaces("".join(piece.text for piece in found))
    if len(text.strip()) <= MIN_BODY_CHARS:
        assert found == []
    elif len(found) < MAX_PIECES:
        assert in_pieces == _without_spaces(text)
    else:
        assert _without_spaces(text).startswith(in_pieces)

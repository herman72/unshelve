"""The public name of an item, made from its title. Each test name is one rule."""

import re
import unicodedata

import pytest
from hypothesis import given
from hypothesis import strategies as st

from unshelve.publish.visibility import SLUG_MAX, slug_for


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        ("Leases for Job Queues in Postgres", "leases-for-job-queues-in-postgres"),
        ("  Hello,   World!  ", "hello-world"),
        ("C++ & Rust: 2026", "c-rust-2026"),
        ("snake_case_title", "snake-case-title"),
        ("STRASSE und Straße", "strasse-und-strasse"),
    ],
)
def test_a_title_becomes_lowercase_words_joined_by_hyphens(title: str, slug: str) -> None:
    assert slug_for(title) == slug


def test_a_persian_title_keeps_its_letters() -> None:
    assert slug_for("صف کارها در پستگرس") == "صف-کارها-در-پستگرس"


def test_a_zero_width_joiner_inside_a_persian_word_is_a_cut() -> None:
    # The ZWNJ is not a letter: "می‌خواهم" is two parts in a name.
    assert slug_for("می‌خواهم") == "می-خواهم"


def test_a_mark_on_a_letter_is_no_cut() -> None:
    # NFKC joins "e" and its accent into "é" again.
    assert slug_for(unicodedata.normalize("NFD", "Café crème")) == "café-crème"
    # A Persian vowel sign has no joined form: it is left out, and the word stays one word.
    assert slug_for("کِتاب") == "کتاب"


@pytest.mark.parametrize("title", [None, "", "   ", "!!! ???", "—"])
def test_a_title_without_letters_or_digits_gives_item(title: str | None) -> None:
    assert slug_for(title) == "item"


def test_a_long_title_is_cut_after_a_whole_word() -> None:
    title = "Leases for job queues in Postgres, and what a worker does when its lease ends early"

    slug = slug_for(title)

    assert slug == "leases-for-job-queues-in-postgres-and-what-a-worker-does"
    assert len(slug) <= SLUG_MAX


def test_one_word_longer_than_the_limit_is_cut_at_the_limit() -> None:
    assert slug_for("x" * 80) == "x" * SLUG_MAX


@given(st.text(max_size=200))
def test_each_title_gives_a_name_of_words_and_single_hyphens(title: str) -> None:
    slug = slug_for(title)

    assert 0 < len(slug) <= SLUG_MAX
    # Word characters of any script, no underscore, one hyphen between two words: the form
    # that publishing accepts as a name.
    assert re.fullmatch(r"[^\W_]+(?:-[^\W_]+)*", slug)

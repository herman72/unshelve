"""The golden set of search is checked when it is loaded. Each test name is one rule."""

import json
from pathlib import Path
from typing import Any

import pytest

from unshelve.evals import golden

GOLDEN = Path(__file__).resolve().parents[2] / "evals" / "search" / "golden.json"


def _write(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "golden.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _set(**query: Any) -> dict[str, Any]:
    """A small set: two items and one query, changed by ``query``."""
    return {
        "items": [{"key": "queue", "title": "A queue"}, {"key": "bread", "title": "Bread"}],
        "queries": [{"q": "queue", "what": "a word", "grades": {"queue": 3}} | query],
    }


def test_the_committed_golden_set_is_valid() -> None:
    found = golden.load(GOLDEN)

    assert (len(found.items), len(found.queries)) == (39, 38)
    assert all(query.relevant for query in found.queries)


@pytest.mark.parametrize("grade", [4, -1])
def test_a_grade_outside_0_to_3_is_refused(tmp_path: Path, grade: int) -> None:
    with pytest.raises(ValueError, match=r"grades\.queue"):
        golden.load(_write(tmp_path, _set(grades={"queue": grade})))


def test_a_grade_for_an_item_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="items that do not exist: cake"):
        golden.load(_write(tmp_path, _set(grades={"queue": 3, "cake": 2})))


def test_a_query_without_an_item_that_helps_cannot_be_measured(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no item with a grade of 1 or more"):
        golden.load(_write(tmp_path, _set(grades={"queue": 0, "bread": 0})))


def test_two_items_with_one_key_are_refused(tmp_path: Path) -> None:
    data = _set()
    data["items"].append({"key": "queue", "title": "Another queue"})

    with pytest.raises(ValueError, match="same key"):
        golden.load(_write(tmp_path, data))


def test_two_queries_with_the_same_words_are_refused(tmp_path: Path) -> None:
    data = _set()
    data["queries"].append({"q": "queue", "what": "again", "grades": {"queue": 2}})

    with pytest.raises(ValueError, match="same words"):
        golden.load(_write(tmp_path, data))


def test_a_misspelt_field_is_refused(tmp_path: Path) -> None:
    # "summery" for "summary": an ignored field would leave the item without its summary, and
    # nothing would say so.
    data = _set()
    data["items"][0]["summery"] = "A queue of jobs."

    with pytest.raises(ValueError, match="summery"):
        golden.load(_write(tmp_path, data))


def test_only_the_grades_from_1_are_relevant(tmp_path: Path) -> None:
    found = golden.load(_write(tmp_path, _set(grades={"queue": 1, "bread": 0})))

    assert found.queries[0].relevant == {"queue"}

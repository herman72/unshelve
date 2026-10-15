"""The reading time of a text."""

from unshelve.reading import reading_minutes


def test_the_reading_time_is_at_least_one_minute() -> None:
    assert reading_minutes("") == 1
    assert reading_minutes("word " * 220) == 1
    assert reading_minutes("word " * 221) == 2

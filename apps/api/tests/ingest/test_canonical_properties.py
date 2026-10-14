"""Rules that are true for each address, and not only for the rows of a table.

The table in ``test_canonical.py`` has the addresses that I thought of. These tests let a
library make thousands of addresses from parts, also strange ones, and check a rule for each.
When a rule fails, the library gives the smallest address that breaks it.
"""

from urllib.parse import urlsplit

from hypothesis import assume, given
from hypothesis import strategies as st

from unshelve.ingest.canonical import canonicalize

_SCHEMES = st.sampled_from(["http://", "https://", "HTTP://", "HTTPS://", "ftp://", "//", ""])
_USERS = st.sampled_from(["", "", "", "user@", "user:secret@", "@"])
_HOST_STARTS = st.lists(
    st.sampled_from(["www.", "m.", "mobile.", "en.m.", "export."]), max_size=3
).map("".join)
_HOSTS = st.sampled_from(
    [
        "example.com",
        "EXAMPLE.com.",
        "youtube.com",
        "youtu.be",
        "youtube-nocookie.com",
        "x.com",
        "twitter.com",
        "fxtwitter.com",
        "arxiv.org",
        "github.com",
        "t.me",
        "telegram.me",
        "wikipedia.org",
        "bücher.example",
        "مثال.example",
        "[::1]",
        "127.0.0.1",
        "localhost",
    ]
)
_PORTS = st.sampled_from(["", "", "", ":80", ":443", ":8080", ":0", ":99999", ":x"])
_SEGMENTS = st.lists(
    st.one_of(
        st.sampled_from(
            [
                "",
                ".",
                "..",
                "s",
                "c",
                "i",
                "web",
                "status",
                "watch",
                "live",
                "shorts",
                "embed",
                "videoseries",
                "live_stream",
                "dQw4w9WgXcQ",
                "abs",
                "pdf",
                "2607.16100",
                "2607.16100v2.pdf",
                "hep-th",
                "9901001",
                "SomeUser",
                "SomeChannel",
                "1234567890",
                "repo.git",
                ".git",
                "r.git.git",
                "%41",
                "%2f",
                "%2E%2E",
                "%",
                "%%341",
                "café",
                "تهران",
                "a b",
            ]
        ),
        st.text(alphabet="abcXYZ019-._~%/ ?#&=@:+é‌", max_size=5),
    ),
    max_size=6,
)
_NAMES = st.sampled_from(
    ["v", "list", "t", "si", "tab", "single", "comment", "s", "page", "a", "b", "q", "", "%61"]
)
_VALUES = st.sampled_from(
    ["", "1", "2", "dQw4w9WgXcQ", "readme-ov-file", "a+b", "caf%c3%a9", "é", "%", "x=y"]
)
_PARAMETERS = st.lists(
    st.one_of(st.tuples(_NAMES, _VALUES).map("=".join), _NAMES, st.just("")), max_size=5
)
_TRACKING = st.sampled_from(
    ["utm_source=telegram", "UTM_Medium=social", "fbclid=IwAR0", "uclick_id=4f6d", "gclid="]
)
_FRAGMENTS = st.sampled_from(["", "", "#", "#top", "#/a/b", "#!/x", "#%41", "#:~:text=a"])


@st.composite
def address_parts(draw: st.DrawFn) -> tuple[str, list[str], str]:
    """An address in three parts: all before "?", the parameters, and the part from "#"."""
    start = draw(_SCHEMES) + draw(_USERS) + draw(_HOST_STARTS) + draw(_HOSTS) + draw(_PORTS)
    return start + "/" + "/".join(draw(_SEGMENTS)), draw(_PARAMETERS), draw(_FRAGMENTS)


def _join(start: str, parameters: list[str], fragment: str) -> str:
    return start + ("?" + "&".join(parameters) if parameters else "") + fragment


def _is_a_web_address(text: str) -> bool:
    try:
        parts = urlsplit(text.strip())
        _ = parts.port
    except ValueError:
        return False
    return parts.scheme in {"http", "https"} and bool((parts.hostname or "").rstrip("."))


@given(address_parts())
def test_a_canonical_form_does_not_change_when_it_goes_in_again(
    parts: tuple[str, list[str], str],
) -> None:
    once = canonicalize(_join(*parts))

    assert canonicalize(once) == once


@given(st.text(max_size=60))
def test_no_text_makes_the_function_fail(text: str) -> None:
    once = canonicalize(text)

    assert canonicalize(once) == once


@given(address_parts(), _TRACKING, st.integers(min_value=0, max_value=5))
def test_a_tracking_parameter_never_changes_the_canonical_form(
    parts: tuple[str, list[str], str], tracking: str, position: int
) -> None:
    start, parameters, fragment = parts
    assume("?" not in start and "#" not in start)
    # A space at the end of an address is not a part of it. With a parameter after it, the
    # same space is in the middle. Telegram never gives an address with a space.
    assume(start == start.strip())
    assume(_is_a_web_address(_join(start, parameters, fragment)))
    with_tracking = [*parameters[:position], tracking, *parameters[position:]]

    assert canonicalize(_join(start, with_tracking, fragment)) == canonicalize(
        _join(start, parameters, fragment)
    )

"""The canonical form of a web address. The table is the contract of version 1 of the rules.

A row says: this address has that canonical form. A change of a row is a change of the rules,
so it needs a new ``canonical.VERSION``.
"""

import pytest

from unshelve.ingest import canonical
from unshelve.ingest.canonical import canonicalize

SAME_PAGE = [
    # --- The scheme, the host and the port -------------------------------------------------
    ("https://example.com/post", "https://example.com/post"),
    ("http://example.com/post", "https://example.com/post"),
    ("HTTP://EXAMPLE.COM/post", "https://example.com/post"),
    ("https://Example.Com/Post", "https://example.com/Post"),  # a path keeps its capitals
    ("https://www.example.com/post", "https://example.com/post"),
    ("http://www.example.com/post", "https://example.com/post"),
    ("https://example.com:443/post", "https://example.com/post"),
    ("http://example.com:80/post", "https://example.com/post"),
    ("https://example.com./post", "https://example.com/post"),
    ("  https://example.com/post \n", "https://example.com/post"),
    ("http://www.example.com/post/?utm_source=telegram#top", "https://example.com/post"),
    ("https://blog.example.com/post", "https://blog.example.com/post"),  # not "www"
    ("https://www.com/post", "https://www.com/post"),  # "www" is the name of this site
    ("https://bücher.example/buch", "https://xn--bcher-kva.example/buch"),
    ("https://BÜCHER.example/buch", "https://xn--bcher-kva.example/buch"),
    ("https://xn--bcher-kva.example/buch", "https://xn--bcher-kva.example/buch"),
    ("https://user:secret@example.com/post", "https://user:secret@example.com/post"),
    # --- The path ---------------------------------------------------------------------------
    ("https://example.com", "https://example.com/"),
    ("https://example.com/", "https://example.com/"),
    ("https://example.com/blog/post/", "https://example.com/blog/post"),
    ("https://example.com/a/./b/../c", "https://example.com/a/c"),
    ("https://example.com/../a", "https://example.com/a"),
    ("https://example.com/a/..", "https://example.com/"),
    ("https://example.com/a%2fb", "https://example.com/a%2Fb"),  # an escaped slash stays
    ("https://example.com/%7Euser", "https://example.com/~user"),
    ("https://example.com/%41bc", "https://example.com/Abc"),
    ("https://example.com/a b", "https://example.com/a%20b"),
    ("https://example.com/a%20b", "https://example.com/a%20b"),
    ("https://example.com/café", "https://example.com/caf%C3%A9"),
    ("https://example.com/caf%c3%a9", "https://example.com/caf%C3%A9"),
    (
        "https://fa.wikipedia.org/wiki/تهران",
        "https://fa.wikipedia.org/wiki/%D8%AA%D9%87%D8%B1%D8%A7%D9%86",
    ),
    (
        "https://fa.wikipedia.org/wiki/%D8%AA%D9%87%D8%B1%D8%A7%D9%86",
        "https://fa.wikipedia.org/wiki/%D8%AA%D9%87%D8%B1%D8%A7%D9%86",
    ),
    ("https://example.com/a//b", "https://example.com/a//b"),  # an empty part is not removed
    # --- The parameters ---------------------------------------------------------------------
    ("https://example.com/post?", "https://example.com/post"),
    ("https://example.com/post?utm_source=telegram", "https://example.com/post"),
    (
        "https://example.com/post?utm_source=x&utm_medium=social&utm_campaign=y",
        "https://example.com/post",
    ),
    ("https://example.com/post?UTM_Source=x", "https://example.com/post"),
    ("https://example.com/post?fbclid=IwAR0abc", "https://example.com/post"),
    ("https://example.com/post?gclid=abc&id=7", "https://example.com/post?id=7"),
    ("https://example.com/post?uclick_id=4f6d", "https://example.com/post"),
    ("https://example.com/post?mc_cid=1&mc_eid=2", "https://example.com/post"),
    ("https://example.com/post?igshid=abc", "https://example.com/post"),
    ("https://example.com/post?_hsenc=a&_hsmi=b", "https://example.com/post"),
    ("https://example.com/post?b=2&a=1", "https://example.com/post?a=1&b=2"),
    ("https://example.com/post?a=1&b=2", "https://example.com/post?a=1&b=2"),
    ("https://example.com/post?tag=b&tag=a", "https://example.com/post?tag=b&tag=a"),
    ("https://example.com/post?a=1&&b=2&", "https://example.com/post?a=1&b=2"),
    ("https://example.com/search?q=a+b", "https://example.com/search?q=a+b"),
    ("https://example.com/search?q=caf%c3%a9", "https://example.com/search?q=caf%C3%A9"),
    ("https://example.com/search?q=café", "https://example.com/search?q=caf%C3%A9"),
    ("https://example.com/post?download", "https://example.com/post?download"),
    ("https://example.com/post?page=2&utm_term=z", "https://example.com/post?page=2"),
    # These names are too general to be removed: a site can need them.
    ("https://example.com/post?ref=home", "https://example.com/post?ref=home"),
    ("https://example.com/post?source=rss", "https://example.com/post?source=rss"),
    # --- The part after "#" -----------------------------------------------------------------
    ("https://example.com/post#comments", "https://example.com/post"),
    ("https://example.com/post#:~:text=some%20words", "https://example.com/post"),
    ("https://example.com/post?id=7#top", "https://example.com/post?id=7"),
    ("https://example.com/#/inbox/5", "https://example.com/#/inbox/5"),  # an old web app
    ("https://example.com/#!/inbox/5", "https://example.com/#!/inbox/5"),
    # --- YouTube ----------------------------------------------------------------------------
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://youtube.com/watch?v=dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ", "https://youtube.com/watch?v=dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ?si=AbCdEf123", "https://youtube.com/watch?v=dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ?t=42", "https://youtube.com/watch?v=dQw4w9WgXcQ"),
    ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "https://youtube.com/watch?v=dQw4w9WgXcQ"),
    (
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123&index=4&t=10s",
        "https://youtube.com/watch?v=dQw4w9WgXcQ",
    ),
    (
        "https://www.youtube.com/watch?feature=youtu.be&v=dQw4w9WgXcQ",
        "https://youtube.com/watch?v=dQw4w9WgXcQ",
    ),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "https://youtube.com/watch?v=dQw4w9WgXcQ"),
    ("https://www.youtube.com/live/dQw4w9WgXcQ", "https://youtube.com/watch?v=dQw4w9WgXcQ"),
    (
        "https://www.youtube.com/live/dQw4w9WgXcQ?si=abc&t=120",
        "https://youtube.com/watch?v=dQw4w9WgXcQ",
    ),
    ("https://www.youtube.com/embed/dQw4w9WgXcQ", "https://youtube.com/watch?v=dQw4w9WgXcQ"),
    (
        "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
        "https://youtube.com/watch?v=dQw4w9WgXcQ",
    ),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ#t=30", "https://youtube.com/watch?v=dQw4w9WgXcQ"),
    # A playlist, a channel and two words with 11 characters are not a video.
    ("https://www.youtube.com/playlist?list=PL123", "https://youtube.com/playlist?list=PL123"),
    ("https://www.youtube.com/@SomeChannel", "https://youtube.com/@SomeChannel"),
    (
        "https://www.youtube.com/embed/videoseries?list=PL123",
        "https://youtube.com/embed/videoseries?list=PL123",
    ),
    ("https://www.youtube.com/@SomeChannel?si=abc", "https://youtube.com/@SomeChannel"),
    # --- X (Twitter) ------------------------------------------------------------------------
    ("https://twitter.com/SomeUser/status/1234567890", "https://x.com/someuser/status/1234567890"),
    ("https://x.com/someuser/status/1234567890", "https://x.com/someuser/status/1234567890"),
    (
        "https://twitter.com/SomeUser/status/1234567890?s=20&t=AbCdEf",
        "https://x.com/someuser/status/1234567890",
    ),
    (
        "https://x.com/SomeUser/status/1234567890?s=20&t=AbCdEf",
        "https://x.com/someuser/status/1234567890",
    ),
    (
        "https://mobile.twitter.com/SomeUser/status/1234567890",
        "https://x.com/someuser/status/1234567890",
    ),
    (
        "https://twitter.com/SomeUser/status/1234567890/photo/1",
        "https://x.com/someuser/status/1234567890",
    ),
    (
        "https://twitter.com/SomeUser/status/1234567890?ref_src=twsrc%5Etfw",
        "https://x.com/someuser/status/1234567890",
    ),
    (
        "https://fxtwitter.com/SomeUser/status/1234567890",
        "https://x.com/someuser/status/1234567890",
    ),
    (
        "https://vxtwitter.com/SomeUser/status/1234567890",
        "https://x.com/someuser/status/1234567890",
    ),
    ("https://twitter.com/i/web/status/1234567890", "https://x.com/i/status/1234567890"),
    ("https://x.com/i/status/1234567890", "https://x.com/i/status/1234567890"),
    ("https://twitter.com/SomeUser", "https://x.com/SomeUser"),
    ("https://twitter.com/SomeUser?s=21&t=abc", "https://x.com/SomeUser"),
    # --- arXiv ------------------------------------------------------------------------------
    ("https://arxiv.org/abs/2607.16100", "https://arxiv.org/abs/2607.16100"),
    ("https://arxiv.org/abs/2607.16100v2", "https://arxiv.org/abs/2607.16100"),
    ("https://arxiv.org/pdf/2607.16100", "https://arxiv.org/abs/2607.16100"),
    ("https://arxiv.org/pdf/2607.16100v3.pdf", "https://arxiv.org/abs/2607.16100"),
    ("https://arxiv.org/html/2607.16100v1", "https://arxiv.org/abs/2607.16100"),
    ("http://export.arxiv.org/abs/2607.16100", "https://arxiv.org/abs/2607.16100"),
    ("https://www.arxiv.org/abs/2607.16100", "https://arxiv.org/abs/2607.16100"),
    ("https://arxiv.org/abs/hep-th/9901001", "https://arxiv.org/abs/hep-th/9901001"),
    ("https://arxiv.org/abs/math.GT/0309136v2", "https://arxiv.org/abs/math.GT/0309136"),
    ("https://arxiv.org/list/cs.LG/recent", "https://arxiv.org/list/cs.LG/recent"),
    # --- GitHub -----------------------------------------------------------------------------
    ("https://github.com/Cloudflare/Workers-SDK", "https://github.com/cloudflare/workers-sdk"),
    ("http://github.com/cloudflare/workers-sdk", "https://github.com/cloudflare/workers-sdk"),
    ("https://github.com/cloudflare/workers-sdk/", "https://github.com/cloudflare/workers-sdk"),
    ("https://github.com/cloudflare/workers-sdk.git", "https://github.com/cloudflare/workers-sdk"),
    (
        "https://github.com/cloudflare/workers-sdk?tab=readme-ov-file",
        "https://github.com/cloudflare/workers-sdk",
    ),
    (
        "https://github.com/cloudflare/workers-sdk#readme",
        "https://github.com/cloudflare/workers-sdk",
    ),
    (
        "https://github.com/Cloudflare/Workers-SDK/blob/main/README.md",
        "https://github.com/cloudflare/workers-sdk/blob/main/README.md",
    ),
    (
        "https://github.com/SomeUser?tab=repositories",
        "https://github.com/someuser?tab=repositories",
    ),
    # --- Telegram ---------------------------------------------------------------------------
    ("https://t.me/SomeChannel/590", "https://t.me/somechannel/590"),
    ("https://telegram.me/somechannel/590", "https://t.me/somechannel/590"),
    ("https://t.me/s/somechannel/590", "https://t.me/somechannel/590"),
    ("https://t.me/somechannel/590?single", "https://t.me/somechannel/590"),
    ("https://t.me/somechannel/590?comment=12", "https://t.me/somechannel/590?comment=12"),
    ("https://t.me/c/1234567890/45", "https://t.me/c/1234567890/45"),
    ("https://t.me/+AbCdEfGhIjK", "https://t.me/+AbCdEfGhIjK"),  # an invitation keeps its capitals
    ("https://t.me/joinchat/AbCdEfGhIjK", "https://t.me/joinchat/AbCdEfGhIjK"),
    # --- Wikipedia on a phone ---------------------------------------------------------------
    ("https://en.m.wikipedia.org/wiki/Job_queue", "https://en.wikipedia.org/wiki/Job_queue"),
    (
        "https://fa.m.wikipedia.org/wiki/تهران",
        "https://fa.wikipedia.org/wiki/%D8%AA%D9%87%D8%B1%D8%A7%D9%86",
    ),
    # --- Strange addresses that the property tests found -----------------------------------
    # Each one changed a second time when its canonical form went into the function again.
    ("https://www.www.example.com/post", "https://example.com/post"),
    ("https://en.m.m.wikipedia.org/wiki/Job_queue", "https://en.wikipedia.org/wiki/Job_queue"),
    ("https://t.me/s/s/somechannel/590", "https://t.me/somechannel/590"),
    ("https://t.me/s/s/s", "https://t.me/s"),
    (
        "https://github.com/cloudflare/workers-sdk.git.git",
        "https://github.com/cloudflare/workers-sdk",
    ),
    ("https://github.com/cloudflare/.git", "https://github.com/cloudflare/.git"),
    ("https://github.com//.git", "https://github.com//.git"),
    ("https://example.com/100%", "https://example.com/100%25"),
    ("https://example.com/%%341", "https://example.com/%2541"),
    ("https://example.com/post?discount=100%", "https://example.com/post?discount=100%25"),
]

# Two addresses that look alike and that can be two pages. They must stay different.
DIFFERENT_PAGES = [
    ("https://example.com/Post", "https://example.com/post"),
    ("https://example.com/post?id=1", "https://example.com/post?id=2"),
    ("https://example.com/post?page=1", "https://example.com/post"),
    ("https://example.com/a/b", "https://example.com/a%2Fb"),
    ("https://example.com/post", "https://blog.example.com/post"),
    ("https://example.com/#/inbox/5", "https://example.com/#/inbox/6"),
    ("http://example.com:8080/post", "https://example.com/post"),
    ("https://example.com/post?tag=a&tag=b", "https://example.com/post?tag=b&tag=a"),
    ("https://youtube.com/watch?v=dQw4w9WgXcQ", "https://youtube.com/watch?v=aaaaaaaaaaa"),
    ("https://youtube.com/playlist?list=PL123", "https://youtube.com/playlist?list=PL456"),
    ("https://music.youtube.com/watch?v=dQw4w9WgXcQ", "https://youtube.com/watch?v=dQw4w9WgXcQ"),
    ("https://x.com/someuser/status/1", "https://x.com/someuser/status/2"),
    ("https://arxiv.org/abs/2607.16100", "https://arxiv.org/abs/2607.16101"),
    (
        "https://github.com/cloudflare/workers-sdk",
        "https://github.com/cloudflare/workers-sdk/tree/main/docs",
    ),
    (
        "https://github.com/cloudflare/workers-sdk/blob/main/README.md",
        "https://github.com/cloudflare/workers-sdk/blob/main/readme.md",
    ),
    ("https://t.me/somechannel/590", "https://t.me/somechannel/591"),
    ("https://t.me/somechannel/590", "https://t.me/somechannel/590?comment=12"),
    ("https://t.me/+AbCdEfGhIjK", "https://t.me/+abcdefghijk"),
]

# Not a web address, or not readable: the text comes back as it is.
LEFT_ALONE = [
    "mailto:someone@example.com",
    "tg://resolve?domain=somechannel",
    "magnet:?xt=urn:btih:abc",
    "example.com/post",
    "https://",
    "https://example.com:port/post",
    "not an address",
    "",
]


def test_the_table_has_the_number_of_rows_that_the_plan_asks_for() -> None:
    assert len(SAME_PAGE) >= 60


@pytest.mark.parametrize(("address", "expected"), SAME_PAGE)
def test_an_address_gets_its_canonical_form(address: str, expected: str) -> None:
    assert canonicalize(address) == expected


@pytest.mark.parametrize(("address", "expected"), SAME_PAGE)
def test_a_canonical_form_does_not_change_again(address: str, expected: str) -> None:
    # If this fails, an address gets a different key the second time that a rule sees it.
    assert canonicalize(expected) == expected
    assert canonicalize(canonicalize(address)) == canonicalize(address)


@pytest.mark.parametrize(("one", "other"), DIFFERENT_PAGES)
def test_two_addresses_that_can_be_two_pages_stay_different(one: str, other: str) -> None:
    assert canonicalize(one) != canonicalize(other)


@pytest.mark.parametrize("text", LEFT_ALONE)
def test_a_text_that_is_not_a_web_address_comes_back_as_it_is(text: str) -> None:
    assert canonicalize(text) == text


def test_spaces_around_a_text_that_is_not_a_web_address_are_removed() -> None:
    assert canonicalize("  mailto:someone@example.com ") == "mailto:someone@example.com"


def test_the_rules_have_a_version() -> None:
    assert canonical.VERSION == 1

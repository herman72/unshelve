"""The canonical form of a web address: one text for the addresses that open the same page.

Many addresses open one page: with ``www.`` or without it, with ``http`` or ``https``, with a
parameter that a share button added. ``canonicalize`` gives the same text for all of them.
Unshelve uses that text to see that a link was saved before (docs/adr/0003).

The function is pure. It opens no address, so it changes only what is safe to change without
a look at the page. It is careful on purpose. Two addresses that it joins by mistake hide an
item. Two addresses that it does not join show an item two times. The second mistake is the
cheap one, so in doubt the addresses stay different.

Its rules are a version. The stored messages keep each address as it arrived, so a later
version can make the keys of old items again.
"""

import re
from collections.abc import Callable, Collection
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

import idna

# Raise this number when a rule changes the result for an address.
VERSION = 1

_DEFAULT_PORT = {"http": 80, "https": 443}
_PLAIN = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")
_LONE_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
# The characters that can stay as they are in each part of an address (RFC 3986).
_PATH_SAFE = "/%:@!$&'()*+,;="
_QUERY_SAFE = "/%:@!$'()*+,;=?"
_FRAGMENT_SAFE = "/%:@!$&'()*+,;=?"

# Parameters that a share button or an advertising system adds. They do not change the page.
_TRACKING = frozenset(
    {
        "fbclid",
        "gclid",
        "gclsrc",
        "dclid",
        "gbraid",
        "wbraid",
        "msclkid",
        "yclid",
        "ysclid",
        "twclid",
        "ttclid",
        "li_fat_id",
        "igshid",
        "igsh",
        "mc_cid",
        "mc_eid",
        "_hsenc",
        "_hsmi",
        "__hstc",
        "__hsfp",
        "__hssc",
        "hsctatracking",
        "mkt_tok",
        "vero_id",
        "vero_conv",
        "oly_enc_id",
        "oly_anon_id",
        "_ga",
        "_gl",
        "_openstat",
        "srsltid",
        "s_cid",
        "spm",
        "uclick_id",
        "ref_src",
        "ref_url",
    }
)
_TRACKING_PREFIX = "utm_"


@dataclass(slots=True)
class _Address:
    scheme: str
    userinfo: str
    host: str
    port: int | None
    path: str
    query: list[str]  # the parts between "&", each one as "name=value"
    fragment: str


def _one_spelling(text: str, safe: str) -> str:
    """Write each character in one way: ``%C3%A9`` for a character outside ASCII, upper-case
    letters in ``%2F``, and a plain ``a`` in the place of ``%61``."""

    def spell(found: re.Match[str]) -> str:
        character = chr(int(found.group(1), 16))
        return character if character in _PLAIN else f"%{found.group(1).upper()}"

    # A "%" that starts no "%XX" is a plain percent sign that somebody did not escape.
    return _ESCAPE.sub(spell, quote(_LONE_PERCENT.sub("%25", text), safe=safe))


def _name(part: str) -> str:
    """The name of one query parameter, in lower case."""
    return unquote(part.split("=", 1)[0]).lower()


def _value(address: _Address, name: str) -> str | None:
    for part in address.query:
        if _name(part) == name:
            return unquote(part.partition("=")[2])
    return None


def _drop(address: _Address, names: Collection[str]) -> None:
    address.query = [part for part in address.query if _name(part) not in names]


def _is_tracking(name: str) -> bool:
    return name in _TRACKING or name.startswith(_TRACKING_PREFIX)


def _without_dot_segments(path: str) -> str:
    """``/a/./b/../c`` is ``/a/c``. The result has no slash at its end, except ``/``."""
    kept: list[str] = []
    for segment in path.split("/")[1:]:
        if segment == "..":
            if kept:
                kept.pop()
        elif segment != ".":
            kept.append(segment)
    return ("/" + "/".join(kept)).rstrip("/") or "/"


def _ascii_host(host: str) -> str:
    if host.isascii():
        return host
    try:
        # The same rules that the HTTP client uses, so the two agree on the name.
        return idna.encode(host, uts46=True).decode("ascii")
    except idna.IDNAError:
        return host


# --- Sites with addresses of their own ------------------------------------------------------
# Only the sites that get an extractor of their own (docs/product.md), and Wikipedia.

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_NOT_A_VIDEO = frozenset({"videoseries", "live_stream"})  # two words with 11 characters


def _youtube(address: _Address) -> None:
    """One video has many addresses: youtu.be/ID, /watch?v=ID, /shorts/ID, /live/ID, /embed/ID."""
    segments = address.path.split("/")[1:]
    video: str | None = None
    if address.host == "youtu.be":
        video = segments[0]
    elif segments[0] == "watch":
        video = _value(address, "v")
    elif segments[0] in {"shorts", "live", "embed", "v"} and len(segments) > 1:
        video = segments[1]
    if video and _VIDEO_ID.match(video) and video not in _NOT_A_VIDEO:
        # The time in the video and the playlist around it are not a different video.
        address.host, address.path, address.query = "youtube.com", "/watch", [f"v={video}"]
        address.fragment = ""
        return
    if address.host == "m.youtube.com":
        address.host = "youtube.com"
    _drop(address, {"si", "feature", "pp", "ab_channel", "app"})


_POST = re.compile(r"^/([A-Za-z0-9_]{1,15})/status(?:es)?/(\d+)")
_POST_WITHOUT_NAME = re.compile(r"^/i/web/status/(\d+)")


def _x(address: _Address) -> None:
    """A post on X has a number. The name before it can have capital letters or not."""
    address.host = "x.com"
    if found := _POST.match(address.path):
        address.path = f"/{found.group(1).lower()}/status/{found.group(2)}"
    elif found := _POST_WITHOUT_NAME.match(address.path):
        address.path = f"/i/status/{found.group(1)}"
    else:
        _drop(address, {"s", "t"})
        return
    address.query, address.fragment = [], ""


_PAPER = re.compile(
    r"^/(?:abs|pdf|html|ps|format)/"
    r"(\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})"  # 2607.16100, or the old hep-th/9901001
    r"(?:v\d+)?(?:\.pdf)?$"
)


def _arxiv(address: _Address) -> None:
    """One paper: its page, its PDF and each of its versions."""
    address.host = "arxiv.org"
    if found := _PAPER.match(address.path):
        address.path, address.query, address.fragment = f"/abs/{found.group(1)}", [], ""


def _github(address: _Address) -> None:
    """The owner and the repository can have capital letters or not. A file path cannot."""
    segments = address.path.split("/")
    for position in (1, 2):
        if position < len(segments):
            segments[position] = segments[position].lower()
    if len(segments) == 3:
        while len(segments[2]) > 4 and segments[2].endswith(".git"):
            segments[2] = segments[2][:-4]
    address.path = "/".join(segments)
    address.query = [part for part in address.query if part != "tab=readme-ov-file"]


_TELEGRAM_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


def _telegram(address: _Address) -> None:
    address.host = "t.me"
    segments = address.path.split("/")
    while len(segments) > 2 and segments[1] == "s":
        del segments[1]  # t.me/s/name/5 shows the post t.me/name/5 in a browser
    if len(segments) > 1 and _TELEGRAM_NAME.match(segments[1]):
        segments[1] = segments[1].lower()
    address.path = "/".join(segments)
    _drop(address, {"single"})


_SITES: dict[str, Callable[[_Address], None]] = {
    "youtube.com": _youtube,
    "m.youtube.com": _youtube,
    "youtu.be": _youtube,
    "youtube-nocookie.com": _youtube,
    "x.com": _x,
    "twitter.com": _x,
    "mobile.x.com": _x,
    "mobile.twitter.com": _x,
    "m.twitter.com": _x,
    "fxtwitter.com": _x,
    "vxtwitter.com": _x,
    "fixupx.com": _x,
    "arxiv.org": _arxiv,
    "export.arxiv.org": _arxiv,
    "browse.arxiv.org": _arxiv,
    "github.com": _github,
    "t.me": _telegram,
    "telegram.me": _telegram,
    "telegram.dog": _telegram,
}
_WIKIPEDIA_ON_A_PHONE = ".m.wikipedia.org"


def _text(address: _Address) -> str:
    host = f"[{address.host}]" if ":" in address.host else address.host
    text = f"{address.scheme}://"
    if address.userinfo:
        text += f"{address.userinfo}@"
    text += host
    if address.port is not None:
        text += f":{address.port}"
    text += address.path
    if address.query:
        text += "?" + "&".join(address.query)
    if address.fragment:
        text += "#" + address.fragment
    return text


def canonicalize(url: str) -> str:
    """The canonical form of a web address. An address that is not http or https, or that
    cannot be read, comes back as it is, without the spaces around it."""
    text = url.strip()
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:  # a port that is not a number, or a broken host in brackets
        return text
    scheme = parts.scheme
    host = (parts.hostname or "").rstrip(".")
    if scheme not in _DEFAULT_PORT or not host:
        return text

    if port == _DEFAULT_PORT[scheme]:
        port = None
    if scheme == "http" and port is None:
        scheme = "https"  # the same page; a site on a port of its own keeps its scheme
    host = _ascii_host(host)
    # A loop, so that the result of this function does not change when it goes in again.
    while host.startswith("www.") and "." in host[4:]:
        host = host[4:]
    while host.endswith(_WIKIPEDIA_ON_A_PHONE):
        host = host.removesuffix(_WIKIPEDIA_ON_A_PHONE) + ".wikipedia.org"

    query = [_one_spelling(part, _QUERY_SAFE) for part in parts.query.split("&") if part]
    address = _Address(
        scheme=scheme,
        userinfo=parts.netloc.rpartition("@")[0],
        host=host,
        port=port,
        path=_without_dot_segments(_one_spelling(parts.path, _PATH_SAFE) or "/"),
        # The order of the parameters does not change the page. The parameters with the same
        # name keep their order.
        query=sorted((part for part in query if not _is_tracking(_name(part))), key=_name),
        # The part after "#" is a place in the page. Old web apps use "#/..." and "#!..." as
        # the address of a page, so these stay.
        fragment=(
            _one_spelling(parts.fragment, _FRAGMENT_SAFE)
            if parts.fragment.startswith(("/", "!"))
            else ""
        ),
    )
    if (site := _SITES.get(address.host)) is not None:
        site(address)
    return _text(address)

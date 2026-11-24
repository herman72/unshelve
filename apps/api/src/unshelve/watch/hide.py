"""No secret leaves the watcher (docs/learn/55, part 3).

A text from the database can hold a secret: an error of a provider can quote a key, and an
address can hold a password. The watcher puts such texts into messages, so ``hide_secrets``
removes each secret first. These are its steps, in this order:

1. Values: each character of a value of ``Secrets`` is marked.
2. Cut ends: an earlier cut can end in the first part of a secret. The worker keeps the first
   2000 characters of an error, and an event the first 300. If the text ends with the first
   ``SECRET_PART_MIN`` or more characters of a value, they are marked. The same at the start
   of the text, with the last characters of a value. White space at an end does not count,
   because a line break can come after a cut. Each run of marked characters becomes one
   ``<secret>``.
3. Patterns: the shape of a key that the watcher does not hold, such as ``sk-ant-...``.
4. Clean: a control character or a format character, such as a zero-width space, is a
   character that a person cannot see. In an error text, each run of white space becomes one
   space, so the text is one line, and each other such character goes. A message keeps each
   "\\n", and each other such character goes. The removal of a character can join two parts of
   a secret, so steps 1 to 3 run again on the clean text.
5. Cut: an error text is cut to ``limit`` characters. The cut comes last, so it never leaves
   the first part of a secret at the end.
"""

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from urllib.parse import quote, quote_plus, unquote, unquote_plus, urlsplit

from pydantic import SecretStr

from unshelve.watch.limits import ERROR_CHARS, SECRET_MIN, SECRET_PART_MIN

if TYPE_CHECKING:
    from unshelve.config import Settings

HIDDEN: Final = "<secret>"
CUT: Final = "…"

# The shapes of keys that the watcher does not hold, in this order. A pattern hides a key with
# a label, such as "<voyage key>". The order keeps a label whole: "Bearer" comes first, and the
# ping comes before the keys, so each takes a key whole before another pattern gives it a label.
# A second call changes nothing.
_PATTERNS: Final = (
    # A header. It comes first, so it hides the whole token after "Bearer": also a token that
    # starts with a hidden part, and a key that a later pattern would hide with a label. An
    # invisible character can stop "Bearer" in the first run of the steps, and then a pattern
    # hides the key with a label. The clean step joins the two, so in the second run, the token
    # takes each label of a pattern below whole. The token ends before a "…": a cut can
    # end in the "<secret>" of an earlier call, and a token that took the "…" would grow back to
    # the whole "<secret>" in a second call.
    (
        re.compile(r"(?i)\bbearer\s+(?:<(?:ping url|anthropic key|bot token|voyage key)>|[^\s…])+"),
        "Bearer <secret>",
    ),
    # The address of a ping, which holds its own key. It comes before the keys, so it takes a
    # key in its path whole. A cut can end just after the "/": then "…" follows, and no key.
    # Without the lookahead, a second call would change that text.
    (re.compile(r"https?://hc-ping\.com/(?!…)\S+"), "<ping url>"),
    # A key of Anthropic.
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"), "<anthropic key>"),
    # A bot token, as the bot hides it in its log (bot/runner.py). A lookbehind, not \b: in
    # "bot123456789:...", the "t" and the "1" are both word characters, so \b does not match.
    (re.compile(r"(?<![0-9])[0-9]{5,12}:[A-Za-z0-9_-]{30,}"), "<bot token>"),
    # A key of the Voyage platform (pa-), or a model key of MongoDB Atlas (al-).
    (re.compile(r"(?<![A-Za-z0-9])(?:pa|al)-[A-Za-z0-9_-]{20,}"), "<voyage key>"),
    # The whole user part of an address: a token can be the user, with no password.
    (re.compile(r"(?<=://)[^/\s@]+@"), "<secret>@"),
)
_WHITE_SPACE: Final = re.compile(r"\s+")
# The password in an address, as SQLAlchemy reads it (sqlalchemy/engine/url.py, _parse_url).
_SQLALCHEMY_PASSWORD: Final = re.compile(r"[\w+]+://[^:/]*:([^@]*)@")
# The password as it is in the address, up to the last "@". deploy/compose.yaml puts the
# password into the address as it is.
_RAW_PASSWORD: Final = re.compile(r"[\w+]+://[^:/]*:(.*)@")
# The user of an address with no password, as SQLAlchemy reads it: up to the last "@" before
# the first ":" or "/".
_USER: Final = re.compile(r"[\w+]+://([^:/]*)@")
# The characters that can end a part of the rest after the first "@" of a password. SQLAlchemy
# ends the host at "/", ":" or "?", the port at "/" or "?", and the database at "?". It reads an
# IPv6 host between "[" and "]", and a key or a value of the query between "?", "&" and "=".
# urlsplit ends an address at "#", and a user part at "@". Each part between two of these places
# is a value, so a character too many only gives more values.
_PART_END: Final = re.compile(r"[@:/?#=&\[\]]")
# A part holds at most this many characters of _PART_END. A host, a port, a database, or a key
# or a value of the query holds fewer. So the number of parts grows with the length of the
# password, not with its square.
_PART_HOLDS: Final = 8
# A password in the query of an address. libpq and SQLAlchemy read it too.
_QUERY_PASSWORD: Final = re.compile(r"[?&]password=([^&]*)")
# os.environ decodes a byte that is not UTF-8 as a lone surrogate. Strict UTF-8 cannot encode
# it, so the encoded form gives the byte back, as in "%FF".
_BYTE: Final = "surrogateescape"
# The Unicode categories of the characters that a person cannot see: a control character and
# a format character, such as a zero-width space.
_INVISIBLE: Final = frozenset({"Cc", "Cf"})


@dataclass(frozen=True, slots=True, repr=False)
class Secrets:
    """The values to hide: the longest first, then by text. Each order of the same values gives
    the same ``Secrets``. An empty value is dropped, because it would match everywhere. The repr
    shows only the number of values, as a ``SecretStr`` shows only "**********"."""

    values: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        values = sorted({value for value in self.values if value}, key=lambda v: (-len(v), v))
        object.__setattr__(self, "values", tuple(values))

    def __repr__(self) -> str:
        return f"Secrets({len(self.values)} hidden)"


def secrets_of(settings: "Settings", environ: Mapping[str, str]) -> Secrets:
    """The secrets that the watcher holds: each ``SecretStr`` of ``settings``, the key of
    Anthropic (an environment variable, not a setting), the password of the database, and the
    token in the address of the git target.

    A value shorter than ``SECRET_MIN`` is not a secret. So the default password "unshelve"
    stays in each fix text that names the command ``unshelve``.
    """
    found = [environ.get("ANTHROPIC_API_KEY", "")]
    for name in type(settings).model_fields:
        value = getattr(settings, name)
        if isinstance(value, SecretStr):
            found.append(value.get_secret_value())
    found += _password(settings.database_url)
    if settings.publish_git_url:
        found += _token(settings.publish_git_url)
    return Secrets(tuple(value for value in found if value and len(value) >= SECRET_MIN))


def _password(url: str) -> list[str]:
    """The password of ``url`` as it is, URL-decoded and URL-encoded: an error can quote the
    address or the plain password.

    One address can give up to three different passwords. SQLAlchemy ends the password at the
    first "@". ``urlsplit`` ends the address at the first "/", "?" or "#", and the password at
    the last "@" before it. deploy/compose.yaml puts the password into the address as it is, so
    the raw password ends at the last "@" of the address. The engine connects with the password
    of SQLAlchemy, but ``_password`` returns each of the three, and ``secrets_of`` keeps each
    one of ``SECRET_MIN`` or more characters.

    SQLAlchemy puts the rest after the first "@" of a password into the host, the port, the
    database and the query, and an error can quote each of them. So the rest is a value too,
    and so is each part of it between two places: its start, its end, or a character of
    ``_PART_END``.

    The query of the address can also hold the password (``?password=``): libpq and SQLAlchemy
    read it. SQLAlchemy decodes it as a form, so "+" is a space.
    """
    found: list[str] = []
    for pattern in (_SQLALCHEMY_PASSWORD, _RAW_PASSWORD):
        match = pattern.match(url)
        if match:
            found.append(match.group(1))
    try:
        password = urlsplit(url).password
    except ValueError:  # an address that urlsplit cannot read has no password for it
        password = None
    if password:
        found.append(password)
    for whole in tuple(found):
        if "@" in whole:
            found += _parts(whole.partition("@")[2])
    forms = [form for raw in found for form in _forms(raw)]
    for raw in _QUERY_PASSWORD.findall(url):
        plain = unquote_plus(raw)
        forms += [raw, unquote(raw), plain, _encoded(plain), quote_plus(plain, errors=_BYTE)]
    return forms


def _parts(rest: str) -> list[str]:
    """Each part of ``rest`` from its start or a character of ``_PART_END`` to its end or a
    later such character, with at most ``_PART_HOLDS`` such characters in it. The whole
    ``rest`` is one of them."""
    ends = [end.start() for end in _PART_END.finditer(rest)]
    starts = [0, *(end + 1 for end in ends)]
    stops = [*ends, len(rest)]
    return [
        rest,
        *(
            rest[start:stop]
            for first, start in enumerate(starts)
            for stop in stops[first : first + _PART_HOLDS + 1]
            if start < stop
        ),
    ]


def _token(url: str) -> list[str]:
    """The token in the user part of ``url``: the password, read as for the database, and the
    user when there is no password. SQLAlchemy and ``urlsplit`` can read a different user part,
    so the user of each of the two is a value when that reader finds no password. A deploy key
    of ssh gives no token."""
    users: list[str] = []
    password_match = _SQLALCHEMY_PASSWORD.match(url)
    user_match = _USER.match(url)
    if user_match and not (password_match and password_match.group(1)):
        users.append(user_match.group(1))
    try:
        parts = urlsplit(url)
    except ValueError:  # an address that urlsplit cannot read has no user for it
        parts = None
    if parts is not None and parts.username and not parts.password:
        users.append(parts.username)
    return _password(url) + [form for user in users for form in _forms(user)]


def _forms(raw: str) -> list[str]:
    """``raw`` as it is, URL-decoded and URL-encoded. SQLAlchemy decodes a query as a form, so
    the decoded forms read "+" as a "+" and as a space."""
    plain = unquote(raw)
    return [raw, plain, unquote_plus(raw), _encoded(plain)]


def _encoded(plain: str) -> str:
    """``plain`` URL-encoded, each "/" too."""
    return quote(plain, safe="", errors=_BYTE)


def hide_secrets(text: str, secrets: Secrets, *, limit: int | None = ERROR_CHARS) -> str:
    """``text`` with no secret in it.

    With a ``limit`` (an error text), the result is one line of at most ``limit`` characters.
    A ``limit`` below 1 raises ``ValueError``. With None (a message), the result keeps its line
    breaks, and it is not cut. A second call changes nothing.
    """
    if limit is not None and limit < 1:
        msg = f"limit must be at least 1, not {limit}"
        raise ValueError(msg)
    text = _hide(text, secrets.values)
    text = _hide(_clean(text, one_line=limit is not None), secrets.values)
    if limit is not None and len(text) > limit:
        text = text[: limit - 1] + CUT
    return text


def _hide(text: str, values: tuple[str, ...]) -> str:
    """Steps 1 to 3: values, cut ends, patterns."""
    text = _replace_marked(text, _values_at(text, values) | _cut_ends_at(text, values))
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _values_at(text: str, values: Iterable[str]) -> set[int]:
    """The index of each character of each place of each value, also where two places overlap."""
    marked: set[int] = set()
    for value in values:
        at = text.find(value)
        while at != -1:
            marked.update(range(at, at + len(value)))
            at = text.find(value, at + 1)
    return marked


def _cut_ends_at(text: str, values: tuple[str, ...]) -> set[int]:
    """The index of each character of the longest first part of a value at the end of ``text``,
    and of the longest last part of a value at its start. The rule looks at each end with and
    without its white space, because a line break can come after a cut."""
    marked: set[int] = set()
    for stop in {len(text), len(text.rstrip())}:
        end = max(
            (k for v in values for k in _part_sizes(v) if text.endswith(v[:k], 0, stop)),
            default=0,
        )
        marked.update(range(stop - end, stop))
    for skip in {0, len(text) - len(text.lstrip())}:
        start = max(
            (k for v in values for k in _part_sizes(v) if text.startswith(v[-k:], skip)),
            default=0,
        )
        marked.update(range(skip, skip + start))
    return marked


def _part_sizes(value: str) -> range:
    """The sizes of a part of ``value`` that a cut can leave: not the whole value, and not a
    part so short that a normal word can be the same."""
    return range(SECRET_PART_MIN, len(value))


def _replace_marked(text: str, marked: set[int]) -> str:
    """``text`` with one ``HIDDEN`` for each run of marked characters."""
    kept: list[str] = []
    for at, char in enumerate(text):
        if at not in marked:
            kept.append(char)
        elif at - 1 not in marked:
            kept.append(HIDDEN)
    return "".join(kept)


def _clean(text: str, *, one_line: bool) -> str:
    r"""Step 4: no character that a person cannot see. One line: one space for each run of white
    space; a tab or a line break is in such a run. Many lines: a "\n" stays."""
    if one_line:
        kept = "".join(c for c in text if c.isspace() or not _invisible(c))
        return _WHITE_SPACE.sub(" ", kept)
    return "".join(c for c in text if c == "\n" or not _invisible(c))


def _invisible(char: str) -> bool:
    """A control character or a format character: a person cannot see it."""
    return unicodedata.category(char) in _INVISIBLE

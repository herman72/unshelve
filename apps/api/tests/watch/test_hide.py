"""No secret leaves the watcher: hide_secrets and secrets_of (docs/learn/55, part 3)."""

import re
import string
import typing
import unicodedata
from dataclasses import MISSING, fields, replace
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import SecretStr

from unshelve.config import Settings
from unshelve.watch.hide import Secrets, hide_secrets, scrub, secrets_of
from unshelve.watch.limits import ERROR_CHARS, SECRET_MIN, SECRET_PART_MIN
from unshelve.watch.observations import (
    BackupVerdict,
    Budget,
    DatabaseView,
    DeadJobs,
    Liveness,
    Observation,
    Queue,
    Refusals,
)

# Made up for the tests: 40 upper case letters and digits. No 4 of its characters in a row are
# in "<secret>", so a test can look for each part of it in a text that holds "<secret>".
SECRET = "MADE0UP0KEY0FOR0THE0WATCH0TESTS0ABCDEFGH"  # noqa: S105 - made up for the tests
# The made-up token of tests/net/test_telegram_files.py.
TOKEN = "123456789:MADE-UP-token_for_the_tests-only"  # noqa: S105 - made up for the tests


def _parts(value: str, size: int) -> list[str]:
    """Each part of ``value`` that has ``size`` characters."""
    return [value[at : at + size] for at in range(len(value) - size + 1)]


# --- Examples ----------------------------------------------------------------------------


def test_the_limits_of_a_text() -> None:
    # An error text has at most 200 characters. A value is a secret from 12 characters, and a
    # part of a value that a cut left is hidden from 8 characters (issue #154).
    assert (ERROR_CHARS, SECRET_MIN, SECRET_PART_MIN) == (200, 12, 8)


def test_the_default_password_is_not_a_secret() -> None:
    # "unshelve" has 8 characters, fewer than SECRET_MIN. Many fix texts hold the name of the
    # command, so a hidden "unshelve" would break such a fix.
    settings = Settings(database_url="postgresql+psycopg://unshelve:unshelve@db:5432/unshelve")
    text = "Fix: put the new key into .env, then run docker compose exec worker unshelve llm check"

    secrets = secrets_of(settings, {})

    assert "unshelve" not in secrets.values
    assert hide_secrets(text, secrets, limit=None) == text


@pytest.mark.parametrize(
    ("password", "expected"),
    [
        pytest.param("ABCDEFGH1234", "password <secret> refused", id="12 characters: a secret"),
        pytest.param("ABCDEFGH123", "password ABCDEFGH123 refused", id="11 characters: not one"),
    ],
)
def test_a_value_is_a_secret_from_secret_min_characters(password: str, expected: str) -> None:
    assert len("ABCDEFGH1234") == SECRET_MIN
    settings = Settings(database_url=f"postgresql+psycopg://unshelve:{password}@db:5432/unshelve")

    secrets = secrets_of(settings, {})

    assert hide_secrets(f"password {password} refused", secrets) == expected


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        pytest.param("ABCDEFGH1234", Secrets(("ABCDEFGH1234",)), id="12 characters: a secret"),
        # A placeholder in a .env for development would hide the command in each fix text that
        # names it.
        pytest.param("unshelve", Secrets(), id="8 characters: not one"),
    ],
)
def test_the_key_of_anthropic_is_a_secret_from_secret_min_characters(
    key: str, expected: Secrets
) -> None:
    settings = Settings(database_url="postgresql+psycopg://unshelve@db:5432/unshelve")

    assert secrets_of(settings, {"ANTHROPIC_API_KEY": key}) == expected


def test_the_values_are_the_longest_first_then_by_text_each_once() -> None:
    values = ("A" * 12, "B" * 20, "C" * 12, "A" * 12, "")

    assert Secrets(values).values == ("B" * 20, "A" * 12, "C" * 12)
    assert Secrets(values) == Secrets(tuple(reversed(values)))
    # The text from its first character: "AB..." comes before "BA...".
    assert Secrets(("BA" * 6, "AB" * 6)).values == ("AB" * 6, "BA" * 6)


def test_values_that_differ_only_in_case_are_in_code_point_order() -> None:
    # "K" (75) comes before "k" (107). A sort that ignores case keeps the order of the set, and
    # the hash seed of each run changes that order. With 20 such pairs, such a sort fails in
    # nearly each run, not only in half of the runs.
    sizes = range(SECRET_MIN + 19, SECRET_MIN - 1, -1)  # the longest first
    expected = tuple(value for size in sizes for value in ("K" * size, "k" * size))

    assert Secrets(tuple(reversed(expected))).values == expected


def test_the_repr_of_secrets_shows_no_value() -> None:
    # As "**********" for a SecretStr: a log line or an error that shows the Secrets shows no
    # secret.
    secrets = Secrets((SECRET, "B" * 12))

    assert repr(secrets) == str(secrets) == "Secrets(2 hidden)"


def test_a_bot_token_in_an_address_is_hidden_with_no_values() -> None:
    # A lookbehind, not \b: the "t" of "bot" and the "1" are both word characters.
    address = f"https://api.telegram.org/bot{TOKEN}/getMe"

    assert hide_secrets(address, Secrets()) == "https://api.telegram.org/bot<bot token>/getMe"


def test_the_cut_comes_last_so_it_leaves_no_part_of_a_secret() -> None:
    text = "x" * 195 + SECRET + " and the rest of the error"

    hidden = hide_secrets(text, Secrets((SECRET,)), limit=200)

    assert hidden == "x" * 195 + "<sec…"
    assert [part for part in _parts(SECRET, 4) if part in hidden] == []


@pytest.mark.parametrize("size", [8, 9, len(SECRET) - 1])
def test_the_first_part_of_a_secret_at_the_end_of_a_cut_text_is_hidden(size: int) -> None:
    # The worker keeps the first 2000 characters of an error, an event the first 300. The cut
    # can end in the first part of a secret.
    text = "ProviderError: voyage 401: a person must fix it: the key " + SECRET[:size]

    hidden = hide_secrets(text, Secrets((SECRET,)))

    assert hidden == "ProviderError: voyage 401: a person must fix it: the key <secret>"


@pytest.mark.parametrize("size", [8, 9, len(SECRET) - 1])
def test_the_last_part_of_a_secret_at_the_start_of_a_text_is_hidden(size: int) -> None:
    text = SECRET[-size:] + " was refused"

    assert hide_secrets(text, Secrets((SECRET,))) == "<secret> was refused"


def test_a_part_shorter_than_the_limit_of_a_part_stays() -> None:
    # Seven characters at an end can be a part of a secret, or a normal word.
    text = f"{SECRET[-7:]} and the key {SECRET[:7]}"

    assert hide_secrets(text, Secrets((SECRET,))) == text


@pytest.mark.parametrize(
    ("values", "text"),
    [
        # Each value alone leaves a part of the other: "GHIJKL" is in both.
        pytest.param(
            ("ABCDEFGHIJKL", "GHIJKLMNOPQR"), "key ABCDEFGHIJKLMNOPQR end", id="two values"
        ),
        # 13 "A" hold two places of 12 "A", one character apart.
        pytest.param(("A" * 12,), "key " + "A" * 13 + " end", id="one value, 1 character apart"),
    ],
)
def test_each_place_of_a_value_is_hidden_also_where_two_places_overlap(
    values: tuple[str, ...], text: str
) -> None:
    assert hide_secrets(text, Secrets(values)) == "key <secret> end"


def _secret_fields(model: type[Settings]) -> dict[str, str]:
    """The name in the environment of each SecretStr field of ``model``."""
    names: dict[str, str] = {}
    for name, field in model.model_fields.items():
        if field.annotation is SecretStr or SecretStr in typing.get_args(field.annotation):
            alias = field.validation_alias
            names[name] = alias if isinstance(alias, str) else f"UNSHELVE_{name.upper()}"
    return names


def test_each_secret_field_of_the_settings_is_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    fields = _secret_fields(Settings)
    assert {"telegram_bot_token", "voyage_api_key", "publish_s3_secret_access_key"} <= set(fields)
    values: list[str] = []
    for name, environment_name in sorted(fields.items()):
        values.append(f"made-up-{name}-value")
        monkeypatch.setenv(environment_name, values[-1])
    anthropic = "made-up-anthropic-key-0123"  # not a setting: services.py reads it

    secrets = secrets_of(Settings(), {"ANTHROPIC_API_KEY": anthropic})

    hidden = hide_secrets(" ".join([*values, anthropic]), secrets, limit=None)
    assert hidden == " ".join(["<secret>"] * (len(values) + 1))


class _MadeUpSecretStr(SecretStr):
    """A subclass of SecretStr. Its value is a SecretStr too."""


class _MoreSettings(Settings):
    """Settings with three more secrets, as a later field such as the address of a ping: one
    that can be None, one that cannot, and one of a subclass of SecretStr."""

    made_up_secret: SecretStr | None = None
    another_secret: SecretStr = SecretStr("another-made-up-secret-value")
    subclass_secret: _MadeUpSecretStr = _MadeUpSecretStr("a-made-up-secret-of-a-subclass")


def test_a_new_secret_field_is_hidden_with_no_change_in_hide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UNSHELVE_MADE_UP_SECRET", "a-made-up-secret-of-a-new-field")

    secrets = secrets_of(_MoreSettings(), {})

    hidden = hide_secrets(
        "ping a-made-up-secret-of-a-new-field failed, another-made-up-secret-value too, "
        "a-made-up-secret-of-a-subclass too",
        secrets,
    )
    assert hidden == "ping <secret> failed, <secret> too, <secret> too"


def test_the_password_of_the_database_is_hidden() -> None:
    url = "postgresql+psycopg://unshelve:Abcdefgh12345@db:5432/unshelve"

    secrets = secrets_of(Settings(database_url=url), {})

    assert hide_secrets(url, secrets) == "postgresql+psycopg://<secret>@db:5432/unshelve"
    assert hide_secrets("the password Abcdefgh12345 failed", secrets) == (
        "the password <secret> failed"
    )


@pytest.mark.parametrize(
    ("password", "text", "expected"),
    [
        # In an address, "@" is "%40". An error can quote the address or the plain password.
        pytest.param(
            "p%40ss-word-longer",
            "password failed: p@ss-word-longer or p%40ss-word-longer",
            "password failed: <secret> or <secret>",
            id="decoded",
        ),
        # The address holds "!" as it is. An error can quote the encoded form, "%21".
        pytest.param(
            "pa!ss-word-longer",
            "password pa%21ss-word-longer refused",
            "password <secret> refused",
            id="encoded",
        ),
        # In an address, "+" is a "+", not a space as in a form. Its encoded form is "%2B".
        pytest.param(
            "pass+word-longer",
            "password pass%2Bword-longer refused",
            "password <secret> refused",
            id="encoded with a plus",
        ),
        # The encoded form of a space is "%20", not "+".
        pytest.param(
            "pass word-longer",
            "password pass%20word-longer refused",
            "password <secret> refused",
            id="encoded with a space",
        ),
        # The encoded form comes from the decoded form, so "%2D" becomes "-" in it.
        pytest.param(
            "p%40ss%2Dword-longer",
            "password p%40ss-word-longer refused",
            "password <secret> refused",
            id="encoded again from the decoded form",
        ),
        # The encoded form encodes each "/" too: "%2F".
        pytest.param(
            "pass/word-longer1",
            "password pass%2Fword-longer1 refused",
            "password <secret> refused",
            id="encoded with a slash",
        ),
        # "%2D" is a "-" that the address encodes, but quote() does not: only the raw form has it.
        pytest.param(
            "p%40ss%2Dword-longer",
            "password p%40ss%2Dword-longer refused",
            "password <secret> refused",
            id="as it is",
        ),
        # urlsplit ends the password at the "/", but SQLAlchemy connects with all of it.
        pytest.param(
            "pass/word-longer1",
            "connect to postgresql+psycopg://unshelve:pass/word-longer1@db:5432/unshelve: "
            "password pass/word-longer1 was refused",
            "connect to postgresql+psycopg://<secret>@db:5432/unshelve: "
            "password <secret> was refused",
            id="as SQLAlchemy reads it",
        ),
        # urlsplit reads only "p%40ss", which is too short. SQLAlchemy reads all of it, and the
        # address holds it as it is, not decoded.
        pytest.param(
            "p%40ss/word-longer1",
            "connect to postgresql+psycopg://unshelve:p%40ss/word-longer1@db:5432/unshelve failed",
            "connect to postgresql+psycopg://<secret>@db:5432/unshelve failed",
            id="as it is, as SQLAlchemy reads it",
        ),
        # SQLAlchemy ends the password at the first "@", so it reads only "p". urlsplit ends the
        # user part at the last "@", so it reads the whole password.
        pytest.param(
            "p@ss-word-longer",
            "password p@ss-word-longer refused",
            "password <secret> refused",
            id="as urlsplit reads it",
        ),
        # The engine connects with the part before the first "@", so the database refuses it.
        pytest.param(
            "Abcdefgh12345@xyzw",
            "password Abcdefgh12345 refused",
            "password <secret> refused",
            id="SQLAlchemy ends the password at the first @",
        ),
        # SQLAlchemy puts the rest after the first "@" into the host, and the connection error
        # quotes the host.
        pytest.param(
            "Abcdefgh12345@xyzxyzxyzxyz",
            'could not translate host name "xyzxyzxyzxyz@db" to address',
            'could not translate host name "<secret>@db" to address',
            id="the rest that SQLAlchemy puts into the host",
        ),
        pytest.param(
            "Abcdefgh12345@middlemiddle12@endendendend",
            "the part middlemiddle12 of the host",
            "the part <secret> of the host",
            id="a part between two @",
        ),
    ],
)
def test_each_form_of_the_password_is_hidden(password: str, text: str, expected: str) -> None:
    url = f"postgresql+psycopg://unshelve:{password}@db:5432/unshelve"

    secrets = secrets_of(Settings(database_url=url), {})

    assert hide_secrets(text, secrets) == expected


# The text of psycopg when it cannot find a host. SQLAlchemy puts the part of the password after
# its first raw "@" into the host. Each example gives the host that SQLAlchemy reads.
_NO_HOST = (
    "OperationalError: (psycopg.OperationalError) failed to resolve host '{}': "
    "[Errno -2] Name or service not known"
)


@pytest.mark.parametrize(
    ("password", "text", "expected"),
    [
        # urlsplit ends the address at the first "#", "/" or "?", so it reads no "@" in the
        # password. deploy/compose.yaml puts the password into the address as it is.
        pytest.param(
            "Abcd#efgh@ijklmnopqrstu",
            _NO_HOST.format("ijklmnopqrstu@db"),
            _NO_HOST.format("<secret>@db"),
            id="a # before the @",
        ),
        pytest.param(
            "Abcd/efgh@ijklmnopqrstu",
            _NO_HOST.format("ijklmnopqrstu@db"),
            _NO_HOST.format("<secret>@db"),
            id="a / before the @",
        ),
        pytest.param(
            "Abcd?efgh@ijklmnopqrstu",
            _NO_HOST.format("ijklmnopqrstu@db"),
            _NO_HOST.format("<secret>@db"),
            id="a ? before the @",
        ),
        # The password of SQLAlchemy, Q7#hk2, has 6 characters, so it is not a value. The raw
        # password and the rest after the "@" are values.
        pytest.param(
            "Q7#hk2@Lm9vX4pZr8TnW",
            _NO_HOST.format("Lm9vX4pZr8TnW@db"),
            _NO_HOST.format("<secret>@db"),
            id="a generated password with symbols",
        ),
        pytest.param(
            "Abc/defgh12345@xyzxyzxyzxyz",
            'could not translate host name "xyzxyzxyzxyz@db" to address',
            'could not translate host name "<secret>@db" to address',
            id="a / before the @, with a long first part",
        ),
        # SQLAlchemy ends the host at ":" and reads the rest as the port.
        pytest.param(
            "Xk9@pQ:2vLz8mNw4RtYY",
            "ValueError: invalid literal for int() with base 10: '2vLz8mNw4RtYY@db:5432'",
            "ValueError: invalid literal for int() with base 10: '<secret>@db:5432'",
            id="an @ then a colon: SQLAlchemy reads a port",
        ),
        # SQLAlchemy ends the host at "/" and reads the rest as the database.
        pytest.param(
            "Abc@defgh/ijklmnopqrstu",
            'database "ijklmnopqrstu@db:5432/unshelve" does not exist',
            'database "<secret>@db:5432/unshelve" does not exist',
            id="an @ then a slash: SQLAlchemy reads a database",
        ),
        # SQLAlchemy ends the host at "?" and reads the rest as the query. The query
        # "x@db:5432/unshelve" has no "=", so it gives no key, and the host has no "@db".
        pytest.param(
            "Abc@ijklmnopqrstu?x",
            _NO_HOST.format("ijklmnopqrstu"),
            _NO_HOST.format("<secret>"),
            id="an @ then a ?: SQLAlchemy reads a query",
        ),
        # SQLAlchemy reads a host in "[" and "]" as an IPv6 host, with no brackets.
        pytest.param(
            "Abc@[ijklmnopqrstu]",
            _NO_HOST.format("ijklmnopqrstu"),
            _NO_HOST.format("<secret>"),
            id="an @ then [ ]: SQLAlchemy reads an IPv6 host",
        ),
        # Each part between two ":" has 4 characters. The host is the part from "[" to "]".
        pytest.param(
            "Abc@[ijkl:mnop:qrst]",
            _NO_HOST.format("ijkl:mnop:qrst"),
            _NO_HOST.format("<secret>"),
            id="an IPv6 host with colons",
        ),
        # Each part between two "@" has fewer than 12 characters. The whole rest is a value.
        pytest.param(
            "Abc@defg@hijklmnop",
            _NO_HOST.format("defg@hijklmnop@db"),
            _NO_HOST.format("<secret>@db"),
            id="each part of the rest is short",
        ),
    ],
)
def test_each_part_that_sqlalchemy_reads_as_host_port_or_database_is_hidden(
    password: str, text: str, expected: str
) -> None:
    url = f"postgresql+psycopg://unshelve:{password}@db:5432/unshelve"

    secrets = secrets_of(Settings(database_url=url), {})

    assert hide_secrets(text, secrets) == expected


def test_a_password_with_many_ends_of_parts_gives_values_in_a_line_with_its_length() -> None:
    # 200 parts of 12 characters, with an "@" between each two. A part holds at most 8 of the
    # "@", so the values grow with the length of the password: 3,333 values. Each part from
    # one "@" to any later "@" gives about 40,000 values, and each check gets slower.
    rest = "@".join(f"p{part:011d}" for part in range(200))
    url = f"postgresql+psycopg://unshelve:Abcdefgh12345@{rest}@db:5432/unshelve"

    secrets = secrets_of(Settings(database_url=url), {})

    assert len(secrets.values) < 5_000
    assert hide_secrets(
        _NO_HOST.format("p00000000150@p00000000151@db"), secrets
    ) == _NO_HOST.format("<secret>@db")


@pytest.mark.parametrize(
    ("password", "text", "expected"),
    [
        # SQLAlchemy reads the text after the "?" as the query, and gives each key and value to
        # libpq. The value ends at the "@" of the address. libpq quotes a value that it cannot
        # use.
        pytest.param(
            "Abc@defgh?sslmode=ijklmnopqrstuv",
            'invalid sslmode value: "ijklmnopqrstuv@db:5432/unshelve"',
            'invalid sslmode value: "<secret>@db:5432/unshelve"',
            id="a value",
        ),
        # libpq quotes a key that it does not know.
        pytest.param(
            "Abc@defgh?Lm9vX4pZr8TnWq=1",
            'invalid connection option "Lm9vX4pZr8TnWq"',
            'invalid connection option "<secret>"',
            id="a key",
        ),
        pytest.param(
            "Abc@defgh?a=b&Lm9vX4pZr8TnWq=1",
            'invalid connection option "Lm9vX4pZr8TnWq"',
            'invalid connection option "<secret>"',
            id="a key after an &",
        ),
        # A value ends at the next "&", not at a second "=". Each part between two "=" has 6
        # characters.
        pytest.param(
            "Abc@defgh?sslmode=abcdef=ghijkl",
            'invalid sslmode value: "abcdef=ghijkl@db:5432/unshelve"',
            'invalid sslmode value: "<secret>@db:5432/unshelve"',
            id="a value with an =",
        ),
        # SQLAlchemy decodes a query as a form, so "+" is a space.
        pytest.param(
            "Abc@defgh?sslmode=ijklmn+opqrstuv",
            'invalid sslmode value: "ijklmn opqrstuv@db:5432/unshelve"',
            'invalid sslmode value: "<secret>@db:5432/unshelve"',
            id="a value with a +",
        ),
    ],
)
def test_each_part_that_sqlalchemy_reads_as_a_key_or_a_value_of_the_query_is_hidden(
    password: str, text: str, expected: str
) -> None:
    url = f"postgresql+psycopg://unshelve:{password}@db:5432/unshelve"

    secrets = secrets_of(Settings(database_url=url), {})

    assert hide_secrets(text, secrets) == expected


@pytest.mark.parametrize(
    ("query", "text", "expected"),
    [
        # The repr of a URL of SQLAlchemy hides only the password of the user part.
        pytest.param(
            "password=madeupqueryword",
            "cannot use postgresql+psycopg://unshelve@db:5432/unshelve?password=madeupqueryword",
            "cannot use postgresql+psycopg://<secret>@db:5432/unshelve?password=<secret>",
            id="as it is",
        ),
        # SQLAlchemy decodes a query as a form: "+" is a space. Its repr encodes the value
        # again, with "+" for a space.
        pytest.param(
            "sslmode=disable&password=queryword%2Bqueryword+2",
            "password 'queryword+queryword 2' refused, or queryword%2Bqueryword+2",
            "password '<secret>' refused, or <secret>",
            id="decoded as a form, then encoded again",
        ),
        # SQLAlchemy decodes "%20" as a space, and its repr encodes the space as "+".
        pytest.param(
            "password=plainwordplain%20x",
            "cannot use postgresql+psycopg://unshelve@db:5432/unshelve?password=plainwordplain+x",
            "cannot use postgresql+psycopg://<secret>@db:5432/unshelve?password=<secret>",
            id="encoded again as a form",
        ),
        # The address can write "+" as "%2b", in lower case. Only the raw form has it.
        pytest.param(
            "password=plainwordplain%2bx",
            "cannot use postgresql+psycopg://unshelve@db:5432/unshelve?"
            "password=plainwordplain%2bx now",
            "cannot use postgresql+psycopg://<secret>@db:5432/unshelve?password=<secret> now",
            id="as it is, in lower case",
        ),
    ],
)
def test_the_password_in_the_query_of_the_address_is_hidden(
    query: str, text: str, expected: str
) -> None:
    url = f"postgresql+psycopg://unshelve@db:5432/unshelve?{query}"

    secrets = secrets_of(Settings(database_url=url), {})

    assert hide_secrets(text, secrets) == expected


@pytest.mark.parametrize(
    ("url", "text", "expected"),
    [
        # A cut before the "@": the pattern of the user part needs the "@".
        pytest.param(
            "https://x-access-token:ghp_MADEUPtoken0123456789@github.com/o/s.git",
            "fatal: unable to access 'https://x-access-token:ghp_MADEUPtoken0",
            "fatal: unable to access 'https://x-access-token:<secret>",
            id="the password",
        ),
        pytest.param(
            "https://ghp_MADEUPtoken0123456789@github.com/o/s.git",
            "fatal: unable to access 'https://ghp_MADEUPtoken0",
            "fatal: unable to access 'https://<secret>",
            id="the user, with no password",
        ),
        # urlsplit ends the address at the first "/", "?" or "#", so it reads no password. The
        # token is read as for the database.
        pytest.param(
            "https://oauth2:abc/def123456789xyz@gitlab.com/o/r.git",
            "Could not resolve host: abc/def123456789xyz@gitlab.com",
            "Could not resolve host: <secret>@gitlab.com",
            id="a / in the token",
        ),
        pytest.param(
            "https://oauth2:abc?def123456789xyz@gitlab.com/o/r.git",
            "Could not resolve host: abc?def123456789xyz@gitlab.com",
            "Could not resolve host: <secret>@gitlab.com",
            id="a ? in the token",
        ),
        # The part after a raw "@" in the token can go into the host, as for the database.
        pytest.param(
            "https://oauth2:abc@def123456789xyz@gitlab.com/o/r.git",
            "Could not resolve host: def123456789xyz@gitlab.com",
            "Could not resolve host: <secret>@gitlab.com",
            id="an @ in the token",
        ),
        # git ends the address at the "#", so the host is the part between the "@" and the "#".
        # The text goes on after the host, so the rule of the cut ends does not hide it.
        pytest.param(
            "https://oauth2:abc@def123456789xyz#q@gitlab.com/o/r.git",
            "* Could not resolve host: def123456789xyz\n* Closing connection 0",
            "* Could not resolve host: <secret> * Closing connection 0",
            id="an @ and a # in the token",
        ),
        # urlsplit cannot read the host "[zz]", but the token is still a value.
        pytest.param(
            "https://x-access-token:ghp_MADEUPtoken0123456789@[zz]/o/s.git",
            "fatal: unable to access 'https://x-access-token:ghp_MADEUPtoken0",
            "fatal: unable to access 'https://x-access-token:<secret>",
            id="the password, with a host that urlsplit cannot read",
        ),
        pytest.param(
            "https://ghp_MADEUPtoken0123456789@[zz]/o/s.git",
            "fatal: unable to access 'https://ghp_MADEUPtoken0",
            "fatal: unable to access 'https://<secret>",
            id="the user, with a host that urlsplit cannot read",
        ),
        # An empty password is no password, so the user is the token.
        pytest.param(
            "https://ghp_MADEUPtoken0123456789:@github.com/o/s.git",
            "fatal: unable to access 'https://ghp_MADEUPtoken0",
            "fatal: unable to access 'https://<secret>",
            id="the user, with an empty password",
        ),
        # SQLAlchemy reads the password "ab#c". urlsplit ends the address at the "#", so it
        # reads no password, and the user is the token.
        pytest.param(
            "https://ghp_MADEUPtoken0123456789@gitlab.com:ab#c@x/o/r.git",
            "fatal: unable to access 'https://ghp_MADEUPtoken0",
            "fatal: unable to access 'https://<secret>",
            id="the user for urlsplit, a password for SQLAlchemy",
        ),
    ],
)
def test_the_token_in_the_address_of_the_git_target_is_hidden(
    url: str, text: str, expected: str
) -> None:
    secrets = secrets_of(Settings(publish_git_url=url), {})

    assert hide_secrets(text, secrets) == expected


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("git@github.com:o/s.git", id="a deploy key"),
        pytest.param("https://github.com/o/s.git", id="no user part"),
        pytest.param("https://[::1/o/s.git", id="urlsplit cannot read it"),
    ],
)
def test_an_address_of_the_git_target_with_no_token_gives_no_value(url: str) -> None:
    assert secrets_of(Settings(publish_git_url=url), {}) == Secrets()


def test_the_password_of_an_address_with_no_user_name_is_hidden() -> None:
    # SQLAlchemy reads the password also with an empty user name. urlsplit reads only "pass",
    # because the "/" ends the netloc for it.
    url = "postgresql+psycopg://:pass/word-longer1@db:5432/unshelve"

    secrets = secrets_of(Settings(database_url=url), {})

    assert hide_secrets("password pass/word-longer1 refused", secrets) == (
        "password <secret> refused"
    )


def test_an_address_that_urlsplit_cannot_read_gives_the_password_of_sqlalchemy() -> None:
    # urlsplit raises ValueError for the "[" with no "]". SQLAlchemy still reads the password,
    # and the password is a value as it is and decoded.
    url = "postgresql+psycopg://unshelve:p%40ss-word-longer@[db:5432/unshelve"

    expected = Secrets(("p%40ss-word-longer", "p@ss-word-longer"))
    assert secrets_of(Settings(database_url=url), {}) == expected


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("postgresql+psycopg://unshelve@db:5432/unshelve", id="no password"),
        pytest.param("postgresql+psycopg://u:p@[db:5432/unshelve", id="urlsplit cannot read it"),
    ],
)
def test_an_address_with_no_password_to_read_gives_no_value(url: str) -> None:
    assert secrets_of(Settings(database_url=url), {}) == Secrets()


_DATABASE_URL = "postgresql+psycopg://unshelve:unshelve@db:5432/unshelve"


@pytest.mark.parametrize(
    ("database_url", "publish_git_url"),
    [
        pytest.param(
            "postgresql+psycopg://unshelve:\udcffAbcdefgh12345@db:5432/unshelve",
            None,
            id="the password",
        ),
        pytest.param(
            "postgresql+psycopg://unshelve@db:5432/unshelve?password=\udcffAbcdefgh12345",
            None,
            id="the password in the query",
        ),
        pytest.param(
            _DATABASE_URL,
            "https://x-access-token:\udcffAbcdefgh12345@github.com/o/s.git",
            id="the token of the git target",
        ),
    ],
)
def test_a_byte_that_is_not_utf_8_is_encoded_as_that_byte(
    database_url: str, publish_git_url: str | None
) -> None:
    # os.environ decodes a byte that is not UTF-8, here 0xFF, as a lone surrogate. Strict UTF-8
    # cannot encode it, so the encoded form gives the byte back: "%FF".
    settings = Settings(database_url=database_url, publish_git_url=publish_git_url)

    secrets = secrets_of(settings, {})

    assert hide_secrets("password %FFAbcdefgh12345 refused", secrets) == (
        "password <secret> refused"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "claude refused sk-ant-api03-MADEUPmadeup0123456789 at 02:03",
            "claude refused <anthropic key> at 02:03",
            id="anthropic",
        ),
        # A key is base64url: "_" and "-" are in it.
        pytest.param(
            "claude refused sk-ant-api03-ABCDEFGH_ijklmnop0123456789 at 02:03",
            "claude refused <anthropic key> at 02:03",
            id="anthropic, with _",
        ),
        # A real key has about 100 characters after "sk-ant-". The pattern has no upper limit,
        # so no end of a long key stays.
        pytest.param(
            "claude refused sk-ant-api03-" + "Ab0_" * 23 + "x-AA at 02:03",
            "claude refused <anthropic key> at 02:03",
            id="anthropic, real length",
        ),
        # A "." is not in a key, so it stays after the label.
        pytest.param(
            "claude=sk-ant-MADEUP01.",
            "claude=<anthropic key>.",
            id="anthropic, a dot after it",
        ),
        # In a repr, an escaped line break puts the letter "n" just before the key.
        pytest.param(
            "config='a\\nsk-ant-api03-MADEUPmadeup0123456789'",
            "config='a\\n<anthropic key>'",
            id="anthropic, after a letter",
        ),
        pytest.param(f"token {TOKEN} refused", "token <bot token> refused", id="bot token"),
        pytest.param(
            "token 12345:" + "A" * 30 + " refused",
            "token <bot token> refused",
            id="bot token, 5 digits and 30 characters",
        ),
        # A new bot has an id of 10 digits.
        pytest.param(
            "token 7123456789:" + "A" * 35 + " refused",
            "token <bot token> refused",
            id="bot token, 10 digits",
        ),
        pytest.param(
            "token 123456789012:" + "A" * 35 + " refused",
            "token <bot token> refused",
            id="bot token, 12 digits",
        ),
        # The secret part has no upper limit: a longer token of a later format is hidden too.
        pytest.param(
            "token 7123456789:" + "A" * 40 + " refused",
            "token <bot token> refused",
            id="bot token, 40 characters",
        ),
        # Only a digit before the token stops the pattern, not a ":".
        pytest.param(
            f"token:{TOKEN} refused",
            "token:<bot token> refused",
            id="bot token, after a colon",
        ),
        pytest.param(
            "voyage refused pa-MADEUPmadeup0123456789ab.",
            "voyage refused <voyage key>.",
            id="voyage",
        ),
        pytest.param(
            "voyage refused pa-MADEUP_madeup-0123456789ab.",
            "voyage refused <voyage key>.",
            id="voyage, with _ and -",
        ),
        # "_" is not a letter or a digit, so the key starts just after it.
        pytest.param(
            "voyage_pa-MADEUPmadeup0123456789ab refused",
            "voyage_<voyage key> refused",
            id="voyage, after _",
        ),
        pytest.param(
            "voyage refused pa-" + "A" * 20 + ".",
            "voyage refused <voyage key>.",
            id="voyage, 20 characters",
        ),
        pytest.param(
            "voyage refused pa-" + "Ab0_" * 10 + "xyz.",
            "voyage refused <voyage key>.",
            id="voyage, real length",
        ),
        # Only a letter or a digit before the key stops the pattern, not a "=".
        pytest.param(
            "env VOYAGE_API_KEY=pa-MADEUPmadeup0123456789ab set",
            "env VOYAGE_API_KEY=<voyage key> set",
            id="voyage, in an environment line",
        ),
        pytest.param(
            "atlas refused al-MADEUPmadeup0123456789ab.",
            "atlas refused <voyage key>.",
            id="atlas",
        ),
        pytest.param(
            "Authorization: bearer made.up.token-0123",
            "Authorization: Bearer <secret>",
            id="bearer",
        ),
        # The name of an HTTP authentication scheme ignores case.
        pytest.param(
            "Authorization: BEARER made.up.token-0123",
            "Authorization: Bearer <secret>",
            id="bearer, upper case",
        ),
        # The whole token, also a short one.
        pytest.param(
            "Authorization: Bearer abc1234",
            "Authorization: Bearer <secret>",
            id="bearer, a short token",
        ),
        pytest.param(
            "Authorization: Bearer <MADEUPTOKEN0123>",
            "Authorization: Bearer <secret>",
            id="bearer, a token in angle brackets",
        ),
        pytest.param(
            "Authorization: Bearer sk-ant-api03-MADEUPmadeup0123456789",
            "Authorization: Bearer <secret>",
            id="bearer, a key of Anthropic",
        ),
        pytest.param(
            "claude=sk-ant-MADEUP01",
            "claude=<anthropic key>",
            id="anthropic, 8 characters",
        ),
        pytest.param(
            "git push https://user:tok@example.invalid/feed.git failed",
            "git push https://<secret>@example.invalid/feed.git failed",
            id="user part",
        ),
        pytest.param(
            "git push https://made-up-token@example.invalid/feed.git failed",
            "git push https://<secret>@example.invalid/feed.git failed",
            id="user with no password",
        ),
        pytest.param(
            "ping https://hc-ping.com/0f1e2d3c-made-up failed",
            "ping <ping url> failed",
            id="ping",
        ),
        # The whole address, also a path after the key, such as /fail.
        pytest.param(
            "ping https://hc-ping.com/0f1e2d3c-made-up/fail failed",
            "ping <ping url> failed",
            id="ping, with a path after the key",
        ),
        # An error of httpx quotes the address as the request had it, also with http.
        pytest.param(
            "ping http://hc-ping.com/0f1e2d3c-made-up failed",
            "ping <ping url> failed",
            id="ping, http",
        ),
        # The ping comes before the keys, so it takes a key in its path whole, with no part of
        # a label after it.
        pytest.param(
            "ping https://hc-ping.com/pa-MADEUPmadeup0123456789ab failed",
            "ping <ping url> failed",
            id="ping, a key in its path",
        ),
    ],
)
def test_the_shape_of_a_key_that_the_watcher_does_not_hold_is_hidden(
    text: str, expected: str
) -> None:
    assert hide_secrets(text, Secrets()) == expected


def test_the_whole_token_after_bearer_is_hidden() -> None:
    # The watcher holds only the first part of the token. The rest is a secret too.
    text = f"Authorization: Bearer {SECRET}.rest-of-the-token-0123"

    assert hide_secrets(text, Secrets((SECRET,))) == "Authorization: Bearer <secret>"


@pytest.mark.parametrize(
    "line_break",
    [
        pytest.param("\n", id="LF"),
        pytest.param("\r", id="CR"),
        pytest.param("\x0b", id="VT"),
        pytest.param("\x0c", id="FF"),
        pytest.param("\x85", id="NEL"),
        pytest.param("\u2028", id="LS"),
    ],
)
def test_a_line_break_after_bearer_hides_the_token_of_a_message(line_break: str) -> None:
    # Each one breaks a line for str.splitlines. A message keeps "\n" and U+2028, and the clean
    # step removes the others: then "Bearer" and the token touch, and only the first run of the
    # steps finds the token. Only \s, not ASCII white space, matches U+2028.
    text = f"Authorization: Bearer{line_break}made.up.token"

    assert hide_secrets(text, Secrets(), limit=None) == "Authorization: Bearer <secret>"


@pytest.mark.parametrize("limit", [ERROR_CHARS, None])
@pytest.mark.parametrize("invisible", ["\x00", "​"])
@pytest.mark.parametrize(
    "key",
    [
        pytest.param("sk-ant-api03-MADEUPmadeup0123456789", id="anthropic"),
        pytest.param(TOKEN, id="bot token"),
        pytest.param("pa-MADEUPmadeup0123456789ab", id="voyage"),
        pytest.param("https://hc-ping.com/0f1e2d3c-made-up", id="ping"),
    ],
)
def test_a_key_after_bearer_and_an_invisible_character_is_hidden_whole(
    key: str, invisible: str, limit: int | None
) -> None:
    # The invisible character stops "bearer" in the first run of the steps, so a pattern hides
    # the key with a label. The clean step joins "bearer" and the label. Then the second run
    # hides the whole label, not only its first word.
    text = f"auth: bearer{invisible} {key}"

    assert hide_secrets(text, Secrets(), limit=limit) == "auth: Bearer <secret>"


@pytest.mark.parametrize("limit", range(3, 23))
def test_a_cut_after_bearer_and_a_label_stays_after_a_second_call(limit: int) -> None:
    # The first call gives "x Bearer <secret> more". Each limit from 3 to 21 cuts at another
    # place: before "Bearer", in it, after it, in "<secret>" or after it. At 22, nothing is cut.
    once = hide_secrets("x bearer\x00 pa-MADEUPmadeup0123456789ab more", Secrets(), limit=limit)

    assert hide_secrets(once, Secrets(), limit=limit) == once


def test_a_label_and_a_hidden_part_after_bearer_are_one_token() -> None:
    # The clean step joins "BE" and "ARER", so only the second run of the steps finds "Bearer".
    # The token after it is a label and a hidden part. A token of only the label would leave the
    # "<secret>", and a second call would then take it.
    text = f"x BE\x1fARER sk-ant-api03-MADEUPmadeup0123{SECRET[:9]}"

    once = hide_secrets(text, Secrets((SECRET,)), limit=None)

    assert once == "x Bearer <secret>"
    assert hide_secrets(once, Secrets((SECRET,)), limit=None) == once


@pytest.mark.parametrize(
    "text",
    [
        # A word that ends in "al-" or "pa-" is not a key of Voyage: the lookbehind. A digit is
        # in a word too.
        "fetch: internal-server-error-from-the-upstream",
        "manual-retry_of_the_import_job_ok",
        "model v2pa-abcdefghijklmnopqrstu",
        # One digit before ":" is not a bot token: 5 to 12 digits.
        "line 3:abcdefghijklmnopqrstuvwxyz_0123456",
        # 4 digits, or 29 characters after the ":", are not a bot token either.
        "line 1234:" + "a" * 30,
        "line 12345:" + "a" * 29,
        # 13 digits, such as a time in milliseconds, are not a bot token: no digit comes before
        # the 5 to 12 digits of a token.
        "id 1795392190123:" + "a" * 30,
        # A bot token needs the ":". A hash with 5 or more digits in a row is not one.
        "git push: commit 1234567fedcba9876543210fedcba9876543210f refused",
        "the dump has sha256 3141592653589793238462643383279502884197169399375105820974944592",
        # 19 characters after "pa-" are not a key of Voyage: 20 or more.
        "pa-" + "a" * 19,
        # "bearer" inside a word is not a header: \b before it, and white space after it.
        "the torchbearer of the release",
        "the bearers of the release",
        # An empty token stays: it shows that a key is missing.
        "Authorization: Bearer ",
        # An "@" after the first "/" is in the path, not in the user part.
        "fetch https://medium.com/@made-up-author/a-post failed: 404",
        # An empty user part holds no secret.
        "fetch https://@example.invalid/feed.git failed",
        "run docker compose exec worker unshelve llm check",
        "run docker compose logs --tail 50 worker",
        "the backup on 2026-11-24 at 02:05 failed",
    ],
)
def test_a_normal_text_stays_as_it_is(text: str) -> None:
    assert hide_secrets(text, Secrets()) == text
    assert hide_secrets(text, Secrets(), limit=None) == text


def test_a_cut_just_after_the_address_of_a_ping_stays_after_a_second_call() -> None:
    once = hide_secrets("https://hc-ping.com/ abc", Secrets(), limit=21)

    assert once == "https://hc-ping.com/…"
    assert hide_secrets(once, Secrets(), limit=21) == once


@pytest.mark.parametrize("limit", range(9, 23))
def test_a_cut_in_the_secret_after_bearer_stays_after_a_second_call(limit: int) -> None:
    # "x Bearer <secret> more": each limit cuts at another place, also in "<secret>". The token
    # ends before the "…", so a second call cannot make "<secre" whole again.
    once = hide_secrets("x Bearer made.up.token more", Secrets(), limit=limit)

    assert hide_secrets(once, Secrets(), limit=limit) == once


def test_an_error_text_is_one_clean_line_and_a_message_keeps_its_lines() -> None:
    text = "line 1\n\tline\x002\r\n\nline 3"

    assert hide_secrets(text, Secrets()) == "line 1 line2 line 3"
    assert hide_secrets(text, Secrets(), limit=None) == "line 1\nline2\n\nline 3"


def _hidden_and_cut(text: str) -> str:
    """``text`` with SECRET hidden, cut to 200 characters."""
    hidden = text.replace(SECRET, "<secret>")
    return hidden if len(hidden) <= 200 else hidden[:199] + "…"


def test_scrub_hides_each_error_text_of_an_observation() -> None:
    # The secret is in the first 200 characters, so only the hide can remove it, not the cut.
    error = f"ProviderError: voyage 401: a person must fix it: key {SECRET} " + "z" * 300
    # Each other field has a value that is not its default, and model and voyage differ. So a
    # scrub that drops a field, or takes the field of another part, gives another observation.
    model = Refusals(
        newest_failure_at=1795392190.0,
        newest_success_at=1795391400.0,
        error=f"model: {error}",
        waiting=(("enrich_item", 2),),
        next_try_at=1795392310.0,
        last_try_at=1795395790.0,
        gave_up=(("enrich_failed", 1),),
    )
    voyage = Refusals(
        newest_failure_at=1795392250.0,
        newest_success_at=1795390000.0,
        error=f"embed: {error}",
        waiting=(("embed_item", 3),),
        next_try_at=1795392370.0,
        last_try_at=1795394170.0,
        gave_up=(("embed_failed", 4),),
    )
    dead = DeadJobs(totals=(("publish_feed", 2),), newest_kind="publish_feed", newest_error=error)
    database = DatabaseView(
        queue=Queue(ready=1, oldest_ready_at=1795391000.0, newest_attempt_end_at=1795391900.0),
        dead=dead,
        model=model,
        voyage=voyage,
        budget=Budget(spent_usd=Decimal("1.0021"), budget_usd=Decimal("1.00"), waiting=3),
    )
    backup = BackupVerdict(ok=False, text=error, at=1795392000.0)
    seen = Observation(
        worker=Liveness(mtime=1795392000.0, started=1795390000, stopped=1795391990),
        bot=Liveness(mtime=1795391990.0, started=1795390010, chats_ready=(0, 1)),
        database=database,
        database_error=error,
        web_ok=False,
        backup=backup,
    )

    clean = scrub(seen, Secrets((SECRET,)))

    assert clean == replace(
        seen,
        database=replace(
            database,
            dead=replace(dead, newest_error=_hidden_and_cut(error)),
            model=replace(model, error=_hidden_and_cut(f"model: {error}")),
            voyage=replace(voyage, error=_hidden_and_cut(f"embed: {error}")),
        ),
        database_error=_hidden_and_cut(error),
        backup=replace(backup, text=_hidden_and_cut(error)),
    )
    assert _hidden_and_cut(error).endswith("zz…")


def test_scrub_keeps_each_unknown_as_none() -> None:
    seen = Observation(worker=None, bot=None)

    assert scrub(seen, Secrets((SECRET,))) == seen


def test_scrub_keeps_an_empty_error_text_empty() -> None:
    # str(TimeoutError()) is "". An empty text is a known error with no words. None would say
    # that no error is known, for example that the connection did not fail.
    seen = Observation(
        worker=None,
        bot=None,
        database=DatabaseView(
            queue=Queue(0, None, None),
            dead=DeadJobs(totals=(), newest_error=""),
            model=Refusals(None, None, ""),
            voyage=Refusals(None, None, ""),
        ),
        database_error="",
        backup=BackupVerdict(ok=False, text="", at=1795392000.0),
    )

    assert scrub(seen, Secrets((SECRET,))) == seen


def test_scrub_keeps_each_unknown_inside_the_view_of_the_database() -> None:
    # An error that is None stays None, not "". A provider that is None (not in use) stays None.
    seen = Observation(
        worker=None,
        bot=None,
        database=DatabaseView(
            queue=Queue(ready=0, oldest_ready_at=None, newest_attempt_end_at=None),
            dead=DeadJobs(totals=()),
            model=Refusals(newest_failure_at=None, newest_success_at=1795391400.0),
            voyage=None,
        ),
    )

    assert scrub(seen, Secrets((SECRET,))) == seen


def test_scrub_hides_the_text_of_a_good_backup_too() -> None:
    # A verdict that the backup is fine can quote a secret too.
    seen = Observation(
        worker=None,
        bot=None,
        backup=BackupVerdict(ok=True, text=f"backup ok, key {SECRET}", at=1795392000.0),
    )

    clean = scrub(seen, Secrets((SECRET,)))

    assert clean == replace(
        seen, backup=BackupVerdict(ok=True, text="backup ok, key <secret>", at=1795392000.0)
    )


def test_scrub_hides_an_error_text_also_when_the_fields_next_to_it_are_unknown() -> None:
    # scrub reads only the text. An error with no count of dead jobs, or with no time of the
    # failure, is hidden too.
    def view(error: str) -> DatabaseView:
        return DatabaseView(
            queue=Queue(ready=0, oldest_ready_at=None, newest_attempt_end_at=None),
            dead=DeadJobs(totals=(), newest_error=error),
            model=Refusals(newest_failure_at=None, newest_success_at=None, error=error),
            voyage=Refusals(newest_failure_at=None, newest_success_at=None, error=error),
        )

    seen = Observation(worker=None, bot=None, database=view(f"key {SECRET}"))

    clean = scrub(seen, Secrets((SECRET,)))

    assert clean == replace(seen, database=view("key <secret>"))


def test_a_field_that_a_check_does_not_give_is_unknown_or_empty() -> None:
    # A default that is not None, such as True or "", would look like a known state. A default
    # can also come from a factory: dataclasses keeps it in default_factory, not in default.
    kinds = (Liveness, Refusals, Budget, Queue, DeadJobs, DatabaseView, BackupVerdict, Observation)
    defaults = {
        f"{kind.__name__}.{field.name}": (
            field.default if field.default_factory is MISSING else field.default_factory()
        )
        for kind in kinds
        for field in fields(kind)
        if field.default is not MISSING or field.default_factory is not MISSING
    }
    # Each default is None or (), and a frozen value needs no factory for them. So a field
    # with a factory is reported, also when its factory gives the same value.
    factories = [
        f"{kind.__name__}.{field.name}"
        for kind in kinds
        for field in fields(kind)
        if field.default_factory is not MISSING
    ]

    assert factories == []
    assert defaults == {
        "Liveness.started": None,
        "Liveness.stopped": None,
        "Liveness.chats_ready": None,
        "Refusals.error": None,
        "Refusals.waiting": (),
        "Refusals.next_try_at": None,
        "Refusals.last_try_at": None,
        "Refusals.gave_up": (),
        "DeadJobs.newest_kind": None,
        "DeadJobs.newest_error": None,
        "DatabaseView.model": None,
        "DatabaseView.voyage": None,
        "DatabaseView.budget": None,
        "Observation.database": None,
        "Observation.database_error": None,
        "Observation.web_ok": None,
        "Observation.backup": None,
    }


def test_the_fields_keep_their_order() -> None:
    # Many fields have the type of the field next to them. A reader that builds a value by
    # position must not swap two of them, such as a failure and a success of a provider.
    order = {
        kind.__name__: [field.name for field in fields(kind)]
        for kind in (
            Liveness,
            Refusals,
            Budget,
            Queue,
            DeadJobs,
            DatabaseView,
            BackupVerdict,
            Observation,
        )
    }

    assert order == {
        "Liveness": ["mtime", "started", "stopped", "chats_ready"],
        "Refusals": [
            "newest_failure_at",
            "newest_success_at",
            "error",
            "waiting",
            "next_try_at",
            "last_try_at",
            "gave_up",
        ],
        "Budget": ["spent_usd", "budget_usd", "waiting"],
        "Queue": ["ready", "oldest_ready_at", "newest_attempt_end_at"],
        "DeadJobs": ["totals", "newest_kind", "newest_error"],
        "DatabaseView": ["queue", "dead", "model", "voyage", "budget"],
        "BackupVerdict": ["ok", "text", "at"],
        "Observation": ["worker", "bot", "database", "database_error", "web_ok", "backup"],
    }


# --- Properties --------------------------------------------------------------------------

# A secret: upper case letters and digits. The other parts of a text have none of them, so each
# part of a secret in an output comes from a secret.
_SECRET_CHARS = string.ascii_uppercase + string.digits
_SECRETS = st.text(_SECRET_CHARS, min_size=SECRET_MIN, max_size=40)
# The other parts: lower case letters, white space, control and format characters, and the
# characters of the patterns.
_OTHER = st.text(
    string.ascii_lowercase + " \n\t\r\x00\x07\x1b\x85\u200b\xad.,:;-_/@<>…é", max_size=30
)
# The shapes of the patterns, and texts near them that a cut can change.
_SHAPES = st.sampled_from(
    [
        "sk-ant-api03-MADEUPmadeup0123",
        TOKEN,
        "pa-MADEUPmadeup0123456789ab",
        "al-MADEUPmadeup0123456789ab",
        "Bearer made.up",
        "bearer \t",
        "https://user:tok@example.invalid",
        "https://hc-ping.com/made-up",
        "https://hc-ping.com/",
        "://",
        "@",
        "<",
        "…",
        "<secret>",
    ]
)
_LIMITS = st.none() | st.integers(min_value=1, max_value=300)


@st.composite
def _planted(draw: st.DrawFn, other: st.SearchStrategy[str] = _OTHER) -> tuple[Secrets, str]:
    """Secrets, and a text of other parts, whole secrets, and the parts of secrets that an
    earlier cut left: the last part of a secret at the start, the first part at the end."""
    values = draw(st.lists(_SECRETS, min_size=1, max_size=4))
    longer = draw(st.sampled_from(values))
    if draw(st.booleans()):  # a value inside a longer value
        start = draw(st.integers(0, len(longer) - SECRET_MIN))
        values.append(longer[start : draw(st.integers(start + SECRET_MIN, len(longer)))])
    secret = st.sampled_from(values)
    parts = draw(st.lists(other | secret, max_size=8))
    if draw(st.booleans()):
        value = draw(secret)
        parts.insert(0, value[-draw(st.integers(SECRET_PART_MIN, len(value) - 1)) :])
    if draw(st.booleans()):
        value = draw(secret)
        parts.append(value[: draw(st.integers(SECRET_PART_MIN, len(value) - 1))])
    return Secrets(tuple(values)), "".join(parts)


# Texts that no pattern changes and that hold no part of a secret. They keep each half of a
# split secret away from the ends of the text, where the rule of the cut ends applies.
_WORDS = st.text(string.ascii_lowercase + " .,", min_size=1, max_size=20)
_INVISIBLE = st.sampled_from(["\x00", "\x07", "\x1b", "\u200b", "\xad", "\ufeff"])


@st.composite
def _split(draw: st.DrawFn) -> tuple[str, str]:
    """A secret, and a text in which an invisible character splits it, inside the text."""
    value = draw(_SECRETS)
    at = draw(st.integers(1, len(value) - 1))
    split = value[:at] + draw(_INVISIBLE) + value[at:]
    return value, draw(_WORDS) + split + draw(_WORDS)


@given(_split(), _LIMITS)
def test_a_secret_that_an_invisible_character_splits_leaves_no_part(
    split: tuple[str, str], limit: int | None
) -> None:
    value, text = split

    hidden = hide_secrets(text, Secrets((value,)), limit=limit)

    assert [part for part in _parts(value, SECRET_PART_MIN) if part in hidden] == []


@given(_planted(), _LIMITS)
def test_no_secret_and_no_cut_part_of_a_secret_is_left(
    planted: tuple[Secrets, str], limit: int | None
) -> None:
    secrets, text = planted

    hidden = hide_secrets(text, secrets, limit=limit)

    for value in secrets.values:
        assert [part for part in _parts(value, SECRET_PART_MIN) if part in hidden] == []


_ANY = _planted(st.text(max_size=20) | _OTHER | _SHAPES)


@given(_ANY, st.integers(min_value=1, max_value=300))
def test_the_output_is_at_most_limit_long(planted: tuple[Secrets, str], limit: int) -> None:
    secrets, text = planted

    assert len(hide_secrets(text, secrets, limit=limit)) <= limit


@given(_ANY, st.none() | st.integers(min_value=1, max_value=120))
def test_hiding_twice_is_hiding_once(planted: tuple[Secrets, str], limit: int | None) -> None:
    secrets, text = planted
    once = hide_secrets(text, secrets, limit=limit)

    assert hide_secrets(once, secrets, limit=limit) == once


def _clean(text: str, *, one_line: bool) -> str:
    """The clean step, written again: no control character (Cc) and no format character (Cf).
    One line: one space for each run of white space. Many lines: a line break stays."""
    if one_line:
        kept = "".join(c for c in text if c.isspace() or unicodedata.category(c) not in "CcCf")
        return re.sub(r"\s+", " ", kept)
    return "".join(c for c in text if c == "\n" or unicodedata.category(c) not in "CcCf")


# Each pattern needs ":", "-", "@" or "/", except the one of "bearer". No part of a secret is
# in such a text, because a secret has only upper case ASCII letters and digits.
_PLAIN = st.text(st.characters(exclude_characters=":-@/" + _SECRET_CHARS)).filter(
    lambda text: re.search("(?i)bearer", text) is None
)


@given(_PLAIN, st.lists(_SECRETS, max_size=3), _LIMITS)
def test_a_text_with_no_secret_is_only_cleaned_and_cut(
    text: str, values: list[str], limit: int | None
) -> None:
    clean = _clean(text, one_line=limit is not None)
    expected = clean if limit is None or len(clean) <= limit else clean[: limit - 1] + "…"

    assert hide_secrets(text, Secrets(tuple(values)), limit=limit) == expected


def test_an_error_text_is_cut_to_200_characters_by_default() -> None:
    assert hide_secrets("y" * 200, Secrets()) == "y" * 200
    assert hide_secrets("y" * 201, Secrets()) == "y" * 199 + "…"
    # The cut keeps the first 199 characters as they are, also a space at their end.
    assert hide_secrets("y" * 198 + " z" + "y" * 10, Secrets()) == "y" * 198 + " …"


def test_a_message_is_not_cut() -> None:
    assert hide_secrets("y" * 500, Secrets(), limit=None) == "y" * 500


def test_a_line_separator_is_white_space_not_a_character_that_a_person_cannot_see() -> None:
    # U+2028 is in the category Zl, not Cc or Cf. A message keeps it, so the words stay apart.
    text = "line\u2028next"

    assert hide_secrets(text, Secrets()) == "line next"
    assert hide_secrets(text, Secrets(), limit=None) == text


def test_a_combining_mark_is_visible_and_stays() -> None:
    # A combining mark is in the category Mn, not Cc or Cf. A decomposed "é" is "e" and U+0301.
    # The Hindi word "namaste" has a virama (U+094D) and a vowel sign (U+0947).
    namaste = "\u0928\u092e\u0938\u094d\u0924\u0947"
    text = f"fetch 'Re\u0301sume\u0301 {namaste}' failed"

    assert hide_secrets(text, Secrets()) == text
    assert hide_secrets(text, Secrets(), limit=None) == text


@pytest.mark.parametrize("limit", [0, -3])
def test_a_limit_below_1_is_refused(limit: int) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        hide_secrets("abcdef", Secrets(), limit=limit)


def test_the_values_come_before_the_patterns() -> None:
    # The pattern of the user part ends at the first "@", in the password. If it came first,
    # the value would not match any more, and "ss-word-longer" would stay.
    text = "connect postgresql://unshelve:p@ss-word-longer@db/unshelve failed"

    hidden = hide_secrets(text, Secrets(("p@ss-word-longer",)))

    assert hidden == "connect postgresql://<secret>@db/unshelve failed"


@pytest.mark.parametrize("limit", [ERROR_CHARS, None])
@pytest.mark.parametrize("invisible", ["\x00", "\x1b", "\u200b", "\xad", "\ufeff"])
def test_a_secret_that_an_invisible_character_splits_is_hidden_after_the_clean(
    invisible: str, limit: int | None
) -> None:
    # Neither half is at an end of the text, so only the second run of the steps finds it.
    text = f"key {SECRET[:20]}{invisible}{SECRET[20:]} end"

    assert hide_secrets(text, Secrets((SECRET,)), limit=limit) == "key <secret> end"


@pytest.mark.parametrize("limit", [ERROR_CHARS, None])
def test_a_key_after_an_invisible_character_is_hidden_before_the_clean(limit: int | None) -> None:
    # The clean joins "x" and "pa-". Then the lookbehind of the pattern of Voyage fails, so
    # only the first run of the steps finds the key.
    text = "x\u200bpa-MADEUPmadeup0123456789ab end"

    assert hide_secrets(text, Secrets(), limit=limit) == "x<voyage key> end"


def test_the_first_part_of_a_secret_before_a_removed_character_is_hidden_after_the_clean() -> None:
    # A message keeps no "\r". Before the clean, the text does not end with the part.
    text = f"key {SECRET[:9]}\r"

    assert hide_secrets(text, Secrets((SECRET,)), limit=None) == "key <secret>"


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        # The clean step makes "\r\n" one space, and the space stays at the end.
        pytest.param(f"key {SECRET[:9]}\r\n", ERROR_CHARS, "key <secret> ", id="end, CRLF"),
        # A message keeps a "\n".
        pytest.param(f"key {SECRET[:9]}\n", None, "key <secret>\n", id="end, LF, a message"),
        pytest.param(f"\n {SECRET[-9:]} was refused", None, "\n <secret> was refused", id="start"),
    ],
)
def test_a_cut_part_of_a_secret_next_to_white_space_at_an_end_is_hidden(
    text: str, limit: int | None, expected: str
) -> None:
    assert hide_secrets(text, Secrets((SECRET,)), limit=limit) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # The first 9 characters of the value end in its space. The space is a part of the
        # value, so it is hidden too.
        pytest.param("key MADE0UP0 ", "key <secret>", id="end"),
        # The last 10 characters of the value start with its space.
        pytest.param(" THE0WATCH was refused", "<secret> was refused", id="start"),
    ],
)
def test_white_space_in_a_cut_part_of_a_value_is_hidden_too(text: str, expected: str) -> None:
    value = "MADE0UP0 KEY0FOR0 THE0WATCH"

    assert hide_secrets(text, Secrets((value,)), limit=None) == expected


@pytest.mark.parametrize(
    ("value", "text", "limit"),
    [
        # The clean step makes two spaces one: only the first run of the steps finds the value.
        pytest.param("pass  phrase 12345", "password pass  phrase 12345 refused", 200, id="spaces"),
        # A message keeps no tab.
        pytest.param("MADE\tUP-KEY-0123", "password MADE\tUP-KEY-0123 refused", None, id="tab"),
    ],
)
def test_a_secret_with_white_space_is_hidden_before_the_clean(
    value: str, text: str, limit: int | None
) -> None:
    assert hide_secrets(text, Secrets((value,)), limit=limit) == "password <secret> refused"

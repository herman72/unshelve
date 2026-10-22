"""AWS Signature Version 4, against the answers that AWS publishes. Each test name is one rule.

The seven cases of the first test come from the test suite of AWS for Signature Version 4, as
kept in awslabs/aws-c-auth (tests/aws-signing-test-suite/v4): the service "service", a body
that is not signed, and the keys of the suite. The two S3 tests are the examples "GET Object"
and "PUT Object" of the Amazon S3 API reference, with the example keys of AWS. These keys are
public test values, not secrets.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from unshelve.net.sigv4 import (
    Credentials,
    canonical_request,
    credential_scope,
    sign,
    signature,
    string_to_sign,
)

# The SHA-256 of no bytes, as the suite writes it.
NO_BODY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
SUITE_TIME = datetime(2015, 8, 30, 12, 36, tzinfo=UTC)
SUITE_SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"  # noqa: S105 - gitleaks:allow
S3_TIME = datetime(2013, 5, 24, tzinfo=UTC)
S3_KEYS = Credentials(
    "AKIAIOSFODNN7EXAMPLE",  # gitleaks:allow
    "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",  # gitleaks:allow
)
S3_HOST = "examplebucket.s3.amazonaws.com"


def _lines(*lines: str) -> str:
    return "\n".join(lines)


@pytest.mark.parametrize(
    ("method", "path", "query", "headers", "expected_request", "expected_signature"),
    [
        pytest.param(
            "GET",
            "/",
            [],
            {"Host": "example.amazonaws.com", "X-Amz-Date": "20150830T123600Z"},
            _lines(
                "GET",
                "/",
                "",
                "host:example.amazonaws.com",
                "x-amz-date:20150830T123600Z",
                "",
                "host;x-amz-date",
                NO_BODY,
            ),
            "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31",
            id="get-vanilla",
        ),
        pytest.param(
            "GET",
            "/-._~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
            [],
            {"Host": "example.amazonaws.com", "X-Amz-Date": "20150830T123600Z"},
            _lines(
                "GET",
                "/-._~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
                "",
                "host:example.amazonaws.com",
                "x-amz-date:20150830T123600Z",
                "",
                "host;x-amz-date",
                NO_BODY,
            ),
            "07ef7494c76fa4850883e2b006601f940f8a34d404d0cfa977f52a65bbf5f24f",
            id="get-unreserved",
        ),
        pytest.param(
            "GET",
            "/ሴ",
            [],
            {"Host": "example.amazonaws.com", "X-Amz-Date": "20150830T123600Z"},
            _lines(
                "GET",
                "/%E1%88%B4",
                "",
                "host:example.amazonaws.com",
                "x-amz-date:20150830T123600Z",
                "",
                "host;x-amz-date",
                NO_BODY,
            ),
            "8318018e0b0f223aa2bbf98705b62bb787dc9c0e678f255a891fd03141be5d85",
            id="get-utf8",
        ),
        pytest.param(
            "GET",
            "/",
            [("Param2", "value2"), ("Param1", "value1")],
            {"Host": "example.amazonaws.com", "X-Amz-Date": "20150830T123600Z"},
            _lines(
                "GET",
                "/",
                "Param1=value1&Param2=value2",
                "host:example.amazonaws.com",
                "x-amz-date:20150830T123600Z",
                "",
                "host;x-amz-date",
                NO_BODY,
            ),
            "b97d918cfa904a5beff61c982a1b6f458b799221646efd99d3219ec94cdf2500",
            id="get-vanilla-query-order-key-case",
        ),
        pytest.param(
            "GET",
            "/",
            [],
            {
                "Host": "example.amazonaws.com",
                "My-Header1": " value1",
                "My-Header2": ' "a   b   c"',
                "X-Amz-Date": "20150830T123600Z",
            },
            _lines(
                "GET",
                "/",
                "",
                "host:example.amazonaws.com",
                "my-header1:value1",
                'my-header2:"a b c"',
                "x-amz-date:20150830T123600Z",
                "",
                "host;my-header1;my-header2;x-amz-date",
                NO_BODY,
            ),
            "acc3ed3afb60bb290fc8d2dd0098b9911fcaa05412b367055dee359757a9c736",
            id="get-header-value-trim",
        ),
        pytest.param(
            "POST",
            "/",
            [],
            {"Host": "example.amazonaws.com", "X-Amz-Date": "20150830T123600Z"},
            _lines(
                "POST",
                "/",
                "",
                "host:example.amazonaws.com",
                "x-amz-date:20150830T123600Z",
                "",
                "host;x-amz-date",
                NO_BODY,
            ),
            "5da7c1a2acd57cee7505fc6676e4e544621c30862966e37dddb68e92efbe5d6b",
            id="post-vanilla",
        ),
        pytest.param(
            "POST",
            "/",
            [],
            {
                "Host": "example.amazonaws.com",
                "My-Header1": "value1",
                "X-Amz-Date": "20150830T123600Z",
            },
            _lines(
                "POST",
                "/",
                "",
                "host:example.amazonaws.com",
                "my-header1:value1",
                "x-amz-date:20150830T123600Z",
                "",
                "host;my-header1;x-amz-date",
                NO_BODY,
            ),
            "c5410059b04c1ee005303aed430f6e6645f61f4dc9e1461ec8f8916fdf18852c",
            id="post-header-key-sort",
        ),
    ],
)
def test_the_signature_is_the_one_of_the_aws_test_suite(
    method: str,
    path: str,
    query: list[tuple[str, str]],
    headers: dict[str, str],
    expected_request: str,
    expected_signature: str,
) -> None:
    canonical = canonical_request(method, path, query, headers, NO_BODY)
    scope = credential_scope(SUITE_TIME, "us-east-1", "service")
    to_sign = string_to_sign(SUITE_TIME, scope, canonical)

    assert canonical == expected_request
    assert signature(SUITE_SECRET, SUITE_TIME, "us-east-1", "service", to_sign) == (
        expected_signature
    )


def test_the_string_to_sign_is_the_one_of_the_aws_test_suite() -> None:
    canonical = canonical_request(
        "GET", "/", [], {"Host": "example.amazonaws.com", "X-Amz-Date": "20150830T123600Z"}, NO_BODY
    )
    scope = credential_scope(SUITE_TIME, "us-east-1", "service")

    assert string_to_sign(SUITE_TIME, scope, canonical) == _lines(
        "AWS4-HMAC-SHA256",
        "20150830T123600Z",
        "20150830/us-east-1/service/aws4_request",
        "bb579772317eb040ac9ed261061d46c1f17a8133879d6129b6e1c25292927e63",
    )


def test_a_get_of_s3_is_signed_as_in_the_example_of_aws() -> None:
    headers = sign(
        "GET",
        "/test.txt",
        {"host": S3_HOST, "range": "bytes=0-9"},
        b"",
        credentials=S3_KEYS,
        region="us-east-1",
        service="s3",
        now=S3_TIME,
    )

    assert headers["x-amz-date"] == "20130524T000000Z"
    assert headers["x-amz-content-sha256"] == NO_BODY
    assert headers["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


def test_a_put_of_s3_is_signed_as_in_the_example_of_aws() -> None:
    # The path holds a "$": S3 signs it as "%24".
    headers = sign(
        "PUT",
        "/test$file.text",
        {
            "date": "Fri, 24 May 2013 00:00:00 GMT",
            "host": S3_HOST,
            "x-amz-storage-class": "REDUCED_REDUNDANCY",
        },
        b"Welcome to Amazon S3.",
        credentials=S3_KEYS,
        region="us-east-1",
        service="s3",
        now=S3_TIME,
    )

    assert headers["x-amz-content-sha256"] == (
        "44ce7dd67c959e0d3524ffac1771dfbba87d2b6b4b4e99e42034a8b803f8b072"
    )
    assert headers["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=date;host;x-amz-content-sha256;x-amz-date;x-amz-storage-class, "
        "Signature=98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"
    )


def test_a_request_without_its_host_is_not_signed() -> None:
    with pytest.raises(ValueError, match="host"):
        sign(
            "GET",
            "/test.txt",
            {},
            b"",
            credentials=S3_KEYS,
            region="us-east-1",
            service="s3",
            now=S3_TIME,
        )


def test_the_time_of_a_signature_is_in_utc() -> None:
    tehran = timezone(timedelta(hours=3, minutes=30))

    headers = sign(
        "GET",
        "/test.txt",
        {"host": S3_HOST},
        b"",
        credentials=S3_KEYS,
        region="us-east-1",
        service="s3",
        now=datetime(2026, 10, 22, 15, 30, tzinfo=tehran),
    )

    assert headers["x-amz-date"] == "20261022T120000Z"
    assert "/20261022/us-east-1/s3/aws4_request" in headers["authorization"]


_NAMES = st.lists(st.from_regex(r"x-[a-z]{1,8}", fullmatch=True), max_size=5, unique=True)
_VALUES = st.text(st.characters(min_codepoint=33, max_codepoint=126), min_size=1, max_size=20)


def _authorization(headers: dict[str, str], payload: bytes = b"") -> str:
    return sign(
        "PUT",
        "/bucket/feed.json",
        {"host": S3_HOST, **headers},
        payload,
        credentials=S3_KEYS,
        region="auto",
        service="s3",
        now=S3_TIME,
    )["authorization"]


@given(names=_NAMES, values=st.data())
def test_the_case_and_the_order_of_the_names_do_not_change_the_signature(
    names: list[str], values: st.DataObject
) -> None:
    headers = {name: values.draw(_VALUES) for name in names}
    shuffled = {name.upper(): value for name, value in reversed(headers.items())}

    assert _authorization(shuffled) == _authorization(headers)


@given(payload=st.binary(max_size=64), other=st.binary(max_size=64))
def test_each_byte_of_the_body_is_in_the_signature(payload: bytes, other: bytes) -> None:
    assume(payload != other)

    assert _authorization({}, payload) != _authorization({}, other)


# A secret starts with a marker that no header holds by chance.
_SECRETS = st.text(st.characters(min_codepoint=33, max_codepoint=126)).map(lambda t: f"SECRET-{t}")


@given(secret=_SECRETS)
def test_the_secret_is_in_no_header_and_in_no_representation(secret: str) -> None:
    headers = sign(
        "GET",
        "/bucket/feed.json",
        {"host": S3_HOST},
        b"",
        credentials=Credentials("AKIDEXAMPLE", secret),
        region="auto",
        service="s3",
        now=S3_TIME,
    )

    assert all(secret not in value for value in headers.values())
    assert secret not in repr(Credentials("AKIDEXAMPLE", secret))

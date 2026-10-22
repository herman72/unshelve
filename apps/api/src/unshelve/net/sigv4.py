"""AWS Signature Version 4: how a request to S3, or to a service with the same API, is signed
(docs/learn/44).

The client and the service make the same signature from the same parts of the request, each on
its own side, and the service compares the two. The secret never travels: the signature comes
from a key that is derived from the secret, and that key is valid for one day, one region and
one service.

1. The canonical request: the method, the path, the query, the headers to sign (lowercase and
   sorted), the names of those headers, and the SHA-256 of the body.
2. The string to sign: the algorithm, the time, the scope (day/region/service/aws4_request) and
   the SHA-256 of the canonical request.
3. The signing key: an HMAC-SHA256 of the day with "AWS4" + the secret as the key, then of the
   region, the service and "aws4_request", each with the result before it as the key.
4. The signature: an HMAC-SHA256 of the string to sign with the signing key, as hexadecimal.

S3 differs from the other services of AWS in one way, and this module follows S3: the path is
encoded once and never normalized ("/a/./b" stays as it is).
"""

import hashlib
import hmac
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import quote

ALGORITHM = "AWS4-HMAC-SHA256"
# The SHA-256 of no bytes: the body of a GET.
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


@dataclass(frozen=True, slots=True)
class Credentials:
    access_key_id: str
    # Not in the representation of the credentials, so not in a log or an error.
    secret_access_key: str = field(repr=False)


def amz_date(now: datetime) -> str:
    """The time of the request in the form of ``x-amz-date``: 20261022T120000Z."""
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def credential_scope(now: datetime, region: str, service: str) -> str:
    """Where the signing key is valid: 20261022/eu-north-1/s3/aws4_request."""
    return f"{now.astimezone(UTC):%Y%m%d}/{region}/{service}/aws4_request"


def canonical_request(
    method: str,
    path: str,
    query: Sequence[tuple[str, str]],
    headers: Mapping[str, str],
    payload_sha256: str,
) -> str:
    """The request in the one form that both sides make. ``headers`` are the headers to sign."""
    signed = _canonical_headers(headers)
    pairs = sorted((_encode(name), _encode(value)) for name, value in query)
    return "\n".join(
        [
            method,
            quote(path, safe="/"),
            "&".join(f"{name}={value}" for name, value in pairs),
            *(f"{name}:{value}" for name, value in signed),
            "",
            ";".join(name for name, _ in signed),
            payload_sha256,
        ]
    )


def string_to_sign(now: datetime, scope: str, canonical: str) -> str:
    return "\n".join([ALGORITHM, amz_date(now), scope, _sha256(canonical.encode())])


def signature(secret: str, now: datetime, region: str, service: str, to_sign: str) -> str:
    key = f"AWS4{secret}".encode()
    for part in (f"{now.astimezone(UTC):%Y%m%d}", region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    return hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()


def sign(
    method: str,
    path: str,
    headers: Mapping[str, str],
    payload: bytes,
    *,
    credentials: Credentials,
    region: str,
    service: str,
    now: datetime,
) -> dict[str, str]:
    """The headers of the signed request: ``headers``, which must hold ``host``, with
    ``x-amz-date``, ``x-amz-content-sha256`` and ``authorization``. Each header but the last
    one is signed, so the service refuses a request whose headers or body changed on the way."""
    if "host" not in {name.lower() for name in headers}:
        raise ValueError("a signed request needs its host header")
    payload_sha256 = _sha256(payload)
    signed = {**headers, "x-amz-date": amz_date(now), "x-amz-content-sha256": payload_sha256}
    scope = credential_scope(now, region, service)
    to_sign = string_to_sign(
        now, scope, canonical_request(method, path, (), signed, payload_sha256)
    )
    names = ";".join(name for name, _ in _canonical_headers(signed))
    proof = signature(credentials.secret_access_key, now, region, service, to_sign)
    return {
        **signed,
        "authorization": (
            f"{ALGORITHM} Credential={credentials.access_key_id}/{scope}, "
            f"SignedHeaders={names}, Signature={proof}"
        ),
    }


def _canonical_headers(headers: Mapping[str, str]) -> list[tuple[str, str]]:
    """Lowercase names, sorted. Each value without its outer spaces, and with one space for each
    run of spaces in it. Two headers with one name become one, their values joined by commas."""
    merged: dict[str, list[str]] = {}
    for name, value in headers.items():
        merged.setdefault(name.strip().lower(), []).append(" ".join(value.split()))
    return sorted((name, ",".join(values)) for name, values in merged.items())


def _encode(text: str) -> str:
    """A part of the query: each byte except the unreserved characters as %XX."""
    return quote(text, safe="")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

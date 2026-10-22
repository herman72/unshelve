"""The feed as one object in an object storage with the S3 API, such as Amazon S3, Cloudflare R2
or MinIO (docs/learn/44).

Each publish reads the object, and writes the feed only when its bytes differ. A write is one
PUT, and the service keeps the old object until the new one is complete: a reader gets the old
feed or the new one, never a part of it.

Each request is signed with AWS Signature Version 4 (``net/sigv4.py``) and goes through the
client for fixed services (``net/service.py``): https only, no redirect, a limit of bytes and of
time. Each publish opens its own connection and closes it, so the target holds nothing open.

The bucket is in the path of the address ("path style"): https://<endpoint>/<bucket>/<object>.
Each service with the S3 API answers there.
"""

import re
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from urllib.parse import urlsplit

from unshelve.contracts.dto import Published
from unshelve.contracts.errors import PublishError
from unshelve.net.service import ServiceAnswer, ServiceClient, ServiceUnreachableError, Transport
from unshelve.net.sigv4 import Credentials, sign
from unshelve.publish.git import hide_secrets

CONTENT_TYPE = "application/json"
# A reader, such as the site or a CDN, may keep the feed for five minutes before it asks again.
CACHE_CONTROL = "public, max-age=300"
# One request. A slow connection fits.
TIMEOUT = 30.0
# A feed of many thousand items is a few megabytes.
MAX_FEED_BYTES = 64 * 1024 * 1024

# The rules of S3 and R2 for a bucket name: 3 to 63 characters.
_BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
# Parts without characters that an address encodes, so the signed path is the path sent.
_OBJECT = re.compile(r"[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*")
_REGION = re.compile(r"[a-z0-9-]{1,32}")
# The key ID goes into the Authorization header: no space, comma, slash or equals sign.
_KEY_ID = re.compile(r"[A-Za-z0-9._~+-]{1,128}")
# The error of S3: <Error><Code>AccessDenied</Code><Message>Access Denied</Message>...
_CODE = re.compile(r"<Code>([^<]{1,100})</Code>")
_MESSAGE = re.compile(r"<Message>([^<]{1,300})</Message>")
# Answers that can pass: a busy or broken service, a write that met another one, a clock that
# is set again.
_PASSING_STATUS = frozenset({408, 409, 429})
_PASSING_CODES = frozenset(
    {
        "InternalError",
        "OperationAborted",
        "RequestTimeTooSkewed",
        "RequestTimeout",
        "ServiceUnavailable",
        "SlowDown",
    }
)


def endpoint_address(text: str) -> str:
    """The address of the service: https and a host, with nothing after it.

    Raises ``ValueError``: a wrong address is a fault of the settings.
    """
    parts = urlsplit(text)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        msg = (
            "the endpoint must be https and a host with nothing after it, such as "
            f"https://<account>.r2.cloudflarestorage.com, not {hide_secrets(text)!r}"
        )
        raise ValueError(msg)
    return f"https://{parts.netloc}"


def bucket_name(text: str) -> str:
    if not _BUCKET.fullmatch(text) or ".." in text:
        msg = (
            "the bucket must be 3 to 63 lowercase letters, digits, dots or hyphens, with a "
            f"letter or a digit at each end, not {text!r}"
        )
        raise ValueError(msg)
    return text


def object_key(text: str) -> str:
    if (
        len(text.encode()) > 1024
        or not _OBJECT.fullmatch(text)
        or {".", ".."} & set(text.split("/"))
    ):
        msg = (
            "the object must be a name such as feed.json or public/feed.json: letters, "
            f"digits and . _ ~ - in each part, '/' between the parts, not {text!r}"
        )
        raise ValueError(msg)
    return text


def region_name(text: str) -> str:
    if not _REGION.fullmatch(text):
        msg = f"the region must be a name such as auto or eu-north-1, not {text!r}"
        raise ValueError(msg)
    return text


def key_id(text: str) -> str:
    if not _KEY_ID.fullmatch(text):
        # Without the text: a secret in the wrong setting must not reach a log.
        msg = "the access key ID must be letters, digits and . _ ~ + -, with no space"
        raise ValueError(msg)
    return text


class S3Target:
    def __init__(
        self,
        endpoint: str,
        *,
        region: str,
        bucket: str,
        key: str,
        credentials: Credentials,
        transport: Transport | None = None,
    ) -> None:
        """``transport`` is for the tests: a pretend object storage in the same process."""
        self._endpoint = endpoint_address(endpoint)
        self._region = region_name(region)
        self._path = f"/{bucket_name(bucket)}/{object_key(key)}"
        key_id(credentials.access_key_id)
        self._credentials = credentials
        self._transport = transport

    @property
    def where(self) -> str:
        return f"{self._endpoint}{self._path}"

    async def publish(self, data: bytes) -> Published:
        async with self._connection() as client:
            if await self._get(client) == data:
                return Published(where=self.where, changed=False)
            headers = {"content-type": CONTENT_TYPE, "cache-control": CACHE_CONTROL}
            answer = await self._send(client, "PUT", data, headers)
            if answer.status != 200:
                raise self._refused(answer)
        return Published(where=self.where, changed=True)

    async def read(self) -> bytes | None:
        async with self._connection() as client:
            return await self._get(client)

    async def _get(self, client: ServiceClient) -> bytes | None:
        answer = await self._send(client, "GET", b"", {})
        if answer.status == 200:
            return answer.body
        # No object yet. A bucket that does not exist is a fault of the settings.
        if answer.status == 404 and _first(_CODE, answer.body) != "NoSuchBucket":
            return None
        raise self._refused(answer)

    async def _send(
        self, client: ServiceClient, method: str, payload: bytes, headers: Mapping[str, str]
    ) -> ServiceAnswer:
        signed = sign(
            method,
            self._path,
            {"host": client.host, **headers},
            payload,
            credentials=self._credentials,
            region=self._region,
            service="s3",
            now=datetime.now(UTC),
        )
        try:
            return await client.send(method, self._path, headers=signed, content=payload or None)
        except ServiceUnreachableError as error:
            msg = f"the object storage gave no answer ({error.reason}) for {self.where}"
            # An object larger than the limit stays larger at the next try.
            raise PublishError(msg, retryable=error.reason != "too_large") from error

    @asynccontextmanager
    async def _connection(self) -> AsyncGenerator[ServiceClient]:
        client = ServiceClient(
            self._endpoint,
            headers={},
            timeout=TIMEOUT,
            max_bytes=MAX_FEED_BYTES,
            transport=self._transport,
        )
        try:
            yield client
        finally:
            await client.aclose()

    def _refused(self, answer: ServiceAnswer) -> PublishError:
        code = _first(_CODE, answer.body)
        message = _first(_MESSAGE, answer.body)
        said = " ".join(
            part for part in (str(answer.status), code, f"({message})" if message else "") if part
        )
        return PublishError(
            f"the object storage answered {said} for {self.where}",
            retryable=(
                answer.status in _PASSING_STATUS or answer.status >= 500 or code in _PASSING_CODES
            ),
        )


def _first(pattern: re.Pattern[str], body: bytes) -> str:
    found = pattern.search(body.decode(errors="replace"))
    return found.group(1).strip() if found else ""

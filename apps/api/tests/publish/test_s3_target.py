"""The object storage target of the public feed, against a pretend object storage in the same
process. Each test name is one rule.

The pretend storage keeps one bucket in memory and answers as S3 does. It also checks each
request as S3 does: the key ID, the region, the SHA-256 of the body, and the signature of the
headers that the request names. So a test fails when the target sends anything other than what
it signed.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from unshelve.config import Settings
from unshelve.contracts.dto import Published
from unshelve.contracts.errors import PublishError
from unshelve.contracts.protocols import FeedTarget
from unshelve.net.service import ServiceClient, Transport
from unshelve.net.sigv4 import (
    Credentials,
    canonical_request,
    credential_scope,
    signature,
    string_to_sign,
)
from unshelve.publish import s3
from unshelve.publish.s3 import (
    S3Target,
    bucket_name,
    endpoint_address,
    key_id,
    object_key,
    region_name,
)
from unshelve.publish.targets import UnusableTarget, build_feed_target
from unshelve.testing.conformance import FeedTargetConformance

ENDPOINT = "https://account.r2.example"
SECRET = "made-up-secret-of-the-tests"  # noqa: S105 - a made-up value
KEYS = Credentials("AKIDEXAMPLE", SECRET)
FEED = b'{\n  "version": 1,\n  "items": []\n}\n'


def _answer(status: int, body: bytes = b"") -> httpx.Response:
    # A stream, as from a real connection: the client reads the bytes as they arrive.
    return httpx.Response(status, stream=httpx.ByteStream(body))


def _error(status: int, code: str, message: str = "Made-up message.") -> httpx.Response:
    return _answer(
        status,
        (
            "<?xml version='1.0' encoding='UTF-8'?>"
            f"<Error><Code>{code}</Code><Message>{message}</Message></Error>"
        ).encode(),
    )


@dataclass
class PretendStorage:
    """One bucket of an object storage, in the memory of the test."""

    bucket: str = "feeds"
    region: str = "auto"
    keys: Credentials = KEYS
    objects: dict[str, bytes] = field(default_factory=dict[str, bytes])
    requests: list[httpx.Request] = field(default_factory=list[httpx.Request])
    # Answers to give first, in this order, before the storage answers by itself.
    answers: list[httpx.Response] = field(default_factory=list[httpx.Response])

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._answer)

    def _answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.answers:
            return self.answers.pop(0)
        if refused := self._check(request):
            return refused
        bucket, _, key = request.url.path.removeprefix("/").partition("/")
        if bucket != self.bucket:
            return _error(404, "NoSuchBucket", "The specified bucket does not exist")
        if request.method == "PUT":
            self.objects[key] = request.content
            return _answer(200)
        if key not in self.objects:
            return _error(404, "NoSuchKey", "The specified key does not exist.")
        return _answer(200, self.objects[key])

    def _check(self, request: httpx.Request) -> httpx.Response | None:
        """The checks of S3: the key ID, the region, the body, the signature."""
        parts = request.headers.get("authorization", "").removeprefix("AWS4-HMAC-SHA256 ")
        fields = dict(part.split("=", 1) for part in parts.split(", ") if "=" in part)
        key, _, scope = fields.get("Credential", "").partition("/")
        if key != self.keys.access_key_id:
            return _error(403, "InvalidAccessKeyId", "The Access Key Id does not exist.")
        now = datetime.strptime(request.headers["x-amz-date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        if scope != credential_scope(now, self.region, "s3"):
            return _error(400, "AuthorizationHeaderMalformed", f"Expecting '{self.region}'.")
        payload = request.headers["x-amz-content-sha256"]
        if payload != hashlib.sha256(request.content).hexdigest():
            return _error(400, "XAmzContentSHA256Mismatch", "The SHA-256 does not match.")
        names = fields.get("SignedHeaders", "").split(";")
        if not {"host", "x-amz-date", "x-amz-content-sha256"} <= set(names):
            return _error(403, "AccessDenied", "A required header is not signed.")
        signed = {name: request.headers[name] for name in names}
        canonical = canonical_request(request.method, request.url.path, [], signed, payload)
        to_sign = string_to_sign(now, scope, canonical)
        if fields.get("Signature") != signature(
            self.keys.secret_access_key, now, self.region, "s3", to_sign
        ):
            return _error(403, "SignatureDoesNotMatch", "The signature does not match.")
        return None


def _target(storage: PretendStorage, **options: Any) -> S3Target:
    settings: dict[str, Any] = {
        "region": "auto",
        "bucket": "feeds",
        "key": "feed.json",
        "credentials": KEYS,
    } | options
    return S3Target(ENDPOINT, transport=storage.transport(), **settings)


class TestS3Target(FeedTargetConformance):
    @pytest.fixture
    def target(self) -> FeedTarget:
        return _target(PretendStorage())


async def test_the_feed_is_one_object_with_its_type_and_its_time_in_a_cache() -> None:
    storage = PretendStorage()

    published = await _target(storage, key="public/feed.json").publish(FEED)

    assert published == Published(
        where="https://account.r2.example/feeds/public/feed.json", changed=True
    )
    get, put = storage.requests
    assert (get.method, put.method) == ("GET", "PUT")
    assert str(put.url) == "https://account.r2.example/feeds/public/feed.json"
    assert put.content == FEED
    assert put.headers["content-type"] == "application/json"
    assert put.headers["cache-control"] == "public, max-age=300"
    assert storage.objects == {"public/feed.json": FEED}


async def test_the_same_bytes_make_no_write() -> None:
    storage = PretendStorage()
    target = _target(storage)
    await target.publish(FEED)

    assert not (await target.publish(FEED)).changed
    assert [request.method for request in storage.requests] == ["GET", "PUT", "GET"]


async def test_each_header_that_could_change_the_feed_is_signed() -> None:
    storage = PretendStorage()

    await _target(storage).publish(FEED)

    put = storage.requests[1]
    signed = put.headers["authorization"].split("SignedHeaders=")[1].split(",")[0].split(";")
    assert signed == [
        "cache-control",
        "content-type",
        "host",
        "x-amz-content-sha256",
        "x-amz-date",
    ]


async def test_a_wrong_secret_is_a_fault_of_the_settings_and_is_in_no_message() -> None:
    target = _target(PretendStorage(), credentials=Credentials("AKIDEXAMPLE", "another-secret"))

    with pytest.raises(PublishError) as raised:
        await target.publish(FEED)

    assert "403 SignatureDoesNotMatch (The signature does not match.)" in str(raised.value)
    assert "https://account.r2.example/feeds/feed.json" in str(raised.value)
    assert raised.value.retryable is False
    assert "another-secret" not in str(raised.value)
    assert "another-secret" not in target.where


async def test_a_wrong_region_is_a_fault_of_the_settings() -> None:
    with pytest.raises(PublishError, match="400 AuthorizationHeaderMalformed") as raised:
        await _target(PretendStorage(region="auto"), region="eu-north-1").read()

    assert "Expecting 'auto'" in str(raised.value)
    assert raised.value.retryable is False


async def test_a_bucket_that_does_not_exist_is_a_fault_of_the_settings() -> None:
    storage = PretendStorage(bucket="feeds")
    target = _target(storage, bucket="other")

    # Not "no feed yet": the settings name a bucket that is not there.
    with pytest.raises(PublishError, match="404 NoSuchBucket") as raised:
        await target.read()
    assert raised.value.retryable is False

    with pytest.raises(PublishError, match="404 NoSuchBucket"):
        await target.publish(FEED)
    # The publish stops at its first request, and writes nothing.
    assert [request.method for request in storage.requests] == ["GET", "GET"]


@pytest.mark.parametrize(
    ("status", "code", "can_pass"),
    [
        (403, "AccessDenied", False),
        (403, "InvalidAccessKeyId", False),
        (301, "PermanentRedirect", False),
        (400, "InvalidRequest", False),
        (403, "RequestTimeTooSkewed", True),
        (409, "OperationAborted", True),
        (429, "", True),
        (500, "InternalError", True),
        (502, "", True),
        (503, "SlowDown", True),
    ],
)
async def test_the_answer_says_whether_a_later_try_can_pass(
    status: int, code: str, can_pass: bool
) -> None:
    storage = PretendStorage(answers=[_error(status, code)])

    with pytest.raises(PublishError, match=f"answered {status}") as raised:
        await _target(storage).publish(FEED)

    assert raised.value.retryable is can_pass


@pytest.mark.parametrize(
    ("status", "code", "can_pass"), [(503, "SlowDown", True), (403, "AccessDenied", False)]
)
async def test_a_refused_write_is_a_fault_too(status: int, code: str, can_pass: bool) -> None:
    storage = PretendStorage(answers=[_error(404, "NoSuchKey"), _error(status, code)])

    with pytest.raises(PublishError, match=f"{status} {code}") as raised:
        await _target(storage).publish(FEED)

    assert raised.value.retryable is can_pass
    assert [request.method for request in storage.requests] == ["GET", "PUT"]


async def test_no_connection_is_a_fault_that_can_pass() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    target = S3Target(
        ENDPOINT,
        region="auto",
        bucket="feeds",
        key="feed.json",
        credentials=KEYS,
        transport=httpx.MockTransport(refuse),
    )

    with pytest.raises(PublishError, match=r"gave no answer \(network\)") as raised:
        await target.publish(FEED)

    assert raised.value.retryable is True


async def test_an_object_larger_than_the_limit_is_a_lasting_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(s3, "MAX_FEED_BYTES", 10)
    storage = PretendStorage(objects={"feed.json": b"x" * 11})

    with pytest.raises(PublishError, match="too_large") as raised:
        await _target(storage).read()

    assert raised.value.retryable is False


async def test_each_publish_closes_its_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[ServiceClient] = []
    close = ServiceClient.aclose

    async def record(self: ServiceClient) -> None:
        closed.append(self)
        await close(self)

    monkeypatch.setattr(ServiceClient, "aclose", record)
    target = _target(PretendStorage())

    await target.publish(FEED)
    await target.read()

    assert len(closed) == 2


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://account.r2.example",
        "https://account.r2.example/feeds",
        "https://account.r2.example?region=auto",
        "account.r2.example",
        "https://",
    ],
)
def test_the_endpoint_is_https_and_a_host_with_nothing_after_it(endpoint: str) -> None:
    with pytest.raises(ValueError, match="the endpoint must be https"):
        endpoint_address(endpoint)


def test_a_password_in_the_endpoint_is_in_no_message() -> None:
    with pytest.raises(ValueError, match="endpoint") as raised:
        endpoint_address("https://someone:made-up-token@account.r2.example")

    assert "made-up-token" not in str(raised.value)


def test_a_good_endpoint_keeps_its_host_and_port() -> None:
    assert endpoint_address("https://account.r2.example/") == "https://account.r2.example"
    assert endpoint_address("https://minio.example:9000") == "https://minio.example:9000"


@pytest.mark.parametrize(
    "name", ["ab", "Feeds", "-feeds", "feeds-", "my..feeds", "my_feeds", "a" * 64]
)
def test_a_bucket_name_follows_the_rules_of_s3(name: str) -> None:
    assert bucket_name("feeds") == "feeds"
    assert bucket_name("my.site-feeds") == "my.site-feeds"
    with pytest.raises(ValueError, match="the bucket must be"):
        bucket_name(name)


@pytest.mark.parametrize(
    "key",
    [
        "",
        "/feed.json",
        "public//feed.json",
        "../feed.json",
        "public/./feed.json",
        "feed.json/",
        "my feed.json",
        "فید.json",
        "x" * 1025,
    ],
)
def test_an_object_name_has_only_characters_that_an_address_keeps(key: str) -> None:
    assert object_key("public/feed_2~a-b.json") == "public/feed_2~a-b.json"
    with pytest.raises(ValueError, match="the object must be"):
        object_key(key)


@pytest.mark.parametrize("region", ["", "EU", "eu north", "eu/north"])
def test_a_region_is_a_plain_name(region: str) -> None:
    assert region_name("auto") == "auto"
    assert region_name("eu-north-1") == "eu-north-1"
    with pytest.raises(ValueError, match="the region must be"):
        region_name(region)


@pytest.mark.parametrize("text", ["", "AKID EXAMPLE", "AKID,Signature=x", "AKID/x"])
def test_the_access_key_id_cannot_change_the_authorization_header(text: str) -> None:
    assert key_id("AKIDEXAMPLE") == "AKIDEXAMPLE"
    with pytest.raises(ValueError, match="access key ID") as raised:
        key_id(text)

    # A secret that was put into the wrong setting must not reach a log.
    assert not text or text not in str(raised.value)


def _settings(**options: Any) -> Settings:
    values: dict[str, Any] = {
        "env": "test",
        "publish_target": "s3",
        "publish_s3_endpoint": ENDPOINT,
        "publish_s3_region": "auto",
        "publish_s3_bucket": "feeds",
        "publish_s3_access_key_id": "AKIDEXAMPLE",
        "publish_s3_secret_access_key": SECRET,
    } | options
    return Settings(**values)


def test_the_settings_choose_the_object_storage_target() -> None:
    target = build_feed_target(_settings(publish_s3_object="public/feed.json"))

    assert isinstance(target, S3Target)
    assert target.where == "https://account.r2.example/feeds/public/feed.json"


async def test_the_settings_give_their_keys_region_and_bucket_to_each_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = PretendStorage(region="eu-north-1")

    class PretendClient(ServiceClient):
        """The client of the target, with the pretend storage as its transport."""

        def __init__(
            self,
            base_url: str,
            *,
            headers: Mapping[str, str],
            timeout: float,
            max_bytes: int,
            transport: Transport | None = None,
        ) -> None:
            super().__init__(
                base_url,
                headers=headers,
                timeout=timeout,
                max_bytes=max_bytes,
                transport=storage.transport(),
            )

    monkeypatch.setattr(s3, "ServiceClient", PretendClient)
    target = build_feed_target(_settings(publish_s3_region="eu-north-1"))
    assert target is not None

    assert (await target.publish(FEED)).changed
    assert storage.objects == {"feed.json": FEED}
    assert storage.requests[0].headers["host"] == "account.r2.example"


async def test_missing_settings_are_named_and_fail_each_publish() -> None:
    target = build_feed_target(
        Settings(
            env="test", publish_target="s3", publish_s3_endpoint=ENDPOINT, publish_s3_bucket=""
        )
    )

    assert isinstance(target, UnusableTarget)
    with pytest.raises(PublishError) as raised:
        await target.publish(FEED)
    assert str(raised.value) == (
        "UNSHELVE_PUBLISH_TARGET is s3, but UNSHELVE_PUBLISH_S3_REGION, "
        "UNSHELVE_PUBLISH_S3_BUCKET, UNSHELVE_PUBLISH_S3_ACCESS_KEY_ID, "
        "UNSHELVE_PUBLISH_S3_SECRET_ACCESS_KEY are not set"
    )
    assert raised.value.retryable is False


@pytest.mark.parametrize(
    ("options", "name"),
    [
        ({"publish_s3_endpoint": "http://account.r2.example"}, "UNSHELVE_PUBLISH_S3_ENDPOINT"),
        ({"publish_s3_region": "EU"}, "UNSHELVE_PUBLISH_S3_REGION"),
        ({"publish_s3_bucket": "Feeds"}, "UNSHELVE_PUBLISH_S3_BUCKET"),
        ({"publish_s3_object": "../feed.json"}, "UNSHELVE_PUBLISH_S3_OBJECT"),
        ({"publish_s3_access_key_id": SECRET + " x"}, "UNSHELVE_PUBLISH_S3_ACCESS_KEY_ID"),
    ],
)
async def test_a_wrong_setting_is_named_and_the_secret_is_not(
    options: dict[str, Any], name: str
) -> None:
    target = build_feed_target(_settings(**options))

    assert isinstance(target, UnusableTarget)
    with pytest.raises(PublishError, match=f"^{name}: ") as raised:
        await target.read()
    assert SECRET not in str(raised.value)
    assert raised.value.retryable is False

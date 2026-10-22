"""The object storage target against a real bucket. It runs only when it is asked for, with the
settings of the bucket in the environment. In ``apps/api``:

    set -a; source ../../.env; set +a
    uv run pytest -m live -o addopts="" tests/publish/test_s3_live.py

``-o addopts=""`` lifts the rule that no test may leave this computer. The test writes an object
of its own next to the feed, ``unshelve-live-test.json``, and never touches the feed.
"""

import os
from datetime import UTC, datetime

import pytest

from unshelve.net.sigv4 import Credentials
from unshelve.publish.s3 import S3Target

pytestmark = pytest.mark.live

TEST_OBJECT = "unshelve-live-test.json"
NAMES = ("ENDPOINT", "REGION", "BUCKET", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY")


@pytest.fixture
def bucket() -> S3Target:
    values = {name: os.environ.get(f"UNSHELVE_PUBLISH_S3_{name}", "") for name in NAMES}
    if missing := [f"UNSHELVE_PUBLISH_S3_{name}" for name, value in values.items() if not value]:
        pytest.skip(f"needs {', '.join(missing)}")
    return S3Target(
        values["ENDPOINT"],
        region=values["REGION"],
        bucket=values["BUCKET"],
        key=TEST_OBJECT,
        credentials=Credentials(values["ACCESS_KEY_ID"], values["SECRET_ACCESS_KEY"]),
    )


async def test_a_real_bucket_takes_the_feed_and_gives_it_back(bucket: S3Target) -> None:
    # New bytes in each run, so the first publish always writes.
    feed = f'{{"version": 1, "items": [], "test": "{datetime.now(UTC).isoformat()}"}}\n'.encode()

    assert (await bucket.publish(feed)).changed
    assert await bucket.read() == feed
    assert not (await bucket.publish(feed)).changed

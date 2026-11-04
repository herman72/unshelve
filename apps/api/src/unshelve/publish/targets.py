"""The targets of the public feed (docs/adr/0004). Each one passes ``FeedTargetConformance``.

``UNSHELVE_PUBLISH_TARGET`` chooses one: ``none`` publishes nothing, ``file`` writes the feed
to ``UNSHELVE_PUBLISH_FILE``, ``git`` commits it to a repository (``publish/git.py``), ``s3``
puts it into an object storage (``publish/s3.py``).
"""

import asyncio
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

from unshelve.config import Settings
from unshelve.contracts.dto import Published
from unshelve.contracts.errors import PublishError
from unshelve.contracts.protocols import FeedTarget
from unshelve.net.sigv4 import Credentials
from unshelve.publish.git import GitTarget
from unshelve.publish.s3 import (
    S3Target,
    bucket_name,
    endpoint_address,
    key_id,
    object_key,
    region_name,
)

# Faults of the settings: a later try fails in the same way.
_LASTING = (PermissionError, IsADirectoryError, NotADirectoryError)


class FileTarget:
    """The feed as one file, for a site that is served from the same computer.

    A write goes to a temporary file in the same folder, and then gets its name in one step
    (``os.replace``): a reader never sees half a feed. The file can be read by other users
    (mode 644), such as the user of a web server.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    async def publish(self, data: bytes) -> Published:
        return await asyncio.to_thread(self._publish, data)

    async def read(self) -> bytes | None:
        return await asyncio.to_thread(self._read)

    def _read(self) -> bytes | None:
        try:
            return self._path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise _error("read", self._path, error) from error

    def _publish(self, data: bytes) -> Published:
        if self._read() == data:
            return Published(where=str(self._path), changed=False)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(dir=self._path.parent, prefix=".feed-")
            try:
                with os.fdopen(handle, "wb") as file:
                    file.write(data)
                    file.flush()
                    os.fsync(file.fileno())
                # mkstemp makes a file that only its owner can read.
                Path(temporary).chmod(0o644)
                Path(temporary).replace(self._path)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise
        except OSError as error:
            raise _error("write", self._path, error) from error
        return Published(where=str(self._path), changed=True)


def _error(action: str, path: Path, error: OSError) -> PublishError:
    return PublishError(
        f"cannot {action} the feed at {path}: {error.strerror or error}",
        retryable=not isinstance(error, _LASTING),
    )


class UnusableTarget:
    """The target of wrong settings. Each call fails with the reason as a lasting fault: the
    job of the feed ends with it, and the other jobs of the worker go on."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    async def publish(self, data: bytes) -> Published:
        raise PublishError(self._reason, retryable=False)

    async def read(self) -> bytes | None:
        raise PublishError(self._reason, retryable=False)


def build_feed_target(settings: Settings) -> FeedTarget | None:
    """The target of ``UNSHELVE_PUBLISH_TARGET``, or None when nothing is published."""
    match settings.publish_target:
        case "file":
            return FileTarget(settings.publish_file)
        case "git":
            return _git_target(settings)
        case "s3":
            return _s3_target(settings)
        case "none":
            return None


def _git_target(settings: Settings) -> FeedTarget:
    if settings.publish_git_url is None:
        return UnusableTarget(
            "UNSHELVE_PUBLISH_TARGET is git, but UNSHELVE_PUBLISH_GIT_URL is not set"
        )
    try:
        return GitTarget(
            settings.publish_git_url,
            branch=settings.publish_git_branch,
            path=settings.publish_git_path,
            workdir=settings.publish_git_dir,
            ssh_key=settings.publish_git_ssh_key,
            known_hosts=settings.publish_git_known_hosts,
            author_name=settings.publish_git_author_name,
            author_email=settings.publish_git_author_email,
        )
    except ValueError as error:
        return UnusableTarget(f"UNSHELVE_PUBLISH_GIT_PATH: {error}")


def _s3_target(settings: Settings) -> FeedTarget:
    secret = settings.publish_s3_secret_access_key
    needed = {
        "ENDPOINT": settings.publish_s3_endpoint or "",
        "REGION": settings.publish_s3_region or "",
        "BUCKET": settings.publish_s3_bucket or "",
        "ACCESS_KEY_ID": settings.publish_s3_access_key_id or "",
        "SECRET_ACCESS_KEY": secret.get_secret_value() if secret is not None else "",
    }
    if missing := [f"UNSHELVE_PUBLISH_S3_{name}" for name, value in needed.items() if not value]:
        verb = "is" if len(missing) == 1 else "are"
        return UnusableTarget(
            f"UNSHELVE_PUBLISH_TARGET is s3, but {', '.join(missing)} {verb} not set"
        )
    # Each wrong setting by its name, the first one first.
    checks: tuple[tuple[str, Callable[[str], str], str], ...] = (
        ("ENDPOINT", endpoint_address, needed["ENDPOINT"]),
        ("REGION", region_name, needed["REGION"]),
        ("BUCKET", bucket_name, needed["BUCKET"]),
        ("OBJECT", object_key, settings.publish_s3_object),
        ("ACCESS_KEY_ID", key_id, needed["ACCESS_KEY_ID"]),
    )
    for name, check, value in checks:
        try:
            check(value)
        except ValueError as error:
            return UnusableTarget(f"UNSHELVE_PUBLISH_S3_{name}: {error}")
    return S3Target(
        needed["ENDPOINT"],
        region=needed["REGION"],
        bucket=needed["BUCKET"],
        key=settings.publish_s3_object,
        credentials=Credentials(needed["ACCESS_KEY_ID"], needed["SECRET_ACCESS_KEY"]),
    )

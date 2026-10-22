"""The feed as one file in a git repository, such as the repository of your site (docs/learn/43).

The worker keeps a working copy under ``workdir``. Each publish:

1. fetches the branch, and puts the copy at the state of the remote (local changes are lost:
   the remote is the truth);
2. writes the feed, and stops when the file holds these bytes already;
3. commits the file, and pushes the commit.

When another push came first, the remote refuses ours. Then the target starts again from 1,
at most ``TRIES`` times. A commit of someone else is never lost: the feed goes on top of it.

Git runs with no git config of this computer (no prompt, no signing, no credential helper),
so it does the same on each computer. A deploy key goes in through ``GIT_SSH_COMMAND``.
"""

import asyncio
import fcntl
import hashlib
import os
import re
import shlex
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath

from unshelve.contracts.dto import Published
from unshelve.contracts.errors import PublishError

# Each try fetches and pushes again. More than 3 pushes in the time of one try is no race.
TRIES = 3
# One git command; a fetch over a slow network fits.
COMMAND_TIMEOUT = 60.0
AUTHOR_NAME = "Unshelve"
AUTHOR_EMAIL = "unshelve@localhost"

# The user and password (or token) of an address: "https://user:token@host" shows "***@host".
_USERINFO = re.compile(r"(?<=://)[^/@\s]+@")
# A refused push, because the branch moved since the fetch.
_BEHIND = re.compile(r"\[rejected\]|non-fast-forward|fetch first|stale info")
# A fetch of a branch that does not exist yet.
_NO_BRANCH = "couldn't find remote ref"
# Faults of the settings: a later try fails in the same way.
_LASTING = re.compile(
    r"does not appear to be a git repository|repository .* not found|Repository not found"
    r"|Permission denied|Authentication failed|Host key verification failed"
    r"|could not read Username|terminal prompts disabled",
    re.IGNORECASE,
)


def hide_secrets(text: str) -> str:
    """``text`` without the user and password of each address in it."""
    return _USERINFO.sub("***@", text)


def feed_path(text: str) -> PurePosixPath:
    """The path of the feed in the repository: relative, inside it, and not in ``.git``.

    Raises ``ValueError``: a wrong path is a fault of the settings.
    """
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts or path.parts[0] == ".git":
        msg = f"the path of the feed must be a relative path inside the repository, not {text!r}"
        raise ValueError(msg)
    return path


def ssh_command(key: Path, known_hosts: Path, *, pinned: bool) -> str:
    """The ssh command of git: only this key, never a prompt, and the known hosts of the
    settings (``pinned``) or of the first connection (``accept-new``)."""
    return shlex.join(
        [
            "ssh",
            "-i",
            str(key),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-o",
            f"StrictHostKeyChecking={'yes' if pinned else 'accept-new'}",
        ]
    )


class _GitError(Exception):
    """A git command ended with an error. ``text`` is what git said, without secrets."""

    def __init__(self, command: str, text: str) -> None:
        super().__init__(f"git {command}: {text}")
        self.command = command
        self.text = text


class GitTarget:
    def __init__(
        self,
        url: str,
        *,
        branch: str,
        path: str,
        workdir: Path,
        ssh_key: Path | None = None,
        known_hosts: Path | None = None,
    ) -> None:
        self._url = url
        self._branch = branch
        self._path = feed_path(path)
        self._repo = workdir / "repo"
        self._ssh = workdir / "ssh"
        self._lock_file = workdir / "lock"
        self._ssh_key = ssh_key
        self._known_hosts = known_hosts
        # The environment of git, made when the lock is taken (``_locked``).
        self._env: dict[str, str] = {}

    @property
    def where(self) -> str:
        return f"{hide_secrets(self._url)} {self._branch}:{self._path}"

    async def publish(self, data: bytes) -> Published:
        async with self._locked():
            try:
                for _ in range(TRIES):
                    published = await self._try(data)
                    if published is not None:
                        return published
            except _GitError as error:
                raise self._fault(error) from None
        msg = f"the branch {self._branch} changed during each of {TRIES} tries ({self.where})"
        raise PublishError(msg, retryable=True)

    async def read(self) -> bytes | None:
        async with self._locked():
            try:
                exists = await self._sync()
            except _GitError as error:
                raise self._fault(error) from None
            return _read(self._repo / self._path) if exists else None

    async def _try(self, data: bytes) -> Published | None:
        """One try: None when the branch moved during it."""
        exists = await self._sync()
        file = self._repo / self._path
        if exists and _read(file) == data:
            return Published(where=self.where, changed=False)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)
        await self._git("add", "--", str(self._path))
        digest = hashlib.sha256(data).hexdigest()[:12]
        await self._git("commit", "--quiet", "-m", f"Update the public feed ({digest})")
        try:
            await self._git("push", "--quiet", "origin", f"HEAD:refs/heads/{self._branch}")
        except _GitError as error:
            if _BEHIND.search(error.text):
                return None
            raise
        commit = (await self._git("rev-parse", "--short=12", "HEAD")).strip()
        return Published(where=f"{self.where} at {commit}", changed=True)

    async def _sync(self) -> bool:
        """Put the working copy at the state of the remote branch. False: no such branch yet."""
        if not (self._repo / ".git").exists():
            self._repo.mkdir(parents=True, exist_ok=True)
            await self._git("init", "--quiet")
            await self._git("remote", "add", "origin", self._url)
        # A worker that stopped during a git command leaves this file, and then each command
        # fails. This copy is ours alone while we hold the lock, so the file is stale.
        (self._repo / ".git" / "index.lock").unlink(missing_ok=True)
        # The address may have changed in the settings since the last run.
        await self._git("remote", "set-url", "origin", self._url)
        branch = f"refs/heads/{self._branch}"
        try:
            await self._git("fetch", "--quiet", "--depth", "1", "origin", branch)
        except _GitError as error:
            if _NO_BRANCH not in error.text:
                raise
            # A new branch: start from no files, and the first commit makes the branch.
            await self._git("symbolic-ref", "HEAD", branch)
            await self._git("update-ref", "-d", branch)
            await self._git("rm", "-r", "-q", "--cached", "--ignore-unmatch", ".")
            await self._git("clean", "-f", "-f", "-d", "-x", "-q")
            return False
        await self._git("checkout", "--quiet", "--force", "-B", self._branch, "FETCH_HEAD")
        await self._git("clean", "-f", "-f", "-d", "-x", "-q")
        return True

    def _fault(self, error: _GitError) -> PublishError:
        return PublishError(
            f"git {error.command} failed for {self.where}: {error.text}",
            retryable=not _LASTING.search(error.text),
        )

    async def _git(self, *args: str) -> str:
        """Run git in the working copy. Raises ``_GitError`` when git ends with an error, and
        ``PublishError`` when git cannot run or takes too long."""
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                *args,
                cwd=self._repo,
                env=self._env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise PublishError(f"cannot run git: {error}", retryable=False) from error
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), COMMAND_TIMEOUT)
        except TimeoutError:
            process.kill()
            await process.wait()
            msg = f"git {args[0]} took more than {COMMAND_TIMEOUT:.0f} s ({self.where})"
            raise PublishError(msg, retryable=True) from None
        if process.returncode != 0:
            raise _GitError(args[0], hide_secrets(stderr.decode(errors="replace").strip()))
        return stdout.decode(errors="replace")

    def _environment(self) -> dict[str, str]:
        environment = {
            **os.environ,
            # The git config of this computer stays out: no prompt, no signing, no helper.
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            # English messages, so that a fault is known by its words.
            "LC_ALL": "C",
            "GIT_AUTHOR_NAME": AUTHOR_NAME,
            "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL,
            "GIT_COMMITTER_NAME": AUTHOR_NAME,
            "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL,
        }
        if self._ssh_key is not None:
            environment["GIT_SSH_COMMAND"] = ssh_command(
                self._private_copy(self._ssh_key),
                self._known_hosts or self._ssh / "known_hosts",
                pinned=self._known_hosts is not None,
            )
        return environment

    def _private_copy(self, key: Path) -> Path:
        """A copy of ``key`` that only this user can read. ssh refuses a key that others can
        read, and a key mounted into a container often keeps the owner of the host."""
        try:
            secret = key.read_bytes()
        except OSError as error:
            msg = f"cannot read the deploy key {key}: {error.strerror or error}"
            raise PublishError(msg, retryable=False) from error
        self._ssh.mkdir(parents=True, exist_ok=True)
        private = self._ssh / "deploy_key"
        private.touch(mode=0o600, exist_ok=True)
        private.chmod(0o600)
        private.write_bytes(secret)
        return private

    @asynccontextmanager
    async def _locked(self) -> AsyncGenerator[None]:
        """One publish at a time for this working copy, also from two workers."""
        self._lock_file.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_file.open("a") as handle:
            await asyncio.to_thread(fcntl.flock, handle, fcntl.LOCK_EX)
            try:
                self._env = self._environment()
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None

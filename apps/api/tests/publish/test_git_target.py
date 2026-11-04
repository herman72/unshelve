"""The git target of the public feed, against bare repositories on this computer. Each test name
is one rule."""

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from tests.publish.support import bare_remote, run_git
from unshelve.config import Settings
from unshelve.contracts.errors import PublishError
from unshelve.contracts.protocols import FeedTarget
from unshelve.publish import git as git_target
from unshelve.publish.git import GitTarget, feed_path, ssh_command
from unshelve.publish.targets import UnusableTarget, build_feed_target
from unshelve.testing.conformance import FeedTargetConformance

FEED = b'{\n  "version": 1,\n  "items": []\n}\n'
NEWER = '{\n  "version": 1,\n  "items": [{"title": "صف کارها"}]\n}\n'.encode()


def _push_as_someone_else(remote: Path, files: dict[str, str], branch: str = "main") -> None:
    """Commit ``files`` on ``branch`` from a clone of its own, as another writer would."""
    clone = Path(tempfile.mkdtemp(dir=remote.parent))
    run_git("clone", "--quiet", str(remote), str(clone), cwd=remote.parent)
    for name, text in files.items():
        (clone / name).parent.mkdir(parents=True, exist_ok=True)
        (clone / name).write_text(text)
    run_git("add", "--all", cwd=clone)
    run_git("commit", "--quiet", "-m", "A page of the site", cwd=clone)
    run_git("push", "--quiet", "origin", f"HEAD:refs/heads/{branch}", cwd=clone)


def _target(remote: Path | str, root: Path, **options: Any) -> GitTarget:
    settings: dict[str, Any] = {"branch": "main", "path": "public/feed.json"} | options
    return GitTarget(str(remote), workdir=root / "work", **settings)


def _commits(remote: Path, branch: str = "main") -> int:
    return int(run_git("rev-list", "--count", branch, cwd=remote))


class TestGitTarget(FeedTargetConformance):
    @pytest.fixture
    def target(self, tmp_path: Path) -> FeedTarget:
        return _target(bare_remote(tmp_path), tmp_path)


async def test_the_feed_is_one_commit_on_top_of_the_site(tmp_path: Path) -> None:
    remote = bare_remote(tmp_path)
    _push_as_someone_else(remote, {"index.html": "<h1>My site</h1>\n"})

    published = await _target(remote, tmp_path).publish(FEED)

    assert run_git("show", "main:public/feed.json", cwd=remote).encode() == FEED
    assert run_git("show", "main:index.html", cwd=remote) == "<h1>My site</h1>\n"
    assert _commits(remote) == 2
    author, subject = run_git("log", "-1", "--format=%an <%ae>|%s", "main", cwd=remote).split("|")
    assert author == "Unshelve <unshelve@localhost>"
    assert subject.startswith("Update the public feed (")
    commit = run_git("rev-parse", "--short=12", "main", cwd=remote).strip()
    assert published.where == f"{remote} main:public/feed.json at {commit}"


async def test_the_same_bytes_make_no_commit(tmp_path: Path) -> None:
    remote = bare_remote(tmp_path)
    target = _target(remote, tmp_path)

    await target.publish(FEED)
    again = await target.publish(FEED)

    assert again.changed is False
    assert _commits(remote) == 1


async def test_a_new_branch_starts_with_the_feed_alone(tmp_path: Path) -> None:
    remote = bare_remote(tmp_path)
    _push_as_someone_else(remote, {"index.html": "<h1>My site</h1>\n"})

    await _target(remote, tmp_path, branch="feed").publish(FEED)

    assert run_git("ls-tree", "-r", "--name-only", "feed", cwd=remote) == "public/feed.json\n"
    assert _commits(remote, "main") == 1  # the branch of the site is as it was


async def test_a_push_that_loses_a_race_is_tried_again_on_top(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = bare_remote(tmp_path)
    target = _target(remote, tmp_path)
    run = target._git  # pyright: ignore[reportPrivateUsage]
    pushes: list[str] = []

    async def push_after_someone_else(*args: str) -> str:
        if args[0] == "push":
            pushes.append(args[-1])
            if len(pushes) == 1:  # another writer pushes between our fetch and our push
                _push_as_someone_else(remote, {"about.html": "<p>About</p>\n"})
        return await run(*args)

    monkeypatch.setattr(target, "_git", push_after_someone_else)
    published = await target.publish(FEED)

    assert len(pushes) == 2
    assert published.changed
    assert _commits(remote) == 2  # the commit of the other writer, then the feed on top
    assert run_git("show", "main:about.html", cwd=remote) == "<p>About</p>\n"
    assert run_git("show", "main:public/feed.json", cwd=remote).encode() == FEED


async def test_a_race_in_each_try_is_a_fault_that_can_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = bare_remote(tmp_path)
    target = _target(remote, tmp_path)
    run = target._git  # pyright: ignore[reportPrivateUsage]
    races: list[int] = []

    async def always_too_late(*args: str) -> str:
        if args[0] == "push":
            races.append(1)
            _push_as_someone_else(remote, {f"page-{len(races)}.html": "<p>Again</p>\n"})
        return await run(*args)

    monkeypatch.setattr(target, "_git", always_too_late)
    with pytest.raises(PublishError, match="changed during each of 3 tries") as raised:
        await target.publish(FEED)

    assert raised.value.retryable is True
    assert len(races) == git_target.TRIES


async def test_two_publishes_at_once_take_turns(tmp_path: Path) -> None:
    remote = bare_remote(tmp_path)
    # Two workers, one working copy: the lock lets one of them in at a time.
    first, second = _target(remote, tmp_path), _target(remote, tmp_path)

    results = await asyncio.gather(first.publish(FEED), second.publish(NEWER))

    assert all(result.changed for result in results)
    assert _commits(remote) == 2
    assert run_git("show", "main:public/feed.json", cwd=remote).encode() in {FEED, NEWER}


async def test_a_lock_file_of_a_stopped_git_does_not_block_the_next_publish(
    tmp_path: Path,
) -> None:
    remote = bare_remote(tmp_path)
    target = _target(remote, tmp_path)
    await target.publish(FEED)
    (tmp_path / "work" / "repo" / ".git" / "index.lock").write_text("")

    assert (await target.publish(NEWER)).changed
    assert run_git("show", "main:public/feed.json", cwd=remote).encode() == NEWER


async def test_the_working_copy_holds_the_files_of_the_branch_alone(tmp_path: Path) -> None:
    remote = bare_remote(tmp_path)
    _push_as_someone_else(remote, {"index.html": "<h1>My site</h1>\n"})
    target = _target(remote, tmp_path)
    await target.publish(FEED)
    (tmp_path / "work" / "repo" / "stray.txt").write_text("left by a stopped run\n")

    await target.publish(NEWER)

    files = sorted(
        str(path.relative_to(tmp_path / "work" / "repo"))
        for path in (tmp_path / "work" / "repo").rglob("*")
        if path.is_file() and ".git" not in path.parts
    )
    assert files == ["index.html", "public/feed.json"]


async def test_a_branch_deleted_on_the_server_starts_again_from_no_files(
    tmp_path: Path,
) -> None:
    remote = bare_remote(tmp_path)
    target = _target(remote, tmp_path)
    await target.publish(FEED)
    run_git("update-ref", "-d", "refs/heads/main", cwd=remote)

    await target.publish(NEWER)

    assert _commits(remote) == 1


async def test_a_new_address_in_the_settings_is_used_at_once(tmp_path: Path) -> None:
    old, new = bare_remote(tmp_path / "old"), bare_remote(tmp_path / "new")
    await _target(old, tmp_path).publish(FEED)

    await _target(new, tmp_path).publish(NEWER)  # the same working copy

    assert run_git("show", "main:public/feed.json", cwd=new).encode() == NEWER


async def test_the_git_config_of_this_computer_stays_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A config that breaks each commit: it asks for a signature from a program that fails.
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = false\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    remote = bare_remote(tmp_path)

    assert (await _target(remote, tmp_path).publish(FEED)).changed


async def test_git_reaches_the_server_with_the_key_and_the_hosts_of_the_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A pretend ssh, first on the PATH: it writes down how git called it, and fails.
    bin_dir, calls = tmp_path / "bin", tmp_path / "ssh-calls"
    bin_dir.mkdir()
    (bin_dir / "ssh").write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {calls}\nexit 255\n')
    (bin_dir / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    key, hosts = tmp_path / "deploy_key", tmp_path / "known_hosts"
    key.write_text("made-up key\n")
    hosts.write_text("example.invalid ssh-ed25519 AAAAmadeup\n")
    target = build_feed_target(
        Settings(
            env="test",
            publish_target="git",
            publish_git_url="git@example.invalid:someone/site.git",
            publish_git_dir=tmp_path / "work",
            publish_git_ssh_key=key,
            publish_git_known_hosts=hosts,
        )
    )
    assert target is not None

    with pytest.raises(PublishError, match="git fetch failed"):
        await target.publish(FEED)

    arguments = calls.read_text().splitlines()
    assert arguments[:2] == ["-i", str(tmp_path / "work" / "ssh" / "deploy_key")]
    assert "StrictHostKeyChecking=yes" in arguments
    assert f"UserKnownHostsFile={hosts}" in arguments
    assert "git@example.invalid" in arguments


async def test_a_repository_that_does_not_exist_is_a_lasting_fault(tmp_path: Path) -> None:
    target = _target(tmp_path / "no-such-site.git", tmp_path)

    with pytest.raises(PublishError, match="does not appear to be a git repository") as raised:
        await target.publish(FEED)

    assert raised.value.retryable is False


async def test_a_token_in_the_address_is_in_no_message(tmp_path: Path) -> None:
    # Nothing listens on port 1: git fails at once, with the address in its message.
    target = _target("https://someone:SECRET-TOKEN@127.0.0.1:1/site.git", tmp_path)

    with pytest.raises(PublishError) as raised:
        await target.publish(FEED)

    assert "SECRET-TOKEN" not in str(raised.value)
    assert "https://***@127.0.0.1:1/site.git" in str(raised.value)
    assert raised.value.retryable is True  # a server that does not answer can come back
    assert "SECRET-TOKEN" not in target.where


@pytest.mark.parametrize(
    "path", ["", "/etc/feed.json", "../feed.json", "public/../../feed.json", ".git/config"]
)
def test_the_feed_stays_inside_the_repository(path: str) -> None:
    with pytest.raises(ValueError, match="relative path inside the repository"):
        feed_path(path)


def test_ssh_uses_the_key_alone_and_never_asks() -> None:
    pinned = ssh_command(Path("/keys/deploy"), Path("/keys/known_hosts"), pinned=True)
    first_use = ssh_command(Path("/keys/deploy"), Path("/work/known_hosts"), pinned=False)

    assert pinned == (
        "ssh -i /keys/deploy -o IdentitiesOnly=yes -o BatchMode=yes "
        "-o UserKnownHostsFile=/keys/known_hosts -o StrictHostKeyChecking=yes"
    )
    assert first_use.endswith(
        "-o UserKnownHostsFile=/work/known_hosts -o StrictHostKeyChecking=accept-new"
    )


async def test_the_deploy_key_is_copied_for_this_user_alone(tmp_path: Path) -> None:
    key = tmp_path / "mounted-key"
    key.write_text("made-up key\n")
    key.chmod(0o644)  # as a key mounted from the host can be

    await _target(bare_remote(tmp_path), tmp_path, ssh_key=key).publish(FEED)

    copy = tmp_path / "work" / "ssh" / "deploy_key"
    assert copy.read_text() == "made-up key\n"
    assert copy.stat().st_mode & 0o777 == 0o600


async def test_a_deploy_key_that_cannot_be_read_is_a_lasting_fault(tmp_path: Path) -> None:
    target = _target(bare_remote(tmp_path), tmp_path, ssh_key=tmp_path / "no-key")

    with pytest.raises(PublishError, match="cannot read the deploy key") as raised:
        await target.publish(FEED)

    assert raised.value.retryable is False


def test_the_settings_choose_the_git_target(tmp_path: Path) -> None:
    settings = Settings(
        env="test",
        publish_target="git",
        publish_git_url="git@github.com:someone/site.git",
        publish_git_dir=tmp_path,
    )

    assert isinstance(build_feed_target(settings), GitTarget)


async def test_the_author_of_the_settings_writes_and_commits_the_feed(tmp_path: Path) -> None:
    # A host that builds the site checks the author: on the Hobby plan of Vercel, a commit to a
    # private repository must come from the owner of the account.
    remote = bare_remote(tmp_path)
    target = build_feed_target(
        Settings(
            env="test",
            publish_target="git",
            publish_git_url=str(remote),
            publish_git_dir=tmp_path / "work",
            publish_git_author_name="Site Owner",
            publish_git_author_email="12345+site-owner@users.noreply.github.com",
        )
    )
    assert target is not None

    await target.publish(FEED)

    people = run_git("log", "-1", "--format=%an <%ae>|%cn <%ce>", "main", cwd=remote).strip()
    assert people.split("|") == ["Site Owner <12345+site-owner@users.noreply.github.com>"] * 2


async def test_an_author_with_no_name_is_a_lasting_fault(tmp_path: Path) -> None:
    target = _target(bare_remote(tmp_path), tmp_path, author_name=" ")

    with pytest.raises(PublishError, match="name consists only of disallowed characters") as raised:
        await target.publish(FEED)

    assert raised.value.retryable is False


@pytest.mark.parametrize(
    ("options", "reason"),
    [
        ({}, "UNSHELVE_PUBLISH_GIT_URL is not set"),
        (
            {"publish_git_url": "git@github.com:someone/site.git", "publish_git_path": "../x"},
            "UNSHELVE_PUBLISH_GIT_PATH",
        ),
    ],
)
async def test_wrong_git_settings_fail_each_publish_and_stop_nothing_else(
    options: dict[str, Any], reason: str
) -> None:
    target = build_feed_target(Settings(env="test", publish_target="git", **options))

    assert isinstance(target, UnusableTarget)
    with pytest.raises(PublishError, match=reason) as raised:
        await target.publish(FEED)
    assert raised.value.retryable is False

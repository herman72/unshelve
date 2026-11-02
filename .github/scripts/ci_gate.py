# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml==6.0.3"]
# ///
"""The gate of main.yml: which groups of CI a push to main runs (docs/learn/53).

A group runs when the push changed one of its files, and no green pull_request run of the group
tested the same tree. The owner merges by rebase, so main gets new commits, but their tree is the
tree that the pull request tested. Each doubt runs the group: a manual run, no green run of
main.yml to compare with, a failed call of the GitHub API, or a changed file that no list names.

Only runs of this repository count. In a public repository, anyone can open a pull request from
a fork, and its runs are in the same lists: a fork controls what such a run does, so its green
run proves nothing.

It writes `<group>=true|false` to $GITHUB_OUTPUT and a table of its reasons to the summary.
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"
# The files that key the cache of setup-uv, and the cache of pnpm in setup-node. A new pull request
# restores the cache of main, and main saves a cache only when a job runs there. So a group runs on
# main after a change of a file that keys one of its caches, also after a proof.
UV_KEYS = ("apps/api/uv.lock", "apps/api/pyproject.toml", "apps/api/.python-version")
PNPM_KEYS = ("pnpm-lock.yaml",)
# The files that no job of CI reads. A changed file that no list of a group selects, and that is
# not here, runs each group without a proof: a new file of a tool (.npmrc, uv.toml, .gitattributes)
# can change any job before a list names it. A pull request with only such a file runs no group.
NOT_READ_BY_CI = (
    "docs/**",
    "README.md",
    "CONTRIBUTING.md",
    "LICENSE",
    ".editorconfig",
    ".pre-commit-config.yaml",
)


@dataclass(frozen=True)
class Group:
    paths: list[str]  # the paths of its pull_request trigger
    cache_keys: tuple[str, ...]  # the files that key a cache that a job of the group restores


def run(*command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603 - git and gh


def git(*args: str) -> str:
    done = run("git", *args)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout.strip()


def api(path: str) -> Any:
    """The answer of the GitHub API, or None when the call failed."""
    done = run("gh", "api", path)
    if done.returncode != 0:
        sys.stderr.write(f"gh api {path} failed: {done.stderr.strip()}\n")
        return None
    return json.loads(done.stdout)


def selects(paths: list[str] | tuple[str, ...], file: str) -> bool:
    """The match of GitHub for the two forms of the lists: a file, or a folder and `/**`."""
    return any(
        file.startswith(path.removesuffix("**")) if path.endswith("/**") else file == path
        for path in paths
    )


def cache_keys(step: dict[str, Any]) -> tuple[str, ...]:
    """The files that key the cache that a step restores, or none."""
    uses = str(step.get("uses", ""))
    options: dict[str, Any] = step.get("with") or {}
    if uses.startswith("astral-sh/setup-uv@") and options.get("enable-cache") is not False:
        return UV_KEYS
    if uses.startswith("actions/setup-node@") and options.get("cache") == "pnpm":
        return PNPM_KEYS
    return ()


def groups() -> dict[str, Group]:
    """Each group that main.yml calls."""
    main: dict[str, Any] = yaml.safe_load((WORKFLOWS / "main.yml").read_text())
    found: dict[str, Group] = {}
    for name, job in main["jobs"].items():
        if "uses" in job:
            file = WORKFLOWS / Path(job["uses"]).name
            workflow: dict[Any, Any] = yaml.safe_load(file.read_text())
            steps = [step for one in workflow["jobs"].values() for step in one.get("steps", [])]
            found[name] = Group(
                # YAML 1.1 reads the key `on` as the boolean true. GitHub reads it as the word.
                paths=workflow[True]["pull_request"]["paths"],
                cache_keys=tuple(sorted({key for step in steps for key in cache_keys(step)})),
            )
    return found


def ours(one: dict[str, Any], repo: str) -> bool:
    """Whether a run comes from a branch of this repository, not from a fork."""
    head: dict[str, Any] = one.get("head_repository") or {}
    return head.get("full_name") == repo


def last_green_main(repo: str) -> str | None:
    """The commit of the last green run of main.yml on main, if this clone has it."""
    workflow = f"repos/{repo}/actions/workflows/main.yml/runs"
    # Only a push runs main.yml on main. A pull request from a fork can add a trigger to main.yml,
    # and its branch can be named main too.
    answer = api(f"{workflow}?branch=main&event=push&status=success&per_page=1")
    runs: list[dict[str, Any]] = answer["workflow_runs"] if answer else []
    if not runs or not ours(runs[0], repo):
        return None
    sha = str(runs[0]["head_sha"])
    # A rewritten history can lose that commit: then there is nothing to compare with.
    return sha if run("git", "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0 else None


def proof(repo: str, group: str, tree: str) -> int | None:
    """The id of a green pull_request run of the group on this tree, or None."""
    workflow = f"repos/{repo}/actions/workflows/{group}.yml/runs"
    answer = api(f"{workflow}?event=pull_request&status=success&per_page=100")
    runs: list[dict[str, Any]] = answer["workflow_runs"] if answer else []
    for one in runs:
        if ours(one, repo) and one["head_commit"]["tree_id"] == tree:
            return int(one["id"])
    return None


def decide(repo: str, event: str) -> dict[str, tuple[bool, str]]:
    """For each group: whether it runs, and why."""
    all_groups = groups()
    if event == "workflow_dispatch":
        return dict.fromkeys(all_groups, (True, "a manual run runs each group"))
    base = last_green_main(repo)
    if base is None:
        return dict.fromkeys(all_groups, (True, "no green run of main.yml to compare with"))
    # Without renames, a moved file counts at its old place and at its new place.
    changed = git("diff", "--name-only", "--no-renames", base, "HEAD").splitlines()
    tree = git("rev-parse", "HEAD^{tree}")
    unknown = [
        file
        for file in changed
        if not selects(NOT_READ_BY_CI, file)
        and not any(selects(group.paths, file) for group in all_groups.values())
    ]
    decisions: dict[str, tuple[bool, str]] = {}
    for name, group in all_groups.items():
        mine = [file for file in changed if selects(group.paths, file)]
        keys = [file for file in mine if file in group.cache_keys]
        if not mine and not unknown:
            decisions[name] = (False, f"no file of the group changed since {base[:7]}")
        elif keys:
            decisions[name] = (True, f"{keys[0]} changed: main saves the new cache")
        elif (found := proof(repo, name, tree)) is not None:
            decisions[name] = (False, f"pull request run {found} passed on this tree")
        elif mine:
            decisions[name] = (True, f"{len(mine)} of its files changed, and no proof")
        else:
            decisions[name] = (True, f"{unknown[0]} is in no list of CI, and no proof")
    return decisions


def main() -> None:
    decisions = decide(os.environ["GITHUB_REPOSITORY"], os.environ.get("GITHUB_EVENT_NAME", ""))
    rows = ["| Group | Runs | Why |", "|---|---|---|"]
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        for group, (runs, why) in decisions.items():
            output.write(f"{group}={str(runs).lower()}\n")
            rows.append(f"| {group} | {'yes' if runs else 'no'} | {why} |")
            sys.stdout.write(f"{group}: {'runs' if runs else 'skips'} ({why})\n")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a") as file:
            file.write("\n".join(rows) + "\n")


if __name__ == "__main__":
    main()

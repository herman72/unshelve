"""The workflows of CI: which files start which group (docs/learn/53). No workflow runs here.

A pull request runs a group only when it changes a file of the group. A file that a job reads, but
that no list names, would let a pull request break the job without a run. These tests name the
files outside each app that the jobs of each group read, and check the lists against them.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[4]
WORKFLOWS = REPO / ".github" / "workflows"
GROUPS = ("api", "web", "site", "stack")

# The files outside the app of each group that its jobs read: in a test, a build or a step.
READS = {
    "api": (
        # The tests of the stack files and of the guide.
        ".env.example",
        "compose.yaml",
        "compose.prod.yaml",
        "deploy/compose.yaml",
        "deploy/compose.prod.yaml",
        "deploy/quickstart-steps.sh",
        "deploy/Dockerfile.api",
        "deploy/backup/restore.sh",
        "docs/quickstart.md",
        # The tests of CI read each workflow.
        ".github/workflows/api.yml",
        ".github/workflows/main.yml",
        ".github/workflows/site.yml",
        ".github/workflows/stack.yml",
        ".github/workflows/web.yml",
        # The tests of the feed read the schema and the cases of the search package.
        "packages/site-search/schema/feed-v1.schema.json",
        "packages/site-search/fixtures/feed.json",
        "packages/site-search/src/u-norm-table.ts",
        "packages/site-search/test/u-norm-cases.json",
        # The contract check makes the types again and compares them.
        "Makefile",
        "packages/api-client/src/schema.d.ts",
        "package.json",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
    ),
    "web": (
        "packages/api-client/src/schema.d.ts",
        "packages/site-search/src/u-norm-table.ts",
        "examples/site/package.json",
        "package.json",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
    ),
    "site": (
        "packages/site-search/src/u-norm-table.ts",
        "package.json",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
    ),
    "stack": (
        # The images.
        "apps/api/uv.lock",
        "apps/web/package.json",
        "packages/api-client/src/schema.d.ts",
        "deploy/Dockerfile.api",
        "deploy/Dockerfile.web",
        ".dockerignore",
        "package.json",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
        # The guide, its files, and the data of the drill.
        "docs/quickstart.md",
        "deploy/quickstart-steps.sh",
        "deploy/check-instance.sh",
        "deploy/backup/drill-data.sh",
        "apps/api/tests/ingest/fixtures/export_made_up/messages.html",
        "compose.yaml",
        "compose.prod.yaml",
        ".env.example",
    ),
}


def _workflow(name: str) -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load((WORKFLOWS / f"{name}.yml").read_text())
    return loaded


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    # YAML 1.1 reads the key `on` as the boolean true. GitHub reads it as the word.
    triggers: dict[str, Any] = workflow[True]
    return triggers


def _paths(group: str) -> list[str]:
    paths: list[str] = _triggers(_workflow(group))["pull_request"]["paths"]
    return paths


def _selects(paths: list[str], file: str) -> bool:
    """The match of GitHub for the two forms of the lists: a file, or a folder and `/**`."""
    return any(
        file.startswith(path.removesuffix("**")) if path.endswith("/**") else file == path
        for path in paths
    )


@pytest.mark.parametrize("group", GROUPS)
def test_each_group_selects_the_files_that_its_jobs_read(group: str) -> None:
    paths = _paths(group)

    assert [file for file in READS[group] if not (REPO / file).is_file()] == []
    assert [file for file in READS[group] if not _selects(paths, file)] == []


@pytest.mark.parametrize("group", GROUPS)
def test_a_change_of_its_own_workflow_starts_a_group(group: str) -> None:
    assert _selects(_paths(group), f".github/workflows/{group}.yml")


@pytest.mark.parametrize("group", GROUPS)
def test_a_list_names_only_a_file_or_a_folder_that_exists(group: str) -> None:
    # The gate of main.yml matches these two forms as GitHub does. A pattern with another
    # wildcard could match one way on a pull request and another way on main.
    wrong = [
        path
        for path in _paths(group)
        if not (
            (REPO / path.removesuffix("/**")).is_dir()
            if path.endswith("/**")
            else (REPO / path).is_file()
        )
        or any(sign in path.removesuffix("/**") for sign in "*?[]!")
    ]

    assert wrong == []


def test_prose_starts_no_group_and_the_guide_starts_two() -> None:
    prose = (
        "README.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "docs/architecture.md",
        "docs/learn/52-stats-page.md",
        "docs/adr/0015-backups-a-logical-dump-and-a-mirror-of-the-files.md",
    )

    assert [(file, g) for file in prose for g in GROUPS if _selects(_paths(g), file)] == []
    # CI runs the marked blocks of the guide, and the tests of the API read it.
    assert [g for g in GROUPS if _selects(_paths(g), "docs/quickstart.md")] == ["api", "stack"]


def test_main_calls_each_group_and_no_other_workflow_exists() -> None:
    main = _workflow("main")

    assert {path.stem for path in WORKFLOWS.glob("*.y*ml")} == {"main", *GROUPS}
    assert {name: job["uses"] for name, job in main["jobs"].items() if "uses" in job} == {
        group: f"./.github/workflows/{group}.yml" for group in GROUPS
    }
    assert [group for group in GROUPS if "workflow_call" not in _triggers(_workflow(group))] == []


def test_a_group_cancels_only_the_older_runs_of_a_pull_request() -> None:
    for group in GROUPS:
        concurrency = _workflow(group)["concurrency"]

        # In a called workflow, github.workflow names the caller: a shared name would make the
        # group wait for its own caller.
        assert concurrency["group"] == f"{group}-${{{{ github.ref }}}}"
        assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    # A run on main waits for the run before it, and is never cancelled.
    assert _workflow("main")["concurrency"]["cancel-in-progress"] is False


def test_each_workflow_uses_the_same_tool_versions() -> None:
    versions: dict[str, set[str]] = {}
    for path in WORKFLOWS.glob("*.yml"):
        workflow: dict[Any, Any] = yaml.safe_load(path.read_text())
        for name, value in workflow.get("env", {}).items():
            versions.setdefault(name, set()).add(str(value))

    assert {name: values for name, values in versions.items() if len(values) > 1} == {}
    assert versions["NODE_VERSION"] == {"24"}

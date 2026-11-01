"""The gate of main.yml (.github/scripts/ci_gate.py), on a made-up history and a pretend GitHub.

Each test makes a small git repository with a base commit, the last green commit of main, and a
new commit on it. A pretend `gh` answers the calls of the gate from a table. The gate reads the
real lists of the four groups, so these tests also show what each list selects.
"""

import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[4]
GATE = REPO / ".github" / "scripts" / "ci_gate.py"
GROUPS = ("api", "web", "site", "stack")
FAKE_GH = """#!{python}
import json, sys

# gh api <path>: the first key that is in the path gives the answer; null is a failed call.
answers = json.load(open({answers!r}))
for key, answer in answers.items():
    if key in sys.argv[2]:
        if answer is None:
            sys.stderr.write("HTTP 502: Bad Gateway\\n")
            sys.exit(1)
        print(json.dumps(answer))
        sys.exit(0)
print(json.dumps({{"workflow_runs": []}}))
"""

Gate = Callable[..., dict[str, str]]


def _git(where: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603 - git with the arguments of a test
        ["git", "-c", "commit.gpgsign=false", *args],  # noqa: S607 - the git of this computer
        cwd=where,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def _write(where: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (where / name).parent.mkdir(parents=True, exist_ok=True)
        (where / name).write_text(text)


class History:
    """A repository whose first commit is the last green commit of main."""

    def __init__(self, where: Path) -> None:
        self.where = where
        _git(where, "init", "-q", "-b", "main")
        _git(where, "config", "user.name", "Test")
        _git(where, "config", "user.email", "test@example.com")
        _write(
            where,
            {
                "apps/api/src/app.py": "app = 1\n",
                "apps/api/uv.lock": "lock 1\n",
                "apps/web/src/page.tsx": "page 1\n",
                "examples/site/page.tsx": "site 1\n",
                "docs/learn/01-note.md": "note 1\n",
                "pnpm-lock.yaml": "lock 1\n",
            },
        )
        self.base = self.commit("base")

    def commit(self, message: str) -> str:
        _git(self.where, "add", "-A")
        _git(self.where, "commit", "-q", "--allow-empty", "-m", message)
        return _git(self.where, "rev-parse", "HEAD")

    def change(self, *names: str) -> None:
        for name in names:
            path = self.where / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{path.read_text() if path.exists() else ''}changed\n")
        self.commit("change")

    def tree(self, commit: str = "HEAD") -> str:
        return _git(self.where, "rev-parse", f"{commit}^{{tree}}")


@pytest.fixture
def history(tmp_path: Path) -> History:
    (tmp_path / "repo").mkdir()
    return History(tmp_path / "repo")


@pytest.fixture
def gate(tmp_path: Path, history: History) -> Gate:
    def run(event: str = "push", **answers: Any) -> dict[str, str]:
        """The outputs of the gate. Each keyword is a part of an API path, and its answer."""
        table = {key.replace("_", "."): value for key, value in answers.items()}
        table.setdefault("main.yml", {"workflow_runs": [{"head_sha": history.base}]})
        (tmp_path / "answers.json").write_text(json.dumps(table))
        bin_folder = tmp_path / "bin"
        bin_folder.mkdir(exist_ok=True)
        fake = bin_folder / "gh"
        answers_file = str(tmp_path / "answers.json")
        fake.write_text(FAKE_GH.format(python=sys.executable, answers=answers_file))
        fake.chmod(0o755)
        output = tmp_path / "output"
        output.write_text("")
        env = {
            "PATH": f"{bin_folder}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_EVENT_NAME": event,
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        }
        subprocess.run(  # noqa: S603 - the gate, with the Python of the tests
            [sys.executable, str(GATE)], cwd=history.where, env=env, check=True, capture_output=True
        )
        return dict(line.split("=", 1) for line in output.read_text().splitlines())

    return run


def _proof(run_id: int, tree: str) -> dict[str, Any]:
    """The answer of the API for a green pull_request run of a group on a tree."""
    return {"workflow_runs": [{"id": run_id, "head_commit": {"tree_id": tree}}]}


def _running(outputs: dict[str, str]) -> list[str]:
    assert set(outputs) == set(GROUPS)
    assert set(outputs.values()) <= {"true", "false"}
    return [group for group in GROUPS if outputs[group] == "true"]


def test_a_push_runs_only_the_groups_of_its_files(history: History, gate: Gate) -> None:
    history.change("apps/web/src/page.tsx")

    assert _running(gate()) == ["web", "stack"]


def test_a_pull_request_run_on_the_same_tree_proves_a_group(
    history: History, gate: Gate, tmp_path: Path
) -> None:
    history.change("apps/web/src/page.tsx")

    outputs = gate(web_yml=_proof(41, history.tree()), stack_yml=_proof(42, history.tree()))

    assert _running(outputs) == []
    assert (
        "| web | no | pull request run 41 passed on this tree |"
        in (tmp_path / "summary").read_text()
    )


def test_a_run_on_another_tree_proves_nothing(history: History, gate: Gate) -> None:
    history.change("apps/web/src/page.tsx")

    # The run of the pull request tested the base: its head was not the head of main.
    proof = _proof(41, history.tree(history.base))

    assert _running(gate(web_yml=proof, stack_yml=proof)) == ["web", "stack"]


@pytest.mark.parametrize(
    ("changed", "groups"),
    [
        # The stack builds images, and restores no cache of GitHub: its proof still counts.
        pytest.param("apps/api/uv.lock", ["api"], id="the lockfile of uv"),
        pytest.param("apps/api/pyproject.toml", ["api"], id="the project file of uv"),
        pytest.param("pnpm-lock.yaml", ["api", "web", "site"], id="the lockfile of pnpm"),
    ],
)
def test_a_lockfile_runs_the_groups_with_its_cache_also_after_a_proof(
    history: History, gate: Gate, changed: str, groups: list[str]
) -> None:
    history.change(changed)
    proofs = {f"{group}_yml": _proof(7, history.tree()) for group in GROUPS}

    # A new pull request restores the cache of main, and main saves the new one only in a run.
    assert _running(gate(**proofs)) == groups


def test_a_lockfile_does_not_run_a_group_with_another_cache(history: History, gate: Gate) -> None:
    history.change("apps/api/uv.lock", "apps/web/src/page.tsx")
    proofs = {f"{group}_yml": _proof(7, history.tree()) for group in GROUPS}

    # The cache of pnpm does not depend on the lockfile of uv, so the proof of web counts.
    assert _running(gate(**proofs)) == ["api"]


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ci_gate", GATE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_each_file_that_keys_a_cache_exists_and_starts_the_groups_of_the_cache() -> None:
    module = _module()
    groups = module.groups()
    uv_and_pnpm = (*sorted(module.UV_KEYS), *module.PNPM_KEYS)

    assert {name: group.cache_keys for name, group in groups.items()} == {
        "api": uv_and_pnpm,
        "web": module.PNPM_KEYS,
        "site": module.PNPM_KEYS,
        "stack": (),
    }
    assert [file for file in uv_and_pnpm if not (REPO / file).is_file()] == []
    # A key that the list of its group does not select would never run the group on main.
    assert [
        (name, file)
        for name, group in groups.items()
        for file in group.cache_keys
        if not module.selects(group.paths, file)
    ] == []


def test_each_file_of_the_repository_is_in_a_list_or_read_by_no_job() -> None:
    module = _module()
    groups = module.groups()
    tracked = subprocess.run(
        ["git", "ls-files"],  # noqa: S607 - the git of this computer
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()

    # A file here can change a job, but no pull request run starts for it. Add it to the list of
    # each group whose jobs read it, or to NOT_READ_BY_CI if no job reads it.
    assert [
        file
        for file in tracked
        if not module.selects(module.NOT_READ_BY_CI, file)
        and not any(module.selects(group.paths, file) for group in groups.values())
    ] == []


def test_a_changed_workflow_starts_its_group_and_the_tests_of_ci(
    history: History, gate: Gate
) -> None:
    history.change(".github/workflows/web.yml")

    assert _running(gate()) == ["api", "web"]
    assert (
        _running(gate(api_yml=_proof(8, history.tree()), web_yml=_proof(9, history.tree()))) == []
    )


def test_a_moved_file_counts_also_at_its_old_place(history: History, gate: Gate) -> None:
    _git(history.where, "mv", "apps/api/src/app.py", "docs/learn/app.py")
    history.commit("move")

    assert _running(gate()) == ["api", "stack"]


def test_a_folder_selects_only_its_own_files(history: History, gate: Gate, tmp_path: Path) -> None:
    # apps/web/** names the folder apps/web, not each name that starts with "apps/web".
    history.change("apps/website/page.tsx", "Makefile.local")

    assert _running(gate()) == list(GROUPS)
    # Each group runs because the files are in no list, not because they are its own.
    rows = (tmp_path / "summary").read_text().splitlines()
    assert [row for row in rows if "Makefile.local is in no list of CI, and no proof" in row] == [
        f"| {group} | yes | Makefile.local is in no list of CI, and no proof |" for group in GROUPS
    ]


def test_prose_runs_no_group(history: History, gate: Gate) -> None:
    history.change("docs/learn/01-note.md", "docs/learn/02-note.md", "README.md", "LICENSE")

    assert _running(gate()) == []


def test_a_file_in_no_list_runs_each_group_without_a_proof(history: History, gate: Gate) -> None:
    # A new file of a tool: pnpm reads it in each install, and no list names it yet.
    history.change(".npmrc")

    assert _running(gate()) == list(GROUPS)
    # A group that passed on this tree tested the new file too.
    assert _running(gate(web_yml=_proof(5, history.tree()))) == ["api", "site", "stack"]


@pytest.mark.parametrize(
    ("event", "answers"),
    [
        pytest.param("workflow_dispatch", {}, id="a manual run"),
        pytest.param("push", {"main_yml": {"workflow_runs": []}}, id="no green run of main"),
        pytest.param("push", {"main_yml": None}, id="a failed call for the runs of main"),
        pytest.param(
            "push",
            {"main_yml": {"workflow_runs": [{"head_sha": "1" * 40}]}},
            id="a green commit that this clone does not have",
        ),
    ],
)
def test_each_doubt_runs_each_group(
    history: History, gate: Gate, event: str, answers: dict[str, Any]
) -> None:
    history.change("docs/learn/01-note.md")
    proofs = {f"{group}_yml": _proof(9, history.tree()) for group in GROUPS}

    assert _running(gate(event, **proofs, **answers)) == list(GROUPS)


def test_a_failed_call_for_a_proof_runs_the_group(history: History, gate: Gate) -> None:
    history.change("apps/web/src/page.tsx")

    assert _running(gate(web_yml=None, stack_yml=_proof(42, history.tree()))) == ["web"]

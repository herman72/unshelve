"""The gate of main.yml (.github/scripts/ci_gate.py), on a made-up history and a pretend GitHub.

Each test makes a small git repository with a base commit, the last green commit of main, and a
new commit on it. A pretend `gh` answers the calls of the gate from a table. The gate reads the
real lists of the four groups, so these tests also show what each list selects.
"""

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
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
        pytest.param("pnpm-lock.yaml", ["api", "web", "site"], id="the lockfile of pnpm"),
    ],
)
def test_a_lockfile_runs_the_groups_with_a_cache_also_after_a_proof(
    history: History, gate: Gate, changed: str, groups: list[str]
) -> None:
    history.change(changed)
    proofs = {f"{group}_yml": _proof(7, history.tree()) for group in GROUPS}

    # A new pull request restores the cache of main, and main saves the new one only in a run.
    assert _running(gate(**proofs)) == groups


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


def test_a_folder_selects_only_its_own_files(history: History, gate: Gate) -> None:
    # apps/web/** names the folder apps/web, not each name that starts with "apps/web".
    history.change("apps/website/page.tsx", "Makefile.local")

    assert _running(gate()) == []


def test_prose_runs_no_group(history: History, gate: Gate) -> None:
    history.change("docs/learn/01-note.md", "docs/learn/02-note.md")

    assert _running(gate()) == []


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

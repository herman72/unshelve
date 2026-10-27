"""The steps of docs/quickstart.md, as deploy/quickstart-steps.sh prints them for CI.

The quickstart jobs of CI run these steps on a fresh clone, and that takes minutes. These tests
check in a second that the printer gives each marked block, in its order, without the
indentation of the list of the guide, so that a here-document ends where it must.
"""

import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[4]
PRINTER = REPO / "deploy" / "quickstart-steps.sh"
MARKS = (
    "quickstart-local",
    "quickstart-local-check",
    "quickstart-server",
    "quickstart-server-check",
    "quickstart-backup",
)


def _steps(mark: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - the printer of this repository
        [str(PRINTER), mark], capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize("mark", MARKS)
def test_each_mark_of_the_guide_gives_steps_that_bash_can_read(mark: str) -> None:
    done = _steps(mark)

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip()
    # Only the marked blocks: the block with `git clone` is for a person, not for CI.
    assert "git clone" not in done.stdout
    # No line keeps the indentation of the list in the guide.
    assert [line for line in done.stdout.splitlines() if line.startswith(" ")] == []
    # bash -n only warns about a here-document without its end, so its stderr must be empty.
    syntax = subprocess.run(
        ["bash", "-n"],  # noqa: S607 - the bash of this computer
        input=done.stdout,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (syntax.returncode, syntax.stderr) == (0, "")


def test_the_server_steps_write_the_settings_and_then_start_the_stack() -> None:
    lines = _steps("quickstart-server").stdout.splitlines()

    # The here-document ends at the start of a line: the commands after it are commands.
    assert lines.index("COMPOSE_FILE=compose.prod.yaml") < lines.index("EOF")
    assert lines.index("EOF") < lines.index("docker compose up -d --build --wait")


def test_a_mark_that_the_guide_does_not_have_stops_the_printer() -> None:
    done = _steps("quickstart-nowhere")

    assert done.returncode == 1
    assert "docs/quickstart.md has no block marked quickstart-nowhere" in done.stderr


def test_ci_runs_each_mark_with_bash_c_and_not_through_a_pipe() -> None:
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text())
    runs = [
        str(step.get("run", ""))
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "quickstart-steps.sh" in str(step.get("run", ""))
    ]

    assert sorted(run.split("quickstart-steps.sh ")[1].split(")")[0] for run in runs) == sorted(
        MARKS
    )
    # From a pipe, bash reads the script line by line, and `docker compose exec` reads the same
    # stdin: it would take the lines after it, and they would never run.
    assert [run for run in runs if not run.startswith('bash -euxo pipefail -c "$(')] == []

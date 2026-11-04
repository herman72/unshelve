"""The build context of the images: the files that `docker build` sends to the builder.

The context is the root of the clone, and .dockerignore names the files that stay out. A name in it
matches only at the root of the context: `var` keeps out ./var, but not apps/api/var, the state of
a worker that runs outside Docker. `COPY apps/api/ ./` of deploy/Dockerfile.api would then put that
state into the image of each service. The test asks the builder itself which files it gets, so a
pass does not depend on how a test reads the rules.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.deploy.support import REPO, docker_available

# `--output type=local` needs BuildKit. It writes the files of the build to a folder: no image is
# made, and FROM scratch pulls nothing.
pytestmark = pytest.mark.skipif(
    not docker_available("buildx", "version"), reason="needs docker buildx"
)

# The files that a worker outside Docker can leave in a clone (.gitignore, config.py): blobs of
# private files, and the working copy of the git target with a copy of its deploy key.
PRIVATE = (
    "var/blobs/ab/cdef",
    "apps/api/var/blobs/ab/cdef",
    "apps/api/var/publish/ssh/deploy_key",
    "apps/api/var/public/feed.json",
)
# Files that the image needs, and a name that only starts like a private folder.
KEPT = (
    "apps/api/pyproject.toml",
    "apps/api/src/unshelve/variables.py",
)


def test_docker_build_sends_no_private_file_at_any_depth(tmp_path: Path) -> None:
    # A made-up clone with the .dockerignore of this one: the files of a real checkout (with or
    # without a var/ of its own) cannot change the result.
    context = tmp_path / "context"
    for name in (*PRIVATE, *KEPT):
        file = context / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(name)
    shutil.copy(REPO / ".dockerignore", context / ".dockerignore")
    sent = tmp_path / "sent"

    done = subprocess.run(  # noqa: S603 - docker with the arguments of this test
        [  # noqa: S607 - the docker of this computer
            "docker",
            "build",
            "--quiet",
            "--file",
            "-",
            "--output",
            f"type=local,dest={sent}",
            str(context),
        ],
        input="FROM scratch\nCOPY . /\n",
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    files = sorted(path.relative_to(sent).as_posix() for path in sent.rglob("*") if path.is_file())
    # The builder gets .dockerignore too: a context holds the file that filters it.
    assert files == sorted((".dockerignore", *KEPT))

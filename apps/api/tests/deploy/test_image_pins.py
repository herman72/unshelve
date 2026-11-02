"""The images of the stack and of CI: each one at a tag and a digest. No image is pulled here.

A tag can move: the owner of an image can push it again with other content. A digest is the hash
of the content, so it cannot. Dependabot updates the tag and the digest together
(.github/dependabot.yml), but only in a FROM line and in the `image` of a Compose service. These
tests read the same places, and the images of the workflows, which Dependabot does not read: a
service or job container, and an image in an `env` value (actionlint). A `docker://` step fails
test_ci_workflows.py, which allows only an action at a commit.
"""

import re
from pathlib import Path
from typing import Any

import yaml

from tests.deploy.support import REPO

DEPLOY = REPO / "deploy"
WORKFLOWS = REPO / ".github" / "workflows"
# The digest of the index, the list of all platforms: a Mac (arm64) and CI (amd64) read the same
# index, and each takes the image of its own platform from it.
PINNED = re.compile(r"(?P<name>[a-z0-9./-]+):(?P<tag>[\w.-]+)@sha256:[0-9a-f]{64}")
FROM = re.compile(
    r"^\s*FROM\s+(?:--platform=\S+\s+)?(?P<image>\S+)(?:\s+AS\s+(?P<stage>\S+))?", re.I | re.M
)
# A value of `env` that names an image: a path with a slash, and a tag.
IMAGE_LIKE = re.compile(r"[a-z0-9][a-z0-9._-]*(?:/[a-z0-9._-]+)+:[\w.-]+(?:@\S+)?")


def _dockerfile_images(path: Path) -> list[str]:
    """The image of each FROM line. A FROM of an earlier stage names no image."""
    images: list[str] = []
    stages: set[str] = set()
    for found in FROM.finditer(path.read_text()):
        if found["image"] not in stages:
            images.append(found["image"])
        if found["stage"]:
            stages.add(found["stage"])
    return images


class _ComposeLoader(yaml.SafeLoader):
    """The safe loader, and the tags of Compose (`!reset`, `!override`) as plain values."""


def _untagged(loader: yaml.SafeLoader, _suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    assert isinstance(node, yaml.ScalarNode)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("!", _untagged)  # pyright: ignore[reportUnknownMemberType]


def _images() -> list[tuple[str, str]]:
    """Each image of the Dockerfiles, the Compose files of deploy/ and the workflows."""
    images = [
        (path.name, image)
        for path in sorted(DEPLOY.glob("Dockerfile.*"))
        for image in _dockerfile_images(path)
    ]
    # Each Compose file of deploy/: the stack, its production file and the file of the drill.
    for path in sorted(DEPLOY.rglob("*.y*ml")):
        compose: dict[str, Any] = yaml.load(path.read_text(), _ComposeLoader)  # noqa: S506 - a safe loader
        # A service with `build` has an image of this repository: a name for the build, no pull.
        images += [
            (path.name, str(service["image"]))
            for service in compose.get("services", {}).values()
            if "image" in service and "build" not in service
        ]
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        workflow: dict[str, Any] = yaml.safe_load(path.read_text())
        envs: list[dict[str, Any]] = [workflow.get("env", {})]
        for job in workflow["jobs"].values():
            envs += [job.get("env", {}), *(step.get("env", {}) for step in job.get("steps", []))]
            services: dict[str, dict[str, Any]] = job.get("services", {})
            images += [(path.name, str(service["image"])) for service in services.values()]
            # A job can also run in a container: `container: <image>` or `container: {image: ...}`.
            container: str | dict[str, Any] | None = job.get("container")
            if container is not None:
                image = container if isinstance(container, str) else str(container["image"])
                images.append((path.name, image))
        images += [
            (path.name, str(value))
            for env in envs
            for value in env.values()
            if IMAGE_LIKE.fullmatch(str(value))
        ]
    return images


def test_each_image_has_a_tag_and_a_digest() -> None:
    images = _images()
    refs: dict[str, set[str]] = {}
    for _, image in images:
        found = PINNED.fullmatch(image)
        refs.setdefault(found["name"] if found else image, set()).add(image)

    # A change that reads nothing must not pass.
    assert {
        "pgvector/pgvector",
        "python",
        "ghcr.io/astral-sh/uv",
        "node",
        "caddy",
        "rhysd/actionlint",
    } <= set(refs)
    assert [(file, image) for file, image in images if not PINNED.fullmatch(image)] == []
    # One tag and one digest for each image, in all files: the database of CI is the database of
    # the stack, and both stages of the web image start from the same Node.
    assert {name: found for name, found in refs.items() if len(found) != 1} == {}


def test_files_come_only_from_a_stage() -> None:
    # Dependabot does not read `COPY --from=<image>` or `RUN --mount=...,from=<image>`: an image
    # there would keep its digest forever.
    for path in sorted(DEPLOY.glob("Dockerfile.*")):
        text = path.read_text()
        stages = {found["stage"] for found in FROM.finditer(text) if found["stage"]}

        assert [name for name in re.findall(r"\bfrom=([^\s,]+)", text) if name not in stages] == []


def test_the_images_have_the_versions_of_ci_and_of_the_code() -> None:
    # Dependabot changes only the digest of these three (the ignore rules of dependabot.yml): a
    # new version changes the code and CI too, in one pull request by hand.
    env: dict[str, str] = yaml.safe_load((WORKFLOWS / "api.yml").read_text())["env"]
    python = (REPO / "apps" / "api" / ".python-version").read_text().strip()
    tags = {
        found["name"]: found["tag"] for _, image in _images() if (found := PINNED.fullmatch(image))
    }

    assert tags["ghcr.io/astral-sh/uv"] == env["UV_VERSION"]
    assert tags["node"] == f"{env['NODE_VERSION']}-slim"
    assert tags["python"] == f"{python}-slim"

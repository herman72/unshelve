from typer.testing import CliRunner

from unshelve import __version__
from unshelve.cli import app

runner = CliRunner()


def test_version_prints_the_installed_version() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_every_service_command_is_registered() -> None:
    result = runner.invoke(app, ["--help"])

    for command in ("api", "worker", "bot", "migrate"):
        assert command in result.stdout

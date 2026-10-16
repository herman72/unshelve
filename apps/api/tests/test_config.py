import pytest

from unshelve.config import Settings
from unshelve.testing.db import NO_DATABASE_URL


def test_defaults_keep_every_external_call_local() -> None:
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]

    assert settings.providers == "fake"
    assert settings.fetcher == "fixtures"
    assert settings.telegram_bot_token is None
    assert settings.capture_chat_ids == []


def test_a_test_without_a_database_of_its_own_gets_none() -> None:
    # The default names the database of the instance on this computer (conftest).
    assert Settings(env="test").database_url == NO_DATABASE_URL


def test_values_come_from_prefixed_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNSHELVE_OWNER_TELEGRAM_ID", "42")
    monkeypatch.setenv("UNSHELVE_CAPTURE_CHAT_IDS", "[-1001234567890, 7]")
    monkeypatch.setenv("UNSHELVE_TELEGRAM_BOT_TOKEN", "123:abc")

    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]

    assert settings.owner_telegram_id == 42
    assert settings.capture_chat_ids == [-1001234567890, 7]
    assert settings.telegram_bot_token is not None
    # The token never appears in logs or reprs.
    assert "abc" not in repr(settings)

"""Runtime settings, read from ``UNSHELVE_*`` environment variables."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="UNSHELVE_",
        # Commands run from apps/api; the .env file lives at the repository root.
        env_file=("../../.env", ".env"),
        env_file_encoding="utf-8",
        # An empty value in .env means "not set", as in .env.example.
        env_ignore_empty=True,
        extra="ignore",
    )

    env: Literal["dev", "test", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = False

    database_url: str = "postgresql+psycopg://unshelve:unshelve@localhost:54329/unshelve"

    # Telegram. The bot serves this one user and captures from these chats only.
    telegram_bot_token: SecretStr | None = None
    owner_telegram_id: int | None = None
    capture_chat_ids: list[int] = Field(default_factory=list[int])

    # "fake" and "fixtures" keep every model and network call local and deterministic.
    providers: Literal["fake", "live"] = "fake"
    fetcher: Literal["fixtures", "live"] = "fixtures"
    llm_model: str = "claude-opus-5-5"

    web_base_url: str = "http://localhost:3000"
    blob_dir: Path = Path("var/blobs")


@lru_cache
def get_settings() -> Settings:
    return Settings()

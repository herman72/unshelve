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

    # The weekly digest: the bot sends it on this day, at this hour of this time zone.
    timezone: str = "UTC"
    digest_day: Literal[
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
    ] = "sunday"
    digest_hour: int = Field(default=9, ge=0, le=23)

    # "fake" and "fixtures" keep every model and network call local and deterministic.
    providers: Literal["fake", "live"] = "fake"
    fetcher: Literal["fixtures", "live"] = "fixtures"
    llm_model: str = "claude-opus-5-5"

    # The fetcher opens saved links (net/fetcher.py). The time is for one whole fetch, with its
    # redirects. The interval is the pause between two requests to the same host.
    fetch_timeout: float = Field(default=30.0, gt=0)
    fetch_host_interval: float = Field(default=1.0, ge=0)
    fetch_user_agent: str = "Mozilla/5.0 (compatible; Unshelve/0.1)"

    web_base_url: str = "http://localhost:3000"
    blob_dir: Path = Path("var/blobs")


@lru_cache
def get_settings() -> Settings:
    return Settings()

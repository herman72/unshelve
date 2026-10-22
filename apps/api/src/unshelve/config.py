"""Runtime settings, read from ``UNSHELVE_*`` environment variables."""

from decimal import Decimal
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
    # The reading time of a digest, in minutes: its items fit into it (resurface.selection).
    digest_minutes: int = Field(default=60, ge=5, le=600)

    # "fake" and "fixtures" keep every model and network call local and deterministic.
    # "live" asks Claude with ANTHROPIC_API_KEY.
    providers: Literal["fake", "live"] = "fake"
    fetcher: Literal["fixtures", "live"] = "fixtures"
    llm_model: str = "claude-opus-5-5"
    # How much the model thinks before it writes a card. "low" is enough for a summary.
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    # No new call on a day when the calls have cost this many dollars. A stored answer is free.
    llm_daily_budget_usd: Decimal = Field(default=Decimal("1.00"), ge=0)
    # How an item gets its card. "live" asks the model at once. "batch" lets the card wait,
    # and `unshelve llm batch send` asks for all waiting cards in one batch, at half the price.
    cards: Literal["live", "batch"] = "live"
    # The prefilter of the scores (docs/adr/0014): an item whose card is less similar than this
    # to each goal gets 0 for each goal, without a question to the model. -1 asks the model for
    # each item. The value is a cosine similarity of the embedder: with voyage-4-large, pairs
    # without a link were at 0.11 to 0.23, and pairs with a link at 0.50 to 0.56 (note 32).
    score_prefilter: float = Field(default=0.3, ge=-1, le=1)

    # The embedder makes the vectors for search (ADR 0012). "fake" is local and free. "voyage"
    # asks Voyage AI with VOYAGE_API_KEY, with this model and this length of vector.
    embedder: Literal["fake", "voyage"] = "fake"
    embed_model: str = "voyage-4-large"
    embed_dim: Literal[256, 512, 1024, 2048] = 1024
    # The usual name of the key, without the UNSHELVE_ prefix.
    voyage_api_key: SecretStr | None = Field(default=None, validation_alias="VOYAGE_API_KEY")

    # The fetcher opens saved links (net/fetcher.py). The time is for one whole fetch, with its
    # redirects. The interval is the pause between two requests to the same host.
    fetch_timeout: float = Field(default=30.0, gt=0)
    fetch_host_interval: float = Field(default=1.0, ge=0)
    fetch_user_agent: str = "Mozilla/5.0 (compatible; Unshelve/0.1)"

    web_base_url: str = "http://localhost:3000"
    blob_dir: Path = Path("var/blobs")

    # The public feed (docs/adr/0004): the items that you made public, as one feed.json.
    # "none" publishes nothing. "file" writes the feed to publish_file.
    publish_target: Literal["none", "file"] = "none"
    publish_file: Path = Path("var/public/feed.json")


@lru_cache
def get_settings() -> Settings:
    return Settings()

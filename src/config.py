"""
FBot Configuration — Pydantic Settings.

All config is loaded from environment variables (or .env file).
This module provides type-safe, validated access to every setting.
"""

from enum import Enum
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingMode(str, Enum):
    """Trading execution mode."""
    PAPER = "paper"
    LIVE = "live"


class Settings(BaseSettings):
    """Application-wide configuration loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Database ──────────────────────────────────────────────────────
    database_url: str = Field(
        default="postgresql+asyncpg://fbot:changeme@db:5432/fbot",
        description="Async PostgreSQL connection string",
    )

    # ── Telegram ──────────────────────────────────────────────────────
    telegram_bot_token: str = Field(default="", description="Telegram Bot API token")
    telegram_chat_id: str = Field(default="", description="Your personal Telegram chat ID")

    # ── Zerodha Kite Connect ──────────────────────────────────────────
    kite_api_key: str = Field(default="", description="Kite Connect API key")
    kite_api_secret: str = Field(default="", description="Kite Connect API secret")
    kite_access_token: str = Field(default="", description="Daily access token (refreshed)")

    # ── Ollama (optional sentiment analysis) ──────────────────────────
    ollama_base_url: str = Field(
        default="http://localhost:11434",
        description="Ollama server URL",
    )
    ollama_model: str = Field(default="gemma4:12b", description="Ollama model for sentiment")

    # ── NewsAPI (optional) ────────────────────────────────────────────
    news_api_key: str = Field(default="", description="NewsAPI.org key")

    # ── Application Settings ──────────────────────────────────────────
    trading_mode: TradingMode = Field(
        default=TradingMode.PAPER,
        description="Paper trade (default) or live",
    )
    log_level: str = Field(default="INFO", description="Logging level")
    scan_universe: str = Field(default="nifty500", description="Stock universe to scan")
    max_position_pct: float = Field(
        default=10.0,
        description="Max % of total holdings value per single stock",
    )
    daily_buy_limit: int = Field(
        default=10,
        description="Max buy recommendations pushed to Telegram per day",
    )
    min_score: int = Field(
        default=60,
        description="Minimum composite score to qualify as a recommendation",
    )
    max_cmp_entry_deviation_pct: float = Field(
        default=5.0,
        description="Max % deviation between CMP and entry price; skip if exceeded",
    )
    min_risk_reward: float = Field(
        default=1.0,
        description="Minimum Risk/Reward ratio to qualify; below this means risk > reward",
    )

    # ── Scheduling ────────────────────────────────────────────────────
    full_scan_hour: int = Field(default=6, description="Hour (IST) for full daily scan")
    digest_hour: int = Field(default=9, description="Hour (IST) for daily digest")
    bulk_deal_hour: int = Field(default=16, description="Hour (IST) for bulk deal check")

    # ── Server ────────────────────────────────────────────────────────
    base_url: str = Field(
        default="https://kuber-bot.duckdns.org",
        description="Public-facing base URL for OAuth callbacks and Telegram links",
    )
    host: str = Field(default="0.0.0.0", description="FastAPI bind host")
    port: int = Field(default=5000, description="FastAPI bind port")

    @property
    def is_paper_mode(self) -> bool:
        """Check if running in paper trade mode."""
        return self.trading_mode == TradingMode.PAPER

    @property
    def has_telegram(self) -> bool:
        """Check if Telegram is configured."""
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def has_kite(self) -> bool:
        """Check if Zerodha Kite is configured."""
        return bool(self.kite_api_key and self.kite_api_secret)

    @property
    def has_ollama(self) -> bool:
        """Check if Ollama URL is configured (availability checked at runtime)."""
        return bool(self.ollama_base_url)

    @property
    def has_news_api(self) -> bool:
        """Check if NewsAPI key is configured."""
        return bool(self.news_api_key)


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings (singleton)."""
    return Settings()

"""Configuration for the practice-first TradingOS backend."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Local-first runtime settings. Live execution is deliberately disabled by default."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="TRADINGOS_", extra="ignore")

    app_name: str = "TradingOS API"
    api_prefix: str = "/api/v1"
    database_url: str = "sqlite:///./data/tradingos.db"
    cors_origins: str = Field(default="http://localhost:3000,http://localhost:3001,http://127.0.0.1:3000,http://127.0.0.1:3001")
    local_admin_token: str | None = None
    credential_encryption_key: str | None = None
    broker_sync_interval_seconds: int = Field(default=30, ge=10, le=3_600)
    broker_asset_refresh_seconds: int = Field(default=300, ge=60, le=86_400)
    broker_candle_count: int = Field(default=200, ge=20, le=1_000)
    broker_request_timeout_seconds: int = Field(default=30, ge=5, le=120)
    auto_reconcile_enabled: bool = False
    practice_execution_enabled: bool = False
    real_execution_enabled: bool = False
    ai_enabled: bool = False
    ai_model: str = "gpt-5-mini"
    ai_base_url: str | None = None
    ai_api_key: str | None = None
    ai_daily_token_budget: int = Field(default=12_000, ge=1_000, le=1_000_000)
    ai_daily_run_limit: int = Field(default=4, ge=1, le=100)
    ai_max_prompt_characters: int = Field(default=12_000, ge=1_000, le=100_000)

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()

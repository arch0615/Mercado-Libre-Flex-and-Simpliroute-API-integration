from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"
    tz: str = "America/Argentina/Buenos_Aires"

    database_url: str = Field(
        default="postgresql+psycopg://mercado:mercado@localhost:5432/mercado"
    )

    ml_client_id: str = ""
    ml_client_secret: str = ""
    ml_redirect_uri: str = "http://localhost:8000/oauth/callback"
    ml_seller_id: str = ""
    ml_trigger_status: Literal["paid", "ready_to_ship"] = "ready_to_ship"
    ml_api_base: str = "https://api.mercadolibre.com"
    ml_auth_base: str = "https://auth.mercadolibre.com.ar"

    simpliroute_api_base: str = "https://api.simpliroute.com"
    simpliroute_token: str = ""
    simpliroute_max_retries: int = 5
    simpliroute_backoff_base_seconds: float = 1.0

    geocoder_backend: Literal["google", "nominatim"] = "google"
    google_maps_api_key: str = ""
    geocoder_min_confidence: float = 0.7

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    alert_dedup_window_minutes: int = 15

    internal_secret: str = "change-me"
    cron_interval_minutes: int = 20
    cron_watchdog_minutes: int = 45


@lru_cache
def get_settings() -> Settings:
    return Settings()

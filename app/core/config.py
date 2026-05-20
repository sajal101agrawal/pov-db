from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "local"
    api_prefix: str = "/api"
    database_url: str = "postgresql://pov:pov@localhost:5433/pov"
    redis_url: str = "redis://localhost:6380/0"
    default_risk_free_rate: float = 0.10
    data_dir: Path = Path("data")
    nse_request_delay_seconds: float = 0.35
    source_retry_attempts: int = 3
    source_retry_base_delay_seconds: float = 0.75
    source_retry_max_delay_seconds: float = 8.0
    pipeline_symbol_limit: int | None = Field(default=None)
    dhan_client_id: str | None = None
    dhan_access_token: str | None = None
    live_option_chain_provider: str = "dhan"
    live_option_chain_min_interval_seconds: float = 3.0

    @field_validator("pipeline_symbol_limit", mode="before")
    @classmethod
    def blank_pipeline_symbol_limit(cls, value: object) -> object:
        return None if value == "" else value


@lru_cache
def get_settings() -> Settings:
    return Settings()

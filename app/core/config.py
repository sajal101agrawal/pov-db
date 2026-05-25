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
    pipeline_compute_concurrency: int = 4
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_region: str = "ap-south-1"
    s3_dump_bucket: str | None = None
    s3_dump_prefix: str = "etl-dumps/"
    dhan_client_id: str | None = None
    dhan_access_token: str | None = None
    live_quote_provider: str = "yahoo"
    live_option_chain_provider: str = "dhan"
    live_market_quote_min_interval_seconds: float = 1.0
    live_option_chain_min_interval_seconds: float = 3.0
    live_symbols: str = "RELIANCE,SBIN,INFY,HDFCBANK,TCS,NIFTY,BANKNIFTY"
    live_poll_interval_seconds: int = 180
    live_cache_ttl_seconds: int = 300
    live_market_start_ist: str = "09:00"
    live_market_end_ist: str = "16:00"

    @field_validator("pipeline_symbol_limit", mode="before")
    @classmethod
    def blank_pipeline_symbol_limit(cls, value: object) -> object:
        return None if value == "" else value


@lru_cache
def get_settings() -> Settings:
    return Settings()

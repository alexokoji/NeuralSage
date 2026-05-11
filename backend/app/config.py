"""Centralized application configuration loaded from environment variables."""
from __future__ import annotations

from functools import lru_cache
from typing import Any, List

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _normalize_postgres_url(url: str, *, driver: str) -> str:
    """Translate a bare Postgres URL into one with the requested SQLAlchemy driver.

    Render injects `postgresql://...` (and historically `postgres://...`) into
    DATABASE_URL. We split that into:
      * `postgresql+asyncpg://...`  for the FastAPI app (async)
      * `postgresql+psycopg://...`  for Alembic + Celery tasks (sync)

    Also rewrites `sslmode=require` → `ssl=require` for asyncpg, which doesn't
    accept libpq's `sslmode` parameter.
    """
    if not url:
        return url

    scheme, sep, rest = url.partition("://")
    if not sep:
        return url

    base = scheme.split("+", 1)[0]
    if base not in ("postgres", "postgresql"):
        return url

    out = f"postgresql+{driver}://{rest}"

    if driver == "asyncpg":
        out = out.replace("sslmode=require", "ssl=require")
        out = out.replace("sslmode=disable", "ssl=disable")
        out = out.replace("sslmode=verify-full", "ssl=verify-full")
        out = out.replace("sslmode=prefer", "ssl=prefer")

    return out


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")

    # App
    APP_NAME: str = "NeuralTrade"
    APP_ENV: str = "development"
    APP_DEBUG: bool = True
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    APP_CORS_ORIGINS: str = "http://localhost:3000"

    # Database — DATABASE_URL is the only required input. SYNC_DATABASE_URL is
    # derived if absent, so Render's auto-injected `postgresql://...` works
    # without further wiring.
    DATABASE_URL: str
    SYNC_DATABASE_URL: str = ""

    # Redis / Celery
    REDIS_URL: str = "redis://redis:6379/0"
    CELERY_BROKER_URL: str = ""
    CELERY_RESULT_BACKEND: str = ""

    # Security
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_MINUTES: int = 60
    JWT_REFRESH_TOKEN_DAYS: int = 7
    ENCRYPTION_KEY: str

    # Rate limiting
    RATE_LIMIT_DEFAULT: str = "100/minute"
    RATE_LIMIT_AUTH: str = "10/minute"

    # Exchanges
    BYBIT_REST_URL: str = "https://api.bybit.com"
    BYBIT_TESTNET_REST_URL: str = "https://api-testnet.bybit.com"
    BYBIT_WS_PUBLIC_URL: str = "wss://stream.bybit.com/v5/public/linear"
    BYBIT_WS_PUBLIC_TESTNET_URL: str = "wss://stream-testnet.bybit.com/v5/public/linear"
    BITGET_REST_URL: str = "https://api.bitget.com"
    BITGET_WS_PUBLIC_URL: str = "wss://ws.bitget.com/v2/ws/public"

    # Trading hard caps
    MAX_RISK_PER_TRADE_PCT: float = 2.0
    MAX_DAILY_DRAWDOWN_PCT: float = 5.0
    MAX_CONCURRENT_TRADES: int = 5
    MAX_CONSECUTIVE_LOSSES: int = 3
    TRADE_LOOP_INTERVAL_SECONDS: int = 15
    OPTIMIZATION_INTERVAL_HOURS: int = 6

    @model_validator(mode="after")
    def _normalize_urls(self) -> "Settings":
        # Snapshot the raw (possibly bare) Postgres URL before mutating it,
        # so we can derive the sync variant from the same source.
        raw_db = self.DATABASE_URL
        raw_sync = self.SYNC_DATABASE_URL

        self.DATABASE_URL = _normalize_postgres_url(raw_db, driver="asyncpg")

        if raw_sync:
            self.SYNC_DATABASE_URL = _normalize_postgres_url(raw_sync, driver="psycopg")
        else:
            self.SYNC_DATABASE_URL = _normalize_postgres_url(raw_db, driver="psycopg")

        # Default Celery broker/result to REDIS_URL so a single Render Key
        # Value instance can serve all three roles in tight deployments.
        if not self.CELERY_BROKER_URL:
            self.CELERY_BROKER_URL = self.REDIS_URL
        if not self.CELERY_RESULT_BACKEND:
            self.CELERY_RESULT_BACKEND = self.REDIS_URL

        return self

    @property
    def cors_origins(self) -> List[str]:
        return [o.strip() for o in self.APP_CORS_ORIGINS.split(",") if o.strip()]

    @field_validator("ENCRYPTION_KEY")
    @classmethod
    def _validate_encryption_key(cls, v: str) -> str:
        # Must decode to 32 raw bytes for AES-256.
        import base64

        try:
            raw = base64.urlsafe_b64decode(v.encode())
        except Exception as exc:
            raise ValueError("ENCRYPTION_KEY must be urlsafe base64") from exc
        if len(raw) != 32:
            raise ValueError("ENCRYPTION_KEY must decode to 32 bytes (AES-256)")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()

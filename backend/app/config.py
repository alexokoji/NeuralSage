"""Centralized application configuration loaded from environment variables."""
from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")

    # App
    APP_NAME: str = "NeuralTrade"
    APP_ENV: str = "development"
    APP_DEBUG: bool = True
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    APP_CORS_ORIGINS: str = "http://localhost:3000"

    # MongoDB — only MONGODB_URL is required.
    # Use a free cluster at https://cloud.mongodb.com or a local mongod instance.
    MONGODB_URL: str = "mongodb://localhost:27017/neuraltrade"
    MONGODB_DB_NAME: str = "neuraltrade"

    # Redis / Celery (optional — leave blank for in-process single-instance mode)
    REDIS_URL: str = ""
    CELERY_BROKER_URL: str = ""
    CELERY_RESULT_BACKEND: str = ""

    # Run the trading loop / optimization / rollover in-process via APScheduler.
    ENABLE_IN_PROCESS_SCHEDULER: bool = True

    # Security
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_MINUTES: int = 60
    JWT_REFRESH_TOKEN_DAYS: int = 7
    ENCRYPTION_KEY: str

    # Rate limiting
    RATE_LIMIT_DEFAULT: str = "100/minute"
    RATE_LIMIT_AUTH: str = "10/minute"

    # Groq Cloud — AI brain (free tier at https://console.groq.com)
    # Add multiple keys comma-separated for load balancing: "gsk_key1,gsk_key2"
    GROQ_API_KEY: str = ""
    GROQ_API_KEY_2: str = ""

    # Exchanges
    BYBIT_REST_URL: str = "https://api.bybit.com"
    BYBIT_TESTNET_REST_URL: str = "https://api-testnet.bybit.com"
    # Optional Cloudflare Worker proxy for testnet signed requests.
    # Bybit testnet blocks cloud-hosting IPs (AWS/Render) at the CDN level.
    # Set this to a Workers URL to route testnet traffic through Cloudflare's
    # own edge IPs, which bypass the WAF block.
    # Example: https://bybit-proxy.yourname.workers.dev
    BYBIT_TESTNET_PROXY_URL: str = ""
    # Shared secret sent in X-Proxy-Secret header so only this backend can use
    # the Worker. Set the same value in the Worker's PROXY_SECRET env var.
    BYBIT_PROXY_SECRET: str = ""
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
    def _defaults(self) -> "Settings":
        if not self.CELERY_BROKER_URL:
            self.CELERY_BROKER_URL = self.REDIS_URL
        if not self.CELERY_RESULT_BACKEND:
            self.CELERY_RESULT_BACKEND = self.REDIS_URL
        return self

    @property
    def cors_origins(self) -> List[str]:
        return [o.strip() for o in self.APP_CORS_ORIGINS.split(",") if o.strip()]

    from pydantic import field_validator

    @field_validator("ENCRYPTION_KEY")
    @classmethod
    def _validate_encryption_key(cls, v: str) -> str:
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

"""Application configuration loaded from environment variables."""

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    """Application settings sourced from environment variables or .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
    database_url: str

    # External APIs
    twelve_data_api_key: str

    # Logging
    log_level: str = "INFO"
    log_json: bool = False

    # Scheduling
    candle_refresh_delay_seconds: int = 60

    # Trading
    # Prop firm account balance in USD (sourced from ACCOUNT_BALANCE env var)
    account_balance: float = 100000.0
    trading_symbol: str = "XAUUSD"

    # Authentication
    auth_service_url: str = "http://127.0.0.1:3000"
    auth_port: int = 3000
    better_auth_url: str = "http://127.0.0.1:8080"
    better_auth_secret: str = ""
    better_auth_seed_users: str = ""
    better_auth_database_url: str = ""

    # Telegram (optional -- system works without these configured)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Signal messenger via CallMeBot (optional -- system works without these)
    # Setup: add +34 644 52 74 88 on Signal and send "I allow callmebot to send me messages"
    signal_phone: str = ""
    signal_api_key: str = ""

    @model_validator(mode="after")
    def normalize_database_url(self) -> "Settings":
        """Ensure DATABASE_URL uses the asyncpg driver.

        Railway and other providers supply postgresql:// but SQLAlchemy async
        requires postgresql+asyncpg://.
        """
        url = self.database_url
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)

        parsed_url = make_url(url)
        if parsed_url.drivername == "postgresql+asyncpg":
            query = dict(parsed_url.query)
            sslmode = query.pop("sslmode", None)
            query.pop("channel_binding", None)
            if sslmode is not None:
                query.setdefault("ssl", sslmode)
            if query != parsed_url.query:
                url = parsed_url.set(query=query).render_as_string(hide_password=False)

        self.database_url = url
        self.trading_symbol = self.trading_symbol.strip().upper().replace("/", "")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return cached singleton Settings instance."""
    return Settings()

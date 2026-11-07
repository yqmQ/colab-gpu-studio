import os
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 12 hours matches the historical fixed lifetime. 30 days is the upper bound.
DEFAULT_SESSION_TTL_SECONDS = 12 * 60 * 60
MIN_SESSION_TTL_SECONDS = 60
MAX_SESSION_TTL_SECONDS = 30 * 24 * 60 * 60


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)
    database_url: str = "sqlite:///./data/studio.db"
    data_dir: Path = Path("./data")
    site_url: str = "http://localhost:8788"
    studio_password: str = ""
    encryption_key: str = ""
    session_secret: str = ""
    # Finite browser-session lifetime. Unset keeps 12h. 30-day tailnet: SESSION_TTL_SECONDS=2592000.
    session_ttl_seconds: int = Field(
        default=DEFAULT_SESSION_TTL_SECONDS,
        ge=MIN_SESSION_TTL_SECONDS,
        le=MAX_SESSION_TTL_SECONDS,
    )
    environment: str = "development"
    max_upload_bytes: int = 100 * 1024 * 1024
    allowed_hosts: list[str] = []
    studio_api_token: str = ""

    @field_validator("session_ttl_seconds", mode="before")
    @classmethod
    def _blank_session_ttl(cls, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            return DEFAULT_SESSION_TTL_SECONDS
        return value


if os.getenv("STUDIO_OFFLINE_TEST") == "1":
    from .offline import install
    install()
    settings = Settings(_env_file=None)
else:
    settings = Settings(_env_file=None) if os.getenv("ENVIRONMENT") == "offline-test" else Settings()

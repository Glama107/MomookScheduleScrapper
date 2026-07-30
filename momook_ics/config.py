"""Runtime configuration, read from environment variables (or a local .env)."""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="MOMOOK_",
        extra="ignore",
    )

    base_url: str = "https://my.momook.com"
    username: str = ""
    password: str = ""

    # Base32 secret behind the TOTP QR code. Leave empty if 2FA is disabled.
    totp_secret: str = ""

    # Timezone used to interpret timestamps Momook returns without an offset,
    # and advertised to the calendar client.
    timezone: str = "Europe/Paris"

    # How far back / forward the feed reaches, in days.
    days_past: int = 30
    days_future: int = 180

    # Seconds a fetched schedule is reused before hitting Momook again.
    cache_ttl: int = 300

    # Shared secret in the feed URL. Anyone holding it can read your schedule.
    feed_token: str = ""

    calendar_name: str = "Momook"

    # Only include events the signed-in user is actually a participant of.
    # Momook can return events attached to a group you belong to; keep this on
    # unless the feed comes out too sparse.
    only_my_events: bool = True

    # Drop cancelled events entirely instead of publishing them as STATUS:CANCELLED.
    hide_cancelled: bool = False

    host: str = "0.0.0.0"
    port: int = 8080

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("totp_secret")
    @classmethod
    def _normalize_totp(cls, value: str) -> str:
        # QR-code secrets are often shown in spaced, lowercase groups.
        return value.replace(" ", "").upper()

    def require_credentials(self) -> None:
        missing = [
            name
            for name, value in (("MOMOOK_USERNAME", self.username), ("MOMOOK_PASSWORD", self.password))
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings

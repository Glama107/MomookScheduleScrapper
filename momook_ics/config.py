"""Runtime configuration, read from environment variables (or a local .env)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
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
    days_past: int = 7
    days_future: int = 90

    # Momook's gateway returns 504 on a query spanning several months, so the
    # window is fetched in slices of at most this many days.
    chunk_days: int = 21

    # Seconds between background refreshes of the cached calendar. A full
    # refresh is several slow queries, so keep this well above a minute — a
    # calendar client polls hourly at best anyway.
    cache_ttl: int = 1800

    # Momook's /api/schedule is slow — tens of seconds for a wide window with
    # every relation attached. Refreshes happen off the request path, so a
    # generous timeout costs subscribers nothing.
    http_timeout: float = 120.0

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
        """What any entry point needs to talk to Momook at all."""
        self._require(MOMOOK_USERNAME=self.username, MOMOOK_PASSWORD=self.password)

    def require_serving(self) -> None:
        """What the HTTP feed additionally needs. The token is the only thing
        protecting your schedule at a public URL; generate one with
        ``openssl rand -hex 24``."""
        self.require_credentials()
        self._require(MOMOOK_FEED_TOKEN=self.feed_token)

    @staticmethod
    def _require(**values: str) -> None:
        missing = sorted(name for name, value in values.items() if not value)
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

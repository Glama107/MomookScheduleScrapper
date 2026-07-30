"""Glue: fetch from Momook, normalise, render ICS, cache the result."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .client import Credentials, MomookClient
from .config import Settings
from .ical import build_calendar
from .model import Event, parse_events

log = logging.getLogger(__name__)


class FeedBuilder:
    """Builds the .ics document, reusing a cached copy for ``cache_ttl`` seconds.

    If a refresh fails but a previous document exists, the stale one is served
    instead of an error: a calendar that is a few minutes out of date beats a
    calendar that vanished from the phone.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._tz = ZoneInfo(settings.timezone)
        self._client = MomookClient(
            settings.base_url,
            Credentials(
                username=settings.username,
                password=settings.password,
                totp_secret=settings.totp_secret,
            ),
        )
        self._lock = threading.Lock()
        self._cached: bytes | None = None
        self._cached_at: float = 0.0
        self._last_error: str | None = None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def cached_at(self) -> float:
        return self._cached_at

    def close(self) -> None:
        self._client.close()

    def window(self) -> tuple[datetime, datetime]:
        now = datetime.now(self._tz)
        return (
            now - timedelta(days=self._settings.days_past),
            now + timedelta(days=self._settings.days_future),
        )

    def fetch_events(self) -> list[Event]:
        start, end = self.window()
        user_id = self._client.user_id()
        rows = self._client.fetch_events(start, end, user_id=user_id)
        log.info("Fetched %d raw schedule rows for user %s", len(rows), user_id)
        events = parse_events(
            rows,
            self._tz,
            only_user_id=user_id if self._settings.only_my_events else None,
        )
        if self._settings.hide_cancelled:
            events = [event for event in events if not event.cancelled]
        return events

    def build(self) -> bytes:
        events = self.fetch_events()
        log.info("Rendering %d calendar events", len(events))
        return build_calendar(
            events,
            name=self._settings.calendar_name,
            timezone_name=self._settings.timezone,
        )

    def get(self, *, force: bool = False) -> bytes:
        with self._lock:
            fresh = (
                self._cached is not None
                and not force
                and (time.monotonic() - self._cached_at) < self._settings.cache_ttl
            )
            if fresh:
                assert self._cached is not None
                return self._cached

            try:
                document = self.build()
            except Exception as exc:  # noqa: BLE001 - reported, then degraded
                self._last_error = f"{type(exc).__name__}: {exc}"
                log.exception("Failed to refresh the Momook schedule")
                if self._cached is not None:
                    log.warning("Serving the cached calendar from %.0fs ago",
                                time.monotonic() - self._cached_at)
                    return self._cached
                raise

            self._last_error = None
            self._cached = document
            self._cached_at = time.monotonic()
            return document

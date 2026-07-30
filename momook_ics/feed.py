"""Glue: fetch from Momook, normalise, render ICS, keep a warm copy.

Momook takes tens of seconds to answer a wide schedule query, which is far
longer than a calendar client will wait. So refreshes run on a background
thread and requests are always answered from the cached document.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .client import MomookClient, MomookOverloadError
from .config import Settings
from .ical import build_calendar
from .model import Event, event_id, parse_events

log = logging.getLogger(__name__)

# Bounds on the adaptive slice splitting in _fetch_slice.
MIN_SLICE_DAYS = 3
MAX_SLICE_DEPTH = 3


class FeedBuilder:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._tz = ZoneInfo(settings.timezone)
        self._client = MomookClient.from_settings(settings)
        self._state_lock = threading.Lock()
        self._build_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cached: bytes | None = None
        self._cached_at: float = 0.0
        self._last_error: str | None = None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def cache_age_seconds(self) -> float | None:
        """Seconds since the cached calendar was built, or None if never."""
        if not self._cached_at:
            return None
        return time.monotonic() - self._cached_at

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._client.close()

    # -- fetching ----------------------------------------------------------

    def window(self) -> tuple[datetime, datetime]:
        now = datetime.now(self._tz)
        return (
            now - timedelta(days=self._settings.days_past),
            now + timedelta(days=self._settings.days_future),
        )

    def fetch_rows(self, window: tuple[datetime, datetime] | None = None) -> list[dict]:
        """Raw schedule rows for the whole window, fetched in slices.

        Momook's gateway returns 504 on a query spanning several months, so the
        window is walked in chunks. Slices overlap on events that straddle a
        boundary, hence the dedupe by event id.
        """
        start, end = window or self.window()
        user_id = self._client.user_id() if self._settings.only_my_events else None

        by_id: dict[object, dict] = {}
        anonymous: list[dict] = []
        for chunk_start, chunk_end in _slices(start, end, self._settings.chunk_days):
            rows = self._fetch_slice(user_id, chunk_start, chunk_end)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                key = event_id(row)
                if key is None:
                    anonymous.append(row)
                else:
                    by_id[key] = row

        merged = list(by_id.values()) + anonymous
        log.info("Fetched %d distinct schedule rows", len(merged))
        return merged

    def _fetch_slice(
        self, user_id: int, start: datetime, end: datetime, depth: int = 0
    ) -> list[dict]:
        """One slice, halved and retried when Momook gives up on it.

        Only an overload is retried: narrowing the window is a fix for "too much
        data", and nothing else. A rejected password must not turn one failure
        into a cascade of login attempts.
        """
        try:
            rows = self._client.fetch_events(start, end, user_id=user_id)
        except MomookOverloadError as exc:
            span_days = (end - start).total_seconds() / 86400
            if depth >= MAX_SLICE_DEPTH or span_days <= MIN_SLICE_DAYS:
                raise
            log.warning(
                "%s → %s failed (%s); splitting in two",
                start.date(),
                end.date(),
                exc,
            )
            middle = start + (end - start) / 2
            return self._fetch_slice(user_id, start, middle, depth + 1) + self._fetch_slice(
                user_id, middle, end, depth + 1
            )
        log.info("%s → %s: %d rows", start.date(), end.date(), len(rows))
        return rows

    def fetch_events(self) -> list[Event]:
        events = parse_events(self.fetch_rows(), self._tz)
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

    # -- cache -------------------------------------------------------------

    def refresh(self) -> bytes:
        """Rebuild and store the calendar. Only one build runs at a time."""
        with self._build_lock:
            return self._build_and_store()

    def _build_and_store(self) -> bytes:
        """Build and publish. Caller must hold ``_build_lock``."""
        document = self.build()
        with self._state_lock:
            self._cached = document
            self._cached_at = time.monotonic()
            self._last_error = None
        return document

    def get(self) -> bytes:
        """The calendar, without ever waiting on Momook if avoidable."""
        with self._state_lock:
            cached = self._cached
        if cached is not None:
            return cached

        try:
            with self._build_lock:
                # Someone else may have finished a build while we queued.
                with self._state_lock:
                    if self._cached is not None:
                        return self._cached
                return self._build_and_store()
        except Exception as exc:  # noqa: BLE001 - recorded, then degraded
            with self._state_lock:
                self._last_error = f"{type(exc).__name__}: {exc}"
                cached = self._cached
            log.exception("Failed to build the Momook calendar")
            if cached is not None:
                return cached
            raise

    def start_background_refresh(self) -> None:
        """Keep the cached calendar warm, off the request path."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._refresh_loop, name="momook-refresh", daemon=True
        )
        self._thread.start()

    def _refresh_loop(self) -> None:
        # A first pass right away, so the cache is warm before anyone subscribes.
        while not self._stop.is_set():
            try:
                self.refresh()
                log.info("Calendar refreshed; next in %ds", self._settings.cache_ttl)
            except Exception as exc:  # noqa: BLE001 - the loop must survive
                with self._state_lock:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                log.warning("Background refresh failed: %s", exc)
            self._stop.wait(self._settings.cache_ttl)


def _slices(start: datetime, end: datetime, days: int) -> list[tuple[datetime, datetime]]:
    """Split ``[start, end]`` into consecutive windows of at most ``days``."""
    if days <= 0:
        return [(start, end)]
    out: list[tuple[datetime, datetime]] = []
    cursor = start
    step = timedelta(days=days)
    while cursor < end:
        out.append((cursor, min(cursor + step, end)))
        cursor += step
    return out or [(start, end)]

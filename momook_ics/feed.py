"""Glue: fetch from Momook, normalise, render ICS, keep a warm copy.

Momook takes tens of seconds to answer a wide schedule query, which is far
longer than a calendar client will wait. So refreshes run on a background
thread and requests are always answered from the cached document.

A deployment serves several accounts. Each gets its own ``FeedBuilder`` — its
own session, its own cached calendar — and a single ``FeedRegistry`` thread
refreshes them one after another.
"""

from __future__ import annotations

import hmac
import logging
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .client import MomookClient, MomookOverloadError
from .config import Account, Settings
from .ical import build_calendar
from .model import Event, event_id, parse_events

log = logging.getLogger(__name__)

# Bounds on the adaptive slice splitting in _fetch_slice.
MIN_SLICE_DAYS = 3
MAX_SLICE_DEPTH = 3


class FeedBuilder:
    """One account's calendar: fetched, rendered, and held warm."""

    def __init__(self, account: Account, settings: Settings) -> None:
        self._account = account
        self._settings = settings
        self._tz = ZoneInfo(account.timezone)
        self._client = MomookClient.from_account(account, settings)
        self._state_lock = threading.Lock()
        self._build_lock = threading.Lock()
        self._cached: bytes | None = None
        self._cached_at: float = 0.0
        self._last_error: str | None = None

    @property
    def account(self) -> Account:
        return self._account

    @property
    def label(self) -> str:
        return self._account.label

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
        user_id = self._client.user_id() if self._account.only_my_events else None

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
        log.info("[%s] Fetched %d distinct schedule rows", self.label, len(merged))
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
                "[%s] %s → %s failed (%s); splitting in two",
                self.label,
                start.date(),
                end.date(),
                exc,
            )
            middle = start + (end - start) / 2
            return self._fetch_slice(user_id, start, middle, depth + 1) + self._fetch_slice(
                user_id, middle, end, depth + 1
            )
        log.info("[%s] %s → %s: %d rows", self.label, start.date(), end.date(), len(rows))
        return rows

    def fetch_events(self) -> list[Event]:
        events = parse_events(self.fetch_rows(), self._tz)
        if self._account.hide_cancelled:
            events = [event for event in events if not event.cancelled]
        return events

    def build(self) -> bytes:
        events = self.fetch_events()
        log.info("[%s] Rendering %d calendar events", self.label, len(events))
        return build_calendar(
            events,
            name=self._account.calendar_name,
            timezone_name=self._account.timezone,
        )

    # -- cache -------------------------------------------------------------

    def refresh(self) -> bytes:
        """Rebuild and publish the calendar. Only one build runs at a time."""
        with self._build_lock:
            try:
                document = self.build()
            except Exception as exc:  # noqa: BLE001 - recorded, re-raised for the caller
                with self._state_lock:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                raise
            with self._state_lock:
                self._cached = document
                self._cached_at = time.monotonic()
                self._last_error = None
            return document

    def get(self) -> bytes | None:
        """The cached calendar, or None if none has been built yet.

        Never builds. A request that misses the cache waits for the background
        thread to come round rather than starting a multi-minute fetch of its
        own — with several accounts subscribed, request-path builds are exactly
        how one cold start turns into a pile of concurrent heavy queries.
        """
        with self._state_lock:
            return self._cached


class FeedRegistry:
    """Every account's feed, refreshed by one shared background thread.

    One thread, not one per account: a refresh is a series of slow queries that
    holds a whole schedule window in memory, and running several at once would
    multiply both that footprint and the load on the school's server. Nothing is
    waiting on the result, so the accounts take their turn.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._builders = [FeedBuilder(account, settings) for account in settings.accounts]
        self._by_token = {builder.account.feed_token: builder for builder in self._builders}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __len__(self) -> int:
        return len(self._builders)

    @property
    def builders(self) -> list[FeedBuilder]:
        return list(self._builders)

    def find(self, token: str) -> FeedBuilder | None:
        """The feed a token unlocks, or None.

        Every candidate is compared even after a match, so the time taken says
        nothing about how close a guess was.
        """
        found: FeedBuilder | None = None
        for candidate, builder in self._by_token.items():
            if hmac.compare_digest(token, candidate):
                found = builder
        return found

    def status(self) -> list[dict]:
        """Per-account health, with nothing secret in it."""
        return [
            {
                "account": builder.label,
                "cache_age_seconds": (
                    round(age) if (age := builder.cache_age_seconds) is not None else None
                ),
                "last_error": builder.last_error,
            }
            for builder in self._builders
        ]

    # -- background refresh -------------------------------------------------

    def start(self) -> None:
        """Keep every cached calendar warm, off the request path."""
        if self._thread is not None or not self._builders:
            return
        self._thread = threading.Thread(
            target=self._refresh_loop, name="momook-refresh", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            # A refresh in flight can be a minute of slow queries; the daemon
            # thread dies with the process rather than holding up shutdown.
            self._thread.join(timeout=5)
        for builder in self._builders:
            builder.close()

    def _refresh_loop(self) -> None:
        """Refresh whichever account is due next, one at a time, forever."""
        cycle = self._settings.cache_ttl
        due = [time.monotonic()] * len(self._builders)

        while not self._stop.is_set():
            index = min(range(len(due)), key=due.__getitem__)
            builder = self._builders[index]

            overdue = time.monotonic() - due[index]
            if overdue < 0 and self._stop.wait(-overdue):
                return
            if overdue > cycle:
                # Builds are taking longer than the roster has room for.
                log.warning(
                    "Refreshes are falling behind: %s is due since %ds (cache_ttl is %ds "
                    "for %d accounts)",
                    builder.label,
                    round(overdue),
                    cycle,
                    len(self._builders),
                )

            try:
                builder.refresh()
                log.info("[%s] Calendar refreshed", builder.label)
            except Exception as exc:  # noqa: BLE001 - the loop must survive
                log.warning("[%s] Background refresh failed: %s", builder.label, exc)

            due[index] = time.monotonic() + cycle
            if self._stop.wait(self._settings.refresh_gap):
                return


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

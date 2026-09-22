"""What a refresh does with a schedule that ends, and with one Momook only half
answers. Momook itself is stubbed out.

Run with:  python -m tests.test_feed
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta

os.chdir(tempfile.mkdtemp(prefix="momook-test-feed-"))

from zoneinfo import ZoneInfo  # noqa: E402

from momook_ics.client import MomookAuthError, MomookError, MomookOverloadError  # noqa: E402
from momook_ics.config import Settings  # noqa: E402
from momook_ics.feed import FeedBuilder  # noqa: E402

TZ = ZoneInfo("Europe/Paris")
NOW = datetime.now(TZ)


def row(identifier: int, when: datetime) -> dict:
    return {
        "Id": identifier,
        "Start": int(when.timestamp()),
        "End": int((when + timedelta(hours=1)).timestamp()),
    }


class FakeMomook:
    """Answers each slice from ``plan(start, end)``: rows, or a failure to raise."""

    def __init__(self, plan) -> None:
        self._plan = plan
        self.asked: list[tuple[datetime, datetime]] = []

    def user_id(self) -> int:
        return 7

    def fetch_events(self, start: datetime, end: datetime, *, user_id: int | None) -> list[dict]:
        self.asked.append((start, end))
        answer = self._plan(start, end)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def close(self) -> None:
        pass


def build(plan, **overrides) -> tuple[FeedBuilder, FakeMomook]:
    settings = Settings(username="u", password="p", feed_token="t", **overrides)
    builder = FeedBuilder(settings.accounts[0], settings)
    momook = FakeMomook(plan)
    builder._client = momook  # type: ignore[assignment]
    return builder, momook


def test_the_walk_stops_at_the_schools_horizon() -> None:
    """A year-wide window costs a year of queries only if there is a year of
    schedule out there. The school has planned five weeks; the walk goes three
    empty slices past them to be sure, and then stops."""
    booked_until = NOW + timedelta(days=33)

    def plan(start: datetime, end: datetime) -> list[dict]:
        if start >= booked_until:
            return []
        return [row(int(start.timestamp()), start + timedelta(days=1))]

    builder, momook = build(plan, days_past=7, days_future=365, chunk_days=21, horizon_gap_days=60)
    harvest = builder.harvest()

    # 18 slices would cover the whole window; two hold events, three more say
    # so, and the rest of the year is never asked for.
    assert len(momook.asked) == 5, momook.asked
    assert harvest.gaps == [], harvest.gaps
    assert len(harvest.items) == 2, harvest.items


def test_a_year_that_is_full_is_walked_to_the_end() -> None:
    """The horizon is where the schedule stops, not a cap on how far it reaches."""
    def plan(start: datetime, end: datetime) -> list[dict]:
        return [row(int(start.timestamp()), start + timedelta(days=1))]

    builder, momook = build(plan, days_past=7, days_future=365, chunk_days=21)
    harvest = builder.harvest()

    assert len(momook.asked) == 18, len(momook.asked)
    assert harvest.horizon >= NOW + timedelta(days=364), harvest.horizon


def test_a_slice_momook_will_not_answer_is_carried_over_not_dropped() -> None:
    """The refresh that must not turn into a cancellation. One fortnight times
    out; the lessons already known for it stay on the calendar, and the other
    eleven months are published as usual."""
    blocked = [False]
    lesson = NOW + timedelta(days=20)

    def plan(start: datetime, end: datetime):
        if blocked[0] and start <= lesson < end:
            return MomookOverloadError("504 from the gateway")
        return [row(1, lesson)] if start <= lesson < end else []

    builder, _ = build(plan, days_past=7, days_future=90, chunk_days=21)

    first = builder.refresh()
    assert b"UID:" in first, first[:200]
    assert builder.unfetched_ranges == 0, builder.unfetched_ranges

    blocked[0] = True
    second = builder.refresh()

    assert builder.unfetched_ranges == 1, builder.unfetched_ranges
    assert second.count(b"BEGIN:VEVENT") == 1, second
    assert builder.last_error is None, builder.last_error


def test_a_refresh_that_gets_nothing_at_all_leaves_the_calendar_alone() -> None:
    """Every slice failing is Momook being down, not a schedule that emptied
    out. Publishing then would replace the calendar with the copy carried over
    and reset its age — a broken feed passing for a healthy one."""
    down = [False]

    def plan(start: datetime, end: datetime):
        if down[0]:
            return MomookOverloadError("504 from the gateway")
        return [row(1, NOW + timedelta(days=3))]

    builder, _ = build(plan, days_past=7, days_future=90, chunk_days=21)
    good = builder.refresh()

    down[0] = True
    try:
        builder.refresh()
    except MomookError as exc:
        assert "Not one of the" in str(exc), exc
    else:
        raise AssertionError("a refresh that got nothing must not publish")

    assert builder.get() == good, "the last good calendar must still be served"
    assert builder.last_error is not None


def test_a_rejected_password_stops_the_refresh_rather_than_leaving_a_gap() -> None:
    """A credential that no longer works is not a slow query: it must surface,
    not be smoothed over with events from a fortnight ago."""
    def plan(start: datetime, end: datetime):
        return MomookAuthError("Momook rejected the login (HTTP 422)")

    builder, momook = build(plan, days_past=7, days_future=90, chunk_days=21)

    try:
        builder.refresh()
    except MomookAuthError:
        pass
    else:
        raise AssertionError("a rejected login must surface")

    assert builder.unfetched_ranges == 0, builder.unfetched_ranges
    assert len(momook.asked) == 1, momook.asked  # and it stops at the first slice


def main() -> None:
    for name, case in sorted(globals().items()):
        if name.startswith("test_"):
            case()
    print("ok — horizon, carried-over gaps, total failure and rejected logins behave")


if __name__ == "__main__":
    main()

"""iCalendar serialisation."""

from __future__ import annotations

from datetime import datetime, timezone

from icalendar import Calendar, Event as VEvent

from .model import Event

PRODID = "-//momook-ics//Momook schedule feed//FR"

# How often subscribers should poll. Apple honours X-PUBLISHED-TTL; the RFC 7986
# REFRESH-INTERVAL is what everyone else reads. Neither is binding — iOS decides
# its own polling rate — but both nudge clients towards a short interval.
REFRESH_INTERVAL = "PT15M"


def build_calendar(events: list[Event], *, name: str, timezone_name: str) -> bytes:
    cal = Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", name)
    cal.add("x-wr-timezone", timezone_name)
    cal.add("x-published-ttl", REFRESH_INTERVAL)
    cal.add("refresh-interval;value=duration", REFRESH_INTERVAL)

    now = datetime.now(timezone.utc)
    for event in events:
        cal.add_component(_to_vevent(event, now))

    # Emit VTIMEZONE blocks for every TZID referenced, so clients that do not
    # carry an Olson database still place the events correctly.
    cal.add_missing_timezones()
    return cal.to_ical()


def _to_vevent(event: Event, now: datetime) -> VEvent:
    vevent = VEvent()
    vevent.add("uid", f"{event.uid}@momook-ics")
    vevent.add("dtstamp", now)
    vevent.add("dtstart", event.start)
    vevent.add("dtend", event.end)
    vevent.add("summary", event.summary)
    if event.description:
        vevent.add("description", event.description)
    if event.location:
        vevent.add("location", event.location)
    if event.categories:
        vevent.add("categories", event.categories)
    vevent.add("status", "CANCELLED" if event.cancelled else "CONFIRMED")
    vevent.add("transp", "TRANSPARENT" if event.cancelled else "OPAQUE")

    # Momook exposes no revision counter, so there is nothing to put in
    # SEQUENCE that would be monotonic — a non-monotonic one is worse than
    # none, since strict clients then ignore genuine updates. LAST-MODIFIED
    # carries the "this changed" signal instead.
    vevent.add("last-modified", event.last_modified or now)
    return vevent

"""iCalendar serialisation."""

from __future__ import annotations

from datetime import datetime, timezone

from icalendar import Alarm, Calendar, Event as VEvent

from .model import Event

PRODID = "-//momook-ics//Momook schedule feed//FR"

# How often subscribers should poll. Apple honours X-PUBLISHED-TTL; the RFC 7986
# REFRESH-INTERVAL is what everyone else reads. Neither is binding — iOS decides
# its own polling rate — but both nudge clients towards a short interval.
REFRESH_INTERVAL = "PT15M"

# A school schedule is not something to be woken up about: the feed carries no
# alarms. But an event that simply omits VALARM inherits the client's default
# alert, so silence has to be stated. ACTION:NONE (RFC 9074) is the standard way
# to say "there is an alarm here, and it does nothing"; X-APPLE-DEFAULT-ALARM is
# what makes iOS and macOS treat it as the event's default and stop adding their
# own. The trigger is Apple's own sentinel date, kept verbatim so their parser
# recognises the shape.
_NO_ALARM_TRIGGER = datetime(1976, 4, 1, 0, 55, 45, tzinfo=timezone.utc)


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
    # Many clients do not surface STATUS:CANCELLED, so say it in the title too.
    vevent.add("summary", f"ANNULÉ — {event.summary}" if event.cancelled else event.summary)
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
    vevent.add_component(_silent_alarm(event))
    return vevent


def _silent_alarm(event: Event) -> Alarm:
    """A do-nothing alarm, so the client does not supply one of its own."""
    alarm = Alarm()
    alarm.add("action", "NONE")
    alarm.add("trigger", _NO_ALARM_TRIGGER, parameters={"VALUE": "DATE-TIME"})
    # Apple keys its "this is the default alarm" bookkeeping on these two.
    alarm.add("x-wr-alarmuid", f"{event.uid}-noalarm@momook-ics")
    alarm.add("x-apple-default-alarm", "TRUE")
    return alarm

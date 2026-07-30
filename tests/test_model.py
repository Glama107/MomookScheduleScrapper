"""Offline checks for the event mapping and ICS output.

Run with:  python -m tests.test_model
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from momook_ics.ical import build_calendar
from momook_ics.model import parse_events

TZ = ZoneInfo("Europe/Paris")
ME = 4242

# Shaped after what /api/schedule returns with the relations the front-end asks
# for. Field names come from the app bundle's mappers.
SAMPLE_ROWS = [
    {
        "Id": 100001,
        "Start": "2026-09-14 08:30:00",
        "End": "2026-09-14 12:00:00",
        "Type": "classroom_lesson",
        "TimeStatus": "planning",
        "ModifiedAt": "2026-09-01 10:12:00",
        "ScheduleEventClassrooms": [
            {"ClassroomEntity": {"Id": 7, "Title": "Salle B12"}},
        ],
        "ScheduleEventRequests": [
            {
                "TrainingTopic": {"Id": 55, "Title": "Meteorology"},
                "Training": {"Id": 3, "Title": "ATPL Theory"},
            }
        ],
        "ScheduleEventTrainingGroups": [
            {"TrainingGroup": {"Id": 12, "Title": "ATPL-2026-A"}},
        ],
        "ScheduleEventUsers": [
            {
                "UserId": 88,
                "UserRole": "instructor",
                "User": {"Id": 88, "TitleFull": "Jean Dupont", "TitleShort": "J. Dupont"},
            },
            {
                "UserId": ME,
                "UserRole": "student",
                "User": {"Id": ME, "TitleFull": "Guillaume M.", "TitleShort": "G. M."},
            },
        ],
    },
    {
        "Id": 100002,
        # Unix seconds are accepted too (2026-09-20 10:00–12:00 Europe/Paris).
        "Start": 1789891200,
        "End": 1789898400,
        "Type": "simulator_training",
        "TimeStatus": "cancelled",
        "ScheduleEventSimulators": [
            {"SimulatorEntity": {"Id": 2, "Title": "FNPT II #1"}},
        ],
        "ScheduleEventUsers": [
            {"UserId": ME, "UserRole": "student", "User": {"Id": ME, "TitleShort": "G. M."}},
        ],
    },
    {
        # Someone else's session: must be filtered out when only_user_id is set.
        "Id": 100003,
        "Start": "2026-09-15T09:00:00+02:00",
        "End": "2026-09-15T10:00:00+02:00",
        "Type": "briefing",
        "ScheduleEventUsers": [
            {"UserId": 999, "User": {"Id": 999, "TitleShort": "X. Y."}},
        ],
    },
]


def main() -> None:
    events = parse_events(SAMPLE_ROWS, TZ, only_user_id=ME)
    assert len(events) == 2, f"expected 2 of my events, got {len(events)}"

    lesson = events[0]
    assert lesson.summary == "Meteorology (Cours) — Salle B12", lesson.summary
    assert lesson.location == "Salle B12"
    assert "Instructeur(s) : Jean Dupont" in lesson.description, lesson.description
    assert "Groupe : ATPL-2026-A" in lesson.description
    assert lesson.start.hour == 8 and lesson.start.minute == 30
    assert lesson.start.tzinfo is not None
    assert not lesson.cancelled

    sim = events[1]
    assert sim.cancelled
    assert sim.summary.startswith("ANNULÉ — "), sim.summary
    assert sim.location == "FNPT II #1"

    unfiltered = parse_events(SAMPLE_ROWS, TZ, only_user_id=None)
    assert len(unfiltered) == 3

    ics = build_calendar(events, name="Momook", timezone_name="Europe/Paris").decode("utf-8")
    assert "BEGIN:VCALENDAR" in ics and ics.rstrip().endswith("END:VCALENDAR")
    assert ics.count("BEGIN:VEVENT") == 2
    assert "UID:momook-100001@momook-ics" in ics
    assert "STATUS:CANCELLED" in ics
    assert "X-PUBLISHED-TTL:PT15M" in ics
    assert "DTSTART;TZID=Europe/Paris:20260914T083000" in ics, _grep(ics, "DTSTART")

    # The parser must survive a payload it has never seen.
    junk = [{}, {"Id": 1}, {"Id": 2, "Start": "nonsense"}, {"Start": "2026-01-01 00:00:00"}]
    survivors = parse_events(junk, TZ, only_user_id=None)
    assert len(survivors) == 1, survivors

    print(f"ok — {len(events)} events, {len(ics.splitlines())} ICS lines")


def _grep(text: str, needle: str) -> str:
    return "\n".join(line for line in text.splitlines() if needle in line)


if __name__ == "__main__":
    main()

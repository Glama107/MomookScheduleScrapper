"""Turn raw Momook schedule events into a flat, calendar-friendly shape.

The API returns deeply nested rows whose exact keys vary with the relations
requested, so every accessor here degrades gracefully rather than raising.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser

# Event types, as defined in the front-end's scheduleEventType enum.
EVENT_TYPE_LABELS = {
    "briefing": "Briefing",
    "debriefing": "Débriefing",
    "aircraft_training": "Vol",
    "aircraft_maintenance": "Maintenance avion",
    "ferry_flight": "Convoyage",
    "classroom_lesson": "Cours",
    "classroom_self_study": "Étude personnelle",
    "classroom_exam": "Examen",
    "classroom_midterm_exam": "Examen partiel",
    "classroom_lease": "Salle réservée",
    "classroom_break": "Pause",
    "classroom_celebration": "Événement",
    "private_lesson": "Cours particulier",
    "simulator_training": "Simulateur",
    "simulator_lease": "Simulateur (location)",
    "simulator_booked": "Simulateur (réservé)",
    "simulator_maintenance": "Maintenance simulateur",
    "simulator_aircraft": "Simulateur",
    "reserved": "Réservé",
    "user_reserved": "Indisponible",
    "user_briefing": "Briefing",
    "off_duty": "Repos",
    "duty_period": "Service",
    "flight_elsewhere": "Vol (externe)",
    "duty_period_elsewhere": "Service (externe)",
}

TIME_STATUS_LABELS = {
    "planning": "En préparation",
    "started": "En cours",
    "stopped": "Terminé",
    "paused": "En pause",
    "cancelled": "Annulé",
}


@dataclass
class Participant:
    name: str
    role: str = ""


@dataclass
class Event:
    uid: str
    start: datetime
    end: datetime
    summary: str
    description: str = ""
    location: str = ""
    cancelled: bool = False
    last_modified: datetime | None = None
    categories: list[str] = field(default_factory=list)


def parse_events(rows: list[dict], tz: ZoneInfo, *, only_user_id: int | None = None) -> list[Event]:
    events: list[Event] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if only_user_id is not None and not _involves_user(row, only_user_id):
            continue
        event = parse_event(row, tz)
        if event is not None:
            events.append(event)
    events.sort(key=lambda e: e.start)
    return events


def parse_event(row: dict, tz: ZoneInfo) -> Event | None:
    start = _parse_datetime(_first(row, "Start", "start"), tz)
    end = _parse_datetime(_first(row, "End", "end"), tz)
    if start is None:
        return None
    if end is None or end <= start:
        # Zero-length or malformed rows still deserve a slot on the calendar.
        end = start + timedelta(hours=1)

    event_id = _first(row, "Id", "id")
    event_type = str(_first(row, "Type", "type") or "").strip()
    time_status = str(_first(row, "TimeStatus", "timeStatus") or "").strip()

    topic = _topic(row)
    training = _training(row)
    resource = _resource(row)
    group = _group(row)
    participants = _participants(row)
    comment = str(_first(row, "Comment", "comment", "Description", "Note") or "").strip()

    summary = _summary(event_type, topic, training, resource)
    if time_status == "cancelled":
        summary = f"ANNULÉ — {summary}"

    description_lines: list[str] = []
    if training and training != topic:
        description_lines.append(f"Formation : {training}")
    if topic:
        description_lines.append(f"Sujet : {topic}")
    if group:
        description_lines.append(f"Groupe : {group}")
    instructors = [p.name for p in participants if p.role == "instructor"]
    if instructors:
        description_lines.append(f"Instructeur(s) : {', '.join(instructors)}")
    others = [p.name for p in participants if p.role != "instructor"]
    if others:
        description_lines.append(f"Participants : {', '.join(others)}")
    if resource:
        description_lines.append(f"Ressource : {resource}")
    if comment:
        description_lines.append(f"Note : {comment}")
    if time_status and time_status in TIME_STATUS_LABELS:
        description_lines.append(f"Statut : {TIME_STATUS_LABELS[time_status]}")
    if event_id is not None:
        description_lines.append(f"Momook #{event_id}")

    categories = [EVENT_TYPE_LABELS.get(event_type, event_type)] if event_type else []

    return Event(
        uid=f"momook-{event_id}" if event_id is not None else f"momook-{start.timestamp():.0f}-{summary}",
        start=start,
        end=end,
        summary=summary,
        description="\n".join(description_lines),
        location=resource,
        cancelled=time_status == "cancelled",
        last_modified=_parse_datetime(_first(row, "ModifiedAt", "modified_at"), tz),
        categories=categories,
    )


# -- field extraction ------------------------------------------------------


def _summary(event_type: str, topic: str, training: str, resource: str) -> str:
    type_label = EVENT_TYPE_LABELS.get(event_type, event_type.replace("_", " ").title() or "Session")
    subject = topic or training
    if subject and resource:
        return f"{subject} ({type_label}) — {resource}"
    if subject:
        return f"{subject} ({type_label})"
    if resource:
        return f"{type_label} — {resource}"
    return type_label


def _topic(row: dict) -> str:
    for request in _rel(row, "ScheduleEventRequest"):
        title = _title(request.get("TrainingTopic"))
        if title:
            return title
    return _title(row.get("TrainingTopic"))


def _training(row: dict) -> str:
    for request in _rel(row, "ScheduleEventRequest"):
        title = _title(request.get("Training"))
        if title:
            return title
    return _title(row.get("Training"))


def _group(row: dict) -> str:
    for link in _rel(row, "ScheduleEventTrainingGroup"):
        title = _title(link.get("TrainingGroup"))
        if title:
            return title
    return ""


def _resource(row: dict) -> str:
    """Where the session happens: classroom, then simulator, then aircraft."""
    for rel_name, entity_key in (
        ("ScheduleEventClassroom", "ClassroomEntity"),
        ("ScheduleEventSimulator", "SimulatorEntity"),
        ("ScheduleEventAircraft", "AircraftEntity"),
    ):
        for link in _rel(row, rel_name):
            title = _title(link.get(entity_key))
            if title:
                return title
    return ""


def _participants(row: dict) -> list[Participant]:
    participants: list[Participant] = []
    seen: set[str] = set()
    for link in _rel(row, "ScheduleEventUser"):
        user = link.get("User") or link.get("CoreUser") or {}
        if not isinstance(user, dict):
            continue
        name = (
            user.get("TitleFull")
            or user.get("TitleShort")
            or " ".join(filter(None, [user.get("Firstname"), user.get("Lastname")])).strip()
            or user.get("Username")
            or ""
        )
        name = str(name).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        participants.append(Participant(name=name, role=_role(link.get("UserRole"))))
    return participants


def _role(raw: object) -> str:
    if isinstance(raw, str):
        return raw.lower()
    if isinstance(raw, list) and raw:
        first = raw[0]
        if isinstance(first, str):
            return first.lower()
        if isinstance(first, dict):
            return str(first.get("Type") or first.get("type") or "").lower()
    if isinstance(raw, dict):
        return str(raw.get("Type") or raw.get("type") or "").lower()
    return ""


def _involves_user(row: dict, user_id: int) -> bool:
    for link in _rel(row, "ScheduleEventUser"):
        candidate = link.get("UserId")
        if candidate is None:
            user = link.get("User") or link.get("CoreUser") or {}
            candidate = user.get("Id") if isinstance(user, dict) else None
        if _as_int(candidate) == user_id:
            return True
    for link in _rel(row, "ScheduleEventAttendee"):
        if _as_int(link.get("UserId")) == user_id:
            return True
    return False


def _rel(row: dict, name: str) -> list[dict]:
    """Momook returns relations pluralised (``ScheduleEventUsers``) but has
    been seen using the singular form too; accept either, list or object."""
    value = row.get(f"{name}s", row.get(name))
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _title(entity: object) -> str:
    if not isinstance(entity, dict):
        return ""
    for key in ("Title", "title", "Name", "name", "TitleFull", "Number"):
        value = entity.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _first(row: dict, *keys: str) -> object:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def _parse_datetime(value: object, tz: ZoneInfo) -> datetime | None:
    """Accept unix seconds, ISO 8601, or ``YYYY-MM-DD HH:MM:SS``."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.isdigit():
        return datetime.fromtimestamp(int(text), tz)
    try:
        parsed = date_parser.isoparse(text)
    except ValueError:
        try:
            parsed = date_parser.parse(text)
        except (ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        # Momook stores wall-clock times for the school's own timezone.
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)

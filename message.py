"""How a reminder looks when it lands.

The pool stores the plain text; presentation happens at delivery, so changing how reminders
look never rewrites what is stored. The shape is a template:

    🔔 {text}
    (Reminder set on {created})

Placeholders: ``{text}``, ``{created}``, ``{created_utc}``, ``{due}``, ``{due_utc}``, ``{id}``.
``{created}`` and ``{due}`` render in the local time of the machine running the poller —
whoever reads the message is almost always in that timezone, and a server is not.
"""

from __future__ import annotations

from .store import local_timezone, parse_when

DEFAULT_TEMPLATE = "🔔 {text}\n(Reminder set on {created})"
PLACEHOLDERS = ("text", "created", "created_utc", "due", "due_utc", "id", "every")
LOCAL_FORMAT = "%d %b %Y, %H:%M"
UTC_FORMAT = "%Y-%m-%dT%H:%MZ"



def _stamp(value, *, local: bool) -> str:
    """A stored instant as readable text; unparseable input degrades to 'unknown'."""
    if not value:
        return "unknown"
    try:
        moment = parse_when(str(value))
    except ValueError:
        return "unknown"
    if local:
        return moment.astimezone(local_timezone()).strftime(LOCAL_FORMAT)
    return moment.strftime(UTC_FORMAT)


def fields(record) -> dict:
    """The template's substitution values for one reminder record."""
    record = dict(record or {})
    return {
        "text": str(record.get("text") or "").strip(),
        "created": _stamp(record.get("created_at"), local=True),
        "created_utc": _stamp(record.get("created_at"), local=False),
        "due": _stamp(record.get("due_at"), local=True),
        "due_utc": _stamp(record.get("due_at"), local=False),
        "id": str(record.get("id") or ""),
        "every": str(record.get("every") or "").strip(),
    }



def render(record, template: str | None = None) -> str:
    """The message that gets sent.

    A broken custom template falls back to the default rather than raising: a typo in a format
    string must never cost someone a reminder.
    """
    values = fields(record)
    for candidate in (template, DEFAULT_TEMPLATE):
        if not candidate:
            continue
        try:
            return candidate.format(**values)
        except (KeyError, IndexError, ValueError):
            continue
    return values["text"]

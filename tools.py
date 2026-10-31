"""The agent-facing tools: add, list and cancel reminders."""

from __future__ import annotations

from . import message
from .store import ReminderStore, StoreError

ADD_SCHEMA = {
    "name": "reminders_add",
    "description": (
        "Add a reminder to the pool. It is delivered to the user once, at the given time, by a "
        "cron job that calls no model — so delivery costs nothing and this is one call.\n"
        "Write `text` as the sentence that will land, addressed to the reader in the second "
        "person: \"Charge your phone\", not \"Charge my phone\". Do not add an emoji or a "
        "timestamp — delivery adds those.\n"
        "`when` is an absolute ISO 8601 time (2026-11-01T15:00Z) or a duration from now "
        "(5m, 2h, 1d).\n"
        "Prefer this over a one-shot cron job: one-shot jobs that come due while the scheduler "
        "is down are retired rather than delivered, and N reminders would otherwise mean N jobs."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "What to remind the user about, written as the message to send.",
            },
            "when": {
                "type": "string",
                "description": "When to deliver it: ISO 8601 (2026-11-01T15:00Z) or a duration from now (2h, 90m, 1d).",
            },
            "target": {
                "type": "string",
                "description": "Optional delivery target (e.g. 'telegram' for the home channel). Defaults to the pool's configured target.",
            },
        },
        "required": ["text", "when"],
    },
}

LIST_SCHEMA = {
    "name": "reminders_list",
    "description": (
        "List the reminders still in the pool, with their ids, due times (UTC) and text. "
        "Use the id with reminders_cancel to remove one."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "all": {
                "type": "boolean",
                "description": "Include reminders whose delivery has failed, not just the pending ones.",
            }
        },
        "required": [],
    },
}

CANCEL_SCHEMA = {
    "name": "reminders_cancel",
    "description": "Remove a reminder from the pool so it is never delivered. Takes the id from reminders_list.",
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "The reminder id (an unambiguous prefix is enough)."}
        },
        "required": ["id"],
    },
}


def _store() -> ReminderStore:
    return ReminderStore()


def handle_add(args: dict, **kwargs) -> str:
    store = _store()
    try:
        record = store.add(
            str(args.get("text", "")),
            str(args.get("when", "")),
            created_by="agent",
            target=(args.get("target") or None),
        )
    except (StoreError, ValueError) as exc:
        return f"Could not add the reminder: {exc}"
    preview = message.render(record, store.template())
    return (
        f"Reminder {record['id']} set for {record['due_at']} (UTC).\n"
        f"It will arrive as:\n{preview}"
    )


def handle_list(args: dict, **kwargs) -> str:
    store = _store()
    records = store.all()
    if not args.get("all"):
        records = [record for record in records if record.get("status") == "pending"]
    if not records:
        return "No reminders in the pool."
    lines = []
    for record in sorted(records, key=lambda item: str(item.get("due_at", ""))):
        line = f"- {record.get('id')}  {record.get('due_at')}  {record.get('text')}"
        if record.get("status") != "pending":
            line += f"  [status={record.get('status')}, attempts={record.get('attempts')}, last_error={record.get('last_error')}]"
        lines.append(line)
    return f"{len(lines)} reminder(s), due times in UTC:\n" + "\n".join(lines)


def handle_cancel(args: dict, **kwargs) -> str:
    reference = str(args.get("id", "")).strip()
    if not reference:
        return "Give me the id of the reminder to cancel (from reminders_list)."
    removed = _store().cancel(reference)
    if removed is None:
        return f"No reminder matches {reference!r} (use reminders_list to see the ids)."
    return f"Cancelled {removed['id']}: {removed['text']}"

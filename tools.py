"""The agent-facing tools: add, list and cancel reminders."""

from __future__ import annotations

from . import cli, message
from .store import ReminderStore, StoreError

ADD_SCHEMA = {
    "name": "reminders_add",
    "description": (
        "Add a reminder to the pool. It is delivered to the user at the given time by a "
        "cron job that calls no model, so delivery costs nothing and this is one call.\n"
        "Write `text` as the sentence that will land, addressed to the reader in the second "
        "person: \"Charge your phone\", not \"Charge my phone\". Do not add an emoji or a "
        "timestamp; delivery adds those.\n"
        "`when` is an absolute ISO 8601 time (2026-11-01T15:00Z) or a duration from now "
        "(5m, 2h, 1d). Pass `every` for a recurring reminder ('2h', 'day', 'weekday', "
        "'week', 'month', 'mon,wed,fri').\n"
        "Prefer this over a cron job: one-shot jobs that come due while the scheduler "
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
                "description": "When to deliver it first: ISO 8601 (2026-11-01T15:00Z) or a duration from now (2h, 90m, 1d).",
            },
            "every": {
                "type": "string",
                "description": (
                    "Optional recurrence rule: a duration ('2h', '7d') or calendar rule "
                    "('day', 'weekday', 'week', 'month', 'mon,wed,fri'). Omit for a one-shot reminder."
                ),
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
            every=(args.get("every") or None),
        )
    except (StoreError, ValueError) as exc:
        return f"Could not add the reminder: {exc}"
    preview = message.render(record, store.template())
    due_local = message.fields(record)["due"]
    recurrence = f", repeats every {record['every']}" if record.get("every") else ""
    reply = (
        f"Reminder {record['id']} set for {due_local} ({record['due_at']}){recurrence}.\n"
        f"It will arrive as:\n{preview}"
    )
    missing = []
    if not (record.get("target") or store.default_target()):
        missing.append("no delivery target")
    if not cli.stub_path().exists() or cli.installed_job() is None:
        missing.append("poller cron job not installed")
    if missing:
        reply += (
            f"\nWarning: {', '.join(missing)}. "
            "Run `hermes reminders setup --target <platform>` so due reminders are delivered."
        )
    return reply


def handle_list(args: dict, **kwargs) -> str:
    store = _store()
    records = store.all()
    if not args.get("all"):
        records = [record for record in records if record.get("status") == "pending"]
    if not records:
        return "No reminders in the pool."
    lines = []
    for record in sorted(records, key=lambda item: str(item.get("due_at", ""))):
        due_local = message.fields(record)["due"]
        line = f"- {record.get('id')}  {due_local} ({record.get('due_at')})  {record.get('text')}"
        if record.get("every"):
            line += f"  [every {record.get('every')}]"
        if record.get("status") != "pending":
            line += f"  [status={record.get('status')}, attempts={record.get('attempts')}, last_error={record.get('last_error')}]"
        lines.append(line)
    return f"{len(lines)} reminder(s):\n" + "\n".join(lines)


def handle_cancel(args: dict, **kwargs) -> str:
    reference = str(args.get("id", "")).strip()
    if not reference:
        return "Give me the id of the reminder to cancel (from reminders_list)."
    removed = _store().cancel(reference)
    if removed is None:
        return f"No reminder matches {reference!r} (use reminders_list to see the ids)."
    return f"Cancelled {removed['id']}: {removed['text']}"


def handle_slash(raw_args: str = "", **kwargs) -> str:
    """In-session `/reminders` command: list pending reminders, or cancel one without an LLM turn."""
    tokens = (raw_args or "").strip().split()
    if not tokens:
        return handle_list({"all": False})
    sub = tokens[0].lower()
    if sub in ("all", "--all", "-a") and len(tokens) == 1:
        return handle_list({"all": True})
    if sub == "list" and len(tokens) == 1:
        return handle_list({"all": False})
    if sub == "list" and len(tokens) == 2 and tokens[1].lower() in ("all", "--all", "-a"):
        return handle_list({"all": True})
    if sub in ("cancel", "rm", "del", "delete") and len(tokens) == 2:
        return handle_cancel({"id": tokens[1]})
    return "Usage: /reminders [all | cancel <id>]"


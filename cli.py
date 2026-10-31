"""``hermes reminders …`` — the interface any local process can call, plus the installer
for the cron poller.

Same store as the agent's tools, so the two can never drift. Nothing here talks to a model;
nothing here holds a credential.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from . import message, sender
from .poller import run_once
from .store import ReminderStore, StoreError, hermes_home

STUB_NAME = "reminders-poller.py"
JOB_NAME = "reminders-poller"
SCHEDULE = "* * * * *"
TIMEOUT_SECONDS = 60

PLUGIN_DIR = Path(__file__).resolve().parent


def plugin_name() -> str:
    """The plugin's import name: its manifest name, which is also its installed directory name.

    Read from ``plugin.yaml`` rather than the folder, because the development checkout is
    ``hermes-reminders`` (not importable) while an install is ``plugins/reminders``.
    """
    manifest = PLUGIN_DIR / "plugin.yaml"
    try:
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if line.startswith("name:"):
                value = line.split(":", 1)[1].strip().strip('"').strip("'")
                if value:
                    return value
    except OSError:
        pass
    return PLUGIN_DIR.name


PLUGIN_NAME = plugin_name()

STUB_TEMPLATE = '''#!/usr/bin/env python3
"""Cron entry point for the `{plugin}` Hermes plugin — installed by `hermes reminders setup`.

The poller itself lives in the plugin ($HERMES_HOME/plugins/{plugin}/poller.py) and is imported
from there on every tick, so a plugin update updates the sender without touching this file.
Edit the plugin, not this stub.
"""
import sys
from pathlib import Path

_HOME = Path(__file__).resolve().parent.parent   # <HERMES_HOME>/scripts/<this file>
sys.path.append(str(_HOME / "plugins"))          # appended, so nothing here can shadow stdlib

from {plugin}.poller import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
'''


# --- the stub and the cron job ---------------------------------------------------------------

def stub_path() -> Path:
    """Where the cron stub lives — the only directory Hermes will run a cron script from."""
    return hermes_home() / "scripts" / STUB_NAME


def write_stub() -> Path:
    """(Re)write the stub. Idempotent: it is how a moved or renamed install is repaired."""
    path = stub_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(STUB_TEMPLATE.format(plugin=PLUGIN_NAME), encoding="utf-8")
    path.chmod(0o755)
    return path


def cron_command() -> str:
    return (
        f'hermes cron create "{SCHEDULE}" --name {JOB_NAME} --no-agent '
        f"--script {STUB_NAME} --deliver local --failure-deliver local"
    )


def scheduler_jobs() -> list[dict]:
    """The scheduler's jobs, read read-only from its own store (``cron/jobs.json``)."""
    path = hermes_home() / "cron" / "jobs.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return []
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    return [job for job in jobs if isinstance(job, dict)] if isinstance(jobs, list) else []


def installed_job() -> dict | None:
    """The plugin's poller job as the scheduler records it, if present — used to avoid adding a duplicate."""
    for job in scheduler_jobs():
        if job.get("name") == JOB_NAME or str(job.get("script") or "").endswith(STUB_NAME):
            return job
    return None


def create_job() -> tuple[bool, str]:
    executable = sender.hermes_executable()
    if not executable:
        return False, "the `hermes` command was not found on PATH"
    command = [
        executable, "cron", "create", SCHEDULE, "--name", JOB_NAME, "--no-agent",
        "--script", STUB_NAME, "--deliver", "local", "--failure-deliver", "local",
    ]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return False, f"`hermes cron create` timed out after {TIMEOUT_SECONDS}s"
    except OSError as exc:
        return False, f"could not run `hermes cron create`: {exc.__class__.__name__}"
    if proc.returncode != 0:
        return False, " ".join((proc.stderr or proc.stdout or "").split())[:300]
    return True, " ".join((proc.stdout or "").split())[:300]


# --- the commands ----------------------------------------------------------------------------

def handle_add(args) -> int:
    when = args.at or args.in_
    if not when:
        print("reminders: give a time — `--at 2026-11-01T15:00Z` or `--in 2h`", file=sys.stderr)
        return 2
    store = ReminderStore()
    record = store.add(args.text, when, created_by="cli", target=args.target)
    print(f"{record['id']}  {record['due_at']}  {record['text']}")
    print("will send:")
    print(message.render(record, store.template()))
    return 0


def handle_list(args) -> int:
    records = ReminderStore().all()
    if not args.all:
        records = [record for record in records if record.get("status") == "pending"]
    records.sort(key=lambda record: str(record.get("due_at", "")))
    if args.json:
        print(json.dumps(records, indent=2))
        return 0
    if not records:
        print("No reminders in the pool.")
        return 0
    for record in records:
        line = f"{record.get('id')}  {record.get('due_at')}  {record.get('text')}"
        if record.get("status") != "pending":
            line += f"  [{record.get('status')}: {record.get('last_error')}]"
        print(line)
    return 0


def handle_cancel(args) -> int:
    removed = ReminderStore().cancel(args.id)
    if removed is None:
        print(f"reminders: no reminder matches {args.id!r} (`hermes reminders list` shows the ids)", file=sys.stderr)
        return 1
    print(f"Cancelled {removed['id']}: {removed['text']}")
    return 0


def handle_setup(args) -> int:
    store = ReminderStore()
    if getattr(args, "target", None):
        store.set_default_target(args.target)
    if getattr(args, "template", None):
        store.set_template(args.template)
    print(f"store: {store.path}")
    print(f"stub:  {write_stub()}")

    if args.no_cron:
        print("cron:  skipped. Create the job with:\n  " + cron_command())
        return 0
    job = installed_job()
    if job:
        print(f"cron:  already installed (job {job.get('id')})")
        return 0

    ok, detail = create_job()
    if ok:
        print(f"cron:  installed {detail or ''}".rstrip())
        return 0
    print(f"cron:  could not create the job automatically — {detail}", file=sys.stderr)
    print("Create it with:\n  " + cron_command(), file=sys.stderr)
    return 1


def handle_poll(args) -> int:
    delivered, failed = run_once(verbose=True)
    if not delivered and not failed:
        print("Nothing due.")
    return 1 if failed else 0


def handle_status(args) -> int:
    store = ReminderStore()
    records = store.all()
    pending = [record for record in records if record.get("status") == "pending"]
    failed = [record for record in records if record.get("status") == "failed"]
    executable = sender.hermes_executable()
    job = installed_job()
    stub = stub_path()
    print(f"store:          {store.path}{'' if store.path.exists() else '  (not created yet)'}")
    print(f"default target: {store.default_target() or '— (set one with `hermes reminders setup --target <platform>`)'}")
    print(f"template:       {store.template() or 'default (🔔 {text} / (Reminder set on {created}))'}")
    print(f"reminders:      {len(pending)} pending, {len(failed)} failed")
    print(f"stub:           {stub}{'' if stub.exists() else '  (missing — run `hermes reminders setup`)'}")
    print(f"cron job:       {job.get('id') if job else 'not installed'}")
    print(f"hermes send:    {executable or 'NOT FOUND'}")
    return 0


def setup_parser(parser) -> None:
    """Attach the `reminders` subcommands and the single handler the host will call."""
    sub = parser.add_subparsers(dest="reminders_command", metavar="<command>")

    add = sub.add_parser("add", help="Add a reminder to the pool")
    add.add_argument(
        "text",
        help="The reminder, written as the sentence to deliver — addressed to the reader: 'Charge your phone'",
    )
    add.add_argument("--at", help="ISO 8601 time (2026-11-01T15:00Z); a time with no offset is read as local")
    add.add_argument("--in", dest="in_", metavar="DURATION", help="Time from now: 2h, 90m, 1d")
    add.add_argument("--target", help="Delivery target (e.g. telegram); defaults to the pool default")

    listing = sub.add_parser("list", help="List the reminders still in the pool")
    listing.add_argument("--all", action="store_true", help="Include reminders whose delivery failed")
    listing.add_argument("--json", action="store_true", help="Machine-readable output")

    cancel = sub.add_parser("cancel", help="Remove a reminder so it is never delivered")
    cancel.add_argument("id", help="Reminder id (a unique prefix is enough)")

    setup = sub.add_parser("setup", help="Install the poller stub and its cron job")
    setup.add_argument("--target", help="Set the default delivery target for the pool")
    setup.add_argument(
        "--template",
        help="Set the delivery format, e.g. '🔔 {text}\\n(Reminder set on {created})'. "
             "Placeholders: {text} {created} {created_utc} {due} {due_utc} {id}",
    )
    setup.add_argument("--no-cron", action="store_true", help="Write the stub only; print the job command")

    sub.add_parser("poll", help="Deliver everything due, once")
    sub.add_parser("status", help="Show store, stub, job and sender state")

    parser.set_defaults(func=handle)


def handle(args) -> int:
    """Entry point the host calls. Returns a process exit code."""
    command = getattr(args, "reminders_command", None)
    dispatch = {
        "add": handle_add, "list": handle_list, "cancel": handle_cancel,
        "setup": handle_setup, "poll": handle_poll, "status": handle_status,
    }
    if command not in dispatch:
        print("usage: hermes reminders add|list|cancel|setup|poll|status", file=sys.stderr)
        return 0
    try:
        return dispatch[command](args)
    except (StoreError, ValueError) as exc:
        print(f"reminders: {exc}", file=sys.stderr)
        return 1

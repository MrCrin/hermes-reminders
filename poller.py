"""One deterministic delivery pass — no model, no network of its own.

Run every minute by the cron job that `hermes reminders setup` installs, through the stub
in ``$HERMES_HOME/scripts/``. Also safe to run by hand: ``hermes reminders poll``.

Prints nothing when there is nothing to do (the cron job's own delivery is silent), and
exits non-zero only when a send failed, so a broken sender shows up in the job's history
rather than disappearing.
"""

from __future__ import annotations

import os
import sys

from . import message
from .sender import send
from .store import ReminderStore, StoreError, utcnow


def run_once(store: ReminderStore | None = None, *, verbose: bool = False) -> tuple[int, int]:
    """Deliver everything due. Returns ``(delivered, failed)``."""
    store = store or ReminderStore()
    now = utcnow()
    due = store.due(now)
    template = store.template()
    default_target = store.default_target()
    delivered = failed = 0
    for record in due:
        target = record.get("target") or default_target
        ok, error = send(message.render(record, template), target)
        if not ok:
            failed += 1
            store.mark_failed_attempt(record["id"], error)
            print(f"reminders: {record['id']} not delivered: {error}", file=sys.stderr)
            continue
        advanced = store.mark_delivered(record["id"], now=now)
        if advanced is not None and advanced.get("status") == "failed":
            # The message went out, but the repeat could not be advanced, so the store has
            # retired it. Counting it as a failure is what carries that into the job's history:
            # the poller is silent when it succeeds, so a stderr line alone would scroll away.
            failed += 1
            print(
                f"reminders: {record['id']} repeat retired: {advanced.get('last_error')}",
                file=sys.stderr,
            )
        else:
            delivered += 1
    if verbose and due:
        print(f"reminders: {delivered} delivered, {failed} failed, {len(due)} due")
    return delivered, failed



def main(argv: list[str] | None = None) -> int:
    try:
        _, failed = run_once(verbose=os.environ.get("REMINDERS_VERBOSE") == "1")
    except StoreError as exc:
        print(f"reminders: {exc}", file=sys.stderr)
        return 1
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover - convenience for `python -m reminders.poller`
    raise SystemExit(main())

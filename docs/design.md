# Design — `reminders` (hermes-reminders)

## What this is

A reminder pool for Hermes Agent. Something asks for a reminder (the agent through a
tool, a local service through the CLI); the reminder lands in one small JSON store; one
no-LLM cron job wakes every minute, sends whatever is due, and removes it **only once
delivery is confirmed**.

The point is that setting a reminder costs one tool call, and delivering it costs no
tokens at all — not a model call, not an agent turn.

## The one constraint that shapes everything

Hermes' cron scheduler runs *scripts*, and it enforces where those scripts live: a cron
script must resolve inside `$HERMES_HOME/scripts/` (`README` of the scheduler: path
traversal and symlink escapes are rejected at run time). A plugin's own directory is
therefore never executable as a cron script.

So the plugin owns the code and installs a **stub**:

```
$HERMES_HOME/scripts/<plugin-name>-poller.py      ~6 lines, written once by `setup`
        |
        |  appends $HERMES_HOME/plugins to sys.path, then imports
        v
$HERMES_HOME/plugins/reminders/poller.py          the real poller, shipped in the repo
```

The stub works out the Hermes home from its own path (`Path(__file__).resolve().parent.parent`),
so it needs no environment variables — cron scripts do not inherit the agent process's
environment. Consequences that matter:

- **Plugin updates update the sender.** The logic is read from the plugin directory on
  every tick; only the stub sits in the scripts directory, and it never changes.
- **`setup` is idempotent.** Re-running it rewrites the stub and re-points the job, which
  repairs a moved or renamed install.
- **The poller must stay stdlib-only.** Cron scripts run under Hermes' own Python, which
  carries Hermes' dependencies and not necessarily the plugin's. `poller.py` therefore
  imports nothing outside the standard library and shells out for delivery.
- **Nothing is patched.** The job is an ordinary Hermes cron job the user owns; the plugin
  only creates it, using the public CLI.

## Layers

1. **Store** — one JSON file (`$HERMES_HOME/reminders.json` by default, overridable).
   Atomic write (temp file + `os.replace`) under an advisory lock (`fcntl.flock`), so the
   agent, the CLI and the poller can all touch it safely.
   Each record: `id`, `text`, `due_at` (UTC, absolute), `created_at`, `created_by`,
   `target` (optional per-reminder override), `attempts`, `last_error`.
2. **Interfaces** —
   - agent tools: `reminders_add`, `reminders_list`, `reminders_cancel`;
   - a bundled skill (`reminders:using`) whose job is discoverability: given only the tools, a
     request like "remind me in 5 minutes" is easy to misread as a system to go and find, so the
     agent starts reading files and memory instead of calling `reminders_add`;
   - CLI: `hermes reminders add|list|cancel|setup|poll|status` — the stable contract any local
     process can call (a voice lane, a script, a cron job of its own).
   Both go through the same store module, so they cannot drift.
3. **Poller** — `poller.main()`: read the store, take everything with `due_at <= now`,
   send it, prune on success. Prints nothing when there is nothing to do, so the cron
   job is silent, and never calls a model.
4. **Setup** — `hermes reminders setup`: writes the stub into `$HERMES_HOME/scripts/`,
   chmod +x, and creates the cron job (`* * * * *`, `--no-agent`, `--deliver local`) if it
   does not already exist.

## Why one job, not one job per reminder

A one-shot cron job that comes due while the scheduler is down is retired with a
diagnostic rather than delivered late (past its ~120-second grace). For reminders that is
the wrong failure: the box restarting at the wrong minute would silently eat the
reminder. A store plus a poller delivers late-but-certain instead, keeps every pending
reminder inspectable and cancellable, and keeps N reminders out of N job records.

## Delivery and pruning

Delivery is `hermes send --to <target> "<text>"`. That reuses the platform credentials
Hermes is already configured with, so there are no tokens or chat ids in this plugin, and
it needs no running gateway for bot-token platforms.

**Presentation happens here, not in the store.** `message.render()` turns a record into the
message that lands — by default a 🔔-prefixed sentence followed by `(Reminder set on …)`, in
the local time of the machine running the poller. The stored `text` stays exactly as given, so
changing the format never rewrites the pool, and the format is one template string the user
owns (`hermes reminders setup --template`). A template with a broken placeholder falls back to
the default: a typo must not cost someone a reminder.

`hermes send` exits 0 only when the platform accepted the message, and 1 when delivery
failed at the platform level. That exit code is the whole pruning rule:

- exit 0 → remove the record (a reminder is not repeated);
- non-zero → keep it, record `last_error`, and retry on the next tick with backoff,
  up to `max_attempts` (default 10), after which it is marked `failed` and stays visible
  in `list`. A reminder is never silently discarded, and never silently repeated.

## Non-goals

- Recurring reminders — that is what `hermes cron` is for.
- Scheduling agent work — likewise.
- Calendar integration, multi-user delivery, per-reminder retry policies.

## What a user should know before installing (disclosures)

- Writes and reads one JSON file under `$HERMES_HOME` (the store).
- Writes one small stub script into `$HERMES_HOME/scripts/`.
- Creates **one** Hermes cron job, in no-agent mode; it never calls a model.
- Runs the `hermes` CLI as a subprocess (`send`) to deliver, and to create the job.
- Makes no network calls of its own; no telemetry; no credentials of its own; touches no
  files outside `$HERMES_HOME`.

## Testing

`tests/` runs against a throwaway `HERMES_HOME` and a fake `hermes` on `PATH`, so no test
touches the live store, the live cron schedule or a real chat.

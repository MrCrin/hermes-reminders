# hermes-reminders

A reminder pool for [Hermes Agent](https://hermes-agent.nousresearch.com/docs/). One tool call
to set a reminder, one cron job to deliver them, and **no model involved in delivery**.

- **Set one from anywhere** — the agent calls `reminders_add`; any local process runs
  `hermes reminders add`. Same store, same rules.
- **Delivery costs nothing** — a single cron job in no-agent mode sends whatever is due. No
  LLM call, no tokens, no agent turn.
- **Never silently lost** — a reminder that fails to send is kept, backed off and retried; it
  is removed **only once the platform has confirmed delivery**.
- **Late beats absent** — the pool holds the work instead of the scheduler holding a one-shot
  job, so a reminder that comes due while Hermes is down is delivered when it comes back
  rather than dropped.

## Why not just a one-shot cron job per reminder?

Hermes documents one-shot jobs for exactly this, and for a single dated nudge they are the
right tool. They make a poor *reminder system* for two reasons: a one-shot that comes due
while the scheduler is down is retired with a diagnostic rather than delivered late, and N
reminders become N job records to inspect. A pool with one poller gives catch-up delivery,
one place to list and cancel, and constant overhead regardless of how many reminders are
pending.

## Install

```bash
# from the plugin catalog, once listed
hermes plugins install reminders
hermes plugins enable reminders

# or straight from a commit
hermes plugins install <owner>/hermes-reminders --ref <40-hex-sha> --yes-deps --enable
```

Requires Hermes `>= 0.22.0` and a messaging platform that `hermes send --list` can reach.

## Setup (once)

```bash
hermes reminders setup --target telegram
```

That writes the poller stub into `$HERMES_HOME/scripts/` and creates exactly one cron job —
`* * * * *`, no-agent mode — that runs it every minute. Check the whole chain at any time:

```bash
hermes reminders status
```

`--no-cron` writes the stub only and prints the `hermes cron create` line, for anyone who
prefers to schedule it themselves.

## Use it

Agent tools: `reminders_add` (`text`, `when`, optional `target`), `reminders_list`,
`reminders_cancel`.

From any shell — the interface the plugin's CLI gives every local process:

```bash
hermes reminders add "check the database" --in 2h
hermes reminders add "ring the dentist" --at 2026-11-01T15:00Z --target telegram
hermes reminders list --json
hermes reminders cancel 3f9c1a2b
hermes reminders poll        # deliver everything due, once
```

`--in` takes a duration (`30m`, `2h`, `1.5h`, `1d`). `--at` takes ISO 8601; a timestamp with
no offset is read as local time, and `Z`/explicit offsets are respected as given. Times are
stored as absolute UTC instants, so daylight-saving changes cannot move a reminder.

## How it works

```
hermes reminders add …  /  reminders_add tool          one JSON pool
                  │                                      $HERMES_HOME/reminders.json
                  └──────────────► pool ◄───────────┐
                                                   │  read
         cron job  (* * * * *, no agent, --deliver local)
                  │                                │
                  └─► $HERMES_HOME/scripts/reminders-poller.py  (6-line stub)
                                │ imports
                                └─► $HERMES_HOME/plugins/reminders/poller.py
                                              │ hermes send --to <target> …
                                              ▼
                                       platform, then prune on exit 0
```

Hermes runs cron scripts only from `$HERMES_HOME/scripts/`, so the plugin puts a small stub
there that imports the real poller from the plugin directory. The logic therefore always
comes from the installed plugin — a plugin update updates the sender without touching the
stub, and re-running `hermes reminders setup` repairs a moved or renamed install. The full
reasoning, including the retry and pruning rules, is in [docs/design.md](docs/design.md).

## Configuration

| What | How |
|---|---|
| Default delivery target | `hermes reminders setup --target telegram` (per reminder: `--target`) |
| Store location | `$HERMES_HOME/reminders.json`; override with `REMINDERS_STORE` |
| Retry limit per reminder | `"max_attempts"` in the store file (default 10) |
| Poller chattiness | `REMINDERS_VERBOSE=1` makes an empty pass print a summary instead of staying silent |

## What this does on your machine

Disclosed because you should know before installing:

- reads and writes `$HERMES_HOME/reminders.json` (plus a `.lock` file beside it);
- writes `$HERMES_HOME/scripts/reminders-poller.py`;
- creates **one** cron job (`reminders-poller`, every minute, no-agent mode) — and reads
  `$HERMES_HOME/cron/jobs.json` read-only first, so it never creates a duplicate;
- runs the `hermes` CLI as a subprocess: `hermes send` to deliver, `hermes cron create` during
  `setup`;
- makes no network calls of its own, sends no telemetry, and holds no credentials of its own —
  delivery credentials stay in Hermes' own configuration;
- touches nothing outside `$HERMES_HOME`;
- Linux and macOS only (the store's lock uses `fcntl`).

## Development

```bash
python3 -m unittest discover -s tests -t tests   # no install, no live home needed
hermes plugins validate .                        # the catalog admission gate
```

Every test runs against a throwaway `HERMES_HOME` and a fake `hermes` on `PATH`, so no test
can touch a real store, a real cron schedule or a real chat.

## Licence

MIT — see [LICENSE](LICENSE).

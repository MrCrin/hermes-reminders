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

`setup` also records the job as one whose completions are not worth announcing, so a surface
that reports every finished job (the Hermes WebUI toasts completions and counts them in its
Tasks badge) does not turn a silent minute-by-minute poller into noise. Those are ordinary
per-job fields — turn them back on for that job if you want the confirmation.

## Use it

Agent tools: `reminders_add` (`text`, `when`, optional `every`, optional `target`),
`reminders_list`, `reminders_cancel`. The plugin also registers the `/reminders` slash command
(`/reminders`, `/reminders all`, `/reminders cancel <id>`) so users can inspect or cancel
reminders in Telegram, WebUI, or the CLI without an LLM call, plus a skill (`reminders:using`)
that tells an agent to reach for the tool in one call instead of rediscovering the store, the
CLI or the cron job.

From any shell — the interface the plugin's CLI gives every local process:

```bash
hermes reminders add "check the database" --in 2h
hermes reminders add "ring the dentist" --at 2026-11-01T15:00Z --target telegram
hermes reminders add "take vitamins" --at 2026-11-12T08:00:00 --every day
hermes reminders list --json
hermes reminders cancel 3f9c1a2b
hermes reminders poll        # deliver everything due, once
```

`--in` takes a duration (`30m`, `2h`, `1.5h`, `1d`). `--at` takes ISO 8601; a timestamp with
no offset is read as local time, and `Z`/explicit offsets are respected as given. `--every`
takes an interval (`2h`, `7d`) or a calendar rule (`day`, `weekday`, `week`, `month`,
`mon,wed,fri`); each delivery advances `due_at` from the original `anchor_at` timestamp to the
next future occurrence so schedules never drift and downtime never triggers a burst of missed
messages.


## How a reminder arrives

```
🔔 Charge your phone
(Reminder set on 31 Oct 2026, 12:43)
```

The bell and the provenance line are added at delivery, not stored, so `text` should be the
sentence you want to read: addressed to the reader, imperative — "Charge your phone", not
"Charge my phone". Change the shape with
`hermes reminders setup --template "…"`; the placeholders are `{text}`, `{created}`,
`{created_utc}`, `{due}`, `{due_utc}` and `{id}`, with `{created}`/`{due}` in the local time
of the machine running the poller. A template with a typo in it falls back to the default
rather than costing you a reminder.

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
| Delivery format | `hermes reminders setup --template "…"` — default is `🔔 {text}` / `(Reminder set on {created})` |
| Store location | `$HERMES_HOME/reminders.json`; override with `REMINDERS_STORE` |
| Retry limit per reminder | `"max_attempts"` in the store file (default 10) |
| Poller chattiness | `REMINDERS_VERBOSE=1` makes an empty pass print a summary instead of staying silent |

## What this does on your machine

Disclosed because you should know before installing:

- reads and writes `$HERMES_HOME/reminders.json` (plus a `.lock` file beside it);
- writes `$HERMES_HOME/scripts/reminders-poller.py`;
- creates **one** cron job (`reminders-poller`, every minute, no-agent mode) — and reads
  `$HERMES_HOME/cron/jobs.json` read-only first, so it never creates a duplicate;
- marks that job as one whose completions should not be announced (`toast_notifications` and
  `badge_notifications` off), writing it through the scheduler's own store API — so web UIs and
  dashboards that toast every finished job stay quiet;
- runs the `hermes` CLI as a subprocess: `hermes send` to deliver, `hermes cron create` during
  `setup`;
- makes no network calls of its own, sends no telemetry, and holds no credentials of its own —
  delivery credentials stay in Hermes' own configuration;
- touches nothing outside `$HERMES_HOME`;
- Linux and macOS only (the store's lock uses `fcntl`).

## How Hermes finds this plugin

Nothing here has to be *searched for*, and that is deliberate:

- **The tools are always listed.** They register in a `reminders` toolset, so Hermes includes
  them by name in the Tool Search catalogue that ships in every session's context. A model that
  sees the name skips straight to `tool_describe` → `tool_call`; it never needs `tool_search`.
- **The skill is always listed too.** The plugin registers a read-only skill, `reminders:using`,
  which appears in the session's skill catalogue with its description (Hermes' plugin-skill
  registry — the same mechanism other plugins use). The body is read only when the detail is
  wanted.

Two things that are Hermes' policy rather than this plugin's choice, worth knowing:

- **Schemas are fetched on demand.** Hermes progressively discloses plugin and MCP tool schemas
  (Tool Search), so the first call to a reminder tool costs one `tool_describe` for the
  parameters. There is no per-plugin opt-out; the only switch is the global
  `tools.tool_search` setting (`defer: []` / `enabled: off`), which also makes cold built-ins
  like `session_search` and `cronjob_manage` eager and pays their schema cost every turn.
- **A surface that started before the plugin did will not see it.** Hermes builds each process's
  plugin registry when that process starts, so a long-running surface — a WebUI or dashboard
  server, a desktop backend — installed-before keeps its old registry, and sessions it serves get
  no reminder tools *and* no bundled skill until that process restarts. Surfaces that re-discover
  plugins (the gateway, the CLI) need only a new session. If an agent says it has no reminder
  tools, that is the first thing to check.
- **A plugin cannot force an agent to comply.** It can make the tool name, the description and
  the instructions unmissable — which is what the descriptions and the bundled skill do. A
  profile that wants the reminder route guaranteed regardless of the task adds one standing line
  to its own `SOUL.md`.

## Versioning

Semantic Versioning, declared in `plugin.yaml` and `pyproject.toml` — a test keeps the two in step,
because a version that drifts from the code is worse than no version at all.

The public interface is the tool names and their arguments, the CLI, and the store file format. A
breaking change to any of those is a major bump; the store also carries its own `version` field so a
format change can migrate rather than break. Notable changes are recorded in
[CHANGELOG.md](CHANGELOG.md), and a release is an annotated git tag (`v0.1.0`).

## Development

```bash
python3 -m unittest discover -s tests -t tests   # no install, no live home needed
hermes plugins validate .                        # the catalog admission gate
```

Every test runs against a throwaway `HERMES_HOME` and a fake `hermes` on `PATH`, so no test
can touch a real store, a real cron schedule or a real chat.

## Licence

MIT — see [LICENSE](LICENSE).

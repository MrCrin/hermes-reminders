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
   The home is the one Hermes is operating on: `hermes_constants.get_hermes_home()` resolves the
   profile the current call belongs to before it looks at `HERMES_HOME`, and the environment is
   used only where Hermes cannot be imported, which is the standalone poller's case.
   Atomic write (temp file + `os.replace`) under an advisory lock (`fcntl.flock`) where the
   platform provides one, so the agent, the CLI and the poller can all touch it safely. Where
   `fcntl` is absent the lock is skipped, which is why no platform subset is declared.
   Each record: `id`, `text`, `due_at` (UTC, absolute), `every` (optional recurrence rule),
   `anchor_at` (the immutable UTC instant the schedule is counted from), `anchor_local` (the
   wall clock that instant means to the reader — see below), `created_at`, `created_by`,
   `target` (optional per-reminder override), `status` (`pending` or `failed`), `attempts`,
   `last_error`, `next_attempt_at` (when a failed send may be retried).

   Beside the records the pool holds three settings, all optional and all written by
   `hermes reminders setup`: `default_target`, `template` and `max_attempts`. The last is how
   many times a failed delivery is retried before the record is marked `failed`; the default is
   10. An absent or unusable value means the built-in default, so a typo in the file cannot cost
   a reminder.

   The file also carries a `version` field (the schema version, currently 1), so a change to the
   on-disk format can migrate an existing pool rather than break it.

   A recurring record carries two anchors, and they answer different questions. `anchor_at` is
   the instant every slot is counted from and is never rewritten, so a schedule cannot drift.
   `anchor_local` is what the next slot is laid out on, so "every day at 19:00" stays at 19:00
   when the clocks change or the reader moves timezone. A rule with a time of day cannot be
   computed from the instant alone: on the day the clocks go forward, a local time and an
   instant are not a one-to-one mapping, and a 02:30 reminder read back off its own instant
   would come back as 03:30 and stay there.

   A rule is one string, and `parse_every` is its whole vocabulary. It accepts, and canonicalises
   to, one of four families:

   | Family | Rule | Canonical |
   | --- | --- | --- |
   | a raw interval | `2h`, `7d`, `1.5h` | `2h`, `7d`, `1.5h` |
   | a named cycle | `day`, `weekday`, `weekend`, `week`, `month` | the same |
   | a cycle of weeks | `2w`, `2w:mon,wed` | the same |
   | a week of the month | `first:mon`, `last:fri`, `2:tue` | `first:mon`, `last:fri`, `second:tue` |

   A day list (`mon,wed,fri`) belongs to the named-cycle family: it is `weekday`/`weekend` spelled
   out. `1w` collapses to `week`, and `1w:mon` to `mon`, because they are the same schedule.

   A raw interval is seconds of arithmetic from the anchor, which is what makes `2h` exact and
   `7d` drift by an hour across a change of the clocks. A week cycle is deliberately not that: it
   is a calendar week, so `2w` keeps its clock time forever. The two are distinguished by unit,
   `d` against `w`, so `14d` and `2w` are the same fortnight except across a timezone change.

   The rules that name days — `weekday`, `weekend`, `mon,wed,fri`, `2w:mon,wed`, `first:mon` —
   also fix which day the *first* delivery lands on, so a weekday reminder set at the weekend
   starts on the Monday rather than firing on the Saturday and settling down afterwards. The day
   walk searches one full cycle plus its starting day, which is the most any of them can need: a
   slot that has already passed today is one cycle away. A multi-week cycle counts whole weeks
   from the anchor, so `2w:mon,wed` fires both days of one week and neither of the next.

   A week of the month is refused at the fifth (`fifth:monday`), because a fifth Monday exists in
   some months and not others, so the rule would fire irregularly. `last:mon` is the final Monday
   of every month. Anything the grammar cannot express, `every other day` included, is refused
   rather than guessed at: a refusal costs one retry, and a guess delivers the wrong reminder.
2. **Interfaces** —
   - agent tools: `reminders_add`, `reminders_list`, `reminders_cancel`;
   - slash command: `/reminders` (`/reminders [all | cancel <id>]`), executed directly without
     an LLM turn across CLI, WebUI and Telegram;
   - a bundled skill (`reminders:using`) whose job is discoverability: given only the tools, a
     request like "remind me in 5 minutes" is easy to misread as a system to go and find, so the
     agent starts reading files and memory instead of calling `reminders_add`;
   - CLI: `hermes reminders add|list|cancel|setup|poll|status` — the stable contract any local
     process can call (a voice lane, a script, a cron job of its own).
   All go through the same store module, so they cannot drift.

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

- exit 0 → remove a one-shot record, or advance a recurring record's `due_at` from `anchor_at`
  to the next slot after `now` and clear its retry state;
- non-zero → keep it, record `last_error`, and retry on the next tick with backoff, up to
  `max_attempts` (default 10, set with `hermes reminders setup --max-attempts`), after which it
  is marked `failed` and stays visible in `list`. A reminder is never silently discarded, and
  never silently repeated.

There is a third outcome, and it is what keeps the last sentence honest. A recurring record
whose rule cannot be read, or cannot be advanced, is **retired**: marked `failed` with the
reason in `last_error`. By then the message has already gone out, so leaving the record pending
would put the same sentence on someone's screen every minute for as long as the rule stays
unreadable, and a rule that cannot advance on this tick will not advance on a later one, so
there is nothing to wait for. The poller counts a retirement as a failure, which is what makes
its exit code non-zero and carries the event into the cron job's history; `reminders_list --all`
shows the record and the reason.

## Non-goals

- Scheduling agent work: that is what `hermes cron` is for.
- Calendar integration, multi-user delivery, per-reminder retry policies.



## What a user should know before installing (disclosures)

- Writes and reads one JSON file under `$HERMES_HOME` (the store).
- Writes one small stub script into `$HERMES_HOME/scripts/`.
- Creates **one** Hermes cron job, in no-agent mode; it never calls a model.
- Leaves that job's own settings alone. A quiet pass prints nothing, but the job still completes
  every minute, so a surface that announces cron completions announces this one.
- Runs the `hermes` CLI as a subprocess (`send`) to deliver, and to create the job.
- Makes no network calls of its own; no telemetry; no credentials of its own; touches no
  files outside `$HERMES_HOME`.

## Testing

`tests/` runs against a throwaway `HERMES_HOME` and a fake `hermes` on `PATH`, so no test
touches the live store, the live cron schedule or a real chat.

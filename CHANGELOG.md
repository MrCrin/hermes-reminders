# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.2] - 2026-11-27

### Fixed

- The store resolved the Hermes home from `$HERMES_HOME` alone. One Hermes process serving several
  profiles carries only the launch profile in its environment, so a reminder set for another
  profile was written into the launch profile's pool and delivered with its credentials. The home
  now comes from Hermes itself wherever Hermes is importable; the environment is used only by the
  standalone poller, which Hermes starts with the owning profile's home.
- `hermes reminders setup` no longer writes the poller job's `toast_notifications` and
  `badge_notifications` into `$HERMES_HOME/cron/jobs.json`. That file is Hermes' own, and the two
  fields are a surface preference rather than plugin state. A fresh setup therefore shows
  completion notices for the poller job until they are switched off in the job's own form; a job
  that already exists keeps what it has.
- The cron stub no longer adds `~/.hermes/plugins` to its import path, so a stub installed outside
  the default home cannot import a plugin from an unrelated home.

## [0.3.1] - 2026-11-25

### Fixed

- The message on an unsupported ordinal such as `fifth:mon` said a month has at most four of any
  weekday, which is not true: a 31-day month that starts on a Monday holds five of three
  weekdays. The refusal is unchanged, and only the reason given for it was wrong.

### Removed

- The declared platform subset in `plugin.yaml`. The store takes its lock only where `fcntl`
  exists and skips it otherwise, so no platform needs to be excluded.

## [0.3.0] - 2026-11-22

### Added

- `weekend` and `weekends` as recurrence rules: Saturday and Sunday, the mirror of `weekday`.
  A day list already covered this (`sat,sun`), so nothing that worked before stops working.
- A cycle of weeks: `2w` for every other week, `2w:mon,wed` for every other week on two days,
  and `3w` and up. A week cycle is a calendar week rather than 604800 seconds, so it keeps its
  clock time across a change of the clocks and across a move to another timezone; `14d` remains
  the raw-interval spelling of a fortnight. `2w:mon,wed` fires both days of one week and neither
  of the next, because the cycle counts whole weeks from the anchor.
- A week of the month: `first:mon` for the first Monday of each month, `last:fri` for the last,
  and `second:tue`, `third:wed`, `fourth:thu` for those between. A day list works here too, and
  `last:mon,wed` means whichever of the two the month reaches last.
- The phrases these rules are usually written as are read as the rules themselves: `every other
  week`, `biweekly`, `fortnightly`, `every second tuesday`, `the first monday of the month`, and
  `every <rule>` in general, so `every monday` is `mon` and `every 2 weeks` is `2w`.
- The retry limit is a pool setting: `hermes reminders setup --max-attempts N` sets how many
  times a failed delivery is retried before the reminder is marked `failed`, and
  `hermes reminders status` reports it. It sits beside `default_target` and `template` in the
  pool file, and an unusable value there is treated as absent rather than obeyed, so a typo
  cannot cost a reminder.

### Changed

- **A repeating reminder now starts on the first slot its pattern matches.** A `weekday`
  reminder set for a Saturday was delivered on the Saturday and only settled into the pattern
  from its second delivery onward. The rule now decides the first delivery as well, which moves
  that first message to the Monday. The clock time is still the one that was asked for, and a
  reminder whose time already matches its rule is untouched.
- The day-pattern walk searches a derived window (one cycle plus its starting day) instead of a
  hard-coded 15 days, so the bound is stated where a longer pattern would have to change it.
- A rule the plugin cannot schedule is refused with one message naming the whole vocabulary,
  rather than a message naming only the token that failed, so a caller learns what it may ask
  for in one step. Two refusals are deliberate: `fifth:monday`, because a month holds at most
  four of a weekday and a fifth would silently skip the months that hold none, and `every other
  day`, because a two-day cycle is not a shape this grammar has and reading it as `day` would
  deliver twice as often as asked.

### Fixed

- A repeating reminder whose stored rule cannot be read is no longer delivered every minute.
  The delivery pass sent the message and then failed to advance the rule, which left the
  reminder `pending` and overdue, so the next tick sent the same message again, indefinitely.
  The reminder is now retired with the reason recorded against it, and the poller reports the
  retirement as a failure so it reaches the job's history rather than passing silently.
- The wall-clock anchor of a repeating reminder now survives a daylight-saving gap. A 02:30
  reminder set on the day the clocks go forward was stored as 03:30 and stayed there for good;
  the time of day that was asked for is the one kept.
- A repeating reminder whose first delivery was asked for a fraction of a second ahead of its
  pattern's own slot was pushed a whole cycle forward. "In five minutes" lands mid-second and
  the store keeps whole seconds, so the first slot is now rounded to the second it is written
  as: a weekday reminder asked for a Wednesday at 09:00:00.4 starts that Wednesday, not the
  Thursday.
- A stored rule or timestamp that is not a string (a number, a list) is refused as a value
  rather than raising an `AttributeError` out of the store. One unreadable record no longer
  stops the poller, which reads every record in the pool on every tick.

## [0.2.0] - 2026-11-14

### Added

- Recurring reminders via `every` on `reminders_add` and `--every` on `hermes reminders add`,
  supporting fixed intervals (`2h`, `7d`) and calendar schedules (`day`, `weekday`, `week`,
  `month`, `mon,wed,fri`). Delivered recurring reminders advance `due_at` from their immutable
  `anchor_at` to the next slot strictly after `now`, preventing schedule drift and catch-up floods.
- In-session `/reminders` slash command (`/reminders`, `/reminders all`, `/reminders cancel <id>`)
  for listing and cancelling reminders in local time without an LLM turn.

### Fixed

- Naive timestamps, calendar recurrences, and local message timestamps read `timezone` from
  `$HERMES_HOME/config.yaml` before falling back to the process clock, so CLI, WebUI, and poller
  processes on a UTC host use the configured Hermes timezone.


## [0.1.0] - 2026-10-31

### Added

- Three agent tools — `reminders_add`, `reminders_list`, `reminders_cancel` — over one JSON pool
  (`$HERMES_HOME/reminders.json`), written atomically under an advisory lock so the agent, the CLI
  and the poller can share it.
- `hermes reminders add|list|cancel|setup|poll|status`: the CLI interface any local process can
  call, sharing the same store as the tools.
- One cron job (`* * * * *`, no-agent mode) delivers whatever is due and prunes a reminder **only
  once `hermes send` reports that the platform accepted it**. A failed send is kept, backed off and
  retried, and never silently discarded; a delivered reminder is never repeated.
- The delivery format is applied at send time — 🔔, the sentence, then `(Reminder set on …)` in
  local time — and is a template the user owns (`hermes reminders setup --template`). A template
  with a broken placeholder falls back to the default rather than costing a reminder.
- Reminders are stored as absolute UTC instants, so daylight-saving changes cannot move one.
- A bundled skill, `reminders:using`, so an agent asked for a reminder calls the tool instead of
  rediscovering the system through the filesystem, memory or the CLI.
- `hermes reminders setup` installs the poller stub into `$HERMES_HOME/scripts/` and creates the
  cron job, idempotently.
- `setup` also marks the job as one whose completions should not be announced, so a surface that
  reports every finished job — a web UI toasting completions and counting them in its task badge —
  does not announce a silent poller 1,440 times a day.

[Unreleased]: https://github.com/MrCrin/hermes-reminders/compare/v0.3.2...HEAD
[0.3.2]: https://github.com/MrCrin/hermes-reminders/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/MrCrin/hermes-reminders/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/MrCrin/hermes-reminders/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/MrCrin/hermes-reminders/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/MrCrin/hermes-reminders/releases/tag/v0.1.0

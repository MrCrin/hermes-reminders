# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Recurring reminders via `every` on `reminders_add` and `--every` on `hermes reminders add`,
  supporting fixed intervals (`2h`, `7d`) and calendar schedules (`day`, `weekday`, `week`,
  `month`, `mon,wed,fri`). Delivered recurring reminders advance `due_at` from their immutable
  `anchor_at` to the next slot strictly after `now`, preventing schedule drift and catch-up floods.
- In-session `/reminders` slash command (`/reminders`, `/reminders all`, `/reminders cancel <id>`)
  for listing and cancelling reminders in local time without an LLM turn.


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

[Unreleased]: https://github.com/MrCrin/hermes-reminders/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/MrCrin/hermes-reminders/releases/tag/v0.1.0

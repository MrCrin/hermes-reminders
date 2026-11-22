---
name: using
description: Use when the user asks to be reminded of something at a time, or asks what reminders are set — set one with the reminders_add tool rather than hunting for the store or the CLI.
version: 1.0.0
metadata:
  hermes:
    tags: [reminders, time, tools]
---

# Reminders (hermes-reminders)

This bundled skill is available as `reminders:using`.

The plugin owns a pool of reminders. Reach it **with the tools**: `reminders_add`,
`reminders_list`, `reminders_cancel`. Do not rediscover the system — no searching the
filesystem, no reading `~/.hermes/reminders.json` by hand, no shelling out to
`hermes reminders …`. Those routes exist for other programs; the tools are the one-call path.

## Set one

```
reminders_add(text="Charge your phone", when="5m")
reminders_add(text="Stand up and stretch", when="2026-11-12T09:00:00", every="day")
```

- **`text` is the sentence that lands on the user's screen.** Write it addressed to them, in
  the second person and imperative: "Charge your phone", not "Charge my phone". No emoji and
  no timestamp — delivery formats those (a 🔔 and a `(Reminder set on …)` line).
- **`when`** is a duration from now (`5m`, `2h`, `1d`) or an absolute ISO 8601 time
  (`2026-11-01T15:00Z`; a time with no offset is read as local).
- **`every`** (optional) sets a repeating schedule: a duration (`2h`, `7d`) or a calendar
  rule: `day`, `weekday` (Mon to Fri), `weekend` (Sat and Sun), `week` (the same weekday each
  week), `month` (the same day of the month), a comma-separated list of days (`mon,wed,fri`),
  a cycle of weeks (`2w` every other week, `2w:mon,wed` every other week on two days), or a
  week of the month (`first:mon`, `last:fri`, also `second:tue`, `third:wed`, `fourth:thu`).
  The clock time is the one in `when`. When delivered, `due_at` advances from the initial
  anchor time to the next future slot. The rule also picks the **first** delivery, so "every
  weekday at 9am" asked for on a Saturday arrives on the Monday.
- **Delivery goes to the pool's configured target** — normally the user's messaging home
  channel — and does not come back into this session. Say when it will arrive; don't speculate
  about how it gets there.
- One call is the whole job. Reply in one line — the id, the local time it will arrive, and
  nothing else. Don't narrate the lookup, and don't offer to check anything.

## Look at them, cancel one

```
reminders_list()                       # ids, due times (local + UTC), recurrence and text
reminders_cancel(id="c8eb6cea")        # an unambiguous id prefix works; stops recurring ones too
```

The user can also run `/reminders` (or `/reminders all`, `/reminders cancel <id>`) directly in
chat without calling the model.


## What not to do

- **Don't create a cron job for a reminder.** The pool exists so that all reminders share one
  no-LLM job; a one-shot job would also be retired rather than delivered if Hermes were down
  when it came due.
- **Don't read or write the pool file.** The tools take the lock and write atomically;
  editing it by hand can lose a reminder.
- **If `reminders_add` isn't available**, this session cannot see the plugin: it is either not
  enabled, or the surface's process started before the plugin was installed (a WebUI or dashboard
  server keeps its own registry until it restarts). Say that, and stop — **do not build a cron job
  instead**. A cron job runs a model to say the sentence, costs tokens, and formats nothing.

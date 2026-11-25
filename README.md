# hermes-reminders

[![CI](https://github.com/MrCrin/hermes-reminders/actions/workflows/ci.yml/badge.svg)](https://github.com/MrCrin/hermes-reminders/actions/workflows/ci.yml)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)
[![Hermes](https://img.shields.io/badge/Hermes-%E2%89%A50.22.0-8a5cf6.svg)](https://hermes-agent.nousresearch.com/docs/)

A simple reminder plugin for [Hermes Agent](https://hermes-agent.nousresearch.com/docs/) that won't clutter your cron.

- You ask for a reminder in your own words, and Hermes uses the plugin to turn it into an entry in reminders.json
- The plugin needs only one cron job, so your cron list stays clean.
- Scripts deliver the reminders. No further agent turns, and no further tokens.

## Install

From the plugin catalogue*:

```bash
hermes plugins install reminders
hermes plugins enable reminders
```

From a commit:

```bash
hermes plugins install MrCrin/hermes-reminders --ref <40-hex-sha> --yes-deps --enable
```

Requires Hermes 0.22.0 or newer, and a messaging platform that `hermes send --list` can reach.

Pass `--enable` when updating as well. A running Hermes process keeps the plugin code it loaded at start, so without it the gateway and the `serve` backend carry on with the old version until they are reloaded or restarted.

*Awaiting approval

## Quick start

Set the delivery target and create the cron job:

```bash
hermes reminders setup --target telegram
```

`setup` writes a script into `$HERMES_HOME/scripts/` and creates one cron job that runs it every minute. The script imports the poller from the installed plugin, so updating the plugin changes what that job runs. `hermes reminders status` prints the state of the setup. `--no-cron` writes the script without creating the job, and prints the `hermes cron create` command to run yourself.

Add a reminder from any shell:

```bash
hermes reminders add "check the database" --in 2h
hermes reminders add "ring the dentist" --at 2026-11-01T15:00Z --target telegram
hermes reminders add "take vitamins" --at 2026-11-12T08:00:00 --every day
hermes reminders add "long run" --at 2026-11-14T09:00:00 --every weekend
hermes reminders list --json
hermes reminders cancel 3f9c1a2b
hermes reminders poll        # send everything due, once
```

`--in` takes a duration (`30m`, `2h`, `1.5h`, `1d`). `--at` takes ISO 8601; a timestamp with no offset is read as local time, and `Z` and explicit offsets are used as given.

This is the one path where you write the text yourself. The CLI writes what you give it straight to the store, with no model involved, so phrase it the way you want to read it when it arrives: `hermes reminders add "charge your phone"`, not `"charge my phone"`. When Hermes sets a reminder from something you told it, it phrases the text for you.

A delivered reminder is two lines:

```
🔔 Charge your phone
(Reminder set on 31 Oct 2026, 12:43)
```

## Agent tools

The plugin registers `reminders_add`, `reminders_list` and `reminders_cancel`. `reminders_add` takes `text` and `when`, plus optional `every` and `target`. It also registers the `/reminders` slash command (`/reminders`, `/reminders all`, `/reminders cancel <id>`), which lists and cancels reminders from Telegram, the WebUI or the CLI without calling a model.

## Repeat rules

`every` takes one of these. This is the syntax the agent writes for you when you describe a repeat in your own words, and you can also pass a rule directly on the command line.

| Rule | Fires |
| --- | --- |
| `2h`, `7d`, `1.5h` | every interval, counted from the time you set it |
| `day` | every day, at the clock time you gave |
| `weekday` | Monday to Friday |
| `weekend` | Saturday and Sunday |
| `week` (`1w`) | the same weekday each week, at the same clock time |
| `month` | the same day of the month, or the last day in a shorter month |
| `mon,wed,fri` | those weekdays; `tue,thu` and a single `sat` work the same way |
| `2w` | every other week, on the weekday you set |
| `2w:mon,wed` | every other week, on Monday and Wednesday |
| `first:mon` | the first Monday of each month |
| `last:fri` | the last Friday of each month |

`3w` and up work the same way as `2w`. `second:tue`, `third:wed` and `fourth:thu` name the other weeks of the month, and `1` to `4` count as the words. A day list works with the month rules too: `last:mon,wed` is the last Monday and the last Wednesday of the month, whichever comes first.

The table gives the canonical spelling of each rule. These plainer spellings are accepted as well:

- `daily` for `day`, `weekly` for `week`, `monthly` for `month`
- `weekdays` and `weekends` for the two named sets
- `monday` for `mon`, and `every monday` for `mon`
- `every other week`, `biweekly` and `fortnightly` for `2w`
- `every second tuesday` for `2w:tue`
- `the first monday of the month` for `first:mon`

A rule the plugin cannot schedule is refused, rather than guessed at, and the error names what the grammar does accept. Two refusals are deliberate:

- `fifth:mon`. The grammar stops at `fourth`. A fifth Monday exists in some months and not others, so the schedule would fire irregularly. Use `last:mon` for the final Monday of every month.
- `every other day`. `2d` is close, but it is a raw interval of 48 hours and drifts by an hour when the clocks change. Reading the phrase as `day` would send twice as often as intended. Neither is what the words mean, so it is refused.

Each send moves `due_at` on to the next matching time, counted from the original `anchor_at`, so a schedule does not drift and a machine that was offline does not produce a burst of late messages.

The rule decides the first send as well as the rest. Asked for Saturday at 09:00 with `every` set to `weekday`, the first send is Monday at 09:00, not the Saturday. A rule that already matches the time you gave is left where it is.

The time of day is stored as a local time, so a daily 19:00 reminder stays at 19:00 when the clocks change or you move to another timezone.

## Configuration

| What | How |
|---|---|
| Default delivery target | `hermes reminders setup --target telegram` (per reminder: `--target`) |
| Delivery format | `hermes reminders setup --template "…"` (default is `🔔 {text}` / `(Reminder set on {created})`) |
| Store location | `$HERMES_HOME/reminders.json`; override with `REMINDERS_STORE` |
| Retry attempts per reminder | `hermes reminders setup --max-attempts 3`; the default is 10 |
| Poller output | `REMINDERS_VERBOSE=1` makes an empty pass print a summary instead of staying silent |

The template can use `{text}`, `{created}`, `{created_utc}`, `{due}`, `{due_utc}` and `{id}`, with `{created}` and `{due}` in the local time of the machine running the poller. A template with a mistake in it falls back to the default rather than losing the reminder.

## How it works

```
hermes reminders add …  /  reminders_add tool          one JSON file
                  │                                      $HERMES_HOME/reminders.json
                  └──────────────► store ◄──────────┐
                                                   │  read
         cron job  (* * * * *, no agent, --deliver local)
                  │                                │
                  └─► $HERMES_HOME/scripts/reminders-poller.py  (6-line script)
                                │ imports
                                └─► $HERMES_HOME/plugins/reminders/poller.py
                                              │ hermes send --to <target> …
                                              ▼
                                       platform, then remove on exit 0
```

Hermes runs cron scripts only from `$HERMES_HOME/scripts/`, so the plugin writes a small script there that imports the poller from the plugin directory. Updating the plugin therefore changes what the job runs without touching the script, and running `hermes reminders setup` again repairs a moved or renamed install.

## Files it writes and commands it runs

- reads and writes `$HERMES_HOME/reminders.json`, and a `.lock` file beside it;
- writes `$HERMES_HOME/scripts/reminders-poller.py`;
- creates one cron job (`reminders-poller`, every minute, no-agent mode), after reading `$HERMES_HOME/cron/jobs.json` first so that it does not create a second copy of it;
- marks that job so its completions are not announced (`toast_notifications` and `badge_notifications` off), writing through the scheduler's own store API;
- runs the `hermes` CLI as a subprocess: `hermes send` to deliver, and `hermes cron create` during `setup`;
- makes no network calls of its own, sends no telemetry, and holds no credentials of its own. Delivery credentials stay in Hermes' own configuration;
- touches nothing outside `$HERMES_HOME`.

## Contributing

Issues and pull requests are welcome. The tests need no installation and no live Hermes home:

```bash
python3 -m unittest discover -s tests -t tests   # the test suite
hermes plugins doctor . --ci                     # loads the plugin, checks what it registers
hermes plugins validate .                        # the catalogue admission check
```

Every test runs against a throwaway `HERMES_HOME` and a fake `hermes` on `PATH`, so no test touches a real store, a real cron schedule or a real chat.

The version is declared in `plugin.yaml` and `pyproject.toml`, with a test to keep the two the same. The tool names, their arguments, the CLI and the store file format are the public interface, so a breaking change to any of them is a major version. Changes are listed in [CHANGELOG.md](CHANGELOG.md), each release is a git tag and a GitHub release, and the design notes are in [docs/design.md](docs/design.md).

## Licence

MIT, see [LICENSE](LICENSE).

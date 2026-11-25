# Marketplace submission

`reminders.yaml` is the catalogue entry for the [Hermes plugin catalogue](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugin-catalog).
Nothing in the plugin reads it. It exists so the submission is reviewable in one place, and so
the next release has a starting point.

## What ships and where

Being listed is a pull request to `NousResearch/hermes-agent` that adds exactly one file,
`plugin-catalog/reminders.yaml`. There is no self-serve registry and no automated ingestion. A
maintainer reviews the pinned tree, and the catalogue CI clones the repository at the pinned
commit and runs `hermes plugins validate` against it.

Before the pull request, the repository has to be public, because the entry's `repo` URL is
cloned by that CI job.

## The pin

`sha` is loaded and checked at install time, so it has to be a full 40-character commit that
exists on the public repository. It is the release: installs check out that commit, not a branch
tip, and a self-updating plugin is refused admission.

Recompute it after the last commit of the release being submitted, never before:

```bash
git rev-parse main
```

The committed value here is the `0.3.1` release commit. Update it in the same pull request that
bumps `version`, so the label on the catalogue card matches the code.

## Before opening the pull request

```bash
python3 -m unittest discover -s tests -t tests   # the suite
hermes plugins validate . --install-deps          # the same check the catalogue CI runs
```

Both should pass, and the validator's warnings should be read rather than skipped, because a
reviewer reads them too. `--install-deps` is what CI runs; the plugin declares no dependencies,
so it changes nothing here.

## The pull request description

The catalogue asks for three things beyond the file itself, and admission rule 13 requires the risky behaviour to be disclosed. For this plugin that means:

- What it does. A reminder service: one tool call sets a reminder, one cron job delivers it, and no model is involved in delivery.
- Which Hermes surfaces it uses. Three tools in a `reminders` toolset (`reminders_add`, `reminders_list`, `reminders_cancel`), the `reminders` CLI subcommand, the `/reminders` slash command, and a bundled read-only skill, `reminders:using`.
- The disclosure. It reads and writes `$HERMES_HOME/reminders.json` and a lock file beside it. It writes a script into `$HERMES_HOME/scripts/`. It creates one cron job, after reading `$HERMES_HOME/cron/jobs.json` read-only so it never creates a duplicate, and marks that job's completion notifications off through the scheduler's own store API. It runs the `hermes` CLI as a subprocess. It makes no network calls of its own, sends no telemetry, and holds no credentials.

The README's "What this does on your machine" section is the same list in the reader's words,
and is the source of the disclosure paragraph.

## After it is listed

Each later release is another pull request bumping `sha` and `version`, reviewed the same way.
Installed copies see the new pin through `hermes plugins update reminders`. The pinned commit
must stay reachable, because a weekly check deletes an entry whose pin has been force-pushed
away.

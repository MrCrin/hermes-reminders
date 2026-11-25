"""The reminder store — one JSON file, atomic writes, advisory locking.

Everything that touches the pool (the agent's tools, the CLI, the cron poller) goes through
this module, so the on-disk format has exactly one definition.

Format::

    {"version": 1, "default_target": "telegram", "template": null, "max_attempts": 10,
     "reminders": [ {record}, … ]}

``default_target``, ``template`` and ``max_attempts`` are the pool's settings, and every one
of them is optional: an absent or unusable value means the built-in default. `hermes reminders
setup` writes them, and the store module owns the file, so nothing else should edit them by hand.

A record is ``id``, ``text``, ``due_at`` (UTC), ``every``, ``anchor_at``, ``anchor_local``,
``created_at``, ``created_by``, ``target``, ``status`` (``pending`` / ``failed``),
``attempts``, ``last_error``, ``next_attempt_at``. One-shot reminders are removed on
delivery; recurring reminders advance ``due_at`` from ``anchor_at`` to the next future slot.

A recurring record carries two anchors, and they answer different questions.
``anchor_at`` is the instant the schedule is counted from and is never rewritten after it
is set, so the schedule cannot drift. ``anchor_local`` is the wall clock that instant means
to the reader ("19:00", not "01:00Z"), and it is what the next slot is laid out on, so a
daily reminder stays at 19:00 across a daylight-saving change and across a move to another
timezone. A rule carrying a time of day cannot be computed from the instant alone: on the
day the clocks go forward, the local time and the instant are not a one-to-one mapping.

"""

from __future__ import annotations

import calendar
import json
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix fallback
    fcntl = None  # type: ignore[assignment]


STORE_ENV = "REMINDERS_STORE"
HOME_ENV = "HERMES_HOME"
DEFAULT_FILENAME = "reminders.json"
DEFAULT_MAX_ATTEMPTS = 10
SCHEMA_VERSION = 1
RETRY_BASE_SECONDS = 60
RETRY_CAP_SECONDS = 3600

_DURATION_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds|m|min|mins|minutes|h|hr|hrs|hours|d|day|days)\s*$",
    re.IGNORECASE,
)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_UNIT_ALIASES = {
    "sec": "s", "secs": "s", "seconds": "s",
    "min": "m", "mins": "m", "minutes": "m",
    "hr": "h", "hrs": "h", "hours": "h",
    "day": "d", "days": "d",
}
_CALENDAR_ALIASES = {
    "day": "day", "daily": "day",
    "weekday": "weekday", "weekdays": "weekday",
    "weekend": "weekend", "weekends": "weekend",
    "week": "week", "weekly": "week",
    "month": "month", "monthly": "month",
}
# The two named day sets. The walk and the two spellings of each set read them from here, so
# `weekday` cannot mean one thing to the parser and another to the schedule.
_WEEKDAY_DAYS = frozenset(range(5))
_WEEKEND_DAYS = frozenset((5, 6))
_WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WEEKDAY_ALIASES = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}
# The day rules the schedule never has to search for: they begin on their own anchor.
_UNCONSTRAINED_RULES = ("day", "week", "month")
# Months a walk over a month-based rule may look at before it gives up.
_MONTH_LOOKAHEAD_MONTHS = 24
# A rule that names a cycle of weeks: `2w` is a fortnight, `2w:mon,wed` a fortnight on two
# days of the week. A week here is a calendar week, not 604800 seconds, so the clock time is
# held across the change of the clocks. A raw interval is spelled in days, as in `14d`.
_WEEK_CYCLE_RE = re.compile(r"^(\d+)\s*(?:w|week|weeks|weekly)(?::\s*([a-z,\s]+))?$")
# Which week of the month: `first:mon` and `last:fri`. A fifth of a weekday exists in some
# months and not others, so that rule would fire irregularly; it is refused rather than guessed.
_ORDINAL_RE = re.compile(r"^(first|second|third|fourth|last|1st|2nd|3rd|4th|[1-4]):([a-z,\s]+)$")
_ORDINAL_ALIASES = {
    "first": 1, "1st": 1, "1": 1,
    "second": 2, "2nd": 2, "2": 2,
    "third": 3, "3rd": 3, "3": 3,
    "fourth": 4, "4th": 4, "4": 4,
    "last": -1,
}
_ORDINAL_WORDS = {1: "first", 2: "second", 3: "third", 4: "fourth", -1: "last"}
_UNSUPPORTED_ORDINALS = ("fifth", "5th")
# One message for every shape this build cannot read, so the caller learns the whole
# vocabulary rather than the one token that happened to fail.
_RECURRENCE_HELP = (
    "cannot read {value!r} as a recurrence (use a duration like 2h/1d, a calendar rule like "
    "day, weekday, weekend, week, month or mon,wed,fri, a cycle of weeks like 2w or "
    "2w:mon,wed, or a week of the month like first:mon or last:fri)"
)
_CONFIG_TZ_RE = re.compile(r"^timezone:\s*(\S+)")



class StoreError(Exception):
    """The store could not be read or written."""


def hermes_home() -> Path:
    """The active Hermes home (profile-aware) without importing Hermes."""
    return Path(os.environ.get(HOME_ENV) or Path.home() / ".hermes").expanduser()


def store_path() -> Path:
    """Where the pool lives: ``$REMINDERS_STORE``, else ``$HERMES_HOME/reminders.json``."""
    override = os.environ.get(STORE_ENV)
    return Path(override).expanduser() if override else hermes_home() / DEFAULT_FILENAME


def local_timezone() -> ZoneInfo | None:
    """The configured Hermes timezone from ``$HERMES_HOME/config.yaml``, else None (process local)."""
    config_path = hermes_home() / "config.yaml"
    try:
        with open(config_path, encoding="utf-8") as handle:
            for line in handle:
                match = _CONFIG_TZ_RE.match(line)
                if match:
                    name = match.group(1).strip().strip("'\"")
                    if name and name.lower() not in ("null", "none", "~", "local"):
                        return ZoneInfo(name)
                    break
    except (OSError, ZoneInfoNotFoundError, ValueError):
        pass
    return None


def _positive_int(value: object) -> int | None:
    """A whole number above zero, or None when the value is not one.

    A retry limit arrives from the pool file, which a person may have edited, so anything
    that is not a positive whole number is treated as absent rather than taken literally. A
    typo in this value must not cost a reminder, the same way a broken template does not.
    """
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_when(value: str, *, now: datetime | None = None) -> datetime:
    """An absolute ISO 8601 time, or a duration from now (``2h``, ``90m``, ``1d``).

    A naive ISO timestamp is read as local time — what someone typing "at 15:00" means.
    Anything carrying an offset (or a trailing ``Z``) is taken at face value.
    """
    now = now or utcnow()
    if not isinstance(value, str):
        # A stored value is not guaranteed to be the shape this build writes. Refuse it as a
        # value rather than raising an AttributeError out of a string method: the poller reads
        # every record in the pool, so one bad one must not take the delivery pass down.
        raise ValueError(
            f"cannot read {value!r} as a time — give ISO 8601 (2026-11-01T15:00Z) or a duration (2h)"
        )
    text = value.strip()
    if not text:
        raise ValueError("no time given")
    match = _DURATION_RE.match(text)
    if match:
        amount = float(match.group(1))
        unit = _UNIT_ALIASES.get(match.group(2).lower(), match.group(2).lower())
        return now + timedelta(seconds=amount * _UNIT_SECONDS[unit])
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"cannot read {value!r} as a time — give ISO 8601 (2026-11-01T15:00Z) or a duration (2h)"
        ) from exc
    if moment.tzinfo is None:
        tz = local_timezone()
        moment = moment.replace(tzinfo=tz) if tz is not None else moment.astimezone()
    return moment.astimezone(timezone.utc)


def _fold_phrase(text: str) -> str:
    """Fold the common multi-word spellings of a rule into the compact form.

    Only phrasings that name exactly one rule are folded. "Every other day" carries a
    two-day cycle this build cannot express, so it is left alone and then refused, which is
    the right answer: reading it as `day` would send the reminder twice as often as asked.
    """
    # "every 2 weeks" and "every monday" are the same rules as "2 weeks" and "monday", and
    # the word adds nothing to any shape that reads correctly with it.
    text = re.sub(r"^every\s+", "", " ".join(text.split()))
    for phrase in ("other week", "alternate week", "biweekly", "fortnightly", "fortnight"):
        if text == phrase:
            return "2w"
    # "every other monday" / "every second tuesday" / "alternate friday" -> once a fortnight.
    match = re.match(r"^(?:other|alternate|second)\s+(\S+)$", text)
    if match:
        return f"2w:{match.group(1)}"
    # "the first monday of the month" / "last friday of each month" -> which week of the month.
    match = re.match(r"^(?:the\s+)?(\S+)\s+(\S+)\s+of\s+(?:the\s+|each\s+|every\s+)?month$", text)
    if match:
        return f"{match.group(1)}:{match.group(2)}"
    return text


def _day_indices(text: str | None) -> list[int]:
    """The weekdays a comma-separated list names, refusing anything that is not one."""
    parts = [part.strip() for part in (text or "").split(",") if part.strip()]
    if not parts:
        raise ValueError(f"no day of the week in {text!r}")
    indices = []
    for part in parts:
        if part not in _WEEKDAY_ALIASES:
            raise ValueError(f"not a day of the week: {part!r}")
        indices.append(_WEEKDAY_ALIASES[part])
    return indices


def _normalise_day_list(text: str) -> str:
    """Canonical day list: short names, sorted, de-duplicated."""
    return ",".join(_WEEKDAY_NAMES[index] for index in sorted(set(_day_indices(text))))


def _normalise_week_cycle(weeks: int, days: str | None) -> str:
    """Canonical week cycle: `2w` (the anchor's own weekday) or `2w:mon,wed`."""
    if weeks < 1:
        raise ValueError("a cycle of weeks is at least one week")
    if weeks == 1:
        return "week" if not days else _normalise_day_list(days)
    return f"{weeks}w" if not days else f"{weeks}w:{_normalise_day_list(days)}"


def _normalise_ordinal(ordinal: str, days: str) -> str:
    """Canonical month ordinal: `first:mon`, `last:fri`."""
    return f"{_ORDINAL_WORDS[_ORDINAL_ALIASES[ordinal]]}:{_normalise_day_list(days)}"


def parse_every(value: str) -> str:
    """Normalize a recurrence rule, and refuse anything this build cannot schedule.

    A rule is a positive duration (`2h`, `7d`), a calendar rule (`day`, `weekday`, `weekend`,
    `week`, `month`, `mon,wed,fri`), a cycle of weeks (`2w`, `2w:mon,wed`) or a week of the
    month (`first:mon`, `last:fri`). Anything else raises `ValueError` naming the vocabulary.
    """
    if not isinstance(value, str):
        # A tool call can carry any JSON type, and a stored rule can be any JSON value. Both
        # have to come back as a readable refusal, not as an AttributeError.
        raise ValueError(_RECURRENCE_HELP.format(value=value))
    text = _fold_phrase(value.strip().lower())
    if not text:
        raise ValueError("no recurrence given")
    match = _DURATION_RE.match(text)
    if match:
        amount = float(match.group(1))
        if amount <= 0:
            raise ValueError(f"recurrence interval must be positive: {value!r}")
        unit = _UNIT_ALIASES.get(match.group(2).lower(), match.group(2).lower())
        number = str(int(amount)) if amount.is_integer() else str(amount)
        return f"{number}{unit}"
    if text in _CALENDAR_ALIASES:
        return _CALENDAR_ALIASES[text]
    if text.startswith(_UNSUPPORTED_ORDINALS):
        raise ValueError(
            "a fifth of a weekday exists in some months and not others, so that rule would "
            "fire irregularly; use `last` for the final one of each month"
        )
    try:
        match = _WEEK_CYCLE_RE.match(text)
        if match:
            return _normalise_week_cycle(int(match.group(1)), match.group(2))
        match = _ORDINAL_RE.match(text)
        if match:
            return _normalise_ordinal(match.group(1), match.group(2))
        return _normalise_day_list(text)
    except ValueError:
        raise ValueError(_RECURRENCE_HELP.format(value=value)) from None


def _local_wall_to_utc(year: int, month: int, day: int, hour: int, minute: int, second: int) -> datetime:
    tz = local_timezone()
    local_dt = datetime(year, month, day, hour, minute, second, tzinfo=tz)
    return (local_dt if tz is not None else local_dt.astimezone()).astimezone(timezone.utc)


def _wall_clock(instant: datetime, literal: datetime | None = None) -> datetime:
    """The local wall clock a repeating reminder is anchored to.

    The time of day is the one that was asked for; the date is the one the schedule landed
    on. Reading the whole clock off the instant instead loses the time of day on the date the
    clocks change, where a local time and an instant are not a one-to-one mapping: a 02:30
    reminder read back off its own instant comes back as 03:30, and stays there for good.
    """
    local_wall = instant.astimezone(local_timezone()).replace(tzinfo=None)
    if literal is None:
        return local_wall
    return datetime.combine(local_wall.date(), literal.time())


def _literal_wall_clock(when: str | datetime) -> datetime | None:
    """The naive wall clock an input named, or None when it named an instant.

    A duration (`2h`) and a timestamp carrying an offset or a `Z` both name an instant, so
    neither has a wall clock of its own to preserve; the local one stands in for both.
    """
    if isinstance(when, datetime):
        return when.replace(tzinfo=None) if when.tzinfo is None else None
    if not isinstance(when, str):
        return None
    text = when.strip()
    if not text or _DURATION_RE.match(text):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo is None else None


def _anchor_wall(anchor_utc: datetime, anchor_local: str | datetime | None) -> datetime:
    """The wall clock a schedule is laid out on, falling back to the anchor instant."""
    if isinstance(anchor_local, datetime):
        return anchor_local.replace(tzinfo=None)
    if anchor_local:
        try:
            parsed = datetime.fromisoformat(str(anchor_local).strip())
            if parsed.tzinfo is None:
                return parsed
        except ValueError:
            pass
    return anchor_utc.astimezone(local_timezone())


def _day_set(text: str | None) -> frozenset[int]:
    """The weekdays a named set or a day list covers: `weekday`, `weekend`, or `mon,wed,fri`."""
    if text == "weekday":
        return _WEEKDAY_DAYS
    if text == "weekend":
        return _WEEKEND_DAYS
    return frozenset(_day_indices(text))


def _week_interval(rule: str) -> int:
    """How many weeks one cycle of a weekly rule spans: 2 for `2w`, 1 for a plain week."""
    match = _WEEK_CYCLE_RE.match(rule)
    return int(match.group(1)) if match else 1


def _weekly_days(rule: str, anchor_wall: datetime) -> frozenset[int]:
    """The weekdays a weekly rule accepts. A bare interval keeps the anchor's own weekday."""
    match = _WEEK_CYCLE_RE.match(rule)
    if match is None:
        return _day_set(rule)
    if match.group(2):
        return _day_set(match.group(2))
    return frozenset({anchor_wall.weekday()})


def _lookahead_days(rule: str) -> int:
    """How far a weekly walk has to look: one full cycle, plus the day it starts on.

    A slot that has already passed today is only reachable on the same weekday a week later,
    so a single-week rule needs eight dates and a two-week rule fifteen.
    """
    return _week_interval(rule) * 7 + 1


def _day_matches(rule: str, day: date, anchor_wall: datetime) -> bool:
    """Is `day` a date this weekly rule names?

    A cycle of more than one week keeps the weeks it skips: the cycle is counted in whole
    weeks from the anchor, so `2w:mon,wed` fires Monday and Wednesday of one week and then
    neither of them the next.
    """
    if rule == "day":
        return True
    if rule == "week":
        return (day - anchor_wall.date()).days % 7 == 0
    elapsed = (day - anchor_wall.date()).days
    if elapsed < 0 or day.weekday() not in _weekly_days(rule, anchor_wall):
        return False
    return (elapsed // 7) % _week_interval(rule) == 0


def _nth_weekday(year: int, month: int, ordinal: int, weekday: int) -> date | None:
    """The nth weekday of a month, or the last one, or None when the month holds no nth."""
    if ordinal == -1:
        last = date(year, month, calendar.monthrange(year, month)[1])
        return last - timedelta(days=(last.weekday() - weekday) % 7)
    first = date(year, month, 1)
    day = first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (ordinal - 1))
    return day if day.month == month else None


def _nth_weekday_instant(year: int, month: int, ordinal: int, weekday: int, wall: datetime) -> datetime | None:
    """The instant of the nth weekday of a month, or None when the month holds no nth."""
    day = _nth_weekday(year, month, ordinal, weekday)
    if day is None:
        return None
    return _local_wall_to_utc(day.year, day.month, day.day, wall.hour, wall.minute, wall.second)


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _walk_weekly(rule: str, anchor_wall: datetime, cutoff_utc: datetime, *, inclusive: bool) -> datetime | None:
    """The first slot of a week-based rule at, or strictly after, the cutoff."""
    cutoff_local = cutoff_utc.astimezone(local_timezone())
    hh, mm, ss = anchor_wall.hour, anchor_wall.minute, anchor_wall.second
    for offset in range(_lookahead_days(rule)):
        day_date = cutoff_local.date() + timedelta(days=offset)
        if not _day_matches(rule, day_date, anchor_wall):
            continue
        candidate = _local_wall_to_utc(day_date.year, day_date.month, day_date.day, hh, mm, ss)
        if candidate > cutoff_utc or (inclusive and candidate == cutoff_utc):
            return candidate
    return None


def _walk_month(anchor_wall: datetime, cutoff_utc: datetime) -> datetime | None:
    """The next slot on the anchor's own day of the month, capped at a shorter month."""
    cutoff_local = cutoff_utc.astimezone(local_timezone())
    hh, mm, ss = anchor_wall.hour, anchor_wall.minute, anchor_wall.second
    year, month = cutoff_local.year, cutoff_local.month
    for _ in range(_MONTH_LOOKAHEAD_MONTHS):
        last_day = calendar.monthrange(year, month)[1]
        candidate = _local_wall_to_utc(year, month, min(anchor_wall.day, last_day), hh, mm, ss)
        if candidate > cutoff_utc:
            return candidate
        year, month = _next_month(year, month)
    return None


def _walk_monthly(match: re.Match, anchor_wall: datetime, cutoff_utc: datetime, *, inclusive: bool) -> datetime | None:
    """The first `first:mon`-style slot at, or strictly after, the cutoff."""
    ordinal = _ORDINAL_ALIASES[match.group(1)]
    days = _day_set(match.group(2))
    cutoff_local = cutoff_utc.astimezone(local_timezone())
    year, month = cutoff_local.year, cutoff_local.month
    for _ in range(_MONTH_LOOKAHEAD_MONTHS):
        # A day list here means "the first Monday, and the first Wednesday": the answer is
        # whichever of them the month reaches first, not the first one written down.
        upcoming = [
            candidate
            for candidate in (
                _nth_weekday_instant(year, month, ordinal, weekday, anchor_wall)
                for weekday in days
            )
            if candidate is not None
            and (candidate > cutoff_utc or (inclusive and candidate == cutoff_utc))
        ]
        if upcoming:
            return min(upcoming)
        year, month = _next_month(year, month)
    return None


def _calendar_slot(rule: str, anchor_wall: datetime, cutoff_utc: datetime, *, inclusive: bool) -> datetime | None:
    """The first slot of a calendar rule at, or strictly after, the cutoff.

    `month` is never asked for inclusively: `first_occurrence` treats it as already standing
    on its anchor, so that walk only needs the stricter comparison.
    """
    if rule == "month":
        return _walk_month(anchor_wall, cutoff_utc)
    ordinal = _ORDINAL_RE.match(rule)
    if ordinal:
        return _walk_monthly(ordinal, anchor_wall, cutoff_utc, inclusive=inclusive)
    return _walk_weekly(rule, anchor_wall, cutoff_utc, inclusive=inclusive)


def next_due(
    anchor: datetime,
    every: str,
    *,
    after: datetime,
    anchor_local: str | datetime | None = None,
) -> datetime:
    """Next occurrence strictly after `after`, anchored to `anchor` so schedules never drift."""
    rule = parse_every(every)
    anchor_utc = anchor.astimezone(timezone.utc)
    cutoff_utc = max(after.astimezone(timezone.utc), anchor_utc)

    match = _DURATION_RE.match(rule)
    if match:
        amount = float(match.group(1))
        unit = _UNIT_ALIASES.get(match.group(2).lower(), match.group(2).lower())
        step = amount * _UNIT_SECONDS[unit]
        elapsed = max(0.0, (cutoff_utc - anchor_utc).total_seconds())
        steps = int(elapsed // step) + 1
        candidate = anchor_utc + timedelta(seconds=steps * step)
        while candidate <= cutoff_utc:
            steps += 1
            candidate = anchor_utc + timedelta(seconds=steps * step)
        return candidate.astimezone(timezone.utc)

    slot = _calendar_slot(
        rule, _anchor_wall(anchor_utc, anchor_local), cutoff_utc, inclusive=False
    )
    if slot is None:
        raise ValueError(f"could not advance {rule!r} reminder from {anchor_utc.isoformat()}")
    return slot


def first_occurrence(
    anchor: datetime,
    every: str,
    *,
    anchor_local: str | datetime | None = None,
) -> datetime:
    """The first occurrence of a pattern at or after `anchor`.

    Only the patterns that name days can move the anchor: a duration, `day`, `week` and
    `month` rule already begins on the instant it was asked for. Asked for a Saturday with
    `weekday`, the first delivery is the Monday, and `first:mon` asked for after the month's
    first Monday starts in the next month. Asked for a day the rule already names, it stays
    exactly where it was.
    """
    rule = parse_every(every)
    anchor_utc = anchor.astimezone(timezone.utc)
    if rule in _UNCONSTRAINED_RULES or _DURATION_RE.match(rule):
        return anchor_utc
    slot = _calendar_slot(
        rule, _anchor_wall(anchor_utc, anchor_local), anchor_utc, inclusive=True
    )
    if slot is None:
        raise ValueError(f"could not find a first {rule!r} occurrence from {anchor_utc.isoformat()}")
    return slot



class ReminderStore:
    """The pool. Every mutating call takes the lock and rewrites the file atomically."""

    def __init__(self, path: str | os.PathLike | None = None, max_attempts: int | None = None) -> None:
        self.path = Path(path) if path else store_path()
        # An explicit limit wins and is used as given; otherwise the pool file's setting is
        # read on demand, so a process that runs for a while still sees an edit.
        self._max_attempts = int(max_attempts) if max_attempts is not None else None

    # -- settings --------------------------------------------------------
    def _retry_limit(self, data: dict) -> int:
        """The retry limit for one read of the pool. Never takes the lock: the caller has it."""
        if self._max_attempts is not None:
            return self._max_attempts
        return _positive_int(data.get("max_attempts")) or DEFAULT_MAX_ATTEMPTS

    def max_attempts(self) -> int:
        """The retry limit for this pool: an explicit one, the pool file's, else the default."""
        with self._lock():
            return self._retry_limit(self._read())

    def set_max_attempts(self, attempts: int) -> None:
        """Set the retry limit for the pool, beside the target and the template."""
        limit = _positive_int(attempts)
        if limit is None:
            raise StoreError(f"a retry limit has to be a whole number above zero, not {attempts!r}")
        with self._lock():
            data = self._read()
            data["max_attempts"] = limit
            self._write(data)

    # -- plumbing --------------------------------------------------------
    @contextmanager
    def _lock(self):
        """Advisory lock on a sidecar file: the store itself is replaced on every write."""
        lock_path = self.path.parent / (self.path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": SCHEMA_VERSION, "default_target": None, "template": None, "reminders": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError) as exc:
            raise StoreError(f"the reminder store at {self.path} is not readable JSON") from exc
        if not isinstance(data, dict):
            raise StoreError(f"the reminder store at {self.path} is not a JSON object")
        data.setdefault("version", SCHEMA_VERSION)
        data.setdefault("default_target", None)
        data.setdefault("template", None)
        data.setdefault("max_attempts", None)
        if not isinstance(data.get("reminders"), list):
            data["reminders"] = []
        return data

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix=".reminders-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    # -- operations ------------------------------------------------------
    def add(
        self,
        text: str,
        when: str | datetime,
        *,
        created_by: str = "unknown",
        target: str | None = None,
        every: str | None = None,
    ) -> dict:
        """Put one reminder in the pool. Returns the stored record."""
        text = (text or "").strip()
        if not text:
            raise StoreError("a reminder needs some text")
        due = parse_when(when) if isinstance(when, str) else when
        if not isinstance(due, datetime):
            raise StoreError(f"cannot read {when!r} as a time")
        # The store keeps whole seconds: `to_iso` writes that much and no more, so rounding
        # here keeps the record and the arithmetic on the same value. "In five minutes" lands
        # mid-second, and a first slot a fraction behind the asked time would otherwise be
        # pushed to the next matching day.
        due = due.replace(microsecond=0)
        recurrence = parse_every(every) if (every and str(every).strip()) else None
        anchor_local_iso = None
        if recurrence:
            # A day-of-week pattern decides which day the first delivery lands on: asked for a
            # Saturday with `weekday`, it starts on the Monday. The clock time is the one that
            # was asked for, and the anchor keeps the date the schedule landed on.
            wall = _wall_clock(due, _literal_wall_clock(when))
            due = first_occurrence(due, recurrence, anchor_local=wall)
            anchor_local_iso = _wall_clock(due, wall).isoformat(timespec="seconds")
        due_iso = to_iso(due)
        record = {
            "id": uuid.uuid4().hex[:8],
            "text": text,
            "due_at": due_iso,
            "every": recurrence,
            "anchor_at": (due_iso if recurrence else None),
            "anchor_local": anchor_local_iso,
            "created_at": to_iso(utcnow()),
            "created_by": created_by,
            "target": (target or None),
            "status": "pending",
            "attempts": 0,
            "last_error": None,
            "next_attempt_at": None,
        }
        with self._lock():
            data = self._read()
            data["reminders"].append(record)
            self._write(data)
        return record


    def all(self) -> list[dict]:
        with self._lock():
            return list(self._read()["reminders"])

    def pending(self) -> list[dict]:
        return [record for record in self.all() if record.get("status") == "pending"]

    def get(self, ref: str) -> dict | None:
        """Find one record by full id, or by an unambiguous prefix of it."""
        ref = (ref or "").strip()
        if not ref:
            return None
        records = self.all()
        for record in records:
            if record.get("id") == ref:
                return record
        matches = [record for record in records if str(record.get("id", "")).startswith(ref)]
        return matches[0] if len(matches) == 1 else None

    def cancel(self, ref: str) -> dict | None:
        """Remove a reminder, whatever its state. Returns the removed record, or None."""
        ref = (ref or "").strip()
        if not ref:
            return None
        with self._lock():
            data = self._read()
            records = data["reminders"]
            removed = None
            for index, record in enumerate(records):
                if record.get("id") == ref:
                    removed = records.pop(index)
                    break
            if removed is None:
                matches = [record for record in records if str(record.get("id", "")).startswith(ref)]
                if len(matches) == 1:
                    removed = matches[0]
                    records.remove(removed)
            if removed is not None:
                self._write(data)
            return removed

    def due(self, now: datetime | None = None) -> list[dict]:
        """Pending reminders whose time has come and whose retry backoff has elapsed."""
        now = now or utcnow()
        due = []
        for record in self.all():
            if record.get("status") != "pending":
                continue
            try:
                if parse_when(record["due_at"]) > now:
                    continue
            except (KeyError, ValueError):
                continue
            next_attempt = record.get("next_attempt_at")
            if next_attempt:
                try:
                    if parse_when(next_attempt) > now:
                        continue
                except ValueError:
                    pass
            due.append(record)
        return due

    def mark_delivered(self, ref: str, *, now: datetime | None = None) -> dict | None:
        """Delivery confirmed: prune a one-shot reminder, or advance a recurring one.

        A repeating reminder whose rule cannot be read or advanced is retired as ``failed``
        with the reason recorded. The send has already happened by this point, so leaving it
        pending would deliver the same message again on the next tick, and the rule cannot
        start working on its own.
        """
        ref = (ref or "").strip()
        if not ref:
            return None
        current = now or utcnow()
        with self._lock():
            data = self._read()
            records = data["reminders"]
            index = None
            for idx, record in enumerate(records):
                if record.get("id") == ref:
                    index = idx
                    break
            if index is None:
                matches = [idx for idx, record in enumerate(records) if str(record.get("id", "")).startswith(ref)]
                if len(matches) == 1:
                    index = matches[0]
            if index is None:
                return None
            record = records[index]
            every = record.get("every")
            if not every:
                removed = records.pop(index)
                self._write(data)
                return removed
            try:
                current_due = parse_when(record["due_at"])
                anchor = parse_when(record.get("anchor_at") or record["due_at"])
                cutoff = max(current, current_due)
                record["due_at"] = to_iso(
                    next_due(
                        anchor,
                        str(every),
                        after=cutoff,
                        anchor_local=record.get("anchor_local"),
                    )
                )
            except (ValueError, KeyError, TypeError) as exc:
                # The message is already on the wire by the time the rule is advanced, so a
                # rule this build cannot read has to retire the reminder. Leaving it pending
                # would put the same sentence on the user's screen every minute, and the rule
                # cannot start working on a later tick.
                record["status"] = "failed"
                record["last_error"] = f"cannot advance a repeating reminder: {exc}"
                record["next_attempt_at"] = None
                self._write(data)
                return dict(record)
            # Both anchors are immutable and written back unchanged: `next_due` counted every
            # slot from `anchor_at`, and `anchor_local` is the wall clock the schedule was set
            # up on. Rewriting either from the advanced slot is how a schedule starts to drift.
            record["anchor_at"] = to_iso(anchor)
            record["status"] = "pending"
            record["attempts"] = 0
            record["last_error"] = None
            record["next_attempt_at"] = None
            self._write(data)
            return dict(record)


    def mark_failed_attempt(self, ref: str, error: str) -> dict | None:
        """Record a failed delivery and back the retry off. Never drops the reminder."""
        now = utcnow()
        with self._lock():
            data = self._read()
            found = None
            for record in data["reminders"]:
                if record.get("id") == ref:
                    found = record
                    break
            if found is None:
                return None
            found["attempts"] = int(found.get("attempts") or 0) + 1
            found["last_error"] = (error or "delivery failed")[:500]
            if found["attempts"] >= self._retry_limit(data):
                found["status"] = "failed"
                found["next_attempt_at"] = None
            else:
                delay = min(RETRY_BASE_SECONDS * (2 ** (found["attempts"] - 1)), RETRY_CAP_SECONDS)
                found["next_attempt_at"] = to_iso(now + timedelta(seconds=delay))
            self._write(data)
            return dict(found)

    def default_target(self) -> str | None:
        with self._lock():
            return self._read().get("default_target")

    def set_default_target(self, target: str | None) -> None:
        with self._lock():
            data = self._read()
            data["default_target"] = (target or None)
            self._write(data)

    def template(self) -> str | None:
        """The delivery template, or None for the built-in default."""
        with self._lock():
            return self._read().get("template")

    def set_template(self, template: str | None) -> None:
        with self._lock():
            data = self._read()
            data["template"] = (template or None)
            self._write(data)

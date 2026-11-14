"""The reminder store — one JSON file, atomic writes, advisory locking.

Everything that touches the pool (the agent's tools, the CLI, the cron poller) goes through
this module, so the on-disk format has exactly one definition.

Format::

    {"version": 1, "default_target": "telegram", "reminders": [ {record}, … ]}

A record is ``id``, ``text``, ``due_at`` (UTC), ``every``, ``anchor_at``, ``created_at``,
``created_by``, ``target``, ``status`` (``pending`` / ``failed``), ``attempts``,
``last_error``, ``next_attempt_at``. One-shot reminders are removed on delivery; recurring
reminders advance ``due_at`` from ``anchor_at`` to the next future slot.

"""

from __future__ import annotations

import calendar
import fcntl
import json
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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
    "week": "week", "weekly": "week",
    "month": "month", "monthly": "month",
}
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
    text = (value or "").strip()
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


def parse_every(value: str) -> str:
    """Normalize a recurrence rule: a positive duration (`2h`, `7d`) or calendar rule
    (`day`, `weekday`, `week`, `month`, `mon,wed,fri`)."""
    text = (value or "").strip().lower()
    if not text:
        raise ValueError("no recurrence given")
    if text in _CALENDAR_ALIASES:
        return _CALENDAR_ALIASES[text]
    match = _DURATION_RE.match(text)
    if match:
        amount = float(match.group(1))
        if amount <= 0:
            raise ValueError(f"recurrence interval must be positive: {value!r}")
        unit = _UNIT_ALIASES.get(match.group(2).lower(), match.group(2).lower())
        number = str(int(amount)) if amount.is_integer() else str(amount)
        return f"{number}{unit}"
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if parts and all(part in _WEEKDAY_ALIASES for part in parts):
        indices = sorted({_WEEKDAY_ALIASES[part] for part in parts})
        return ",".join(_WEEKDAY_NAMES[idx] for idx in indices)
    raise ValueError(
        f"cannot read {value!r} as a recurrence (use a duration like 2h/1d, or day, weekday, week, month, mon,wed,fri)"
    )


def _local_wall_to_utc(year: int, month: int, day: int, hour: int, minute: int, second: int) -> datetime:
    tz = local_timezone()
    local_dt = datetime(year, month, day, hour, minute, second, tzinfo=tz)
    return (local_dt if tz is not None else local_dt.astimezone()).astimezone(timezone.utc)


def next_due(anchor: datetime, every: str, *, after: datetime) -> datetime:
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

    tz = local_timezone()
    anchor_local = anchor_utc.astimezone(tz)
    cutoff_local = cutoff_utc.astimezone(tz)
    hh, mm, ss = anchor_local.hour, anchor_local.minute, anchor_local.second

    if rule == "month":
        year, month = cutoff_local.year, cutoff_local.month
        for _ in range(24):
            last_day = calendar.monthrange(year, month)[1]
            cand = _local_wall_to_utc(year, month, min(anchor_local.day, last_day), hh, mm, ss)
            if cand > cutoff_utc:
                return cand
            if month == 12:
                year, month = year + 1, 1
            else:
                month += 1
        raise ValueError(f"could not advance monthly reminder from {anchor_utc.isoformat()}")

    allowed_days = (
        {_WEEKDAY_ALIASES[part] for part in rule.split(",")}
        if rule not in ("day", "weekday", "week")
        else None
    )
    start_date = cutoff_local.date()
    for offset in range(15):
        day_date = start_date + timedelta(days=offset)
        if rule == "weekday" and day_date.weekday() >= 5:
            continue
        if rule == "week" and (day_date - anchor_local.date()).days % 7 != 0:
            continue
        if allowed_days is not None and day_date.weekday() not in allowed_days:
            continue
        cand = _local_wall_to_utc(day_date.year, day_date.month, day_date.day, hh, mm, ss)
        if cand > cutoff_utc:
            return cand
    raise ValueError(f"could not advance {rule!r} reminder from {anchor_utc.isoformat()}")



class ReminderStore:
    """The pool. Every mutating call takes the lock and rewrites the file atomically."""

    def __init__(self, path: str | os.PathLike | None = None, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> None:
        self.path = Path(path) if path else store_path()
        self.max_attempts = int(max_attempts)

    # -- plumbing --------------------------------------------------------
    @contextmanager
    def _lock(self):
        """Advisory lock on a sidecar file: the store itself is replaced on every write."""
        lock_path = self.path.parent / (self.path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
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
        recurrence = parse_every(every) if (every and str(every).strip()) else None
        due_iso = to_iso(due)
        record = {
            "id": uuid.uuid4().hex[:8],
            "text": text,
            "due_at": due_iso,
            "every": recurrence,
            "anchor_at": (due_iso if recurrence else None),
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
        """Delivery confirmed: prune a one-shot reminder, or advance a recurring one."""
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
            current_due = parse_when(record["due_at"])
            anchor = parse_when(record.get("anchor_at") or record["due_at"])
            cutoff = max(current, current_due)
            record["due_at"] = to_iso(next_due(anchor, str(every), after=cutoff))
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
            if found["attempts"] >= self.max_attempts:
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

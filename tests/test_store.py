"""The store: time parsing, the pool, and — the rule that matters — pruning only on
confirmed delivery."""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import sandbox, store  # noqa: E402


class ParseWhenTests(unittest.TestCase):
    def test_durations(self):
        now = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
        cases = {"30s": 30, "10m": 600, "2h": 7200, "1d": 86400, "90 mins": 5400, "1.5h": 5400}
        for text, seconds in cases.items():
            with self.subTest(text=text):
                self.assertEqual(store.parse_when(text, now=now), now + timedelta(seconds=seconds))

    def test_absolute_utc(self):
        self.assertEqual(
            store.parse_when("2026-11-01T15:00:00Z"),
            datetime(2026, 11, 1, 15, 0, tzinfo=timezone.utc),
        )

    def test_offset_is_respected(self):
        self.assertEqual(
            store.parse_when("2026-11-01T15:00:00+02:00"),
            datetime(2026, 11, 1, 13, 0, tzinfo=timezone.utc),
        )

    def test_naive_is_local(self):
        naive = store.parse_when("2026-11-01T15:00:00")
        self.assertEqual(naive.astimezone().hour, 15)
        self.assertEqual(naive.tzinfo, timezone.utc)

    def test_garbage_is_a_clear_error(self):
        with self.assertRaises(ValueError):
            store.parse_when("sometime tomorrow")
        with self.assertRaises(ValueError):
            store.parse_when("")


class PoolTests(unittest.TestCase):
    def setUp(self):
        self._sandbox = sandbox()
        self.env = self._sandbox.__enter__()
        self.addCleanup(lambda: self._sandbox.__exit__(None, None, None))
        self.store = store.ReminderStore()

    def test_add_creates_a_pending_record(self):
        record = self.store.add("check the database", "2h", created_by="test")
        self.assertEqual(record["status"], "pending")
        self.assertEqual(record["attempts"], 0)
        self.assertEqual(len(record["id"]), 8)
        self.assertEqual(len(self.store.all()), 1)

    def test_add_needs_text_and_a_time(self):
        with self.assertRaises(store.StoreError):
            self.store.add("   ", "2h")
        with self.assertRaises(ValueError):
            self.store.add("something", "not a time")

    def test_store_file_is_json_in_the_hermes_home(self):
        self.store.add("x", "1h")
        self.assertEqual(self.store.path, self.env["home"] / "reminders.json")
        data = json.loads(self.store.path.read_text())
        self.assertEqual(data["version"], 1)
        self.assertEqual(len(data["reminders"]), 1)

    def test_due_only_returns_things_whose_time_has_come(self):
        soon = self.store.add("later", "2h")
        now = self.store.add("now", "1s")
        due = self.store.due()
        self.assertEqual([record["id"] for record in due], [])
        due = self.store.due(store.utcnow() + timedelta(hours=3))
        self.assertEqual(sorted(record["id"] for record in due), sorted([soon["id"], now["id"]]))

    def test_cancel_removes_by_id_and_by_prefix(self):
        record = self.store.add("gone", "1h")
        self.assertIsNotNone(self.store.cancel(record["id"][:4]))
        self.assertEqual(self.store.all(), [])

    def test_cancel_of_an_unknown_id_is_none(self):
        self.assertIsNone(self.store.cancel("nope"))

    def test_delivered_reminders_are_pruned(self):
        record = self.store.add("one shot", "1h")
        pruned = self.store.mark_delivered(record["id"])
        self.assertEqual(pruned["id"], record["id"])
        self.assertEqual(self.store.all(), [])

    def test_failed_attempt_is_kept_and_backed_off(self):
        record = self.store.add("retry me", "0s")  # due immediately
        self.store.mark_failed_attempt(record["id"], "connection refused")
        kept = self.store.get(record["id"])
        self.assertEqual(kept["attempts"], 1)
        self.assertEqual(kept["last_error"], "connection refused")
        self.assertIsNotNone(kept["next_attempt_at"])
        # It is due, but the backoff keeps it out of this pass...
        self.assertEqual(self.store.due(store.utcnow() + timedelta(seconds=5)), [])
        # ...and lets it through once the backoff has elapsed.
        self.assertEqual(len(self.store.due(store.utcnow() + timedelta(seconds=120))), 1)

    def test_attempts_stop_at_the_limit_and_the_record_survives(self):
        limited = store.ReminderStore(max_attempts=2)
        record = limited.add("never lands", "1h")
        limited.mark_failed_attempt(record["id"], "boom")
        limited.mark_failed_attempt(record["id"], "boom")
        final = limited.get(record["id"])
        self.assertEqual(final["status"], "failed")
        self.assertIsNone(final["next_attempt_at"])
        self.assertEqual(limited.due(store.utcnow() + timedelta(days=1)), [])
        self.assertEqual(len(limited.all()), 1)  # never silently discarded

    def test_default_target_round_trip(self):
        self.assertIsNone(self.store.default_target())
        self.store.set_default_target("telegram")
        self.assertEqual(self.store.default_target(), "telegram")

    def test_an_unreadable_store_says_so(self):
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text("{not json")
        with self.assertRaises(store.StoreError):
            self.store.all()


if __name__ == "__main__":
    unittest.main()

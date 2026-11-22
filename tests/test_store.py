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
    def setUp(self):
        self._sandbox = sandbox(with_hermes=False)
        self.env = self._sandbox.__enter__()
        self.addCleanup(lambda: self._sandbox.__exit__(None, None, None))

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


class ParseEveryTests(unittest.TestCase):
    def test_durations_normalize_to_short_units(self):
        cases = {
            "30s": "30s",
            "10m": "10m",
            "90 mins": "90m",
            "2 hours": "2h",
            "1.5h": "1.5h",
            "7 days": "7d",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(store.parse_every(raw), expected)

    def test_calendar_keywords_and_weekday_lists(self):
        cases = {
            "day": "day",
            "daily": "day",
            "weekday": "weekday",
            "weekdays": "weekday",
            "weekend": "weekend",
            "weekends": "weekend",
            "Weekend": "weekend",
            "week": "week",
            "weekly": "week",
            "month": "month",
            "monthly": "month",
            "Monday": "mon",
            "fri, mon, wed, mon": "mon,wed,fri",
            "Tues,Thurs": "tue,thu",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(store.parse_every(raw), expected)

    def test_invalid_recurrence_is_rejected(self):
        for bad in ("", "   ", "0s", "0h", "0w", "yearly", "mon,funday", "mon and wed",
                    "every other day", "every other 2nd monday", "2w:day", "first:day",
                    "first monday", "fifth:monday", "quarterly", "2 months"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    store.parse_every(bad)

    def test_a_recurrence_rule_that_is_not_a_string_is_rejected(self):
        # A tool call can carry any JSON type. It must come back as a readable refusal
        # rather than an AttributeError from deep inside the parser.
        for bad in (["mon"], {"mon": 1}, 7, ["mon", "wed"]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    store.parse_every(bad)


class CalendarWalkTests(unittest.TestCase):
    """Which dates a pattern accepts, and where its walk starts."""

    def setUp(self):
        self._sandbox = sandbox(with_hermes=False)
        self.env = self._sandbox.__enter__()
        self.addCleanup(lambda: self._sandbox.__exit__(None, None, None))

    def test_a_weekend_rule_advances_over_the_week_to_saturday(self):
        # 2026-11-04 is a Wednesday.
        wednesday = datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(
            store.next_due(wednesday, "weekend", after=wednesday),
            datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc),
        )

    def test_a_weekend_rule_rolls_from_sunday_to_saturday(self):
        # 2026-11-08 is a Sunday; the next weekend day is Saturday 2026-11-14.
        sunday = datetime(2026, 11, 8, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(
            store.next_due(sunday, "weekend", after=sunday),
            datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc),
        )

    def test_every_rule_finds_its_next_slot_from_any_day_of_the_week(self):
        # The walk's window is the thing that can be too small. A single-weekday rule whose
        # slot has already passed today is only reachable a week later, so the window has to
        # span a full cycle plus the day it starts on.
        rules = ("day", "weekday", "weekend", "week", "mon", "sun", "mon,wed,fri")
        for day in range(7):
            anchor = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc) + timedelta(days=day)
            after = anchor + timedelta(hours=2)  # today's 09:00 slot has gone
            for rule in rules:
                with self.subTest(day=day, rule=rule):
                    got = store.next_due(anchor, rule, after=after)
                    self.assertGreater(got, after)
                    self.assertLessEqual(got - after, timedelta(days=7))

    def test_the_first_occurrence_is_the_next_matching_day(self):
        # Saturday 2026-11-07: a weekday pattern cannot start on the day it was asked for.
        saturday = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(
            store.first_occurrence(saturday, "weekday"),
            datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            store.first_occurrence(saturday, "mon,wed,fri"),
            datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc),
        )

    def test_the_first_occurrence_keeps_a_rule_that_already_matches(self):
        # Monday 2026-11-02 09:00Z. None of these may be pushed to the next slot.
        monday = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
        for rule in ("weekday", "mon", "mon,wed,fri", "day", "week", "month", "2h", "7d"):
            with self.subTest(rule=rule):
                self.assertEqual(store.first_occurrence(monday, rule), monday)



class WeekIntervalTests(unittest.TestCase):
    """`2w` and `2w:mon,wed`: a cycle longer than one week."""

    def setUp(self):
        self._sandbox = sandbox(with_hermes=False)
        self.env = self._sandbox.__enter__()
        self.addCleanup(lambda: self._sandbox.__exit__(None, None, None))

    def test_an_interval_is_written_and_read_in_weeks(self):
        cases = {
            "2w": "2w",
            "3w": "3w",
            "2 week": "2w",
            "2 weeks": "2w",
            "every 2 weeks": "2w",
            "2 weekly": "2w",
            "every other week": "2w",
            "biweekly": "2w",
            "fortnightly": "2w",
            "fortnight": "2w",
            "1w": "week",
            "1w:mon": "mon",
            "2w:mon": "2w:mon",
            "2w: monday": "2w:mon",
            "2w:wed,mon": "2w:mon,wed",
            "2 weeks: mon, wed": "2w:mon,wed",
            "every other monday": "2w:mon",
            "every second tuesday": "2w:tue",
            "alternate friday": "2w:fri",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(store.parse_every(raw), expected)

    def test_a_two_week_cycle_is_a_fortnight_apart(self):
        monday = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
        self.assertEqual(
            store.next_due(monday, "2w", after=monday),
            datetime(2026, 11, 16, 8, 0, tzinfo=timezone.utc),
        )

    def test_a_two_week_cycle_on_a_named_day_skips_the_week_between(self):
        monday = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
        self.assertEqual(
            store.next_due(monday, "2w:mon", after=monday),
            datetime(2026, 11, 16, 8, 0, tzinfo=timezone.utc),
        )

    def test_two_days_in_one_week_share_the_cycle(self):
        # Anchored Monday: `mon,wed` fires Monday, then Wednesday, then a fortnight on.
        monday = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
        wednesday = store.next_due(monday, "2w:mon,wed", after=monday)
        self.assertEqual(wednesday, datetime(2026, 11, 4, 8, 0, tzinfo=timezone.utc))
        # Not Monday the 9th or Wednesday the 11th: that week is skipped entirely.
        self.assertEqual(
            store.next_due(monday, "2w:mon,wed", after=wednesday),
            datetime(2026, 11, 16, 8, 0, tzinfo=timezone.utc),
        )

    def test_a_three_week_cycle_skips_two_weeks(self):
        monday = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
        self.assertEqual(
            store.next_due(monday, "3w", after=monday),
            datetime(2026, 11, 23, 8, 0, tzinfo=timezone.utc),
        )

    def test_the_first_occurrence_of_a_two_week_cycle_is_the_first_day_it_names(self):
        # Wednesday 2026-11-04: `2w:mon` cannot start there, so it starts Monday the 9th.
        wednesday = datetime(2026, 11, 4, 8, 0, tzinfo=timezone.utc)
        self.assertEqual(
            store.first_occurrence(wednesday, "2w:mon"),
            datetime(2026, 11, 9, 8, 0, tzinfo=timezone.utc),
        )

    def test_the_first_occurrence_keeps_a_two_week_cycle_that_already_matches(self):
        monday = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
        self.assertEqual(store.first_occurrence(monday, "2w:mon"), monday)
        self.assertEqual(store.first_occurrence(monday, "2w"), monday)


class MonthOrdinalTests(unittest.TestCase):
    """`first:mon` and `last:fri`: which week of the month."""

    def setUp(self):
        self._sandbox = sandbox(with_hermes=False)
        self.env = self._sandbox.__enter__()
        self.addCleanup(lambda: self._sandbox.__exit__(None, None, None))

    def test_an_ordinal_is_written_and_read(self):
        cases = {
            "first:mon": "first:mon",
            "1:mon": "first:mon",
            "1st:mon": "first:mon",
            "1st:monday": "first:mon",
            "first monday of the month": "first:mon",
            "the first monday of each month": "first:mon",
            "1st monday of the month": "first:mon",
            "second:tue": "second:tue",
            "2nd tuesday of the month": "second:tue",
            "third:wed": "third:wed",
            "fourth:thu": "fourth:thu",
            "last:fri": "last:fri",
            "last friday of the month": "last:fri",
            "the last friday of every month": "last:fri",
            "last:mon,wed": "last:mon,wed",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(store.parse_every(raw), expected)

    def test_a_fifth_weekday_is_refused_and_points_at_last(self):
        with self.assertRaises(ValueError) as caught:
            store.parse_every("fifth monday of the month")
        self.assertIn("last", str(caught.exception))

    def test_the_first_monday_of_a_month(self):
        start = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
        first = store.first_occurrence(start, "first:mon")
        self.assertEqual(first, datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc))
        self.assertEqual(
            store.next_due(first, "first:mon", after=first),
            datetime(2026, 12, 7, 9, 0, tzinfo=timezone.utc),
        )

    def test_the_first_occurrence_rolls_into_next_month_once_this_one_has_passed(self):
        # 2026-11-20 is after November's first Monday, so the first delivery is December's.
        start = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(
            store.first_occurrence(start, "first:mon"),
            datetime(2026, 12, 7, 9, 0, tzinfo=timezone.utc),
        )

    def test_the_last_friday_of_a_month(self):
        start = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
        first = store.first_occurrence(start, "last:fri")
        self.assertEqual(first, datetime(2026, 11, 27, 9, 0, tzinfo=timezone.utc))
        self.assertEqual(
            store.next_due(first, "last:fri", after=first),
            datetime(2026, 12, 25, 9, 0, tzinfo=timezone.utc),
        )

    def test_the_last_friday_of_a_short_month(self):
        # February 2026 ends on Saturday the 28th, so its last Friday is the 27th.
        start = datetime(2026, 2, 1, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(
            store.first_occurrence(start, "last:fri"),
            datetime(2026, 2, 27, 9, 0, tzinfo=timezone.utc),
        )

    def test_an_ordinal_crosses_the_year_boundary(self):
        start = datetime(2026, 12, 20, 9, 0, tzinfo=timezone.utc)
        first = store.first_occurrence(start, "first:mon")
        self.assertEqual(first, datetime(2027, 1, 4, 9, 0, tzinfo=timezone.utc))

    def test_an_ordinal_day_list_takes_the_earliest_day_of_that_month(self):
        # November 2026 starts on a Sunday, so its first Sunday is the 1st and its first
        # Tuesday is the 3rd. The answer is the 1st, not the first day named in the list.
        start = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(
            store.first_occurrence(start, "first:tue,sun"),
            datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc),
        )

    def test_an_ordinal_keeps_the_clock_time_it_was_asked_for(self):
        start = datetime(2026, 11, 1, 17, 45, tzinfo=timezone.utc)
        self.assertEqual(store.first_occurrence(start, "first:mon").hour, 17)
        self.assertEqual(store.first_occurrence(start, "first:mon").minute, 45)

    def test_an_ordinal_keeps_its_wall_clock_across_a_daylight_saving_change(self):
        import os
        import time

        cfg = self.env["home"] / "config.yaml"
        cfg.write_text("timezone: America/New_York\n", encoding="utf-8")
        prev_tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        try:
            # First Monday of March 2026 is the 2nd, before the 8th, so 09:00 EST is 14:00Z.
            # April's first Monday is the 6th, after the change, so 09:00 EDT is 13:00Z.
            start = datetime(2026, 3, 1, 9, 0)
            first = store.first_occurrence(start, "first:mon", anchor_local=start)
            self.assertEqual(first, datetime(2026, 3, 2, 14, 0, tzinfo=timezone.utc))
            self.assertEqual(
                store.next_due(first, "first:mon", after=first, anchor_local="2026-03-02T09:00:00"),
                datetime(2026, 4, 6, 13, 0, tzinfo=timezone.utc),
            )
        finally:
            if prev_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = prev_tz
            time.tzset()


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

    def test_recurring_interval_advances_from_anchor_without_drift(self):
        record = self.store.add(
            "stretch",
            "2026-11-01T12:00:00Z",
            every="2h",
        )
        self.assertEqual(record["every"], "2h")
        self.assertEqual(record["anchor_at"], "2026-11-01T12:00:00Z")

        # Delivered 17 seconds past the minute; next slot stays locked to 14:00:00Z.
        delivered_at = datetime(2026, 11, 1, 12, 0, 17, tzinfo=timezone.utc)
        advanced = self.store.mark_delivered(record["id"], now=delivered_at)
        self.assertEqual(advanced["due_at"], "2026-11-01T14:00:00Z")
        self.assertEqual(advanced["anchor_at"], "2026-11-01T12:00:00Z")
        self.assertEqual(len(self.store.all()), 1)

    def test_recurring_interval_skips_missed_slots_after_downtime(self):
        record = self.store.add(
            "hydrate",
            "2026-11-01T12:00:00Z",
            every="1h",
        )
        # Host wakes at 15:30Z after 3.5h offline; next slot is 16:00Z, not 13:00Z.
        woke_at = datetime(2026, 11, 1, 15, 30, 0, tzinfo=timezone.utc)
        advanced = self.store.mark_delivered(record["id"], now=woke_at)
        self.assertEqual(advanced["due_at"], "2026-11-01T16:00:00Z")

    def test_recurring_delivery_clears_prior_retry_state(self):
        record = self.store.add("standup", "2026-11-01T09:00:00Z", every="day")
        self.store.mark_failed_attempt(record["id"], "temporary outage")
        advanced = self.store.mark_delivered(
            record["id"],
            now=datetime(2026, 11, 1, 9, 2, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(advanced["status"], "pending")
        self.assertEqual(advanced["attempts"], 0)
        self.assertIsNone(advanced["last_error"])
        self.assertIsNone(advanced["next_attempt_at"])
        self.assertEqual(advanced["due_at"], "2026-11-02T09:00:00Z")

    def test_recurring_weekday_and_day_list_advance_to_matching_days(self):
        # 2026-11-06 is a Friday; next weekday is Monday 2026-11-09.
        fri = self.store.add("work log", "2026-11-06T17:00:00Z", every="weekday")
        advanced = self.store.mark_delivered(
            fri["id"],
            now=datetime(2026, 11, 6, 17, 0, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(advanced["due_at"], "2026-11-09T17:00:00Z")

        # 2026-11-02 is a Monday; next in mon,wed,fri is Wednesday 2026-11-04.
        mwf = self.store.add("gym", "2026-11-02T07:00:00Z", every="mon,wed,fri")
        advanced_mwf = self.store.mark_delivered(
            mwf["id"],
            now=datetime(2026, 11, 2, 7, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(advanced_mwf["due_at"], "2026-11-04T07:00:00Z")

    def test_recurring_monthly_preserves_month_end_anchor(self):
        # Jan 31 -> Feb 28 -> Mar 31 (never drifts permanently to the 28th).
        record = self.store.add("invoices", "2026-01-31T09:00:00Z", every="month")
        feb = self.store.mark_delivered(
            record["id"],
            now=datetime(2026, 1, 31, 9, 0, 10, tzinfo=timezone.utc),
        )
        self.assertEqual(feb["due_at"], "2026-02-28T09:00:00Z")

        mar = self.store.mark_delivered(
            record["id"],
            now=datetime(2026, 2, 28, 9, 0, 10, tzinfo=timezone.utc),
        )
        self.assertEqual(mar["due_at"], "2026-03-31T09:00:00Z")

    def test_recurring_cancel_removes_it_permanently(self):
        record = self.store.add("vitamins", "2026-11-01T08:00:00Z", every="day")
        self.assertIsNotNone(self.store.cancel(record["id"]))
        self.assertEqual(self.store.all(), [])

    def test_recurring_daily_keeps_wall_clock_hour_across_dst(self):
        import os
        import time

        prev_tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        try:
            # 2026-03-07 09:00 EST is 14:00Z; 2026-03-08 09:00 EDT (after spring-forward) is 13:00Z.
            record = self.store.add("morning pages", "2026-03-07T09:00:00", every="day")
            self.assertEqual(record["due_at"], "2026-03-07T14:00:00Z")
            advanced = self.store.mark_delivered(
                record["id"],
                now=datetime(2026, 3, 7, 14, 0, 5, tzinfo=timezone.utc),
            )
            self.assertEqual(advanced["due_at"], "2026-03-08T13:00:00Z")
        finally:
            if prev_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = prev_tz
            time.tzset()

    def test_hermes_config_timezone_overrides_utc_process_clock(self):
        import os
        import time

        (self.env["home"] / "config.yaml").write_text("timezone: America/Edmonton\n", encoding="utf-8")
        prev_tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        try:
            # 2026-11-11 19:00 in America/Edmonton (UTC-6) is 2026-11-12T01:00:00Z, even when OS TZ is UTC.
            record = self.store.add("Make Proats", "2026-11-11T19:00:00", every="day")
            self.assertEqual(record["due_at"], "2026-11-12T01:00:00Z")
            advanced = self.store.mark_delivered(
                record["id"],
                now=datetime(2026, 11, 12, 1, 0, 5, tzinfo=timezone.utc),
            )
            self.assertEqual(advanced["due_at"], "2026-11-13T01:00:00Z")
        finally:
            if prev_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = prev_tz
            time.tzset()

    def test_recurring_calendar_reminder_keeps_wall_clock_hour_after_timezone_move(self):
        cfg = self.env["home"] / "config.yaml"
        cfg.write_text("timezone: America/Edmonton\n", encoding="utf-8")
        # Set at 19:00 in America/Edmonton (2026-11-12T01:00:00Z).
        record = self.store.add("Make Proats", "2026-11-11T19:00:00", every="day")
        self.assertEqual(record["anchor_local"], "2026-11-11T19:00:00")
        self.assertEqual(record["due_at"], "2026-11-12T01:00:00Z")

        # User moves to Europe/London (GMT, UTC+0); next delivery should be 19:00 London (19:00Z), not 01:00Z.
        cfg.write_text("timezone: Europe/London\n", encoding="utf-8")
        advanced = self.store.mark_delivered(
            record["id"],
            now=datetime(2026, 11, 12, 1, 0, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(advanced["due_at"], "2026-11-12T19:00:00Z")

    def test_a_repeating_reminder_starts_on_the_first_day_its_pattern_names(self):
        # Asked for Saturday 2026-11-07 with a weekday pattern: the first delivery has to be
        # the Monday, not the Saturday.
        record = self.store.add("work log", "2026-11-07T09:00:00Z", every="weekday")
        self.assertEqual(record["due_at"], "2026-11-09T09:00:00Z")
        self.assertEqual(record["anchor_at"], "2026-11-09T09:00:00Z")

    def test_a_weekend_rule_snaps_its_first_occurrence_the_same_way(self):
        # Asked for Wednesday 2026-11-04 with a weekend pattern: first delivery is Saturday.
        record = self.store.add("long run", "2026-11-04T09:00:00Z", every="weekend")
        self.assertEqual(record["due_at"], "2026-11-07T09:00:00Z")

    def test_a_day_list_rule_snaps_its_first_occurrence(self):
        # Asked for Sunday 2026-11-08 with mon,wed,fri: first delivery is Monday.
        record = self.store.add("gym", "2026-11-08T07:00:00Z", every="mon,wed,fri")
        self.assertEqual(record["due_at"], "2026-11-09T07:00:00Z")

    def test_a_first_occurrence_already_on_its_pattern_is_left_alone(self):
        # Monday 2026-11-02 is a weekday, so the reminder must not be pushed a day forward.
        record = self.store.add("standup", "2026-11-02T09:00:00Z", every="weekday")
        self.assertEqual(record["due_at"], "2026-11-02T09:00:00Z")

    def test_a_snap_keeps_the_time_of_day_it_was_asked_for(self):
        record = self.store.add("work log", "2026-11-07T17:45:00Z", every="weekday")
        self.assertEqual(record["anchor_local"], "2026-11-09T17:45:00")

    def test_the_wall_clock_anchor_survives_a_daylight_saving_gap(self):
        import os
        import time

        cfg = self.env["home"] / "config.yaml"
        cfg.write_text("timezone: America/New_York\n", encoding="utf-8")
        prev_tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        try:
            # 02:30 does not exist on 2026-03-08 in New York: the clocks jump 02:00 to 03:00.
            # The instant is the same one (07:30Z), but the wall clock has to stay 02:30, or
            # every later delivery drifts to 03:30 for good.
            record = self.store.add("night check", "2026-03-08T02:30:00", every="day")
            self.assertEqual(record["due_at"], "2026-03-08T07:30:00Z")
            self.assertEqual(record["anchor_local"], "2026-03-08T02:30:00")

            advanced = self.store.mark_delivered(
                record["id"],
                now=datetime(2026, 3, 8, 7, 30, 5, tzinfo=timezone.utc),
            )
            # 2026-03-09 02:30 EDT (UTC-4), not 03:30.
            self.assertEqual(advanced["due_at"], "2026-03-09T06:30:00Z")
        finally:
            if prev_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = prev_tz
            time.tzset()



    def test_a_first_occurrence_a_fraction_of_a_second_ahead_still_lands_today(self):
        # "in 5 minutes" lands mid-second, and the store keeps whole seconds. The first slot
        # must not be pushed to tomorrow just because it is a fraction behind the asked time.
        # 2026-11-02 is a Monday.
        record = self.store.add("standup", "2026-11-02T09:00:00.123456Z", every="weekday")
        self.assertEqual(record["due_at"], "2026-11-02T09:00:00Z")
        self.assertEqual(record["anchor_local"], "2026-11-02T09:00:00")

    def test_a_two_week_repeat_advances_from_its_anchor(self):
        record = self.store.add("retro", "2026-11-02T09:00:00Z", every="2w:mon")
        advanced = self.store.mark_delivered(
            record["id"], now=datetime(2026, 11, 2, 9, 0, 5, tzinfo=timezone.utc)
        )
        self.assertEqual(advanced["due_at"], "2026-11-16T09:00:00Z")
        self.assertEqual(advanced["anchor_at"], "2026-11-02T09:00:00Z")

    def test_a_two_week_repeat_keeps_its_wall_clock_across_a_timezone_move(self):
        cfg = self.env["home"] / "config.yaml"
        cfg.write_text("timezone: America/Edmonton\n", encoding="utf-8")
        record = self.store.add("retro", "2026-11-11T19:00:00", every="2w")
        self.assertEqual(record["anchor_local"], "2026-11-11T19:00:00")

        cfg.write_text("timezone: Europe/London\n", encoding="utf-8")
        advanced = self.store.mark_delivered(
            record["id"],
            now=datetime(2026, 11, 12, 1, 0, 5, tzinfo=timezone.utc),
        )
        # A fortnight after 11 Nov is 25 Nov, still at 19:00, now London time.
        self.assertEqual(advanced["due_at"], "2026-11-25T19:00:00Z")

    def test_a_monthly_ordinal_repeat_snaps_its_first_occurrence(self):
        # 2026-11-20 is past November's first Monday, so the first delivery is December's.
        record = self.store.add("payroll", "2026-11-20T09:00:00Z", every="first:mon")
        self.assertEqual(record["due_at"], "2026-12-07T09:00:00Z")
        self.assertEqual(record["anchor_local"], "2026-12-07T09:00:00")

    def test_a_monthly_ordinal_repeat_advances_to_the_next_month(self):
        record = self.store.add("payroll", "2026-11-02T09:00:00Z", every="first:mon")
        advanced = self.store.mark_delivered(
            record["id"], now=datetime(2026, 11, 2, 9, 0, 5, tzinfo=timezone.utc)
        )
        self.assertEqual(advanced["due_at"], "2026-12-07T09:00:00Z")
        self.assertEqual(advanced["anchor_at"], "2026-11-02T09:00:00Z")

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

    def test_template_round_trip(self):
        self.assertIsNone(self.store.template())
        self.store.set_template("🔔 {text}")
        self.assertEqual(self.store.template(), "🔔 {text}")
        self.store.set_template(None)
        self.assertIsNone(self.store.template())

    def test_an_unreadable_store_says_so(self):
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text("{not json")
        with self.assertRaises(store.StoreError):
            self.store.all()

    def test_lock_works_when_fcntl_is_absent(self):
        from unittest import mock

        with mock.patch.object(store, "fcntl", None):
            record = self.store.add("no fcntl", "1h")
            self.assertEqual(self.store.get(record["id"])["text"], "no fcntl")


if __name__ == "__main__":
    unittest.main()

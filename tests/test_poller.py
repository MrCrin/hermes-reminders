"""The poller: deterministic delivery, pruning on confirmation, silence when idle, and the
stub → plugin import path that the cron job depends on."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import cli, poller, sandbox, sender, stage_plugin, store  # noqa: E402


class PollerTests(unittest.TestCase):
    def setUp(self):
        self._sandbox = sandbox()
        self.env = self._sandbox.__enter__()
        self.addCleanup(lambda: self._sandbox.__exit__(None, None, None))
        self.store = store.ReminderStore()
        self.store.set_default_target("telegram")

    def test_a_due_reminder_is_sent_then_pruned(self):
        self.store.add("check the database", "0s")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            delivered, failed = poller.run_once()
        self.assertEqual((delivered, failed), (1, 0))
        self.assertEqual(self.store.all(), [])  # pruned
        self.assertEqual(stdout.getvalue(), "")  # silent tick
        log = self.env["log"].read_text()
        self.assertIn("send --to telegram --quiet -- 🔔 check the database", log)
        self.assertIn("(Reminder set on ", log)

    def test_send_separates_options_from_leading_hyphen_message(self):
        self.store.set_template("- {text}")
        self.store.add("hyphenated line", "0s")
        delivered, failed = poller.run_once()
        self.assertEqual((delivered, failed), (1, 0))
        self.assertIn("send --to telegram --quiet -- - hyphenated line", self.env["log"].read_text())

    def test_a_due_recurring_reminder_is_sent_and_rescheduled(self):
        record = self.store.add("daily review", "0s", every="day")
        before_due = record["due_at"]
        delivered, failed = poller.run_once()
        self.assertEqual((delivered, failed), (1, 0))
        kept = self.store.get(record["id"])
        self.assertIsNotNone(kept)
        self.assertEqual(kept["status"], "pending")
        self.assertGreater(kept["due_at"], before_due)
        self.assertEqual( poller.run_once(), (0, 0))

    def test_a_two_week_repeat_is_delivered_and_re_armed_a_fortnight_later(self):
        record = self.store.add("retro", "0s", every="2w")
        self.assertEqual(poller.run_once(), (1, 0))
        kept = self.store.get(record["id"])
        self.assertEqual(kept["status"], "pending")
        gap = store.parse_when(kept["due_at"]) - store.parse_when(record["due_at"])
        self.assertEqual(gap, timedelta(days=14))

    def test_a_repeat_whose_rule_cannot_be_advanced_is_retired_not_repeated(self):
        """The message is already on the wire by the time the rule is advanced, so a rule this
        build cannot read must retire the reminder rather than put the same sentence on the
        user's screen every minute until someone notices."""
        record = self.store.add("drink water", "0s", every="day")
        # Written by a newer version, or edited by hand: not a rule this build reads.
        data = json.loads(self.store.path.read_text())
        data["reminders"][0]["every"] = "every-monday"
        self.store.path.write_text(json.dumps(data))

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            delivered, failed = poller.run_once()
        self.assertEqual((delivered, failed), (0, 1))
        self.assertIn("every-monday", stderr.getvalue())

        kept = self.store.get(record["id"])
        self.assertEqual(kept["status"], "failed")
        self.assertIn("every-monday", kept["last_error"])
        self.assertIsNone(kept["next_attempt_at"])

        # The tick after that has nothing to do, and nothing is sent a second time.
        self.assertEqual(poller.run_once(), (0, 0))
        sent = [line for line in self.env["log"].read_text().splitlines() if "drink water" in line]
        self.assertEqual(len(sent), 1)

    def test_a_repeat_whose_stored_rule_is_not_a_string_is_retired_the_same_way(self):
        record = self.store.add("drink water", "0s", every="day")
        data = json.loads(self.store.path.read_text())
        data["reminders"][0]["every"] = ["mon"]
        self.store.path.write_text(json.dumps(data))

        with contextlib.redirect_stderr(io.StringIO()):
            delivered, failed = poller.run_once()
        self.assertEqual((delivered, failed), (0, 1))
        self.assertEqual(self.store.get(record["id"])["status"], "failed")

    def test_the_poller_keeps_delivering_after_one_bad_repeat_is_retired(self):
        """One unreadable record must not cost the reminders queued behind it."""
        broken = self.store.add("broken", "0s", every="day")
        healthy = self.store.add("healthy", "0s", every="day")
        data = json.loads(self.store.path.read_text())
        for entry in data["reminders"]:
            if entry["id"] == broken["id"]:
                entry["every"] = "every-monday"
        self.store.path.write_text(json.dumps(data))

        with contextlib.redirect_stderr(io.StringIO()):
            delivered, failed = poller.run_once()
        self.assertEqual((delivered, failed), (1, 1))
        self.assertEqual(self.store.get(broken["id"])["status"], "failed")
        self.assertEqual(self.store.get(healthy["id"])["status"], "pending")


    def test_one_unreadable_due_time_does_not_stop_the_rest_of_the_pass(self):
        broken = self.store.add("broken", "0s")
        healthy = self.store.add("healthy", "0s")
        data = json.loads(self.store.path.read_text())
        for entry in data["reminders"]:
            if entry["id"] == broken["id"]:
                entry["due_at"] = 1750000000  # a JSON number, not a timestamp
        self.store.path.write_text(json.dumps(data))

        self.assertEqual(poller.run_once(), (1, 0))
        self.assertIsNone(self.store.get(healthy["id"]))  # delivered and pruned

    def test_nothing_due_is_silent_and_sends_nothing(self):
        self.store.add("tomorrow", "1d")
        delivered, failed = poller.run_once()
        self.assertEqual((delivered, failed), (0, 0))
        self.assertEqual(len(self.store.all()), 1)
        self.assertFalse(self.env["log"].exists())

    def test_a_failed_send_keeps_the_reminder(self):
        record = self.store.add("will bounce", "0s")
        os.environ["FAKE_HERMES_FAIL"] = "1"
        try:
            delivered, failed = poller.run_once()
        finally:
            os.environ.pop("FAKE_HERMES_FAIL", None)
        self.assertEqual((delivered, failed), (0, 1))
        kept = self.store.get(record["id"])
        self.assertIsNotNone(kept)
        self.assertEqual(kept["attempts"], 1)
        self.assertIn("exited 1", kept["last_error"])

    def test_main_exits_nonzero_when_a_send_fails(self):
        self.store.add("will bounce", "0s")
        import os
        os.environ["FAKE_HERMES_FAIL"] = "1"
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(poller.main(), 1)
        finally:
            os.environ.pop("FAKE_HERMES_FAIL", None)

    def test_a_missing_sender_is_recorded_not_guessed(self):
        record = self.store.add("no sender", "0s")
        original = sender.hermes_executable
        sender.hermes_executable = lambda: None
        try:
            delivered, failed = poller.run_once()
        finally:
            sender.hermes_executable = original
        self.assertEqual((delivered, failed), (0, 1))
        self.assertIn("not found", self.store.get(record["id"])["last_error"])

    def test_a_reminder_with_no_target_is_not_sent_blindly(self):
        self.store.set_default_target(None)
        record = self.store.add("no target", "0s")
        delivered, failed = poller.run_once()
        self.assertEqual((delivered, failed), (0, 1))
        self.assertIn("no delivery target", self.store.get(record["id"])["last_error"])

    def test_hermes_executable_falls_back_to_user_local_bin(self):
        from unittest import mock

        fake_user_home = self.env["root"] / "user-home"
        local_bin = fake_user_home / ".local" / "bin"
        local_bin.mkdir(parents=True)
        local_hermes = local_bin / "hermes"
        local_hermes.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        local_hermes.chmod(0o755)
        with mock.patch.object(sender.shutil, "which", return_value=None), \
                mock.patch.object(sender.Path, "home", return_value=fake_user_home):
            self.assertEqual(sender.hermes_executable(), str(local_hermes))


class StubEndToEndTests(unittest.TestCase):
    """The cron job runs the stub, the stub imports the plugin. Prove that path works."""

    def test_the_stub_delivers_through_the_plugin(self):
        with sandbox() as env:
            stage_plugin(env["home"])
            stub = cli.write_stub()
            self.assertTrue(stub.exists())
            self.assertTrue(stub.stat().st_mode & 0o111, "the stub must be executable")

            store.ReminderStore().set_default_target("telegram")
            store.ReminderStore().add("from the cron stub", "0s")

            proc = subprocess.run(
                [sys.executable, str(stub)], capture_output=True, text=True, timeout=60
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(store.ReminderStore().all(), [])
            self.assertIn("from the cron stub", env["log"].read_text())

    def test_the_stub_says_what_it_is_and_where_the_code_lives(self):
        with sandbox() as env:
            stub = cli.write_stub()
            text = stub.read_text()
            self.assertIn("hermes reminders setup", text)
            self.assertIn(cli.PLUGIN_NAME, text)
            self.assertIn("plugins", text)

    def test_the_stub_never_reaches_into_an_unrelated_home(self):
        # A `~/.hermes/plugins` that is not this home's own is somebody else's install, and a
        # hardcoded default home can be another profile's. The stub resolves the plugin from the
        # home it lives in, and from the root home above it when it runs for a profile, and from
        # nowhere else.
        with sandbox() as env:
            user_home = env["root"] / "user-home"
            foreign = user_home / ".hermes" / "plugins" / cli.PLUGIN_NAME
            foreign.mkdir(parents=True)
            marker = env["root"] / "foreign-plugin-imported"
            (foreign / "poller.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('imported')\n"
                "def main():\n    return 0\n",
                encoding="utf-8",
            )
            stub = cli.write_stub()
            subprocess.run(
                [sys.executable, str(stub)], capture_output=True, text=True, timeout=60,
                env=dict(os.environ, HOME=str(user_home)),
            )
            self.assertFalse(marker.exists(), "the stub imported a plugin from a foreign home")

    def test_the_stub_in_a_secondary_profile_finds_plugin_in_root_home(self):
        with sandbox() as env:
            root_home = env["home"]
            stage_plugin(root_home)
            profile_home = root_home / "profiles" / "worker"
            os.environ["HERMES_HOME"] = str(profile_home)
            stub = cli.write_stub()
            store.ReminderStore().set_default_target("telegram")
            store.ReminderStore().add("from secondary profile", "0s")

            proc = subprocess.run(
                [sys.executable, str(stub)], capture_output=True, text=True, timeout=60
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(store.ReminderStore().all(), [])
            self.assertIn("from secondary profile", env["log"].read_text())


if __name__ == "__main__":
    unittest.main()

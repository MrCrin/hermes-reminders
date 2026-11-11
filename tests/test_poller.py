"""The poller: deterministic delivery, pruning on confirmation, silence when idle, and the
stub → plugin import path that the cron job depends on."""

from __future__ import annotations

import contextlib
import io
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
        self.assertIn("send --to telegram --quiet 🔔 check the database", log)
        self.assertIn("(Reminder set on ", log)

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


if __name__ == "__main__":
    unittest.main()

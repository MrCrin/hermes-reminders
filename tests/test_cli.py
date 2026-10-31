"""The CLI: add/list/cancel, and the setup that installs the stub and (idempotently) the job."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import cli, sandbox, store  # noqa: E402


def parse(argv: list[str]):
    """Build the parser the way the host does, then run the command it selects."""
    parser = argparse.ArgumentParser(prog="hermes reminders")
    cli.setup_parser(parser)
    args = parser.parse_args(argv)
    return args.func(args)


class CliTests(unittest.TestCase):
    def setUp(self):
        self._sandbox = sandbox()
        self.env = self._sandbox.__enter__()
        self.addCleanup(lambda: self._sandbox.__exit__(None, None, None))
        self.store = store.ReminderStore()

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = parse(argv)
        return code, out.getvalue(), err.getvalue()

    def test_add_then_list_then_cancel(self):
        code, out, _ = self.run_cli(["add", "ring the dentist", "--in", "2h", "--target", "telegram"])
        self.assertEqual(code, 0)
        reminder_id = out.split()[0]

        code, out, _ = self.run_cli(["list"])
        self.assertEqual(code, 0)
        self.assertIn("ring the dentist", out)

        code, out, _ = self.run_cli(["cancel", reminder_id[:4]])
        self.assertEqual(code, 0)
        self.assertEqual(self.store.all(), [])

    def test_add_without_a_time_is_refused(self):
        code, _, err = self.run_cli(["add", "no time given"])
        self.assertEqual(code, 2)
        self.assertIn("--at", err)

    def test_cancel_of_an_unknown_id_reports_and_fails(self):
        code, _, err = self.run_cli(["cancel", "deadbeef"])
        self.assertEqual(code, 1)
        self.assertIn("no reminder matches", err)

    def test_list_json_is_machine_readable(self):
        self.run_cli(["add", "robot food", "--at", "2026-11-01T15:00Z"])
        _, out, _ = self.run_cli(["list", "--json"])
        records = json.loads(out)
        self.assertEqual(records[0]["text"], "robot food")
        self.assertEqual(records[0]["due_at"], "2026-11-01T15:00:00Z")

    def test_list_empty_is_not_an_error(self):
        code, out, _ = self.run_cli(["list"])
        self.assertEqual(code, 0)
        self.assertIn("No reminders", out)

    def test_setup_writes_the_stub_and_creates_the_job(self):
        code, out, _ = self.run_cli(["setup", "--target", "telegram"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.store.default_target(), "telegram")
        stub = self.env["home"] / "scripts" / cli.STUB_NAME
        self.assertTrue(stub.exists())
        self.assertIn("cron create", self.env["log"].read_text())

    def test_setup_does_not_add_a_second_job_when_one_is_installed(self):
        self.run_cli(["setup"])
        self.env["log"].write_text("")
        # The scheduler's own store now shows our job, so setup must report rather than duplicate it.
        cron_dir = self.env["home"] / "cron"
        cron_dir.mkdir(parents=True, exist_ok=True)
        (cron_dir / "jobs.json").write_text(json.dumps(
            {"jobs": [{"id": "abc123", "name": cli.JOB_NAME, "script": cli.STUB_NAME}]}
        ))
        code, out, _ = self.run_cli(["setup"])
        self.assertEqual(code, 0)
        self.assertIn("already installed", out)
        self.assertNotIn("cron create", self.env["log"].read_text())

    def test_setup_no_cron_prints_the_command_instead(self):
        code, out, _ = self.run_cli(["setup", "--no-cron"])
        self.assertEqual(code, 0)
        self.assertIn(cli.cron_command(), out)
        self.assertFalse(self.env["log"].exists())

    def test_status_reports_the_whole_chain(self):
        self.run_cli(["setup", "--no-cron"])
        self.run_cli(["add", "something", "--in", "1h"])
        code, out, _ = self.run_cli(["status"])
        self.assertEqual(code, 0)
        for needle in ("store:", "default target:", "reminders:      1 pending", "stub:", "cron job:", "hermes send:"):
            self.assertIn(needle, out)

    def test_poll_reports_nothing_due(self):
        code, out, _ = self.run_cli(["poll"])
        self.assertEqual(code, 0)
        self.assertIn("Nothing due", out)

    def test_unknown_command_prints_usage(self):
        code, _, err = self.run_cli([])
        self.assertEqual(code, 0)
        self.assertIn("usage: hermes reminders", err)


if __name__ == "__main__":
    unittest.main()

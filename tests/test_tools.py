"""Agent tools and the `/reminders` slash command."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reminders  # noqa: E402
from support import sandbox, store, tools  # noqa: E402


class DummyContext:
    def __init__(self) -> None:
        self.tools: list[str] = []
        self.commands: dict[str, dict] = {}
        self.cli_commands: list[str] = []
        self.skills: list[str] = []

    def register_tool(self, name: str, **kwargs) -> None:
        self.tools.append(name)

    def register_command(self, name: str, handler, description: str = "", args_hint: str = "", **kwargs) -> None:
        self.commands[name] = {
            "handler": handler,
            "description": description,
            "args_hint": args_hint,
        }

    def register_cli_command(self, name: str, **kwargs) -> None:
        self.cli_commands.append(name)

    def register_skill(self, name: str, *args, **kwargs) -> None:
        self.skills.append(name)


class ToolsAndSlashTests(unittest.TestCase):
    def setUp(self):
        self._sandbox = sandbox()
        self.env = self._sandbox.__enter__()
        self.addCleanup(lambda: self._sandbox.__exit__(None, None, None))
        self.store = store.ReminderStore()

    def test_register_wires_tools_cli_skill_and_slash_command(self):
        ctx = DummyContext()
        reminders.register(ctx)
        self.assertEqual(ctx.tools, ["reminders_add", "reminders_list", "reminders_cancel"])
        self.assertIn("reminders", ctx.commands)
        # Telegram menu inclusion requires args_hint not to start with '<'.
        self.assertFalse(ctx.commands["reminders"]["args_hint"].startswith("<"))

    def test_tool_add_list_and_cancel_recurring(self):
        added = tools.handle_add({
            "text": "Review your goals",
            "when": "2026-11-01T09:00:00Z",
            "every": "daily",
        })
        self.assertIn("repeats every day", added)
        records = self.store.all()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["every"], "day")

        listed = tools.handle_list({})
        self.assertIn(records[0]["id"], listed)
        self.assertIn("[every day]", listed)
        self.assertIn("2026-11-01T09:00:00Z", listed)

        cancelled = tools.handle_cancel({"id": records[0]["id"][:4]})
        self.assertIn("Cancelled", cancelled)
        self.assertEqual(self.store.all(), [])

    def test_slash_command_lists_and_cancels_without_an_agent_turn(self):
        self.assertEqual(tools.handle_slash(""), "No reminders in the pool.")

        record = self.store.add("Take vitamins", "2026-11-01T08:00:00Z", every="day")
        failed = self.store.add("Broken target", "2026-11-01T08:00:00Z")
        for _ in range(self.store.max_attempts()):
            self.store.mark_failed_attempt(failed["id"], "boom")

        pending_only = tools.handle_slash("")
        self.assertIn("Take vitamins", pending_only)
        self.assertIn("[every day]", pending_only)
        self.assertNotIn("Broken target", pending_only)

        all_items = tools.handle_slash("all")
        self.assertIn("Take vitamins", all_items)
        self.assertIn("Broken target", all_items)

        cancelled = tools.handle_slash(f"cancel {record['id'][:4]}")
        self.assertIn("Cancelled", cancelled)
        self.assertIsNone(self.store.get(record["id"]))

        usage = tools.handle_slash("unknown-subcommand")
        self.assertIn("Usage: /reminders", usage)

    def test_tool_add_warns_when_poller_or_target_is_not_configured(self):
        import json
        from support import cli

        unconfigured = tools.handle_add({
            "text": "Call supplier",
            "when": "2026-11-01T09:00:00Z",
        })
        self.assertIn("hermes reminders setup", unconfigured)

        # Once stub, cron job, and default target are in place, no warning is appended.
        cli.write_stub()
        cron_dir = self.env["home"] / "cron"
        cron_dir.mkdir(parents=True, exist_ok=True)
        (cron_dir / "jobs.json").write_text(
            json.dumps({"jobs": [{"id": "job1", "name": cli.JOB_NAME, "script": cli.STUB_NAME}]}),
            encoding="utf-8",
        )
        self.store.set_default_target("telegram")
        configured = tools.handle_add({
            "text": "Call supplier",
            "when": "2026-11-01T09:00:00Z",
        })
        self.assertNotIn("hermes reminders setup", configured)


if __name__ == "__main__":
    unittest.main()

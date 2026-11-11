"""The delivery format: bell, the sentence, and a '(Reminder set on …)' line."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import message, store  # noqa: E402

RECORD = {
    "id": "c8eb6cea",
    "text": "Charge your phone",
    "due_at": "2026-10-31T18:48:32Z",
    "created_at": "2026-10-31T18:43:32Z",
}


class RenderTests(unittest.TestCase):
    def test_default_shape(self):
        lines = message.render(RECORD).splitlines()
        self.assertEqual(lines[0], "🔔 Charge your phone")
        self.assertTrue(lines[1].startswith("(Reminder set on "), lines[1])
        self.assertTrue(lines[1].endswith(")"), lines[1])
        self.assertNotIn("unknown", message.render(RECORD))

    def test_created_renders_in_this_machine_s_local_time(self):
        expected = store.parse_when(RECORD["created_at"]).astimezone().strftime(message.LOCAL_FORMAT)
        self.assertEqual(message.render(RECORD, "{created}"), expected)

    def test_utc_placeholders_are_stable_regardless_of_timezone(self):
        self.assertEqual(message.render(RECORD, "{created_utc}"), "2026-10-31T18:43Z")
        self.assertEqual(message.render(RECORD, "{due_utc}"), "2026-10-31T18:48Z")

    def test_a_custom_template_is_used_verbatim(self):
        self.assertEqual(message.render(RECORD, "{text} [{id}]"), "Charge your phone [c8eb6cea]")

    def test_a_broken_template_falls_back_to_the_default(self):
        for broken in ("{nope}", "{text", "!r}"):
            with self.subTest(broken=broken):
                self.assertEqual(message.render(RECORD, broken), message.render(RECORD))

    def test_missing_timestamps_degrade_to_unknown(self):
        self.assertEqual(message.render({"text": "x"}, "{created}"), "unknown")
        self.assertEqual(message.render({"text": "x"}, "{created_utc}"), "unknown")

    def test_an_empty_record_still_renders_something_sendable(self):
        self.assertTrue(message.render({}).strip())

    def test_the_text_is_stripped(self):
        self.assertEqual(message.render({"text": "  spaced  "}, "{text}"), "spaced")

    def test_every_placeholder_renders_recurrence(self):
        recurring = dict(RECORD, every="day")
        self.assertEqual(message.render(recurring, "{text} ({every})"), "Charge your phone (day)")
        self.assertEqual(message.render(RECORD, "{every}"), "")



if __name__ == "__main__":
    unittest.main()

"""Packaging guards: the manifest, the package metadata and the changelog must agree.

The version lives in two files, so it can drift silently — a bump applied to one of them leaves
an install that reports a version the code does not have, and a catalog entry is required to
match the code at the commit it pins. One test is cheaper than remembering.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT  # noqa: E402

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def declared_version() -> str:
    """The version from ``plugin.yaml`` — the manifest is the plugin's identity."""
    manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    match = re.search(r"^version:\s*(\S+)\s*$", manifest, re.MULTILINE)
    assert match, "plugin.yaml declares no version"
    return match.group(1)


def packaged_version() -> str:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"\s*$', pyproject, re.MULTILINE)
    assert match, "pyproject.toml declares no version"
    return match.group(1)


class PackagingTests(unittest.TestCase):
    def test_the_two_versions_agree_and_are_semver(self):
        declared = declared_version()
        self.assertRegex(declared, SEMVER)
        self.assertEqual(
            declared,
            packaged_version(),
            "plugin.yaml and pyproject.toml disagree on the version — bump both",
        )

    def test_the_changelog_has_a_section_for_this_version(self):
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("## [Unreleased]", changelog)
        self.assertIn(f"## [{declared_version()}]", changelog, "no changelog entry for this version")

    def test_the_manifest_names_a_plugin_and_its_kind(self):
        manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        self.assertRegex(manifest, r"(?m)^name:\s*\S+")
        self.assertRegex(manifest, r"(?m)^kind:\s*backend\s*$")
        self.assertRegex(manifest, r"(?m)^requires_hermes:\s*\"[><=~^ ]*\d+\.\d+")
        # No platform subset: the store's lock is optional (see
        # test_lock_works_when_fcntl_is_absent in test_store), so nothing here
        # justifies claiming one.
        self.assertNotIn("platforms:", manifest)


if __name__ == "__main__":
    unittest.main()

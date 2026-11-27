"""Test plumbing.

Loads the plugin package straight from the repo (no install, no Hermes import), and gives
every test a throwaway ``HERMES_HOME`` plus a fake ``hermes`` first on ``PATH`` — so no test
can touch a real store, a real cron schedule or a real chat.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import sys
import tempfile
import types
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

_SPEC = importlib.util.spec_from_file_location(
    "reminders", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
assert _SPEC and _SPEC.loader, "could not build a spec for the plugin package"
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("reminders", _MODULE)
_SPEC.loader.exec_module(_MODULE)

store = importlib.import_module("reminders.store")
sender = importlib.import_module("reminders.sender")
poller = importlib.import_module("reminders.poller")
cli = importlib.import_module("reminders.cli")
tools = importlib.import_module("reminders.tools")
message = importlib.import_module("reminders.message")

FAKE_HERMES = """#!/usr/bin/env bash
# Stand-in for the real `hermes` CLI: records how it was called, then succeeds or fails.
echo "$@" >> "${FAKE_HERMES_LOG:-/dev/null}"
if [ -n "${FAKE_HERMES_FAIL:-}" ]; then
  echo "fake hermes: the platform refused the message" >&2
  exit 1
fi
exit 0
"""


@contextmanager
def sandbox(*, fail_sends: bool = False, with_hermes: bool = True):
    """A temporary HERMES_HOME and a fake `hermes`. Yields paths the tests can assert on."""
    tmp = Path(tempfile.mkdtemp(prefix="reminders-test-"))
    bin_dir = tmp / "bin"
    home = tmp / "home"
    bin_dir.mkdir()
    home.mkdir()
    log = tmp / "hermes-calls.log"
    if with_hermes:
        executable = bin_dir / "hermes"
        executable.write_text(FAKE_HERMES, encoding="utf-8")
        executable.chmod(0o755)

    previous = dict(os.environ)
    os.environ["HERMES_HOME"] = str(home)
    os.environ.pop("REMINDERS_STORE", None)
    os.environ.pop("REMINDERS_VERBOSE", None)
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{previous.get('PATH', '')}"
    os.environ["FAKE_HERMES_LOG"] = str(log)
    if fail_sends:
        os.environ["FAKE_HERMES_FAIL"] = "1"
    try:
        yield {"home": home, "log": log, "bin": bin_dir, "root": tmp}
    finally:
        os.environ.clear()
        os.environ.update(previous)
        shutil.rmtree(tmp, ignore_errors=True)


def stage_plugin(home: Path) -> Path:
    """Copy the repo into ``<home>/plugins/<manifest name>`` the way an install would."""
    target = home / "plugins" / cli.PLUGIN_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        ROOT, target,
        ignore=shutil.ignore_patterns("__pycache__", ".git", "*.pyc", "tests"),
    )
    return target


@contextmanager
def hermes_reports_home(home: Path):
    """Stand in for ``hermes_constants.get_hermes_home()`` answering *home*.

    The real module ships inside a Hermes install and is deliberately not a test dependency, so
    this installs a module carrying the one function the plugin calls. In a live process that
    function resolves the profile the current call belongs to before it looks at ``HERMES_HOME``.
    """
    module = types.ModuleType("hermes_constants")
    setattr(module, "get_hermes_home", lambda: Path(home))
    with mock.patch.dict(sys.modules, {"hermes_constants": module}):
        yield


@contextmanager
def hermes_absent():
    """Make ``hermes_constants`` unimportable, the way it is for the standalone cron stub."""
    with mock.patch.dict(sys.modules, {"hermes_constants": None}):
        yield

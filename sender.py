"""Delivery: hand a reminder to Hermes' own send path, and report the truth about it.

`hermes send` reuses the platform credentials Hermes is already configured with, so this
plugin holds no tokens and no chat ids of its own, and needs no running gateway for
bot-token platforms.

Its exit code is the contract this plugin is built on: **0 only when the platform accepted
the message**, 1 when delivery failed at the platform level. That is what makes
"prune only on confirmed delivery" possible.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .store import hermes_home

SEND_TIMEOUT_SECONDS = 30


def hermes_executable() -> str | None:
    """The `hermes` command, or None if it cannot be found."""
    found = shutil.which("hermes")
    if found:
        return found
    for candidate in (
        Path.home() / ".local" / "bin" / "hermes",
        hermes_home() / "bin" / "hermes",
        Path("/usr/local/bin/hermes"),
    ):
        if candidate.exists():
            return str(candidate)
    return None


def send(text: str, target: str | None = None) -> tuple[bool, str]:
    """Send one message. Returns ``(delivered, error)``.

    The error string never contains the message target's credentials — `hermes send` reads
    them itself and this function only keeps the first line of whatever it printed.
    """
    executable = hermes_executable()
    if not executable:
        return False, "the `hermes` command was not found on PATH"
    target = (target or "").strip()
    if not target:
        return False, "no delivery target — pass one, or set a default with `hermes reminders setup --target`"

    command = [executable, "send", "--to", target, "--quiet", "--", text]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=SEND_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return False, f"`hermes send` timed out after {SEND_TIMEOUT_SECONDS}s"
    except OSError as exc:
        return False, f"could not run `hermes send`: {exc.__class__.__name__}"

    if proc.returncode == 0:
        return True, ""
    detail = " ".join((proc.stderr or proc.stdout or "").split())[:300]
    return False, f"hermes send exited {proc.returncode}" + (f": {detail}" if detail else "")

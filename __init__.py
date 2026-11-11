"""reminders — a deterministic reminder pool for Hermes Agent.

Three agent tools (`reminders_add`, `reminders_list`, `reminders_cancel`) and one CLI
command (`hermes reminders …`) over a single JSON store, plus a no-LLM cron poller
(:mod:`reminders.poller`) that delivers whatever is due and prunes it only once delivery is
confirmed.

Imports happen inside :func:`register` so that importing this package stays cheap and
dependency-free: the cron stub imports it from Hermes' own interpreter.
"""

from __future__ import annotations

from pathlib import Path


def register(ctx) -> None:
    """Register the reminder tools, the CLI command and the usage skill."""
    from . import cli, tools as _t

    for name, schema, handler, emoji in (
        ("reminders_add", _t.ADD_SCHEMA, _t.handle_add, "⏰"),
        ("reminders_list", _t.LIST_SCHEMA, _t.handle_list, "📋"),
        ("reminders_cancel", _t.CANCEL_SCHEMA, _t.handle_cancel, "🗑️"),
    ):
        ctx.register_tool(name=name, toolset="reminders", schema=schema, handler=handler, emoji=emoji)

    ctx.register_cli_command(
        name="reminders",
        help="Add, list and cancel reminders; install the no-LLM delivery poller",
        setup_fn=cli.setup_parser,
        handler_fn=cli.handle,
        description=(
            "Manage the reminder pool: `hermes reminders add|list|cancel|setup|poll|status`. "
            "One JSON store; one cron job delivers what is due without calling a model."
        ),
    )

    if hasattr(ctx, "register_command"):
        ctx.register_command(
            name="reminders",
            handler=_t.handle_slash,
            description="List set reminders, or cancel one (/reminders cancel <id>)",
            args_hint="[all | cancel <id>]",
        )


    # The skill is how an agent learns the tools exist without discovering them by hand: a
    # request like "remind me in 5 minutes" is otherwise easy to misread as a system to go and
    # find in the filesystem, in memory, or through the CLI.
    skill = Path(__file__).resolve().parent / "SKILL.md"
    if skill.is_file():
        ctx.register_skill(
            "using",
            skill,
            description=(
                "Use when the user asks to be reminded of something at a time, or asks what "
                "reminders are set — set one with the reminders_add tool rather than hunting "
                "for the store or the CLI."
            ),
        )


__all__ = ["register"]

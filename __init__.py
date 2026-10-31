"""reminders — a deterministic reminder pool for Hermes Agent.

Three agent tools (`reminders_add`, `reminders_list`, `reminders_cancel`) and one CLI
command (`hermes reminders …`) over a single JSON store, plus a no-LLM cron poller
(:mod:`reminders.poller`) that delivers whatever is due and prunes it only once delivery is
confirmed.

Imports happen inside :func:`register` so that importing this package stays cheap and
dependency-free: the cron stub imports it from Hermes' own interpreter.
"""

from __future__ import annotations


def register(ctx) -> None:
    """Register the reminder tools and the ``hermes reminders`` command."""
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


__all__ = ["register"]

"""memory-shield: keep an agent from rewriting or wiping what it remembers about its owner.

One pre_tool_call hook on the built-in `memory` tool, plus file and shell tools when they try to
edit the memory files directly. By default the owner's profile is append-only, the agent's own
notes cannot be deleted, and nothing is written to memory from a shared chat unless the person
writing is on the owner's trusted list. Violations are blocked,
escalated to Hermes' approval gate ("approve") or only logged ("observe"). Right before an edit
or deletion goes through, a copy of the memory file is kept so it can be restored.
One slash command, /memory-shield, shows the policy, the log and the snapshots.
"""

from __future__ import annotations

import logging

from .shield import WATCHED_TOOLS, Shield, decide

logger = logging.getLogger(__name__)

_shield = None


def _on_pre_tool_call(tool_name=None, args=None, task_id=None, **kwargs):
    if tool_name not in WATCHED_TOOLS or _shield is None:
        return None
    try:
        return _shield.check(tool_name, args)
    except Exception as exc:  # fall back to the bare policy rather than to no protection
        logger.warning("memory-shield: check failed, applying the policy without log or snapshot: %s", exc)
        return decide(tool_name, args, _shield._get_config)


def _command(raw_args: str = "") -> str:
    return _shield.command(raw_args)


def register(ctx) -> None:
    global _shield
    _shield = Shield(get_config=ctx.get_config)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_command("memory-shield", _command,
                         description="Memory guard: policy, log of blocked writes, snapshots and restore",
                         args_hint="[log | snapshots | restore <n> | whoami]")

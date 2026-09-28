"""Run a Brainstorm Council through three separately configured agent-exec seats.

Set BRAINSTORM_ROOT and BRAINSTORM_SESSION_ID for an existing Brainstorm
session.  The service workspace ID is intentionally the registered empty
``brainstorm-scratch`` workspace: Council passes ephemeral seat directories,
so providers use explicit prompt-only mode and prompts must be self-contained.
This exercises only the completion seam; it does not promise search or source
verification.  It uses Brainstorm's public API and does not write events.
"""

from __future__ import annotations

import os
from pathlib import Path

from agent_exec.client import Client
from agent_exec.integrations.brainstorm import BrainstormProvider
from brainstorm import api


def main() -> None:
    root = Path(os.environ["BRAINSTORM_ROOT"]).resolve()
    session_id = os.environ["BRAINSTORM_SESSION_ID"]
    caller_task_id = os.environ.get("AGENT_EXEC_CALLER_TASK_ID")
    with Client() as client:
        providers = {
            "codex": BrainstormProvider(
                client, "brainstorm-scratch", "codex-scout", prompt_only=True, caller_task_id=caller_task_id
            ),
            "claude": BrainstormProvider(
                client, "brainstorm-scratch", "claude-chat", prompt_only=True, caller_task_id=caller_task_id
            ),
            "grok": BrainstormProvider(
                client, "brainstorm-scratch", "grok-chat", prompt_only=True, caller_task_id=caller_task_id
            ),
        }
        print(api.start_council(root, session_id, providers=providers, sync=True))


if __name__ == "__main__":
    main()

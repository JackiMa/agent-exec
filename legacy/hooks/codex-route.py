#!/usr/bin/env python3
"""PreToolUse hook: hard-block Claude from editing source files directly, so
implementation must go through the codex-worker agent.

CLAUDE.md is a *soft* policy — the model can talk itself out of it. This is the
hard version: it removes the capability rather than discouraging its use.

OFF by default. Toggle with `codex-route on|off|status`.

Config: ~/.codex-worker/route.json
  {"extensions": [".py", ".ts", ...],   # only these are blocked
   "allow_globs": ["**/.claude/**"]}    # ...except these
"""

import fnmatch
import json
import os
import sys
from pathlib import Path

HOME = Path(os.environ.get("CODEX_WORKER_HOME", Path.home() / ".codex-worker"))
FLAG = HOME / "ROUTE_ENFORCE"
CONFIG = HOME / "route.json"

DEFAULTS = {
    # Source code. Docs, config and data stay editable — routing every YAML
    # tweak through a subagent is friction with no payoff.
    "extensions": [
        ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".rs", ".go", ".java",
        ".kt", ".swift", ".c", ".h", ".cc", ".cpp", ".hpp", ".rb", ".php",
        ".cs", ".scala", ".sh", ".bash", ".zsh", ".sql", ".vue", ".svelte",
    ],
    "allow_globs": [
        "**/.claude/**",      # never lock yourself out of the config
        "**/*.test.*", "**/*.spec.*",
        "**/scratchpad/**", "/tmp/**",
    ],
}

ALLOW = {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                "permissionDecision": "allow"}}


def emit(obj):
    print(json.dumps(obj))
    sys.exit(0)


def main():
    if not FLAG.exists():
        emit(ALLOW)

    try:
        payload = json.load(sys.stdin)
    except Exception:
        emit(ALLOW)                       # never break the session on bad input

    path = (payload.get("tool_input") or {}).get("file_path")
    if not path:
        emit(ALLOW)

    cfg = DEFAULTS.copy()
    if CONFIG.exists():
        try:
            cfg.update(json.loads(CONFIG.read_text()))
        except Exception:
            pass

    if Path(path).suffix.lower() not in cfg["extensions"]:
        emit(ALLOW)
    if any(fnmatch.fnmatch(path, g) for g in cfg["allow_globs"]):
        emit(ALLOW)

    emit({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            f"Routing policy: source edits go to Codex, not Claude. "
            f"Blocked write to {path}.\n\n"
            f"Delegate instead, by what the change touches:\n"
            f'  codex-chore   mechanical, nothing to understand (luna)\n'
            f'  codex-worker  writing NEW code to a spec (terra)\n'
            f'  codex-deep    bug fixes, refactors, existing code (sol)\n'
            f'  -> Agent(subagent_type: "<one of the above>", prompt: "<brief>")\n\n'
            f"The brief must name absolute paths, constraints and acceptance "
            f"criteria — the worker does not see this conversation.\n"
            f"If this edit genuinely should not go through Codex, ask the user "
            f"to run `codex-route off`. Do not work around this hook."
        ),
    }})


if __name__ == "__main__":
    main()

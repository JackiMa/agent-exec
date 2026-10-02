"""Provider-specific commands and output parsing. No shell interpretation."""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

from .config import Role, Settings


def command(settings: Settings, role: Role, cwd: Path, *, delegate_scout: bool = False) -> list[str]:
    if role.provider == "command":
        # Trusted administrator configuration, primarily for deterministic fixtures.
        return list(role.argv)
    configured = settings.executables[role.provider]
    executable = shutil.which(configured)
    if not executable:
        raise ValueError(f"Provider executable unavailable: {role.provider}")
    if role.provider == "codex":
        args = [executable, "exec", "--json", "--skip-git-repo-check", "--cd", str(cwd), "--sandbox", "workspace-write" if role.access == "worktree" else "read-only", "-c", "features.multi_agent=false", "-c", "features.multi_agent_v2=false", "-c", f'model_reasoning_effort="{role.effort}"']
        # An override for a nonexistent MCP entry creates an invalid transport.
        # Disable only our installed entry, retaining user's provider/auth config.
        config = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
        if config.exists() and re.search(r'^\s*\[mcp_servers\.[\"\x27]?agent_exec[\"\x27]?\]\s*$', config.read_text(), re.MULTILINE):
            args += ["-c", "mcp_servers.agent_exec.enabled=false"]
        if role.model:
            args += ["--model", role.model]
        if delegate_scout:
            args += ["-c", f"mcp_servers.agent_exec_scout.command={json.dumps(sys.executable)}",
                     "-c", 'mcp_servers.agent_exec_scout.args=["-m", "agent_exec.mcp_server"]',
                     "-c", "mcp_servers.agent_exec_scout.enabled=true",
                     "-c", 'mcp_servers.agent_exec_scout.env_vars=["AGENT_EXEC_CHILD", "AGENT_EXEC_SCOUT_TOKEN", "AGENT_EXEC_URL", "PYTHONPATH"]',
                     "-c", 'mcp_servers.agent_exec_scout.env.AGENT_EXEC_CHILD="1"',
                     "-c", 'mcp_servers.agent_exec_scout.default_tools_approval_mode="writes"',
                     "-c", 'mcp_servers.agent_exec_scout.tools.agent_exec_submit.approval_mode="approve"',
                     "-c", 'mcp_servers.agent_exec_scout.tools.agent_exec_cancel.approval_mode="approve"']
        return args + ["-"]
    if role.provider == "claude":
        args = [executable, "-p", "--output-format", "json", "--tools", "", "--safe-mode", "--permission-prompts", "none", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
        if role.model:
            args += ["--model", role.model]
        return args
    if role.provider == "grok":
        args = [executable, "--prompt-file", "/dev/stdin", "--output-format", "json", "--tools", "", "--deny", "*", "--no-subagents", "--disable-web-search", "--permission-mode", "dontAsk"]
        if role.model:
            args += ["--model", role.model]
        return args
    raise ValueError("Unsupported provider")


def parse_output(provider: str, text: str) -> tuple[str, str | None, str | None]:
    """Return output, provider session id, semantic error (never a verdict)."""
    if provider == "command":
        return text.strip(), None, None
    output = []
    session_id = None
    semantic_error = None
    try:
        document = json.loads(text)
    except ValueError:
        document = None
    lines = [text] if isinstance(document, dict) else text.splitlines()
    for line in lines:
        try:
            value = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict):
            continue
        if provider == "codex":
            if value.get("type") == "thread.started":
                session_id = value.get("thread_id")
            if value.get("type") in {"turn.failed", "error"}:
                semantic_error = "Provider reported a failed turn; inspect stderr/events"
            item = value.get("item")
            if value.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message":
                if isinstance(item.get("text"), str):
                    output.append(item["text"])
        elif provider in {"claude", "grok"}:
            session_id = value.get("session_id", value.get("sessionId", session_id))
            if value.get("is_error"):
                semantic_error = "Provider reported is_error; inspect its result"
            if isinstance(value.get("result"), str):
                output.append(value["result"])
            elif provider == "grok" and isinstance(value.get("text"), str):
                output.append(value["text"])
            if provider == "grok" and value.get("stopReason") not in {None, "end_turn", "stop_sequence"}:
                semantic_error = "Grok did not finish a normal final turn"
    return "\n\n".join(output), session_id, semantic_error

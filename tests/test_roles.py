import sys

import pytest

from agent_exec.config import ALIASES, Settings, default_roles
from agent_exec.providers import command


def test_default_roles_have_expected_effective_configuration():
    roles = default_roles()

    assert roles["codex-scout"].model == "gpt-6-luna"
    assert roles["codex-scout"].effort == "medium"
    assert roles["codex-reviewer"].model == "gpt-6.1-sol"
    assert roles["codex-reviewer"].effort == "high"
    assert roles["codex-worker"].model == "gpt-6.1-sol"
    assert roles["codex-worker"].effort == "medium"
    assert roles["codex-worker"].access == "worktree"
    assert roles["codex-gate"].model == "gpt-6-luna"
    assert roles["codex-gate"].effort == "medium"
    assert roles["codex-gate"].access == "read-only"
    assert roles["codex-gate"].kind == "gate"
    assert "PASS" in roles["codex-gate"].description
    assert "RECHECK_SOL" in roles["codex-gate"].description
    assert "without running tests, builds, or servers" in roles["codex-gate"].description
    assert roles["codex-debug"].model == "gpt-6.1-sol"
    assert roles["codex-debug"].effort == "xhigh"


def test_gate_alias_is_distinct_from_reviewer():
    assert ALIASES["gate"] == "codex-gate"
    assert ALIASES["critic"] == "codex-reviewer"
    assert ALIASES["gate"] != ALIASES["critic"]


@pytest.mark.parametrize(
    ("name", "model", "effort", "sandbox"),
    [
        ("codex-scout", "gpt-6-luna", "medium", "read-only"),
        ("codex-reviewer", "gpt-6.1-sol", "high", "read-only"),
        ("codex-worker", "gpt-6.1-sol", "medium", "workspace-write"),
        ("codex-gate", "gpt-6-luna", "medium", "read-only"),
        ("codex-debug", "gpt-6.1-sol", "xhigh", "workspace-write"),
    ],
)
def test_codex_role_command_uses_model_effort_and_sandbox(tmp_path, name, model, effort, sandbox):
    settings = Settings()
    settings.executables["codex"] = sys.executable

    args = command(settings, default_roles()[name], tmp_path)

    assert args[0] == sys.executable
    assert args[args.index("--sandbox") + 1] == sandbox
    assert args[args.index("--model") + 1] == model
    assert f'model_reasoning_effort="{effort}"' in args


def test_delegated_scout_command_forwards_only_scoped_child_context(monkeypatch, tmp_path):
    fixture_token = "fixture-scoped-token-must-not-appear-in-argv"
    monkeypatch.setenv("AGENT_EXEC_SCOUT_TOKEN", fixture_token)
    settings = Settings()
    settings.executables["codex"] = sys.executable

    delegated = command(settings, default_roles()["codex-scout"], tmp_path, delegate_scout=True)
    nondelegating = command(settings, default_roles()["codex-scout"], tmp_path)

    assert 'mcp_servers.agent_exec_scout.env_vars=["AGENT_EXEC_CHILD", "AGENT_EXEC_SCOUT_TOKEN", "AGENT_EXEC_URL", "PYTHONPATH"]' in delegated
    assert 'mcp_servers.agent_exec_scout.env.AGENT_EXEC_CHILD="1"' in delegated
    assert not any("mcp_servers.agent_exec_scout.env_vars" in argument for argument in nondelegating)
    assert not any("mcp_servers.agent_exec_scout.env.AGENT_EXEC_CHILD" in argument for argument in nondelegating)
    assert fixture_token not in delegated
    assert "features.multi_agent=false" in delegated
    assert "features.multi_agent_v2=false" in delegated

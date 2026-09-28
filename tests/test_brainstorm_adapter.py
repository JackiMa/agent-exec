from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_exec.integrations.brainstorm import BrainstormProvider


class FakeClient:
    def __init__(self, *, status: str = "succeeded", output: str = "answer", timeout: bool = False) -> None:
        self.status = status
        self.output = output
        self.timeout = timeout
        self.submissions: list[dict[str, Any]] = []
        self.waited: list[tuple[str, float | None]] = []
        self.cancelled: list[str] = []
        self.resulted: list[str] = []

    def submit(self, payload: dict[str, Any], idempotency_key: str | None = None) -> dict[str, str]:
        assert idempotency_key is None
        self.submissions.append(payload)
        return {"id": f"run-{len(self.submissions)}"}

    def wait(self, run_id: str, timeout: float | None = None, poll_interval: float = 0.25) -> dict[str, str]:
        self.waited.append((run_id, timeout))
        if self.timeout:
            raise TimeoutError(run_id)
        return {"id": run_id, "status": self.status, "error": "executor failed"}

    def result(self, run_id: str) -> dict[str, str]:
        self.resulted.append(run_id)
        return {"output": self.output}

    def cancel(self, run_id: str) -> dict[str, str]:
        self.cancelled.append(run_id)
        return {"id": run_id, "status": "cancelled"}


def test_complete_submits_one_run_and_returns_only_succeeded_output(tmp_path: Path) -> None:
    client = FakeClient()
    observed: list[str] = []
    provider = BrainstormProvider(
        client,
        "brainstorm-workspace",
        "codex-scout",
        expected_cwd=tmp_path,
        caller_task_id="council-42",
        on_run=observed.append,
    )

    assert provider.complete("inspect this", timeout=12, cwd=tmp_path) == "answer"
    assert client.submissions == [
        {
            "workspace": "brainstorm-workspace",
            "goal": "inspect this",
            "role": "codex-scout",
            "timeout_seconds": 12,
            "caller_task_id": "council-42",
        }
    ]
    assert client.waited == [("run-1", 12)]
    assert client.resulted == ["run-1"]
    assert client.cancelled == []
    assert observed == ["run-1"]


@pytest.mark.parametrize("status", ["failed", "cancelled", "timed_out", "interrupted"])
def test_complete_rejects_every_non_succeeded_terminal_state(status: str) -> None:
    client = FakeClient(status=status)
    provider = BrainstormProvider(client, "workspace", "claude-chat")

    with pytest.raises(RuntimeError, match=status):
        provider.complete("work", timeout=3)
    assert client.resulted == []


def test_wait_timeout_cancels_only_its_submitted_run() -> None:
    client = FakeClient(timeout=True)
    provider = BrainstormProvider(client, "workspace", "grok-chat")

    with pytest.raises(TimeoutError):
        provider.complete("work", timeout=1)
    assert client.cancelled == ["run-1"]
    assert client.resulted == []


def test_supplied_cwd_requires_explicit_matching_configuration(tmp_path: Path) -> None:
    client = FakeClient()
    unconfigured = BrainstormProvider(client, "workspace", "codex-scout")
    with pytest.raises(ValueError, match="expected_cwd"):
        unconfigured.complete("work", timeout=1, cwd=tmp_path)
    assert client.submissions == []

    configured = BrainstormProvider(client, "workspace", "codex-scout", expected_cwd=tmp_path)
    with pytest.raises(ValueError, match="expected_cwd"):
        configured.complete("work", timeout=1, cwd=tmp_path / "other")
    assert client.submissions == []


def test_prompt_only_explicitly_accepts_ephemeral_council_cwd(tmp_path: Path) -> None:
    client = FakeClient()
    provider = BrainstormProvider(
        client, "brainstorm-scratch", "codex-scout", prompt_only=True
    )

    assert provider.complete("self-contained prompt", timeout=2, cwd=tmp_path / "seat") == "answer"
    assert client.submissions[0]["workspace"] == "brainstorm-scratch"
    assert "cwd" not in client.submissions[0]

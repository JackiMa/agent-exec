"""Adapter from Brainstorm's completion protocol to agent-exec runs.

The adapter intentionally depends only on the small synchronous Client-shaped
protocol below.  This keeps Brainstorm optional and prevents this package from
importing, configuring, or writing to the Brainstorm project.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


class AgentExecClient(Protocol):
    """The subset of :class:`agent_exec.client.Client` used by this adapter."""

    def submit(
        self, payload: Mapping[str, Any], idempotency_key: str | None = None
    ) -> Mapping[str, Any]: ...

    def wait(
        self, run_id: str, timeout: float | None = None, poll_interval: float = 0.25
    ) -> Mapping[str, Any]: ...

    def result(self, run_id: str) -> Mapping[str, Any]: ...

    def cancel(self, run_id: str) -> Mapping[str, Any]: ...


class BrainstormProvider:
    """Submit one Brainstorm completion as one owned agent-exec run.

    Each call owns only its locally returned run ID.  In particular, an
    adapter wait timeout cancels that run and never attempts to discover or
    cancel another caller's work.
    """

    def __init__(
        self,
        client: AgentExecClient,
        workspace_id: str,
        role: str,
        *,
        expected_cwd: str | Path | None = None,
        prompt_only: bool = False,
        caller_task_id: str | None = None,
        on_run: Callable[[str], None] | None = None,
    ) -> None:
        if not workspace_id:
            raise ValueError("workspace_id must be a registered workspace ID")
        if not role:
            raise ValueError("role must not be empty")
        self.client = client
        self.workspace_id = workspace_id
        self.role = role
        self.name = f"agent-exec:{role}"
        self.expected_cwd = Path(expected_cwd).resolve() if expected_cwd is not None else None
        self.prompt_only = prompt_only
        self.caller_task_id = caller_task_id
        self._on_run = on_run

    def _validate_cwd(self, cwd: str | Path | None) -> None:
        if self.prompt_only:
            # Brainstorm Council creates ephemeral per-seat directories.  This
            # explicit mode accepts that hint but never transfers it to the
            # service: prompts must therefore be self-contained.
            return
        if cwd is None:
            return
        if self.expected_cwd is None or Path(cwd).resolve() != self.expected_cwd:
            raise ValueError("Brainstorm provider cwd must equal its configured expected_cwd")

    def complete(self, prompt: str, timeout: int, cwd: str | Path | None = None) -> str:
        """Return executor output only after the submitted run succeeds."""
        self._validate_cwd(cwd)
        payload: dict[str, Any] = {
            "workspace": self.workspace_id,
            "goal": prompt,
            "role": self.role,
            "timeout_seconds": timeout,
        }
        if self.caller_task_id is not None:
            payload["caller_task_id"] = self.caller_task_id

        submitted = self.client.submit(payload)
        run_id = submitted.get("id")
        if not isinstance(run_id, str) or not run_id:
            raise RuntimeError("agent-exec submit did not return a run ID")
        if self._on_run is not None:
            self._on_run(run_id)

        try:
            run = self.client.wait(run_id, timeout=timeout)
        except TimeoutError:
            # Preserve the owned wait timeout even if a concurrent terminal
            # transition makes cancellation unavailable at that instant.
            try:
                self.client.cancel(run_id)
            except Exception:  # noqa: BLE001 - the original timeout is authoritative
                pass
            raise

        status = run.get("status")
        if status != "succeeded":
            detail = run.get("error")
            suffix = f": {detail}" if isinstance(detail, str) and detail else ""
            raise RuntimeError(f"agent-exec run {run_id} ended with {status!r}{suffix}")

        result = self.client.result(run_id)
        output = result.get("output")
        if not isinstance(output, str):
            raise RuntimeError(f"agent-exec run {run_id} returned no text output")
        return output

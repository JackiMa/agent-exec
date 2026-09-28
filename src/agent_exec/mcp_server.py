"""MCP stdio proxy for an already-running agent-exec HTTP service."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .client import Client

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
SUBMIT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
CANCEL = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)


def _run_payload(
    workspace: str,
    goal: str,
    role: str,
    timeout_seconds: int | None,
    caller_task_id: str | None,
    context: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"workspace": workspace, "goal": goal, "role": role}
    if timeout_seconds is not None:
        payload["timeout_seconds"] = timeout_seconds
    if caller_task_id is not None:
        payload["caller_task_id"] = caller_task_id
    if context is not None:
        payload["context"] = context
    return payload


def build_mcp(
    *,
    base_url: str | None = None,
    token: str | None = None,
    token_file: str | Path | None = None,
    client_factory: Callable[..., Client] = Client,
) -> FastMCP:
    """Build an MCP server whose tools proxy the HTTP API."""

    server = FastMCP(
        "agent-exec",
        instructions="Proxy tools for the existing local agent-exec service.",
        log_level="WARNING",
    )

    def call(method: str, *args: Any, **kwargs: Any) -> Any:
        with client_factory(base_url=base_url, token=token, token_file=token_file) as client:
            return getattr(client, method)(*args, **kwargs)

    @server.tool(
        name="agent_exec_capabilities",
        description="List configured roles, workspaces, and execution limits.",
        annotations=READ_ONLY,
    )
    def capabilities() -> dict[str, Any]:
        return call("capabilities")

    @server.tool(
        name="agent_exec_plan",
        description="Validate and preview a run without executing a provider.",
        annotations=READ_ONLY,
    )
    def plan(
        workspace: str,
        goal: str,
        role: str = "codex-scout",
        timeout_seconds: int | None = None,
        caller_task_id: str | None = None,
        context: str | None = None,
    ) -> dict[str, Any]:
        return call(
            "plan",
            _run_payload(workspace, goal, role, timeout_seconds, caller_task_id, context),
        )

    @server.tool(
        name="agent_exec_submit",
        description="Submit a run and return its run ID and initial status promptly.",
        annotations=SUBMIT,
    )
    def submit(
        workspace: str,
        goal: str,
        role: str = "codex-scout",
        timeout_seconds: int | None = None,
        caller_task_id: str | None = None,
        context: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return call(
            "submit",
            _run_payload(workspace, goal, role, timeout_seconds, caller_task_id, context),
            idempotency_key=idempotency_key,
        )

    @server.tool(
        name="agent_exec_status",
        description="Get the current state of one run.",
        annotations=READ_ONLY,
    )
    def status(run_id: str) -> dict[str, Any]:
        return call("get", run_id)

    @server.tool(
        name="agent_exec_events",
        description="Poll bounded run events after a cursor.",
        annotations=READ_ONLY,
    )
    def events(run_id: str, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        return call("events", run_id, after=after, limit=limit)

    @server.tool(
        name="agent_exec_result",
        description="Read the bounded executor output for a run.",
        annotations=READ_ONLY,
    )
    def result(run_id: str) -> dict[str, Any]:
        return call("result", run_id)

    @server.tool(name="agent_exec_diff", description="Read the frozen patch of a finished writing run; does not apply it.", annotations=READ_ONLY)
    def diff(run_id: str) -> dict[str, Any]:
        return call("diff", run_id)

    @server.tool(
        name="agent_exec_cancel",
        description="Request cancellation of a queued or running run.",
        annotations=CANCEL,
    )
    def cancel(run_id: str) -> dict[str, Any]:
        return call("cancel", run_id)

    return server


def run_mcp(
    *,
    base_url: str | None = None,
    token: str | None = None,
    token_file: str | Path | None = None,
) -> None:
    build_mcp(base_url=base_url, token=token, token_file=token_file).run(transport="stdio")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-exec mcp")
    parser.add_argument("--url")
    parser.add_argument("--token-file")
    args = parser.parse_args(argv)
    run_mcp(base_url=args.url, token_file=args.token_file)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

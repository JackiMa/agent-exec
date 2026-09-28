"""Command-line interface for the agent-exec service and legacy commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .client import Client, ClientError


NEW_COMMANDS = frozenset({"capabilities", "plan", "task", "mcp", "serve"})
FAILURE_STATUSES = frozenset({"failed", "cancelled", "timed_out", "interrupted"})


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _read_text(value: str | None, filename: str | None, label: str) -> str:
    if value is not None:
        return value
    if filename is None:
        raise ValueError(f"{label} is required")
    return Path(filename).read_text(encoding="utf-8")


def _add_payload_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", required=True, help="registered workspace ID")
    parser.add_argument("--role", default="codex-scout")
    goal = parser.add_mutually_exclusive_group(required=True)
    goal.add_argument("--goal")
    goal.add_argument("--goal-file")
    parser.add_argument("--timeout", type=int, dest="timeout_seconds")
    parser.add_argument("--caller-task-id")
    parser.add_argument("--context")


def _payload(args: argparse.Namespace) -> dict[str, Any]:
    value: dict[str, Any] = {
        "workspace": args.workspace,
        "role": args.role,
        "goal": _read_text(args.goal, args.goal_file, "goal"),
    }
    if args.timeout_seconds is not None:
        value["timeout_seconds"] = args.timeout_seconds
    if args.caller_task_id is not None:
        value["caller_task_id"] = args.caller_task_id
    if args.context is not None:
        value["context"] = args.context
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-exec")
    parser.add_argument("--url", help="service URL (default: AGENT_EXEC_URL or loopback)")
    parser.add_argument("--token-file", help="bearer token file")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("capabilities", help="show configured service capabilities")

    plan = commands.add_parser("plan", help="validate and preview a run")
    _add_payload_arguments(plan)

    serve = commands.add_parser("serve", help="run the local HTTP service")
    serve.add_argument("--config", help="service configuration file")

    commands.add_parser("mcp", help="run the MCP stdio proxy")

    task = commands.add_parser("task", help="manage service runs")
    tasks = task.add_subparsers(dest="task_command", required=True)

    submit = tasks.add_parser("submit")
    _add_payload_arguments(submit)
    submit.add_argument("--idempotency-key")
    submit.add_argument("--wait", action="store_true")
    submit.add_argument("--wait-timeout", type=float)
    submit.add_argument("--poll-interval", type=float, default=0.25)

    listing = tasks.add_parser("list")
    listing.add_argument("--limit", type=int, default=100)

    show = tasks.add_parser("show")
    show.add_argument("run_id")

    wait = tasks.add_parser("wait")
    wait.add_argument("run_id")
    wait.add_argument("--timeout", type=float)
    wait.add_argument("--poll-interval", type=float, default=0.25)

    result = tasks.add_parser("result")
    result.add_argument("run_id")

    diff = tasks.add_parser("diff")
    diff.add_argument("run_id")

    events = tasks.add_parser("events")
    events.add_argument("run_id")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--limit", type=int, default=200)

    logs = tasks.add_parser("logs")
    logs.add_argument("run_id")
    logs.add_argument("--stream", choices=("stdout", "stderr"), default="stdout")
    logs.add_argument("--offset", type=int, default=0)
    logs.add_argument("--limit", type=int, default=65536)

    cancel = tasks.add_parser("cancel")
    cancel.add_argument("run_id")

    retry = tasks.add_parser("retry")
    retry.add_argument("run_id")

    verdict = tasks.add_parser("verdict")
    verdict.add_argument("run_id")
    decision = verdict.add_mutually_exclusive_group(required=True)
    decision.add_argument("--accept", action="store_true")
    decision.add_argument("--reject", action="store_true")
    verdict.add_argument("--reason", required=True)
    verdict.add_argument("--evidence", action="append", required=True)
    return parser


def _command_position(argv: Sequence[str]) -> int | None:
    index = 0
    while index < len(argv):
        item = argv[index]
        if item in {"--url", "--token-file"}:
            index += 2
            continue
        if item.startswith("--url=") or item.startswith("--token-file="):
            index += 1
            continue
        if item.startswith("-"):
            return None
        return index
    return None


def _legacy(argv: list[str]) -> int:
    from .legacy import main as legacy_main

    result = legacy_main(argv)
    return int(result) if result is not None else 0


def _run_task(client: Client, args: argparse.Namespace) -> int:
    command = args.task_command
    if command == "submit":
        run = client.submit(_payload(args), idempotency_key=args.idempotency_key)
        if args.wait:
            run = client.wait(run["id"], timeout=args.wait_timeout, poll_interval=args.poll_interval)
        _print_json(run)
        return 1 if args.wait and run.get("status") in FAILURE_STATUSES else 0
    if command == "list":
        value = client.list_runs(args.limit)
    elif command == "show":
        value = client.get(args.run_id)
    elif command == "wait":
        value = client.wait(args.run_id, timeout=args.timeout, poll_interval=args.poll_interval)
    elif command == "result":
        value = client.result(args.run_id)
    elif command == "diff":
        value = client.diff(args.run_id)
    elif command == "events":
        value = client.events(args.run_id, after=args.after, limit=args.limit)
    elif command == "logs":
        value = client.logs(args.run_id, stream=args.stream, offset=args.offset, limit=args.limit)
    elif command == "cancel":
        value = client.cancel(args.run_id)
    elif command == "retry":
        value = client.retry(args.run_id)
    elif command == "verdict":
        value = client.verdict(
            args.run_id,
            accepted=args.accept,
            reason=args.reason,
            evidence=args.evidence,
        )
    else:  # pragma: no cover - argparse prevents this
        raise AssertionError(command)
    _print_json(value)
    if command == "wait" and value.get("status") in FAILURE_STATUSES:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    position = _command_position(raw_argv)
    if position is not None:
        command = raw_argv[position]
        if command == "legacy":
            return _legacy(raw_argv[position + 1 :])
        if command not in NEW_COMMANDS:
            return _legacy(raw_argv)

    args = _build_parser().parse_args(raw_argv)
    if args.command == "serve":
        from .server import serve

        serve(args.config)
        return 0
    if args.command == "mcp":
        from .mcp_server import run_mcp

        run_mcp(base_url=args.url, token_file=args.token_file)
        return 0

    try:
        with Client(base_url=args.url, token_file=args.token_file) as client:
            if args.command == "capabilities":
                _print_json(client.capabilities())
                return 0
            if args.command == "plan":
                _print_json(client.plan(_payload(args)))
                return 0
            return _run_task(client, args)
    except (ClientError, TimeoutError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

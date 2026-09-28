from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class ApiHandler(BaseHTTPRequestHandler):
    token = "mcp-test-token"
    requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _json(self, status: int, body: Any) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") != f"Bearer {self.token}":
            self._json(401, {"detail": "invalid bearer token"})
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        self.requests.append(("GET", self.path, None))
        if self.path == "/v1/capabilities":
            self._json(
                200,
                {
                    "roles": [{"name": "codex-scout", "access": "read-only"}],
                    "workspaces": [{"id": "fixture", "allow_write": False}],
                    "limits": {"max_concurrency": 1},
                },
            )
            return
        if self.path == "/v1/runs/abc":
            self._json(200, {"id": "abc", "status": "succeeded"})
            return
        self._json(404, {"detail": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.requests.append(("POST", self.path, body))
        if self.path == "/v1/runs":
            self._json(202, {"id": "abc", "status": "queued", "role": body["role"]})
            return
        self._json(404, {"detail": "not found"})


def test_actual_mcp_stdio_initialize_list_and_tool_calls(tmp_path: Path) -> None:
    ApiHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    token_file = tmp_path / "service.token"
    token_file.write_text(ApiHandler.token + "\n", encoding="utf-8")
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    env = dict(os.environ)
    env.pop("AGENT_EXEC_CHILD", None)
    env["PYTHONPATH"] = source_root

    async def scenario() -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "agent_exec.mcp_server",
                "--url",
                f"http://127.0.0.1:{server.server_port}",
                "--token-file",
                str(token_file),
            ],
            env=env,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "agent-exec"
                tools = await session.list_tools()
                hints = {tool.name: tool.annotations for tool in tools.tools}
                assert hints["agent_exec_capabilities"].readOnlyHint is True
                assert hints["agent_exec_submit"].readOnlyHint is False
                assert hints["agent_exec_cancel"].destructiveHint is True
                assert {tool.name for tool in tools.tools} == {
                    "agent_exec_capabilities",
                    "agent_exec_plan",
                    "agent_exec_submit",
                    "agent_exec_status",
                    "agent_exec_events",
                    "agent_exec_result",
                    "agent_exec_diff",
                    "agent_exec_cancel",
                }
                capabilities = await session.call_tool("agent_exec_capabilities")
                assert capabilities.isError is False
                assert capabilities.structuredContent["roles"][0]["name"] == "codex-scout"
                submitted = await session.call_tool(
                    "agent_exec_submit",
                    {"workspace": "fixture", "goal": "inspect", "role": "codex-scout"},
                )
                assert submitted.isError is False
                assert submitted.structuredContent["id"] == "abc"
                status = await session.call_tool("agent_exec_status", {"run_id": "abc"})
                assert status.structuredContent["status"] == "succeeded"

    try:
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert ("GET", "/v1/capabilities", None) in ApiHandler.requests
    assert ("GET", "/v1/runs/abc", None) in ApiHandler.requests
    submission = next(item for item in ApiHandler.requests if item[:2] == ("POST", "/v1/runs"))
    assert submission[2] == {"workspace": "fixture", "goal": "inspect", "role": "codex-scout"}

"""Actual stdio MCP protocol with a fixture HTTP transport, no TCP sockets."""
import asyncio
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


FIXTURE_SERVER = r'''
import os
if os.environ.get("AGENT_EXEC_TEST_SELECTOR_POLL") == "1":
    import selectors
    original = selectors.EpollSelector.select
    selectors.EpollSelector.select = lambda self, timeout=None: original(self, .02 if timeout is None else min(timeout, .02))
import httpx
from agent_exec.client import Client
from agent_exec.mcp_server import build_mcp
child = os.environ.get("AGENT_EXEC_CHILD") == "1"
prefix = "/v1/scouts" if child else "/v1"
def handler(request):
    if request.headers.get("authorization") != "Bearer fixture-scout-token":
        return httpx.Response(401, json={"detail": "incorrect fixture credential"})
    path = request.url.path
    if path == prefix + "/capabilities":
        return httpx.Response(200, json={"roles": [{"name": "codex-scout", "access": "read-only"}], "workspaces": [{"id": "fixture", "allow_write": False}]})
    if path == prefix + "/runs" and request.method == "POST":
        import json
        payload = json.loads(request.content)
        return httpx.Response(202, json={"id": "fixture-run", "role": payload["role"], "status": "queued", "scoped_path": child})
    if path == prefix + "/runs/fixture-run":
        return httpx.Response(200, json={"id": "fixture-run", "status": "succeeded", "scoped_path": child})
    return httpx.Response(404, json={"detail": "unexpected fixture path " + path})
def factory(**kwargs):
    return Client(transport=httpx.MockTransport(handler), **kwargs)
build_mcp(token="owner-token-ignored" if child else "fixture-scout-token", client_factory=factory).run(transport="stdio")
'''


@pytest.mark.parametrize("child", [False, True])
def test_stdio_mcp_owner_and_scoped_child_without_tcp(child):
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(root / "src"))
    env.pop("AGENT_EXEC_CHILD", None)
    env.pop("AGENT_EXEC_SCOUT_TOKEN", None)
    if child:
        env.update(AGENT_EXEC_CHILD="1", AGENT_EXEC_SCOUT_TOKEN="fixture-scout-token")

    async def scenario():
        parameters = StdioServerParameters(command=sys.executable, args=["-u", "-c", FIXTURE_SERVER], env=env, cwd=str(root))
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "agent-exec"
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                assert ("agent_exec_diff" in tools) is not child
                assert tools["agent_exec_submit"].annotations.readOnlyHint is False
                assert tools["agent_exec_cancel"].annotations.destructiveHint is True
                caps = await session.call_tool("agent_exec_capabilities")
                assert not caps.isError
                assert caps.structuredContent["roles"][0]["name"] == "codex-scout"
                submission = await session.call_tool("agent_exec_submit", {"workspace": "fixture", "goal": "inspect", "role": "codex-scout"})
                assert not submission.isError
                assert submission.structuredContent["scoped_path"] is child
                status = await session.call_tool("agent_exec_status", {"run_id": "fixture-run"})
                assert status.structuredContent["status"] == "succeeded"
                if child:
                    rejected = await session.call_tool("agent_exec_submit", {"workspace": "fixture", "goal": "write", "role": "codex-worker"})
                    assert rejected.isError
                    assert "only scouts" in str(rejected.content)

    asyncio.run(asyncio.wait_for(scenario(), timeout=20))

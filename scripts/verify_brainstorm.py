#!/usr/bin/env python3
"""Exercise the installed Brainstorm Council through a temporary real HTTP service.

All model processes are deterministic fixtures. Production sessions and services
are untouched. The artifact directory is retained for inspection.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time

import uvicorn

from agent_exec.client import Client
from agent_exec.config import Role, Settings, Workspace
from agent_exec.core import Service
from agent_exec.integrations.brainstorm import BrainstormProvider
from agent_exec.server import create_app


def verify(brainstorm_root: Path, artifact: Path) -> dict:
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(brainstorm_root))
    os.environ["BRAINSTORM_ALLOW_ROOT"] = "1"
    from brainstorm import api

    artifact.mkdir(parents=True, exist_ok=True)
    scratch = artifact / "scratch"
    scratch.mkdir(exist_ok=True)
    independent = "## 主张\n冻结快照\n## 证据\nfixture\n## 反证\n保留\n## 未知\n未知\n## 建议尝试\n验证\n"
    chair = "## 共识\n最小尝试\n## 分歧\n仍未知\n## 未知\n未知\n## 建议尝试\n测试\n## 杀招\n停止\n"
    code = "import sys; p=sys.stdin.read(); print(" + repr(chair) + " if '保留分歧，不要和稀泥' in p else " + repr(independent) + ")"
    service = Service(Settings(state_dir=artifact / "state", workspaces={"scratch": Workspace(scratch)}, roles={"fixture": Role("command", argv=(sys.executable, "-c", code))}, max_concurrency=3))
    service.start()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(service, token="isolated-fixture-token"), log_level="error"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    run_ids = []
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            if time.monotonic() >= deadline:
                raise RuntimeError("fixture HTTP service did not start")
            time.sleep(.02)
        with Client(base_url=f"http://127.0.0.1:{port}", token="isolated-fixture-token") as client:
            root = artifact / "brainstorm"
            session = api.create_session("agent-exec Council integration fixture", root)
            providers = {seat: BrainstormProvider(client, "scratch", "fixture", prompt_only=True, caller_task_id="brainstorm:" + session["id"], on_run=run_ids.append) for seat in ("codex", "claude", "grok")}
            status = api.start_council(root, session["id"], providers=providers, sync=True, request_id="agent-exec-fixture-1")
            assert status["state"] == "done", status
            assert len(run_ids) == 7, run_ids
            assert all(client.get(r)["status"] == "succeeded" for r in run_ids)
            human, agent = api.emit(root, session["id"])
            assert human.is_file() and agent.is_file()
            report = {"status": "passed", "real_models": False, "brainstorm_root": str(brainstorm_root), "artifact_directory": str(artifact), "council_status": status, "execution_count": len(run_ids), "run_ids": run_ids, "human": str(human), "agent": str(agent), "public_api_writes_only": True}
            (artifact / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            return report
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        service.close()
        sock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--brainstorm-root", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path)
    args = parser.parse_args()
    artifact = args.artifact_dir or Path(tempfile.mkdtemp(prefix="agent-exec-brainstorm-"))
    print(json.dumps(verify(args.brainstorm_root.resolve(), artifact.resolve()), ensure_ascii=False, indent=2))

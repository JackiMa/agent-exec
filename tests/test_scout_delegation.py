"""End-to-end coverage for parent-scoped read-only scout delegation."""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
import httpx
from fastapi.testclient import TestClient

from agent_exec.client import Client, ClientError
from agent_exec.config import Role, Settings, Workspace, load_settings
from agent_exec.core import Service, ServiceError, TERMINAL
from agent_exec.server import create_app


def until(predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.025)
    raise AssertionError("condition did not become true")


def terminal(service, run):
    return until(lambda: (current if (current := service.get(run["id"]))["status"] in TERMINAL else None))


def fake_codex(path):
    """A provider-shaped executable: it consumes the prompt and emits Codex JSON."""
    script = path / "codex"
    script.write_text(
        "#!" + sys.executable + "\n"
        "import json, os, signal, time\n"
        "prompt = __import__('sys').stdin.read()\n"
        "if 'finish parent' in prompt: time.sleep(.3)\n"
        "if 'hold' in prompt:\n"
        "    signal.signal(signal.SIGTERM, lambda *_: __import__('sys').exit(0))\n"
        "    while True: time.sleep(.05)\n"
        "text = 'cwd=' + os.getcwd()\n"
        "if 'read-source' in prompt:\n"
        "    text += ';source=' + open('source.txt').read().strip()\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':text}}), flush=True)\n"
    )
    script.chmod(0o755)
    return script


@pytest.fixture
def scout_service(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(
        state_dir=tmp_path / "state",
        workspaces={"fixture": Workspace(workspace, True)},
        roles={"codex-scout": Role("codex", "fixture-scout", description="SCOUT DESCRIPTION MARKER")},
        executables={"codex": str(fake_codex(tmp_path)), "claude": "claude", "grok": "grok"},
        max_concurrency=1,
        max_scout_concurrency=1,
        max_scout_children=4,
        default_timeout_seconds=5,
        max_timeout_seconds=8,
        terminate_grace_seconds=.05,
    )
    service = Service(settings)
    service.start()
    yield service
    service.close()


def submit_parent(service, goal="hold parent", **extra):
    return service.submit({"workspace": "fixture", "role": "codex-scout", "goal": goal, **extra})


def prepared(service, run_id):
    run = service.get(run_id)
    return run if run["status"] == "running" and run.get("deadline_at") and run.get("argv") else None


def running_parent(service, goal="hold parent", **extra):
    run = submit_parent(service, goal, **extra)
    return until(lambda: prepared(service, run["id"]))


def test_root_capacity_does_not_block_scout_and_depth_slots_are_separate(scout_service):
    parent = running_parent(scout_service)
    child = scout_service.submit_scout(parent["id"], {"goal": "hold scout", "role": "scout"})
    child = until(lambda: prepared(scout_service, child["id"]))
    assert child["depth"] == 1 and child["root_run_id"] == parent["id"]
    assert child["deadline_at"] is not None
    scout_token = scout_service.scout_token(child["id"])
    grandchild = scout_service.submit_scout(child["id"], {"goal": "hold grandchild", "role": "research"})
    grandchild = until(lambda: prepared(scout_service, grandchild["id"]))
    assert grandchild["depth"] == 2 and grandchild["root_run_id"] == parent["id"]
    with pytest.raises(ServiceError, match="depth limit"):
        scout_service.scout_token(grandchild["id"])
    scout_service.cancel(parent["id"])
    assert terminal(scout_service, parent)["status"] == "cancelled"
    assert terminal(scout_service, child)["status"] == "cancelled"
    assert terminal(scout_service, grandchild)["status"] == "cancelled"
    with pytest.raises(ServiceError, match="expired"):
        scout_service.scout_parent(scout_token)


def test_default_capacity_reserves_ten_scout_slots_per_depth(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = tmp_path / "service.yaml"
    config.write_text("max_scout_concurrency: 10\n")
    assert load_settings(config).max_scout_concurrency == 10
    settings = Settings(
        state_dir=tmp_path / "state",
        workspaces={"fixture": Workspace(workspace, True)},
        roles={"codex-scout": Role("codex", "fixture-scout", description="SCOUT DESCRIPTION MARKER")},
        executables={"codex": str(fake_codex(tmp_path)), "claude": "claude", "grok": "grok"},
        max_concurrency=4,
        max_scout_concurrency=10,
        max_scout_children=10,
        default_timeout_seconds=20,
        max_timeout_seconds=20,
        terminate_grace_seconds=.05,
    )
    service = Service(settings)
    service.start()
    try:
        roots = [running_parent(service, f"hold root {number}") for number in range(4)]
        children = [service.submit_scout(roots[0]["id"], {"goal": f"hold scout {number}"}) for number in range(10)]
        with pytest.raises(ServiceError, match="quota"):
            service.submit_scout(roots[0]["id"], {"goal": "hold scout over quota"})

        def first_depth_ready():
            current = [service.get(child["id"]) for child in children]
            running = [run for run in current if prepared(service, run["id"])]
            return running if len(running) == 10 else None

        first_depth = until(first_depth_ready, timeout=15)
        grandchildren = [service.submit_scout(child["id"], {"goal": f"hold grandchild {number}"}) for number, child in enumerate(first_depth)]

        def second_depth_ready():
            current = [service.get(child["id"]) for child in grandchildren]
            running = [run for run in current if prepared(service, run["id"])]
            return running if len(running) == 10 else None

        second_depth = until(second_depth_ready, timeout=15)
        assert len([run for run in roots if prepared(service, run["id"])]) == 4
        assert len(first_depth) == 10
        assert len(second_depth) == 10
        assert service.capabilities()["limits"]["max_total_concurrency"] == 24

        for root in roots:
            service.cancel(root["id"])
        for run in [*roots, *children, *grandchildren]:
            assert terminal(service, run)["status"] == "cancelled"
    finally:
        service.close()


def test_scout_payload_is_fixed_to_role_workspace_and_parent(scout_service):
    parent = running_parent(scout_service)
    bad_payloads = [
        {"goal": "x", "role": "gate"},
        {"goal": "x", "workspace": "other"},
        {"goal": "x", "execution_cwd": "/tmp"},
        {"goal": "x", "parent_run_id": "f" * 32},
        {"goal": "x", "provider": "command"},
    ]
    for payload in bad_payloads:
        with pytest.raises(ServiceError):
            scout_service.submit_scout(parent["id"], payload)
    scout_service.settings.roles["codex-scout"] = Role("command", argv=(sys.executable, "-c", "print('bad')"))
    with pytest.raises(ServiceError, match="read-only Codex"):
        scout_service.plan_scout(parent["id"], {"goal": "x"})
    scout_service.cancel(parent["id"])


def test_scoped_http_only_exposes_direct_children_and_not_owner_api(scout_service):
    scout_service.settings.max_concurrency = 2
    first, second = running_parent(scout_service), running_parent(scout_service, "hold second")
    child = scout_service.submit_scout(first["id"], {"goal": "hold child"})
    child = until(lambda: scout_service.get(child["id"]) if scout_service.get(child["id"])["status"] == "running" else None)
    first_token, second_token = scout_service.scout_token(first["id"]), scout_service.scout_token(second["id"])
    with TestClient(create_app(scout_service, token="owner-token")) as client:
        scoped = {"Authorization": "Bearer " + first_token}
        assert client.get("/v1/scouts/runs/" + child["id"], headers=scoped).status_code == 200
        assert client.get("/v1/scouts/runs/" + second["id"], headers=scoped).status_code == 404
        assert client.post("/v1/scouts/runs/" + child["id"] + "/retry", headers=scoped).status_code == 404
        assert client.get("/v1/runs", headers=scoped).status_code == 401
        assert client.get("/v1/scouts/runs", headers={"Authorization": "Bearer owner-token"}).status_code == 401
        assert client.get("/v1/scouts/runs/" + child["id"], headers={"Authorization": "Bearer " + second_token}).status_code == 404
    scout_service.cancel(first["id"])
    scout_service.cancel(second["id"])


def test_scoped_idempotency_and_parent_quota(scout_service):
    scout_service.settings.max_concurrency = 2
    first, second = running_parent(scout_service), running_parent(scout_service, "hold second")
    one = scout_service.submit_scout(first["id"], {"goal": "a"}, "same")
    assert scout_service.submit_scout(first["id"], {"goal": "a"}, "same")["id"] == one["id"]
    two = scout_service.submit_scout(second["id"], {"goal": "a"}, "same")
    assert one["id"] != two["id"]
    # Replay does not consume quota; distinct submissions do.
    for number in range(3):
        scout_service.submit_scout(first["id"], {"goal": f"q{number}"})
    with pytest.raises(ServiceError, match="quota"):
        scout_service.submit_scout(first["id"], {"goal": "over"})
    scout_service.cancel(first["id"])
    scout_service.cancel(second["id"])


def test_provider_child_client_uses_only_scoped_credential(monkeypatch, tmp_path):
    owner_file = tmp_path / "owner.token"
    owner_file.write_text("owner-secret\n")
    monkeypatch.setenv("AGENT_EXEC_CHILD", "1")
    monkeypatch.setenv("AGENT_EXEC_SCOUT_TOKEN", "scoped-secret")
    seen = []

    def responder(request):
        seen.append((request.url.path, request.headers.get("authorization")))
        return httpx.Response(202, json={"id": "a" * 32, "status": "queued"})

    with Client(token_file=owner_file, transport=httpx.MockTransport(responder)) as client:
        assert client.submit({"workspace": "ignored", "goal": "inspect", "role": "research"})["status"] == "queued"
        with pytest.raises(ClientError, match="only scouts"):
            client.submit({"workspace": "ignored", "goal": "write", "role": "gate"})
        with pytest.raises(ClientError, match="cannot retry"):
            client.retry("a" * 32)
    assert seen == [("/v1/scouts/runs", "Bearer scoped-secret")]


def test_parent_finish_timeout_cascades_and_never_leaves_orphan(scout_service):
    parent = submit_parent(scout_service, "finish parent")
    parent = until(lambda: prepared(scout_service, parent["id"]))
    child = scout_service.submit_scout(parent["id"], {"goal": "hold child"})
    assert terminal(scout_service, parent)["status"] == "succeeded"
    assert terminal(scout_service, child)["status"] == "cancelled"
    timed = running_parent(scout_service, "hold timeout", timeout_seconds=1)
    timed_child = scout_service.submit_scout(timed["id"], {"goal": "hold child"})
    assert terminal(scout_service, timed)["status"] == "timed_out"
    assert terminal(scout_service, timed_child)["status"] in {"cancelled", "timed_out"}


def test_scout_uses_parent_worktree_cwd_and_secrets_stay_out_of_state(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for command in (("init", "-q", "-b", "main"), ("config", "user.name", "Test"), ("config", "user.email", "test@example.invalid")):
        subprocess.run(["git", "-C", str(repo), *command], check=True, capture_output=True)
    (repo / "source.txt").write_text("original\n")
    subprocess.run(["git", "-C", str(repo), "add", "source.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
    settings = Settings(state_dir=tmp_path / "state", workspaces={"fixture": Workspace(repo, True)},
        roles={"codex-scout": Role("codex", description="ROLE DESCRIPTION SECRET-MARKER"), "parent-worker": Role("codex", access="worktree", description="WRITER ROLE DESCRIPTION")},
        executables={"codex": str(fake_codex(tmp_path)), "claude": "claude", "grok": "grok"},
        max_concurrency=1, max_scout_concurrency=1, default_timeout_seconds=3, max_timeout_seconds=5, terminate_grace_seconds=.05)
    service = Service(settings)
    service.start()
    try:
        parent = service.submit({"workspace": "fixture", "role": "parent-worker", "goal": "hold worktree"})
        parent = until(lambda: prepared(service, parent["id"]))
        worktree = Path(parent["execution_cwd"])
        assert worktree != repo
        (worktree / "source.txt").write_text("uncommitted-parent\n")
        token = service.scout_token(parent["id"])
        child = service.submit_scout(parent["id"], {"goal": "read-source"})
        child = terminal(service, child)
        assert child["status"] == "succeeded"
        assert "cwd=" + str(worktree) in service.result(child["id"])["output"]
        assert "source=uncommitted-parent" in service.result(child["id"])["output"]
        persisted = (settings.state_dir / "state.sqlite3").read_bytes()
        event_data = json.dumps(service.events(parent["id"])).encode()
        assert token.encode() not in persisted and token.encode() not in event_data
        assert (repo / "source.txt").read_text() == "original\n"
        assert "ROLE DESCRIPTION SECRET-MARKER" in (settings.state_dir / "runs" / child["id"] / "prompt.txt").read_text()
    finally:
        service.cancel(parent["id"]) if 'parent' in locals() else None
        service.close()

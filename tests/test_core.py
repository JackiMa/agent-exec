import json
import os
import signal
import subprocess
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_exec.config import Role, Settings, Workspace, load_settings
from agent_exec.core import Service, ServiceError, TERMINAL
from agent_exec.providers import parse_output, command
from agent_exec.server import create_app


def until(fn, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.025)
    raise AssertionError("condition did not become true")


def finished(service, run):
    return until(lambda: (r if (r := service.get(run["id"]))["status"] in TERMINAL else None))


def command_role(code, access="read-only"):
    return Role("command", access=access, argv=(sys.executable, "-c", code))


@pytest.fixture
def settings(tmp_path):
    work = tmp_path / "workspace"
    work.mkdir()
    return Settings(state_dir=tmp_path / "state", workspaces={"test": Workspace(work, True)}, roles={"ok": command_role("print('answer')")}, max_concurrency=1, terminate_grace_seconds=0.1)


@pytest.fixture
def service(settings):
    svc = Service(settings)
    svc.start()
    yield svc
    svc.close()


def payload(role="ok", **extra):
    return {"workspace": "test", "role": role, "goal": "bounded fixture", **extra}


def git_repo(path):
    for argv in (["init", "-q", "-b", "main"], ["config", "user.name", "Test"], ["config", "user.email", "test@example.invalid"]):
        subprocess.run(["git", "-C", str(path), *argv], check=True, capture_output=True)
    (path / "source.txt").write_text("original\n")
    subprocess.run(["git", "-C", str(path), "add", "source.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "fixture"], check=True)


def test_execution_is_not_acceptance_and_verdict_needs_evidence(service):
    run = finished(service, service.submit(payload()))
    assert run["status"] == "succeeded" and run["acceptance"] == "pending"
    assert service.result(run["id"])["output"] == "answer"
    with pytest.raises(ServiceError, match="evidence"):
        service.verdict(run["id"], True, "checked", [])
    assert service.verdict(run["id"], True, "checked output", ["test log"])["acceptance"] == "accepted"
    events = service.events(run["id"])
    assert events[-1]["type"] == "run.verdict"
    assert service.events(run["id"], after=events[-1]["id"]) == []


@pytest.mark.parametrize("ending,status,code", [("raise SystemExit(7)", "failed", 7), ("import time; time.sleep(15)", "timed_out", None)])
def test_success_receipt_cannot_override_supervisor(service, ending, status, code):
    service.settings.roles["fake"] = command_role("from pathlib import Path; Path('receipt.json').write_text('{\"status\":\"success\"}'); print('SUCCESS',flush=True); " + ending)
    run = finished(service, service.submit(payload("fake", timeout_seconds=1)))
    assert run["status"] == status and run["acceptance"] == "pending"
    if code is not None:
        assert run["exit_code"] == code
    with pytest.raises(ServiceError):
        service.verdict(run["id"], True, "model said success", ["receipt.json"])


def test_empty_final_answer_and_provider_semantic_failure(service):
    service.settings.roles["empty"] = command_role("pass")
    run = finished(service, service.submit(payload("empty")))
    assert run["status"] == "failed" and "no final answer" in run["error"]
    text = '\n'.join([json.dumps({"type": "thread.started", "thread_id": "provider-123"}), json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}), json.dumps({"type": "turn.failed"})])
    answer, sid, error = parse_output("codex", text)
    assert answer == "done" and sid == "provider-123" and error
    assert parse_output("claude", '{"is_error":true,"result":"bad"}')[2]
    output, sid, error = parse_output("grok", json.dumps({"text": "GROK_OK", "sessionId": "grok-id", "stopReason": "end_turn"}, indent=2))
    assert (output, sid, error) == ("GROK_OK", "grok-id", None)
    assert parse_output("grok", '{"text":"partial","stopReason":"max_tokens"}')[2]


def test_codex_override_does_not_create_invalid_missing_mcp_transport(settings, tmp_path, monkeypatch):
    config_home = tmp_path / "codex-home"
    config_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(config_home))
    settings.executables["codex"] = sys.executable
    args = command(settings, Role("codex"), tmp_path)
    assert "mcp_servers.agent_exec.enabled=false" not in args
    (config_home / "config.toml").write_text('[mcp_servers.agent_exec]\ncommand="agent-exec"\n')
    args = command(settings, Role("codex"), tmp_path)
    assert "mcp_servers.agent_exec.enabled=false" in args


def test_concurrent_idempotency_conflict_and_replay_after_workspace_loss(service):
    with ThreadPoolExecutor(max_workers=8) as pool:
        runs = list(pool.map(lambda _: service.submit(payload(), "same-request"), range(12)))
    assert len({r["id"] for r in runs}) == 1
    finished(service, runs[0])
    with pytest.raises(ServiceError) as exc:
        service.submit(payload(goal="different"), "same-request")
    assert exc.value.status_code == 409
    service.settings.workspaces["test"].path.rmdir()
    assert service.submit(payload(), "same-request")["id"] == runs[0]["id"]


def test_cancel_queued_and_running_and_retry(service):
    service.settings.roles["slow"] = command_role("import time; print('ready',flush=True); time.sleep(30)")
    active = service.submit(payload("slow"))
    until(lambda: service.get(active["id"]).get("process_identity"))
    queued = service.submit(payload())
    assert service.cancel(queued["id"])["status"] == "cancelled"
    service.cancel(active["id"])
    assert finished(service, active)["status"] == "cancelled"
    again = finished(service, service.retry(queued["id"]))
    assert again["status"] == "succeeded" and again["retry_of"] == queued["id"]
    assert service.cancel(again["id"])["status"] == "succeeded"


def test_retry_refuses_silent_role_change(service):
    old = finished(service, service.submit(payload()))
    service.settings.roles["ok"] = command_role("print('changed')")
    with pytest.raises(ServiceError, match="changed"):
        service.retry(old["id"])


def test_slow_admission_does_not_block_cancel_or_supervision(service, monkeypatch):
    service.settings.roles["slow"] = command_role("import time; time.sleep(30)")
    run = service.submit(payload("slow"))
    until(lambda: service.get(run["id"]).get("process_identity"))
    entered, release = threading.Event(), threading.Event()
    original = service._validate
    def validate(p):
        entered.set()
        release.wait(3)
        return original(p)
    monkeypatch.setattr(service, "_validate", validate)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(service.submit, payload())
        assert entered.wait(2)
        start = time.monotonic()
        service.cancel(run["id"])
        elapsed = time.monotonic() - start
        release.set()
        pending.result(timeout=3)
        assert elapsed < .5
    assert finished(service, run)["status"] == "cancelled"


def test_queue_capacity_and_unknown_fields(service):
    service.settings.max_pending = 1
    service.settings.roles["slow"] = command_role("import time; time.sleep(30)")
    run = service.submit(payload("slow"))
    with pytest.raises(ServiceError) as exc:
        service.submit(payload())
    assert exc.value.status_code == 429
    for bad in (payload(argv=["rm"]), payload(timeout_seconds=True), payload(workspace="../"), payload(role=["ok"])):
        with pytest.raises(ServiceError):
            service.plan(bad)
    service.cancel(run["id"])


def test_output_limit_is_bounded_and_failure_retained(service):
    service.settings.max_output_bytes = 2048
    service.settings.roles["large"] = command_role("print('x'*1000000)")
    run = finished(service, service.submit(payload("large")))
    assert run["status"] == "failed" and run["output_truncated"]
    path = service.settings.state_dir / "runs" / run["id"] / "stdout.log"
    assert path.stat().st_size == 2048
    chunk = service.logs(run["id"], limit=123)
    assert len(chunk["text"]) == 123 and chunk["next_offset"] == 123 and chunk["truncated"]
    with pytest.raises(ServiceError):
        service.logs(run["id"], stream="../../service.token")


def test_patch_pipe_read_deadline_and_overflow(service, tmp_path):
    marker = tmp_path / "patch-child.pid"
    code = f"import os,time; from pathlib import Path; Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(30)"
    with pytest.raises(RuntimeError, match="deadline"):
        service._capture_patch_command([sys.executable, "-c", code], 1024, time.monotonic() + .3)
    assert not alive(int(marker.read_text()))
    with pytest.raises(RuntimeError, match="limit"):
        service._capture_patch_command([sys.executable, "-c", "print('x'*10000)"], 128, time.monotonic() + 3)


def test_logs_are_available_before_provider_exit(service):
    service.settings.roles["slow"] = command_role("import time; print('live progress',flush=True); time.sleep(30)")
    run = service.submit(payload("slow"))
    until(lambda: "live progress" in service.logs(run["id"])["text"])
    assert service.get(run["id"])["status"] == "running"
    service.cancel(run["id"])


def test_worktree_isolation_dirty_refusal_and_permission(service):
    work = service.settings.workspaces["test"].path
    git_repo(work)
    service.settings.roles["writer"] = command_role("from pathlib import Path; Path('source.txt').write_text('changed'); Path('new.txt').write_text('new'); print('patch ready')", "worktree")
    (work / "dirty.txt").write_text("precious")
    with pytest.raises(ServiceError, match="clean"):
        service.submit(payload("writer"))
    assert (work / "dirty.txt").read_text() == "precious"
    (work / "dirty.txt").unlink()
    run = finished(service, service.submit(payload("writer")))
    assert run["status"] == "succeeded", run
    assert (work / "source.txt").read_text() == "original\n"
    assert (Path(run["execution_cwd"]) / "source.txt").read_text() == "changed"
    assert run["base_revision"]
    exported = service.diff(run["id"])
    assert "source.txt" in exported["patch"] and "new.txt" in exported["patch"]
    assert exported["sha256"] == run["patch_sha256"] and not exported["auto_adopted"]
    service.settings.workspaces["test"] = Workspace(work, False)
    with pytest.raises(ServiceError) as exc:
        service.submit(payload("writer"))
    assert exc.value.status_code == 403


def test_exclusive_service_lock_and_durable_restart(settings):
    first = Service(settings)
    first.start()
    try:
        with pytest.raises(ServiceError, match="Another"):
            Service(settings).start()
        run = finished(first, first.submit(payload()))
    finally:
        first.close()
    second = Service(settings)
    second.start()
    try:
        assert second.get(run["id"])["status"] == "succeeded"
        assert second.result(run["id"])["output"] == "answer"
    finally:
        second.close()


def alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def test_crash_recovery_kills_own_process_group(settings, tmp_path):
    # Abruptly kill only this test's owner process, exercising PR_SET_PDEATHSIG.
    script = tmp_path / "owner.py"
    pidfile = tmp_path / "descendant.pid"
    runfile = tmp_path / "run.id"
    child_code = f"import subprocess,time; from pathlib import Path; p=subprocess.Popen(['sleep','60']); Path({str(pidfile)!r}).write_text(str(p.pid)); time.sleep(60)"
    script.write_text(f'''import time
from pathlib import Path
from agent_exec.core import Service
from agent_exec.config import Settings, Workspace, Role
s=Service(Settings(state_dir=Path({str(settings.state_dir)!r}),workspaces={{"test":Workspace(Path({str(settings.workspaces['test'].path)!r}))}},roles={{"slow":Role("command",argv=({sys.executable!r},"-c",{child_code!r}))}}))
s.start()
r=s.submit({{"workspace":"test","role":"slow","goal":"fixture"}})
Path({str(runfile)!r}).write_text(r['id'])
time.sleep(60)
''')
    owner = subprocess.Popen([sys.executable, str(script)])
    try:
        until(pidfile.exists)
        descendant = int(pidfile.read_text())
        owner.kill()
        owner.wait(timeout=5)
        until(lambda: not alive(descendant))
        replacement = Service(settings)
        replacement.start()
        try:
            recovered = replacement.get(runfile.read_text())
            assert recovered["status"] == "interrupted" and recovered["acceptance"] == "pending"
        finally:
            replacement.close()
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)


def test_actual_core_http_contract(service):
    with TestClient(create_app(service, token="test-token")) as http:
        assert http.get("/v1/capabilities").status_code == 401
        assert set(http.get("/healthz").json()) == {"status", "version"}
        headers = {"Authorization": "Bearer test-token"}
        assert http.post("/v1/runs", headers=headers, content="{").status_code == 422
        run = http.post("/v1/runs", headers=headers, json=payload()).json()
        finished(service, run)
        result = http.get(f"/v1/runs/{run['id']}/result", headers=headers).json()
        assert result["output"] == "answer"
        assert http.post(f"/v1/runs/{run['id']}/verdict", headers=headers, json={"accepted": True, "reason": "ok", "evidence": []}).status_code == 422


def test_config_rejects_request_level_shell_and_nonabsolute_workspace(tmp_path):
    conf = tmp_path / "service.yaml"
    conf.write_text("workspaces:\n  bad:\n    path: relative\n")
    with pytest.raises(ValueError, match="absolute"):
        load_settings(conf)
    conf.write_text("roles:\n  bad:\n    provider: command\n    argv: shell string\n")
    with pytest.raises(ValueError, match="argv"):
        load_settings(conf)

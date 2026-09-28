from __future__ import annotations

import json
import sys
import types
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_exec import cli
from agent_exec.client import Client, ClientError
from agent_exec.server import _check_bind_auth, create_app


TOKEN = "transport-test-token"


class FakeServiceError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class FakeService:
    def __init__(self) -> None:
        self.submissions: list[tuple[dict[str, Any], str | None]] = []

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "version": "0.1.0",
            "instance_id": "private-instance",
            "counts": {"running": 1},
        }

    def capabilities(self) -> dict[str, Any]:
        return {"roles": [], "workspaces": [], "limits": {}}

    def submit(self, payload: dict[str, Any], idempotency_key: str | None = None) -> dict[str, Any]:
        self.submissions.append((payload, idempotency_key))
        return {"id": "a" * 32, "status": "queued"}

    def plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"workspace": payload["workspace"], "argv": ["[configured executable]"]}

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        return [{"id": "a" * 32, "limit": limit}]

    def get(self, run_id: str) -> dict[str, Any]:
        if run_id == "missing":
            raise FakeServiceError(404, "unknown run")
        return {"id": run_id, "status": "succeeded"}

    def events(self, run_id: str, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        return [{"id": after + 1, "run_id": run_id, "limit": limit}]

    def logs(self, run_id: str, stream: str, offset: int, limit: int) -> dict[str, Any]:
        return {"text": "fixture", "offset": offset, "next_offset": offset + 7, "truncated": False}

    def result(self, run_id: str) -> dict[str, Any]:
        return {"run": self.get(run_id), "output": "fixture", "truncated": False}

    def cancel(self, run_id: str) -> dict[str, Any]:
        return {"id": run_id, "status": "cancelled"}

    def retry(self, run_id: str) -> dict[str, Any]:
        return {"id": "b" * 32, "retry_of": run_id, "status": "queued"}

    def verdict(self, run_id: str, accepted: bool, reason: str, evidence: list[str]) -> dict[str, Any]:
        return {"id": run_id, "acceptance": "accepted" if accepted else "rejected"}


@pytest.fixture
def service() -> FakeService:
    return FakeService()


@pytest.fixture
def api(service: FakeService) -> Iterator[TestClient]:
    with TestClient(create_app(service, token=TOKEN, max_body_bytes=256)) as client:
        yield client


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_public_health_is_minimal_and_docs_are_disabled(api: TestClient) -> None:
    response = api.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0"}
    assert api.get("/docs").status_code == 404
    assert api.get("/openapi.json").status_code == 404


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/v1/health"),
        ("get", "/v1/capabilities"),
        ("get", "/v1/runs"),
        ("post", "/v1/plan"),
    ],
)
def test_v1_rejects_missing_and_incorrect_auth(api: TestClient, method: str, path: str) -> None:
    request = getattr(api, method)
    assert request(path).status_code == 401
    response = request(path, headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
def test_unconfigured_auth_rejects_v1_but_keeps_minimal_health(service: FakeService) -> None:
    with TestClient(create_app(service)) as api:
        assert api.get("/healthz").status_code == 200
        assert api.get("/v1/health", headers={"Authorization": "Bearer anything"}).status_code == 503


def test_submit_maps_idempotency_and_service_errors(api: TestClient, service: FakeService) -> None:
    response = api.post(
        "/v1/runs",
        headers={**auth(), "Idempotency-Key": "same-run"},
        json={"workspace": "fixture", "goal": "test"},
    )
    assert response.status_code == 202
    assert service.submissions == [({"workspace": "fixture", "goal": "test"}, "same-run")]

    missing = api.get("/v1/runs/missing", headers=auth())
    assert missing.status_code == 404
    assert missing.json() == {"detail": "unknown run"}


def test_request_body_limit_uses_declared_and_streamed_sizes(api: TestClient) -> None:
    declared = api.post("/v1/plan", headers=auth(), content=b"x" * 257)
    assert declared.status_code == 413
    assert declared.json() == {"detail": "request body too large"}

    def chunks() -> Iterator[bytes]:
        yield b"x" * 200
        yield b"y" * 100

    streamed = api.post("/v1/plan", headers=auth(), content=chunks())
    assert streamed.status_code == 413


def test_no_cors_middleware_is_installed(api: TestClient) -> None:
    response = api.options(
        "/v1/health",
        headers={"Origin": "https://untrusted.example", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in response.headers


def test_remote_bind_requires_strong_token() -> None:
    _check_bind_auth("127.0.0.1", None)
    with pytest.raises(RuntimeError, match="non-loopback"):
        _check_bind_auth("0.0.0.0", None)
    with pytest.raises(RuntimeError, match="at least 32"):
        _check_bind_auth("192.168.1.5", "short")
    _check_bind_auth("::", "x" * 32)


def test_client_defaults_env_auth_maps_requests_and_redacts_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/missing"):
            return httpx.Response(404, json={"detail": f"bad secret {TOKEN}"})
        return httpx.Response(200, json={"roles": [], "workspaces": [], "limits": {}})

    monkeypatch.setenv("AGENT_EXEC_URL", "http://127.0.0.1:7777/")
    monkeypatch.setenv("AGENT_EXEC_TOKEN", TOKEN)
    with Client(transport=httpx.MockTransport(handler)) as client:
        assert client.capabilities()["roles"] == []
        with pytest.raises(ClientError) as error:
            client.get("missing")
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(error.value)
    assert error.value.status_code == 404


def test_wait_returns_terminal_without_cancelling_on_caller_timeout() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        return httpx.Response(200, json={"id": "run", "status": "running"})

    with Client(token=TOKEN, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TimeoutError):
            client.wait("run", timeout=0, poll_interval=0.001)
    assert requests == ["GET"]


def test_child_can_inspect_but_cannot_submit_or_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json={"id": "run", "status": "succeeded"})

    monkeypatch.setenv("AGENT_EXEC_CHILD", "1")
    with Client(token=TOKEN, transport=httpx.MockTransport(handler)) as client:
        assert client.get("run")["status"] == "succeeded"
        with pytest.raises(ClientError, match="cannot submit"):
            client.submit({"workspace": "w", "goal": "g"})
        with pytest.raises(ClientError, match="cannot retry"):
            client.retry("run")
    assert calls == ["GET"]


def test_cli_wait_returns_nonzero_for_failed_run(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    class FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *_args: Any) -> None:
            pass

        def wait(self, run_id: str, timeout: float | None, poll_interval: float) -> dict[str, Any]:
            return {"id": run_id, "status": "failed", "exit_code": 7}

    monkeypatch.setattr(cli, "Client", FakeClient)
    assert cli.main(["task", "wait", "run-7", "--timeout", "1"]) == 1
    assert json.loads(capsys.readouterr().out)["exit_code"] == 7


def test_cli_forwards_old_root_commands_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[list[str]] = []
    legacy = types.ModuleType("agent_exec.legacy")
    legacy.main = lambda argv: received.append(argv) or 9  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent_exec.legacy", legacy)
    original = ["dispatch", "codex-scout", "do work"]
    assert cli.main(original) == 9
    assert received == [original]


def test_cli_explicit_legacy_removes_only_the_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[list[str]] = []
    legacy = types.ModuleType("agent_exec.legacy")
    legacy.main = lambda argv: received.append(argv) or 0  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent_exec.legacy", legacy)
    assert cli.main(["legacy", "check", "--no-probe"]) == 0
    assert received == [["check", "--no-probe"]]

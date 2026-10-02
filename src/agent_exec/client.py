"""Synchronous Python client for the agent-exec HTTP API."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Mapping

import httpx


DEFAULT_URL = "http://127.0.0.1:9891"
DEFAULT_TOKEN_FILE = Path("~/.config/agent-exec/service.token")
TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}
)


class ClientError(RuntimeError):
    """A redacted transport or API error."""

    def __init__(self, detail: str, *, status_code: int | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _load_token(token: str | None, token_file: str | Path | None) -> str | None:
    if os.environ.get("AGENT_EXEC_CHILD") == "1":
        # Never fall back to the owner's token file inside a provider process.
        return os.environ.get("AGENT_EXEC_SCOUT_TOKEN") or None
    if token is not None:
        return token.strip() or None
    environment_token = os.environ.get("AGENT_EXEC_TOKEN")
    if environment_token:
        return environment_token.strip() or None
    selected_file = token_file or os.environ.get("AGENT_EXEC_TOKEN_FILE") or DEFAULT_TOKEN_FILE
    try:
        value = Path(selected_file).expanduser().read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return value or None


def _reject_child_execution(operation: str) -> None:
    if os.environ.get("AGENT_EXEC_CHILD") == "1":
        raise ClientError(
            f"agent-exec provider children cannot {operation} runs; use the owning host process"
        )


class Client:
    """Small, blocking API client suitable for CLI and MCP proxy use."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        token_file: str | Path | None = None,
        timeout: float = 30.0,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("AGENT_EXEC_URL") or DEFAULT_URL).rstrip("/")
        self.child_mode = os.environ.get("AGENT_EXEC_CHILD") == "1"
        self.token = _load_token(token, token_file)
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self._http = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
            trust_env=False,
            transport=transport,
        )

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _redact(self, value: str) -> str:
        value = value.replace("\r", " ").replace("\n", " ")[:1000]
        if self.token:
            value = value.replace(self.token, "[REDACTED]")
        return value

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if self.child_mode:
            if not self.token:
                raise ClientError("Provider child has no scout delegation credential", status_code=403)
            path = path.replace("/v1/", "/v1/scouts/", 1)
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ClientError(f"agent-exec request failed: {self._redact(str(exc))}") from exc

        if not response.is_success:
            detail = f"agent-exec API returned HTTP {response.status_code}"
            try:
                payload = response.json()
                candidate = payload.get("detail") if isinstance(payload, dict) else None
                if isinstance(candidate, str) and candidate:
                    detail = candidate
            except ValueError:
                pass
            raise ClientError(self._redact(detail), status_code=response.status_code)
        try:
            return response.json()
        except ValueError as exc:
            raise ClientError("agent-exec API returned invalid JSON", status_code=response.status_code) from exc

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/v1/health")

    def capabilities(self) -> dict[str, Any]:
        return self._request("GET", "/v1/capabilities")

    def submit(
        self,
        payload: Mapping[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if self.child_mode:
            if not self.token:
                raise ClientError("Provider children cannot submit without a scout delegation credential", status_code=403)
            if payload.get("role", "codex-scout") not in {"codex-scout", "scout", "research"}:
                raise ClientError("Provider children may submit only scouts", status_code=403)
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self._request("POST", "/v1/runs", json=dict(payload), headers=headers)

    def plan(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/plan", json=dict(payload))

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/runs", params={"limit": limit})

    def get(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/runs/{run_id}")

    def events(self, run_id: str, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            f"/v1/runs/{run_id}/events",
            params={"after": after, "limit": limit},
        )

    def logs(
        self,
        run_id: str,
        stream: str = "stdout",
        offset: int = 0,
        limit: int = 65536,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/runs/{run_id}/logs",
            params={"stream": stream, "offset": offset, "limit": limit},
        )

    def result(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/runs/{run_id}/result")

    def diff(self, run_id: str) -> dict[str, Any]:
        _reject_child_execution("export writing diffs for")
        return self._request("GET", f"/v1/runs/{run_id}/diff")

    def cancel(self, run_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/runs/{run_id}/cancel")

    def retry(self, run_id: str) -> dict[str, Any]:
        _reject_child_execution("retry")
        return self._request("POST", f"/v1/runs/{run_id}/retry")

    def verdict(
        self,
        run_id: str,
        accepted: bool,
        reason: str,
        evidence: list[str],
    ) -> dict[str, Any]:
        _reject_child_execution("accept or reject")
        return self._request(
            "POST",
            f"/v1/runs/{run_id}/verdict",
            json={"accepted": accepted, "reason": reason, "evidence": evidence},
        )

    def wait(
        self,
        run_id: str,
        timeout: float | None = None,
        poll_interval: float = 0.25,
    ) -> dict[str, Any]:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be nonnegative")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            run = self.get(run_id)
            if run.get("status") in TERMINAL_STATUSES:
                return run
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"timed out waiting for agent-exec run {run_id}")
                time.sleep(min(poll_interval, remaining))
            else:
                time.sleep(poll_interval)

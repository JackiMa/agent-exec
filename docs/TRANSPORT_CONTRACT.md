# Internal transport contract (v1)

Owner implements src/agent_exec/config.py, core.py, providers.py. Transport owns server.py, client.py, cli.py, mcp_server.py and corresponding tests. Legacy extraction owns legacy.py, _legacy.py and legacy assets/tests.

## Python service
`from agent_exec.config import Settings, load_settings`
`load_settings(path: str | Path | None = None) -> Settings`.
Settings attributes: host (default 127.0.0.1), port (9891), token_file (Path), state_dir (Path).
Default configuration: $AGENT_EXEC_SERVICE_CONFIG or ~/.config/agent-exec/service.yaml.
Service local state defaults ~/.local/state/agent-exec, separate from legacy ~/.codex-worker.
`from agent_exec.core import Service, ServiceError`
`Service(settings)`; `start()` acquires an exclusive state-dir service lock, recovers interrupted runs and starts queue. `close()` cancels its own workers and releases lock.
ServiceError has .status_code (HTTP equivalent) and .detail string.

All methods synchronous; run execution occurs in service-owned threads. Use threadpool in async handlers.
- health() -> {status, version, instance_id, counts}
- capabilities() -> {roles: [{name,provider,model,effort,access,kind}], workspaces: [{id,allow_write}], limits: {...}}
- submit(payload: dict, idempotency_key: str | None = None) -> run dict
  Payload: workspace (registered ID), goal (1..100000 chars), role (default codex-scout), timeout_seconds (positive int capped by settings), caller_task_id (optional <=256 chars), context (optional <=100000 chars).
  Unknown fields rejected. Same key+same JSON body (key order ignored) returns same run even if the workspace becomes unavailable; different bodies conflict.
- plan(payload: dict) -> {role,provider,model,access,workspace,base_revision,argv,...} no model/worktree execution, redacted command preview.
- list_runs(limit: int = 100) -> list[run dict], max 200.
- get(run_id) -> run dict, 404 for unknown ID.
- events(run_id, after: int = 0, limit: int = 200) -> list[{id,run_id,type,time,data}], global increasing IDs; no SSE required; poll cursor, clamp limit.
- logs(run_id, stream: str = "stdout", offset: int = 0, limit: int = 65536) -> {text,offset,next_offset,truncated}; bounded byte ranges, only stdout/stderr accepted.
- result(run_id) -> {run: run dict, output: str, truncated: bool}, output capped and only executor artifact, never interpreted as trusted verdict.
- diff(run_id) -> bounded frozen patch, SHA256, base revision, lossless patch_base64. Includes untracked files; does not touch the index or adopt changes.
- cancel(run_id) -> run dict; queued becomes cancelled, running sets cancel_requested then process-group cleanup; terminal idempotent.
- retry(run_id) -> new run dict (terminal only); explicit, never automatic. Record retry_of. Changed role, executable or workspace mapping requires a new submission. Writing retries retain the original base revision.
- verdict(run_id, accepted: bool, reason: str, evidence: list[str]) -> run dict. Only succeeded can be accepted, failed can be rejected. accepted requires nonempty reason and evidence. No claim of independent identity: API client owns judgment.
Run: id, status, acceptance (pending/accepted/rejected), role, workspace, created_at, updated_at, started_at, finished_at, exit_code, error, cancel_requested, caller_task_id, retry_of, execution_cwd, base_revision, provider_session_id, evidence/reason, parent_run_id, root_run_id, depth, deadline_at. States queued/running/succeeded/failed/cancelled/timed_out/interrupted. IDs opaque 32 lowercase hex.
No arbitrary argv/env/path in requests; only local config can define executable. No external auto-merge/adopt/delete API.

## HTTP
Public GET /healthz returns ONLY {status,version}; all /v1 endpoints require Authorization: Bearer token. Compare constant-time against token file (no token generation on request). Disable browser docs/OpenAPI or protect them; no CORS by default. Bound Content-Length/streaming body to ~256 KiB, reject oversized requests. Do not echo tokens/prompts in errors.
GET /v1/health, /v1/capabilities, /v1/runs?limit=...
POST /v1/runs (Idempotency-Key header) -> 202
POST /v1/plan
GET /v1/runs/{id}, /events?after=&limit=, /logs?stream=&offset=&limit=, /result, /diff
POST /v1/runs/{id}/cancel, /retry, /verdict {accepted,reason,evidence}
Parent-scoped `/v1/scouts` uses a separate short-lived credential issued to an active Codex run (never the owner bearer token). It exposes health/capabilities/plan, POST/GET runs, and direct-child status/events/logs/result/cancel. It only admits Codex read-only `codex-scout` (aliases scout/research), in the parent's registered workspace and actual execution cwd. No diff/verdict/retry endpoints. Tokens are in-memory, revoked when parent finishes/cancels, and absent from persisted run records. Idempotency is scoped to parent. Default max_scout_depth=2, max_scout_children=10 lifetime direct children per parent; ten additional shared execution slots per scout depth via max_scout_concurrency=10; a single parent can run ten scouts concurrently when that depth pool is available. Descendants inherit the parent's absolute deadline; finish/cancel/timeout requests cancellation of descendants. Process cleanup is asynchronous: a parent terminal state does not prove all descendants have exited; wait for their terminal states before treating cleanup as complete. Root max_concurrency=4 means default total execution ceiling=24. Global max_pending still applies; no guarantee of admission when the queue is full.
Server CLI: agent-exec serve [--config PATH] (agent-execd same main). Refuse non-loopback if token absent/too short. Explicit host=0.0.0.0 supports direct trusted-LAN access and existing loopback clients; clients use the server LAN IP. Use TLS/SSH outside trusted networks. No automatic firewall or router port forwarding.

## Client and CLI
Client(base_url=None, token=None, token_file=None, timeout=...) defaults AGENT_EXEC_URL http://127.0.0.1:9891 and AGENT_EXEC_TOKEN or AGENT_EXEC_TOKEN_FILE or ~/.config/agent-exec/service.token. Use httpx trust_env=False for loopback/private API to avoid leaking token to configured global proxies. Client errors useful but redacted.
Expose methods mapping to service plus wait(run_id, timeout, poll_interval). Timeout of caller MUST NOT silently cancel execution; explicit cancel; adapter may deliberately cancel when owned deadline exhausted.
agent-exec task submit --workspace ID --role ROLE [--goal TEXT | --goal-file PATH] [--timeout SECONDS] [--caller-task-id ID] [--idempotency-key KEY] [--wait]; task list/show/wait/result/events/logs/cancel/retry/verdict; agent-exec capabilities; agent-exec plan; agent-exec mcp; agent-exec serve.
Do not collide with old commands start/dispatch/list/wait/result/check/...; delegate old root commands and explicit `legacy ...` to `agent_exec.legacy.main(argv)`, implemented by extraction worker.
New task submit --wait returns nonzero for failed/cancelled/timed_out/interrupted. JSON stdout. File logs never stdout of MCP process.
`agent-exec install-service` is not in transport scope; owner adds installer script.

## MCP
Use official mcp FastMCP stdio; proxy Client, never instantiate another scheduler.
Tools agent_exec_capabilities, agent_exec_plan, agent_exec_submit, agent_exec_status, agent_exec_events, agent_exec_result, agent_exec_diff, agent_exec_cancel. Bounded waits optional; return run ID promptly. No arbitrary command execution, no acceptance tool (judgment remains owner).
Use MCP package protocol via SDK. Test initialize/tools/list/tools/call through actual stdio client as well as HTTP.
Child Codex per-run configuration mounts `agent_exec_scout` using the same stdio proxy and scoped environment credential. The child tool list omits diff. Native multi_agent remains disabled; server-side scope checks are authoritative for role/run access. CLI/Python Client automatically use `/v1/scouts` when AGENT_EXEC_CHILD=1 and ignore owner token arguments/files/environment.

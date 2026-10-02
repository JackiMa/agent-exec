---
name: agent-exec
description: Delegate durable agent jobs through the local agent-exec HTTP or MCP service, inspect results and patches, and retain host ownership of acceptance. Use for external or cross-provider execution and agent-exec debugging.
---

# agent-exec host orchestration

`agent-exec` runs bounded local tasks.  The host owns decomposition, task
acceptance, and any integration with native Codex or Claude features.  It is a
stdio MCP server for those hosts; it does not replace a vendor binary.

## Preconditions and task identity

Use only a registered workspace ID from `agent-exec capabilities`.  Record a
durable host task ID and pass it as `caller_task_id` on every submitted task so
the host task and service run remain traceable.  A service run reaching
`succeeded` proves execution only; the host must inspect evidence and make its
own acceptance decision.

Every brief must state the goal, in-scope paths, out-of-scope paths, done
criteria, and exact checks.  Keep credentials, prompts, and machine-specific
configuration out of source control.

The Brainstorm adapter is strict by default: a supplied cwd must equal its
configured `expected_cwd`.  Its explicit `prompt_only=True` mode accepts a
caller cwd as an execution hint without transferring it to agent-exec; use it
for Council's ephemeral seat directories only when the prompt is
self-contained and does not depend on files in that directory.

## Decompose work

At low or medium effort, work solo unless the user explicitly requests
delegation.  At high effort or above, dispatch independent slices:

- scouts for read-only discovery, logs, and repository mapping;
- settled workers for a specified edit and its local check;
- reviewers for independent evidence or diff review.
- gates for lightweight completion checks: inspect claimed files and saved
  test/experiment evidence; return PASS or RECHECK_SOL for a direct contradiction.
  Gates do not rerun tests/builds/servers or redesign the solution.

Codex jobs may delegate read-only `codex-scout` jobs through their scoped
`agent_exec_scout` MCP or the task CLI. A scout can delegate another scout within
the configured depth limit (default two scout levels). Never delegate workers,
debuggers, gates or reviewers from a child. Each child inherits the actual parent
execution directory, including a parent's isolated worktree, and the parent's
deadline. Child credentials only authorize that parent's direct scouts; they
cannot accept results, access unrelated runs or invoke the owner API. Inspect
scout evidence before returning; finishing the parent cancels unfinished scouts.

Default models: scout/gate `gpt-6-luna` at medium; worker `gpt-6.1-sol` at medium;
reviewer `gpt-6.1-sol` at high; debug `gpt-6.1-sol` at xhigh. `gate` maps to
`codex-gate`; `critic` maps to `codex-reviewer`.

A worker must not
commit, merge, rebase, stash, reset, or clean.  Writing workers need their own
worktree and must leave every out-of-scope file exactly as found.  Provider
sandboxing and worktrees constrain a run; they do not make a host prompt or a
same-user process a complete security boundary.

## Commands

Use the new task namespace for service work:

```sh
agent-exec task submit --workspace REGISTERED_ID --role codex-scout --goal 'map the module' --caller-task-id HOST_TASK_ID
agent-exec task list
agent-exec task show RUN_ID
agent-exec task wait RUN_ID
agent-exec task result RUN_ID
agent-exec task diff RUN_ID
agent-exec task events RUN_ID
agent-exec task logs RUN_ID
agent-exec task cancel RUN_ID
agent-exec task retry RUN_ID
agent-exec task verdict RUN_ID --accept --reason 'owner checked output' --evidence PATH_OR_URL
```

Older root commands (`start`, `dispatch`, `list`, `wait`, `result`, `check`,
and related legacy commands) retain their legacy behavior.  Use
`agent-exec legacy ...` to select that interface explicitly; do not treat it
as the new durable task API.

## MCP configuration

Use the supplied [Codex template](../../examples/codex-mcp.toml) or
[Claude template](../../examples/claude-mcp.json). Both start the `agent-exec mcp` stdio proxy and reference a token
file with `AGENT_EXEC_TOKEN_FILE`; neither contains a token nor replaces the
Codex or Claude executable.

Codex headless delegation needs the template's explicit per-tool approval for
submit/cancel. Query tools are annotated read-only. If host policy rejects a
mutation, report that policy result; do not relabel the tool as read-only or
disable the global sandbox. The local installed configuration grants only this
service's submit/cancel operations; the service still checks its role/workspace registry.

For Codex integration and native-thread differences, read [Codex integration](../../docs/CODEX_INTEGRATION.md). The service defaults to 24 total execution slots (4 root + 10 per scout depth) and up to 10 lifetime direct scouts per parent. Service run IDs are not native Codex agent IDs.

# Implementation plan

1. Inventory and preserve the original Claude-era implementation, its role roster and collaboration contract.
2. Extract portable source, templates and migration provenance into this independent repository.
3. Implement one durable service with a SQLite task/run/event store, bounded concurrency, cancellation, restart recovery and distinct execution/acceptance state.
4. Expose the same operations through CLI, authenticated HTTP and MCP. Add trace/artifact inspection and reproducible provider fixtures.
5. Split task kind, capability, provider/model and permission policy; retain legacy role aliases.
6. Add explicit workspace registration and isolated writing worktrees. Do not equate prompt instructions or a same-user process with a security sandbox.
7. Provide tested host adapters for brainstorm, Claude and Codex; document native-agent features that remain host-owned.
8. Install a user service, exercise a real read-only provider job, verify HTTP/MCP/client behavior and push a private GitHub repository unless the user requests public.

Review: consult GPT Pro using a redacted architecture/evidence packet; incorporate correctness findings.

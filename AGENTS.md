# agent-exec

The service owns execution state; hosts own task decomposition and acceptance.
Preserve existing user work, credentials, provider sessions and running services.
Read-only discovery uses scouts. Writers use isolated Git worktrees after dirty-file backup.
Workers never commit, stash, reset, clean, merge or modify another checkout.
Execution success is based on supervisor-observed exit, not model-written receipts.
Keep prompts, logs, credentials, databases and machine-specific configuration out of Git.
Run `uv run pytest` for implementation changes. Report fixture vs real-provider evidence separately.

# Legacy agent-exec compatibility capture

This directory captures the configuration tables and collaboration contract used
by the pre-service `agent-exec` runner. `src/agent_exec/_legacy.py` is an
unchanged byte-for-byte copy of that runner; `agent_exec.legacy.main(argv)`
executes it in a fresh Python process.

The legacy runner deliberately retains its historical defaults:

- job state: `~/.codex-worker`
- tables: `~/.config/agent-exec/roles.yaml` and `backends.yaml`

Set `CODEX_WORKER_HOME` and `AGENT_EXEC_CONFIG` to use an isolated legacy
environment. The new service has separate state and configuration as specified
in `docs/TRANSPORT_CONTRACT.md`.

`provenance.json` records the source runner digest and copied-asset origins.
The test fixture supplies a temporary HOME, job root, table directory, ledger,
and fake `codex` executable; it never reads or writes the host legacy state.

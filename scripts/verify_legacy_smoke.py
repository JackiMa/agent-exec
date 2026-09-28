#!/usr/bin/env python3
"""Run historical shell tests with a staged temporary HOME and fake Codex."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="agent-exec-legacy-home-") as directory:
        home = Path(directory)
        for folder in (".local/bin", ".config/agent-exec", ".claude/hooks", ".codex-worker/jobs", ".codex"):
            (home / folder).mkdir(parents=True)
        shutil.copy2(root / "src/agent_exec/_legacy.py", home / ".local/bin/agent-exec")
        (home / ".local/bin/codex-exec").symlink_to("agent-exec")
        shutil.copy2(root / "tests/legacy/oracle/bin/codex", home / ".local/bin/codex")
        for file in ("roles.yaml", "backends.yaml"):
            shutil.copy2(root / "legacy" / file, home / ".config/agent-exec" / file)
        for source in (root / "legacy/hooks").glob("*.py"):
            shutil.copy2(source, home / ".claude/hooks" / source.name)
        env = dict(os.environ, HOME=str(home), CODEX_HOME=str(home / ".codex"), CODEX_WORKER_HOME=str(home / ".codex-worker"), AGENT_EXEC_CONFIG=str(home / ".config/agent-exec"), AGENT_EXEC_LEDGER=str(home / "ledger.tsv"), AE=str(home / ".local/bin/agent-exec"))
        env.pop("CODEX_EXEC_JOB", None)
        env["PATH"] = str(home / ".local/bin") + os.pathsep + os.environ.get("PATH", os.defpath)
        return subprocess.run(["bash", str(root / "legacy/tests/smoke.sh")], env=env, cwd=home, timeout=120).returncode


if __name__ == "__main__":
    sys.exit(main())

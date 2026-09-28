#!/usr/bin/env python3
"""Install this checkout as a user service. Existing configuration is preserved."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time
import uuid

import yaml


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".agent-exec-new")
    temporary.write_text(text)
    temporary.chmod(mode)
    temporary.replace(path)


def install(root: Path, home: Path, start: bool) -> dict:
    python = root / ".venv/bin/python"
    if not python.exists():
        raise RuntimeError("Run uv sync in the project first")
    config_dir = home / ".config/agent-exec"
    state = home / ".local/state/agent-exec"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    backup = state / "install-backups" / (time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
    backup.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_dir.mkdir(parents=True, exist_ok=True)
    token = config_dir / "service.token"
    if not token.exists():
        atomic_write(token, secrets.token_urlsafe(48) + "\n")
    elif token.stat().st_mode & 0o077:
        raise RuntimeError("Existing service.token must have mode 0600")
    scratch = state / "workspaces/brainstorm-scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "service.yaml"
    if not config_path.exists():
        config = {"host": "127.0.0.1", "port": 9891, "state_dir": str(state), "token_file": str(token), "max_concurrency": 3, "max_pending": 100, "default_timeout_seconds": 300, "max_timeout_seconds": 1800, "max_output_bytes": 4194304, "workspaces": {"agent-exec": {"path": str(root), "allow_write": True}, "brainstorm-scratch": {"path": str(scratch), "allow_write": False}}, "executables": {name: shutil.which(name) or name for name in ("codex", "claude", "grok")}}
        atomic_write(config_path, yaml.safe_dump(config, sort_keys=False))
    for name in ("roles.yaml", "backends.yaml"):
        target = config_dir / name
        if not target.exists():
            shutil.copy2(root / "legacy" / name, target)
    launcher = home / ".local/bin/agent-exec"
    if launcher.exists() or launcher.is_symlink():
        if launcher.is_symlink():
            (backup / "agent-exec.symlink.txt").write_text(os.readlink(launcher))
        else:
            shutil.copy2(launcher, backup / "agent-exec")
    # Works even if a legacy supervisor invokes `python3 agent-exec _run ...`.
    body = "#!/usr/bin/env python3\nimport os, sys\nos.execv(" + repr(str(python)) + ", [" + repr(str(python)) + ", '-m', 'agent_exec.cli', *sys.argv[1:]])\n"
    atomic_write(launcher, body, 0o755)
    links = []
    for base in (home / ".codex/skills", home / ".claude/skills"):
        destination = base / "agent-exec"
        target = root / "skills/agent-exec"
        base.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            if not destination.is_symlink() or destination.resolve() != target.resolve():
                raise RuntimeError(f"Existing skill is not owned by this checkout: {destination}")
        else:
            destination.symlink_to(target, target_is_directory=True)
        links.append(str(destination))
    unit = home / ".config/systemd/user/agent-exec.service"
    if unit.exists():
        shutil.copy2(unit, backup / "agent-exec.service")
    # All arguments are systemd-quoted, never interpolated into a shell.
    def quote(s: str) -> str:
        return json.dumps(s.replace("%", "%%"))
    unit_text = "\n".join([
        "[Unit]", "Description=agent-exec local agent execution API", "After=network.target", "", "[Service]", "Type=simple",
        "WorkingDirectory=" + str(root).replace("%", "%%"),
        "ExecStart=" + quote(str(python)) + " -m agent_exec.server --config " + quote(str(config_path)),
        "Environment=" + quote("PATH=" + os.environ.get("PATH", os.defpath)),
        "Environment=PYTHONUNBUFFERED=1", "UMask=0077", "KillMode=control-group", "TimeoutStopSec=45", "Restart=on-failure", "RestartSec=3", "", "[Install]", "WantedBy=default.target", "",
    ])
    atomic_write(unit, unit_text)
    if start:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "agent-exec.service"], check=True)
        import httpx
        installed = yaml.safe_load(config_path.read_text())
        host = installed.get("host", "127.0.0.1")
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        if ":" in host:
            host = "[" + host + "]"
        url = f"http://{host}:{installed.get('port', 9891)}/v1/health"
        auth = Path(installed.get("token_file", token)).expanduser().read_text().strip()
        deadline = time.monotonic() + 15
        with httpx.Client(trust_env=False, timeout=1) as client:
            while True:
                try:
                    response = client.get(url, headers={"Authorization": "Bearer " + auth})
                    if response.is_success and response.json().get("status") == "ok":
                        break
                except (httpx.HTTPError, ValueError):
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("Service did not become ready; inspect journalctl --user -u agent-exec.service")
                time.sleep(0.2)
    return {"project": str(root), "config": str(config_path), "token_file": str(token), "state": str(state), "launcher": str(launcher), "skills": links, "unit": str(unit), "backups": str(backup), "started": start}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", action="store_true", help="enable and start the user service")
    parser.add_argument("--home", type=Path, default=Path.home(), help="override only for installation fixture tests")
    args = parser.parse_args()
    if args.start and args.home.resolve() != Path.home().resolve():
        parser.error("--start requires the real user home")
    print(json.dumps(install(Path(__file__).resolve().parents[1], args.home, args.start), indent=2))

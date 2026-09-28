import importlib.util
import os
from pathlib import Path
import subprocess
import sys


def test_install_preserves_configuration_and_legacy_launcher_backup(tmp_path):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("install_user", root / "scripts/install_user.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    home = tmp_path / "home"
    config = home / ".config/agent-exec"
    config.mkdir(parents=True)
    (config / "roles.yaml").write_text("precious existing roles")
    binary = home / ".local/bin/agent-exec"
    binary.parent.mkdir(parents=True)
    binary.write_text("old runner")
    result = installer.install(root, home, False)
    assert (config / "roles.yaml").read_text() == "precious existing roles"
    assert (Path(result["backups"]) / "agent-exec").read_text() == "old runner"
    assert (config / "service.token").stat().st_mode & 0o077 == 0
    assert (home / ".codex/skills/agent-exec").resolve() == root / "skills/agent-exec"
    assert 'WorkingDirectory="' not in Path(result["unit"]).read_text()
    env = dict(os.environ, HOME=str(home))
    check = subprocess.run([sys.executable, str(binary), "--help"], env=env, capture_output=True, text=True)
    assert check.returncode == 0 and "capabilities" in check.stdout
    token = (config / "service.token").read_text()
    second = installer.install(root, home, False)
    assert (config / "service.token").read_text() == token
    assert second["backups"] != result["backups"]
    assert (Path(result["backups"]) / "agent-exec").read_text() == "old runner"

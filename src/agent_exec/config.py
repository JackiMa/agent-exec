"""Local administrator configuration. API callers cannot supply executables or paths."""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Role:
    provider: str
    model: str = ""
    effort: str = "medium"
    access: str = "read-only"
    kind: str = "research"
    description: str = ""
    argv: tuple[str, ...] = ()


@dataclass(frozen=True)
class Workspace:
    path: Path
    allow_write: bool = False


def default_roles() -> dict[str, Role]:
    return {
        "codex-scout": Role("codex", "gpt-5.6-luna", description="Read sources and return evidence; no architecture decisions."),
        "codex-reviewer": Role("codex", "gpt-5.6-terra", "high", kind="review", description="Independently reproduce defects and check evidence."),
        "codex-worker": Role("codex", "gpt-5.6-terra", "high", "worktree", "implement", "Implement a settled, scoped change in an isolated checkout."),
        "codex-debug": Role("codex", "gpt-5.6-sol", "high", "worktree", "debug", "Investigate and repair a difficult failure with a reproducer."),
        "claude-chat": Role("claude", kind="discuss", description="Text-only Claude completion; tools disabled."),
        "grok-chat": Role("grok", kind="discuss", description="Text-only Grok completion; tools disabled."),
    }


ALIASES = {"scout": "codex-scout", "research": "codex-scout", "gate": "codex-reviewer", "critic": "codex-reviewer", "chore": "codex-worker", "operator": "codex-worker", "worker": "codex-worker", "debug": "codex-debug", "deep": "codex-debug", "scientist": "codex-debug", "codex-terra": "codex-worker", "codex-sol": "codex-debug"}


@dataclass
class Settings:
    state_dir: Path = field(default_factory=lambda: Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "agent-exec")
    token_file: Path = field(default_factory=lambda: Path.home() / ".config/agent-exec/service.token")
    host: str = "127.0.0.1"
    port: int = 9891
    max_concurrency: int = 3
    max_pending: int = 100
    default_timeout_seconds: int = 300
    max_timeout_seconds: int = 1800
    max_output_bytes: int = 4 * 1024 * 1024
    terminate_grace_seconds: float = 2.0
    workspaces: dict[str, Workspace] = field(default_factory=dict)
    roles: dict[str, Role] = field(default_factory=default_roles)
    executables: dict[str, str] = field(default_factory=lambda: {"codex": "codex", "claude": "claude", "grok": "grok"})


def load_settings(path: str | Path | None = None) -> Settings:
    path = Path(path or os.environ.get("AGENT_EXEC_SERVICE_CONFIG", Path.home() / ".config/agent-exec/service.yaml")).expanduser()
    settings = Settings()
    if not path.exists():
        return settings
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError("Service configuration must be a mapping")
    unknown = set(data) - set(Settings.__dataclass_fields__)
    if unknown:
        raise ValueError(f"Unknown service configuration fields: {sorted(unknown)}")
    for name, value in data.items():
        if name in ("state_dir", "token_file"):
            setattr(settings, name, Path(value).expanduser().resolve())
        elif name == "workspaces":
            if not isinstance(value, dict):
                raise ValueError("workspaces must be a mapping")
            workspaces = {}
            for key, spec in value.items():
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", str(key)) or not isinstance(spec, dict) or set(spec) - {"path", "allow_write"}:
                    raise ValueError("Invalid workspace registration")
                if not isinstance(spec.get("allow_write", False), bool):
                    raise ValueError("allow_write must be boolean")
                p = Path(spec["path"]).expanduser()
                if not p.is_absolute():
                    raise ValueError("Workspace paths must be absolute")
                workspaces[key] = Workspace(p.resolve(), spec.get("allow_write", False))
            settings.workspaces = workspaces
        elif name == "roles":
            for key, spec in value.items():
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", str(key)) or not isinstance(spec, dict):
                    raise ValueError("Invalid role definition")
                merged = asdict(settings.roles[key]) if key in settings.roles else {}
                merged.update(spec)
                if "argv" in merged:
                    if not isinstance(merged["argv"], (list, tuple)) or not all(isinstance(s, str) and s for s in merged["argv"]):
                        raise ValueError("Role argv must be a list of nonempty strings")
                    merged["argv"] = tuple(merged["argv"])
                role = Role(**merged)
                if role.provider not in {"codex", "claude", "grok", "command"} or role.access not in {"read-only", "worktree"}:
                    raise ValueError("Unsupported role provider or access")
                if role.provider in {"claude", "grok"} and role.access != "read-only":
                    raise ValueError("Claude/Grok adapters currently support text-only roles")
                if role.provider == "command" and not role.argv:
                    raise ValueError("A command role requires administrator-configured argv")
                settings.roles[key] = role
        elif name == "executables":
            if not isinstance(value, dict) or set(value) - {"codex", "claude", "grok"} or not all(isinstance(v, str) and v for v in value.values()):
                raise ValueError("Invalid executables mapping")
            settings.executables.update(value)
        else:
            setattr(settings, name, value)
    for key, low, high in (("port", 1, 65535), ("max_concurrency", 1, 32), ("max_pending", 1, 10000), ("default_timeout_seconds", 1, 86400), ("max_timeout_seconds", 1, 86400), ("max_output_bytes", 1024, 64*1024*1024)):
        v = getattr(settings, key)
        if type(v) is not int or not low <= v <= high:
            raise ValueError(f"Invalid {key}")
    if settings.default_timeout_seconds > settings.max_timeout_seconds:
        raise ValueError("Default timeout exceeds maximum")
    if not isinstance(settings.host, str) or not settings.host:
        raise ValueError("host must be a nonempty string")
    if not isinstance(settings.terminate_grace_seconds, (int, float)) or not 0 <= settings.terminate_grace_seconds <= 30:
        raise ValueError("Invalid terminate_grace_seconds")
    return settings

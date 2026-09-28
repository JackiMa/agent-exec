#!/usr/bin/env python3
"""agent-exec (codex-exec) — background job runner for external coding agents.

Roles and backends come from two hand-maintained tables in ~/.config/agent-exec/
(roles.yaml, backends.yaml). A dispatch names a ROLE; the row decides kind, write
access (sandbox / worktree), backend (engine / model / effort) and the delivery
contract. The calling agent and its driver decide nothing.

  agent-exec roles                       -> the table, as markdown
  agent-exec render [--out DIR]          -> generate the driver files from the table
  agent-exec check  [--no-probe]         -> tables valid, drivers in sync, backends up
  agent-exec resolve ROLE [--task-file]  -> what a dispatch would use, and why
  agent-exec dispatch --role ROLE        -> dispatch through the table
  agent-exec hook-agent                  -> PreToolUse gate: refuse subagents below
                                            dispatch_policy.min_effort (stdin: hook JSON)

Runs `codex exec` as a detached, tracked job and renders a compact
deterministic digest of the result.

Design note: everything that can be decided by code is decided here, not by the
shim agent. Path existence checks, git diff reconciliation and report rendering
are deterministic — spending model tokens on them is both more expensive and
less reliable. The shim's only job is start -> wait -> print digest.

  codex-exec start  [opts] < brief.md   -> JOB_ID, returns immediately
  codex-exec wait   JOB [--timeout SEC] -> exit 0 done / 2 still running
  codex-exec digest JOB                 -> compact markdown report (the handoff)
  codex-exec result JOB                 -> full JSON, when the digest is not enough
  codex-exec log    JOB [--tail N]
  codex-exec list [--all] | kill JOB
  codex-exec verdict JOB PASS|FAIL|PARTIAL|BLOCKED [opts]
  codex-exec report [--since DATE] [--by profile] [--json|--tsv]
  codex-exec gc [--older-than HOURS] [--dry-run] [--force]

Isolated jobs (--worktree) additionally support:

  codex-exec diff   JOB                 -> the patch it produced
  codex-exec adopt  JOB [--drop]        -> apply it, optionally clean the worktree
  codex-exec drop   JOB [--force]       -> discard the worktree

Writable worktree jobs start from an immutable snapshot of the origin's tracked
and non-ignored working state. The base is recorded at
``refs/codex/base/codex-worker/<job>``; successful adoptions advance
``refs/codex/adopted/codex-worker/<job>``. ``diff`` records the exact reviewed
patch hash in ``diff-viewed`` and ``adopt`` records its outcome in
``adopt.json``. Configuration keys ``worktree_sandbox`` and
``snapshot_max_file_mb`` control writer isolation and oversized untracked-file
exclusion respectively.

--role picks a row of the roles table (--profile is the legacy name); explicit --model/--effort/--sandbox
override it. The engine comes from the row's backend (or --engine); codex is the
only engine wired in today — a new backend engine needs a build_cmd branch.
"""

import argparse
import datetime
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from statistics import median
from pathlib import Path

ROOT = Path(os.environ.get("CODEX_WORKER_HOME", Path.home() / ".codex-worker"))
JOBS = ROOT / "jobs"
CONFIG = ROOT / "config.json"

DEFAULT_CONFIG = {
    "default_sandbox": "yolo",      # yolo | workspace-write | read-only
    "worktree_sandbox": "yolo",     # sandbox for otherwise-unspecified worktree writers
    "snapshot_max_file_mb": 50,      # exclude larger untracked files from snapshots
    "digest_max_files": 15,
}

# ---------------------------------------------------------------- tables
#
# Two hand-maintained tables define every role and every backend. Nothing
# about a dispatch is decided by the calling agent or its driver: the role
# name selects a row, and the row decides what is delivered (kind), how the
# job is isolated (writes -> sandbox / worktree), which engine at which effort
# (route -> backend) and how the result is judged (deliverable, brief_requires,
# verdict_level). `agent-exec render` compiles the roles table into the driver
# files Claude picks from; `agent-exec check` proves the two still agree.

TABLES_DIR = Path(os.environ.get("AGENT_EXEC_CONFIG",
                                 Path.home() / ".config" / "agent-exec"))
ROLES_FILE = TABLES_DIR / "roles.yaml"
BACKENDS_FILE = TABLES_DIR / "backends.yaml"
RENDER_LOCK = TABLES_DIR / "render.lock.json"
ROLES_MD = TABLES_DIR / "roles.generated.md"
PROJECT_FILE = ".agent-exec.yaml"
PROBE_CACHE = ROOT / "probe-cache.json"
PROBE_TTL_SEC = 3600

KINDS = ("QUERY", "CHANGE", "VERIFY")
WRITES = ("none", "tree", "worktree", "expdir")
WRITES_ORDER = ("tree", "expdir", "worktree", "none")   # least -> most restrictive
EFFORTS = ("low", "medium", "high", "xhigh", "max")
LEVELS = ("L0", "L1", "L2")
TEMPLATES = ("dispatch", "gate")
ENGINES = ("codex",)   # an engine needs a build_cmd branch; extend when an adapter lands
SANDBOXES = ("read-only", "workspace-write", "yolo")
DELIVERABLE_TOKENS = ("receipt", "claims", "evidence", "reproducer",
                      "experiment", "sources", "verdict", "defects")
DEFAULT_BRIEF_REQUIRES = {"CHANGE": ["CLAIMS", "CHECKS", "RECEIPT"],
                          "QUERY": ["CLAIM"], "VERIFY": []}

# ---------------------------------------------------------------- dispatch gate
#
# A subagent costs minutes. When the session is running at low effort the user
# wants an answer now, so `agent-exec hook-agent` (a PreToolUse hook on the Agent
# tool) refuses the dispatch under dispatch_policy.min_effort and tells Claude to
# do the work itself. Claude Code hands the hook the turn's effort in the payload
# (effort.level) and in $CLAUDE_EFFORT. The same min_effort value is rendered into
# the CLAUDE.md policy block, so the written rule cannot drift from the enforced one.

GATE_OFF = ROOT / "DISPATCH_GATE_OFF"        # touch this file to disable the gate
FORCE_TOKEN = "FORCE_DISPATCH"               # in the Agent prompt: the user asked for a worker
AGENT_TOOLS = ("Agent", "Task")
DEFAULT_MIN_EFFORT = "high"
# "手动说明": what the user's own last message looks like when they asked for a worker.
DEFAULT_OVERRIDE_PHRASES = [
    r"subagent", r"sub-agent", r"\bagent\b", r"codex", r"dispatch", r"\bworker\b",
    r"派工", r"派个", r"派活", r"派.{0,3}(给|去|做)", r"后台跑", r"并行", r"丢给", r"交给.{0,6}(跑|做|处理)",
]
BRIEF_TOKEN_RE = {
    "CLAIMS": re.compile(r"\bclaims?\b", re.I),
    "CLAIM": re.compile(r"\bclaims?\b", re.I),
    "CHECKS": re.compile(r"\bchecks?\b", re.I),
    "RECEIPT": re.compile(r"STATUS\s*:\s*PRECHECK_PASS", re.I),
    "REPRODUCER": re.compile(r"\breproducer\b", re.I),
    "LOCKED_VERIFIER": re.compile(r"LOCKED_VERIFIER", re.I),
}


def writes_to_access(writes):
    """(sandbox or None for the config default, worktree?)."""
    return {"none": ("read-only", False), "tree": (None, False),
            "worktree": (None, True), "expdir": ("workspace-write", False)}[writes]


def tables_sha():
    h = hashlib.sha256()
    for p in (ROLES_FILE, BACKENDS_FILE):
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"")
    return h.hexdigest()[:12]


def _load_yaml(path, errors):
    try:
        import yaml
    except ImportError:
        errors.append("PyYAML is not installed (python3 -m pip install pyyaml)")
        return None
    if not path.exists():
        errors.append(f"missing table: {path}")
        return None
    try:
        doc = yaml.safe_load(path.read_text())
    except Exception as exc:
        errors.append(f"{path}: not valid YAML: {exc}")
        return None
    if not isinstance(doc, dict):
        errors.append(f"{path}: top level must be a mapping")
        return None
    return doc


def _validate_backends(doc, errors):
    out = {}
    for name, b in (doc.get("backends") or {}).items():
        where = f"backends.{name}"
        if not isinstance(b, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        if b.get("engine") not in ENGINES:
            errors.append(f"{where}.engine: want one of {ENGINES}, got {b.get('engine')!r}")
        if not isinstance(b.get("model"), str) or not b["model"]:
            errors.append(f"{where}.model: required string")
        emap = b.get("effort_map") or {}
        if (not isinstance(emap, dict)
                or any(k not in EFFORTS or v not in EFFORTS for k, v in emap.items())):
            errors.append(f"{where}.effort_map: keys and values must be in {EFFORTS}")
        sb = b.get("sandboxes") or []
        if not isinstance(sb, list) or any(s not in SANDBOXES for s in sb):
            errors.append(f"{where}.sandboxes: list drawn from {SANDBOXES}")
        if b.get("probe") is not None and not isinstance(b["probe"], str):
            errors.append(f"{where}.probe: shell command string, or omit")
        out[name] = {"engine": b.get("engine"), "model": b.get("model"),
                     "effort_map": emap if isinstance(emap, dict) else {},
                     "sandboxes": sb if isinstance(sb, list) else [],
                     "probe": b.get("probe"), "price": b.get("price"),
                     "zdr": bool(b.get("zdr", False))}
    if not out:
        errors.append("backends: table is empty")
    return out


def _validate_roles(doc, backends, errors):
    out, profiles = {}, {}
    for name, r in (doc.get("roles") or {}).items():
        where = f"roles.{name}"
        if not isinstance(r, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        role = {"kind": r.get("kind"), "writes": r.get("writes")}
        if role["kind"] not in KINDS:
            errors.append(f"{where}.kind: want one of {KINDS}, got {role['kind']!r}")
        if role["writes"] not in WRITES:
            errors.append(f"{where}.writes: want one of {WRITES}, got {role['writes']!r}")
        route = r.get("route")
        if not isinstance(route, list) or not route:
            errors.append(f"{where}.route: non-empty list of {{backend, effort}}")
            route = []
        clean = []
        for i, hop in enumerate(route):
            if not isinstance(hop, dict) or hop.get("backend") not in backends:
                errors.append(f"{where}.route[{i}].backend: unknown backend "
                              f"{hop.get('backend') if isinstance(hop, dict) else hop!r}")
                continue
            eff = hop.get("effort")
            if eff is not None and eff not in EFFORTS:
                errors.append(f"{where}.route[{i}].effort: want one of {EFFORTS} or null")
            clean.append({"backend": hop["backend"], "effort": eff})
        role["route"] = clean
        deliv = r.get("deliverable") or []
        if isinstance(deliv, str):
            deliv = [deliv]
        if not deliv or any(t not in DELIVERABLE_TOKENS for t in deliv):
            errors.append(f"{where}.deliverable: list drawn from {DELIVERABLE_TOKENS}")
        role["deliverable"] = deliv
        for key in ("description", "summary"):
            if not isinstance(r.get(key), str) or not r[key].strip():
                errors.append(f"{where}.{key}: required string")
            role[key] = (r.get(key) or "").strip() if isinstance(r.get(key), str) else ""
        for key in ("use_for", "not_for"):
            v = r.get(key) or []
            if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
                errors.append(f"{where}.{key}: list of strings")
                v = []
            role[key] = v
        role["driver"] = r.get("driver")
        if role["driver"] is not None and (not isinstance(role["driver"], str)
                                           or "/" in role["driver"] or not role["driver"]):
            errors.append(f"{where}.driver: file stem (no path) or null")
        role["template"] = r.get("template", "dispatch")
        if role["template"] not in TEMPLATES:
            errors.append(f"{where}.template: want one of {TEMPLATES}")
        role["guard"] = (r.get("guard") or "").strip()
        role["profile"] = r.get("profile")
        if role["profile"] is not None:
            if not isinstance(role["profile"], str):
                errors.append(f"{where}.profile: string or null")
            elif role["profile"] in profiles:
                errors.append(f"{where}.profile: {role['profile']!r} already used by "
                              f"roles.{profiles[role['profile']]}")
            else:
                profiles[role["profile"]] = name
        br = r.get("brief_requires")
        if br is None:
            br = DEFAULT_BRIEF_REQUIRES.get(role["kind"], [])
        if not isinstance(br, list) or any(t not in BRIEF_TOKEN_RE for t in br):
            errors.append(f"{where}.brief_requires: list drawn from {tuple(BRIEF_TOKEN_RE)}")
            br = []
        role["brief_requires"] = br
        role["verdict_level"] = r.get("verdict_level", "L1")
        if role["verdict_level"] not in LEVELS:
            errors.append(f"{where}.verdict_level: want one of {LEVELS}")
        role["tier_up"] = r.get("tier_up")
        role["network"] = r.get("network", False)
        if not isinstance(role["network"], bool):
            errors.append(f"{where}.network: true | false")
        role["min_effort"] = r.get("min_effort")
        if role["min_effort"] is not None and role["min_effort"] not in EFFORTS:
            errors.append(f"{where}.min_effort: one of {EFFORTS}, or null to use "
                          "dispatch_policy.min_effort")
        out[name] = role
    for name, role in out.items():
        if role["tier_up"] is not None and role["tier_up"] not in out:
            errors.append(f"roles.{name}.tier_up: unknown role {role['tier_up']!r}")
        if role["writes"] in WRITES:
            want, _ = writes_to_access(role["writes"])
            for hop in role["route"]:
                b = backends[hop["backend"]]
                if want and want not in b["sandboxes"]:
                    errors.append(f"roles.{name}: writes={role['writes']} needs sandbox "
                                  f"{want!r}, which {hop['backend']} does not list")
    if not out:
        errors.append("roles: table is empty")
    return out, profiles


def _validate_policy(dp, errors):
    """dispatch_policy: the one dial for `who may be dispatched at what effort`."""
    if dp is None:
        dp = {}
    if not isinstance(dp, dict):
        errors.append("dispatch_policy: must be a mapping")
        dp = {}
    out = {"min_effort": dp.get("min_effort", DEFAULT_MIN_EFFORT),
           "override_phrases": dp.get("override_phrases", DEFAULT_OVERRIDE_PHRASES)}
    if out["min_effort"] is not None and out["min_effort"] not in EFFORTS:
        errors.append(f"dispatch_policy.min_effort: one of {EFFORTS}, or null to "
                      "dispatch at every effort")
        out["min_effort"] = None
    ph = out["override_phrases"]
    if not isinstance(ph, list) or any(not isinstance(x, str) for x in ph):
        errors.append("dispatch_policy.override_phrases: list of regexes")
        ph = out["override_phrases"] = []
    for rx in ph:
        try:
            re.compile(rx)
        except re.error as exc:
            errors.append(f"dispatch_policy.override_phrases: {rx!r} is not a regex: {exc}")
    return out


_TABLES = None


def load_tables():
    """Parse + validate both tables once. Never exits: callers that need a
    usable table go through require_tables(); everything else keeps working."""
    global _TABLES
    if _TABLES is not None:
        return _TABLES
    errors = []
    bdoc = _load_yaml(BACKENDS_FILE, errors)
    rdoc = _load_yaml(ROLES_FILE, errors)
    backends = _validate_backends(bdoc, errors) if bdoc else {}
    roles, profiles = _validate_roles(rdoc, backends, errors) if rdoc else ({}, {})
    rdoc = rdoc or {}
    settings = {
        "driver_dir": Path(os.path.expanduser(rdoc.get("driver_dir") or "~/.claude/agents")),
        "driver_command": rdoc.get("driver_command", "legacy"),
        # the file whose <!-- agent-exec:* --> blocks render rewrites; null = none
        "roster_file": (Path(os.path.expanduser(rdoc["roster_file"]))
                        if rdoc.get("roster_file") else None),
        "dispatch_policy": _validate_policy(rdoc.get("dispatch_policy"), errors),
    }
    if settings["driver_command"] not in ("legacy", "role"):
        errors.append("driver_command: want legacy | role")
    _TABLES = {"roles": roles, "backends": backends, "profiles": profiles,
               "settings": settings, "errors": errors}
    return _TABLES


def require_tables():
    t = load_tables()
    if t["errors"]:
        sys.exit("agent-exec: the role tables are not usable:\n  - "
                 + "\n  - ".join(t["errors"])
                 + f"\n  fix {ROLES_FILE} / {BACKENDS_FILE}, then run: agent-exec check")
    return t


def role_for_profile(profile):
    t = require_tables()
    if profile not in t["profiles"]:
        sys.exit(f"agent-exec: unknown profile {profile!r}; known: "
                 f"{', '.join(sorted(t['profiles']))} (or use --role)")
    return t["profiles"][profile]


def probe_backend(name, b, fresh=False):
    """(up?, why). Cached PROBE_TTL_SEC so dispatch does not pay for a probe
    every time; a backend with no probe configured is assumed up."""
    if not b.get("probe"):
        return True, "no probe configured"
    cache = read_json(PROBE_CACHE, {}) or {}
    ent = cache.get(name) or {}
    now = time.time()
    if not fresh and ent and now - ent.get("at", 0) < PROBE_TTL_SEC:
        return bool(ent.get("ok")), f"cached {int(now - ent['at'])}s ago: {ent.get('out')}"
    try:
        r = subprocess.run(b["probe"], shell=True, capture_output=True, text=True, timeout=15)
        ok = r.returncode == 0
        text = (r.stdout or r.stderr).strip()
        out = text.splitlines()[0][:80] if text else f"exit {r.returncode}"
    except Exception as exc:
        ok, out = False, str(exc)[:80]
    cache[name] = {"ok": ok, "out": out, "at": now}
    try:
        write_json(PROBE_CACHE, cache)
    except OSError:
        pass
    return ok, out


def project_override(cwd, role_name):
    """Per-project tightening for one role, from <git root>/.agent-exec.yaml:
    roles: {NAME: {writes: ..., verdict_level: ...}}. Loosening is ignored."""
    if not cwd:
        return {}, None
    root = git_root(cwd) or str(cwd)
    p = Path(root) / PROJECT_FILE
    if not p.exists():
        return {}, None
    try:
        import yaml
        doc = yaml.safe_load(p.read_text()) or {}
    except Exception as exc:
        print(f"agent-exec: WARNING {p}: unreadable ({exc}); ignored", file=sys.stderr)
        return {}, p
    ov = ((doc.get("roles") or {}).get(role_name) or {}) if isinstance(doc, dict) else {}
    return {k: ov[k] for k in ("writes", "verdict_level") if k in ov}, p


def resolve_role(role_name, cwd=None, dirs=None, mode="role", explicit=None,
                 probe=True, fresh=False):
    """The whole dispatch decision for one role, each value tagged with the
    layer it came from: role default -> project override (tighten only) ->
    brief header (per-run parameters) -> explicit flag.

    mode="role":   the row is authoritative — writes decides sandbox/worktree,
                   route is probed, a brief may tighten but never loosen.
    mode="legacy": `--profile` compatibility — same row for engine/model/
                   effort/sandbox, but WORKTREE stays header-driven and nothing
                   is probed, so existing drivers behave exactly as before."""
    t = require_tables()
    dirs, explicit = dirs or {}, explicit or {}
    if role_name not in t["roles"]:
        sys.exit(f"agent-exec: unknown role {role_name!r}; known: "
                 f"{', '.join(sorted(t['roles']))}")
    role = t["roles"][role_name]
    c = cfg()
    val, src, notes = {}, {}, []

    def put(k, v, s):
        val[k], src[k] = v, s

    put("role", role_name, "role")
    put("mode", mode, "-")
    for k in ("kind", "writes", "deliverable", "verdict_level", "brief_requires",
              "profile", "tier_up", "network"):
        put(k, role[k], "role")

    ov, ovfile = project_override(cwd, role_name)
    if "writes" in ov:
        if (ov["writes"] in WRITES
                and WRITES_ORDER.index(ov["writes"]) > WRITES_ORDER.index(val["writes"])):
            put("writes", ov["writes"], f"project ({ovfile})")
        else:
            notes.append(f"project override writes={ov['writes']!r} ignored: "
                         f"not tighter than {val['writes']!r}")
    if "verdict_level" in ov:
        if (ov["verdict_level"] in LEVELS
                and LEVELS.index(ov["verdict_level"]) > LEVELS.index(val["verdict_level"])):
            put("verdict_level", ov["verdict_level"], f"project ({ovfile})")
        else:
            notes.append(f"project override verdict_level={ov['verdict_level']!r} ignored: "
                         f"not stricter than {val['verdict_level']!r}")

    chosen = None
    for i, hop in enumerate(role["route"]):
        b = t["backends"][hop["backend"]]
        ok, why = (probe_backend(hop["backend"], b, fresh)
                   if (probe and mode == "role") else (True, "not probed"))
        if ok:
            chosen = (i, hop, b, why)
            break
        notes.append(f"route[{i}] {hop['backend']}: DOWN ({why})")
    if chosen is None:
        sys.exit(f"agent-exec: BLOCKED — no backend in role {role_name}'s route answers "
                 "its probe:\n  - " + "\n  - ".join(notes))
    i, hop, b, why = chosen
    put("backend", hop["backend"], f"route[{i}]" + (" FALLBACK" if i else ""))
    put("fallback", bool(i), "-")
    put("engine", b["engine"], f"backend {hop['backend']}")
    put("model", b["model"], f"backend {hop['backend']}")
    eff = b["effort_map"].get(hop["effort"], hop["effort"]) if hop["effort"] else None
    put("effort", eff, f"route[{i}].effort via {hop['backend']}.effort_map")

    if dirs.get("TIER"):
        model, effort = resolve_tier(dirs["TIER"])
        put("model", model, "brief TIER")
        put("effort", effort, "brief TIER")

    sandbox, worktree = writes_to_access(val["writes"])
    hdr = (dirs.get("WORKTREE") or "").strip().lower()
    hdr_yes, hdr_no = hdr in ("yes", "true", "1"), hdr in ("no", "false", "0")
    if mode == "legacy":
        worktree = hdr_yes
        put("worktree", worktree, "brief WORKTREE" if hdr else "default (legacy)")
    else:
        if hdr_yes and not worktree:
            if val["writes"] == "none":
                notes.append("WORKTREE: yes ignored — a read-only role has nothing to isolate")
            else:
                worktree, sandbox = True, None
                put("writes", "worktree", "brief WORKTREE (tightened)")
        elif hdr_no and worktree:
            sys.exit(f"agent-exec: refused — role {role_name} writes in an isolated "
                     "worktree; a brief cannot loosen that (WORKTREE: no).")
        put("worktree", worktree, src["writes"])

    for k in ("engine", "model", "effort"):
        if explicit.get(k):
            put(k, explicit[k], "explicit flag")
    put("sandbox_hint", sandbox, "writes")
    if explicit.get("sandbox"):
        put("sandbox", explicit["sandbox"], "explicit flag")
    elif sandbox:
        put("sandbox", sandbox, f"writes={val['writes']}")
    else:
        key = "worktree_sandbox" if val["worktree"] else "default_sandbox"
        put("sandbox", c[key], f"config {key}")
    if val["sandbox"] not in b["sandboxes"]:
        notes.append(f"sandbox {val['sandbox']!r} is not listed in "
                     f"{hop['backend']}.sandboxes {b['sandboxes']}")
    val["_source"], val["_notes"] = src, notes
    return val


def resolve_tier(tier):
    """`TIER: <backend|model> <effort>` -> (model, effort). The name must be a
    backend alias or a model id from backends.yaml: a family nickname ("sol") or an
    alias where a model id was expected ("codex-terra") used to reach `codex --model`
    verbatim and die four seconds later, past every gate."""
    parts = tier.split()
    if len(parts) != 2:
        sys.exit(f"agent-exec: bad TIER directive {tier!r} (want: <backend|model> <effort>)")
    name, effort = parts
    t = require_tables()
    models = {b["model"]: alias for alias, b in t["backends"].items()}
    if name in t["backends"]:
        name = t["backends"][name]["model"]
    elif name not in models:
        sys.exit(f"agent-exec: refused — TIER names {name!r}, which is neither a backend "
                 f"alias ({', '.join(sorted(t['backends']))}) nor a model id in "
                 f"{BACKENDS_FILE.name}. Add it there or use an alias.")
    if effort not in EFFORTS:
        sys.exit(f"agent-exec: refused — TIER effort {effort!r}; want one of {EFFORTS}")
    return name, effort


def check_brief_requires(res, body):
    return [tok for tok in res["brief_requires"] if not BRIEF_TOKEN_RE[tok].search(body)]


# ---------------------------------------------------------------- drivers
#
# The driver files are generated, never edited. Placeholders: @@NAME@@ driver
# stem, @@DESC@@ description, @@GUARD@@ optional pinned sentence, @@TOOL@@
# the runner name the driver calls, @@SEL@@ the selector (--profile X or
# --role X). `agent-exec check` re-renders and diffs, so a hand edit is caught.

DRIVER_DISPATCH = """\
---
name: @@NAME@@
description: "@@DESC@@"
tools: Bash
model: haiku
---

You are a driver for the Codex CLI. Codex does the work; you forward, verbatim, in both directions. You decide nothing.@@GUARD@@

Run ONE command (Bash tool timeout 600000 ms). The brief's header lines (CWD/TIER/LABEL/WORKTREE/RESUME) are parsed by @@TOOL@@ itself — copy everything through untouched:

```bash
cat > /tmp/brief.$$.md <<'BRIEF'
<incoming prompt, unchanged>
BRIEF
@@TOOL@@ dispatch @@SEL@@ --task-file /tmp/brief.$$.md
```

If the output ends with `STILL RUNNING: <JOB>`, repeat this single command (same timeout) up to 6 more times:

```bash
@@TOOL@@ finish <JOB>
```

**Your final message is the command output, copied verbatim, and nothing else.** It is already compact and reconciled against the filesystem. Anything you add is wasted tokens and a chance to introduce something Codex never said. If it never finished, the STILL RUNNING line with the JOB id IS your final message.
"""

DRIVER_GATE = """\
---
name: @@NAME@@
description: "@@DESC@@"
tools: Bash
model: haiku
---

You are a driver for the Codex CLI. Codex does the checking. You write nothing.

Run ONE combined command — do not verify anything yourself, do not judge the
work, and do not split the steps into separate tool calls. `--cwd` must be
absolute: take it from the prompt, or use the current directory. Run with the
Bash tool timeout set to 400000 ms:

```bash
cat > /tmp/gate.$$.md <<'BRIEF'
Verify the completion claim below against what is actually on disk.

Check only that the claimed code, files, tests, documentation and experiment
records exist and say what the claim says they say. Read the files. Run a
claimed test command if one is named. Do not redesign anything, do not judge
whether the approach is good, and do not review code quality.

Your entire final message must start with PASS or RECHECK_SOL, followed by one
line per claim: the proving file path or command output (PASS), or why it
could not be confirmed from direct evidence (RECHECK_SOL).

--- COMPLETION CLAIM ---
<incoming prompt, unchanged>
BRIEF
JOB=$(@@TOOL@@ start @@SEL@@ --cwd <ABS_DIR> --label "gate <3-4 words>" --task-file /tmp/gate.$$.md | grep -oE '[0-9]{8}T[0-9]{6}-[a-f0-9]+' | head -1)
echo "JOB=$JOB"
@@TOOL@@ wait "$JOB" --timeout 300 && @@TOOL@@ digest "$JOB" || echo "STILL RUNNING: $JOB"
```

If it printed STILL RUNNING, repeat the wait-and-digest line up to 3 more times.

**Your final message is the digest, copied verbatim, and nothing else.** Do not
soften a RECHECK_SOL into a PASS, and do not add your own opinion about whether
the work looks done. A gate that never finished is not a PASS — say so.
"""


def compose_description(role):
    desc = role["description"]
    if role["use_for"]:
        desc += " Use for: " + "; ".join(role["use_for"]) + "."
    if role["not_for"]:
        desc += " Not for: " + "; ".join(role["not_for"]) + "."
    return desc.replace('"', '\\"')


def render_driver(name, role, settings):
    # Under driver_command: legacy, a role that still has a `profile:` name keeps
    # calling `codex-exec dispatch --profile` (byte-identical to the hand-written
    # originals). A role without one has no old behaviour to preserve and always
    # takes the enforced `agent-exec dispatch --role` path.
    legacy = settings["driver_command"] == "legacy" and bool(role["profile"])
    tool = "codex-exec" if legacy else "agent-exec"
    sel = f"--profile {role['profile']}" if legacy else f"--role {name}"
    tpl = DRIVER_GATE if role["template"] == "gate" else DRIVER_DISPATCH
    return (tpl.replace("@@NAME@@", role["driver"])
               .replace("@@DESC@@", compose_description(role))
               .replace("@@GUARD@@", (" " + role["guard"]) if role["guard"] else "")
               .replace("@@TOOL@@", tool)
               .replace("@@SEL@@", sel))


def block_markers(name):
    return f"<!-- agent-exec:{name} -->", f"<!-- /agent-exec:{name} -->"


def policy_markdown(t):
    """The dispatch-gate rule, rendered from the value the hook actually enforces."""
    p = t["settings"]["dispatch_policy"]
    lo = p["min_effort"]
    if not lo:
        return ("Generated by `agent-exec render`. No effort floor is set: subagents may "
                "be dispatched at every effort level.")
    below = ", ".join(EFFORTS[:EFFORTS.index(lo)])
    return (
        "Generated by `agent-exec render` from `~/.config/agent-exec/roles.yaml` "
        "— edit the YAML, not this block.\n\n"
        f"**At {below} effort you do the work yourself.** A PreToolUse hook "
        f"(`agent-exec hook-agent`) refuses the Agent tool below `{lo}` effort, because a "
        "worker costs minutes and a user running at that effort wants the answer now. "
        "The routing policy above is suspended for those turns: read, edit and run the "
        "checks directly, and keep the change to what was asked.\n\n"
        "The refusal lifts when the user asks for a worker in their own words. If they "
        f"did ask and the dispatch was still refused, put `{FORCE_TOKEN}` on its own line "
        "in the Agent prompt and call again — only on their say-so, never to get past the "
        f"gate on your own judgement. `{GATE_OFF}` (touch the file) turns the gate off "
        "entirely.")


def roster_markdown(t):
    """The compact roster spliced between the markers of settings.roster_file
    (CLAUDE.md): what Claude reads to know which subagents exist."""
    L = ["Generated by `agent-exec render` from `~/.config/agent-exec/roles.yaml` "
         "— edit the YAML, not this block.", "",
         "| agent | role | kind | writes | backend/effort | one-liner |",
         "|---|---|---|---|---|---|"]
    for name, r in sorted(t["roles"].items(), key=lambda kv: (KINDS.index(kv[1]["kind"]), kv[0])):
        if not r["driver"]:
            continue
        hop = r["route"][0]
        L.append(f"| `{r['driver']}` | {name} | {r['kind']} | "
                 f"{r['writes']}{'+net' if r['network'] else ''} | "
                 f"{hop['backend']}/{hop['effort'] or '-'} | {r['summary']} |")
    return "\n".join(L)


def splice_block(text, name, block):
    """text with the named marker block replaced; None if the markers are absent."""
    begin, end = block_markers(name)
    i, j = text.find(begin), text.find(end)
    if i < 0 or j < 0 or j < i:
        return None
    return text[:i + len(begin)] + "\n" + block + "\n" + text[j:]


# The blocks render owns inside roster_file. Everything outside them is the
# user's own text and is never touched.
MANAGED_BLOCKS = (("roster", lambda t: roster_markdown(t)),
                  ("policy", lambda t: policy_markdown(t)))


def roles_markdown(t):
    s = t["settings"]
    L = [f"# agent-exec roles — generated from {ROLES_FILE} ({tables_sha()}), "
         f"driver_command={s['driver_command']}. Do not edit; edit the YAML and run "
         "`agent-exec render`.", "",
         "| role | kind | writes | route (backend/effort) | deliverable | verdict | "
         "tier_up | driver | summary |",
         "|---|---|---|---|---|---|---|---|---|"]
    for name, r in sorted(t["roles"].items(), key=lambda kv: (KINDS.index(kv[1]["kind"]), kv[0])):
        route = " → ".join(f"{h['backend']}/{h['effort'] or '-'}" for h in r["route"])
        L.append(f"| {name} | {r['kind']} | {r['writes']}{'+net' if r['network'] else ''} | {route} | "
                 f"{'+'.join(r['deliverable'])} | {r['verdict_level']} | "
                 f"{r['tier_up'] or '-'} | {r['driver'] or '-'} | {r['summary']} |")
    p = t["settings"]["dispatch_policy"]
    L += ["", f"dispatch gate: subagents need effort >= `{p['min_effort'] or 'any'}` "
          f"(`agent-exec hook-agent`; off while {GATE_OFF} exists)",
          "", "| backend | engine | model | sandboxes | probe |", "|---|---|---|---|---|"]
    for name, b in sorted(t["backends"].items()):
        L.append(f"| {name} | {b['engine']} | {b['model']} | {', '.join(b['sandboxes'])} | "
                 f"{b['probe'] or '-'} |")
    return "\n".join(L) + "\n"


def cmd_roles(a):
    t = require_tables()
    if a.json:
        print(json.dumps({"roles": t["roles"], "backends": t["backends"],
                          "settings": {k: str(v) for k, v in t["settings"].items()},
                          "tables_sha": tables_sha()}, indent=2, ensure_ascii=False))
    else:
        sys.stdout.write(roles_markdown(t))
    return 0


def cmd_render(a):
    t = require_tables()
    s = t["settings"]
    out_dir = Path(a.out).resolve() if a.out else s["driver_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    lock = {"tables_sha": tables_sha(), "driver_command": s["driver_command"],
            "rendered_at": time.time(), "drivers": {}}
    old_lock = read_json(RENDER_LOCK, {}) or {}
    changed = 0
    for name, role in sorted(t["roles"].items()):
        if not role["driver"]:
            continue
        text = render_driver(name, role, s)
        p = out_dir / f"{role['driver']}.md"
        lock["drivers"][role["driver"]] = {
            "role": name, "sha256": hashlib.sha256(text.encode()).hexdigest()}
        old = p.read_text() if p.exists() else None
        if old == text:
            print(f"same     {p}")
            continue
        changed += 1
        if a.dry_run:
            print(f"would {'write' if old is None else 'update'} {p}")
        else:
            p.write_text(text)
            print(f"{'wrote' if old is None else 'updated':<8} {p}")
    managed_now = set(lock["drivers"])
    for drv in sorted(set((old_lock.get("drivers") or {})) - managed_now):
        p = out_dir / f"{drv}.md"
        if p.exists():
            if a.prune and not a.dry_run:
                p.unlink()
                print(f"pruned   {p} (no role owns it any more)")
            else:
                print(f"ORPHAN   {p} — no role owns it; rerun with --prune to delete")
    rf = s["roster_file"]
    if rf and not a.out:
        cur = rf.read_text() if rf.exists() else ""
        new = cur
        for name, render in MANAGED_BLOCKS:
            spliced = splice_block(new, name, render(t))
            if spliced is None:
                b, e = block_markers(name)
                print(f"block    {rf}: markers {b} … {e} not found, skipped")
            else:
                new = spliced
        if new == cur:
            print(f"same     {rf} (managed blocks)")
        elif a.dry_run:
            print(f"would update {rf} (managed blocks)")
        else:
            rf.write_text(new)
            print(f"updated  {rf} (managed blocks)")
    md = roles_markdown(t)
    if a.dry_run:
        pass
    elif a.out:
        (out_dir / "roles.generated.md").write_text(md)
        write_json(out_dir / "render.lock.json", lock)
    else:
        ROLES_MD.write_text(md)
        write_json(RENDER_LOCK, lock)
        print(f"table    {ROLES_MD}")
    print(f"{changed} driver(s) changed · tables {lock['tables_sha']} · "
          f"driver_command={s['driver_command']}")
    return 0


def cmd_check(a):
    t = load_tables()
    fails, oks = [], []
    for e in t["errors"]:
        fails.append(f"table: {e}")
    if not t["errors"]:
        s = t["settings"]
        lock = read_json(RENDER_LOCK, {}) or {}
        if lock.get("tables_sha") != tables_sha():
            fails.append(f"render: tables changed since the last render "
                         f"({lock.get('tables_sha')} → {tables_sha()}); run: agent-exec render")
        for name, role in sorted(t["roles"].items()):
            if not role["driver"]:
                continue
            p = s["driver_dir"] / f"{role['driver']}.md"
            want = render_driver(name, role, s)
            if not p.exists():
                fails.append(f"driver: {p} missing; run: agent-exec render")
            elif p.read_text() != want:
                fails.append(f"driver: {p} differs from the table (hand-edited or stale); "
                             "run: agent-exec render")
            else:
                oks.append(f"driver {p.name} matches role {name}")
        rf = s["roster_file"]
        if rf:
            cur = rf.read_text() if rf.exists() else ""
            for name, render in MANAGED_BLOCKS:
                new = splice_block(cur, name, render(t))
                b, _ = block_markers(name)
                if new is None:
                    fails.append(f"{name}: {rf} has no {b} block to keep in sync")
                elif new != cur:
                    fails.append(f"{name}: the {name} block in {rf} differs from the "
                                 "table; run: agent-exec render")
                else:
                    oks.append(f"{name} block in {rf.name} matches the table")
        for drv, ent in (lock.get("drivers") or {}).items():
            owner = t["roles"].get(ent.get("role") or "")
            if not owner or owner["driver"] != drv:
                fails.append(f"driver: {drv}.md was rendered for role {ent.get('role')!r}, "
                             "which no longer owns it; run: agent-exec render --prune")
        if not a.no_probe:
            for name, b in sorted(t["backends"].items()):
                ok, why = probe_backend(name, b, a.fresh)
                (oks if ok else fails).append(f"backend {name}: {'up' if ok else 'DOWN'} ({why})")
    if not a.quiet:
        for o in oks:
            print("ok    " + o)
    for f in fails:
        print("FAIL  " + f)
    if fails:
        print(f"agent-exec check: {len(fails)} problem(s)")
        return 1
    if not a.quiet:
        print(f"agent-exec check: tables {tables_sha()} and drivers agree")
    return 0


def cmd_resolve(a):
    dirs, body = {}, ""
    if a.task_file:
        brief = sys.stdin.read() if a.task_file == "-" else Path(a.task_file).read_text()
        dirs, body = parse_directives(brief)
    if a.cwd:
        dirs["CWD"] = a.cwd
    cwd = dirs.get("CWD") or os.getcwd()
    res = resolve_role(a.role, cwd=cwd, dirs=dirs, mode="role",
                       probe=not a.no_probe, fresh=a.fresh)
    src = res.pop("_source")
    notes = res.pop("_notes")
    if a.json:
        print(json.dumps({"resolved": res, "source": src, "notes": notes},
                         indent=2, ensure_ascii=False))
        return 0
    order = ("role", "kind", "writes", "worktree", "sandbox", "network", "backend", "engine", "model",
             "effort", "fallback", "deliverable", "brief_requires", "verdict_level",
             "tier_up", "profile")
    print(f"resolve {a.role} · cwd {cwd} · tables {tables_sha()}")
    for k in order:
        v = res.get(k)
        v = "+".join(v) if isinstance(v, list) else v
        print(f"  {k:<15} {str(v):<28} ← {src.get(k, '-')}")
    for n in notes:
        print(f"  note: {n}")
    if body:
        missing = check_brief_requires(res, body)
        print("  brief: " + ("all required sections present"
                             if not missing else "MISSING " + ", ".join(missing)))
    return 0



TRANSCRIPT_TAIL_BYTES = 512 * 1024


def _tail_lines(path, nbytes=TRANSCRIPT_TAIL_BYTES):
    """The last nbytes of a JSONL file as whole lines. Transcripts reach tens of
    MB and this runs inside a 5s PreToolUse hook, so the whole file is read only
    when the tail holds no answer."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        start = max(0, size - nbytes)
        fh.seek(start)
        blob = fh.read()
    lines = blob.decode("utf-8", "replace").splitlines()
    return (lines[1:] if start and lines else lines), start == 0


def _last_user_text(path):
    """The user's own last message — not a tool result, not a command echo."""
    try:
        lines, whole = _tail_lines(path)
    except OSError:
        return ""
    found = _scan_user_text(lines)
    if found is None and not whole:
        try:
            found = _scan_user_text(Path(path).read_text(errors="replace").splitlines())
        except OSError:
            found = None
    return found or ""


def _scan_user_text(lines):
    # No line cap: one long autonomous turn can put hundreds of assistant and
    # tool_result entries between the user's message and now. Parsing stops at
    # the first real user message, so the cost is only paid when there is none.
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("type") != "user" or e.get("isMeta"):
            continue
        c = (e.get("message") or {}).get("content")
        if isinstance(c, list):
            if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
                continue
            c = "\n".join(x.get("text", "") for x in c
                           if isinstance(x, dict) and x.get("type") == "text")
        if not isinstance(c, str) or not c.strip():
            continue
        prose = _user_prose(c)
        if prose:
            return prose
    return None


# A transcript records plenty of `user` entries the user never wrote: slash-command
# echoes, command output, injected reminders, IDE notices, background-task wake-ups,
# and the summary a compaction leaves behind. They arrive wrapped in tags, so what
# survives tag-stripping is the user's own prose — and a worker's name inside one of
# those wrappers is not the user asking for a worker. Matching the override phrases
# against the prose alone is what keeps the gate honest.
TAG_BLOCK_RE = re.compile(r"<([A-Za-z][\w.-]*)\b[^>]*>.*?</\1\s*>|<[A-Za-z][\w.-]*\b[^>]*/?>",
                          re.S)
COMPACTION_PREFIX = "This session is being continued from a previous conversation"


def _user_prose(text):
    if text.lstrip().startswith(COMPACTION_PREFIX):
        return ""
    return TAG_BLOCK_RE.sub(" ", text).strip()


def gate_decision(payload, env_effort=None):
    """(allow?, reason) for one Agent tool call. Fails open on anything unclear:
    a gate that misreads its input must not stop the session from working."""
    if payload.get("hook_event_name") not in (None, "PreToolUse"):
        return True, "not PreToolUse"
    if payload.get("tool_name") not in AGENT_TOOLS:
        return True, "not the Agent tool"
    if GATE_OFF.exists():
        return True, f"gate disabled ({GATE_OFF})"
    if payload.get("agent_id"):
        return True, "the call comes from inside a subagent, not the main thread"

    t = load_tables()
    if t["errors"]:
        return True, "tables unusable; not the gate's job to block on that"
    pol = t["settings"]["dispatch_policy"]
    ti = payload.get("tool_input") or {}

    floor = pol["min_effort"]
    sub = ti.get("subagent_type")
    for name, role in t["roles"].items():
        if sub and sub in (role["driver"], name) and role["min_effort"]:
            floor = role["min_effort"]
            break
    if not floor:
        return True, "no effort floor configured"

    eff = payload.get("effort")
    effort = eff.get("level") if isinstance(eff, dict) else eff
    if not isinstance(effort, str) or not effort:
        effort = env_effort or os.environ.get("CLAUDE_EFFORT")
    if effort not in EFFORTS:
        return True, f"effort unknown ({effort!r}); failing open"
    if EFFORTS.index(effort) >= EFFORTS.index(floor):
        return True, f"effort {effort} >= {floor}"

    blob = " ".join(str(ti.get(k, "")) for k in ("prompt", "description"))
    if FORCE_TOKEN in blob:
        return True, f"{FORCE_TOKEN} in the prompt"
    asked = _last_user_text(payload.get("transcript_path") or "")
    for rx in pol["override_phrases"]:
        if re.search(rx, asked, re.I):
            return True, f"the user asked for one (matched {rx!r})"

    who = f" ({sub})" if sub else ""
    return False, (
        f"Effort is {effort}, below {floor}: subagent dispatch{who} is off, so this turn "
        "answers now instead of in minutes.\n"
        "Do the work yourself this turn — read, edit and run the checks directly, and keep "
        "it to what was asked.\n"
        f"If the user did ask for a worker, put {FORCE_TOKEN} on its own line in the Agent "
        "prompt and call again. Only on their say-so.\n"
        f"Change the bar in {ROLES_FILE} (dispatch_policy.min_effort), raise the session "
        f"effort, or touch {GATE_OFF} to turn the gate off.")


def cmd_hook_agent(a):
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0                      # unreadable input is never a reason to block
    allow, reason = gate_decision(payload)
    if a.explain:
        print(f"{'allow' if allow else 'DENY'}: {reason}", file=sys.stderr)
    if allow:
        return 0
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason}}))
    return 0


REPORT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "files_changed", "verification", "incomplete", "risks"],
    "properties": {
        "summary": {"type": "string",
                    "description": "What was done, 1-3 sentences, past tense."},
        "files_changed": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "change", "why"],
                "properties": {
                    "path": {"type": "string"},
                    "change": {"type": "string",
                               "enum": ["created", "modified", "deleted"]},
                    "why": {"type": "string", "description": "Under 12 words."},
                },
            },
        },
        "verification": {"type": "string",
                         "description": "Commands actually run and their real result. "
                                        "'none' if nothing was run."},
        "incomplete": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
}

# Prepended to every WRITABLE job's brief. Field incident: the isolation
# protocol lived only in this driver layer (the WORKTREE header never reaches
# the engine), so a worker that had no idea a supervisor existed committed its
# own work, adopted a sibling's patch, and pushed the main branch. The contract
# has to be in the worker's own context, injected here so no brief can forget it.
WRITER_CONTRACT = """\
=== EXECUTION CONTEXT (injected by the job runner — read first, then do the task below) ===
You are one worker among several, run by an orchestrator.
{isolation}
- NEVER run: git commit / add / stash / checkout / reset / merge / rebase /
  push / branch / tag, nor any `agent-exec` or `codex-exec` command. Reviewing
  and applying your work is the supervisor's job, not yours{commit_exception}.
- The task's "Out of scope" list is a hard boundary. If the task seems to
  require touching something it forbids, STOP and report the conflict instead.
- In your report's `verification` field, paste the real output of
  `git status --short` and `git diff --stat` (when in a git repo). Never write
  a checkmark for a check you did not actually run.
=== END EXECUTION CONTEXT ===

"""
ISO_WORKTREE = ("Your cwd is an ISOLATED git worktree. Your deliverable is the "
                "UNCOMMITTED changes you leave in it — they return to the "
                "supervisor as a patch for review. Touch nothing outside this "
                "worktree.")
ISO_DIRECT = ("You edit this tree in place. Your deliverable is uncommitted "
              "working-tree changes.")

# Build artifacts. Excluded from generated patches (an untracked .pyc makes the
# whole patch unapplyable) and from the discrepancy report (pure noise).
NOISE_DIRS = ("__pycache__", "node_modules", ".pytest_cache", ".mypy_cache",
              ".ruff_cache", ".venv", "dist", "build", ".next", "target")
NOISE_MATCH = NOISE_DIRS + (".pyc", ".DS_Store", ".egg-info")

SESSION_KEYS = ("session_id", "sessionId", "thread_id", "threadId", "conversation_id")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
ENGINE_VERSION_CACHE = {}


def cfg():
    c = dict(DEFAULT_CONFIG)
    if CONFIG.exists():
        try:
            c.update(json.loads(CONFIG.read_text()))
        except Exception:
            pass
    return c


def read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def write_json(p: Path, obj):
    p.write_text(json.dumps(obj, indent=2, ensure_ascii=False))


def job_dir(job_id: str) -> Path:
    d = JOBS / job_id
    if not d.is_dir():
        sys.exit(f"codex-exec: no such job: {job_id}")
    return d


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _supervisor_pid(d):
    try:
        return int((d / "supervisor.pid").read_text().strip())
    except (OSError, TypeError, ValueError):
        return None


def _record_result_commit(meta, status):
    """Commit a worktree exactly once and add the result to status."""
    if not meta.get("worktree") or "result_commit" in status:
        return
    try:
        commit, error = commit_worktree_result(meta)
    except Exception as exc:
        commit, error = None, f"unexpected result commit failure: {exc}"
    status["result_commit"] = commit
    if error:
        status["result_commit_error"] = error


def status_of(d: Path, persist=True) -> dict:
    """Read state and reconcile dead detached supervisors when requested.

    Read-only callers pass ``persist=False``: they see ``lost`` in their output
    but never update a job directory or commit its worktree.
    """
    status = read_json(d / "status.json", {"state": "unknown"}) or {"state": "unknown"}
    if status.get("state") not in ("starting", "running"):
        return status
    pid = _supervisor_pid(d)
    if pid is not None and _pid_alive(pid):
        return status
    reconciled = dict(status)
    display_pid = pid if pid is not None else "missing"
    reconciled.update({
        "state": "lost",
        "lost_reason": f"supervisor pid {display_pid} not alive",
        "lost_detected_at": time.time(),
    })
    if persist:
        _record_result_commit(read_json(d / "meta.json", {}) or {}, reconciled)
        write_json(d / "status.json", reconciled)
    return reconciled


def running_jobs():
    if not JOBS.is_dir():
        return []
    return [d for d in JOBS.iterdir()
            if d.is_dir() and status_of(d, persist=False).get("state")
            in ("starting", "running")]


# ---------------------------------------------------------------- session ids

def find_session_id(d: Path):
    """The codex session id, from the first events.jsonl lines."""
    events = d / "events.jsonl"
    if not events.exists():
        return None

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in SESSION_KEYS and isinstance(v, str) and UUID_RE.fullmatch(v):
                    return v
                if (found := walk(v)):
                    return found
        elif isinstance(o, list):
            for v in o:
                if (found := walk(v)):
                    return found
        return None

    with events.open() as fh:
        for i, line in enumerate(fh):
            if i > 50:
                break
            try:
                if (found := walk(json.loads(line))):
                    return found
            except Exception:
                continue
    return None


# ---------------------------------------------------------------- start

def build_cmd(a, d: Path, cwd: Path):
    """The engine argv. The brief itself goes on stdin (task.md)."""
    if a.engine != "codex":
        sys.exit(f"agent-exec: engine {a.engine!r} has no adapter (known: {', '.join(ENGINES)})")
    cmd = ["codex", "exec"]
    if a.resume:
        cmd += ["resume", a.resume]
    cmd += ["--json", "--skip-git-repo-check"]
    if a.effort:
        cmd += ["-c", f"model_reasoning_effort={a.effort}"]
    # `resume` accepts neither --cd nor --sandbox; it inherits both from the
    # recorded session. The subprocess still runs with cwd=cwd either way.
    if not a.resume:
        cmd += ["--cd", str(cwd)]
        cmd += (["--dangerously-bypass-approvals-and-sandbox"]
                if a.sandbox == "yolo" else ["--sandbox", a.sandbox])
        # writes: expdir + network: true — the workspace-write sandbox stays,
        # only its network hole opens (research needs the web; experiments
        # need hubs and trackers). read-only and yolo have no such knob.
        if a.sandbox == "workspace-write" and getattr(a, "network", False):
            cmd += ["-c", "sandbox_workspace_write.network_access=true"]
    if a.model:
        cmd += ["--model", a.model]
    if not a.no_schema:
        cmd += ["--output-schema", str(d / "report.schema.json")]
    cmd += ["-o", str(d / "last-message.json"), "-"]
    return cmd


def git_root(path):
    r = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def _git(root, *args, env=None):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          text=True, env=env)


def _require_git(result, action):
    if result.returncode != 0:
        raise RuntimeError(f"{action} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _job_ref(kind, branch):
    return f"refs/codex/{kind}/{branch}"


def _snapshot_origin(root, branch, max_file_mb):
    """Create a commit object for the real working state without using its index."""
    try:
        max_file_mb = int(max_file_mb)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"snapshot_max_file_mb must be a non-negative integer: {exc}")
    if max_file_mb < 0:
        raise RuntimeError("snapshot_max_file_mb must be a non-negative integer")
    head = _require_git(_git(root, "rev-parse", "HEAD"), "resolve origin HEAD")
    tmp_dir = ROOT / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, index_name = tempfile.mkstemp(prefix="snapshot-index-", dir=tmp_dir)
    os.close(fd)
    os.unlink(index_name)  # read-tree expects a missing index, not an empty file.
    env = {**os.environ, "GIT_INDEX_FILE": index_name}
    try:
        _require_git(_git(root, "read-tree", head, env=env), "initialize snapshot index")
        status_result = _git(root, "status", "--porcelain", env=env)
        _require_git(status_result, "inspect origin state")
        status = status_result.stdout
        dirty_count = len(status.splitlines()) if status else 0
        base = head
        snapshot = None
        if dirty_count:
            _require_git(_git(root, "add", "-u", "--", ".", env=env),
                         "stage tracked snapshot files")
            untracked_result = _git(
                root, "ls-files", "--others", "--exclude-standard", "-z", env=env)
            _require_git(untracked_result, "list untracked snapshot files")
            untracked = untracked_result.stdout.split("\0")
            limit = max_file_mb * 1024 * 1024
            included, excluded = [], []
            for rel in filter(None, untracked):
                path = Path(root) / rel
                try:
                    size = path.lstat().st_size
                except OSError:
                    continue
                if size > limit:
                    excluded.append((rel, size))
                else:
                    included.append(rel)
            for start in range(0, len(included), 200):
                _require_git(_git(root, "add", "--", *included[start:start + 200], env=env),
                             "stage untracked snapshot files")
            if excluded:
                listing = ", ".join(f"{name} ({size} bytes)" for name, size in excluded)
                print("codex-exec: WARNING excluded oversized untracked file(s) from "
                      f"snapshot (limit {max_file_mb} MiB): {listing}", file=sys.stderr)
            tree = _require_git(_git(root, "write-tree", env=env), "write snapshot tree")
            commit = _git(
                root, "-c", "user.name=codex-exec",
                "-c", "user.email=codex-exec@local", "commit-tree", tree,
                "-p", head, "-m", f"codex-exec snapshot for {branch}", env=env,
            )
            snapshot = _require_git(commit, "create snapshot commit")
            base = snapshot
        _require_git(_git(root, "update-ref", _job_ref("base", branch), base),
                     "record snapshot base ref")
        return base, snapshot, dirty_count
    finally:
        try:
            Path(index_name).unlink()
        except FileNotFoundError:
            pass


def make_worktree(cwd, job_id, config=None):
    """Give a job its own checkout so parallel jobs cannot corrupt each other.

    Native subagents get this via isolation:'worktree'; external CLIs have no
    equivalent, so we build it here. Returns the checkout and snapshot metadata.
    """
    root = git_root(cwd)
    if not root:
        sys.exit(f"codex-exec: --worktree needs a git repo, and {cwd} is not in one.\n"
                 "  Run without --worktree and serialize jobs on this directory.")
    wt = ROOT / "worktrees" / job_id
    branch = f"codex-worker/{job_id}"
    c = config or cfg()
    try:
        base, snapshot, dirty_count = _snapshot_origin(
            root, branch, c.get("snapshot_max_file_mb", 50))
    except RuntimeError as exc:
        sys.exit(f"codex-exec: snapshot failed:\n{exc}")
    # Clear stale registrations from manually deleted worktrees before adding.
    subprocess.run(["git", "-C", root, "worktree", "prune"], capture_output=True)
    r = subprocess.run(["git", "-C", root, "worktree", "add", "-b", branch,
                        str(wt), base], capture_output=True, text=True)
    if r.returncode != 0:
        _git(root, "update-ref", "-d", _job_ref("base", branch))
        _git(root, "branch", "-D", branch)
        sys.exit(f"codex-exec: git worktree add failed:\n{r.stderr}")
    return str(wt), root, branch, snapshot, dirty_count, base


def launch_supervisor(job_id, d):
    sup = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "_run", job_id],
        stdout=subprocess.DEVNULL, stderr=(d / "supervisor.log").open("w"),
        start_new_session=True,
    )
    return sup.pid


def runner_sha():
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]
    except OSError:
        return None


def engine_version(engine):
    if engine in ENGINE_VERSION_CACHE:
        return ENGINE_VERSION_CACHE[engine]
    version = None
    try:
        result = subprocess.run([engine, "--version"], capture_output=True,
                                text=True, timeout=5)
        if result.returncode == 0 and result.stdout.splitlines():
            version = result.stdout.splitlines()[0].strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    ENGINE_VERSION_CACHE[engine] = version
    return version


def _duplicate_target(meta):
    return meta.get("origin") if meta.get("worktree") else meta.get("cwd")


def cmd_start(a):
    c = cfg()
    # Precedence: explicit flag > role row (via --role, or --profile mapped to
    # its role) > argv[0]/config default. dispatch pre-resolves and hands the
    # row in as a.resolved; a bare `start` resolves here.
    res = getattr(a, "resolved", None)
    role_name = getattr(a, "role", None)
    if res is None and (role_name or a.profile):
        res = resolve_role(role_name or role_for_profile(a.profile), cwd=a.cwd,
                           mode="role" if role_name else "legacy")
    hint = None
    a.network = False
    if res:
        a.network = bool(res["network"])
        a.engine = a.engine or res["engine"]
        a.model = a.model or res["model"]
        a.effort = a.effort or res["effort"]
        a.profile = a.profile or res["profile"]
        hint = res["sandbox_hint"]
        if res["mode"] == "role" and res["worktree"]:
            a.worktree = True
    a.engine = a.engine or a.default_engine
    if a.yolo:
        a.sandbox = "yolo"
    else:
        a.sandbox = a.sandbox or hint
    if not a.sandbox:
        a.sandbox = c["worktree_sandbox"] if a.worktree else c["default_sandbox"]

    brief = sys.stdin.read() if a.task_file in (None, "-") else Path(a.task_file).read_text()
    if not brief.strip():
        sys.exit("codex-exec: empty brief (pipe it on stdin or pass --task-file)")
    brief_sha = hashlib.sha256(brief.encode()).hexdigest()[:12]

    cwd = Path(a.cwd or os.getcwd()).resolve()
    if not cwd.is_dir():
        sys.exit(f"codex-exec: --cwd not a directory: {cwd}")

    # A read-only job cannot write, so there is nothing to isolate. Dying over
    # a missing git repo would stall recon that is safe to run anywhere.
    if a.worktree and a.sandbox == "read-only":
        print("codex-exec: NOTE read-only job — WORKTREE ignored (nothing to isolate)",
              file=sys.stderr)
        a.worktree = False

    target = (git_root(cwd) or str(cwd)) if a.worktree else str(cwd)
    if not getattr(a, "allow_duplicate", False):
        for other in running_jobs():
            om = read_json(other / "meta.json", {}) or {}
            if (om.get("brief_sha") == brief_sha
                    and _duplicate_target(om) == target):
                print("codex-exec: WARNING duplicate dispatch: job "
                      f"{other.name} is already running this brief in {target}",
                      file=sys.stderr)

    # Two jobs writing one tree corrupt each other; there is no worktree
    # isolation here the way native subagents have it.
    # Only writers can collide. A read-only job sharing the tree is harmless,
    # and warning about it would stall recon that is safe to run.
    if not a.worktree and a.sandbox != "read-only":
        for other in running_jobs():
            om = read_json(other / "meta.json", {})
            if om.get("cwd") == str(cwd) and om.get("sandbox") != "read-only":
                print(f"codex-exec: WARNING job {other.name} is already running in "
                      f"{cwd}; concurrent writes to one tree will conflict. "
                      "Use --worktree to isolate them.", file=sys.stderr)

    job_id = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    d = JOBS / job_id
    d.mkdir(parents=True)
    write_json(d / "report.schema.json", REPORT_SCHEMA)

    origin, worktree, branch = str(cwd), None, None
    snapshot_commit, origin_dirty_at_start = None, 0
    if a.worktree:
        (worktree, repo_root, branch, snapshot_commit,
         origin_dirty_at_start, _) = make_worktree(cwd, job_id, c)
        # The job runs in the isolated checkout; the original tree is untouched.
        cwd, origin = Path(worktree), repo_root
    contract = ""
    if a.sandbox != "read-only":
        contract = WRITER_CONTRACT.format(
            isolation=ISO_WORKTREE if worktree else ISO_DIRECT,
            commit_exception=("" if worktree
                              else " (unless the task explicitly instructs one)"),
        )

    cmd = build_cmd(a, d, cwd)
    task = contract + brief
    (d / "task.md").write_text(task)

    base = subprocess.run(["git", "-C", str(cwd), "rev-parse", "HEAD"],
                          capture_output=True, text=True)
    write_json(d / "meta.json", {
        "job_id": job_id, "engine": a.engine, "cwd": str(cwd),
        "sandbox": a.sandbox, "model": a.model, "effort": a.effort,
        "profile": a.profile, "label": a.label,
        "role": res["role"] if res else None,
        "dispatch_mode": res["mode"] if res else None,
        "backend": res["backend"] if res else None,
        "fallback": bool(res["fallback"]) if res else False,
        "kind": res["kind"] if res else None,
        "writes": res["writes"] if res else None,
        "deliverable": res["deliverable"] if res else None,
        "verdict_level": res["verdict_level"] if res else None,
        "tables_sha": tables_sha() if res else None,
        "worktree": worktree, "origin": origin, "branch": branch,
        "base_commit": base.stdout.strip() if base.returncode == 0 else None,
        "snapshot_commit": snapshot_commit,
        "origin_dirty_at_start": origin_dirty_at_start,
        "resume_of": a.resume, "max_seconds": a.max_seconds,
        "network": a.network, "stdin_brief": True, "cmd": cmd,
        # The brief's header is stripped before task.md is written, so without
        # this a bad TIER/WORKTREE line leaves no trace in the job directory.
        "directives": getattr(a, "directives", None),
        "source": res["_source"] if res else None,
        "runner_sha": runner_sha(),
        "engine_version": engine_version(a.engine),
        "config_snapshot": {
            key: c.get(key) for key in (
                "default_sandbox", "worktree_sandbox", "snapshot_max_file_mb")
        },
        "brief_sha": brief_sha,
    })
    write_json(d / "status.json", {"state": "starting", "started_at": time.time()})

    (d / "supervisor.pid").write_text(str(launch_supervisor(job_id, d)))
    print(job_id)


def cmd_run(a):
    """Internal: detached supervisor. Runs the engine to completion."""
    d = job_dir(a.job)
    meta = read_json(d / "meta.json", {})
    started = time.time()
    proc = None
    handling_signal = False

    def lost_on_signal(signum, _frame):
        nonlocal handling_signal
        if handling_signal:
            return
        handling_signal = True
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        ended = time.time()
        status = read_json(d / "status.json", {}) or {}
        status.update({
            "state": "lost", "lost_reason": f"signal {signum}",
            "lost_detected_at": ended, "ended_at": ended,
            "duration_sec": round(ended - started, 1),
        })
        _record_result_commit(meta, status)
        write_json(d / "status.json", status)
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, lost_on_signal)
    signal.signal(signal.SIGHUP, lost_on_signal)
    with (d / "events.jsonl").open("w") as out, (d / "stderr.log").open("w") as err:
        proc = subprocess.Popen(
            meta["cmd"], cwd=meta["cwd"],
            stdin=subprocess.PIPE if meta.get("stdin_brief") else subprocess.DEVNULL,
            stdout=out, stderr=err, start_new_session=True,
            # Every shell the engine spawns inherits this; adopt/drop check it
            # and refuse, so a worker cannot merge its own (or a sibling's) work.
            env={**os.environ, "CODEX_EXEC_JOB": a.job},
        )
        (d / "engine.pid").write_text(str(proc.pid))
        write_json(d / "status.json",
                   {"state": "running", "started_at": started, "pid": proc.pid})
        payload = (d / "task.md").read_text().encode() if meta.get("stdin_brief") else None
        try:
            proc.communicate(input=payload, timeout=meta.get("max_seconds") or None)
            state = "done" if proc.returncode == 0 else "failed"
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
            proc.wait()
            state = "timeout"

    final_status = {
        "state": state, "exit_code": proc.returncode,
        "started_at": started, "ended_at": time.time(),
        "duration_sec": round(time.time() - started, 1),
        # A resumed run keeps the same session id; when the events do not
        # advertise it again, the id we resumed from is still the right one.
        "session_id": find_session_id(d) or meta.get("resume_of"),
    }
    if meta.get("worktree"):
        _record_result_commit(meta, final_status)
    else:
        final_status["result_commit"] = None
    write_json(d / "status.json", final_status)
    try:
        ledger_settle(meta, final_status)
    except OSError as exc:
        print(f"agent-exec: ledger row not written: {exc}", file=sys.stderr)


# ---------------------------------------------------------------- report

def strip_bookkeeping(report):
    """Engines that write their own report file tend to list it as a changed
    file. That is our plumbing, not the user's diff."""
    if isinstance(report, dict) and isinstance(report.get("files_changed"), list):
        report["files_changed"] = [
            f for f in report["files_changed"]
            if not str(f.get("path", "")).startswith(str(ROOT))
        ]
    return report


def load_report(d: Path):
    """codex writes last-message.json; report.json is read first for older jobs
    whose engine wrote its own report file."""
    for name in ("report.json", "last-message.json"):
        p = d / name
        if p.exists() and p.stat().st_size:
            text = p.read_text().strip()
            try:
                return strip_bookkeeping(json.loads(text)), None
            except json.JSONDecodeError:
                # Some models fence the JSON despite instructions.
                m = re.search(r"\{.*\}", text, re.S)
                if m:
                    try:
                        return json.loads(m.group(0)), None
                    except json.JSONDecodeError:
                        pass
                return None, text
    return None, None


def verify(d: Path, report: dict):
    """Deterministic reconciliation of the engine's claims against the filesystem.

    This is the check that would otherwise cost a model several tool calls and
    still be guessy. Returns a list of discrepancy strings."""
    meta = read_json(d / "meta.json", {})
    cwd = Path(meta.get("cwd", "."))
    issues, claimed = [], set()

    for f in (report or {}).get("files_changed", []):
        p = Path(f.get("path", ""))
        if not p.is_absolute():
            p = cwd / p
        claimed.add(str(p.resolve()))
        exists = p.exists()
        if f.get("change") == "deleted" and exists:
            issues.append(f"claimed deleted but still present: {p}")
        elif f.get("change") != "deleted" and not exists:
            issues.append(f"claimed {f.get('change')} but not on disk: {p}")

    if shutil.which("git"):
        r = subprocess.run(["git", "-C", str(cwd), "status", "--porcelain"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            actual = set()
            for line in r.stdout.splitlines():
                if len(line) > 3:
                    actual.add(str((cwd / line[3:].strip().split(" -> ")[-1]).resolve()))
            # Build artifacts are never a meaningful diff; flagging them just
            # trains the reader to ignore this section.
            unclaimed = {p for p in actual - claimed
                         if not any(n in p for n in NOISE_MATCH)}
            if unclaimed:
                sample = ", ".join(sorted(unclaimed)[:5])
                issues.append(f"{len(unclaimed)} file(s) changed in git but not reported: {sample}")

    v = (report or {}).get("verification", "").strip().lower()
    if v in ("", "none", "n/a", "nothing"):
        issues.append("nothing was verified — the summary is an unproven claim")
    return issues


def _patch_excludes():
    return ([f":(exclude)**/{n}/**" for n in NOISE_DIRS]
            + [":(exclude)**/*.pyc"])


def _stage_worktree(wt):
    result = _git(wt, "add", "-A", "--", ".", *_patch_excludes())
    if result.returncode != 0:
        raise RuntimeError(f"git add failed: {result.stderr.strip()}")


def commit_worktree_result(meta):
    """Commit a worktree result on its worker branch without changing job state."""
    wt = meta.get("worktree")
    if not wt or not Path(wt).is_dir():
        return None, "worktree is missing"
    try:
        _stage_worktree(wt)
    except RuntimeError as exc:
        return None, str(exc)
    changed = _git(wt, "diff", "--cached", "--quiet", "HEAD")
    if changed.returncode == 0:
        return None, None
    if changed.returncode != 1:
        return None, f"inspect staged result failed: {changed.stderr.strip()}"
    commit = _git(
        wt, "-c", "user.name=codex-exec", "-c", "user.email=codex-exec@local",
        "commit", "--no-verify", "-m", f"codex-exec result for {meta.get('job_id', 'job')}",
    )
    if commit.returncode != 0:
        return None, commit.stderr.strip() or commit.stdout.strip() or "git commit failed"
    head = _git(wt, "rev-parse", "HEAD")
    if head.returncode != 0:
        return None, f"resolve result commit failed: {head.stderr.strip()}"
    return head.stdout.strip(), None


def _resolve_patch_base(meta):
    """Return (revision expression, sha), retaining HEAD fallback for legacy jobs."""
    wt = meta.get("worktree")
    branch = meta.get("branch")
    if wt and branch:
        for kind in ("adopted", "base"):
            ref = _job_ref(kind, branch)
            resolved = _git(wt, "rev-parse", "--verify", "--quiet", ref)
            if resolved.returncode == 0:
                return ref, resolved.stdout.strip()
    head = _git(wt, "rev-parse", "HEAD") if wt else None
    return "HEAD", (head.stdout.strip() if head and head.returncode == 0 else None)


def make_patch(d: Path, meta: dict):
    """Snapshot an isolated worktree's work as an applyable patch + diffstat.

    Staging first is what makes new files show up in the diff. The worktree is
    disposable, so staging in it costs nothing.
    """
    wt = meta.get("worktree")
    if not wt or not Path(wt).is_dir():
        return None, None
    _stage_worktree(wt)
    base, _ = _resolve_patch_base(meta)
    # --binary so genuinely binary files (images, fixtures) still apply cleanly.
    patch_result = _git(wt, "diff", "--cached", "--binary", base)
    _require_git(patch_result, "build worktree patch")
    patch = patch_result.stdout
    stat_result = _git(wt, "diff", "--cached", "--stat", base)
    _require_git(stat_result, "build worktree diffstat")
    stat = stat_result.stdout.strip()
    (d / "changes.patch").write_text(patch)
    return (d / "changes.patch"), stat


def cmd_diff(a):
    d = job_dir(a.job)
    p, _ = make_patch(d, read_json(d / "meta.json", {}))
    if not p:
        print("(no patch — job did not use --worktree)")
        return 0
    patch_bytes = p.read_bytes()
    (d / "diff-viewed").write_text(hashlib.sha256(patch_bytes).hexdigest())
    sys.stdout.write(patch_bytes.decode())
    return 0


def refuse_inside_worker(what):
    job = os.environ.get("CODEX_EXEC_JOB")
    if job:
        sys.exit(f"codex-exec: {what} refused — this shell belongs to worker job "
                 f"{job}. Only the supervising orchestrator reviews and applies "
                 "work. Finish your task and report; do not manage patches.")


def cmd_adopt(a):
    # The orchestrator holds a rotating secret in the lock file (mode 0600) and
    # passes it as CODEX_ADOPT_TOKEN. A dispatched driver has neither, so a
    # driver that "helpfully" adopts is refused even if it reads this source.
    LOCK = ROOT / "ORCHESTRATOR_ADOPT_LOCK"
    if LOCK.exists():
        want = LOCK.read_text().strip()
        if not want or os.environ.get("CODEX_ADOPT_TOKEN", "") != want:
            sys.exit("codex-exec adopt refused: only the supervising "
                     "orchestrator applies patches. Report the job id instead.")
    """Apply an isolated job's work onto the original tree. Caller's decision.

    Atomic: `git apply` (no --3way) verifies every hunk before touching any
    file, so a conflicted patch applies NOTHING. The --3way fallback we used
    before could half-apply — a deletion landed while the matching content
    merge was skipped, silently losing work. Never again."""
    refuse_inside_worker("adopt")
    d = job_dir(a.job)
    meta = read_json(d / "meta.json", {})
    if status_of(d).get("state") not in ("done", "failed", "timeout", "killed", "lost"):
        sys.exit(f"codex-exec: job {a.job} has not finished; wait before adopt")
    patch, _ = make_patch(d, meta)
    if not patch or not patch.stat().st_size:
        sys.exit("codex-exec: nothing to adopt (no worktree, or no changes)")
    patch_sha = hashlib.sha256(patch.read_bytes()).hexdigest()
    viewed = d / "diff-viewed"
    if (not getattr(a, "force", False)
            and (not viewed.exists() or viewed.read_text().strip() != patch_sha)):
        sys.exit(f"codex-exec: patch has not been reviewed or changed since review; "
                 f"run codex-exec diff {a.job} before adopt")
    _, from_sha = _resolve_patch_base(meta)
    head = _require_git(_git(meta["worktree"], "rev-parse", "HEAD"),
                        "resolve worker result")
    r = subprocess.run(["git", "-C", meta["origin"], "apply", str(patch)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        write_json(d / "adopt.json", {"result": "conflict", "stderr": r.stderr})
        base = (meta.get("base_commit") or "?")[:10]
        sid = status_of(d).get("session_id")
        sys.exit(f"codex-exec: patch conflicts with {meta['origin']} — NOTHING was "
                 f"applied (adopt is atomic).\n{r.stderr}"
                 f"The tree has moved since the job's base commit {base}.\n"
                 f"  Rebase the worker onto the current tree: new job with "
                 f"RESUME: {sid or '<session>'}\n"
                 f"  Or merge by hand from {patch} (kept permanently).\n"
                 f"  Worktree intact at {meta['worktree']}")
    ref_update = _git(meta["origin"], "update-ref", _job_ref("adopted", meta["branch"]), head)
    if ref_update.returncode != 0:
        rollback = _git(meta["origin"], "apply", "--reverse", str(patch))
        detail = f"adopted ref update failed: {ref_update.stderr.strip()}"
        if rollback.returncode == 0:
            detail += "; applied patch was reversed"
        else:
            detail += ("; WARNING reverse apply also failed and origin may be changed: "
                       f"{rollback.stderr.strip()}")
        write_json(d / "adopt.json", {"result": "conflict", "stderr": detail})
        sys.exit(f"codex-exec: {detail}")
    (d / "adopted").write_text(str(time.time()))
    write_json(d / "adopt.json", {
        "result": "applied", "range": f"{from_sha}..{head}", "at": time.time(),
    })
    ledger_upsert(a.job, disposition="ADOPT")
    print(f"applied {patch} onto {meta['origin']}")
    if getattr(a, "drop", False):
        try:
            _cleanup_worktree(d, meta)
        except RuntimeError as exc:
            sys.exit(f"codex-exec: applied patch, but cleanup was incomplete:\n{exc}")
        print(f"removed adopted worktree {meta['worktree']} and branch {meta['branch']}\n"
              f"changes.patch retained at {d / 'changes.patch'}")
    else:
        print(f"worktree still at {meta['worktree']} — remove with: "
              f"codex-exec drop {a.job}")
    return 0


def _cleanup_worktree(d, meta):
    """Remove a worktree, branch, and optional bookkeeping refs."""
    errors = []
    origin, worktree, branch = meta.get("origin"), meta.get("worktree"), meta.get("branch")
    if not origin:
        raise RuntimeError("job metadata has no origin")
    if worktree and Path(worktree).exists():
        removed = _git(origin, "worktree", "remove", "--force", worktree)
        if removed.returncode != 0:
            errors.append(removed.stderr.strip())
    if branch:
        branch_ref = f"refs/heads/{branch}"
        exists = _git(origin, "rev-parse", "--verify", "--quiet", branch_ref)
        if exists.returncode == 0:
            deleted = _git(origin, "branch", "-D", branch)
            if deleted.returncode != 0:
                errors.append(deleted.stderr.strip())
        for kind in ("base", "adopted"):
            deleted_ref = _git(origin, "update-ref", "-d", _job_ref(kind, branch))
            if deleted_ref.returncode != 0:
                errors.append(deleted_ref.stderr.strip())
    if errors:
        raise RuntimeError("\n".join(filter(None, errors)))


def cmd_drop(a):
    refuse_inside_worker("drop")
    d = job_dir(a.job)
    meta = read_json(d / "meta.json", {})
    if not meta.get("worktree"):
        print("(no worktree)")
        return 0
    # Dropping unadopted work destroys it. Make that an explicit choice.
    patch, _ = make_patch(d, meta)
    if patch and patch.stat().st_size and not (d / "adopted").exists() and not a.force:
        sys.exit(f"codex-exec: job {a.job} has unadopted changes "
                 f"({patch.stat().st_size} bytes of patch) and dropping discards them.\n"
                 f"  Review: codex-exec diff {a.job}\n"
                 f"  Keep:   codex-exec adopt {a.job}\n"
                 f"  Discard anyway: codex-exec drop {a.job} --force")
    try:
        _cleanup_worktree(d, meta)
    except RuntimeError as exc:
        sys.exit(f"codex-exec: incomplete worktree cleanup:\n{exc}")
    # The patch is the only recovery material once the worktree is gone. Keep it.
    ledger_upsert(a.job, disposition="DROP")
    print(f"removed worktree {meta['worktree']} and branch {meta['branch']}\n"
          f"changes.patch retained at {d / 'changes.patch'}")
    return 0


def cmd_gc(a):
    refuse_inside_worker("gc")
    if a.older_than < 0:
        sys.exit("codex-exec: --older-than must be non-negative")
    cutoff = time.time() - (a.older_than * 3600)
    dropped = skipped = errors = 0
    if not JOBS.is_dir():
        print("summary dropped=0 skipped=0 errors=0")
        return 0
    for d in sorted(JOBS.iterdir()):
        if not d.is_dir():
            continue
        meta = read_json(d / "meta.json", {}) or {}
        worktree = meta.get("worktree")
        if not worktree or not Path(worktree).is_dir():
            continue
        status = status_of(d, persist=not a.dry_run)
        adopt = read_json(d / "adopt.json", {}) or {}
        adopted = adopt.get("result") == "applied" or (d / "adopted").exists()
        ended_at = status.get("ended_at")
        age_at = ended_at if isinstance(ended_at, (int, float)) else adopt.get("at")
        if not isinstance(age_at, (int, float)) or age_at > cutoff:
            print(f"skip {d.name}: worktree is newer than threshold or age is unknown")
            skipped += 1
            continue
        if not adopted and not a.force:
            print(f"skip {d.name}: unadopted worktree {worktree}")
            skipped += 1
            continue
        action = "would drop" if a.dry_run else "drop"
        print(f"{action} {d.name}: {worktree}")
        if a.dry_run:
            dropped += 1
            continue
        try:
            make_patch(d, meta)
            _cleanup_worktree(d, meta)
            dropped += 1
        except (RuntimeError, OSError) as exc:
            print(f"error {d.name}: {exc}")
            errors += 1
    label = "would_drop" if a.dry_run else "dropped"
    print(f"summary {label}={dropped} skipped={skipped} errors={errors}")
    return 1 if errors else 0


# ---------------------------------------------------------------- ledger v2
#
# Line C: every job gets a row the moment it settles, written by the runner from
# meta/status, so the identity columns can never be misfiled by hand. verdict,
# adopt and drop fill the judgement columns of the same row later.

LEDGER_V2 = Path(os.environ.get("AGENT_EXEC_LEDGER",
                                Path.home() / ".claude" / "codex-ledger-v2.tsv"))
LEDGER_COLS = ("date", "job_id", "session", "role", "backend", "model", "effort", "kind",
               "writes", "worktree", "mode", "fallback", "state", "wall_min", "cwd",
               "verdict", "fail_class", "disposition", "notes")


def ledger_upsert(job_id, **fields):
    """Add or update the v2 ledger row for job_id. None values are left alone."""
    fields = {k: str(v).replace("\t", " ").replace("\n", " ")
              for k, v in fields.items() if v is not None}
    LEDGER_V2.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER_V2.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        lines = LEDGER_V2.read_text().splitlines() if LEDGER_V2.exists() else []
        rows = [dict(zip(LEDGER_COLS, l.split("\t"))) for l in lines[1:] if l.strip()]
        hit = next((r for r in rows if r.get("job_id") == job_id), None)
        if hit is None:
            hit = {c: "" for c in LEDGER_COLS}
            hit["job_id"] = job_id
            rows.append(hit)
        hit.update(fields)
        LEDGER_V2.write_text("\t".join(LEDGER_COLS) + "\n" + "".join(
            "\t".join(r.get(c, "") for c in LEDGER_COLS) + "\n" for r in rows))


def ledger_settle(meta, status):
    """The identity half of the row, from what the runner itself recorded."""
    ledger_upsert(meta["job_id"],
                  date=time.strftime("%Y-%m-%d"), session=status.get("session_id") or "",
                  role=meta.get("role") or "", backend=meta.get("backend") or "",
                  model=meta.get("model") or "", effort=meta.get("effort") or "",
                  kind=meta.get("kind") or "", writes=meta.get("writes") or "",
                  worktree="Y" if meta.get("worktree") else "N",
                  mode=meta.get("dispatch_mode") or "", fallback="Y" if meta.get("fallback") else "N",
                  state=status.get("state") or "",
                  wall_min=f"{(status.get('duration_sec') or 0) / 60:.1f}",
                  cwd=meta.get("origin") or meta.get("cwd") or "")


def cmd_verdict(a):
    d = job_dir(a.job)
    write_json(d / "verdict.json", {
        "verdict": a.verdict,
        "fail_class": a.fail_class,
        "rework": a.rework,
        "review_min": a.review_min,
        "solo_estimate_min": a.solo_estimate_min,
        "notes": a.notes,
        "at": time.time(),
    })
    ledger_upsert(a.job, verdict=a.verdict, fail_class=a.fail_class or "-", notes=a.notes)
    print(f"recorded {a.verdict} verdict for {a.job}")
    return 0


REPORT_COLUMNS = (
    "group", "n", "done", "failed", "timeout", "killed", "lost", "done_pct",
    "median_duration_sec", "median_command_execution", "median_input_tokens",
    "median_output_tokens", "resume_pct", "worktree_pct", "snapshot_pct",
    "adopt_applied", "adopt_conflict", "diff_before_adopt_pct",
    "median_adopt_latency_sec", "PASS", "FAIL", "PARTIAL", "BLOCKED", "none",
    "rework_sum", "median_review_min",
)


def _strict_json(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _job_date(d, status):
    stamp = status.get("started_at")
    if isinstance(stamp, (int, float)):
        return datetime.datetime.fromtimestamp(stamp).date()
    try:
        return datetime.datetime.strptime(d.name[:15], "%Y%m%dT%H%M%S").date()
    except ValueError:
        return None


def _event_metrics(d):
    commands = input_tokens = output_tokens = 0
    try:
        lines = (d / "events.jsonl").read_text().splitlines()
    except OSError:
        return commands, input_tokens, output_tokens
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if (event.get("type") == "item.completed"
                and (event.get("item") or {}).get("type") == "command_execution"):
            commands += 1
        if event.get("type") == "turn.completed":
            usage = event.get("usage") or {}
            input_tokens += usage.get("input_tokens") or 0
            output_tokens += usage.get("output_tokens") or 0
    return commands, input_tokens, output_tokens


def _pct(part, total):
    return round(100.0 * part / total, 1) if total else 0.0


def _med(values):
    if not values:
        return None
    value = median(values)
    return int(value) if float(value).is_integer() else round(value, 1)


def _report_group(name, jobs):
    states = {state: 0 for state in ("done", "failed", "timeout", "killed", "lost")}
    durations, commands, inputs, outputs = [], [], [], []
    adopt_latencies, reviews = [], []
    verdicts = {key: 0 for key in ("PASS", "FAIL", "PARTIAL", "BLOCKED", "none")}
    resumes = worktrees = snapshots = applied = conflicts = adopted_count = diffed = rework = 0
    for d, meta, status in jobs:
        state = status.get("state")
        if state in states:
            states[state] += 1
        if isinstance(status.get("duration_sec"), (int, float)):
            durations.append(status["duration_sec"])
        command_count, input_count, output_count = _event_metrics(d)
        commands.append(command_count)
        inputs.append(input_count)
        outputs.append(output_count)
        resumes += bool(meta.get("resume_of"))
        is_worktree = bool(meta.get("worktree"))
        worktrees += is_worktree
        snapshots += bool(is_worktree and meta.get("snapshot_commit"))
        adopt = _strict_json(d / "adopt.json") or {}
        is_adopted = adopt.get("result") == "applied" or (d / "adopted").exists()
        adopted_count += is_adopted
        if adopt.get("result") == "applied":
            applied += 1
        elif adopt.get("result") == "conflict":
            conflicts += 1
        diffed += bool(is_adopted and (d / "diff-viewed").exists())
        if (isinstance(adopt.get("at"), (int, float))
                and isinstance(status.get("ended_at"), (int, float))):
            adopt_latencies.append(adopt["at"] - status["ended_at"])
        verdict = _strict_json(d / "verdict.json") or {}
        result = verdict.get("verdict")
        verdicts[result if result in verdicts else "none"] += 1
        if isinstance(verdict.get("rework"), (int, float)):
            rework += verdict["rework"]
        if isinstance(verdict.get("review_min"), (int, float)):
            reviews.append(verdict["review_min"])
    n = len(jobs)
    row = {"group": name, "n": n, **states}
    row.update({
        "done_pct": _pct(states["done"], n),
        "median_duration_sec": _med(durations),
        "median_command_execution": _med(commands),
        "median_input_tokens": _med(inputs),
        "median_output_tokens": _med(outputs),
        "resume_pct": _pct(resumes, n),
        "worktree_pct": _pct(worktrees, n),
        "snapshot_pct": _pct(snapshots, n),
        "adopt_applied": applied, "adopt_conflict": conflicts,
        "diff_before_adopt_pct": _pct(diffed, adopted_count),
        "median_adopt_latency_sec": _med(adopt_latencies),
        **verdicts, "rework_sum": rework, "median_review_min": _med(reviews),
    })
    return row


def _parse_report_date(value, option):
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        sys.exit(f"codex-exec: {option} must be YYYY-MM-DD: {value}")


def cmd_report(a):
    since = _parse_report_date(
        a.since or str(datetime.date.today() - datetime.timedelta(days=7)), "--since")
    until = _parse_report_date(a.until, "--until") if a.until else None
    groups, skipped = {}, 0
    if JOBS.is_dir():
        for d in sorted(JOBS.iterdir()):
            if not d.is_dir():
                continue
            meta = _strict_json(d / "meta.json")
            raw_status = _strict_json(d / "status.json")
            if meta is None or raw_status is None:
                skipped += 1
                continue
            status = status_of(d, persist=False)
            day = _job_date(d, status)
            if day is None or day < since or (until is not None and day > until):
                continue
            if a.by == "day":
                group = str(day)
            else:
                group = meta.get(a.by)
                group = str(group) if group not in (None, "") else "(none)"
            groups.setdefault(group, []).append((d, meta, status))
    rows = [_report_group(name, groups[name]) for name in sorted(groups)]
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        print(f"skipped={skipped}", file=sys.stderr)
        return 0
    if a.tsv:
        print("\t".join(REPORT_COLUMNS))
        for row in rows:
            print("\t".join("" if row[key] is None else str(row[key])
                            for key in REPORT_COLUMNS))
    else:
        for row in rows:
            values = " ".join(f"{key}={row[key]}" for key in REPORT_COLUMNS[1:])
            print(f"{row['group']}: {values}")
    print(f"skipped={skipped}")
    return 0


def cmd_digest(a):
    """The handoff artifact: compact, deterministic, cheap to read."""
    d = job_dir(a.job)
    st, meta = status_of(d), read_json(d / "meta.json", {})
    c = cfg()
    report, raw = load_report(d)
    L = []

    head = f"{st.get('state', '?')} in {st.get('duration_sec', '?')}s"
    L.append(f"## {meta.get('engine', 'codex')} job {a.job} — {head}")
    access = meta.get("sandbox")
    tier = "/".join(x for x in (meta.get("model"), meta.get("effort")) if x)
    L.append(f"cwd `{meta.get('cwd')}`"
             + (f" · role `{meta['role']}`" if meta.get("role") else "")
             + (f" · profile `{meta['profile']}`" if meta.get("profile") else "")
             + (f" · {tier}" if tier else "")
             + (f" · backend `{meta['backend']}`" if meta.get("backend") else "")
             + (" · **FALLBACK**" if meta.get("fallback") else "")
             + f" · access `{access}`")
    if st.get("session_id"):
        L.append(f"session `{st['session_id']}` — follow up with "
                 f"`--resume {st['session_id']}`")
    L.append("")

    if report:
        L.append(f"**Summary** {report.get('summary', '(none)')}")
        files = report.get("files_changed", [])
        if files:
            L.append(f"\n**Files ({len(files)})**")
            mark = {"created": "+", "modified": "~", "deleted": "-"}
            for f in files[:c["digest_max_files"]]:
                L.append(f"  {mark.get(f.get('change'), '?')} {f.get('path')}"
                         f" — {(f.get('why') or '')[:70]}")
            if len(files) > c["digest_max_files"]:
                L.append(f"  … {len(files) - c['digest_max_files']} more (see result JSON)")
        else:
            L.append("\n**Files** none")
        L.append(f"\n**Verification** {report.get('verification', '(not reported)')[:400]}")
        for key, label in (("incomplete", "Incomplete"), ("risks", "Risks")):
            items = report.get(key) or []
            if items:
                L.append(f"\n**{label}**")
                L.extend(f"  - {str(i)[:200]}" for i in items)
        issues = verify(d, report)
        if issues:
            L.append("\n**⚠ Discrepancies** (checked against disk, not self-reported)")
            L.extend(f"  - {i}" for i in issues)
    elif raw:
        L.append("**No structured report.** Raw final message:\n")
        L.append(raw[:1500])
    else:
        L.append("**No report produced.**")

    if meta.get("worktree"):
        _, stat = make_patch(d, meta)
        base = (meta.get("base_commit") or "")[:10]
        L.append(f"\n**Isolated worktree** — the original tree at "
                 f"`{meta['origin']}` is untouched."
                 + (f" Based on commit `{base}`." if base else ""))
        L.append(f"```\n{stat or '(no changes)'}\n```")
        L.append(f"Review `codex-exec diff {a.job}` · take it with "
                 f"`codex-exec adopt {a.job}` · discard with `codex-exec drop {a.job}`")

    if st.get("state") in ("failed", "timeout"):
        err = (d / "stderr.log")
        if err.exists() and err.stat().st_size:
            L.append(f"\n**stderr**\n```\n{err.read_text()[-800:]}\n```")

    L.append(f"\n_full: `codex-exec result {a.job}` · `codex-exec log {a.job}` · {d}_")

    text = "\n".join(L)
    (d / "REPORT.md").write_text(text)
    print(text)
    return 0 if st.get("state") == "done" else 1


DIRECTIVES = ("CWD", "TIER", "LABEL", "WORKTREE", "RESUME", "MAX_SECONDS")
DIRECTIVE_RE = re.compile(
    r"^\s*(CWD|TIER|LABEL|WORKTREE|RESUME|MAX_SECONDS)\s*:\s*(.*)$", re.I)


def parse_directives(brief):
    """Split a brief into (directives, body). Directives are `KEY: value` lines
    at the very top; parsing is deterministic so the haiku driver never has to
    interpret the brief at all."""
    d = {}
    lines = brief.splitlines()
    i = 0
    saw_directive = False
    while i < len(lines):
        match = DIRECTIVE_RE.match(lines[i])
        if match:
            d[match.group(1).upper()] = match.group(2).strip()
            saw_directive = True
            i += 1
            continue
        if saw_directive and not lines[i].strip():
            i += 1
            continue
        else:
            break
    body = "\n".join(lines[i:]).lstrip("\n")
    return d, body


def validate_body_directives(body):
    """Reject misplaced near-top directives and warn about later lookalikes."""
    for index, line in enumerate(body.splitlines()):
        if not DIRECTIVE_RE.match(line):
            continue
        quoted = repr(line)
        if index < 30:
            sys.exit(f"codex-exec: misplaced directive {quoted}; directives must be "
                     "at the very top of the brief")
        print(f"codex-exec: WARNING possible misplaced directive {quoted}; directives "
              "must be at the very top of the brief", file=sys.stderr)


def _wait_for(job, timeout, poll=5):
    d = job_dir(job)
    deadline = time.time() + timeout
    while True:
        st = status_of(d)
        if st["state"] == "lost":
            return 1
        if st["state"] in ("done", "failed", "timeout", "killed"):
            return 0
        if time.time() >= deadline:
            return 2
        time.sleep(poll)


def _digest_or_running(job, timeout):
    result = _wait_for(job, timeout)
    if result == 2:
        print(f"STILL RUNNING: {job}")
        print(f"Next: codex-exec finish {job}")
        return 0
    cmd_digest(argparse.Namespace(job=job))
    return 1 if result == 1 else 0


def cmd_dispatch(a):
    """One-shot driver entry: directive-headed brief in, digest out.

    The brief itself carries CWD/TIER/LABEL/WORKTREE/RESUME as header lines, so
    the calling agent forwards it verbatim and makes zero decisions."""
    brief = sys.stdin.read() if a.task_file in (None, "-") else Path(a.task_file).read_text()
    dirs, body = parse_directives(brief)
    validate_body_directives(body)
    if not body.strip():
        sys.exit("codex-exec: brief has no body after directives")

    role_name = getattr(a, "role", None)
    mode = "role" if role_name else "legacy"
    if not role_name:
        if not a.profile:
            sys.exit("agent-exec: dispatch needs --role ROLE (or the legacy --profile NAME)")
        role_name = role_for_profile(a.profile)
    res = resolve_role(role_name, cwd=dirs.get("CWD") or os.getcwd(), dirs=dirs, mode=mode)
    if mode == "role":
        missing = check_brief_requires(res, body)
        if missing:
            sys.exit(f"agent-exec: refused — a brief for role {role_name} must contain "
                     f"{', '.join(res['brief_requires'])}; missing: {', '.join(missing)}")
    for n in res["_notes"]:
        print(f"agent-exec: NOTE {n}", file=sys.stderr)

    model, effort = resolve_tier(dirs["TIER"]) if dirs.get("TIER") else (None, None)

    label = dirs.get("LABEL") or " ".join(
        next((l for l in body.splitlines() if l.strip()), "job").lstrip("# ").split()[:5])

    import tempfile as _tf
    with _tf.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write(body)
        body_path = fh.name
    ns = argparse.Namespace(
        profile=a.profile or res["profile"], role=role_name, resolved=res, directives=dirs,
        engine=None, default_engine=a.default_engine,
        worktree=res["worktree"],
        cwd=dirs.get("CWD") or None, sandbox=None, yolo=False,
        model=model, effort=effort, label=label,
        resume=dirs.get("RESUME") or None, max_seconds=int(dirs.get("MAX_SECONDS") or 3600),
        no_schema=False, task_file=body_path,
        allow_duplicate=getattr(a, "allow_duplicate", False),
    )
    import io, contextlib
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            cmd_start(ns)
    finally:
        os.unlink(body_path)
    job = buf.getvalue().strip().splitlines()[-1]
    print(f"JOB={job}")
    return _digest_or_running(job, a.timeout)


def cmd_finish(a):
    """Wait for a job and print its digest, or say it is still running."""
    return _digest_or_running(a.job, a.timeout)


def cmd_wait(a):
    d = job_dir(a.job)
    deadline = time.time() + a.timeout
    while True:
        st = status_of(d)
        if st["state"] in ("done", "failed", "timeout", "killed", "lost"):
            print(f"{st['state']} ({st.get('duration_sec', '?')}s)")
            return 1 if st["state"] == "lost" else 0
        if time.time() >= deadline:
            elapsed = round(time.time() - st.get("started_at", time.time()))
            print(f"running ({elapsed}s elapsed)")
            return 2
        time.sleep(a.poll)


def cmd_result(a):
    d = job_dir(a.job)
    st, meta = status_of(d), read_json(d / "meta.json", {})
    report, raw = load_report(d)
    out = {"job_id": a.job, "engine": meta.get("engine"), "state": st.get("state"),
           "exit_code": st.get("exit_code"), "duration_sec": st.get("duration_sec"),
           "cwd": meta.get("cwd"), "session_id": st.get("session_id")}
    if report:
        out["report"] = report
        out["discrepancies"] = verify(d, report)
    elif raw:
        out["report_text"] = raw
    if st.get("state") in ("failed", "timeout"):
        err = (d / "stderr.log")
        if err.exists():
            out["stderr_tail"] = err.read_text()[-2000:]
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if st.get("state") == "done" else 1


def cmd_log(a):
    events = job_dir(a.job) / "events.jsonl"
    if not events.exists():
        print("(no events yet)")
        return 0
    for line in events.read_text().splitlines()[-a.tail:]:
        blob = line.strip()
        print(blob[:400] + ("…" if len(blob) > 400 else ""))
    return 0


def cmd_list(a):
    if not JOBS.is_dir():
        return 0
    rows = [d for d in sorted(JOBS.iterdir(), reverse=True) if d.is_dir()]
    if a.pending:
        # Isolated jobs that finished but were neither adopted nor dropped: the
        # patch is sitting in a worktree waiting for a decision.
        rows = [d for d in rows if (m := read_json(d / "meta.json", {})).get("worktree")
                and Path(m["worktree"]).exists() and not (d / "adopt.json").exists()
                and status_of(d, persist=False).get("state") not in ("running", "starting")]
        print(f"{len(rows)} isolated job(s) awaiting adopt/drop")
    for d in (rows if a.all or a.pending else rows[:20]):
        st, meta = status_of(d, persist=False), read_json(d / "meta.json", {})
        print(f"{d.name}  {meta.get('engine','?'):<5} {st.get('state','?'):<8} "
              f"{str(st.get('duration_sec','-')):>7}s  {meta.get('label') or meta.get('cwd','')}")
    return 0


def cmd_kill(a):
    d = job_dir(a.job)
    for f in ("engine.pid", "supervisor.pid"):
        pidfile = d / f
        if pidfile.exists():
            try:
                os.killpg(os.getpgid(int(pidfile.read_text())), signal.SIGKILL)
            except Exception:
                pass
    st = read_json(d / "status.json", {"state": "unknown"}) or {"state": "unknown"}
    st["state"] = "killed"
    write_json(d / "status.json", st)
    print("killed")
    return 0


def main():
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)   # `| head` is not an error
    default_engine = "codex"
    p = argparse.ArgumentParser(prog=Path(sys.argv[0]).name)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="launch a detached job")
    s.add_argument("--role", help="role from the roles table; the row decides access, "
                   "backend and contract (explicit flags still override)")
    s.add_argument("--profile", help="legacy profile name (roles.yaml `profile:`)")
    s.add_argument("--engine", choices=list(ENGINES))
    s.set_defaults(default_engine=default_engine)
    s.add_argument("--worktree", action="store_true",
                   help="run in an isolated git worktree; changes come back as a patch")
    s.add_argument("--cwd")
    s.add_argument("--sandbox", choices=["read-only", "workspace-write",
                                         "danger-full-access", "yolo"])
    s.add_argument("--yolo", action="store_true", help="no sandbox, full access")
    s.add_argument("--model")
    s.add_argument("--effort", choices=["low", "medium", "high", "xhigh",
                                        "max", "ultra"])
    s.add_argument("--label")
    s.add_argument("--resume", metavar="SESSION_ID")
    s.add_argument("--max-seconds", type=int, default=3600)
    s.add_argument("--no-schema", action="store_true")
    s.add_argument("--task-file", default="-")
    s.add_argument("--allow-duplicate", action="store_true",
                   help="suppress same-brief duplicate dispatch warnings")
    s.set_defaults(fn=cmd_start)

    s = sub.add_parser("hook-agent",
                       help="PreToolUse hook: refuse Agent dispatch below the effort floor")
    s.add_argument("--explain", action="store_true",
                   help="print the decision and why, on stderr")
    s.set_defaults(fn=cmd_hook_agent)

    s = sub.add_parser("_run"); s.add_argument("job"); s.set_defaults(fn=cmd_run)

    s = sub.add_parser("dispatch",
                       help="one-shot: directive-headed brief in, digest out")
    s.add_argument("--role", help="role from the roles table (preferred)")
    s.add_argument("--profile", help="legacy profile name")
    s.add_argument("--task-file", default="-")
    s.add_argument("--timeout", type=int, default=540)
    s.add_argument("--allow-duplicate", action="store_true",
                   help="suppress same-brief duplicate dispatch warnings")
    s.set_defaults(fn=cmd_dispatch, default_engine=default_engine)

    s = sub.add_parser("finish", help="wait for a job, then print its digest")
    s.add_argument("job")
    s.add_argument("--timeout", type=int, default=540)
    s.set_defaults(fn=cmd_finish)

    s = sub.add_parser("wait")
    s.add_argument("job")
    s.add_argument("--timeout", type=int, default=540)
    s.add_argument("--poll", type=int, default=5)
    s.set_defaults(fn=cmd_wait)

    s = sub.add_parser("digest"); s.add_argument("job"); s.set_defaults(fn=cmd_digest)
    s = sub.add_parser("result"); s.add_argument("job"); s.set_defaults(fn=cmd_result)
    s = sub.add_parser("diff", help="patch produced by an isolated job")
    s.add_argument("job"); s.set_defaults(fn=cmd_diff)
    s = sub.add_parser("adopt", help="apply an isolated job's work to the real tree")
    s.add_argument("job"); s.add_argument("--force", action="store_true")
    s.add_argument("--drop", action="store_true",
                   help="remove the worktree and refs after a successful apply")
    s.set_defaults(fn=cmd_adopt)
    s = sub.add_parser("drop", help="discard an isolated job's worktree")
    s.add_argument("job"); s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_drop)

    s = sub.add_parser("verdict", help="attach an effectiveness verdict to a job")
    s.add_argument("job")
    s.add_argument("verdict", choices=["PASS", "FAIL", "PARTIAL", "BLOCKED"])
    s.add_argument("--class", dest="fail_class")
    s.add_argument("--rework", type=int, default=0)
    s.add_argument("--review-min", type=float)
    s.add_argument("--solo-estimate-min", type=float)
    s.add_argument("--notes")
    s.set_defaults(fn=cmd_verdict)

    s = sub.add_parser("report", help="summarize job effectiveness metrics")
    s.add_argument("--since")
    s.add_argument("--until")
    s.add_argument("--by", choices=["profile", "runner_sha", "engine_version",
                                     "sandbox", "day"], default="profile")
    output = s.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true")
    output.add_argument("--tsv", action="store_true")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("gc", help="clean old adopted worktrees")
    s.add_argument("--older-than", type=float, default=24, metavar="HOURS")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--force", action="store_true",
                   help="also discard old unadopted worktrees")
    s.set_defaults(fn=cmd_gc)

    s = sub.add_parser("log")
    s.add_argument("job"); s.add_argument("--tail", type=int, default=25)
    s.set_defaults(fn=cmd_log)

    s = sub.add_parser("list"); s.add_argument("--all", action="store_true")
    s.add_argument("--pending", action="store_true",
                   help="only isolated jobs whose patch is still awaiting adopt/drop")
    s.set_defaults(fn=cmd_list)
    s = sub.add_parser("kill"); s.add_argument("job"); s.set_defaults(fn=cmd_kill)

    s = sub.add_parser("roles", help="print the roles + backends tables")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_roles)

    s = sub.add_parser("render", help="generate driver files from the roles table")
    s.add_argument("--out", help="write here instead of driver_dir (golden tests)")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--prune", action="store_true",
                   help="delete driver files no role owns any more")
    s.set_defaults(fn=cmd_render)

    s = sub.add_parser("check", help="tables valid, drivers in sync, backends answering")
    s.add_argument("--no-probe", action="store_true")
    s.add_argument("--fresh", action="store_true", help="ignore the probe cache")
    s.add_argument("--quiet", action="store_true", help="print failures only")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("resolve", help="show what a dispatch for ROLE would use, and why")
    s.add_argument("role")
    s.add_argument("--cwd")
    s.add_argument("--task-file", help="brief to read CWD/TIER/WORKTREE from and to "
                   "check brief_requires against ('-' = stdin)")
    s.add_argument("--no-probe", action="store_true")
    s.add_argument("--fresh", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_resolve)

    a = p.parse_args()
    if a.cmd not in ("list", "report", "roles", "render", "check", "resolve",
                     "hook-agent"):
        JOBS.mkdir(parents=True, exist_ok=True)
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()

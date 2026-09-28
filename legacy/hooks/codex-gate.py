#!/usr/bin/env python3
"""Stop hook: before a completion report reaches the user, check the three-way
alignment — what the user asked for, what Claude claims it did, what is
actually on disk. Material deliverables only, verified by READING files (never
by running tests) by one luna/medium job working alone — no fan-out, the claims
are small. Only a claim contradicted by what is on disk fails the check; work that
diverges from the request passes but surfaces a "请核对" notice to the user.

Claude reports work as finished that is not finished. CLAUDE.md asking it to
self-check is the soft version and fails for the same reason the miss happened.
This is the hard version: the harness runs the check, so it cannot be skipped.

Runs `agent-exec start --role gate` (luna/medium, read-only) over the turn's
final message, handing it the files this turn actually wrote. PASS lets the turn
end; RECHECK_SOL blocks it and hands the unconfirmed claims back to Claude. The
corrected report is gated again, but only on the items that were contradicted.

Only fires on turns that wrote PROJECT files. Scratch-only writes (/tmp,
scratchpad, ~/.codex-worker) are how Q&A turns take notes — gating those
blocked real conversations for nothing.

Two strikes per turn: a failed gate blocks with fix instructions (naming the
specific unconfirmed claims and the commit inspected), and the corrected report
is gated AGAIN by a fresh codex job. On a second failure the hook escalates by
itself — `agent-exec dispatch --role deep` investigates the unconfirmed
claims, fixes what is mechanically fixable, and returns concrete advice; that
advice is handed to Claude to act on. Going through the table means the
escalation obeys the deep role's write policy: once that role isolates in a
worktree, its repairs come back as a patch for review instead of landing.
The turn then ends unconditionally on the next stop — bounded work, never a
loop. Sol escalations
are capped at SOL_CAP per session; past that, a double failure is reported to
the user instead of re-investigated.

Fails OPEN. A broken gate must never wedge a session.

Toggle with `codex-gate.py on|off|status`. Config: ~/.codex-worker/GATE_ENFORCE
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOME = Path(os.environ.get("CODEX_WORKER_HOME", Path.home() / ".codex-worker"))
FLAG = HOME / "GATE_ENFORCE"
STATE = HOME / "gate-state.json"    # {session_id: failed_attempts_this_turn}

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# Bash writes files too — heredocs, tee, sed -i, python open(...,'w'). A turn
# that only used Bash must still gate, or the highest-risk edits walk past it.
BASH_WRITE = re.compile(
    r">{1,2}\s*(?!/dev/null|&\d)[\w/.\"'$]"      # redirection to a real file
    r"|(?:^|[|;&(]\s*)(?:command\s+|\\)?(?:tee|cp|mv|rm|mkdir|touch|chmod|ln|install)\s"  # `command cp` / `\cp` bypass aliases
    r"|\bsed\s+-i\b"
    r"|open\([^)]*,\s*['\"](?:w|a)"              # python inline writes
    r"|\bgit\s+(?:-C\s+\S+\s+)?(?:add|commit|checkout|restore|apply|mv|rm)\b",
    re.M,
)
# Write TARGETS inside a bash command, best-effort. Used only to decide whether
# the writes touched anything beyond scratch space — a turn that wrote nothing
# but /tmp analysis files is Q&A, and gating Q&A blocked real conversations.
BASH_TARGETS = (
    re.compile(r">{1,2}[ \t]*([^\s;|&)<]+)"),
    re.compile(r"\btee\s+(?:-a\s+)?([^\s;|&)]+)"),
    re.compile(r"\bsed\s+-i\S*\s+(?:-e\s+\S+\s+|'[^']*'\s+|\"[^\"]*\"\s+|\S+\s+)([^\s;|&)]+)"),
    re.compile(r"\b(?:cp|mv|rm|mkdir|touch|chmod|ln|install)\s+(?:-\S+\s+)*([^;|&)\n]+)"),
    re.compile(r"open\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"](?:w|a)"),  # write-mode only; a read is not a write
)
# A heredoc body is data, not commands: `cat > x <<EOF ... EOF` writes x, and
# whatever the body says about cp or STATUS: is none of the gate's business.
HEREDOC = re.compile(r"<<-?[ \t]*['\"]?(\w+)['\"]?[^\n]*\n.*?^\1[ \t]*$", re.S | re.M)
PATHISH = re.compile(r"[~\w./-]+")
QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")

SCRATCH_MARKERS = ("/scratchpad/", "/.codex-worker/")
SCRATCH_PREFIXES = ("/tmp/", "/var/tmp/", "/dev/", "/proc/", "/run/")


def is_scratch(path):
    p = os.path.expanduser(str(path))
    return p.startswith(SCRATCH_PREFIXES) or any(m in p for m in SCRATCH_MARKERS)


def bash_write_targets(command):
    """Write targets of one bash command, best-effort. [] if it writes nothing;
    None if it writes but the targets could not be extracted (relative paths,
    odd quoting) — those count as project writes: a false positive costs one
    cheap gate job, a false negative lets an unverified claim through."""
    command = HEREDOC.sub(" ", command)
    # A relative target means "relative to wherever the command was by then":
    # after `cd ~/x && tee out.txt` the file is ~/x/out.txt, not ./out.txt, and
    # a later `cd ~/y` in the same command must not move it. So the command is
    # cut at every cd and each piece resolved against its own directory.
    pieces = re.split(r"\bcd\s+([^\s;|&)]+)", command)
    targets, writes = [], False
    for i in range(0, len(pieces), 2):
        seg, cdir = pieces[i], pieces[i - 1] if i else None
        seg_targets, seg_writes = _segment_targets(seg)
        writes |= seg_writes
        targets += [t if t.startswith(("/", "~")) or not cdir else os.path.join(cdir, t)
                    for t in seg_targets]
    if not writes:
        return []
    return targets or None


def _segment_targets(command):
    """(targets, writes?) for a cd-free stretch of a command."""
    # Quoted arguments are data, not shell syntax: a `>` inside '<h2>x</h2>'
    # is markup, not a redirect. Only two things are taken from inside quotes:
    # a quoted redirect target and a python open() (in `python3 -c "..."`).
    targets = [t for t in re.findall(r">{1,2}[ \t]*['\"]([^'\"]+)['\"]", command)
               + BASH_TARGETS[-1].findall(command) if PATHISH.fullmatch(t)]
    inline = re.search(r"open\([^)]*,\s*['\"](?:w|a)", command)
    command = QUOTED.sub("''", command)
    if not (targets or inline or BASH_WRITE.search(command)):
        return [], False
    for rx in BASH_TARGETS:
        for m in rx.findall(command):
            targets += [t for t in m.split() if not t.startswith("-")]
    if re.search(r"\bgit\s+(?:-C\s+\S+\s+)?(?:add|commit|checkout|restore|apply|mv|rm)\b",
                 command):
        cdir = re.search(r"\bgit\s+-C\s+([^\s;|&]+)", command)
        targets.append(cdir.group(1) if cdir else ".")
    return [t for t in targets if PATHISH.fullmatch(t) and t != "/dev/null"], True


WAIT_SECONDS = 420  # Stop hook budget is 900s; 420 + SOL_WAIT 420 stays inside it
MAX_WRITTEN = 40
MAX_CLAIM_CHARS = 6000
MAX_REQUEST_CHARS = 3000

# Harness wrapper blocks inside user messages — not part of what the user asked.
WRAPPER_BLOCKS = re.compile(
    r"<(system-reminder|local-command-stdout|local-command-caveat|"
    r"command-name|command-message|command-args)>.*?</\1>", re.S)


def user_text(body):
    """The user's own words from a transcript message body."""
    if isinstance(body, list):
        body = "\n".join(b.get("text", "") for b in body
                         if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(body, str):
        return ""
    return WRAPPER_BLOCKS.sub("", body).strip()

BRIEF = """Check one thing: does reality match the story? You have the user's
original REQUEST, the assistant's COMPLETION CLAIM, and the live filesystem
{at_commit}.

Method — READ, do not execute. Read the files the claim names, as they are
now. Quick read-only commands (ls, grep, git diff/log/status) are fine. Do NOT
run test suites, builds, servers or anything long-running; a claim that could
only be proven by executing something is recorded in risks as "not verifiable
by reading" and does NOT fail the check.

Verify the MATERIAL claims only — files said to be created, changed or
deleted; content said to exist; records said to be written. Skip trivia:
wording, formatting, counts, incidental detail. Do not redesign anything and
do not review code quality.

Verdict rule:
- RECHECK_SOL only when what you READ contradicts a material claim — a file
  said to exist is missing, its content says otherwise, a claimed change is
  absent.
- Divergence between REQUEST and CLAIM (did something different, dropped a
  requested item) is NOT a failure. Record each divergence in risks as
  "MISMATCH: asked <what>, delivered <what>".
- A claim that faithfully relays what an evidence file says is confirmed by
  that file, even if the file is stale relative to the code today. Note the
  staleness in risks; it is not a contradiction.
- Verify only the report's own claims. Whether a document the report points
  to has complete or valid references inside it is out of scope.

Work alone and in sequence. Do NOT spawn sub-agents: every claim here is a
few file reads, and a child agent costs more than it saves. Ignore the skills
list and the memory folder — nothing there applies to this check; do not open
any SKILL.md or memory file. Send no preamble: your first message is the
verdict.

Your report is schema-forced JSON. Fill it exactly like this:
- summary: MUST start with the single word PASS or RECHECK_SOL, then one
  sentence.
- verification: file paths read and quick commands run, with real
  output{commit_note}.
- incomplete: on RECHECK_SOL, one entry per contradicted claim — quote the
  claim's own words, then the evidence that contradicts it. Empty on PASS.
- risks: MISMATCH entries and not-verifiable-by-reading notes.
- files_changed: [] (you change nothing).

--- USER REQUEST ---
{request}

--- COMPLETION CLAIM ---
{claim}
{written}{focus}"""

WRITTEN_SECTION = """
--- FILES THIS TURN ACTUALLY WROTE (from the assistant's own tool calls; start here) ---
{items}
"""
FOCUS_SECTION = """
--- PREVIOUSLY CONTRADICTED ---
This is a corrected report. Verify ONLY the items below against the corrected
claim and the disk; everything else passed last time. PASS means each is now
borne out by what you read.
{items}
"""


SOL_CAP = 2   # sol escalations per session; past this the gate reports, not loops


def _load_state():
    try:
        return json.load(open(STATE))
    except (OSError, json.JSONDecodeError):
        return {}


def get_state(session_id):
    """{'a': failed attempts this turn, 's': sol escalations this session}."""
    v = _load_state().get(session_id, {})
    if isinstance(v, int):          # pre-upgrade format
        v = {"a": v, "s": 0}
    return {"a": v.get("a", 0), "s": v.get("s", 0), "f": v.get("f") or []}


def set_state(session_id, attempts, sol_runs, focus=()):
    """focus: the contradicted items a second gate should confine itself to."""
    data = _load_state()
    if attempts or sol_runs:
        data[session_id] = {"a": attempts, "s": sol_runs, "f": list(focus)[:20]}
    else:
        data.pop(session_id, None)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    json.dump(data, open(STATE, "w"))


def allow(status=None):
    """Let the turn end. `status` becomes a user-visible one-liner."""
    out = {"suppressOutput": True}
    if status:
        out["systemMessage"] = status
    print(json.dumps(out))
    sys.exit(0)


def block(reason, status):
    """Stop the turn: `reason` goes to Claude, `status` to the user."""
    print(json.dumps({"decision": "block", "reason": reason,
                      "systemMessage": status}))
    sys.exit(0)


def read_turn(transcript_path, cwd=None):
    """(wrote_project, user_request, final_assistant_text, files_written) for this
    turn. files_written are absolute: the gate job reads them from its own cwd."""
    try:
        records = [json.loads(l) for l in open(transcript_path) if l.strip()]
    except (OSError, json.JSONDecodeError):
        return False, "", "", []

    # Walk back to the last real user message; everything after it is this turn.
    start = 0
    for i in range(len(records) - 1, -1, -1):
        msg = records[i].get("message") or {}
        if msg.get("role") != "user":
            continue
        body = msg.get("content")
        # A tool_result is delivered as a user message; it is not a new turn.
        if isinstance(body, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in body
        ):
            continue
        start = i
        break

    request = user_text((records[start].get("message") or {}).get("content"))
    wrote_project, texts, written = False, [], []
    for rec in records[start:]:
        msg = rec.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        body = msg.get("content")
        if not isinstance(body, list):
            continue
        for b in body:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                name, inp = b.get("name"), b.get("input") or {}
                if name in EDIT_TOOLS:
                    fp = inp.get("file_path") or inp.get("notebook_path")
                    if not fp or not is_scratch(fp):
                        wrote_project = True
                        written += [fp] if fp else []
                elif name == "Bash":
                    targets = bash_write_targets(inp.get("command", ""))
                    if targets is None or any(not is_scratch(t) for t in targets):
                        wrote_project = True
                        written += [t for t in targets or [] if not is_scratch(t)]
            elif b.get("type") == "text" and b.get("text", "").strip():
                texts.append(b["text"])
    written = [os.path.expanduser(w) if w.startswith(("/", "~")) else os.path.join(cwd or "", w)
               for w in written]
    return wrote_project, request, texts[-1] if texts else "", list(dict.fromkeys(written))


def git_head(cwd):
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except (subprocess.SubprocessError, OSError):
        return None


def run_gate(claim, request, cwd, head, written=(), focus=()):
    """Return the gate's full verdict text, or None if it could not be obtained.

    The whole report is returned, not just the summary — a bare RECHECK_SOL
    with no named claim gave Claude nothing to fix and never converged."""
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write(BRIEF.format(
            claim=claim[:MAX_CLAIM_CHARS],
            request=request[:MAX_REQUEST_CHARS] or "(not captured)",
            at_commit=f" (git HEAD is {head})" if head else "",
            commit_note=(", and the commit you inspected" if head else ""),
            written=(WRITTEN_SECTION.format(items="\n".join(f"- {w}" for w in written[:MAX_WRITTEN]))
                     if written else ""),
            focus=(FOCUS_SECTION.format(items="\n".join(f"- {f}" for f in focus))
                   if focus else ""),
        ))
        brief_path = fh.name
    try:
        started = subprocess.run(
            ["agent-exec", "start", "--role", "gate", "--cwd", cwd,
             "--label", "completion gate", "--task-file", brief_path],
            capture_output=True, text=True, timeout=60,
        )
        job = re.search(r"\d{8}T\d{6}-[a-f0-9]+", started.stdout + started.stderr)
        if not job:
            return None, None, "no-start"
        job = job.group(0)
        subprocess.run(["agent-exec", "wait", job, "--timeout", str(WAIT_SECONDS)],
                       capture_output=True, text=True, timeout=WAIT_SECONDS + 30)
        got = subprocess.run(["agent-exec", "result", job],
                             capture_output=True, text=True, timeout=30)
        result = json.loads(got.stdout)
        report = result.get("report") or {}
        if not report.get("summary"):
            return None, job, result.get("state") or "unknown"
        return report, job, result.get("state") or "done"
    except (subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        return None, None, "error"
    finally:
        os.unlink(brief_path)


def verdict_text(report):
    parts = [report.get("summary", "")]
    parts += [f"- contradicted: {i}" for i in report.get("incomplete") or []]
    if report.get("verification"):
        parts.append(f"(gate checked: {report['verification'][:500]})")
    return "\n".join(p for p in parts if p)


MISMATCH_RE = re.compile(r"^\s*MISMATCH[:：]\s*", re.I)


def mismatches(report):
    return [MISMATCH_RE.sub("", r).strip()
            for r in report.get("risks") or [] if MISMATCH_RE.match(str(r))]


SOL_WAIT = 420

SOL_BRIEF = """CWD: {cwd}
LABEL: gate escalation

Two independent completion gates could not confirm the claims below against the
filesystem. You are the escalation: find out what is actually true, repair what
you can, and tell the orchestrator what to do next.

CLAIMS
C1. For every unconfirmed claim below, the disk now bears it out, or your reply
    states with a file path or command output why it is false or unverifiable.
C2. Any repair touches only what those claims require.

CHECKS
C1: re-run each command the gate verdict quotes and read each file it names.
C2: `git status --short` and `git diff --stat` (when in a git repo) list nothing
    outside the claimed files.

For each unconfirmed claim:
1. Establish from direct evidence whether it is true, false, or unverifiable.
2. If the work itself is incomplete and the completion is mechanical (a missing
   file, an unrun test, an unsaved output), finish it yourself. Touch nothing
   beyond what the claims require.
3. If it needs a real design decision, do not decide — describe the decision.

End your final message with a section titled ADVICE: numbered, concrete steps
the orchestrator must take to make its report truthful, then this block:

STATUS: PRECHECK_PASS | PRECHECK_FAIL | BLOCKED
CHANGED: <files or none>
UNVERIFIED: <claim IDs not actually run, or none>
BLOCKERS: <list with reproduction commands, or none>

--- WHAT THE USER ASKED FOR ---
{request}

--- SECOND GATE VERDICT ---
{gate}

--- ORIGINAL COMPLETION CLAIM ---
{claim}
"""


def run_sol(claim, request, gate_summary, cwd):
    """Sol-tier investigation of a twice-failed claim. Returns text or None."""
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write(SOL_BRIEF.format(cwd=cwd, gate=gate_summary, claim=claim[:MAX_CLAIM_CHARS],
                                  request=request[:MAX_REQUEST_CHARS] or "(not captured)"))
        brief_path = fh.name
    try:
        # dispatch, not start: the table decides sandbox and worktree, the brief
        # must carry CLAIMS / CHECKS / RECEIPT, and a dead backend falls back.
        run = subprocess.run(
            ["agent-exec", "dispatch", "--role", "deep", "--task-file", brief_path,
             "--timeout", str(SOL_WAIT)],
            capture_output=True, text=True, timeout=SOL_WAIT + 90,
        )
        job = re.search(r"JOB=(\d{8}T\d{6}-[a-f0-9]+)", run.stdout)
        if not job or "STILL RUNNING" in run.stdout:
            return None
        got = subprocess.run(["agent-exec", "result", job.group(1)],
                             capture_output=True, text=True, timeout=30)
        report = json.loads(got.stdout).get("report") or {}
        parts = [report.get("summary", "")]
        for key in ("incomplete", "risks"):
            for item in report.get(key) or []:
                parts.append(f"- ({key}) {item}")
        text = "\n".join(p for p in parts if p).strip()
        return text or None
    except (subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        return None
    finally:
        os.unlink(brief_path)


def main():
    if len(sys.argv) > 1:  # toggle CLI
        cmd = sys.argv[1]
        HOME.mkdir(parents=True, exist_ok=True)
        if cmd == "on":
            FLAG.touch(); print("completion gate: ON")
        elif cmd == "off":
            FLAG.unlink(missing_ok=True); print("completion gate: OFF")
        else:
            print(f"completion gate: {'ON' if FLAG.exists() else 'OFF'}")
        return

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        allow()

    if not FLAG.exists():
        allow()

    session = payload.get("session_id", "unknown")
    continuation = bool(payload.get("stop_hook_active"))
    st = get_state(session)
    attempts, sol_runs = (st["a"] if continuation else 0), st["s"]
    if not continuation:
        set_state(session, 0, sol_runs)   # fresh turn: reset the strike count
    if attempts >= 2:
        set_state(session, 0, sol_runs)   # both strikes spent: end the turn
        allow()

    cwd = payload.get("cwd") or os.getcwd()
    wrote_project, request, claim, written = read_turn(payload.get("transcript_path", ""), cwd)
    if not wrote_project:
        allow()                           # Q&A / scratch-only turn: no gate
    if not claim.strip():
        allow()

    head = git_head(cwd)
    at = f" @{head}" if head else ""
    t0 = time.monotonic()
    focus = st["f"] if continuation and attempts == 1 else []
    report, job, state = run_gate(claim, request, cwd, head, written, focus)
    elapsed = f"{time.monotonic() - t0:.0f}s"
    if report is None:
        if job and state == "running":
            allow(f"⚠️ codex-gate 超时（>{WAIT_SECONDS}s）未出结论 — 本轮报告未经核验，"
                  f"稍后可看 agent-exec digest {job}")
        if job:
            allow(f"⚠️ codex-gate 作业异常（{state}）— 本轮报告未经核验（job {job}）")
        allow(f"⚠️ codex-gate 未运行（codex 不可用）— 本轮报告未经核验")
    summary = verdict_text(report)
    if summary.strip().upper().startswith("PASS"):
        set_state(session, 0, sol_runs)
        mism = mismatches(report)
        if mism:
            shown = "；".join(m[:100] for m in mism[:2])
            more = f"（共{len(mism)}条）" if len(mism) > 2 else ""
            allow(f"✅ codex-gate PASS · {elapsed}{at}\n"
                  f"⚠️ 与你的要求存在偏差，请核对{more}：{shown}")
        allow(f"✅ codex-gate PASS · luna/medium · {elapsed}{at}")

    if attempts == 0:
        set_state(session, 1, sol_runs, report.get("incomplete") or [])
        block(
            "The completion gate could not confirm part of what you just "
            f"reported (it inspected the tree{' at ' + head if head else ''}):\n\n"
            f"{summary}\n\n"
            "Check each unconfirmed item against the filesystem yourself. Fix "
            "what is actually missing, then correct your report — do not simply "
            "repeat it. Your corrected report will be gated again.",
            f"🔶 codex-gate 第1次判负（{elapsed}{at}）— Claude 正在核验修正，修正后将再次 gate",
        )
    if sol_runs >= SOL_CAP:
        # Escalation budget spent: surface the verdict and end the turn instead
        # of burning more jobs than the work itself cost.
        set_state(session, 0, sol_runs)
        allow(f"🛑 codex-gate 两次判负，sol 升级已达本会话上限（{SOL_CAP}次）— "
              f"本轮报告未通过核验{at}，请人工复核：{summary.splitlines()[0][:120]}")
    set_state(session, 2, sol_runs + 1)
    t1 = time.monotonic()
    sol = run_sol(claim, request, summary, cwd)
    sol_elapsed = f"{time.monotonic() - t1:.0f}s"
    if sol is None:
        block(
            "A second, independent gate ALSO failed, and the sol escalation "
            "could not run:\n\n"
            f"{summary}\n\n"
            "Verify each unconfirmed item against the filesystem yourself, fix "
            "what is missing, and make your report truthful. Note in the report "
            "that automated escalation was unavailable.",
            "🛑 codex-gate 两次判负，sol 升级也未能运行 — Claude 将自行核验",
        )
    block(
        "A second, independent gate ALSO failed. A sol-tier job has since "
        "investigated (and possibly repaired) the unconfirmed claims — its "
        "findings and advice:\n\n"
        f"{sol}\n\n"
        "Re-read the files it touched, act on the ADVICE items, and rewrite "
        "your report to match what is actually on disk. State plainly that two "
        "gates failed and sol intervened. This was the final automated check.",
        f"🛑 codex-gate 两次判负 → sol/high 已介入调查（{sol_elapsed}）— Claude 正在按其建议纠正",
    )


if __name__ == "__main__":
    main()

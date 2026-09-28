#!/usr/bin/env bash
# agent-exec smoke tests. Each case must PASS; the negative cases prove a gate
# actually refuses. Run: ~/.config/agent-exec/tests/smoke.sh   (exit 0 = all pass)
set -u
AE=${AE:-$HOME/.local/bin/agent-exec}
HERE=$(cd "$(dirname "$0")" && pwd)
fail=0; pass=0
ok()  { echo "PASS  $1"; pass=$((pass+1)); }
bad() { echo "FAIL  $1"; fail=$((fail+1)); }
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT

# A private copy of the tables: never touch the real driver_dir or CLAUDE.md.
cp "$HOME/.config/agent-exec/"*.yaml "$T/"
sed -i 's/^driver_command: .*/driver_command: legacy/; s/^roster_file: .*/roster_file: null/' "$T/roles.yaml"
# Pin what the cases assume, so the suite tests the code and not whatever the live dials
# happen to be set to right now.
sed -i 's/^  min_effort: .*/  min_effort: high/' "$T/roles.yaml"

# G1a. legacy render still reproduces the 2026-09-14 hand-written originals byte for
# byte for the roles whose text was never edited (gate, scout, critic). operator /
# worker / deep were deliberately re-partitioned on 2026-09-15 when chore, debug,
# research and scientist were added; their originals stay in golden-0914/.
AGENT_EXEC_CONFIG=$T "$AE" render --out "$T/out" >/dev/null
g1=1
for f in codex-gate.md codex-scout.md codex-critic.md; do
  cmp -s "$T/out/$f" "$HERE/golden-0914/$f" || { g1=0; echo "      $f differs from golden-0914"; }
done
[ $g1 = 1 ] && ok "G1a render(legacy) == 0914 originals for the 3 untouched roles" || bad "G1a render(legacy) drifted from the untouched originals"

# G1b/G1c. check catches a hand-edited driver (temp driver_dir)
sed -i "s#^driver_dir: .*#driver_dir: $T/drv#" "$T/roles.yaml"
AGENT_EXEC_CONFIG=$T "$AE" render >/dev/null
AGENT_EXEC_CONFIG=$T "$AE" check --no-probe --quiet >/dev/null && ok "G1b check green after render" || bad "G1b check should be green right after render"
printf '\n# sneaky\n' >> "$T/drv/codex-deep.md"
AGENT_EXEC_CONFIG=$T "$AE" check --no-probe --quiet >/dev/null && bad "G1c check must FAIL on a hand-edited driver" || ok "G1c check fails on a hand-edited driver"
AGENT_EXEC_CONFIG=$T "$AE" render >/dev/null

# G1d. the roster block in CLAUDE.md is rendered and checked like a driver
printf 'intro\n\n<!-- agent-exec:policy -->\nstale\n<!-- /agent-exec:policy -->\n\n<!-- agent-exec:roster -->\nstale\n<!-- /agent-exec:roster -->\n\noutro\n' > "$T/CLAUDE.md"
sed -i "s#^roster_file: .*#roster_file: $T/CLAUDE.md#" "$T/roles.yaml"
AGENT_EXEC_CONFIG=$T "$AE" check --no-probe --quiet >/dev/null && bad "G1d check must FAIL while the roster block is stale" || ok "G1d check fails on stale managed blocks"
AGENT_EXEC_CONFIG=$T "$AE" render >/dev/null
grep -q 'codex-scientist' "$T/CLAUDE.md" && grep -q '^intro$' "$T/CLAUDE.md" && grep -q '^outro$' "$T/CLAUDE.md" && ok "G1e render fills the managed blocks and leaves the rest of the file alone" || bad "G1e roster render wrong"
AGENT_EXEC_CONFIG=$T "$AE" check --no-probe --quiet >/dev/null && ok "G1f check green after roster render" || bad "G1f check should be green after roster render"

# G2. a worktree role (debug) cannot be loosened by a brief header
printf 'CWD: %s\nWORKTREE: no\n\nCLAIMS C1. CHECKS: x. STATUS: PRECHECK_PASS\n' "$T" > "$T/b_loosen.md"
AGENT_EXEC_CONFIG=$T "$AE" resolve debug --task-file "$T/b_loosen.md" --no-probe >/dev/null 2>&1 && bad "G2 WORKTREE: no on a worktree role must be refused" || ok "G2 WORKTREE: no on a worktree role is refused"

# G3. under driver_command: legacy, profiled roles keep the old command and
# profile-less roles (the four new ones) already take the enforced --role path
grep -q 'codex-exec dispatch --profile deep ' "$T/drv/codex-deep.md" && grep -q 'agent-exec dispatch --role debug ' "$T/drv/codex-debug.md" && grep -q 'agent-exec dispatch --role research ' "$T/drv/codex-research.md" && ok "G3 legacy: profiled roles -> codex-exec --profile, new roles -> agent-exec --role" || bad "G3 driver command selection wrong"
sed -i 's/^driver_command: .*/driver_command: role/' "$T/roles.yaml"
AGENT_EXEC_CONFIG=$T "$AE" render >/dev/null
grep -q 'agent-exec dispatch --role deep ' "$T/drv/codex-deep.md" && grep -q 'agent-exec start --role gate ' "$T/drv/codex-gate.md" && ok "G3b driver_command: role flips every driver to --role" || bad "G3b role flip incomplete"
sed -i 's/^driver_command: .*/driver_command: legacy/' "$T/roles.yaml"

# G4. writes -> sandbox / worktree / network exactly as the table says
J=$(AGENT_EXEC_CONFIG=$T "$AE" resolve research --cwd "$T" --no-probe --json)
echo "$J" | python3 -c 'import json,sys; r=json.load(sys.stdin)["resolved"]; assert (r["sandbox"],r["worktree"],r["network"],r["backend"],r["effort"])==("workspace-write",False,True,"codex-terra","high"), r' && ok "G4a research -> workspace-write + network, terra/high, no worktree" || bad "G4a research resolve wrong"
J=$(AGENT_EXEC_CONFIG=$T "$AE" resolve chore --cwd "$T" --no-probe --json)
echo "$J" | python3 -c 'import json,sys; r=json.load(sys.stdin)["resolved"]; assert (r["sandbox"],r["worktree"],r["network"],r["backend"],r["effort"])==("yolo",False,False,"codex-luna","medium"), r' && ok "G4b chore -> tree (config default sandbox), luna/medium" || bad "G4b chore resolve wrong"
J=$(AGENT_EXEC_CONFIG=$T "$AE" resolve debug --cwd "$T" --no-probe --json)
echo "$J" | python3 -c 'import json,sys; r=json.load(sys.stdin)["resolved"]; assert (r["worktree"],r["verdict_level"],r["brief_requires"])==(True,"L2",["CLAIMS","CHECKS","RECEIPT","REPRODUCER","LOCKED_VERIFIER"]), r' && ok "G4c debug -> worktree, L2, reproducer + locked verifier required" || bad "G4c debug resolve wrong"

# G5. dispatch --role refuses a CHANGE brief without CLAIMS/CHECKS/RECEIPT, before launching
M="G5-MARKER-$$-$RANDOM"
printf 'CWD: %s\n\nJust do it. %s\n' "$T" "$M" > "$T/b_bare.md"
AGENT_EXEC_CONFIG=$T "$AE" dispatch --role deep --task-file "$T/b_bare.md" >/dev/null 2>&1 && bad "G5 bare CHANGE brief must be refused" || ok "G5 bare CHANGE brief refused"
grep -rlq "$M" "$HOME/.codex-worker/jobs/"*/task.md 2>/dev/null && bad "G5b refusal still launched a job" || ok "G5b refusal launched no job"
# G5c. a debug brief with claims/checks/receipt but no reproducer is still refused
printf 'CWD: %s\n\nFix it. %s\nCLAIMS C1. CHECKS: x.\nSTATUS: PRECHECK_PASS\n' "$T" "$M" > "$T/b_norepro.md"
out=$(AGENT_EXEC_CONFIG=$T "$AE" dispatch --role debug --task-file "$T/b_norepro.md" 2>&1) && bad "G5c debug brief without REPRODUCER must be refused" || { echo "$out" | grep -q "missing: REPRODUCER, LOCKED_VERIFIER" && ok "G5c debug brief without REPRODUCER/LOCKED_VERIFIER refused, names both" || { bad "G5c refused for the wrong reason"; echo "$out" | sed 's/^/      /'; }; }
grep -rlq "$M" "$HOME/.codex-worker/jobs/"*/task.md 2>/dev/null && bad "G5d refusal still launched a job" || ok "G5d refusal launched no job"

# G6. route with a dead first backend falls back and says so; all dead → BLOCKED
# (own copy of the tables: this group breaks them on purpose)
G6=$T/g6; mkdir -p "$G6"; cp "$T"/*.yaml "$G6"/
python3 - "$G6" <<'PY'
import sys, pathlib
t = pathlib.Path(sys.argv[1])
b = (t/"backends.yaml").read_text().replace(
    "  codex-sol:\n    engine: codex\n    model: gpt-5.6-sol\n",
    "  codex-dead:\n    engine: codex\n    model: gpt-5.6-sol\n    effort_map: {high: high}\n    sandboxes: [read-only, workspace-write, yolo]\n    probe: /nonexistent/binary --version\n\n  codex-sol:\n    engine: codex\n    model: gpt-5.6-sol\n")
(t/"backends.yaml").write_text(b)
r = (t/"roles.yaml").read_text().replace(
    "    route: [{backend: codex-sol, effort: high}]\n    deliverable: [receipt, claims]\n    verdict_level: L1\n    tier_up: null\n    summary: 架构",
    "    route: [{backend: codex-dead, effort: high}, {backend: codex-sol, effort: high}]\n    deliverable: [receipt, claims]\n    verdict_level: L1\n    tier_up: null\n    summary: 架构")
(t/"roles.yaml").write_text(r)
PY
out=$(AGENT_EXEC_CONFIG=$G6 "$AE" resolve deep --cwd "$G6" --fresh 2>&1)
echo "$out" | grep -q "route\[1\] FALLBACK" && echo "$out" | grep -q "codex-dead: DOWN" && ok "G6a dead first backend → FALLBACK to route[1], noted" || { bad "G6a fallback not taken/noted"; echo "$out" | sed 's/^/      /'; }
sed -i 's/route: \[{backend: codex-dead, effort: high}, {backend: codex-sol, effort: high}\]/route: [{backend: codex-dead, effort: high}]/' "$G6/roles.yaml"
AGENT_EXEC_CONFIG=$G6 "$AE" resolve deep --cwd "$G6" --fresh >/dev/null 2>&1 && bad "G6b all backends dead must BLOCK" || ok "G6b all backends dead → BLOCKED, no silent role change"

# G7. kimi is retired: no engine, no backend, no entry point
"$AE" start --engine kimi --cwd "$T" </dev/null >/dev/null 2>&1 && bad "G7a --engine kimi must be rejected" || ok "G7a --engine kimi rejected"
G7=$T/g7; mkdir -p "$G7"; cp "$T"/*.yaml "$G7"/
printf 'version: 1\nbackends:\n  k:\n    engine: kimi\n    model: kimi-code/k3\n    sandboxes: [yolo]\n' > "$G7/backends.yaml"
AGENT_EXEC_CONFIG=$G7 "$AE" check --no-probe --quiet 2>&1 | grep -q "engine: want one of" && ok "G7b a backend with engine: kimi fails check" || bad "G7b engine: kimi accepted"
[ -e "$HOME/.local/bin/kimi-exec" ] && bad "G7c kimi-exec entry point still present" || ok "G7c kimi-exec entry point gone"
grep -qi kimi "$AE" && bad "G7d kimi code left in agent-exec" || ok "G7d no kimi code left in agent-exec"

# G8. the dispatch gate: below dispatch_policy.min_effort the Agent tool is refused
pay() { python3 -c '
import json,sys
d={"hook_event_name":"PreToolUse","tool_name":sys.argv[1],
   "tool_input":{"subagent_type":sys.argv[2],"prompt":sys.argv[3],"description":"d"},
   "transcript_path":sys.argv[4]}
if sys.argv[5]: d["effort"]={"level":sys.argv[5]}
if sys.argv[6]: d["agent_id"]=sys.argv[6]
print(json.dumps(d))' "$@"; }

printf '%s\n' '{"type":"user","message":{"role":"user","content":"帮我看一下这个函数为什么慢"}}' > "$T/tr-plain.jsonl"
cp "$T/tr-plain.jsonl" "$T/tr-asked.jsonl"
printf '%s\n' '{"type":"user","message":{"role":"user","content":"派个 worker 去跑一下"}}' >> "$T/tr-asked.jsonl"
cp "$T/tr-plain.jsonl" "$T/tr-toolresult.jsonl"
printf '%s\n' '{"type":"user","message":{"role":"user","content":[{"type":"tool_result","content":"codex dispatch worker subagent"}]}}' >> "$T/tr-toolresult.jsonl"

G8() { # name expect(deny|allow) -- then: tool subagent prompt transcript effort agent_id
  local name=$1 expect=$2 out; shift 2
  out=$(pay "$@" | AGENT_EXEC_CONFIG=$T ${G8ENV:-} "$AE" hook-agent)
  if echo "$out" | grep -q '"permissionDecision": "deny"'; then
    [ "$expect" = deny ] && ok "$name" || { bad "$name (denied, wanted allow)"; echo "$out" | sed 's/^/      /'; }
  else
    [ "$expect" = allow ] && ok "$name" || bad "$name (allowed, wanted deny)"
  fi
}
G8 "G8a medium effort → Agent refused"               deny  Agent codex-deep  "do it" "$T/tr-plain.jsonl"      medium ""
G8 "G8b high effort → Agent allowed"                 allow Agent codex-deep  "do it" "$T/tr-plain.jsonl"      high   ""
G8 "G8c FORCE_DISPATCH in the prompt → allowed"      allow Agent codex-deep  "FORCE_DISPATCH
do it" "$T/tr-plain.jsonl" medium ""
G8 "G8d user asked for a worker → allowed"           allow Agent codex-deep  "do it" "$T/tr-asked.jsonl"      medium ""
G8 "G8e trigger words in a tool_result do not count" deny  Agent codex-deep  "do it" "$T/tr-toolresult.jsonl" medium ""
G8 "G8f low effort → refused"                        deny  Agent codex-scout "look"  "$T/tr-plain.jsonl"      low    ""
G8ENV="env -u CLAUDE_EFFORT" G8 "G8g effort absent everywhere → fails open" allow Agent codex-deep "do it" "$T/tr-plain.jsonl" "" ""
G8 "G8h non-Agent tool is never gated"               allow Bash  ""          "rm"    "$T/tr-plain.jsonl"      medium ""
G8 "G8i call from inside a subagent → allowed"       allow Agent codex-deep  "do it" "$T/tr-plain.jsonl"      medium "ag_1"
G8ENV="env CLAUDE_EFFORT=medium" G8 "G8j CLAUDE_EFFORT fallback when the payload omits effort" deny Agent codex-deep "do it" "$T/tr-plain.jsonl" "" ""
mkdir -p "$T/w"; touch "$T/w/DISPATCH_GATE_OFF"
G8ENV="env CODEX_WORKER_HOME=$T/w" G8 "G8k DISPATCH_GATE_OFF disables the gate" allow Agent codex-deep "do it" "$T/tr-plain.jsonl" medium ""
rm -f "$T/w/DISPATCH_GATE_OFF"
python3 - "$T" <<'ROLEPATCH'
import sys, pathlib
p = pathlib.Path(sys.argv[1])/"roles.yaml"
p.write_text(p.read_text().replace("    driver: codex-scout\n",
                                   "    driver: codex-scout\n    min_effort: low\n", 1))
ROLEPATCH
G8 "G8l per-role min_effort lets scout through at medium"       allow Agent codex-scout "look"  "$T/tr-plain.jsonl" medium ""
G8 "G8m a per-role floor does not lift the global one for others" deny Agent codex-deep "do it" "$T/tr-plain.jsonl" medium ""

# G9. the CLAUDE.md policy block always states the value the hook enforces
sed -i 's/^  min_effort: high$/  min_effort: xhigh/' "$T/roles.yaml"
AGENT_EXEC_CONFIG=$T "$AE" render >/dev/null
grep -q 'below `xhigh` effort' "$T/CLAUDE.md" && ok "G9a the policy block names the enforced floor" || bad "G9a policy block did not follow min_effort"
AGENT_EXEC_CONFIG=$T "$AE" check --no-probe --quiet >/dev/null && ok "G9b check green after the policy re-render" || bad "G9b check should be green after render"
sed -i 's/^  min_effort: xhigh$/  min_effort: high/' "$T/roles.yaml"
AGENT_EXEC_CONFIG=$T "$AE" check --no-probe --quiet >/dev/null && bad "G9c check must FAIL while the policy block is stale" || ok "G9c a stale policy block fails check"

# G10. what counts as "the user asked": only the user's own prose, and the reader
# must find it in a transcript far larger than the tail it reads first
python3 - "$T" <<'TRANSCRIPTS'
import json, sys, pathlib
t = pathlib.Path(sys.argv[1])
def u(c): return json.dumps({"type": "user", "message": {"role": "user", "content": c}})
plain = u("这个函数为什么慢")
# a background-task wake-up that happens to name a worker
(t/"tr-notif.jsonl").write_text(plain + "\n" + u(
    "<task-notification>\n<task-id>x1</task-id>\ncodex worker dispatch finished\n</task-notification>") + "\n")
# the same wrapper, but the user really did ask afterwards
(t/"tr-notif-then-ask.jsonl").write_text(
    (t/"tr-notif.jsonl").read_text() + u("派个 worker 去改") + "\n")
# trigger words only inside an injected reminder attached to a plain message
(t/"tr-reminder.jsonl").write_text(plain + "\n" + u(
    "<system-reminder>subagent codex dispatch worker</system-reminder>\n帮我看看这个") + "\n")
# the ask is older than the 512KB tail the reader starts with
pad = "\n".join(json.dumps({"type": "assistant", "message": {"role": "assistant",
                 "content": "x" * 200}}) for _ in range(4000))
(t/"tr-far.jsonl").write_text(u("派个 worker 去改") + "\n" + pad + "\n")
TRANSCRIPTS
[ "$(wc -c < "$T/tr-far.jsonl")" -gt 524288 ] && ok "G10a the far-ask transcript is bigger than the 512KB tail" || bad "G10a fixture too small to test the fallback"
G10=Agent
G8 "G10b a task-notification naming a worker is not the user asking" deny  Agent codex-deep "do it" "$T/tr-notif.jsonl"         medium ""
G8 "G10c a real ask after such a notice still counts"                allow Agent codex-deep "do it" "$T/tr-notif-then-ask.jsonl" medium ""
G8 "G10d trigger words inside an injected reminder do not count"     deny  Agent codex-deep "do it" "$T/tr-reminder.jsonl"      medium ""
G8 "G10e an ask older than the tail is still found (whole-file fallback)" allow Agent codex-deep "do it" "$T/tr-far.jsonl"      medium ""

# G11. TIER goes through the backend table: aliases resolve, nicknames are refused
tier() { printf 'CWD: %s\nTIER: %s\n\nCLAIM x\n' "$T" "$1" > "$T/b_tier.md"; AGENT_EXEC_CONFIG=$T "$AE" resolve scout --no-probe --json --task-file "$T/b_tier.md" 2>&1; }
tier "codex-terra high" | python3 -c 'import json,sys; r=json.load(sys.stdin)["resolved"]; assert (r["model"],r["effort"])==("gpt-5.6-terra","high"), r' 2>/dev/null && ok "G11a TIER with a backend alias resolves to its model id" || bad "G11a alias TIER not resolved"
tier "sol high" | grep -q "neither a backend alias" && ok "G11b TIER with a family nickname is refused, aliases listed" || bad "G11b nickname TIER accepted"
tier "gpt-5.6-sol xhigh" | python3 -c 'import json,sys; r=json.load(sys.stdin)["resolved"]; assert r["model"]=="gpt-5.6-sol", r' 2>/dev/null && ok "G11c TIER with a listed model id still works" || bad "G11c model-id TIER broken"
tier "codex-sol ultra" | grep -q "TIER effort" && ok "G11d TIER with an unknown effort is refused" || bad "G11d bad effort accepted"
M="G11-$$-$RANDOM"; printf 'CWD: %s\nTIER: sol high\n\nCLAIMS C1 %s. CHECKS x. STATUS: PRECHECK_PASS\n' "$T" "$M" > "$T/b_tier2.md"
AGENT_EXEC_CONFIG=$T "$AE" dispatch --role deep --task-file "$T/b_tier2.md" >/dev/null 2>&1 && bad "G11e dispatch with a bad TIER must be refused" || ok "G11e dispatch with a bad TIER refused"
grep -rlq "$M" "$HOME/.codex-worker/jobs/"*/task.md 2>/dev/null && bad "G11f refusal still launched a job" || ok "G11f refusal launched no job"

# G12. ledger v2: verdict/adopt/drop upsert one row per job; settle writes identity
W=$T/w; JB=$W/jobs/20260101T000000-aaaaaa; mkdir -p "$JB"
printf '{"job_id":"20260101T000000-aaaaaa","role":"scout","backend":"codex-luna","model":"gpt-5.6-luna","effort":"medium","kind":"QUERY","writes":"none","dispatch_mode":"role","cwd":"/x"}\n' > "$JB/meta.json"
printf '{"state":"done","duration_sec":90}\n' > "$JB/status.json"
LG=$T/ledger.tsv
CODEX_WORKER_HOME=$W AGENT_EXEC_LEDGER=$LG "$AE" verdict 20260101T000000-aaaaaa PASS --notes "first" >/dev/null
[ "$(wc -l < "$LG")" = 2 ] && head -1 "$LG" | grep -q "^date	job_id	session	role" && ok "G12a first verdict creates the ledger with a header and one row" || { bad "G12a ledger shape wrong"; cat "$LG"; }
CODEX_WORKER_HOME=$W AGENT_EXEC_LEDGER=$LG "$AE" verdict 20260101T000000-aaaaaa FAIL --class D >/dev/null
[ "$(wc -l < "$LG")" = 2 ] && grep -q "	FAIL	D	" "$LG" && ok "G12b a second verdict updates the same row (upsert), no duplicate" || { bad "G12b upsert broke"; cat "$LG"; }
python3 - "$AE" "$W" "$LG" <<'SETTLE'
import sys, json, os, importlib.util, pathlib
os.environ["CODEX_WORKER_HOME"]=sys.argv[2]; os.environ["AGENT_EXEC_LEDGER"]=sys.argv[3]
spec = importlib.util.spec_from_loader("ae", loader=None); ae = importlib.util.module_from_spec(spec)
exec(compile(pathlib.Path(sys.argv[1]).read_text(), sys.argv[1], "exec"), ae.__dict__)
jb = pathlib.Path(sys.argv[2])/"jobs/20260101T000000-aaaaaa"
ae.ledger_settle(json.load(open(jb/"meta.json")), {**json.load(open(jb/"status.json")), "session_id": "sess1"})
SETTLE
grep -q "	sess1	scout	codex-luna	gpt-5.6-luna	medium	QUERY	none	N	role	N	done	1.5	/x	FAIL	D	" "$LG" && ok "G12c settle fills the identity columns and keeps the verdict" || { bad "G12c settle row wrong"; cat "$LG"; }

# G13. list --pending shows isolated jobs awaiting a decision, and only those
JW=$W/jobs/20260102T000000-bbbbbb; mkdir -p "$JW" "$W/wt-b"
printf '{"job_id":"20260102T000000-bbbbbb","role":"deep","worktree":"%s","cwd":"%s","label":"pending one"}\n' "$W/wt-b" "$W/wt-b" > "$JW/meta.json"
printf '{"state":"done","duration_sec":5}\n' > "$JW/status.json"
CODEX_WORKER_HOME=$W "$AE" list --pending | grep -q "^1 isolated" && ok "G13a an undisposed isolated job is listed as pending" || bad "G13a pending job not listed"
printf '{"result":"applied"}\n' > "$JW/adopt.json"
CODEX_WORKER_HOME=$W "$AE" list --pending | grep -q "^0 isolated" && ok "G13b once adopted it leaves the pending list" || bad "G13b adopted job still pending"

# G14. the Stop hook (codex-gate.py): no fan-out, write set and focus in the brief,
# escalation routed through the table. Exercised in-process with subprocess faked.
HOOK=$HOME/.claude/hooks/codex-gate.py
CODEX_WORKER_HOME=$T/w AE=$AE python3 - "$HOOK" "$T" > "$T/g14.out" 2>&1 <<'HOOKTEST'
import sys, json, os, re, subprocess, pathlib
hook, T = sys.argv[1], pathlib.Path(sys.argv[2])
ns = {"__name__": "hooktest"}
exec(compile(pathlib.Path(hook).read_text(), hook, "exec"), ns)
def check(name, cond): print(("PASS  " if cond else "FAIL  ") + name)

B = ns["BRIEF"]
check("G14a gate brief no longer asks for parallel sub-agents",
      "parallel subagents" not in B and "Do NOT spawn sub-agents" in B)

t = ns["bash_write_targets"]
cmd = "cat > src/a.py <<'EOF'\ncp junk here\nSTATUS: PRECHECK_PASS >\nEOF\ncommand cp -f b.txt c.txt && echo \"----- done\""
check("G14b heredoc bodies are ignored and only path-shaped targets survive",
      t(cmd) == ["src/a.py", "b.txt", "c.txt"])
check("G14c a command that writes nothing yields []", t("ls -la && grep -n x y") == [])
check("G14n a relative target after `cd` is resolved against that directory",
      t("cd ~/.config/agent-exec && tests/smoke.sh 2>&1 | tee tests/last-run.txt") == ["~/.config/agent-exec/tests/last-run.txt"])
check("G14q each relative target resolves against the cd that precedes it, not the last cd",
      t("cd ~/a && command cp -f f g && cd ~/b && tee o.txt") == ["~/a/f", "~/a/g", "~/b/o.txt"])
check("G14r `command cp` / `\\cp` (alias bypass) count as writes",
      t("command cp -f x y") == ["x", "y"] and t("\\cp x y") == ["x", "y"] and t("command rm -f z") == ["z"])
check("G14s a read-mode open() is not a write; a write-mode one is",
      t("python3 -c \"import json;json.load(open('/h/settings.json'))\"") == []
      and t("python3 -c \"open('/h/out.txt','w').write('x')\"") == ["/h/out.txt"])
check("G14o a `>` inside a quoted argument is markup, not a redirect",
      t("lark-cli docs +update --old-str '<h2>a</h2>' --new-str '<h2>gate</h2>' 2>&1 | tail -2") == []
      and t("grep '<h2>' foo.txt") == [])
check("G14p a quoted redirect target is still a write",
      t('echo hi > "out.txt"') == ["out.txt"] and t("sed -i 's|a>b|c|' M.md") == ["M.md"])

tr = T / "tr-turn.jsonl"
def rec(role, content): return json.dumps({"type": role, "message": {"role": role, "content": content}})
tu = lambda name, inp: {"type": "tool_use", "name": name, "input": inp}
tr.write_text("\n".join([
    rec("user", "please do the thing"),
    rec("assistant", [tu("Edit", {"file_path": "/proj/f1.py"})]),
    rec("assistant", [tu("Bash", {"command": "tee /tmp/notes.md"})]),
    rec("assistant", [tu("Bash", {"command": "cat > docs/x.md <<'EOF'\n> not a path\nEOF"})]),
    rec("assistant", [{"type": "text", "text": "Done: f1.py and docs/x.md."}]),
]) + "\n")
wrote, req, claim, written = ns["read_turn"](str(tr), "/proj")
check("G14d read_turn returns the turn's project write set, absolute, scratch excluded",
      wrote and req == "please do the thing" and written == ["/proj/f1.py", "/proj/docs/x.md"])

ns["set_state"]("s1", 1, 0, ["claim A is wrong", "claim B is wrong"])
check("G14e the contradicted items ride in the session state",
      ns["get_state"]("s1")["f"] == ["claim A is wrong", "claim B is wrong"])
ns["set_state"]("s1", 0, 0)
check("G14f a PASS clears the focus list", ns["get_state"]("s1")["f"] == [])

calls = []
def fake_run(argv, **kw):
    calls.append(list(argv))
    class R: stdout = ""; stderr = ""; returncode = 0
    r = R()
    if "--task-file" in argv:
        (T / ("brief-" + argv[1] + ".md")).write_text(pathlib.Path(argv[argv.index("--task-file") + 1]).read_text())
    if argv[1] == "start": r.stdout = "20260101T000000-cccccc\n"
    if argv[1] == "dispatch": r.stdout = "JOB=20260101T000000-cccccc\n## codex job 20260101T000000-cccccc — done\n"
    if argv[1] == "result": r.stdout = json.dumps({"state": "done", "report": {"summary": "PASS ok", "incomplete": [], "risks": ["ADVICE: 1. do x"]}})
    return r
ns["subprocess"].run = fake_run

ns["run_gate"]("the claim", "the request", "/tmp", None, ["a.py", "docs/x.md"], ["prev item 1"])
gb = (T / "brief-start.md").read_text()
check("G14g run_gate starts the gate through the table (agent-exec start --role gate)",
      calls[0][:4] == ["agent-exec", "start", "--role", "gate"])
check("G14h the gate brief carries the write set and the focus list",
      "FILES THIS TURN ACTUALLY WROTE" in gb and "- a.py" in gb and "- docs/x.md" in gb
      and "PREVIOUSLY CONTRADICTED" in gb and "- prev item 1" in gb)
ns["run_gate"]("c", "r", "/tmp", None, [], [])
check("G14i without a focus list the brief has no PREVIOUSLY CONTRADICTED section",
      "PREVIOUSLY CONTRADICTED" not in (T / "brief-start.md").read_text())

calls.clear()
out = ns["run_sol"]("the claim", "the request", "RECHECK_SOL x", "/tmp")
check("G14j run_sol dispatches through the table (agent-exec dispatch --role deep)",
      calls and calls[0][:4] == ["agent-exec", "dispatch", "--role", "deep"] and "ADVICE" in (out or ""))
sb = (T / "brief-dispatch.md").read_text()
check("G14k the escalation brief opens with the CWD header", sb.startswith("CWD: /tmp\n"))
def fake_still(argv, **kw):
    class R: stdout = "JOB=20260101T000000-dddddd\nSTILL RUNNING: 20260101T000000-dddddd\n"; stderr = ""; returncode = 0
    return R()
ns["subprocess"].run = fake_still
check("G14l an escalation that is still running yields no advice (fails open)", ns["run_sol"]("c", "r", "g", "/tmp") is None)
HOOKTEST
while read -r line; do case "$line" in PASS*) ok "${line#PASS  }";; FAIL*) bad "${line#FAIL  }";; *) echo "      $line";; esac; done < "$T/g14.out"
# the escalation brief must satisfy the deep role's L0 sections, by the real resolver
AGENT_EXEC_CONFIG=$T "$AE" resolve deep --no-probe --task-file "$T/brief-dispatch.md" 2>&1 | grep -q "all required sections present" && ok "G14m the escalation brief passes the deep role's brief_requires" || { bad "G14m escalation brief would be refused by dispatch"; AGENT_EXEC_CONFIG=$T "$AE" resolve deep --no-probe --task-file "$T/brief-dispatch.md" 2>&1 | tail -2; }

# G11. "the user asked" means an instruction to hand the work over, not a mention of
# the machinery. Most conversations in this project are *about* codex and dispatch.
mkuser() { printf '%s\n' "$(python3 -c '
import json,sys; print(json.dumps({"type":"user","message":{"role":"user","content":sys.argv[1]}}))' "$2")" > "$1"; }

mkuser "$T/tr-about.jsonl"   "当前能根据effot程度来允许是否采用 agent-exec 的方式调用吗？高以上才按我的agent-exec 来"
mkuser "$T/tr-codefn.jsonl"  "dispatch 这个函数为什么要先 parse 再 resolve"
mkuser "$T/tr-docname.jsonl" "派工体系这份文档写得怎么样"
mkuser "$T/tr-poolsz.jsonl"  "the worker pool size is wrong"
mkuser "$T/tr-req-zh.jsonl"  "派给 operator 去做"
mkuser "$T/tr-req-en.jsonl"  "dispatch a worker for this"
mkuser "$T/tr-req-let.jsonl" "让 codex-deep 去定位一下根因"

G8 "G11a asking how agent-exec works is not a request for one" deny  Agent codex-deep "x" "$T/tr-about.jsonl"   medium ""
G8 "G11b discussing a function named dispatch is not one"      deny  Agent codex-deep "x" "$T/tr-codefn.jsonl"  medium ""
G8 "G11c naming the 派工 doc is not one"                        deny  Agent codex-deep "x" "$T/tr-docname.jsonl" medium ""
G8 "G11d the noun 'worker' in a bug report is not one"         deny  Agent codex-deep "x" "$T/tr-poolsz.jsonl"  medium ""
G8 "G11e 派给 X 去做 is a request"                              allow Agent codex-deep "x" "$T/tr-req-zh.jsonl"  medium ""
G8 "G11f dispatch a worker is a request"                       allow Agent codex-deep "x" "$T/tr-req-en.jsonl"  medium ""
G8 "G11g 让 codex-deep 去定位 is a request"                     allow Agent codex-deep "x" "$T/tr-req-let.jsonl" medium ""

echo "== $pass passed, $fail failed =="
exit $fail

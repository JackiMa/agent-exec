# Worker contract — conservative rollout (rev B)

Operational rules for dispatching CLI workers. Revised after two adversarial
review rounds (GPT-5 Pro, session db8f52d75a1b, 2026-08-18). The strict
variant this is cut down from lives in `agent-collab-contract-strict.md`.
Referenced from CLAUDE.md ("Writing the brief" / "Judging the result" /
"The scoreboard").

## Enforcement levels

- **L0 — every brief, zero infra.** Numbered CLAIMS; per-claim check
  commands; LOCKED_VERIFIER list; receipt block pasted verbatim into the
  brief; repo-relative paths in the body. Never loosens.
- **L1 — default for CHANGE tasks.** Orchestrator runs the checks personally,
  in the order given in CLAUDE.md "Judging the result" (baseline timing
  differs for WORKTREE vs non-WORKTREE; post-adopt FAIL → reverse the patch
  or quarantine). Identity check whenever the claim depends on which artifact
  or config ran. Record BASE_TREE / RESULT_TREE hashes with the verdict; if
  the tree moved between baseline and adopt, the baseline is void.
- **L1S — loosened L1** (earned via scoreboard): independent checks run on a
  random ~50% of low-risk dispatches, decided AFTER delivery, never by a
  fixed alternating pattern. Always still run: a bug fix's post-change
  reproducer; anything touching identity/config, verifiers, concurrency or
  lifecycle, or safety-adjacent paths — those never enter spot-check mode.
  Unaudited rows get `audited=N, first_pass=NA` and do not feed the stats.
- **L2 — high-risk and probation.** Fresh worktree checkout, rebuild, run
  checks there; low-cost state reset from the project's trap rules (clean
  build dir, fixed seeds/N, kill known daemons, print actual binary/config
  identity); critic review whose input is the task contract (GOAL, CLAIMS,
  CHECKS, CONSTRAINTS) + diff + base/result identity + raw evidence — and
  NOT the worker's narrative.

## Verifier boundary (TCB)

Each brief names its gate-owned files explicitly:

```
LOCKED_VERIFIER:
- path/to/acceptance_check.py
- path/to/expected.json
```

Only these are untouchable; touching one voids the result unless the task is
itself a VERIFIER_CHANGE approved by the orchestrator. Ordinary tests stay
editable (adding regression tests is normal work) but may not be the task's
only independent oracle. Bug-fix rule: at least ONE check is a dedicated
reproducer that fails on the base tree; regression/compat checks may pass on
both sides. A defect that cannot be reproduced deterministically gets a
declared BASELINE_EXCEPTION with substitute evidence — never a fabricated
failing command.

## Receipt semantics

```
STATUS: PRECHECK_PASS | PRECHECK_FAIL | BLOCKED
CHANGED: <files or none>
UNVERIFIED: <claim IDs not actually run, or none>
BLOCKERS: <list with reproduction commands, or none>
```

- PRECHECK_PASS requires every required claim's check actually run and
  passed; any claim in UNVERIFIED forbids it. PRECHECK_FAIL = a required
  check failed or was not run for reasons inside the worker's scope.
- BLOCKED is legitimate only if ALL hold: the blocker reproduces for the
  orchestrator; it also exists on the unmodified tree (not introduced by the
  candidate); it blocks a required claim; resolving it exceeds the worker's
  authorized scope. A blocker the candidate created is PRECHECK_FAIL. The
  evidence duty applies to every external-cause attribution, including ones
  parked in BLOCKERS under a PRECHECK_FAIL status.
- Missing receipt: log `protocol_miss` in notes and judge from disk + digest;
  RESUME only if substantive information (UNVERIFIED/BLOCKERS) is missing.
- QUERY (read-only analysis) briefs use the evidence triple instead of
  CHANGED: per conclusion CLAIM / SUPPORT (revision + file:line) /
  FALSIFIER_SEARCH (re-runnable command) / UNRESOLVED. High-risk QUERY
  verdicts go to a critic that checks whether citations support the claims
  and what counter-evidence paths were not searched.

## Scoreboard (ledger)

One row per settled **logical task** (RESUME rounds are the same row) in
`~/.claude/codex-ledger.tsv`:

```
date task_id project agent tier kind level audited first_pass verdict disposition fail_class rework wall_min notes
```

- `audited`: Y if independent checks were run this dispatch; N under L1S
  spot-skip. `first_pass`: Y/N/NA — NA when unaudited; only audited rows
  feed statistics.
- `verdict`: PASS | FAIL | BLOCKED | UNJUDGED (correctness). `disposition`:
  ADOPT | DROP | CANCEL (what happened to the patch). A correct patch
  superseded by another is PASS+DROP.
- `fail_class`: combinable — failure mode A (verified wrong object) and/or
  honesty B (claimed undone work) / C (false external blame, only when the
  attribution is positively disproven) / D (honest miss). `-` = passed.
  Integrity axis = B, C, and A-with-false-claim; capability axis = D.
- Stats per **agent+tier+kind**, audited rows only:

```
awk -F'\t' 'NR>1 && $8=="Y" {k=$4"/"$5"/"$6; n[k]++; fp[k]+=($9=="Y")}
  END{for(k in n) printf "%-40s n=%d first-pass=%.0f%%\n", k, n[k], 100*fp[k]/n[k]}' ~/.claude/codex-ledger.tsv
```

## Loosen / tighten rules

- **Loosen to L1S** (per agent+tier+kind): last 20 audited rows have ≥18
  first-pass AND no integrity incident in the window AND the combination is
  low-risk. 20 audited samples is a pilot gate, not proof of the true rate —
  hence the 18/20 bar and the exclusions above.
- **Integrity probation → L2**: any fresh integrity incident (B, confirmed C,
  or A where the worker claimed verification it did not do). Exit: 10 audited
  eligible tasks with ≥9 first-pass and no new incident (not "10 consecutive"
  — that is too sticky and punishes honest D).
- **Capability response (not probation)**: honest-D-heavy or low first-pass
  without integrity incidents → change tier, shrink the brief, improve
  decomposition. Punishing honest failure teaches hiding, not competence.
- **Throughput check**: at each review compute
  `minutes_per_adopted_pass = sum(wall_min of eligible tasks) / count(verdict=PASS & disposition=ADOPT)`.
  A rule change that raises first-pass but worsens this number is a loss.

## Known repo-trap registry

Keep per-project trap lists as numbered rules in the project's memory.
Briefs paste the cited rules' one-liners — a bare ID is invisible to the
worker; the registry exists so the same rule reads identically in every
brief. Trap rules may also declare the L2 state-reset steps for the project.

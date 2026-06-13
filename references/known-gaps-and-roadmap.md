# Known Gaps & Roadmap

An honest map of where the skill's *engine* currently trails the methodology it
teaches. Compiled 2026-06-13 from a read-only audit of the state engine, ops
layer, and worker runner against the Claude Code Workflow tool's operating model.
Each item: evidence (`file:line`), severity, and a fix/roadmap sketch. The
methodology these gaps relate to lives in `workflow-patterns.md`.

Nothing here blocks daily use — the runner is a correct flat-fan-out-with-barrier
executor and the state engine's crash-safety is solid. These are the deltas
between "works" and "enforces good multi-agent practice by construction."

## Architectural (methodology outruns the engine)

### A1 — No pipelining / mid-phase expansion (barrier-only fan-out)  · HIGH
`run_all` freezes the agent set at run creation and `asyncio.gather`s it as one
flat batch (`scripts/workflow_run_codex.py` ~L923-930); the multi-phase outer
drivers barrier hard between phases (`workflow_runner_matrix.py` ~L433-437;
`workflow_start.py` planner→barrier→workers ~L304-323). Wall-clock is therefore
`Σ max(phase_p)`, not `max_chain(Σ)` — the exact cost the `pipeline()` primitive
avoids. The `{"kind":"workflow-expansion"}` envelope hinted in
`coding-cli-runners.md` is unimplemented.
**Roadmap:** replace the static `gather` in `run_all` with a dynamic task pool
(`asyncio.Queue` + worker pool) that accepts new agents mid-run; add a `stage`/
`depends_on` field to `parse_job` so an item can advance independently; gate
expansion with `max-round`/`max-job` guards. Substantial, not a patch. Until then,
**pipeline at the lead level** (see `workflow-patterns.md` §1): launch each
item's next-phase lane as its prior lane returns instead of barriering the batch.

### A2 — No schema-validated structured worker output  · HIGH
Every provider's `extract_result` returns raw text; the only validation in the
tree is the agent *lifecycle* status enum (`workflow_state.py:185`), never result
*content*. Even the bundled `structured` smoke job asks for JSON that nothing
parses (`workflow_runner_matrix.py` ~L42). Claude's tool forces schema-validated
returns (bad shape → auto-retry); this skill cannot guarantee a machine-readable
contract.
**Roadmap:** add `--result-schema <jsonschema>` (+ per-job `schema`); in
`run_worker`, `json.loads`+validate after extraction, route failures into the
existing retry loop with the validation error in the retry prompt, store the
parsed object as `result_json`. Until then, approximate via
`workflow-patterns.md` §10 (prompt the shape, lead validates, re-dispatch).

## Correctness / robustness

### A3 — No per-worker timeout (a hung worker wedges the whole barrier)  · MED ·most actionable
`--timeout-secs` is appended only to the `ccc` command (~L286), never to
`codex-direct`/`opencode-direct`/`kimi-direct`, and `run_worker` awaits
`proc.wait()` with no `asyncio.wait_for` (~L838). One wedged worker (e.g. blocked
on an approval prompt) blocks `asyncio.gather` indefinitely, and the matrix/start
outer `subprocess.run` calls have no `timeout=` either.
**Fix:** wrap the worker wait in `asyncio.wait_for(timeout=args.timeout_secs)`;
on timeout, `terminate_process_group` + mark `failed` (or retry); apply the flag
to all providers.

### A4 — Completion gate has two bypasses  · HIGH
The gate `wf done` enforces is undercut by: (H1) `wf set-status <run> completed`
sets status with no blocker check (`workflow_state.py:477-491`), documented as a
"recovery" escape; (H2) `wf verify --record-only --status passed --summary "ok"`
trivially satisfies the gate because a commandless check counts as valid evidence
on any non-empty summary (`workflow_health.py:149-156`, `:368`).
**Fix:** make `set-status completed/failed` require `--force`/`--allow-recovery`;
require `--record-only` to carry evidence provenance and emit a
`verification: external` event so the bypass is at least auditable.

### A5 — Stop does not promptly kill in-flight workers  · MED
`terminate_process_group` (SIGTERM-group → 3s → SIGKILL-group) is solid but only
called from the exception path (~L904); a cooperative `wf stop` mid-run is
detected only *between* awaits, so a live subprocess keeps running until it exits
on its own.
**Fix:** race `proc.wait()` against a stop-poll task; terminate on stop observed.

### A6 — `validate_status` raises `SystemExit` inside a held mutator  · MED
Accidentally safe (the `with exclusive_lock` finally releases and `save_run` is
skipped, discarding partial mutation) but fragile and leaks a traceback-style
exit. Validate all args *before* entering the mutator.

### A7 — Lock coverage is partial; `fcntl` absence degrades silently  · MED
`cmd_init` saves with no lock; `cmd_verify` writes its log before acquiring the
lock; `fcntl is None` silently means no locking. Atomic write itself is solid.
**Fix:** one-time stderr warning when `fcntl is None`; document the lock boundary.

## Methodology-enforcement nudges (the engine could teach the practice)

### A8 — Completed phase with zero agents is unenforced  · MED ·highest-value nudge
SKILL.md and `operations.md` both warn against it, but neither `analyze_run` nor
`structural_issues` ever checks phase agent membership on completion. A stated
rule with no code teeth.
**Fix:** WARNING in `analyze_run` when a `completed` phase has zero agents and no
artifact audit trail. **Implemented** (2026-06-13) as a non-blocking `phase-empty`
WARNING — a `lead-local` agent or a phase-tied artifact satisfies it; see
`tests/test_workflow.py::test_completed_phase_without_agents_warns_but_does_not_block`.

### A9 — Silent truncation (no "no silent caps")  · LOW
`add_event` hard-caps to 250 events with no marker (`workflow_state.py:180`); the
planner slices `jobs_raw[:max_jobs]` dropping extras silently
(`workflow_start.py:123`).
**Fix:** record an `info` event/decision when the planner truncates; write a
rollover marker when events hit the cap. (See `workflow-patterns.md` §9.)

### A10 — `status` health omits structural issues that `check`/`done` enforce  · MED
`cmd_status` derives health only from `analyze_run`; orphan links / invalid
statuses (`structural_issues`) surface only in `check`/`done`, so a run can read
"ok" in `status` but FAIL `check`.
**Fix:** fold `structural_issues` into the `status_line` summary.

### A11 — Lifecycle commands (`pause`/`resume`/`stop`) absent from the ops layer  · LOW-MED
They live only in the low-level state CLI; an operator told to "use the intent
commands" won't find lifecycle control there. Route them through `workflow_ops`
or document the split.

## Smaller drift / polish
- `--mode` is documented as a vocabulary but unvalidated and inconsistent
  (`native-subagents` vs default `hybrid`); schema-doc `coordinator.tool:
  "codex-direct"` vs hardcoded `"codex"` default.
- Runner matrix targets run sequentially (sum-of-targets wall-clock) — fine for
  correctness, a scalability limit for big matrices.
- Partial failure (9/10 workers OK) exits non-zero indistinguishably from total
  failure, and the matrix `break`s remaining phases on it — consider a `partial`
  status / `--allow-partial`.
- `parse_job` bare-prompt naming uses `hash()` (randomized per `PYTHONHASHSEED`)
  → non-reproducible agent ids + collision risk; use `hashlib.sha1(...)[:8]` or
  the job index.
- `ccc --ccc-runner claude` isn't in the selector cwd-forwarding sets, so the
  runner's `--cd`/`--work-dir` plumbing is skipped (works only via ccc's default).
- `load_run` on a missing id throws a raw `FileNotFoundError`; wrap callers to
  emit `no run '<id>' (try: wf list)`.

## Prioritization
1. **A3** (per-worker timeout) — most actionable correctness fix.
2. **A4** (gate bypasses) — closes the holes the completion gate exists to plug.
3. **A8** (zero-agent phase) — highest-value methodology nudge; cheapest.
4. **A1 / A2** — the two architectural parity gaps; biggest, plan deliberately.
5. Everything else as polish.

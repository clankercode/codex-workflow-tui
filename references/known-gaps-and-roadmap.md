# Known Gaps & Roadmap

An honest map of where the skill's *engine* currently trails the methodology it
teaches. Originally compiled 2026-06-13 from a read-only audit of the state
engine, ops layer, and worker runner against the Claude Code Workflow tool's
operating model; refreshed 2026-06-15 after the `workflow apply` launch path and
operator-trust fixes landed.
Each item: evidence (`file:line`), severity, and a fix/roadmap sketch. The
methodology these gaps relate to lives in `workflow-patterns.md`.

Nothing here blocks daily use — the runner is a correct flat-fan-out-with-barrier
executor and the state engine's crash-safety is solid. These are the deltas
between "works" and "enforces good multi-agent practice by construction."

## Architectural (methodology outruns the engine)

### A1 — No pipelining / mid-phase expansion (barrier-only fan-out)  · HIGH · **Implemented**
`run_all` was a static `asyncio.gather` over the initial agent set. It is now a
dynamic `asyncio.Queue` + worker pool (`scripts/workflow_run_codex.py` ~L1003+).
Completed workers can emit a `{"kind":"workflow-expansion","schema_version":1,
"jobs":[...]}` envelope; new jobs are enqueued mid-run and gated by
`--max-round`/`--max-job` caps with logged truncation. `parse_job` now carries
`stage`/`depends_on` metadata so items advance independently across a pipeline.
`workflow apply` also flattens declared `phases[].jobs` into staged jobs with
dependency edges.
**Tests:** `test_pipeline_respects_dependencies_across_stages`,
`test_expansion_envelope_enqueues_and_runs_new_jobs`,
`test_expansion_caps_hold_and_are_logged`.

### A1b — Workflow-plan phases are execution stages, not rich phase records  · MED · **Implemented**
`workflow apply` now instantiates declared plan phases as first-class phase
records, attaches workers to those declared phases, preserves phase gates and
planned-check metadata without treating them as completed verification, records
plan decisions, and keeps runtime expansion children attached to the parent
worker's phase.
**Tests:** `test_wf_apply_records_declared_phase_gates_and_plan_decisions`,
`test_wf_apply_expansion_agents_inherit_declared_phase`.

### A2 — No schema-validated structured worker output  · HIGH · **Implemented**
Added `--result-schema <jsonschema-file>` and per-job `schema` metadata. After
`extract_result`, `run_worker` parses the result as JSON and validates it with
`jsonschema`; failures are routed into the failure-retry loop with the
validation error injected into the retry prompt, and successful parses are
stored as `result_json` on the agent.
**Tests:** `test_schema_validation_passes_and_stores_result_json`,
`test_schema_validation_failure_marks_agent_failed`,
`test_schema_validation_failure_is_retried_with_error_in_prompt`.

## Correctness / robustness

### A3 — No per-worker timeout (a hung worker wedges the whole barrier)  · MED · **Implemented**
`wait_for_process_completion` now races process completion against stop requests
and `--timeout-secs`; on timeout it terminates the process group and marks the
worker failed. Runner-matrix also passes a subprocess timeout when requested.

### A4 — Completion gate has two bypasses  · HIGH · **Implemented**
`wf set-status completed/failed` now requires `--force` or `--allow-recovery`,
and `wf verify --record-only` requires evidence provenance via `--evidence-path`
or `--external-ref`. Record-only checks emit a `verification: external` event,
and legacy commandless passed checks without provenance no longer satisfy
`wf done`.

### A5 — Stop does not promptly kill in-flight workers  · MED · **Implemented**
`wait_for_process_completion` now races `proc.wait()` against a stop-poll task.
When a cooperative `wf stop` is observed first, the worker process group is
terminated and the agent is marked cancelled. The stop-poll task is cancelled
and drained on every exit path so the wait cannot hang.
**Test:** `test_stop_promptly_terminates_in_flight_worker`.

### A6 — `validate_status` raises `SystemExit` inside a held mutator  · MED · **Implemented**
Status values are now validated before entering mutators for phase, agent, and
run status updates.

### A7 — Lock coverage is partial  · MED
Most mutations use the per-run lock and `fcntl is None` now emits a one-time
warning instead of degrading silently. Remaining caveat: `cmd_init` creates a run
without the per-run lock because the run directory does not exist yet.

## Methodology-enforcement nudges (the engine could teach the practice)

### A8 — Completed phase with zero agents is unenforced  · MED ·highest-value nudge
SKILL.md and `operations.md` both warn against it, but neither `analyze_run` nor
`structural_issues` ever checks phase agent membership on completion. A stated
rule with no code teeth.
**Fix:** WARNING in `analyze_run` when a `completed` phase has zero agents and no
artifact audit trail. **Implemented** (2026-06-13) as a non-blocking `phase-empty`
WARNING — a `lead-local` agent or a phase-tied artifact satisfies it; see
`tests/test_workflow.py::test_completed_phase_without_agents_warns_but_does_not_block`.

### A9 — Silent truncation (no "no silent caps")  · LOW · **Implemented for known caps**
Planner truncation now records a decision/event when `--max-jobs` cuts a job
list, and event retention writes a rollover marker when the 250-event snapshot
cap drops old entries. Keep applying this rule to new caps as they appear.

### A10 — `status` health omits structural issues that `check`/`done` enforce  · MED · **Implemented**
`workflow_ops.status_line` and JSON status output now include both
`structural_issues` and `workflow_health.analyze_run` findings.

### A11 — Lifecycle commands (`pause`/`resume`/`stop`) absent from the ops layer  · LOW-MED · **Implemented**
`workflow_ops.py`, the `wf` wrapper, and the TUI command palette now expose
pause, resume, and stop.

## Smaller drift / polish
- `--mode` is documented as a vocabulary but unvalidated and inconsistent
  (`native-subagents` vs default `hybrid`); schema-doc `coordinator.tool:
  "codex-direct"` vs hardcoded `"codex"` default.
- Runner matrix targets run sequentially (sum-of-targets wall-clock) — fine for
  correctness, a scalability limit for big matrices.
- Partial failure (9/10 workers OK) exits non-zero indistinguishably from total
  failure, and the matrix `break`s remaining phases on it — consider a `partial`
  status / `--allow-partial`.
- `workflow apply` now supports first-class per-job worktree lanes
  (`worktree: true` or a worktree object) and launches workers inside the lane.
  Automated merge-back/merger-agent orchestration remains future work.
- `ccc --ccc-runner claude` isn't in the selector cwd-forwarding sets, so the
  runner's `--cd`/`--work-dir` plumbing is skipped (works only via ccc's default).
- `load_run` on a missing id throws a raw `FileNotFoundError`; wrap callers to
  emit `no run '<id>' (try: wf list)`.

## Prioritization
1. **A7** (remaining lock boundary documentation / init locking story).
2. Automated merge-back/merger-agent orchestration for worktree lanes.
3. Opaque running-agent/liveness invariant for truthful TUI state.
4. Everything else as polish.

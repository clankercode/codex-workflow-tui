# Workflow State Schema

Use this reference when reading or extending workflow state.

## Location

The source of truth is one JSON snapshot per run:

```text
~/.agents/workflow-system/state/runs/<run-id>/run.json
```

Each run directory also contains:

```text
artifacts/   prompts, final reports, worker outputs
logs/        worker transcripts, JSONL streams, and stderr logs
run.json     current state snapshot
```

That is the default for the recommended install at `~/.agents/skills/workflow` when launched through the `workflow` command. Set `WORKFLOW_HOME` to move the whole workflow system, or `WORKFLOW_STATE_DIR` to move only state.

## Top-Level Shape

```json
{
  "schema_version": 1,
  "run_id": "wf-20260611T044500Z-example-a1b2c3",
  "title": "Example workflow",
  "prompt": "Original objective",
  "cwd": "/repo/path",
  "mode": "hybrid",
  "status": "running",
  "tags": [],
  "created_at": "2026-06-11T04:45:00Z",
  "updated_at": "2026-06-11T04:47:00Z",
  "coordinator": {"tool": "codex-direct", "thread_id": null},
  "paths": {},
  "phases": [],
  "agents": [],
  "events": [],
  "decisions": [],
  "artifacts": [],
  "checks": [],
  "control": {},
  "metrics": {}
}
```

## Status Values

Use the same status vocabulary for runs, phases, and agents:

```text
pending
running
blocked
completed
failed
cancelled
paused
```

`completed` means the work item met its acceptance criteria. `failed` means it terminated with a failed command, unrecovered exception, or invalid result. `blocked` means human input or external state is required.

## Control State

Pause, resume, and stop requests are persisted under the optional top-level `control` object:

```json
{
  "control": {
    "paused": true,
    "pause_requested_at": "2026-06-11T04:50:00Z",
    "pause_reason": "operator requested pause",
    "resumed_at": null,
    "stop_requested": false
  }
}
```

`workflow pause <run-id>` sets the run status to `paused` and tells cooperative runners not to launch more workers. Already-running workers are allowed to finish.

`workflow resume <run-id>` clears `control.paused` and changes a paused run back to `running`.

`workflow stop <run-id>` sets `control.stop_requested`, marks unfinished phases and agents `cancelled`, and best-effort terminates recorded active worker process groups. Completed agent results and artifacts remain intact.

## Phase Records

```json
{
  "phase_id": "phase-research",
  "name": "Research",
  "goal": "Find prior art and implementation constraints",
  "order": 10,
  "status": "running",
  "created_at": "2026-06-11T04:45:00Z",
  "started_at": "2026-06-11T04:45:00Z",
  "completed_at": null,
  "agent_ids": ["agent-a"]
}
```

Phase ids may be stable human ids such as `phase-research`, or generated ids. Prefer stable ids when external tooling will reference them. Display order defaults to the persisted array order created by `add-phase`; tools may set optional integer `order` values if they need to display phases independently from insertion order.

## Agent Records

```json
{
  "agent_id": "codex-01-reviewer",
  "phase_id": "phase-review",
  "name": "Reviewer",
  "role": "code quality review",
  "agent_type": "codex-exec",
  "status": "running",
  "prompt": "Review the change...",
  "cwd": "/repo/path",
  "model": "gpt-5.5",
  "thread_id": "",
  "process_id": 12345,
  "process_group_id": 12345,
  "write_scope": ["src/review"],
  "jsonl_path": ".../logs/codex-01-reviewer.jsonl",
  "log_path": ".../logs/codex-01-reviewer.stderr.log",
  "output_path": ".../artifacts/codex-01-reviewer.final.md",
  "summary": "",
  "result": "",
  "exit_code": null,
  "created_at": "2026-06-11T04:45:00Z",
  "started_at": "2026-06-11T04:45:01Z",
  "completed_at": null,
  "updated_at": "2026-06-11T04:46:00Z"
}
```

For native subagents, set `agent_type` to `native-subagent` or the custom agent type, and store the subagent id in `thread_id` or `agent_id` as available from the current tool surface.
For work performed directly by the coordinator session, set `agent_type` to `lead-local` so completed phases still have an owner/audit record.

For external workers, `jsonl_path` is the durable transcript path. Direct Codex and OpenCode providers usually store JSONL there; `ccc-*` providers may store `transcript.txt` or `transcript.jsonl` there. The final answer is mirrored into `result` and `output_path`.

External cooperative workers should record both `process_id` and `process_group_id` when available. `workflow stop` prefers `process_group_id` so child CLI processes are terminated together.

## Token Telemetry

Token counts are derived from provider usage metadata in agent transcripts, never from output text length.

The TUI normalizes usage into:

```json
{
  "total": 1234,
  "input": 1000,
  "cached_input": 400,
  "output": 200,
  "reasoning": 34,
  "known": true,
  "total_source": "reported_total"
}
```

`total_source` is `reported_total` when the provider emits a total token count. It is `derived_from_provider_parts` when only input/output/reasoning parts are present. If no usage metadata exists, `known` is false and the TUI displays `unknown`. Mixed known/unknown run totals display `+?` to avoid presenting incomplete aggregate totals as exact.

Manual scripted agents may emit explicit zero usage in JSONL when they perform no model call. That is a real zero-token count and is distinct from missing usage.

## Optional Health And Verification Fields

Schema v1 remains additive. Old runs do not need these fields, but new operator commands may add them:

```json
{
  "status_reason": "waiting for credentials",
  "status_message": "Blocked until the operator refreshes API credentials.",
  "blocked_by": "operator",
  "last_activity_at": "2026-06-11T04:47:00Z",
  "checks": []
}
```

`status_reason` is a short machine-friendly reason. `status_message` is a readable operator note. `blocked_by` records the actor or external dependency responsible for a blocked state. `last_activity_at` is updated when new workflow events are recorded.

Checks are durable verification evidence:

```json
{
  "check_id": "chk-unit-tests",
  "ts": "2026-06-11T04:58:00Z",
  "name": "unit tests",
  "kind": "verification",
  "status": "passed",
  "required": true,
  "command": "python3 tests/test_workflow.py",
  "cwd": "/repo/path",
  "exit_code": 0,
  "duration_seconds": 24.6,
  "summary": "66 tests OK",
  "log_path": ".../logs/chk-unit-tests.log",
  "completed_at": "2026-06-11T04:58:25Z"
}
```

Valid check statuses are `passed`, `failed`, `error`, and `skipped`. `wf verify` records checks. `wf done` requires at least one required passing check by default, unless `--allow-unverified` or `--force` is used. Optional checks are visible evidence but do not satisfy the default completion gate. Commandless manual checks must include a non-empty `summary` to count as evidence.

For completion health, repeated checks are grouped by `(kind, name, command, cwd)` and the latest record in that identity is authoritative. A later passing exact rerun resolves an earlier failed check with the same identity; a different command or name is a different verification identity. Executed checks must have `exit_code=0` to count as passing evidence, even if a malformed or hand-edited record says `status=passed`.

The lower-level `workflow set-status` command remains available for recovery and compatibility.

## Events

Events are a bounded recent timeline inside `run.json`.

```json
{
  "event_id": "evt-abc123",
  "ts": "2026-06-11T04:47:00Z",
  "level": "info",
  "kind": "agent",
  "operation": "updated",
  "source": "workflow_state.update_agent",
  "message": "agent updated: reviewer",
  "phase_id": "phase-review",
  "agent_id": "codex-01-reviewer",
  "data": {"status": "completed", "agent_type": "codex-direct"}
}
```

`kind`, `operation`, and `source` are optional for old events but preferred for new events. Free-form manual notes should use `kind=run_note` and `operation=note` unless they can name a more specific action. Current tooling keeps the newest 250 events in the snapshot. Future tooling can add an append-only `events.jsonl` without changing the snapshot fields.

## Decisions

Decisions are durable choices that should remain visible after event rollover.

```json
{
  "decision_id": "dec-runner-ccc-opencode",
  "ts": "2026-06-11T04:46:00Z",
  "title": "Runner selected: ccc-opencode",
  "rationale": "Run 3 coding-CLI workers with max_agents=4, startup_delay=1.0, sandbox=read-only.",
  "made_by": "workflow_run.py"
}
```

Use decisions for runner/provider choices, scope changes, risk acceptances, and architectural tradeoffs. `workflow_run.py` automatically records its runner/concurrency choice.

## Artifacts

Artifacts are durable outputs that should be easy to navigate from the TUI.

```json
{
  "artifact_id": "art-ccc-opencode-01-review-output",
  "ts": "2026-06-11T04:50:00Z",
  "kind": "worker-output",
  "title": "review final output",
  "path": "/abs/path/to/output.txt",
  "phase_id": "phase-cli-workers",
  "agent_id": "ccc-opencode-01-review"
}
```

Use `kind=file` for manually recorded reports and `kind=worker-output` for worker final outputs. `workflow_run.py` automatically exposes each worker final output as an artifact.

## Write Semantics

Use `workflow_state.py` for primitive mutations and `workflow_ops.py` for operator intent commands. Snapshot writes use temp-file-plus-rename and refresh derived metrics. External tools should treat `metrics` as derived and rebuildable.

Do not infer lifecycle status from logs. Logs can inform summaries, but status changes must be explicit state updates.

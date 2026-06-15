# Workflow Operations

Use these commands when operating a workflow. Prefer `workflow apply` for repeatable workflows, then drop down to `workflow start`, `workflow run`, or manual state commands only when they fit the job better.

## Launch From A Workflow File

For saved workflows, checked-in workflow specs, or generator scripts, use `workflow apply`:

```bash
workflow apply workflows/review.workflow.json \
  --runner ccc \
  --ccc-runner @mm \
  --max-agents 4 \
  --startup-delay 1.0
```

`workflow exec` is an alias for the same command.

The input may be JSON or an executable/Python script that prints JSON. The object should use `kind: "workflow-plan"` and include either:

- `jobs`: a flat list of worker jobs
- `phases[].jobs`: staged jobs; later phase jobs depend on prior phase jobs

Job `name` values are dependency keys. Keep them unique and stable across the whole plan, especially when using explicit `depends_on` or `phases[].jobs`.

The launcher records the normalized plan as a `workflow-plan` artifact, preserves execution fields such as `cwd`, `runner`, `ccc_runner`, `tags`, model, sandbox/approval settings, and caps, and lets explicit CLI flags override plan defaults.

Jobs and phases may override execution settings (`runner`, `ccc_runner`, `model`, `sandbox`, `approval`, etc.) with root < phase < job precedence. CLI flags override all plan-provided values.

Multi-phase apply preserves stage order with dependencies and records declared phases, gates, and planned checks.

Jobs can declare `worktree: true` or a `worktree` object. `workflow apply` creates each lane before launch and runs that worker in the lane cwd. After lane workers complete, merge completed lane branches back into the run cwd:

```bash
workflow merge-lanes <run-id>
```

`merge-lanes` only considers completed agents with `worktree.branch` metadata, skips lanes already marked merged, aborts if the run cwd is dirty, and records `worktree` events for successful merges or conflicts. Use `--agent <id-or-name>` to merge selected lanes.

When a lane has a declared `write_scope`, `merge-lanes` checks whether changed files fall outside that scope and records a `scope-violation` warning event and includes `scope_warnings` in the merge result. This catches workers that edited files beyond their owned lane before integration. If the scope check itself cannot run (e.g. an unresolvable base ref), `merge-lanes` records a `scope-check-error` warning instead of silently reporting a clean pass.

### Conflict Assist

When `merge-lanes` records a conflict, use `merge-conflicts` to prepare a merger-agent prompt and context artifact:

```bash
workflow merge-conflicts <run-id> [--agent <agent-id>]
```

The command finds the conflicted lane (or the named agent if `--agent` is given), refuses to run if no conflicted lane is recorded, and refuses if the conflicted agent has no `merge_check_id` (the conflict would otherwise borrow unrelated evidence). It gathers the failed merge log, conflicted files, branch metadata, and original task prompt, then writes:

- A **merger prompt** artifact (`merger-prompt-<agent-id>.md`) with bounded resolution instructions.
- A **merger context** artifact (`merger-context-<agent-id>.json`) with structured conflict metadata (`conflict_files`, `cwd_has_conflict_markers`, `cwd_has_unrelated_changes`, `merge_in_progress`, `merge_check_id`).
- A `merge-conflict-assist` event in run state. The prompt and context artifacts are the durable record; no pending check is created (the resolution is recorded as a fresh verification below).

The command distinguishes three cwd states:

- **Merge in progress** (`cwd_has_conflict_markers: true`) — `merge-lanes --leave-conflicts` left the conflict in place. The context includes the conflicted files; a merger agent can work on them.
- **Cwd has unrelated dirty changes** (`cwd_has_unrelated_changes: true`) — the previous merge was aborted but the user has uncommitted work of their own. The command prints a hint to commit or stash those changes first (`merge-lanes` refuses a dirty tree), then re-create the conflict markers before resolving.
- **Cwd is clean** — the previous merge was aborted and nothing else is dirty. The command prints a hint to re-create the conflict markers via `merge-lanes --leave-conflicts` (or `git merge --no-edit <branch>`) before re-running `merge-conflicts`.

The merger prompt is designed to be fed to any coding-CLI worker (Codex, Kimi, OpenCode, ccc) for automated resolution. Human verification is always required before marking the workflow complete — the merger agent's output is not trusted without review. After resolution, record a passing verification check explicitly (a fresh check; do not reuse an existing `check_id`, which would duplicate it):

```bash
wf verify <run-id> \
  --record-only --status passed --summary "merge resolved" \
  --evidence-path <path to resolution evidence>
```

Then re-run `wf merge-lanes <run-id>` so the resolved lane is marked merged. The lane is not auto-skipped after a conflict: re-running `merge-lanes` re-attempts the merge, which completes cleanly ("Already up to date") once the resolution is committed.

## Create A Run

Use manual run creation when the lead session is doing the orchestration by hand or a workflow shape cannot yet be represented as a `workflow-plan`:

```bash
workflow init \
  --title "PR review" \
  --prompt "Review the current branch with independent lanes" \
  --cwd "$PWD" \
  --mode native-subagents \
  --tag review
```

The command prints the `run_id` and `run.json` path.

## Add Phases

```bash
workflow add-phase <run-id> \
  --phase-id phase-research \
  --name "Research" \
  --goal "Understand code and docs" \
  --status running
```

## Track Native Subagents

Native subagents are controlled by the host session, not by the workflow runner. Track them in workflow state only when you can keep status and output coherent. If the host does not expose live status/update hooks, prefer recording a lead-local event, artifact, or completed summary after the subagent returns rather than creating a stale `running` agent.

Running managed agents should have a liveness source. External workers normally record `process_id` and `jsonl_path`; native subagents should record a host id in `thread_id`. If a sidecar is intentionally unmanaged, set `unmanaged: true` in state so health checks know the missing process/transcript data is expected. Native subagents without a `process_id` or `jsonl_path` are auto-classified as effectively unmanaged; health checks do not demand liveness sources that the workflow runner cannot provide.

When you do have a returned subagent id and will update it later, add it to state:

```bash
workflow add-agent <run-id> \
  --phase phase-research \
  --name "Security researcher" \
  --role "security review" \
  --agent-type native-subagent \
  --agent-id "<subagent-id>" \
  --status running \
  --prompt-file prompts/security.md
```

When it returns:

```bash
workflow update-agent <run-id> <subagent-id> \
  --status completed \
  --summary "No critical issues found" \
  --result-file results/security.md
```

If the lead session does the work locally instead of delegating it, still leave an agent-shaped audit trail before closing the phase:

```bash
workflow add-agent <run-id> \
  --phase phase-implementation \
  --name "Lead local implementation" \
  --role "implementation" \
  --agent-type lead-local \
  --status completed \
  --prompt "Implemented directly in the coordinator session; see events and artifacts."
```

This keeps phase views honest: a completed phase with no agents means no worker or local-owner record was captured.

## Run External Coding-CLI Workers

When you have a broad goal but not a hand-written job list, let a planner agent create the workflow:

```bash
workflow start "write a concise architecture review for this repository" \
  --runner ccc \
  --ccc-runner @mm \
  --max-agents 4 \
  --startup-delay 1.0
```

`workflow start` asks the planner for up to `--max-jobs` independent jobs, creates the workflow run, records the generated plan as both a decision and a `generated-plan` artifact, and launches the jobs with the selected worker runner. Planner-specific flags are prefixed with `--planner-*`; if omitted, the planner uses the worker runner settings.

Useful rehearsal modes:

```bash
workflow start "summarize the current repo" --mock
workflow start "summarize the current repo" --mock-plan --runner ccc-opencode
```

`--mock` avoids model calls for the planner and workers. `--mock-plan` uses the deterministic local planner but still runs real workers unless combined with `--dry-run` or `--mock`.

For deterministic one-stage fan-out from shell, use the lower-level runner:

```bash
workflow run \
  --title "Parallel review" \
  --cwd "$PWD" \
  --runner ccc-opencode \
  --permission-mode safe \
  --max-agents 4 \
  --startup-delay 1.0 \
  --job "security::Review this branch for security risks. Return file-linked findings." \
  --job "tests::Review missing or weak tests. Return concrete gaps." \
  --job "maintainability::Review maintainability and complexity risks."
```

Use `--runner codex-direct` for direct `codex exec --json` workers:

```bash
workflow run \
  --title "Parallel review" \
  --cwd "$PWD" \
  --runner codex-direct \
  --sandbox read-only \
  --approval never \
  --max-agents 3 \
  --job "security::Review this branch for security risks. Return file-linked findings."
```

Use `--runner kimi-direct` for direct Kimi CLI workers:

```bash
workflow run \
  --title "Kimi review" \
  --cwd "$PWD" \
  --runner kimi-direct \
  --max-agents 3 \
  --job "review::Review this branch for correctness and maintainability."
```

The launcher creates one run, one phase, one agent record per worker, durable transcripts, stderr logs, and final output files.
It also records the runner/concurrency choice as a decision and exposes each worker final output in the Artifacts tab.

By default, the launcher runs at most 4 agents at once and starts no more than one worker per second. Tune this with `--max-agents <n>` and `--startup-delay <seconds>`.
For `ccc` workers, the default output mode is `stream-json` so the TUI can show live output, latest tool calls, and token stats when the provider emits them.

For generic `ccc` targets, pass either a runner selector or a preset:

```bash
workflow run \
  --title "MiniMax review" \
  --cwd "$PWD" \
  --runner ccc \
  --ccc-runner @mm \
  --job "review::Review the current branch."
```

To smoke-test several runners with the same reusable job set:

```bash
workflow runner-matrix \
  --target codex-direct \
  --target kimi-direct \
  --target ccc:kimi \
  --target minimax=ccc:@mm \
  --output-dir ~/tmp/workflow-runner-matrix
```

The matrix command writes a summary JSON, copies the smoke jobs file into the output directory, and archives each target's workflow state, logs, and artifacts.
Use `--mock` first to validate state and archive behavior without model calls.

Use `--sandbox workspace-write` only for workers that may edit files, and give each worker a disjoint file scope in its prompt.

Use `--mock` or `--dry-run` to test state and TUI behavior without model calls.

For provider details, read `coding-cli-runners.md`.

## Pause, Resume, And Stop

Workflow control is cooperative and state-driven:

```bash
workflow pause <run-id> --reason "waiting for quota window"
workflow resume <run-id>
workflow stop <run-id> --reason "wrong prompt"
```

Pause prevents cooperative runners from launching more workers. It does not interrupt already-running workers; they can finish and record their artifacts.

Resume clears the pause flag and lets pending workers launch again.

Stop cancels unfinished phases and agents, records the stop request in `control`, and best-effort sends `SIGTERM` to recorded worker process groups. Completed agent outputs are preserved. Use `--no-terminate` when you only want state updated.

The live TUI exposes these actions from `Ctrl-P`:

- `Workflow: Pause selected run`
- `Workflow: Resume selected run`
- `Workflow: Stop selected run`

## Run The Fibonacci Stress Test

Use the scripted reduction-tree stress test when you want a large workflow with many agents but no model spend:

```bash
workflow fibonacci-stress \
  --n 100 \
  --output-dir ~/tmp/custom-wf-test
```

For `F(100)`, this creates 99 completed manual agents:

- 50 leaf agents compute one independent binomial term each.
- 49 reducer agents each perform exactly one sum.
- Reduction phases have agent counts `50, 25, 12, 6, 3, 2, 1`.

The command verifies the final value with an independent iterative Fibonacci implementation and writes:

- final answer artifact
- reduction-tree JSON artifact
- timing JSON artifact with total agents, e2e time, average agent time, and longest agent
- archived `run.json` plus artifacts under `<output-dir>/archive/<run-id>/`

The manual agents emit explicit zero-token usage metadata, so the TUI can distinguish real zero-token scripted work from unknown model usage.

## Token Telemetry

The TUI never estimates tokens from output text.

- If a provider reports `total_tokens` or equivalent, that reported total is used.
- If a provider reports input/output/reasoning parts without a total, the TUI derives `total = input + output + reasoning` and labels it `derived`.
- If an agent has no usage metadata, token totals show `unknown`.
- If a run mixes known and unknown agents, the run total shows `+?` to mark the aggregate incomplete.

Prefer `ccc` `stream-json` mode, direct `codex --json`, or `opencode --format json` for the best usage telemetry.

## View State

```bash
workflow status
workflow last
workflow list
workflow show <run-id> --detail
workflow tui
```

If installed command symlinks are present, the short forms are:

```bash
workflow tui
workflow list
workflow show <run-id> --detail
wf list
wf show <run-id> --detail
wf status
wf last --cwd
wf doctor
wf check <run-id>
wf verify <run-id> --cmd "python3 tests/test_workflow.py"
wf done <run-id>
wf block <run-id> "waiting for credentials"
wf start "review this repository and fix the highest-impact issues" --runner ccc --ccc-runner @mm
wf preview --title "review" --job "security::Review this branch."
wf tui
wf run --runner ccc-opencode --title "..." --job "name::prompt"
wf run-codex --title "..." --job "name::prompt"
workflow-state list
workflow-state show <run-id> --detail
workflow-tui
workflow-run --runner ccc-opencode --title "..." --job "name::prompt"
workflow-run-codex --title "..." --job "name::prompt"
```

The global command installed for everyday use is `~/.local/bin/workflow`; `~/.local/bin/wf` is the shorter alias.
The live TUI uses Textual from `$WORKFLOW_HOME/.venv` when launched through these aliases. With the recommended user-scope install, that is `~/.agents/workflow-system/.venv`.

The live TUI checks the workflow skill git upstream in the background. Use the Textual command palette for `Workflow: Check for updates` or `Workflow: Update skill from git`; the update action runs `git pull --ff-only` in the skill checkout and reports success or failure as a notification.

The equivalent shell update is:

```bash
cd ~/.agents/skills/workflow
git pull --ff-only
```

For deterministic visual checks, render Rich snapshots:

```bash
workflow tui --snapshot \
  --fixture ~/.agents/skills/workflow/tests/fixtures/rich-workflow.json \
  --tab agents \
  --width 110 \
  --height 30
```

For interactive navigation QA, use the tmux harness. It stages fixture state, starts a new tmux session, drives the TUI with keys, and writes one capture log:

```bash
python3 ~/.agents/skills/workflow/scripts/workflow_tui_tmux_qa.py \
  --session workflow-tui-qa \
  --output-dir /tmp/workflow-tui-qa
```

Use `--dry-run` to print the key/capture plan without starting tmux.

## TUI Keys

- `Up`/`Down`: move within the current sidebar list.
- `Right`/`Left`: move across top-level tabs.
- `Enter`: focus the selected detail full-width.
- `Escape`: leave focus mode, or quit when not focused.
- `/`: cycle common filters.
- `!`: jump back to the attention overview.
- `c`: clear the active filter.
- `a`: toggle Agents between selected-phase scope and all agents.
- `v`: toggle the selected agent detail between live output and prompt.
- `y`: copy the selected row id.
- `p`: copy the selected row's useful path.
- `Ctrl-Y`: copy the selected row as JSON.
- `Ctrl-P`: open the Textual command palette, including workflow update commands.

## Record Verification

```bash
workflow verify <run-id> \
  --name "unit tests" \
  --cmd "python3 tests/test_workflow.py"
```

`workflow verify` stores a structured `checks[]` record and a log file when the command emits output. Bare manual verification is rejected; use `--record-only --status <status> --summary <evidence>` when the evidence is external, and do not combine `--record-only` with `--cmd`. By default, the safer completion command requires at least one required passing check:

```bash
workflow done <run-id>
```

If a verification command fails, rerun the same `--name`, `--cmd`, and `--cwd` after fixing the issue. Completion health uses the latest check for that identity, so the later exact passing rerun resolves the earlier failure.

Use `workflow done <run-id> --allow-unverified` only when a run's evidence is intentionally external. The primitive `workflow set-status <run-id> completed` remains available for recovery and compatibility.

## Operator Health Commands

Use the intent commands before digging through raw JSON:

```bash
workflow status --cwd
workflow doctor
workflow check <run-id>
```

`status` summarizes active and recent runs with derived health. `doctor` checks state writability, command availability, and TUI dependencies. `check` validates one run for failed/blocked/stale work, missing artifacts, invalid links, failed checks, and other operator-facing issues.

# Workflow Operations

Use these commands when operating a workflow manually.

## Create A Run

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

After spawning a native subagent from the main session, add it to state:

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

For deterministic fan-out from shell:

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

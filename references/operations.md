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

Use `--sandbox workspace-write` only for workers that may edit files, and give each worker a disjoint file scope in its prompt.

Use `--mock` or `--dry-run` to test state and TUI behavior without model calls.

For provider details, read `coding-cli-runners.md`.

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
- `a`: toggle Agents between selected-phase scope and all agents.
- `v`: toggle the selected agent detail between live output and prompt.
- `y`: copy the selected row id.
- `p`: copy the selected row's useful path.
- `Ctrl-Y`: copy the selected row as JSON.
- `Ctrl-P`: open the Textual command palette, including workflow update commands.

## Record Verification

```bash
workflow event <run-id> \
  --level info \
  --message "verification passed: pytest tests/test_workflow.py"
```

Only mark the run complete after verification evidence is recorded:

```bash
workflow set-status <run-id> completed
```

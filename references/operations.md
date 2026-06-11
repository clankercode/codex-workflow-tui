# Workflow Operations

Use these commands when operating a workflow manually.

## Create A Run

```bash
python3 ~/.llm-general/ai-coding/codex/skills/workflow/scripts/workflow_state.py init \
  --title "PR review" \
  --prompt "Review the current branch with independent lanes" \
  --cwd "$PWD" \
  --mode native-subagents \
  --tag review
```

The command prints the `run_id` and `run.json` path.

## Add Phases

```bash
python3 ~/.llm-general/ai-coding/codex/skills/workflow/scripts/workflow_state.py add-phase <run-id> \
  --phase-id phase-research \
  --name "Research" \
  --goal "Understand code and docs" \
  --status running
```

## Track Native Subagents

After spawning a native subagent from the main session, add it to state:

```bash
python3 ~/.llm-general/ai-coding/codex/skills/workflow/scripts/workflow_state.py add-agent <run-id> \
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
python3 ~/.llm-general/ai-coding/codex/skills/workflow/scripts/workflow_state.py update-agent <run-id> <subagent-id> \
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

## View State

```bash
python3 ~/.llm-general/ai-coding/codex/skills/workflow/scripts/workflow_state.py list
python3 ~/.llm-general/ai-coding/codex/skills/workflow/scripts/workflow_state.py show <run-id> --detail
python3 ~/.llm-general/ai-coding/codex/skills/workflow/scripts/workflow_tui.py
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
The live TUI uses Textual from `~/.llm-general/ai-coding/codex/workflow-system/.venv` when launched through these aliases.

For deterministic visual checks, render Rich snapshots:

```bash
workflow tui --snapshot \
  --fixture ~/.llm-general/ai-coding/codex/skills/workflow/tests/fixtures/rich-workflow.json \
  --tab agents \
  --width 110 \
  --height 30
```

For interactive navigation QA, use the tmux harness. It stages fixture state, starts a new tmux session, drives the TUI with keys, and writes one capture log:

```bash
python3 ~/.llm-general/ai-coding/codex/skills/workflow/scripts/workflow_tui_tmux_qa.py \
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

## Record Verification

```bash
python3 ~/.llm-general/ai-coding/codex/skills/workflow/scripts/workflow_state.py event <run-id> \
  --level info \
  --message "verification passed: pytest tests/test_workflow.py"
```

Only mark the run complete after verification evidence is recorded:

```bash
python3 ~/.llm-general/ai-coding/codex/skills/workflow/scripts/workflow_state.py set-status <run-id> completed
```

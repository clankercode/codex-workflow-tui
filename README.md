# Codex Workflow

Stateful multi-agent coding workflows for Codex, with a Textual TUI and provider-neutral worker runners.

Site: https://clankercode.github.io/codex-workflow-tui/

## What It Provides

- A global Codex skill named `workflow`
- Persistent workflow state under `~/.llm-general/ai-coding/codex/workflow-system/state`
- Native subagent operating guidance for parallel research, implementation, review, and synthesis
- External coding-CLI workers through direct Codex, direct OpenCode, and `ccc` providers
- Rate limits for worker startup and concurrency
- A Rich/Textual TUI for active runs, phases, agents, events, decisions, artifacts, live output, and copyable ids/paths
- Snapshot fixtures and tmux-driven visual QA tests

## Commands

```bash
workflow tui
workflow init --title "Example" --prompt "Do the thing" --cwd "$PWD"
workflow run --runner ccc-opencode --max-agents 4 --startup-delay 1.0 --job "review::Review this branch."
python3 scripts/workflow_tui_tmux_qa.py
python3 tests/test_workflow.py
```

`workflow` and `wf` are expected to point at `scripts/wf`.

## Installation

For a global Codex install, symlink this directory into the Codex skills path:

```bash
ln -sfn "$PWD" ~/.codex/skills/workflow
ln -sfn "$PWD/scripts/wf" ~/.local/bin/workflow
ln -sfn "$PWD/scripts/wf" ~/.local/bin/wf
```

Install the TUI dependency in the workflow virtualenv or current Python environment:

```bash
python3 -m pip install -r scripts/requirements-tui.txt
```

## Documentation

Start with `SKILL.md`, then see:

- `references/operations.md`
- `references/state-schema.md`
- `references/coding-cli-runners.md`
- `references/claude-code-parity.md`
- `references/codex-cli-workers.md`

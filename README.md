# Codex Workflow

Stateful multi-agent coding workflows for Codex, with a Textual TUI and provider-neutral worker runners.

Site: https://clankercode.github.io/codex-workflow-tui/

## What It Provides

- A global Codex skill named `workflow`
- Persistent workflow state beside the skill checkout, e.g. `~/.agents/workflow-system/state`
- Native subagent operating guidance for parallel research, implementation, review, and synthesis
- Saved `workflow-plan` JSON or generator-script launch with recorded plan artifacts
- External coding-CLI workers through direct Codex, direct OpenCode, direct Kimi, and `ccc` providers
- Rate limits for worker startup and concurrency
- A Rich/Textual TUI for active runs, phases, agents, events, decisions, artifacts, live output, and copyable ids/paths
- Snapshot fixtures and tmux-driven visual QA tests

## Commands

```bash
workflow tui
workflow apply workflows/review.workflow.json --runner ccc --ccc-runner @mm --max-agents 4
workflow init --title "Example" --prompt "Do the thing" --cwd "$PWD"
workflow run --title "Review lanes" --runner ccc-opencode --max-agents 4 --startup-delay 1.0 --job "review::Review this branch."
workflow run --title "Kimi lane" --runner kimi-direct --job "review::Review this branch."
workflow runner-matrix --target kimi-direct --target ccc:kimi --mock --output-dir ~/tmp/workflow-runner-matrix
workflow fibonacci-stress --n 100 --output-dir ~/tmp/custom-wf-test
python3 scripts/workflow_tui_tmux_qa.py
python3 tests/test_workflow.py
```

`workflow` and `wf` are expected to point at `scripts/wf`.

## Installation

Clone the skill into Codex's user-scope skill folder so future updates are a fast-forward pull:

```bash
mkdir -p ~/.agents/skills ~/.local/bin
git clone https://github.com/clankercode/codex-workflow-tui.git \
  ~/.agents/skills/workflow
cd ~/.agents/skills/workflow
```

Codex scans `$HOME/.agents/skills` automatically. Add the command aliases:

```bash
ln -sfn "$PWD/scripts/wf" ~/.local/bin/workflow
ln -sfn "$PWD/scripts/wf" ~/.local/bin/wf
```

Install the TUI dependency in the workflow virtualenv or current Python environment:

```bash
python3 -m pip install -r scripts/requirements-tui.txt
```

Update later from the TUI command palette with `Workflow: Update skill from git`, or from a shell:

```bash
cd ~/.agents/skills/workflow
git pull --ff-only
```

## Documentation

Start with `SKILL.md`, then see:

- `references/workflow-patterns.md` — orchestration pattern library (pipeline-by-default, adversarial/diverse verify, loop-until-dry, completeness critic, structured returns)
- `references/operations.md`
- `references/state-schema.md`
- `references/coding-cli-runners.md`
- `references/claude-code-parity.md`
- `references/known-gaps-and-roadmap.md` — where the engine trails the methodology, ranked
- `references/codex-cli-workers.md`
- `examples/runner-smoke-jobs.json`

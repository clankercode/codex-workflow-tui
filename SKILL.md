---
name: workflow
description: Coordinate stateful multi-agent coding workflows. For large parallelizable tasks, ask whether to use this skill.
---

# Workflow

## Operating Principle

Treat the current coding-agent conversation as the lead agent: keep decisions, acceptance criteria, and synthesis here. Move noisy exploration, review, and bounded implementation lanes into subagents or external coding-CLI workers, and persist every run to workflow state before substantial work begins.

## Start Every Workflow

1. Decide whether this is a workflow. Use a workflow when at least two independent lanes exist, output would otherwise pollute the main context, or the user asks for subagents, agent teams, dynamic workflows, a workflow TUI, or active workflow state.
2. Initialize state before delegating:

```bash
workflow init \
  --title "short workflow title" \
  --prompt "original user request or compact objective" \
  --cwd "$PWD" \
  --mode hybrid
```

3. Add phases that match how the work will be judged. Prefer phases such as `research`, `design`, `implementation`, `review`, and `verification`.
4. Spawn native subagents for sidecar tasks when the current session exposes subagent tools. For each spawned subagent, add an agent record with its prompt, scope, and returned agent id.
5. For larger or more isolated work, launch external coding-CLI workers with `workflow_run.py`. These workers can use direct Codex, `ccc`-wrapped Codex, `ccc`-wrapped OpenCode, or another `ccc` runner while updating the same state files.
   Use `--max-agents` to cap simultaneous workers and `--startup-delay` to pace launches; defaults are 4 workers and 1.0 seconds.
6. Keep state current: mark phases and agents `running`, `blocked`, `completed`, or `failed`; record important choices with `workflow decision`, durable outputs with `workflow artifact`, and verification/result summaries as events.
   If the lead session implements a phase locally, add a `lead-local` agent or a decision/event before marking that phase complete so the TUI does not show a mysterious empty implementation phase.
7. Use the TUI while work is active:

```bash
workflow tui
```

The live TUI is Textual-based and the snapshot renderer uses Rich panels/tables.
The installed `workflow`/`wf` aliases use the private workflow virtualenv when it exists.
In the TUI, use arrow keys to move rows and sections, `a` to toggle phase/all agent scope, `v` to toggle live output/prompt, `y` to copy the selected id, `p` to copy the useful path, and `Ctrl-Y` to copy selected-row JSON.
Use `Ctrl-P` for the Textual command palette; it includes `Workflow: Check for updates` and `Workflow: Update skill from git`.

or, without the installed alias:

```bash
python3 ~/.agents/skills/workflow/scripts/workflow_tui.py
```

## Delegation Rules

- Use native subagents first for small to medium parallel tasks inside the current turn. They are best for research, review, test triage, and disjoint code lanes.
- Prefer the installed custom agents when they fit: `workflow-explorer` for read-only research, `workflow-reviewer` for review, `workflow-implementer` for bounded edits, and `workflow-synthesizer` for combining worker results.
- Use external coding-CLI workers for larger tasks that benefit from their own independent process, durable transcript, or long-running isolation.
- Do not delegate the immediate blocking task. Keep the critical path local while sidecar agents work.
- Give every agent a bounded scope, expected output, file ownership if it may edit, and a reminder that other agents may be working concurrently.
- Prefer read-only agents for research and review. Give write access only when the lane owns a disjoint file set or worktree.
- For write-heavy parallelism, use worktrees or explicit file ownership. Avoid two agents editing the same file.
- Always verify subagent and worker results before presenting them as true.

## State Contract

State lives under:

```text
~/.agents/workflow-system/state/runs/<run-id>/run.json
```

This is the default when the skill is installed at `~/.agents/skills/workflow` and launched through the `workflow` command. `WORKFLOW_STATE_DIR` can override it. This is the stable integration point for tooling. Do not invent ad hoc status files for workflow runs. Use the bundled state script instead.

Read `references/state-schema.md` before building tools that consume workflow state. Read `references/operations.md` for command examples and lifecycle conventions.

## Claude Code Parity Targets

This skill deliberately mirrors the useful parts of Claude Code workflows:

- subagents: delegated workers with isolated context and concise summaries
- agent view: a TUI over active and completed runs
- dynamic workflows: scripted fan-out/fan-in with persistent phase and agent results
- agent teams: role separation, task ownership, and synthesis by a lead agent
- hooks/status: machine-readable state that external tools can watch

Read `references/claude-code-parity.md` when designing or extending the workflow system.

## Coding CLI Workers

Use external coding-CLI workers when the task needs a separate process, durable logs, or a bigger isolated run:

```bash
workflow run \
  --title "review lanes" \
  --cwd "$PWD" \
  --runner ccc-opencode \
  --max-agents 4 \
  --startup-delay 1.0 \
  --sandbox read-only \
  --job "security::Review this branch for security issues. Return findings with file paths." \
  --job "tests::Review this branch for missing or weak tests. Return gaps and suggested checks."
```

For generic `ccc`, `--ccc-runner` accepts either a CLI selector such as `kimi` or a preset such as `@mm`:

```bash
workflow run --runner ccc --ccc-runner @mm --title "review" --job "review::Review this branch."
```

Read `references/coding-cli-runners.md` before using this path for write access.

## Stress Testing

Use the built-in Fibonacci reduction tree to stress workflow state, artifacts, TUI navigation, and token telemetry without spending model calls:

```bash
workflow fibonacci-stress --n 100 --output-dir ~/tmp/custom-wf-test
```

For `F(100)`, this creates 99 completed manual agents: 50 independent binomial-term agents and 49 one-sum reducers. The final answer, reduction tree, timing data, and run snapshot are archived under the output directory.

Token totals in the TUI come only from reported usage metadata. If a provider reports `total_tokens`, that total is used. If it reports input/output/reasoning parts without a total, the TUI derives and labels the total. If usage is missing, the TUI shows `unknown`; it does not estimate from text length.

## Completion Gate

Before saying the workflow is done:

1. Re-open the run state and confirm every required phase is complete or explicitly waived.
2. Read every subagent or worker summary, not just the top-level status.
3. Run the verification commands appropriate to the actual work.
4. Record verification as a workflow event.
5. Mark the run `completed` only after the evidence supports it.

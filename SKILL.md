---
name: workflow
description: Coordinate stateful multi-agent coding workflows. For large parallelizable tasks, ask whether to use this skill.
---

# Workflow

## Operating Principle

Treat the current coding-agent conversation as the lead agent: keep decisions, acceptance criteria, and synthesis here. Move noisy exploration, review, and bounded implementation lanes into subagents or external coding-CLI workers, and persist every run to workflow state before substantial work begins.

## Start Every Workflow

1. Decide whether this is a workflow. Use a workflow when at least two independent lanes exist, output would otherwise pollute the main context, or the user asks for subagents, agent teams, dynamic workflows, a workflow TUI, or active workflow state.
2. Choose the front door:
   - For repeatable or complex workflows, prefer a saved `workflow-plan` JSON file or generator script and launch it with `workflow apply` (alias: `workflow exec`). This records the normalized plan as a durable artifact before workers start.
   - For a broad natural-language goal with no plan file yet, use `workflow start "<goal>"` so a planner creates the first job set.
   - For a simple flat batch with a hand-written job list, use `workflow run`.
   - Use `workflow init` plus manual `add-phase`/`add-agent` only for manual bookkeeping or lead-local work that cannot yet be represented by a plan.

```bash
workflow apply workflows/review.workflow.json \
  --runner ccc \
  --ccc-runner @mm \
  --max-agents 4
```

Generator scripts must print one JSON `workflow-plan` object. Job `name` values must be unique and stable across the whole plan because dependency edges use those names. `workflow apply` preserves plan metadata such as `cwd`, `runner`, `ccc_runner`, `tags`, `model`, sandbox/approval settings, retry policy, and caps, while explicit CLI flags override plan defaults. Jobs and phases may override execution settings (`runner`, `ccc_runner`, `model`, `sandbox`, `approval`, `permission_mode`, `cli_agent`, `timeout_secs`, `quota_retries`, `quota_fail_fast`, `quota_retry_buffer_secs`, `failure_retries`, `kimi_max_steps_per_turn`, `result_schema`) with root < phase < job precedence. Multi-phase plans are flattened into staged jobs with dependency edges; a later phase starts only after its declared upstream jobs complete, and declared phases/gates/checks are recorded in run state.

Jobs can request isolated write lanes with `worktree: true` or a worktree object:

```json
{
  "name": "impl-a",
  "prompt": "Implement the bounded change.",
  "write_scope": ["src/a"],
  "worktree": {
    "branch": "workflow/my-run/impl-a",
    "base": "HEAD",
    "merge_target": "main"
  }
}
```

When enabled, `workflow apply` creates the git worktree before launching the worker and sets that worker's `cwd` to the lane path. If `path` is omitted, the lane lives under the workflow run state directory, not inside the source checkout. Use `dry_run` to record planned lanes without creating them.
3. If you are operating manually, initialize state before delegating:

```bash
workflow init \
  --title "short workflow title" \
  --prompt "original user request or compact objective" \
  --cwd "$PWD" \
  --mode hybrid
```

4. Add phases that match how the work will be judged. Prefer phases such as `research`, `design`, `implementation`, `review`, and `verification`.
5. Spawn native subagents for sidecar tasks when the current session exposes subagent tools. Native subagents are best treated as lead-session sidecars unless you can keep their workflow status/output coherent. Do not create long-lived `running` workflow agents for native subagents unless you will update them when they finish; otherwise record a lead-local event, artifact, or completed summary after they return.
6. For larger or more isolated work, launch external coding-CLI workers through `workflow apply`, `workflow start`, or the lower-level `workflow run`. These workers can use direct Codex, direct Kimi, `ccc`-wrapped Codex, `ccc`-wrapped OpenCode, or another `ccc` runner while updating the same state files.
   Use `--max-agents` to cap simultaneous workers and `--startup-delay` to pace launches; defaults are 4 workers and 1.0 seconds.
   When the user gives a broad natural-language goal, use `wf start "<goal>"` to ask a planner agent for a job decomposition, save the generated plan as an artifact, and then launch the worker jobs through the same runner/rate-limit interface. Use `--mock` for a no-model rehearsal; `--mock-plan` only mocks the planner and still launches real workers unless combined with `--mock` or `--dry-run`.
7. Keep state current: mark phases and agents `running`, `blocked`, `completed`, or `failed`; record important choices with `workflow decision`, durable outputs with `workflow artifact`, and verification/result summaries as events.
   If the lead session implements a phase locally, add a `lead-local` agent or a decision/event before marking that phase complete so the TUI does not show a mysterious empty implementation phase.
8. Use the TUI while work is active:

```bash
workflow tui
```

The live TUI is Textual-based and the snapshot renderer uses Rich panels/tables.
The installed `workflow`/`wf` aliases use the private workflow virtualenv when it exists.
The default TUI home tab is `runs`; use the visible `attention` tab for warnings and actionable items, or press `!` to jump there directly.
The header shows the current layout mode as `layout: command  L`; `L` is the displayed layout affordance while later layout-mode work is still being wired.
In the TUI, use arrow keys to move rows and sections, `a` to toggle phase/all agent scope, `v` to toggle live output/prompt, `y` to copy the selected id, `p` to copy the useful path, and `Ctrl-Y` to copy selected-row JSON.
Use `Ctrl-P` for the Textual command palette; it includes update checks plus pause, resume, and stop controls for the selected run.

or, without the installed alias:

```bash
python3 ~/.agents/skills/workflow/scripts/workflow_tui.py
```

## Monitoring Active Workflows

Use `workflow watch-emit` to pair an idle lead agent with the Monitor tool. Unlike `wf watch` / `wf monitor` (which refresh a compact status panel for humans), `watch-emit` emits append-only transition deltas — one line per state change — and stays silent when nothing has changed. This lets the agent sleep until real progress occurs, waking with full context instead of a blind ping.

```
Monitor command="workflow watch-emit <run-id>" triggerTurn=true
```

To watch all active runs without specifying an id, omit the run-id argument. Read `references/watch-emit.md` for output line formats, sidecar snapshot details, and the comparison to fixed-interval heartbeats.

## Delegation Rules

- Use native subagents first for small to medium parallel tasks inside the current turn. They are best for research, review, test triage, and disjoint code lanes.
- Prefer custom workflow agents when they are installed and available in the current agent surface: `workflow-explorer` for read-only research, `workflow-reviewer` for review, `workflow-implementer` for bounded edits, and `workflow-synthesizer` for combining worker results.
  If those custom agents are not installed, use general subagents or external workers with equivalent bounded prompts.
- Use external coding-CLI workers for larger tasks that benefit from their own independent process, durable transcript, or long-running isolation.
- Do not delegate the immediate blocking task. Keep the critical path local while sidecar agents work.
- Give every agent a bounded scope, expected output, file ownership if it may edit, and a reminder that other agents may be working concurrently.
- Prefer read-only agents for research and review. Give write access only when the lane owns a disjoint file set or worktree.
- For write-heavy parallelism, declare per-job `worktree` lanes or explicit `write_scope`. Avoid two agents editing the same file.
- Always verify subagent and worker results before presenting them as true.
- A worker's final output is data for the lead, not prose for a human. Ask for a parseable shape (JSON or markdown with fixed headings), state the schema in the prompt, and re-dispatch the lane on malformed output instead of parsing slop. Persist it as the agent `--result-file`.

## Concurrency Discipline

Right-size and right-shape the fan-out before launching it.

- **Scale to the request.** "find any bugs" → a few finders, single-vote verify. "thoroughly audit" → larger pool, 3–5-vote verify, explicit synthesis. A quick check → maybe no workflow at all. Over-orchestration is its own cost.
- **Pipeline by default; barrier only when needed.** "fan out N lanes, wait for all, then synthesize" is a *barrier* — correct only when the next stage needs every prior result at once (dedup/merge across the full set, early-exit on an aggregate, or a prompt that references "the other findings"). Otherwise it wastes wall-clock: the slow lane stalls all the fast ones. Prefer to start each downstream lane as soon as its upstream lane returns, so item A can be in `verify` while item B is still in `find`.
- **Smell test:** if a cheap per-item transform sits between two fan-outs with no cross-item dependency, you don't need the barrier between them.
- **Verify before you trust, and refute by default.** For findings that would be expensive if wrong, spawn independent skeptics (or distinct-lens verifiers) and keep only what survives.

Read `references/workflow-patterns.md` for the full pattern library (adversarial verify, perspective-diverse verify, judge panel, loop-until-dry, multi-modal sweep, completeness critic, no-silent-caps, structured returns) and worked compositions.

### Advanced Patterns (plan-file forms)

When a pattern earns a durable, re-runnable plan, express it as a multi-phase `workflow-plan` JSON and launch it with `workflow apply`:

- **Layered review** — one impl phase plus an ordered multi-model review stack, using **phase-level `ccc_runner` overrides** so each layer runs on a different model (root < phase < job precedence). The layered stack is defense in depth, not a guarantee — keep lead-level review mandatory. See `references/layered-review-workflows.md`.
- **Multi-agent phase fan-out** — several implementation agents in the **same phase**, each on a disjoint scope with its own `worktree` lane, then a synthesis phase (barrier over the full set) and a review phase. All jobs in a phase run in parallel; phase N+1 waits on all of phase N. See `references/multi-agent-phase-dogfood.md`.

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
- pipelining and quality patterns: pipeline-by-default staging plus adversarial/diverse verification, loop-until-dry discovery, and completeness critics

Read `references/claude-code-parity.md` when designing or extending the workflow system, and `references/workflow-patterns.md` for the orchestration pattern library.

## Coding CLI Workers

Use external coding-CLI workers when the task needs a separate process, durable logs, or a bigger isolated run.

For reusable workflows, start from a checked-in plan file or generator script:

```bash
workflow apply workflows/review.workflow.json \
  --runner ccc \
  --ccc-runner @mimo25p \
  --max-agents 4
```

`workflow apply` accepts either JSON or an executable/Python script that prints JSON. The plan should use `kind: "workflow-plan"` and contain either `jobs` or `phases[].jobs`; job `name` values must be unique/stable because `depends_on` points at names. The launcher records the normalized plan as a `workflow-plan` artifact before dispatch.

For broad ad-hoc goals, use the planner:

```bash
workflow start "review this repository and fix the highest-impact issues" \
  --runner ccc \
  --ccc-runner @mm \
  --max-agents 4
```

`workflow start` first runs a planner agent, records its generated plan as a decision and artifact, then launches the planned jobs. Use `--mock` for a no-model rehearsal, or `--mock-plan` to use the deterministic planner while still launching real workers. Add `--dry-run` or `--mock` with `--mock-plan` when you want to avoid worker calls.

For deterministic one-stage fan-out from shell, use the lower-level runner:

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

For direct Kimi workers, use `--runner kimi-direct`. The runner uses Kimi's quiet print mode and sends the prompt on stdin, which is safer for larger prompts than placing them on argv:

```bash
workflow run --runner kimi-direct --title "review" --job "review::Review this branch."
```

For generic `ccc`, `--ccc-runner` accepts either a CLI selector such as `kimi` or a preset such as `@mm`:

```bash
workflow run --runner ccc --ccc-runner @mm --title "review" --job "review::Review this branch."
```

To compare several runners with the same reusable smoke jobs, use the runner matrix harness:

```bash
workflow runner-matrix \
  --target kimi-direct \
  --target ccc:kimi \
  --target minimax=ccc:@mm \
  --output-dir ~/tmp/workflow-runner-matrix
```

Use `--mock` or `--dry-run` for no-model rehearsals. Use `--all-common` to expand the common direct and `ccc` target set.

Read `references/coding-cli-runners.md` before using this path for write access.

Pause or stop an active run when needed:

```bash
workflow pause <run-id>
workflow resume <run-id>
workflow stop <run-id>
```

Pause is cooperative and stops new worker launches; stop cancels unfinished state and best-effort terminates recorded worker process groups.

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
2. Read every subagent or worker summary, not just the top-level status. A completed phase with zero agents is a red flag — either real work is unrecorded or no work happened.
3. Confirm no coverage was silently capped. If `--max-agents` truncated a job list, a pool was sampled, or retries were skipped, that limit must be recorded as an event or decision — not left implied.
4. Run the verification commands appropriate to the actual work.
5. Record verification with `workflow verify` (a structured check), not just an event.
6. Mark the run `completed` only after the evidence supports it.

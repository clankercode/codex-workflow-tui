# Codex CLI Workers

This is the legacy Codex-specific runner note. For the current provider-neutral interface, read `coding-cli-runners.md` first.

Use `codex exec --json` workers when a lane is large enough to deserve its own process, event stream, and final artifact.

## Why Use CLI Workers

- They isolate noisy logs and intermediate tool calls.
- They produce JSONL events that scripts can capture.
- They can be resumed later by Codex session id when needed.
- They avoid relying on a single prompt to perform exact fan-out.
- They work from shell, cron, hooks, or another agent.

## Command Shape

For Codex CLI `0.139.0`, approval mode is a top-level flag:

```bash
codex --ask-for-approval never exec --json --cd "$PWD" --sandbox read-only "prompt"
```

The `codex-direct` provider handles this shape. It captures:

- stdout JSONL to `logs/<agent>.jsonl`
- stderr progress to `logs/<agent>.stderr.log`
- final assistant message to `artifacts/<agent>.final.md`
- thread id, exit code, summary, result, and process id into `run.json`

## Safety

Default to `--sandbox read-only`. Escalate to `workspace-write` only when a worker owns a disjoint write scope. Use `danger-full-access` only in externally isolated environments.

For write-heavy work, prefer one of:

- a dedicated git worktree per worker
- an explicit file ownership partition
- sequential implementation with parallel review only

## Prompt Template

```text
You are one worker in a workflow. Other agents may be working concurrently.

Scope:
- Own only <paths or concern>.
- Do not edit files outside the scope.

Task:
<specific task>

Output:
- Summary of actions/findings.
- Files changed or inspected.
- Verification run and result.
- Blockers or assumptions.
```

## When Not To Use

Do not use external workers for tiny changes, tasks that need constant conversation with the user, or tasks where the next main action is blocked on the worker result. Keep the critical path in the lead session.

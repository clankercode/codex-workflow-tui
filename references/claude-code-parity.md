# Claude Code Workflow Parity

This skill mirrors Claude Code workflow concepts using native subagents when available, coding-CLI workers, and local state.

## Mapping

| Claude Code concept | Workflow equivalent |
| --- | --- |
| Subagents | Native subagents spawned from the main conversation when the host exposes them |
| Agent view | `workflow_tui.py` over explicit workflow state |
| Dynamic workflows | `workflow_run.py` or main-session orchestration that fans out workers and records phases |
| Agent teams | Lead-agent pattern: main session coordinates roles, state, and synthesis |
| Background sessions | External coding-CLI workers through direct Codex, OpenCode, Kimi, or `ccc` providers |
| Workflows saved as commands | Skill scripts in `~/.agents/skills/workflow/scripts` plus command symlinks in `~/.local/bin` |
| Subagent transcripts | Native subagent IDs plus worker transcripts or JSONL logs |
| Hook-visible state | `run.json` state contract for external hook/tool consumers |
| Worktree isolation | Use git worktrees or disjoint file ownership for write-heavy workers |
| Pause and resume | `workflow pause` stops new worker launches; `workflow resume` continues unfinished workers |
| Stop without losing completed work | `workflow stop` cancels unfinished workers while preserving completed artifacts |
| `pipeline()` — per-item staging, no barrier between stages | Lead-driven staging: launch each item's next-phase lane as its prior lane returns, rather than barriering the whole batch. Single-runner per-item pipelining is an engine gap (see Practical Differences). |
| `parallel()` barrier | One `workflow run` job set, or a phase the lead waits on in full — use only when the next stage needs all results at once |
| `schema`-validated structured returns | Prompt workers for a parseable shape, persist as `--result-file`, lead validates and re-dispatches on malformed output |
| Quality patterns (adversarial verify, loop-until-dry, judge panel, completeness critic) | `references/workflow-patterns.md` |

## UX Targets

Replicate the parts that make Claude workflows operationally rich:

- dispatch independent lanes without flooding the lead context
- show active and completed workflows in one TUI
- show phases, agents, prompts, logs, results, and events
- preserve enough state for resume, audit, reporting, and future tooling
- keep prompt and result artifacts as normal markdown files
- use explicit status updates instead of inferring state from logs
- make review and verification phases first-class

## Practical Differences

Native subagents currently depend on the host session. Do not assume a runtime will automatically create a team because a task is large.

Direct Codex JSONL and `ccc` run artifacts are the stable automation substrates for this workflow system. App-server and remote control are promising for future live steering, but they are more version-sensitive and should not be the only state source.

Claude Code workflows resume stopped or paused runs inside the same session by reusing completed agent results and rerunning only unfinished work. This implementation keeps that spirit but uses durable local JSON state: completed agents and artifacts remain intact, pause is cooperative, and stop is best-effort process termination plus cancellation state.

## Recommended Workflow Shapes

### Parallel Review

Spawn independent read-only lanes for security, test coverage, maintainability, and performance. Wait for all. Synthesize findings by severity. Run targeted verification before marking complete.

### Research With Cross-Check

Spawn agents by angle or source family. Add a second review phase where agents or the lead attempts to falsify the strongest claims. Keep only claims that survive cross-checking.

### Large Implementation

Start with research/design agents. Partition implementation by file ownership or worktree. Run review agents against the integrated result. Finish with local verification.

### Debugging With Competing Hypotheses

Spawn agents with distinct hypotheses. Require each result to include disconfirming evidence. The lead chooses the surviving explanation and implements or directs the fix.

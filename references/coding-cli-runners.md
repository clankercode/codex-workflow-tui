# Coding CLI Runners

Use `workflow_run.py` when a workflow lane should run in its own process.
The runner interface is provider-neutral: every provider builds a command, captures stdout/stderr, extracts a final result, and mirrors paths back into `run.json`.
`workflow start` uses the same providers twice: once for the planner agent, then again for the generated worker jobs.

## Providers

Every `workflow run` command also needs `--title` and at least one `--job` or `--jobs-file`.
The available runner selectors are:

```text
--runner codex-direct          direct codex exec --json
--runner ccc-codex             codex through ccc
--runner ccc-opencode          OpenCode through ccc
--runner ccc --ccc-runner kimi
--runner ccc --ccc-runner @mm
--runner opencode-direct
```

Prefer `ccc-*` providers when portability matters. `ccc` normalizes runner selection, permissions, output modes, and run artifacts across coding CLIs. Its artifact footer has this shape:

```text
>> ccc:output-log >> /abs/path/to/run-dir
```

The workflow runner parses that footer and records:

- `output.txt` as the final worker result
- `transcript.jsonl` or `transcript.txt` as the durable transcript
- the `ccc` run directory name as `thread_id`

The default `ccc` output mode is `stream-json`. This gives the TUI the best available live output, tool-call, and token telemetry for Codex, OpenCode, and presets that forward provider JSON. Use a formatted/text mode only when human-readable stdout matters more than live telemetry.

Token totals are provider-usage totals, not text estimates. When a runner emits a total token count, the TUI uses it directly. When it emits input/output/reasoning parts without a total, the TUI derives and labels the total. When no usage metadata is present, the TUI shows `unknown`.

For `--runner ccc`, `--ccc-runner` is a raw `ccc` target:

- Plain values such as `kimi`, `opencode`, `codex`, `claude`, `cx`, or `oc` are interpreted by `ccc` as CLI runner selectors.
- Values starting with `@`, such as `@mm`, `@cx-reviewer`, or `@reviewer`, are interpreted by `ccc` as presets.

The workflow state labels these as `ccc-runner-*` or `ccc-preset-*` agent types while passing the exact token through to `ccc`.

## Rate Limits

Worker launch pacing is built in:

- `--max-agents 4` caps simultaneous workers. The old `--concurrency` flag remains as a compatibility alias.
- `--startup-delay 1.0` waits at least this many seconds between worker process starts.

Use `--startup-delay 0` only for local dry-runs, mock tests, or known-safe fake runners.

## Recommended Commands

Parallel OpenCode workers through `ccc`:

```bash
workflow run \
  --runner ccc-opencode \
  --max-agents 4 \
  --startup-delay 1.0 \
  --title "parallel review" \
  --cwd "$PWD" \
  --job "security::Review this branch for security risks. Return file-linked findings." \
  --job "tests::Review missing or weak tests. Return concrete gaps."
```

Parallel Codex workers directly:

```bash
workflow run \
  --runner codex-direct \
  --title "parallel review" \
  --cwd "$PWD" \
  --max-agents 3 \
  --sandbox read-only \
  --approval never \
  --job "security::Review this branch for security risks. Return file-linked findings."
```

Pass extra `ccc` controls one token at a time:

```bash
workflow run \
  --runner ccc \
  --ccc-runner @mm \
  --ccc-control +3 \
  --permission-mode safe \
  --title "review" \
  --job "review::Review the current branch."
```

## Safety

Default to read-only review lanes. Use write-capable workers only with disjoint file ownership or dedicated worktrees.

For `codex-direct`, use `--sandbox read-only` unless the worker needs to edit. For `ccc-*`, use `--permission-mode safe` or an explicit `ccc` alias/config profile when you need tighter control.

## Provider Notes

- `codex-direct` gives raw Codex JSONL and precise Codex thread ids.
- `ccc-codex` is better when you want the same invocation shape as other CLIs.
- `ccc-opencode` is the preferred OpenCode path because `ccc` writes normalized `output.txt` and transcript artifacts.
- `opencode-direct` uses `opencode run --format json`; it is useful for experiments, but `ccc-opencode` is the sturdier default.

# Coding CLI Runners

Use the workflow worker runners when a workflow lane should run in its own process.
The runner interface is provider-neutral: every provider builds a command, captures stdout/stderr, extracts a final result, and mirrors paths back into `run.json`.
`workflow apply` is the preferred front door for saved workflow-plan files or generator scripts.
`workflow start` uses the same providers twice: once for the planner agent, then again for the generated worker jobs.
`workflow run` is the lower-level flat fan-out primitive.

## Workflow-Plan Launchers

Use `workflow apply` for repeatable workflows:

```bash
workflow apply workflows/review.workflow.json \
  --runner ccc \
  --ccc-runner @mimo25p \
  --max-agents 4
```

`workflow exec` is an alias. The input may be JSON or an executable/Python script that prints one JSON object:

```json
{
  "schema_version": 1,
  "kind": "workflow-plan",
  "title": "Project planning",
  "summary": "optional objective",
  "goal": "optional original goal",
  "jobs": [{"name": "architecture", "role": "planner", "prompt": "bounded worker prompt"}]
}
```

Plans may also use `phases[].jobs`. `workflow apply` records the normalized plan as a `workflow-plan` artifact, preserves execution metadata such as `cwd`, `runner`, `ccc_runner`, `tags`, sandbox/approval settings, and caps, and lets CLI flags override plan defaults.
Job `name` values are dependency keys, so keep them unique and stable across the whole plan when using `depends_on` or phase staging.

Jobs may declare `worktree: true` or a `worktree` object with `path`, `branch`, `base`, `merge_target`, `owner`, or `label`. `workflow apply` creates the lane before launch, stores the normalized lane metadata on the agent, and runs that worker with the lane as its cwd. Omitted paths default under the workflow run state directory. `--dry-run` records planned lanes without creating them.

Multi-phase plans are flattened into staged jobs with dependency edges and declared phase/gate/check records. This gives ordered execution, failure propagation, and visible phase status in the run state.

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
--runner kimi-direct
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

Parallel Kimi workers directly:

```bash
workflow run \
  --runner kimi-direct \
  --title "parallel Kimi review" \
  --cwd "$PWD" \
  --max-agents 3 \
  --startup-delay 1.0 \
  --job "review::Review this branch for correctness and maintainability."
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

## Runner Matrix Smoke Tests

Use `workflow runner-matrix` when you want to compare multiple agents or runners against the same reusable workflow.
Target specs are explicit so a matrix does not accidentally spend across every installed model:

```bash
workflow runner-matrix \
  --target codex-direct \
  --target kimi-direct \
  --target ccc:kimi \
  --target minimax=ccc:@mm \
  --output-dir ~/tmp/workflow-runner-matrix
```

Target forms:

- `kimi-direct`, `codex-direct`, `opencode-direct`, `ccc-codex`, or `ccc-opencode`
- `ccc:<selector>` for a generic `ccc` CLI selector, such as `ccc:kimi`
- `<label>=ccc:<selector>` for friendly archive labels, such as `minimax=ccc:@mm`

The harness uses `examples/runner-smoke-jobs.json` by default, writes a copy to the output directory, runs one workflow per target, and archives each run under `<output-dir>/archive/<label>/<run-id>/`.
It also writes `<output-dir>/runner-matrix-summary.json`.
Use `--mock` or `--dry-run` for no-model rehearsals.
Use `--all-common` to expand `codex-direct`, `kimi-direct`, `opencode-direct`, `ccc:opencode`, and `ccc:kimi`.

Runner matrices can also load reusable workflow-plan scripts when comparing several targets:

```bash
workflow runner-matrix \
  --project-src ~/tmp/agent-capacity-market-workflow-test/source-plan \
  --workflow-script ~/.agents/skills/workflow/examples/project_planning_workflow.py \
  --workflow-script-arg=--project-dir \
  --workflow-script-arg '{project_dir}' \
  --target kimi=ccc:@kimi \
  --target mimo25p=ccc:@mimo25p \
  --target glm5t=ccc:@glm5t \
  --target-max kimi=4 \
  --target-max mimo25p=8 \
  --target-max glm5t=4 \
  --output-dir ~/tmp/agent-capacity-market-workflow-test/runs
```

The script prints a reusable workflow object:

```json
{
  "schema_version": 1,
  "kind": "workflow-plan",
  "title": "Project planning",
  "summary": "optional objective",
  "goal": "optional original goal",
  "jobs": [{"name": "architecture", "role": "planner", "prompt": "bounded worker prompt"}]
}
```

When `--project-src` is set, the matrix copies that directory once per target under `<output-dir>/workdirs/<label>/`, excluding `.git` and common caches.
Script args may include `{project_dir}`, `{target}`, `{label}`, and `{output_dir}` placeholders.
The generated workflow JSON is saved before execution under `<output-dir>/workflows/<label>.workflow.json`; the same normalization rules as `workflow apply` are used before a derived jobs array is passed to `workflow run`.

This is dynamic pre-run expansion. Runtime expansion is also supported when a worker returns a structured envelope such as `{"kind":"workflow-expansion","schema_version":1,"jobs":[...]}`; those jobs are enqueued behind `--max-round` and `--max-job` guards. Use explicit `depends_on` names when generated jobs must wait for specific upstream jobs instead of the whole prior stage.

## Safety

Default to read-only review lanes. Use write-capable workers only with disjoint file ownership or dedicated worktrees.

For `codex-direct`, use `--sandbox read-only` unless the worker needs to edit. For `ccc-*`, use `--permission-mode safe` or an explicit `ccc` alias/config profile when you need tighter control.

## Provider Notes

- `codex-direct` gives raw Codex JSONL and precise Codex thread ids.
- `ccc-codex` is better when you want the same invocation shape as other CLIs.
- `ccc-opencode` is the preferred OpenCode path because `ccc` writes normalized `output.txt` and transcript artifacts.
- `opencode-direct` uses `opencode run --format json`; it is useful for experiments, but `ccc-opencode` is the sturdier default.
- `kimi-direct` uses `kimi --quiet --input-format text --work-dir <cwd>` and pipes the prompt on stdin. It records stdout as the final output. Use `--model` to override Kimi's configured default.
- `ccc --ccc-runner kimi` remains useful when you want ccc-managed artifacts and the same runner selection surface as other coding CLIs.

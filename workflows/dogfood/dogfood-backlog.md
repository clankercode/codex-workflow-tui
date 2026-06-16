# Workflow Dogfood Backlog

This file is the repo-canonical backlog for workflow-skill/tooling discoveries
from the Wave 2 dogfood run and later dogfood sessions. Chat notes, workflow
events, and run-state artifacts are useful breadcrumbs, but this checked-in
document is the durable review target.

## Fixed In This Session

- `workflow run` can attach workers to an existing run via `--run` /
  `--attach-run` instead of always creating a new top-level run.
- `workflow run --parent-run` links child runs back to a parent via metadata
  and one parent event.
- Runner telemetry refreshes no longer append repeated unchanged `running`
  events.
- TUI run overview shows compact two-line rows with duration.
- TUI run detail lists all running agents instead of hiding them behind one
  longest-running label.
- TUI copy ID attempts the real Ctrl+V clipboard through `wl-copy`, `xclip`, or
  `xsel` after Textual clipboard handling.
- TUI live tool counts are labeled as tail-window counts.
- TUI parses TodoWrite-like payloads into live todo panels.
- TUI live todo statuses render with compact markers such as `[✓]`, `[~]`,
  `[ ]`, and `[!]`.
- TUI shows actual provider thinking/reasoning text only when present.
- `workflow apply <workflow-json-or-script>` / `workflow exec ...` launches
  existing `workflow-plan` JSON or generator scripts through a first-class
  command, records the normalized plan as an artifact, and reuses a shared
  normalizer with `runner-matrix`.
- `workflow apply` supports multi-phase plans by flattening phases into staged
  jobs with inter-phase dependencies; dependent phases do not run after an
  upstream phase failure.
- `workflow-plan` execution metadata such as `cwd`, `runner`, `tags`, caps, and
  `ccc_control` is preserved with CLI overrides taking precedence.
- Agents page/table now shows compact duration/elapsed time, and selected-agent
  detail includes elapsed/duration in live stats.
- Full test suite isolation was fixed for the mock-plan truncation test by
  using mock workers as well as a mock planner.
- `workflow apply` now instantiates declared plan phases as first-class phase
  records, attaches workers to those declared phases, preserves phase gates and
  planned-check metadata without marking checks as passed, records plan
  decisions, and keeps runtime expansion children attached to the parent
  worker's phase.
- Workflow tests now have a `WORKFLOW_TEST_MODE=fast` split that skips
  timing-heavy integration/snapshot/matrix/update tests, and test-only runner
  sleep overrides reduce full-suite wall time without changing production
  defaults.

## Prioritized Follow-Up Backlog

### P0: Make Workflows Repeatable

1. **First-class workflow worktree lanes.** Implemented 2026-06-15.
   `workflow apply` now preserves job lane metadata, creates per-job git
   worktrees, records lane state on agents, and launches workers in the lane cwd.

2. **Automated worktree merge-back orchestration.** V1 implemented 2026-06-15.
   `workflow merge-lanes` now merges completed worktree lane branches into the
   run cwd, records merge checks/events, aborts conflicts by default, and marks
   lane metadata with merge status/commit/check id.

3. **Conflict-solving merger agents for worktree lanes.** Implemented 2026-06-15.
   Add agentic conflict/semantic reconciliation after `merge-lanes` records a
   merge conflict.
   Dogfood result: one saved workflow ran implementation in a fixed worktree
   lane with `@mimo25p`, then `@mm3` and `@glm51` review phases in the same
   lane. V1 adds `workflow merge-conflicts <run-id> [--agent <id>]`, which
   prepares durable merger prompt/context artifacts and records a
   `merge-conflict-assist` event after a failed `merge-lanes` conflict. It does
   not silently auto-resolve; operators or merger agents must resolve, verify,
   and rerun `merge-lanes`. Reviews fixed misleading/conflicting guidance,
   malformed conflict-state handling, and a pending-check lifecycle bug.

4. **Per-job runner/model configuration.** Implemented 2026-06-15.
   Allow workflow-plan jobs or phases to override runner/model settings
   (`runner`, `ccc_runner`, `model`, permission/sandbox knobs) so an
   implementation job can use one agent/model and its review job can use
   another in the same saved workflow launch.
   Dogfood result: implementation ran with `@mimo25p` in a worktree lane.
   `@mm3` review caught a high-impact bug where state metadata reflected
   per-job runners/models but actual launched commands could still use the
   default provider; it fixed that with process-level tests. A proper
   multi-phase layered workflow then exercised phase-level `ccc_runner`
   overrides in one run: `@glm51` semantic review fixed a duplicate test method
   name that hid coverage, and `@mimo25p` final review passed after focused,
   fast, and snapshot tests. Lead review then caught and fixed additional
   execution-field edge cases missed by the model-review stack: phase/job
   `mock` and `dry_run` must avoid real launches, phase `cwd` must reach actual
   worker commands, relative CLI `--cwd` must stay resolved when applied as a
   per-job override, phase/job `ccc_control` must normalize scalar values to
   lists, CLI `ccc_control`/`ccc_output_mode` must beat plan values, and
   phase/job `result_schema` must validate output at runtime.

### P1: Prevent Misleading State

5. **Opaque running-agent invariant.** Implemented 2026-06-15.
   Add a workflow-state invariant/check for opaque running agents: a running
   managed agent should have at least one credible liveness source
   (`process_id`, `jsonl_path`, native-agent adapter metadata, or an explicit
   unmanaged/native-sidecar marker). The TUI/status command should warn when
   this invariant is violated.
   Dogfood result: task ran through a saved workflow plan with a declared
   `worktree` lane, created branch `workflow/opaque-running-agent-invariant`,
   implemented in the lane, and fast-forward merged back to `main`.

6. **Process identity for running agents.** Implemented 2026-06-15.
   Show process identity for running agents when available: PID/process group
   for external workers, and a clear non-PID native-agent identifier for native
   subagents so users can tell where an agent is running.
   Dogfood note: first `ccc @mm27` attempt (`wf-20260615T125524Z...`) was
   stopped during exploration before edits. It was still reading files, but its
   visible output also showed nested-runner confusion ("I'll use ccc @mm27")
   even though it was already running as that worker. Retry prompts should say
   explicitly not to launch or delegate to another runner.
   Dogfood result: retry with explicit no-delegation wording produced a useful
   patch; `@mm3` review in safe mode produced empty output due permission
   auto-reject, while `@mm3` with yolo produced actionable findings that were
   fixed before merge-back.

7. **Native-subagent status/output integration.** Implemented 2026-06-16.
   Native Codex subagents can be registered in workflow state, but they do not
   currently provide a PID, JSONL path, or live output stream like `ccc`
   workers, so the TUI can show "running" with no useful activity. Either the
   workflow system should launch/manage native subagents through a first-class
   adapter that records them at spawn time and keeps status/output coherent, or
   native sidecar agents should be shown separately as unmanaged helpers. Do not
   encourage manual mirroring of native subagents into workflow state.
   Dogfood result: native subagents without `process_id`/`jsonl_path` are now
   auto-classified as effectively unmanaged so health checks do not demand
   liveness sources the workflow runner cannot provide. Native subagents with
   host ids still count as inspectable liveness sources.

8. **Lane ownership/scope checks.** Implemented 2026-06-16.
   Add automatic lane ownership/scope checks for workflow workers: compare
   changed files against declared owned/forbidden paths and surface warnings
   before review/integration.
   Dogfood result: `merge-lanes` compares worktree branch changes against
   declared `write_scope`, returns `scope_warnings`, and records
   `scope-violation` or `scope-check-error` events. Reviews fixed prefix
   collision false negatives, blank-scope behavior, dry-run mutation, and
   silent diff failures.

9. **Completed-run output fallback.** Implemented 2026-06-16.
   Completed, failed, or cancelled runs can appear blank in the TUI when the
   worker final output artifact exists but is empty/unhelpful and
   `result_summary`/`summary` is unset. The TUI should fall back to transcript
   or event-derived signal before showing an empty preview: latest assistant
   output/thinking/todo/tool/error snippet, cancellation reason, last status
   event, or artifact summary. The runner should also avoid treating empty
   final-output files as useful worker-output artifacts, or write a synthetic
   summary such as "cancelled before final output; see transcript."
   Dogfood result: terminal agents with empty final output now surface fallback
   data in the TUI from thinking, tool calls, latest output, termination reason,
   summary, or exit code. Health warns on empty output with fallback data and no
   longer treats zero-byte output artifacts as useful output.

### P2: Improve Live Operator Visibility

10. **Run-level merged live output.**
   Add run-level live output that merges all live agent streams with stable
   colored agent-name prefixes.

11. **Compact live monitor/status command.**
    Add a low-noise `workflow status/watch` view for lead agents and humans:
    one compact row per run/agent with status, PID, elapsed time, last event,
    latest output excerpt, tool-call count, token delta, and optional `--json`.
    This should avoid direct `run.json` dumps and broad log tails during active
    dogfood runs. Avoid printing full process command lines by default because
    worker prompts are embedded in argv and can be huge; show only PID/process
    group, runner type, elapsed time, and safe command basename unless verbose
    mode is requested. Support long quiet waits without repeatedly opening
    helper sessions.

12. **Event rollover visibility.**
   Add event rollover visibility in the TUI: make it obvious when a run has
   lost old events and point users to durable artifacts/logs for complete
   history.

13. **First-class dogfood/backlog support.**
    Add a command or convention that creates a durable backlog artifact,
    registers it, and appends discoveries there instead of relying on bounded
    event history.

14. **Ergonomic layered review workflows.**
    Make it easy to declare an implementation plus ordered multi-model review
    stack in one `workflow-plan`, instead of launching one workflow per review
    layer. Dogfood failure: while testing per-job runner/model config, the lead
    still fell back to separate serial review workflow launches because mixed
    runner support was newly implemented and not yet merged. The tool should
    encourage a single durable plan for the whole implementation/review/fix
    lifecycle, including rotating review model order.
    Dogfood follow-up: once the feature existed, a single layered workflow with
    phase-level `ccc_runner` overrides worked well and should be documented as
    the preferred pattern for implementation plus ordered model-diverse review.
    Review-quality note: the layered model stack can still miss important edge
    cases. Keep lead-level review mandatory after agent review, and bias review
    prompts toward actual launched command behavior and state lifecycle
    invariants, not only primary happy-path behavior.

15. **Real multi-agent phase dogfood.**
    Recent "bigger E2E" workflow plans still used one agent per phase, which
    dogfoods staged review pipelines but not true multi-agent phases. Add
    workflow templates/guidance that make phase fan-out natural: multiple
    implementation agents in the same phase with disjoint worktrees/scopes,
    explicit synthesis/integration phases, and review phases that can fan out
    across independent review lenses before a final lead review.

16. **Runs overview graph and attention notifications.**
    Redesign the overview page to list runs rather than attention items, with a
    graph/flow preview for each selected run showing phases, dependencies,
    running/completed agents, and blocked/attention states. Move attention into
    a far-right tab labelled with unread count such as `attention (2)`.
    Attention items should behave like notifications: new items are marked
    unread, surface a user-visible notification/toast with about a 7.5s timeout,
    and become easier to triage without displacing the runs overview.

17. **TUI pane scrolling and run-list bounds.**
    The right-hand detail pane should be scrollable with the mouse wheel. The
    left-hand list scrolling/windowing logic works for one-line overview-style
    rows, but the runs tab uses two or three visual lines per row and can scroll
    the selection off the bottom of the visible list. Add regression tests for
    variable-height run rows and mouse-wheel/detail-pane scrolling.

18. **Runs detail layout for live output and running agents.**
    The runs right-hand pane currently shows live output above other details,
    which is awkward when live output is long. Move live output above the prompt
    instead, and make the running-agents summary a table or multi-column layout
    so more active agents fit without pushing important run metadata away.

19. **Live throughput stats and smoothed counters.**
    All live stats boxes should show an estimated tokens-per-second rate when
    token telemetry is available. Monotonic numeric values that update
    regularly, such as token totals or tokens-per-second, should use a smooth
    odometer-like display that increases at an estimated rate while the agent or
    run is live. Once an agent/run completes, show the exact recorded value.
    Do not apply smoothing to times or dates.

20. **Finished-ago field for completed runs and agents.**
    Agent and run detail views should show a `finished ago` / completed-age
    field once status is terminal, alongside duration. Live runs/agents should
    keep showing elapsed/live duration; completed ones should make it easy to
    see both how long they ran and how recently they finished.

### Completed Or Superseded

- Runs tab main status/preview now shows multiple running agents for a workflow
  at a glance.
- First-class `workflow apply` / `workflow exec` v1 exists for
  `workflow-plan` JSON/generator scripts and is shared with `runner-matrix`.
- `workflow apply` v2 preserves declared phases, gates, planned checks, and
  plan decisions in run state.
- Agents page duration stat is implemented in the agents table and selected
  agent detail.

## Open Quality Notes

- Workflow events are persisted in `run.json`, but they are bounded history and
  can roll over. Dogfood discoveries should be copied into artifacts or a
  project issue/backlog when they need to survive noisy runs.
- Current Wave 2 builder run was launched before the event-spam fix, so its
  event stream will remain noisy until it exits.
- A `ccc @mm27` review worker launched with `permission_mode=safe` completed
  with empty output after read permission was auto-rejected. Review workflows
  that need repository reads should use an appropriate permission profile, and
  the runner should probably warn when a successful worker has empty final
  output plus permission-rejection tool errors.
- A cancelled `ccc @mm27` implementation run recorded final-output artifacts
  but had empty summaries, so completed/cancelled run previews could look blank
  despite useful JSONL/event history. This is separate from deleting the source
  workflow JSON: `workflow apply` had already copied the normalized plan into
  run artifacts.
- The workflow test suite is mostly integration-style: it uses fake coding CLIs
  in temp `PATH`s rather than real model calls, but still pays subprocess,
  polling, timeout, snapshot-rendering, local-git, and archive costs.

## Verification

- `python3 -m py_compile scripts/workflow_run_codex.py scripts/workflow_tui.py
  scripts/workflow_tui_app.py scripts/workflow_tui_live.py` passed.
- Focused runner attach/event tests passed.
- Full `python3 -m unittest tests.test_workflow` passed twice after combined
  runner/TUI changes; the second post-todo-marker run passed 162 tests in
  183.674s.
- Full `python3 -m unittest tests.test_workflow` passed after workflow-apply
  and agents-duration changes: 171 tests in 49.052s.

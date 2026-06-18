# Workflow TUI Layout Redesign

## Goal

Redesign the workflow TUI so the home screen is a runs-first command center with strong live-operations visibility. The current first tab is an attention triage view, but that is not the main workflow the operator uses. The redesigned TUI should make it fast to pick a run, inspect its agents, watch live work, and jump into detailed agent views.

## Approved Direction

The TUI should open on the runs view. Attention becomes a real `attention` tab placed visually at the far right of the header, labelled with an unread count when applicable, such as `attention 2`.

The TUI has three global layout modes:

- `command`: default when no preference exists. Balanced command-board layout with runs, selected-run flow/health, agent drill-down, and live ops strip.
- `ops`: live-operations console layout with dominant merged live output, compact runs list, and selected-run facts.
- `timeline`: progress/story layout with selected-run timeline or phase flow, run list, and phase/live snippets.

Pressing `L` cycles:

```text
command -> ops -> timeline -> command
```

The layout mode is global for the whole TUI, not per tab or per run. The live TUI persists the last-used mode. The persisted mode is restored on the next launch. If no valid preference exists, the TUI starts in `command`.

## Navigation Model

Header tabs are split conceptually into working tabs and the notification tray:

```text
runs graph phases agents events decisions artifacts                  layout: command  L  attention 2
```

The visible `overview` concept should be renamed to `attention`. The first tab in the current version is not important enough to preserve as a user-facing concept. Internal compatibility is acceptable during migration, but operators should see and use `attention`.

Existing shortcuts should adapt:

- `!` jumps to `attention`.
- `L` cycles layout modes and persists the new mode.
- Left/right still navigate tabs, with `attention` reachable at the far right.
- New attention items may still show toast notifications, but must not steal focus or displace the runs dashboard.

## Runs To Agents Drill-Down

The runs tab should be a real command center, not only a run summary.

On the `runs` tab:

- Selecting a run shows its agents in the right pane by default.
- `Enter` on a run focuses the right pane's agent list.
- `Right` also moves focus from run list to right-pane agent list when that pane contains agents.
- In the focused agent list, `Up` and `Down` move between agents.
- `Enter` opens the `agents` tab with that agent selected.
- `Right` does the same as `Enter` from the focused agent list.
- `Escape` or `Left` returns focus to the runs list.
- Selected agent identity carries across tabs by `agent_id`; jumping to the `agents` tab should land on the same agent where possible.

If the selected run has no agents, show run facts plus an empty-state message instead of an empty agent list.

If the selected agent disappears after refresh, preserve by id when possible; otherwise clamp to the nearest available agent.

## Layout Behavior

### Command

`command` is the default and the normal operator dashboard.

Structure:

- Left pane: runs list.
- Right pane top: selected run flow/health summary.
- Right pane middle: agents for selected run.
- Right pane bottom: compact merged live ops strip.

This mode optimizes for quickly answering: Which run is active? Is it healthy? Which agents are doing work? Where do I drill in?

### Ops

`ops` is the live-console layout.

Structure:

- Narrow left pane: runs list.
- Center/main pane: dominant merged live output console.
- Right rail: selected-run facts and active-agent status. On narrow terminals, this collapses into a compact panel above the live console.

This mode optimizes for watching active work with minimal tab switching.

### Timeline

`timeline` is the phase/progress layout.

Structure:

- Top area: selected-run timeline or phase flow.
- Side pane: runs list.
- Detail pane: selected phase/agent snippets, latest activity, and next gate.

This mode optimizes for understanding stage progress, dependencies, and what is blocking the next phase.

## Preference Persistence

Persist global TUI preferences under the workflow home, not in individual run state:

```text
workflow_state.workflow_root() / "tui-preferences.json"
```

Initial shape:

```json
{
  "layout_mode": "command"
}
```

Rules:

- Missing file falls back to `command`.
- Invalid JSON or invalid mode falls back to `command`.
- Live `L` toggles write the preference.
- A snapshot/manual `--layout {command,ops,timeline}` option overrides the active layout for that render.
- `--layout` does not write preferences; persistence belongs to live interactive toggles.
- If writing preferences fails, the TUI keeps working and shows a brief notification rather than crashing.

## Implementation Boundaries

This is not a full TUI rewrite.

Expected touched areas:

- Visible tab naming and tab order.
- Header/footer rendering.
- Layout-mode preference helper.
- Live Textual key binding and state.
- Snapshot CLI option for layout.
- Runs-tab right-pane rendering.
- Selection/focus logic for run-to-agent drill-down.
- Tests and snapshots.
- `SKILL.md` and relevant references if user-facing keys or behavior change.

Existing graph, phases, agents, events, decisions, and artifacts tabs should remain mostly as-is in v1 unless needed for selection handoff.

## Testing Plan

Add focused fast tests before broad snapshot churn.

Required coverage:

- Preference read/write/fallback for missing, invalid JSON, and invalid layout.
- `L` cycles `command -> ops -> timeline -> command` and persists in the live app.
- Snapshot/manual rendering supports `--layout`.
- Runs snapshots exist for `command`, `ops`, and `timeline`.
- Visible tab labels include `attention`, not `overview`.
- `!` jumps to `attention`.
- Run-to-agent selection:
  - `Enter` on a run focuses the run's agent list.
  - Selecting an agent and pressing `Enter` opens `agents` with that agent selected.
  - Selection is preserved by id across refresh where possible.
- Empty selected-run agent list renders a useful empty state.

Verification should include:

```bash
pytest -q tests/test_workflow.py
```

If snapshot updates are needed, keep them intentional and review the textual diffs carefully.

## Non-Goals

- No new backend workflow-state schema.
- No per-run or per-tab layout preferences.
- No replacement of the Textual/Rich rendering stack.
- No redesign of every detail tab in the first implementation pass.
- No attention auto-focus on new notifications.

# Watch-Emit: State-Transition Emitter

`workflow watch-emit` emits structured state-transition lines for machine consumption. It pairs with the Monitor tool so an idle lead agent wakes on real workflow progress with full context, instead of receiving a blind heartbeat ping.

## Usage

```bash
# One-shot: check once, print any changes, exit
workflow watch-emit [<run-id>] [--state-dir PATH]

# Loop: poll repeatedly, emit only when something changes
workflow watch-emit [<run-id>] [--interval 1s] [--state-dir PATH]
```

| Flag | Default | Description |
|------|---------|-------------|
| `run-id` | *(omit)* | Watch a specific run. When omitted, watch all active (non-terminal) runs. |
| `--interval` | `1` | Seconds between polls. |

watch-emit always loops; there is no one-shot mode.
| `--state-dir` | `$WORKFLOW_STATE_DIR` or `~/.agents/workflow-system/state` | Override the state root. |

### One-shot vs Loop

- **One-shot** (default): load state, diff against the snapshot, print changed lines, update snapshot, exit 0. Composable in pipelines and scripts.
- watch-emit always loops: it polls at `--interval` (default 1s), emits transition lines only when state changes, and stays silent otherwise.

### Single run vs All runs

- **`workflow watch-emit <run-id>`**: watch one run. Works even if the run is already terminal (useful for catching late status updates).
- **`workflow watch-emit`** (no run-id): watch all non-terminal runs in the state directory. Newly created runs are detected automatically and announced with a `[new]` suffix.

## Output Line Formats

Each emitted line uses a short run id (last 8 characters of the full run id) and one of four prefixes:

```
<short-id> RUN: <old-status> → <new-status>
<short-id> PHASE <phase-name>: <old-status> → <new-status>
<short-id> AGENT <agent-name>: <old-status> → <new-status> (exit <N>)
<short-id> EVENT: <message>
```

- **RUN** — the top-level run status changed (e.g. `running → completed`).
- **PHASE** — a phase status changed (e.g. `pending → running`).
- **AGENT** — an agent status changed. If the new status is terminal and an exit code exists, `(exit N)` is appended.
- **EVENT** — new events appeared. At most 3 new event lines are emitted per check.

### First-run baseline

On the first check for a run (no snapshot exists), the current status is emitted as the baseline:

```
ab12cd34 RUN: → running
ab12cd34 PHASE Research: → running
ab12cd34 AGENT Security reviewer: → running
```

No historical state is replayed — only the current snapshot is announced.

## Pairing with the Monitor Tool

Use `watch-emit` as the Monitor command with `triggerTurn=true`:

```
Monitor command="workflow watch-emit <run-id>" triggerTurn=true
```

How this works:

1. The Monitor tool starts `watch-emit` as a background process.
2. While nothing changes, the process is silent — the agent sleeps.
3. When a real state transition occurs, `watch-emit` prints a line and the Monitor tool wakes the agent with the transition context.
4. The agent reads the transition line(s) and decides what to do — no extra tool call needed to discover what changed.

This is fundamentally different from a fixed-interval heartbeat:

| Pattern | Behavior |
|---------|----------|
| Heartbeat (fixed ping every 30s) | Wakes every 30s regardless; agent must query state to find changes. |
| `watch-emit` | Wakes only on real transitions; full context is in the wake signal. |

The pattern mirrors how GitHub CI status watchers work: emit on state change, not on a fixed schedule.

### All-active-runs monitoring

To watch every active workflow without specifying a run id:

```
Monitor command="workflow watch-emit" triggerTurn=true
```

New runs are detected automatically and announced with `[new]`.

## Sidecar Snapshot

`watch-emit` stores a snapshot per run at `<run-dir>/watch-state.json` to track what it has already emitted. The snapshot contains:

```json
{
  "status": "running",
  "phases": {
    "phase-research": "completed",
    "phase-implementation": "running"
  },
  "agents": {
    "agent-abc123": {
      "status": "completed",
      "exit_code": 0
    }
  },
  "event_count": 5
}
```

- The snapshot is rewritten after every check.
- Deleting the snapshot causes the next check to emit a fresh baseline (no historical replay).
- The snapshot is safe to delete if it grows stale — it is a cache, not authoritative state.

## vs `wf watch` / `wf monitor`

`watch-emit` is distinct from the existing compact status commands:

| Command | Purpose | Output |
|---------|---------|--------|
| `wf monitor` | One-shot compact status panel for humans | Rich-formatted table of runs/agents |
| `wf watch` | Continuously refresh the compact panel | Full table redraw every N seconds |
| `wf watch-emit` | Transition deltas for machine consumption | One line per state change, silent when unchanged |

`wf monitor`/`wf watch` are for human operators who want a dashboard. `watch-emit` is for the Monitor tool — append-only transition deltas that wake an agent only when something real happens.

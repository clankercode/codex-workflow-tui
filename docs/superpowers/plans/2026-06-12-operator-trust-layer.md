# Workflow Operator Trust Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make workflow runs easier to operate by adding a trust/health command layer, optional verification schema, and a TUI attention cockpit.

**Architecture:** Keep existing low-level `workflow_state.py` commands as stable plumbing. Add a focused `workflow_ops.py` command module for operator intent commands, and extend `workflow_tui.py` with derived attention rows instead of making schema v2 mandatory. Schema additions are additive and optional so old runs remain readable.

**Tech Stack:** Python standard library, Rich/Textual renderers, existing JSON run snapshots, unittest snapshot tests, tmux visual QA.

---

### Task 1: Operator Command Module

**Files:**
- Create: `scripts/workflow_ops.py`
- Modify: `scripts/wf`
- Test: `tests/test_workflow.py`

- [ ] Add `workflow_ops.py` with `status`, `last`, `doctor`, `check`, `verify`, `done`, `block`, and `preview` subcommands.
- [ ] Keep `--dry-run` behavior unchanged; make `preview` no-write.
- [ ] Route those verbs through `scripts/wf`.
- [ ] Add tests for command routing, no-write preview, doctor output, check findings, verification records, safe completion, and block reasons.

### Task 2: Optional Schema Fields

**Files:**
- Modify: `scripts/workflow_state.py`
- Modify: `references/state-schema.md`
- Test: `tests/test_workflow.py`

- [ ] Add optional `checks[]`, `status_reason`, `status_message`, `blocked_by`, `last_activity_at`, and `health` conventions.
- [ ] Refresh derived metrics without requiring new fields in old runs.
- [ ] Document compatibility: old v1 state is valid; safe commands use new fields when present.

### Task 3: TUI Attention Cockpit

**Files:**
- Modify: `scripts/workflow_tui.py`
- Modify: `tests/snapshots/*.txt`
- Test: `tests/test_workflow.py`

- [ ] Add an `overview` tab that lists attention items: failed/blocked/stale agents, failed checks, missing artifacts, open decisions, and latest activity.
- [ ] Make `overview` the default TUI tab while keeping `runs` available.
- [ ] Add detail panels that show the linked entity, evidence, and suggested command/action.
- [ ] Preserve stable selection across reloads.

### Task 4: Search And Focus Affordances

**Files:**
- Modify: `scripts/workflow_tui.py`
- Modify: `scripts/workflow_tui_app.py`
- Test: `tests/test_workflow.py`

- [ ] Add `/` or command-palette search/filter for rows by status, title, id, phase, agent type, and text summary.
- [ ] Add `enter` focus mode for selected agent/artifact/attention item using the existing renderer path, with `escape` to return.
- [ ] Keep snapshot renderer deterministic with CLI `--filter` and `--focus` options.

### Task 5: Verification And Review

**Files:**
- Modify: `scripts/workflow_tui_tmux_qa.py`
- Test: `tests/test_workflow.py`

- [ ] Extend fixtures to cover checks, blocked reasons, and attention rows.
- [ ] Extend tmux QA to capture overview, search, focus, verify/done-visible state, and existing navigation.
- [ ] Run full tests, py_compile, diff check, tmux QA, and subagent review.

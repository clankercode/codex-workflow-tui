# Workflow TUI Layout Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the approved workflow TUI redesign: runs-first home screen, real `attention` tab, global persisted layout modes, and run-to-agent drill-down.

**Architecture:** Keep the existing Rich/Textual split. Add layout-mode and preference helpers in focused modules/functions, then thread the selected mode from `workflow_tui.py` into `workflow_tui_render.py`. Keep run state unchanged; persist only local TUI preferences under `workflow_state.workflow_root()`.

**Tech Stack:** Python, Rich, Textual, pytest/unittest-style tests in `tests/test_workflow.py`, existing workflow fixture/snapshot helpers.

---

## File Structure

- Modify `scripts/workflow_tui_render.py`: visible tab labels/order, layout mode constants, attention alias helpers, runs-tab mode renderers, run-agent table/detail helpers.
- Modify `scripts/workflow_tui.py`: CLI `--layout`, snapshot/render plumbing, tab validation/aliases, default snapshot tab.
- Modify `scripts/workflow_tui_app.py`: persisted preference load/save, `L` binding, layout state, run-to-agent focus state, `Enter`/`Right`/`Left`/`Escape` navigation.
- No planned changes to `scripts/wf`; the wrapper help remains unchanged unless implementation discovers it already lists TUI key hints.
- Modify `SKILL.md` and `references/operations.md`: update TUI keys and home-screen behavior.
- Modify `tests/test_workflow.py`: unit tests, live app behavior tests, and snapshot tests.
- Modify or add files under `tests/snapshots/`: runs snapshots for `command`, `ops`, and `timeline`; update `tests/snapshots/snapshot-overview.txt` to `tests/snapshots/snapshot-attention.txt` or replace the overview fixture case with an attention case in `test_snapshot_fixtures_match_checked_in_screens`.

## Task 1: Tab Rename And Layout Mode Plumbing

**Files:**
- Modify: `scripts/workflow_tui_render.py`
- Modify: `scripts/workflow_tui.py`
- Test: `tests/test_workflow.py`

- [ ] **Step 1: Write failing tests for visible `attention` tab and layout validation**

Add tests near the existing TUI tab/snapshot tests in `tests/test_workflow.py`:

```python
def test_tui_visible_tabs_use_attention_not_overview(self) -> None:
    sys.path.insert(0, str(SCRIPTS))
    import workflow_tui  # pylint: disable=import-outside-toplevel

    self.assertIn("attention", workflow_tui.TABS)
    self.assertNotIn("overview", workflow_tui.TABS)
    self.assertEqual(workflow_tui.TABS[0], "runs")
    self.assertEqual(workflow_tui.TABS[-1], "attention")


def test_layout_mode_validation_defaults_to_command(self) -> None:
    sys.path.insert(0, str(SCRIPTS))
    import workflow_tui  # pylint: disable=import-outside-toplevel

    self.assertEqual(workflow_tui.normalize_layout_mode(None), "command")
    self.assertEqual(workflow_tui.normalize_layout_mode("ops"), "ops")
    self.assertEqual(workflow_tui.normalize_layout_mode("bogus"), "command")
```

- [ ] **Step 2: Run tests to confirm they fail**

Run:

```bash
pytest -q tests/test_workflow.py::WorkflowScriptTests::test_tui_visible_tabs_use_attention_not_overview tests/test_workflow.py::WorkflowScriptTests::test_layout_mode_validation_defaults_to_command
```

Expected: fail because `TABS` still contains `overview` and no layout helper exists.

- [ ] **Step 3: Add layout constants and attention tab aliases**

In `scripts/workflow_tui_render.py`, replace the tab constants and add helpers:

```python
LAYOUT_MODES = ("command", "ops", "timeline")
DEFAULT_LAYOUT_MODE = "command"
TABS = ("runs", "graph", "phases", "agents", "events", "decisions", "artifacts", "attention")
TAB_ALIASES = {"overview": "attention"}


def normalize_tab(tab: str) -> str:
    return TAB_ALIASES.get(str(tab or ""), str(tab or ""))


def normalize_layout_mode(value: Any) -> str:
    text = "" if value is None else str(value)
    return text if text in LAYOUT_MODES else DEFAULT_LAYOUT_MODE
```

Make these exact replacements in the tab conditionals:

```python
if tab == "attention":
```

in every location that currently checks:

```python
if tab == "overview":
```

inside `item_key`, `make_sidebar`, `make_detail_body`, `sidebar_title_for`, `detail_panel_title`, `copy_value_for_selection`, and `rows_for_tab`. Also update `action_enabled_for_tab(normalize_tab(tab), action)` so old callers passing `overview` still behave like `attention`.

- [ ] **Step 4: Wire parser aliases and default tab**

In `scripts/workflow_tui.py`:

```python
def canonical_tab(value: str) -> str:
    tab = normalize_tab(value)
    if tab not in TABS:
        raise argparse.ArgumentTypeError(f"invalid tab {value!r}; expected one of {TABS}")
    return tab
```

Change parser lines:

```python
parser.add_argument("--tab", type=canonical_tab, default="runs")
parser.add_argument("--layout", choices=LAYOUT_MODES, default=None)
```

Pass `layout_mode=args.layout` into `render_snapshot`.

- [ ] **Step 5: Update header labels**

In `make_tabs_title`, remove the `overview` label and add:

```python
labels = {
    "runs": "run",
    "graph": "grf",
    "phases": "pha",
    "agents": "agt",
    "events": "evt",
    "decisions": "dec",
    "artifacts": "art",
    "attention": "attn",
}
```

Add layout text in `make_header(tab, *, width=110, filter_text="", layout_mode="command")`:

```python
layout_label = normalize_layout_mode(layout_mode)
left.append("  ")
left.append(f"layout: {layout_label}", style="green")
left.append("  L", style="dim")
```

- [ ] **Step 6: Run tests**

Run:

```bash
pytest -q tests/test_workflow.py::WorkflowScriptTests::test_tui_visible_tabs_use_attention_not_overview tests/test_workflow.py::WorkflowScriptTests::test_layout_mode_validation_defaults_to_command
```

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add scripts/workflow_tui.py scripts/workflow_tui_render.py tests/test_workflow.py
git commit -m "feat(tui): rename attention tab and add layout modes"
```

## Task 2: Persist Global Layout Preference

**Files:**
- Modify: `scripts/workflow_tui.py`
- Modify: `scripts/workflow_tui_app.py`
- Test: `tests/test_workflow.py`

- [ ] **Step 1: Write failing preference tests**

Add tests near other TUI helper tests:

```python
def test_tui_preferences_read_write_and_fallback(self) -> None:
    sys.path.insert(0, str(SCRIPTS))
    import workflow_tui  # pylint: disable=import-outside-toplevel

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "prefs.json"
        self.assertEqual(workflow_tui.load_tui_preferences(path)["layout_mode"], "command")

        workflow_tui.save_tui_preferences({"layout_mode": "ops"}, path)
        self.assertEqual(workflow_tui.load_tui_preferences(path)["layout_mode"], "ops")

        path.write_text("{bad json", encoding="utf-8")
        self.assertEqual(workflow_tui.load_tui_preferences(path)["layout_mode"], "command")

        path.write_text(json.dumps({"layout_mode": "nope"}), encoding="utf-8")
        self.assertEqual(workflow_tui.load_tui_preferences(path)["layout_mode"], "command")


def test_layout_mode_cycle_order(self) -> None:
    sys.path.insert(0, str(SCRIPTS))
    import workflow_tui  # pylint: disable=import-outside-toplevel

    self.assertEqual(workflow_tui.next_layout_mode("command"), "ops")
    self.assertEqual(workflow_tui.next_layout_mode("ops"), "timeline")
    self.assertEqual(workflow_tui.next_layout_mode("timeline"), "command")
    self.assertEqual(workflow_tui.next_layout_mode("bad"), "ops")
```

- [ ] **Step 2: Run tests to confirm they fail**

Run:

```bash
pytest -q tests/test_workflow.py::WorkflowScriptTests::test_tui_preferences_read_write_and_fallback tests/test_workflow.py::WorkflowScriptTests::test_layout_mode_cycle_order
```

Expected: fail because preference helpers do not exist.

- [ ] **Step 3: Implement preference helpers**

In `scripts/workflow_tui.py`, add:

```python
def tui_preferences_path() -> Path:
    return workflow_state.workflow_root() / "tui-preferences.json"


def load_tui_preferences(path: Path | None = None) -> dict[str, str]:
    pref_path = path or tui_preferences_path()
    try:
        data = json.loads(pref_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    return {"layout_mode": normalize_layout_mode(data.get("layout_mode") if isinstance(data, dict) else None)}


def save_tui_preferences(preferences: dict[str, str], path: Path | None = None) -> None:
    pref_path = path or tui_preferences_path()
    pref_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"layout_mode": normalize_layout_mode(preferences.get("layout_mode"))}
    tmp = pref_path.with_suffix(pref_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(pref_path)


def next_layout_mode(current: str) -> str:
    current = normalize_layout_mode(current)
    index = LAYOUT_MODES.index(current)
    return LAYOUT_MODES[(index + 1) % len(LAYOUT_MODES)]
```

- [ ] **Step 4: Wire live app state and `L` binding**

In `scripts/workflow_tui_app.py`, add binding:

```python
binding("l", "cycle_layout", "Layout"),
```

In `WorkflowDashboardApp.__init__`:

```python
self.layout_mode = tui.load_tui_preferences()["layout_mode"]
```

Pass `layout_mode=self.layout_mode` into `tui.render_dashboard(...)`.

Add action:

```python
def action_cycle_layout(self) -> None:
    self.layout_mode = tui.next_layout_mode(self.layout_mode)
    try:
        tui.save_tui_preferences({"layout_mode": self.layout_mode})
        self.notify(f"Layout: {self.layout_mode}", title="Workflow", timeout=0.8)
    except OSError as exc:
        self.notify(f"Layout: {self.layout_mode} (not saved: {exc})", title="Workflow", severity="warning", timeout=2.0)
    self.update_dashboard()
```

- [ ] **Step 5: Write failing live layout action test**

Add a fake-Textual test next to `test_live_footer_refreshes_agent_bindings_on_tab_change`:

```python
def test_live_layout_action_cycles_persists_and_renders(self) -> None:
    import types

    sys.path.insert(0, str(SCRIPTS))
    module_names = ["textual", "textual.app", "textual.screen", "textual.worker", "textual.widgets", "workflow_tui_app"]
    original_modules = {name: sys.modules.get(name) for name in module_names}
    for name in module_names:
        sys.modules.pop(name, None)

    observations: dict[str, object] = {}

    class FakeApp:
        def __init__(self) -> None:
            self.size = types.SimpleNamespace(width=110, height=30)

        def notify(self, *_args: object, **_kwargs: object) -> None:
            pass

        def run(self) -> None:
            self.dashboard = types.SimpleNamespace(size=types.SimpleNamespace(width=110, height=30), update=lambda value: observations.setdefault("rendered", value))
            self.runs = [{"run_id": "run-1", "agents": []}]
            self.action_cycle_layout()
            observations["layout_mode"] = self.layout_mode

    class FakeStatic:
        pass

    class FakeTui:
        TABS = ("runs", "agents", "attention")
        AGENT_SCOPES = ("phase", "all")
        AGENT_VIEWS = ("live", "prompt")
        LAYOUT_MODES = ("command", "ops", "timeline")
        UPDATE_CHECK_INTERVAL = 999.0
        UPDATE_CHECK_TIMEOUT = 1.0
        UPDATE_PULL_TIMEOUT = 1.0
        saved: list[dict[str, str]] = []

        @staticmethod
        def load_tui_preferences() -> dict[str, str]:
            return {"layout_mode": "command"}

        @staticmethod
        def save_tui_preferences(preferences: dict[str, str]) -> None:
            FakeTui.saved.append(preferences)

        @staticmethod
        def next_layout_mode(current: str) -> str:
            return "ops" if current == "command" else "timeline"

        @staticmethod
        def render_dashboard(*_args: object, **kwargs: object) -> str:
            observations["render_layout"] = kwargs["layout_mode"]
            return f"layout={kwargs['layout_mode']}"

        @staticmethod
        def current_rows_for(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
            return []

        @staticmethod
        def index_for_key(*_args: object, **_kwargs: object) -> int:
            return 0

        @staticmethod
        def clamp_index(index: int, length: int) -> int:
            return 0 if length <= 0 else max(0, min(index, length - 1))

        @staticmethod
        def action_enabled_for_tab(_tab: str, _action: str) -> bool:
            return True

    try:
        sys.modules["textual"] = types.ModuleType("textual")
        app_module = types.ModuleType("textual.app")
        app_module.App = FakeApp
        app_module.ComposeResult = object
        app_module.SystemCommand = object
        sys.modules["textual.app"] = app_module
        screen_module = types.ModuleType("textual.screen")
        screen_module.Screen = object
        sys.modules["textual.screen"] = screen_module
        worker_module = types.ModuleType("textual.worker")
        worker_module.Worker = type("Worker", (), {"StateChanged": object})
        worker_module.WorkerState = types.SimpleNamespace(ERROR="error", SUCCESS="success")
        sys.modules["textual.worker"] = worker_module
        widgets_module = types.ModuleType("textual.widgets")
        widgets_module.Footer = FakeStatic
        widgets_module.Header = FakeStatic
        widgets_module.Static = FakeStatic
        sys.modules["textual.widgets"] = widgets_module

        import workflow_tui_app  # pylint: disable=import-outside-toplevel

        workflow_tui_app.run_textual_app(FakeTui)
    finally:
        for name, original in original_modules.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original

    self.assertEqual(observations["layout_mode"], "ops")
    self.assertEqual(observations["render_layout"], "ops")
    self.assertEqual(FakeTui.saved, [{"layout_mode": "ops"}])
```

- [ ] **Step 6: Use preferences for live only, not snapshots**

In `render_snapshot`, resolve layout with:

```python
layout_mode = normalize_layout_mode(layout_mode)
```

In the live app, use the persisted mode. Do not write preferences from snapshot mode.

- [ ] **Step 7: Run tests**

Run:

```bash
pytest -q tests/test_workflow.py::WorkflowScriptTests::test_tui_preferences_read_write_and_fallback tests/test_workflow.py::WorkflowScriptTests::test_layout_mode_cycle_order tests/test_workflow.py::WorkflowScriptTests::test_live_layout_action_cycles_persists_and_renders
```

Expected: pass.

- [ ] **Step 8: Commit**

```bash
git add scripts/workflow_tui.py scripts/workflow_tui_app.py tests/test_workflow.py
git commit -m "feat(tui): persist layout mode preference"
```

## Task 3: Runs Tab Command/Ops/Timeline Renderers

**Files:**
- Modify: `scripts/workflow_tui_render.py`
- Modify: `scripts/workflow_tui.py`
- Test: `tests/test_workflow.py`
- Update: `tests/snapshots/snapshot-runs.txt`
- Add: `tests/snapshots/snapshot-runs-ops.txt`
- Add: `tests/snapshots/snapshot-runs-timeline.txt`

- [ ] **Step 1: Write failing renderer tests**

Add:

```python
def test_runs_snapshot_supports_layout_modes(self) -> None:
    for layout, expected_text in [
        ("command", "Run Agents"),
        ("ops", "Live Console"),
        ("timeline", "Timeline"),
    ]:
        with self.subTest(layout=layout):
            rendered = self.run_script(
                "workflow_tui.py",
                "--snapshot",
                "--fixture",
                str(FIXTURE),
                "--tab",
                "runs",
                "--layout",
                layout,
                "--width",
                "120",
                "--height",
                "34",
                env=self.snapshot_env(),
            ).stdout
            self.assertIn(expected_text, rendered)
```

- [ ] **Step 2: Run test to confirm it fails**

Run:

```bash
pytest -q tests/test_workflow.py::WorkflowScriptTests::test_runs_snapshot_supports_layout_modes
```

Expected: fail because `--layout` and mode renderers do not exist or command mode still uses `Run Detail`.

- [ ] **Step 3: Add run agents table helper**

In `scripts/workflow_tui_render.py`:

```python
def make_run_agents_table(run: dict[str, Any], selected: int = 0, visible: int = 8) -> Table:
    agents = list(run.get("agents", []))
    return make_agent_table(agents, selected, visible, "No agents for this run.")
```

- [ ] **Step 4: Split run facts from current detail**

Create helpers from existing `make_run_detail` code:

```python
def make_run_facts_panel(run: dict[str, Any]) -> Panel:
    control = run.get("control") or {}
    rows = [
        ("id", run.get("run_id", "")),
        ("title", run.get("title", "")),
        ("status", run.get("status", "")),
        ("mode", run.get("mode", "")),
        ("paused", "yes" if control.get("paused") else "no"),
        ("stop req", "yes" if control.get("stop_requested") else "no"),
        ("cwd", run.get("cwd", "")),
        ("updated", display_timestamp(run.get("updated_at", ""))),
        ("state", path_text(run)),
    ]
    return Panel(make_facts_table(rows), title="Run", border_style="blue", box=box.ROUNDED)
```

Keep `make_run_detail` working by composing helpers.

- [ ] **Step 5: Add command layout detail**

Add:

```python
def make_run_command_detail(run: dict[str, Any], *, detail_height: int | None = None, selected_agent: int = 0) -> Group:
    live = collect_run_activity(run)
    panels: list[Any] = [
        make_run_facts_panel(run),
        make_run_live_stats_panel(run, live),
        Panel(make_run_agents_table(run, selected_agent, 8), title="Run Agents", border_style="cyan", box=box.ROUNDED),
        Panel(make_run_live_strip(run, live), title="Live Ops", border_style="yellow", box=box.ROUNDED),
    ]
    return Group(*panels)
```

Move the existing live stats block from `make_run_detail` into `make_run_live_stats_panel(run, live)` without changing its row labels or styles. Move the existing merged-output block from `make_run_detail` into `make_run_live_strip(run, live)` without changing `detail_height`, `scroll_offset`, or truncation behavior. After the move, `make_run_detail` must call `make_run_facts_panel`, `make_run_live_stats_panel`, and `make_run_live_strip` so the old non-layout detail remains behaviorally equivalent.

- [ ] **Step 6: Add ops layout detail**

Add:

```python
def make_run_ops_detail(run: dict[str, Any], *, detail_height: int | None = None, selected_agent: int = 0) -> Group:
    live = collect_run_activity(run)
    return Group(
        Panel(make_run_live_strip(run, live), title="Live Console", border_style="yellow", box=box.ROUNDED),
        make_run_live_stats_panel(run, live),
        Panel(make_run_agents_table(run, selected_agent, 6), title="Active Agents", border_style="cyan", box=box.ROUNDED),
    )
```

- [ ] **Step 7: Add timeline layout detail**

Add:

```python
def make_run_timeline_detail(run: dict[str, Any], *, detail_height: int | None = None, selected_agent: int = 0) -> Group:
    return Group(
        Panel(make_phase_table(ordered_phases(run), 0, 8), title="Timeline", border_style="green", box=box.ROUNDED),
        Panel(make_run_agents_table(run, selected_agent, 6), title="Run Agents", border_style="cyan", box=box.ROUNDED),
        make_run_live_stats_panel(run, collect_run_activity(run)),
    )
```

- [ ] **Step 8: Dispatch by layout mode**

Change `make_detail_body(..., layout_mode="command", selected_run_agent_index=0)` so:

```python
if tab == "runs":
    selected_run = rows[clamp_index(selected, len(rows))]
    mode = normalize_layout_mode(layout_mode)
    if mode == "ops":
        return make_run_ops_detail(selected_run, detail_height=detail_height, selected_agent=selected_run_agent_index)
    if mode == "timeline":
        return make_run_timeline_detail(selected_run, detail_height=detail_height, selected_agent=selected_run_agent_index)
    return make_run_command_detail(selected_run, detail_height=detail_height, selected_agent=selected_run_agent_index)
```

Add `layout_mode: str = DEFAULT_LAYOUT_MODE` to `render_dashboard(...)` and `render_snapshot(...)`, normalize it once in each function with `normalize_layout_mode(layout_mode)`, pass it into `make_header(..., layout_mode=layout_mode)` and `make_detail_body(..., layout_mode=layout_mode, selected_run_agent_index=selected_run_agent_index)`.

- [ ] **Step 9: Run focused tests**

Run:

```bash
pytest -q tests/test_workflow.py::WorkflowScriptTests::test_runs_snapshot_supports_layout_modes
```

Expected: pass.

- [ ] **Step 10: Update snapshots intentionally**

Render:

```bash
python3 scripts/workflow_tui.py --snapshot --fixture tests/fixtures/rich-workflow.json --tab runs --layout command --width 110 --height 30 > tests/snapshots/snapshot-runs.txt
python3 scripts/workflow_tui.py --snapshot --fixture tests/fixtures/rich-workflow.json --tab runs --layout ops --width 110 --height 30 > tests/snapshots/snapshot-runs-ops.txt
python3 scripts/workflow_tui.py --snapshot --fixture tests/fixtures/rich-workflow.json --tab runs --layout timeline --width 110 --height 30 > tests/snapshots/snapshot-runs-timeline.txt
```

Add these cases to `test_snapshot_fixtures_match_checked_in_screens`.

- [ ] **Step 11: Run snapshot tests**

Run:

```bash
pytest -q tests/test_workflow.py::WorkflowScriptTests::test_snapshot_fixtures_match_checked_in_screens tests/test_workflow.py::WorkflowScriptTests::test_runs_snapshot_supports_layout_modes
```

Expected: pass.

- [ ] **Step 12: Commit**

```bash
git add scripts/workflow_tui.py scripts/workflow_tui_render.py tests/test_workflow.py tests/snapshots/snapshot-runs.txt tests/snapshots/snapshot-runs-ops.txt tests/snapshots/snapshot-runs-timeline.txt
git commit -m "feat(tui): add run layout modes"
```

## Task 4: Run-To-Agent Drill-Down Navigation

**Files:**
- Modify: `scripts/workflow_tui_app.py`
- Modify: `scripts/workflow_tui.py`
- Modify: `scripts/workflow_tui_render.py`
- Test: `tests/test_workflow.py`

- [ ] **Step 1: Write failing selection helper tests**

Add:

```python
def test_agent_index_for_selected_run_agent_id(self) -> None:
    sys.path.insert(0, str(SCRIPTS))
    import workflow_tui  # pylint: disable=import-outside-toplevel

    run = workflow_tui.load_fixture(str(FIXTURE))[0]
    agents = run["agents"]
    target = agents[-1]["agent_id"]
    self.assertEqual(workflow_tui.agent_index_for_id(run, target), len(agents) - 1)
    self.assertEqual(workflow_tui.agent_index_for_id(run, "missing"), 0)
```

- [ ] **Step 2: Run test to confirm it fails**

Run:

```bash
pytest -q tests/test_workflow.py::WorkflowScriptTests::test_agent_index_for_selected_run_agent_id
```

Expected: fail because helper does not exist.

- [ ] **Step 3: Add agent selection helpers**

In `scripts/workflow_tui_render.py` or `scripts/workflow_tui.py`:

```python
def agent_index_for_id(run: dict[str, Any] | None, agent_id: str | None) -> int:
    agents = list((run or {}).get("agents", []))
    if not agent_id:
        return 0
    for index, agent in enumerate(agents):
        if str(agent.get("agent_id", "")) == str(agent_id):
            return index
    return 0
```

Re-export if placed in render module.

- [ ] **Step 4: Add live app state**

In `WorkflowDashboardApp.__init__`:

```python
self.run_focus = "runs"
self.selected_run_agent_id: str | None = None
self.selected_run_agent_index = 0
```

When rendering, pass `selected_run_agent_index=self.selected_run_agent_index`.

- [ ] **Step 5: Add snapshot selected-agent plumbing**

In `scripts/workflow_tui.py`, add the snapshot-only parser argument:

```python
parser.add_argument("--selected-run-agent-index", type=int, default=0)
```

Pass it into `render_snapshot`:

```python
selected_run_agent_index=args.selected_run_agent_index,
```

Update `render_snapshot`, `render_dashboard`, and `make_detail_body` signatures to accept:

```python
selected_run_agent_index: int = 0
```

Pass `selected_run_agent_index` through to `make_detail_body(...)` so runs snapshots can mark the selected run agent deterministically.

- [ ] **Step 6: Add focused run-agent movement helpers**

Add helper methods in `WorkflowDashboardApp`:

```python
def selected_run_agents(self) -> list[dict[str, Any]]:
    return list((self.selected_run or {}).get("agents", []))


def set_selected_run_agent_index(self, index: int) -> None:
    agents = self.selected_run_agents()
    self.selected_run_agent_index = tui.clamp_index(index, len(agents))
    self.selected_run_agent_id = str(agents[self.selected_run_agent_index].get("agent_id") or "") if agents else None


def move_run_agent_selection(self, delta: int) -> bool:
    if self.tab != "runs" or self.run_focus != "agents":
        return False
    self.set_selected_run_agent_index(self.selected_run_agent_index + delta)
    self.update_dashboard()
    return True
```

Then add this guard as the first branch of `action_move_down`:

```python
if self.move_run_agent_selection(1):
    return
```

and this guard as the first branch of `action_move_up`:

```python
if self.move_run_agent_selection(-1):
    return
```

For `action_top`, use:

```python
if self.tab == "runs" and self.run_focus == "agents":
    self.set_selected_run_agent_index(0)
    self.update_dashboard()
    return
```

For `action_bottom`, use:

```python
if self.tab == "runs" and self.run_focus == "agents":
    self.set_selected_run_agent_index(len(self.selected_run_agents()) - 1)
    self.update_dashboard()
    return
```

For `action_page_down`, use:

```python
if self.tab == "runs" and self.run_focus == "agents":
    self.set_selected_run_agent_index(self.selected_run_agent_index + self.page_step())
    self.update_dashboard()
    return
```

For `action_page_up`, use:

```python
if self.tab == "runs" and self.run_focus == "agents":
    self.set_selected_run_agent_index(self.selected_run_agent_index - self.page_step())
    self.detail_scroll_offset = 0
    self.update_dashboard()
    return
```

- [ ] **Step 7: Add focus and jump actions**

Add focus and jump methods:

```python
def focus_run_agents(self) -> bool:
    if self.tab != "runs" or self.run_focus == "agents":
        return False
    agents = self.selected_run_agents()
    if not agents:
        self.notify("Selected run has no agents.", title="Workflow", severity="warning", timeout=1.0)
        return True
    self.run_focus = "agents"
    self.set_selected_run_agent_index(tui.agent_index_for_id(self.selected_run, self.selected_run_agent_id))
    self.update_dashboard()
    return True


def open_selected_run_agent(self) -> bool:
    if self.tab != "runs" or self.run_focus != "agents":
        return False
    agents = list((self.selected_run or {}).get("agents", []))
    if not agents:
        return True
    agent = agents[tui.clamp_index(self.selected_run_agent_index, len(agents))]
    agent_id = str(agent.get("agent_id") or "")
    self.agent_scope_index = tui.AGENT_SCOPES.index("all") if "all" in tui.AGENT_SCOPES else self.agent_scope_index
    self.selected_row_ids["agents"] = agent_id
    self.tab_index = tui.TABS.index("agents")
    self.run_focus = "runs"
    rows = self.active_rows()
    self.row_index = tui.index_for_key(rows, "agents", agent_id)
    self.fallback_indexes["agents"] = self.row_index
    self.update_tab_chrome()
    return True
```

Change `action_toggle_focus` so it starts with:

```python
if self.open_selected_run_agent() or self.focus_run_agents():
    return
```

- [ ] **Step 8: Make right/left context-sensitive without regressing tab navigation**

Change bindings:

```python
binding("right", "nav_right", "Next", show=False),
binding("left", "nav_left", "Prev", show=False),
```

Add actions:

```python
def action_nav_right(self) -> None:
    if self.open_selected_run_agent() or self.focus_run_agents():
        return
    self.action_next_tab()


def action_nav_left(self) -> None:
    if self.tab == "runs" and self.run_focus == "agents":
        self.run_focus = "runs"
        self.update_dashboard()
        return
    self.action_previous_tab()
```

Change `action_escape_or_quit` to start with:

```python
if self.tab == "runs" and self.run_focus == "agents":
    self.run_focus = "runs"
    self.update_dashboard()
    return
```

- [ ] **Step 9: Add selection transfer tests**

Add pure helper tests for the selection functions and one render-level assertion that the selected run-agent index marks the intended agent in the runs detail:

```python
def test_run_agent_selection_key_transfers_to_agents_tab(self) -> None:
    sys.path.insert(0, str(SCRIPTS))
    import workflow_tui  # pylint: disable=import-outside-toplevel

    run = workflow_tui.load_fixture(str(FIXTURE))[0]
    target = run["agents"][-1]["agent_id"]
    rows = workflow_tui.rows_for_tab(run, "agents", [run], agent_scope="all")
    index = workflow_tui.index_for_key(rows, "agents", target)
    self.assertEqual(rows[index]["agent_id"], target)


def test_runs_detail_marks_selected_run_agent(self) -> None:
    sys.path.insert(0, str(SCRIPTS))
    import workflow_tui  # pylint: disable=import-outside-toplevel

    run = workflow_tui.load_fixture(str(FIXTURE))[0]
    rendered = self.run_script(
        "workflow_tui.py",
        "--snapshot",
        "--fixture",
        str(FIXTURE),
        "--tab",
        "runs",
        "--layout",
        "command",
        "--selected-run-agent-index",
        "1",
        "--width",
        "120",
        "--height",
        "34",
        env=self.snapshot_env(),
    ).stdout
    self.assertIn(run["agents"][1]["name"], rendered)
```

- [ ] **Step 10: Add live run drill-down tests**

Add a fake-Textual test next to the existing live TUI tests:

```python
def test_live_run_agent_drilldown_navigation(self) -> None:
    import types

    sys.path.insert(0, str(SCRIPTS))
    module_names = ["textual", "textual.app", "textual.screen", "textual.worker", "textual.widgets", "workflow_tui_app"]
    original_modules = {name: sys.modules.get(name) for name in module_names}
    for name in module_names:
        sys.modules.pop(name, None)

    observations: dict[str, object] = {}

    class FakeApp:
        def __init__(self) -> None:
            self.size = types.SimpleNamespace(width=110, height=30)
            self.refresh_binding_calls = 0

        def refresh_bindings(self) -> None:
            self.refresh_binding_calls += 1

        def notify(self, *_args: object, **_kwargs: object) -> None:
            pass

        def run(self) -> None:
            self.dashboard = types.SimpleNamespace(size=types.SimpleNamespace(width=110, height=30), update=lambda _value: None)
            self.runs = [{"run_id": "run-1", "agents": [{"agent_id": "agent-a"}, {"agent_id": "agent-b"}]}]
            self.selected_run_id = "run-1"
            self.action_toggle_focus()
            observations["after_enter_focus"] = self.run_focus
            self.action_move_down()
            observations["selected_run_agent_id"] = self.selected_run_agent_id
            self.action_nav_right()
            observations["after_right_tab"] = self.tab
            observations["after_right_row"] = self.selected_row_ids["agents"]
            self.tab_index = FakeTui.TABS.index("runs")
            self.run_focus = "agents"
            self.action_nav_left()
            observations["after_left_focus"] = self.run_focus
            self.run_focus = "agents"
            self.action_escape_or_quit()
            observations["after_escape_focus"] = self.run_focus

    class FakeStatic:
        pass

    class FakeTui:
        TABS = ("runs", "agents", "attention")
        AGENT_SCOPES = ("phase", "all")
        AGENT_VIEWS = ("live", "prompt")
        UPDATE_CHECK_INTERVAL = 999.0
        UPDATE_CHECK_TIMEOUT = 1.0
        UPDATE_PULL_TIMEOUT = 1.0

        @staticmethod
        def load_tui_preferences() -> dict[str, str]:
            return {"layout_mode": "command"}

        @staticmethod
        def render_dashboard(*_args: object, **_kwargs: object) -> str:
            return ""

        @staticmethod
        def current_rows_for(
            selected_run: dict[str, object] | None,
            tab: str,
            *_args: object,
            agent_scope: str = "phase",
            **_kwargs: object,
        ) -> list[dict[str, object]]:
            if tab == "agents":
                agents = list((selected_run or {}).get("agents", []))
                if agent_scope == "phase":
                    return agents[:1]
                return agents
            return [selected_run] if selected_run else []

        @staticmethod
        def item_key(tab: str, row: dict[str, object], index: int) -> str:
            if tab == "runs":
                return str(row.get("run_id") or index)
            if tab == "agents":
                return str(row.get("agent_id") or index)
            return str(index)

        @staticmethod
        def index_for_key(rows: list[dict[str, object]], tab: str, key: str | None) -> int:
            for index, row in enumerate(rows):
                if FakeTui.item_key(tab, row, index) == key:
                    return index
            return 0

        @staticmethod
        def agent_index_for_id(run: dict[str, object] | None, agent_id: str | None) -> int:
            agents = list((run or {}).get("agents", []))
            for index, agent in enumerate(agents):
                if str(agent.get("agent_id")) == str(agent_id):
                    return index
            return 0

        @staticmethod
        def clamp_index(index: int, length: int) -> int:
            return 0 if length <= 0 else max(0, min(index, length - 1))

        @staticmethod
        def action_enabled_for_tab(_tab: str, _action: str) -> bool:
            return True

    try:
        sys.modules["textual"] = types.ModuleType("textual")
        app_module = types.ModuleType("textual.app")
        app_module.App = FakeApp
        app_module.ComposeResult = object
        app_module.SystemCommand = object
        sys.modules["textual.app"] = app_module
        screen_module = types.ModuleType("textual.screen")
        screen_module.Screen = object
        sys.modules["textual.screen"] = screen_module
        worker_module = types.ModuleType("textual.worker")
        worker_module.Worker = type("Worker", (), {"StateChanged": object})
        worker_module.WorkerState = types.SimpleNamespace(ERROR="error", SUCCESS="success")
        sys.modules["textual.worker"] = worker_module
        widgets_module = types.ModuleType("textual.widgets")
        widgets_module.Footer = FakeStatic
        widgets_module.Header = FakeStatic
        widgets_module.Static = FakeStatic
        sys.modules["textual.widgets"] = widgets_module

        import workflow_tui_app  # pylint: disable=import-outside-toplevel

        workflow_tui_app.run_textual_app(FakeTui)
    finally:
        for name, original in original_modules.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original

    self.assertEqual(observations["after_enter_focus"], "agents")
    self.assertEqual(observations["selected_run_agent_id"], "agent-b")
    self.assertEqual(observations["after_right_tab"], "agents")
    self.assertEqual(observations["after_right_row"], "agent-b")
    self.assertEqual(observations["after_left_focus"], "runs")
    self.assertEqual(observations["after_escape_focus"], "runs")
```

- [ ] **Step 11: Run focused tests**

Run:

```bash
pytest -q tests/test_workflow.py::WorkflowScriptTests::test_agent_index_for_selected_run_agent_id tests/test_workflow.py::WorkflowScriptTests::test_run_agent_selection_key_transfers_to_agents_tab tests/test_workflow.py::WorkflowScriptTests::test_runs_detail_marks_selected_run_agent tests/test_workflow.py::WorkflowScriptTests::test_live_run_agent_drilldown_navigation
```

Expected: pass.

- [ ] **Step 12: Commit**

```bash
git add scripts/workflow_tui.py scripts/workflow_tui_app.py scripts/workflow_tui_render.py tests/test_workflow.py
git commit -m "feat(tui): add run to agent drilldown"
```

## Task 5: Docs, Keys, And Final Verification

**Files:**
- Modify: `SKILL.md`
- Modify: `references/operations.md`
- Modify: `tests/test_workflow.py`
- Modify: `tests/snapshots/*` if previous tasks changed snapshots

- [ ] **Step 1: Write failing doc/shortcut test**

Add or update the existing key test:

```python
def test_tui_key_docs_include_layout_and_attention(self) -> None:
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    operations = (ROOT / "references" / "operations.md").read_text(encoding="utf-8")
    for text in (skill, operations):
        self.assertIn("L", text)
        self.assertIn("layout", text.lower())
        self.assertIn("attention", text.lower())
```

- [ ] **Step 2: Run test to confirm it fails if docs are stale**

Run:

```bash
pytest -q tests/test_workflow.py::WorkflowScriptTests::test_tui_key_docs_include_layout_and_attention
```

Expected: fail until docs mention layout/attention.

- [ ] **Step 3: Update `SKILL.md` TUI key paragraph**

Replace the TUI key sentence with:

```markdown
In the TUI, use arrow keys to move rows/tabs, `L` to cycle global layout mode, `!` to jump to attention, `Enter`/`Right` on runs to drill into agents, `a` to toggle phase/all agent scope, `v` to toggle live output/prompt, `y` to copy the selected id, `p` to copy the useful path, and `Ctrl-Y` to copy selected-row JSON.
```

- [ ] **Step 4: Update `references/operations.md` TUI Keys**

Change the TUI key list to include:

```markdown
- `L`: cycle global runs layout (`command`, `ops`, `timeline`) and persist the last-used mode.
- `!`: jump to the attention tab.
- On `runs`, `Enter` or `Right`: focus the selected run's agent list; from that list, `Enter` or `Right` opens the selected agent in the agents tab.
- On focused run agents, `Left` or `Escape`: return to the runs list.
```

Also change any phrase that says overview is the first/default tab to say runs is the home tab and attention is the notification tray.

- [ ] **Step 5: Run doc test**

Run:

```bash
pytest -q tests/test_workflow.py::WorkflowScriptTests::test_tui_key_docs_include_layout_and_attention
```

Expected: pass.

- [ ] **Step 6: Run focused TUI test suite**

Run:

```bash
pytest -q tests/test_workflow.py -k 'tui or snapshot or attention or layout or run_agent'
```

Expected: pass.

- [ ] **Step 7: Run full suite**

Run:

```bash
pytest -q
```

Expected: all tests pass.

- [ ] **Step 8: Review diff**

Run:

```bash
git diff --stat
git diff --check
git diff -- tests/snapshots
```

Expected: no whitespace errors; snapshot diffs are intentional and readable.

- [ ] **Step 9: Commit final docs/test polish**

```bash
git add SKILL.md references/operations.md tests/test_workflow.py tests/snapshots
git commit -m "docs(tui): document layout and attention navigation"
```

## Final Acceptance

Run:

```bash
pytest -q
git status --short
```

Expected:

- Full test suite passes.
- Only unrelated pre-existing untracked files remain, if any.
- Manual snapshot command works:

```bash
python3 scripts/workflow_tui.py --snapshot --fixture tests/fixtures/rich-workflow.json --tab runs --layout command --width 120 --height 34
```

The output should show a runs-first command dashboard with `layout: command`, an agent list for the selected run, and no `overview` tab label.

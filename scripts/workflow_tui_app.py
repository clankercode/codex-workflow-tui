#!/usr/bin/env python3
"""Live Textual app wrapper for workflow_tui rendering helpers."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import workflow_state


def copy_to_system_clipboard(value: str) -> tuple[bool, str]:
    """Copy to the regular desktop clipboard used by Ctrl+V."""
    commands = [
        ("wl-copy", ["wl-copy"]),
        ("xclip", ["xclip", "-selection", "clipboard"]),
        ("xsel", ["xsel", "--clipboard", "--input"]),
    ]
    for method, command in commands:
        executable = shutil.which(command[0])
        if not executable:
            continue
        try:
            subprocess.run([executable, *command[1:]], input=value, text=True, check=True, capture_output=True, timeout=2.0)
        except (OSError, subprocess.SubprocessError):
            continue
        return True, method
    return False, ""


def maybe_reexec_textual_venv(tui: Any) -> None:
    """Restart the CLI backend inside the workflow virtualenv when Textual is installed there."""
    venv_python = workflow_state.workflow_root() / ".venv" / "bin" / "python"
    current = Path(sys.executable).resolve()
    if venv_python.exists() and current != venv_python.resolve():
        entrypoint = Path(tui.__file__).resolve()
        os.execv(str(venv_python), [str(venv_python), str(entrypoint), *sys.argv[1:]])


def run_textual_app(tui: Any) -> None:
    """Run the live Textual dashboard using the core TUI module as a backend."""
    try:
        from textual.app import App, ComposeResult, SystemCommand
        from textual.screen import Screen
        from textual.worker import Worker, WorkerState
        from textual.widgets import Footer, Header, Static
    except ModuleNotFoundError as exc:
        if exc.name == "textual":
            maybe_reexec_textual_venv(tui)
        raise SystemExit("Textual is required for the live TUI. Run `workflow tui` or install the workflow virtualenv.") from exc
    try:
        from textual.binding import Binding
    except ModuleNotFoundError:
        Binding = None  # type: ignore[assignment]

    def binding(key: str, action: str, description: str, *, show: bool = True) -> Any:
        """Return a Textual binding, falling back to tuple shape in tests."""
        if Binding is None:
            return (key, action, description)
        return Binding(key, action, description, show=show)

    class WorkflowDashboardApp(App):
        CSS = """
        Screen {
            background: #101418;
            color: #d7dde8;
        }

        #dashboard {
            width: 100%;
            height: 1fr;
            padding: 0;
        }
        """
        BINDINGS = [
            binding("q", "quit", "Quit"),
            binding("escape", "escape_or_quit", "Back"),
            binding("r", "reload_runs", "Reload"),
            binding("a", "toggle_agent_scope", "Scope"),
            binding("v", "toggle_agent_view", "View"),
            binding("y", "copy_selected_id", "ID"),
            binding("p", "copy_selected_path", "Path"),
            binding("ctrl+y", "copy_selected_json", "Row"),
            binding("enter", "toggle_focus", "Focus"),
            binding("/", "cycle_filter", "Filter"),
            binding("!", "show_attention", "Attn"),
            binding("c", "clear_filter", "Clear"),
            binding("tab", "next_tab", "Next", show=False),
            binding("shift+tab", "previous_tab", "Prev", show=False),
            binding("right", "next_tab", "Next", show=False),
            binding("left", "previous_tab", "Prev", show=False),
            binding("j", "move_down", "Down", show=False),
            binding("k", "move_up", "Up", show=False),
            binding("down", "move_down", "Down", show=False),
            binding("up", "move_up", "Up", show=False),
            binding("space", "page_down", "Page down", show=False),
            binding("pagedown", "page_down", "Page down", show=False),
            binding("pageup", "page_up", "Page up", show=False),
            binding("g", "top", "Top", show=False),
            binding("G", "bottom", "Bottom", show=False),
        ]
        TITLE = "Agent Workflows"

        def __init__(self) -> None:
            super().__init__()
            self.runs: list[dict[str, Any]] = []
            self.run_index = 0
            self.row_index = 0
            self.tab_index = 0
            self.agent_scope_index = 0
            self.agent_view_index = 0
            self.filter_index = 0
            self.filter_presets = ("", "failed", "blocked", "running", "artifact")
            self.focus_mode = False
            self.selected_run_id: str | None = None
            self.selected_row_ids: dict[str, str | None] = {tab: None for tab in tui.TABS}
            self.fallback_indexes: dict[str, int] = {tab: 0 for tab in tui.TABS}
            self.dashboard: Static | None = None
            self.update_status: tui.UpdateStatus | None = None
            self.notified_update_head: str | None = None
            self.detail_scroll_offset: int = 0

        @property
        def tab(self) -> str:
            return tui.TABS[self.tab_index]

        @property
        def agent_scope(self) -> str:
            return tui.AGENT_SCOPES[self.agent_scope_index]

        @property
        def agent_view(self) -> str:
            return tui.AGENT_VIEWS[self.agent_view_index]

        @property
        def selected_run(self) -> dict[str, Any] | None:
            if not self.runs:
                return None
            if self.selected_run_id:
                self.run_index = tui.index_for_key(self.runs, "runs", self.selected_run_id)
            else:
                self.run_index = tui.clamp_index(self.run_index, len(self.runs))
                self.selected_run_id = tui.item_key("runs", self.runs[self.run_index], self.run_index)
            return self.runs[self.run_index]

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield Static(id="dashboard")
            yield Footer()

        def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
            yield from super().get_system_commands(screen)
            yield SystemCommand(
                "Workflow: Check for updates",
                "Check the workflow skill git upstream without blocking the TUI.",
                self.action_check_for_updates,
            )
            yield SystemCommand(
                "Workflow: Update skill from git",
                "Run git pull --ff-only in the workflow skill checkout.",
                self.action_update_skill_from_git,
            )
            yield SystemCommand(
                "Workflow: Pause selected run",
                "Pause the selected workflow before launching more workers.",
                self.action_pause_selected_run,
            )
            yield SystemCommand(
                "Workflow: Resume selected run",
                "Resume the selected paused workflow.",
                self.action_resume_selected_run,
            )
            yield SystemCommand(
                "Workflow: Stop selected run",
                "Cancel the selected workflow and terminate recorded active workers.",
                self.action_stop_selected_run,
            )

        def on_mount(self) -> None:
            self.dashboard = self.query_one("#dashboard", Static)
            self.reload_state()
            self.set_interval(1.0, self.reload_state)
            self.start_update_check(notify_current=False)
            self.set_interval(tui.UPDATE_CHECK_INTERVAL, lambda: self.start_update_check(notify_current=False))

        def start_update_check(self, notify_current: bool) -> None:
            if notify_current:
                self.notify("Checking workflow skill updates...", title="Workflow", timeout=1.0)
            name = "workflow-update-check-manual" if notify_current else "workflow-update-check-auto"
            self.run_worker(
                lambda: tui.check_skill_update(timeout=tui.UPDATE_CHECK_TIMEOUT),
                name=name,
                group="workflow-update-check",
                exit_on_error=False,
                exclusive=True,
                thread=True,
            )

        def start_skill_update(self) -> None:
            self.notify("Updating workflow skill from git...", title="Workflow", timeout=1.2)
            self.run_worker(
                lambda: tui.update_skill_from_git(timeout=tui.UPDATE_PULL_TIMEOUT, check_timeout=tui.UPDATE_CHECK_TIMEOUT),
                name="workflow-update-pull",
                group="workflow-update-pull",
                exit_on_error=False,
                exclusive=True,
                thread=True,
            )

        def selected_run_id_for_action(self) -> str:
            selected = self.selected_run
            return str((selected or {}).get("run_id") or self.selected_run_id or "")

        def start_workflow_control(self, action: str) -> None:
            run_id = self.selected_run_id_for_action()
            if not run_id:
                self.notify("No workflow run is selected.", title="Workflow", severity="warning", timeout=1.5)
                return
            self.notify(f"{action.title()} requested for {run_id}.", title="Workflow", timeout=1.2)
            self.run_worker(
                lambda: tui.workflow_control_action(run_id, action),
                name=f"workflow-control-{action}",
                group="workflow-control",
                exit_on_error=False,
                exclusive=True,
                thread=True,
            )

        def handle_update_status(self, status: Any, notify_current: bool) -> None:
            self.update_status = status
            if status.state == "available":
                should_notify = notify_current or self.notified_update_head != status.remote_head
                self.notified_update_head = status.remote_head
                if should_notify:
                    self.notify(
                        f"{status.message} Use the command palette to run Workflow: Update skill from git.",
                        title="Workflow update",
                        severity="warning",
                        timeout=8.0,
                    )
                return
            if status.state == "current":
                self.notified_update_head = None
                if notify_current:
                    self.notify(status.message, title="Workflow", timeout=3.0)
                return
            if notify_current:
                self.notify(status.message, title="Workflow update", severity="warning", timeout=5.0)

        def handle_update_action(self, result: Any) -> None:
            self.update_status = result.status
            if result.status.state == "current":
                self.notified_update_head = None
            severity = "error" if not result.success else "warning" if result.status.state == "available" else "information"
            self.notify(result.message, title="Workflow update", severity=severity, timeout=6.0)

        def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
            if event.state == WorkerState.ERROR and event.worker.name.startswith("workflow-update-"):
                self.notify("Workflow update worker failed.", title="Workflow update", severity="error", timeout=5.0)
                return
            if event.state != WorkerState.SUCCESS:
                return
            if event.worker.name.startswith("workflow-update-check-"):
                status = event.worker.result
                if isinstance(status, tui.UpdateStatus):
                    self.handle_update_status(status, notify_current=event.worker.name.endswith("-manual"))
                return
            if event.worker.name == "workflow-update-pull":
                result = event.worker.result
                if isinstance(result, tui.UpdateActionResult):
                    self.handle_update_action(result)
                return
            if event.worker.name.startswith("workflow-control-"):
                result = event.worker.result
                if isinstance(result, tui.WorkflowControlResult):
                    severity = "information" if result.success else "error"
                    self.notify(result.message, title="Workflow", severity=severity, timeout=4.0)
                    self.reload_state()

        def reload_state(self) -> None:
            self.capture_selection()
            self.runs = tui.load_runs()
            self.restore_selection()
            self.update_dashboard()

        def active_rows(self) -> list[dict[str, Any]]:
            rows = tui.current_rows_for(
                self.selected_run,
                self.tab,
                self.runs,
                selected_phase_id=self.selected_row_ids.get("phases"),
                agent_scope=self.agent_scope,
            )
            filter_rows = getattr(tui, "apply_row_filter", None)
            if filter_rows is None:
                return rows
            return filter_rows(rows, self.filter_text)

        @property
        def filter_text(self) -> str:
            return self.filter_presets[self.filter_index]

        def capture_selection(self) -> None:
            if self.runs:
                self.run_index = tui.clamp_index(self.run_index, len(self.runs))
                self.selected_run_id = tui.item_key("runs", self.runs[self.run_index], self.run_index)
            rows = self.active_rows()
            if rows:
                index = self.run_index if self.tab == "runs" else self.row_index
                index = tui.clamp_index(index, len(rows))
                self.selected_row_ids[self.tab] = tui.item_key(self.tab, rows[index], index)
                self.fallback_indexes[self.tab] = index

        def restore_selection(self) -> None:
            self.run_index = tui.index_for_key(self.runs, "runs", self.selected_run_id)
            if self.runs:
                self.selected_run_id = tui.item_key("runs", self.runs[self.run_index], self.run_index)
            rows = self.active_rows()
            if self.tab == "runs":
                self.row_index = 0
                return
            selected_id = self.selected_row_ids.get(self.tab)
            fallback = self.fallback_indexes.get(self.tab, 0)
            self.row_index = tui.index_for_key(rows, self.tab, selected_id) if selected_id else tui.clamp_index(fallback, len(rows))
            if rows:
                self.selected_row_ids[self.tab] = tui.item_key(self.tab, rows[self.row_index], self.row_index)

        def update_dashboard(self) -> None:
            if self.dashboard is None:
                return
            width = max(0, self.dashboard.size.width, self.size.width - 2)
            height = max(0, self.dashboard.size.height, self.size.height - 3)
            self.dashboard.update(
                tui.render_dashboard(
                    self.runs,
                    width=width,
                    height=height,
                    tab=self.tab,
                    run_index=self.run_index,
                    row_index=self.row_index,
                    chrome=False,
                    selected_phase_id=self.selected_row_ids.get("phases"),
                    agent_scope=self.agent_scope,
                    agent_view=self.agent_view,
                    filter_text=self.filter_text,
                    focus=self.focus_mode,
                    scroll_offset=self.detail_scroll_offset,
                )
            )

        def update_tab_chrome(self) -> None:
            self.refresh_bindings()
            self.update_dashboard()

        def action_escape_or_quit(self) -> None:
            if self.focus_mode:
                self.focus_mode = False
                self.update_dashboard()
                return
            self.exit()

        def action_reload_runs(self) -> None:
            self.reload_state()

        def action_check_for_updates(self) -> None:
            self.start_update_check(notify_current=True)

        def action_update_skill_from_git(self) -> None:
            self.start_skill_update()

        def action_pause_selected_run(self) -> None:
            self.start_workflow_control("pause")

        def action_resume_selected_run(self) -> None:
            self.start_workflow_control("resume")

        def action_stop_selected_run(self) -> None:
            self.start_workflow_control("stop")

        def check_action(self, action: str, _parameters: tuple[object, ...]) -> bool | None:
            if not tui.action_enabled_for_tab(self.tab, action):
                return False
            return True

        def action_toggle_agent_scope(self) -> None:
            if not tui.action_enabled_for_tab(self.tab, "toggle_agent_scope"):
                return
            self.capture_selection()
            self.agent_scope_index = (self.agent_scope_index + 1) % len(tui.AGENT_SCOPES)
            self.restore_selection()
            self.update_dashboard()

        def action_toggle_agent_view(self) -> None:
            if not tui.action_enabled_for_tab(self.tab, "toggle_agent_view"):
                return
            self.agent_view_index = (self.agent_view_index + 1) % len(tui.AGENT_VIEWS)
            self.update_dashboard()

        def copy_selection(self, mode: str) -> None:
            rows = self.active_rows()
            selected = self.run_index if self.tab == "runs" else self.row_index
            label, value = tui.copy_value_for_selection(self.selected_run, self.tab, rows, selected, mode)
            if not value:
                self.notify(f"No {label or mode} to copy", title="Workflow", severity="warning", timeout=0.8)
                return
            with contextlib.suppress(Exception):
                self.copy_to_clipboard(value)
            copied, method = copy_to_system_clipboard(value)
            if copied:
                self.notify(f"Copied {label} to clipboard ({method})", title="Workflow", timeout=0.8)
            else:
                self.notify(f"Copied {label} inside Textual; no system clipboard helper found", title="Workflow", severity="warning", timeout=2.0)

        def action_copy_selected_id(self) -> None:
            self.copy_selection("id")

        def action_copy_selected_path(self) -> None:
            self.copy_selection("path")

        def action_copy_selected_json(self) -> None:
            self.copy_selection("json")

        def action_toggle_focus(self) -> None:
            if not self.active_rows():
                return
            self.focus_mode = not self.focus_mode
            self.update_dashboard()

        def action_cycle_filter(self) -> None:
            self.capture_selection()
            self.filter_index = (self.filter_index + 1) % len(self.filter_presets)
            self.restore_selection()
            label = self.filter_text or "none"
            self.notify(f"Filter: {label}", title="Workflow", timeout=0.8)
            self.update_dashboard()

        def action_clear_filter(self) -> None:
            if self.filter_index == 0:
                return
            self.capture_selection()
            self.filter_index = 0
            self.restore_selection()
            self.notify("Filter cleared", title="Workflow", timeout=0.8)
            self.update_dashboard()

        def action_show_attention(self) -> None:
            self.capture_selection()
            self.tab_index = tui.TABS.index("overview")
            self.focus_mode = False
            self.detail_scroll_offset = 0
            self.restore_selection()
            self.update_tab_chrome()

        def action_next_tab(self) -> None:
            self.capture_selection()
            self.tab_index = (self.tab_index + 1) % len(tui.TABS)
            self.focus_mode = False
            self.detail_scroll_offset = 0
            self.restore_selection()
            self.update_tab_chrome()

        def action_previous_tab(self) -> None:
            self.capture_selection()
            self.tab_index = (self.tab_index - 1) % len(tui.TABS)
            self.focus_mode = False
            self.detail_scroll_offset = 0
            self.restore_selection()
            self.update_tab_chrome()

        def action_move_down(self) -> None:
            rows = self.active_rows()
            if self.tab == "runs":
                self.run_index = tui.clamp_index(self.run_index + 1, len(self.runs))
                if self.runs:
                    self.selected_run_id = tui.item_key("runs", self.runs[self.run_index], self.run_index)
            else:
                self.row_index = tui.clamp_index(self.row_index + 1, len(rows))
                if rows:
                    self.selected_row_ids[self.tab] = tui.item_key(self.tab, rows[self.row_index], self.row_index)
                    self.fallback_indexes[self.tab] = self.row_index
            self.update_dashboard()

        def action_move_up(self) -> None:
            if self.tab == "runs":
                self.run_index = tui.clamp_index(self.run_index - 1, len(self.runs))
                if self.runs:
                    self.selected_run_id = tui.item_key("runs", self.runs[self.run_index], self.run_index)
            else:
                self.row_index = max(0, self.row_index - 1)
                rows = self.active_rows()
                if rows:
                    self.selected_row_ids[self.tab] = tui.item_key(self.tab, rows[self.row_index], self.row_index)
                    self.fallback_indexes[self.tab] = self.row_index
            self.update_dashboard()

        def action_top(self) -> None:
            if self.tab == "runs":
                self.run_index = 0
                if self.runs:
                    self.selected_run_id = tui.item_key("runs", self.runs[self.run_index], self.run_index)
            else:
                self.row_index = 0
                rows = self.active_rows()
                if rows:
                    self.selected_row_ids[self.tab] = tui.item_key(self.tab, rows[self.row_index], self.row_index)
                    self.fallback_indexes[self.tab] = self.row_index
            self.update_dashboard()

        def action_bottom(self) -> None:
            rows = self.active_rows()
            if self.tab == "runs":
                self.run_index = max(0, len(self.runs) - 1)
                if self.runs:
                    self.selected_run_id = tui.item_key("runs", self.runs[self.run_index], self.run_index)
            else:
                self.row_index = max(0, len(rows) - 1)
                if rows:
                    self.selected_row_ids[self.tab] = tui.item_key(self.tab, rows[self.row_index], self.row_index)
                    self.fallback_indexes[self.tab] = self.row_index
            self.update_dashboard()

        def page_step(self) -> int:
            return max(5, min(12, self.size.height // 4))

        def action_page_down(self) -> None:
            rows = self.active_rows()
            if self.tab == "runs":
                self.run_index = tui.clamp_index(self.run_index + self.page_step(), len(self.runs))
                if self.runs:
                    self.selected_run_id = tui.item_key("runs", self.runs[self.run_index], self.run_index)
            else:
                self.row_index = tui.clamp_index(self.row_index + self.page_step(), len(rows))
                if rows:
                    self.selected_row_ids[self.tab] = tui.item_key(self.tab, rows[self.row_index], self.row_index)
                    self.fallback_indexes[self.tab] = self.row_index
            self.update_dashboard()

        def action_page_up(self) -> None:
            if self.tab == "runs":
                self.run_index = max(0, self.run_index - self.page_step())
                if self.runs:
                    self.selected_run_id = tui.item_key("runs", self.runs[self.run_index], self.run_index)
            else:
                self.row_index = max(0, self.row_index - self.page_step())
                rows = self.active_rows()
                if rows:
                    self.selected_row_ids[self.tab] = tui.item_key(self.tab, rows[self.row_index], self.row_index)
                    self.fallback_indexes[self.tab] = self.row_index
            self.detail_scroll_offset = 0
            self.update_dashboard()

        def action_scroll_detail_down(self) -> None:
            self.detail_scroll_offset += 3
            self.update_dashboard()

        def action_scroll_detail_up(self) -> None:
            self.detail_scroll_offset = max(0, self.detail_scroll_offset - 3)
            self.update_dashboard()

        def on_mouse_scroll_down(self, event: object) -> None:
            self.detail_scroll_offset += 3
            self.update_dashboard()

        def on_mouse_scroll_up(self, event: object) -> None:
            self.detail_scroll_offset = max(0, self.detail_scroll_offset - 3)
            self.update_dashboard()

    WorkflowDashboardApp().run()

#!/usr/bin/env python3
"""Textual/Rich TUI for agent workflow state."""

from __future__ import annotations

import argparse
import codecs
import contextlib
import io
import json
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

import workflow_state
import workflow_health
import workflow_tui_live

TABS = ("overview", "runs", "phases", "agents", "events", "decisions", "artifacts")
AGENT_SCOPES = ("phase", "all")
AGENT_VIEWS = ("live", "prompt")
AGENT_ONLY_ACTIONS = frozenset({"toggle_agent_scope", "toggle_agent_view"})
MIN_WIDTH = 80
MIN_HEIGHT = 12
DISPLAY_TIMESTAMP_WIDTH = 18
COMPACT_TIMESTAMP_WIDTH = 14
TAIL_BYTES = 96_000
MAX_PREVIEW_CHARS = 2_400
MAX_ARTIFACT_PREVIEW_BYTES = 24_000
UPDATE_CHECK_TIMEOUT = 2.0
UPDATE_PULL_TIMEOUT = 30.0
UPDATE_CHECK_INTERVAL = 15 * 60.0
STATUS_META = {
    "pending": ("PEND", "magenta"),
    "running": ("RUN", "cyan"),
    "blocked": ("BLCK", "yellow"),
    "completed": ("DONE", "green"),
    "failed": ("FAIL", "red"),
    "cancelled": ("CNCL", "red"),
    "paused": ("PAUS", "yellow"),
}
TIMESTAMP_KEYS = {"ts", "created_at", "updated_at", "started_at", "completed_at"}
SNAPSHOT_NOW_ENV = "WORKFLOW_TUI_SNAPSHOT_NOW"
EVENT_STYLE_BY_KIND = {
    "workflow initialized": "bright_green",
    "run status": "bright_green",
    "phase added": "bright_cyan",
    "phase updated": "cyan",
    "agent added": "bright_magenta",
    "agent updated": "magenta",
    "decision recorded": "bright_yellow",
    "artifact recorded": "bright_blue",
}


@dataclass(frozen=True)
class GitCommandResult:
    """Bounded git subprocess result for update checks and actions."""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def summary(self) -> str:
        return trim_preview("\n".join(part for part in (self.stderr, self.stdout) if part).strip(), 400)


@dataclass(frozen=True)
class UpdateStatus:
    """Current git-update status for the workflow skill checkout."""

    state: str
    message: str
    local_head: str = ""
    remote_head: str = ""
    upstream: str = ""


@dataclass(frozen=True)
class UpdateActionResult:
    """Result of an attempted git pull update."""

    success: bool
    message: str
    status: UpdateStatus


@dataclass(frozen=True)
class WorkflowControlResult:
    """Result of a pause, resume, or stop action from the live TUI."""

    action: str
    success: bool
    message: str


def skill_repo_root() -> Path:
    """Return the workflow skill checkout that contains this TUI script."""
    return Path(__file__).resolve().parents[1]


def text_from_process(value: Any) -> str:
    """Decode subprocess output from either text or timeout paths."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def run_git_command(repo_root: Path, args: list[str], timeout: float) -> GitCommandResult:
    """Run a bounded git command in the skill checkout."""
    command = ["git", "-C", str(repo_root), *args]
    try:
        result = subprocess.run(command, check=False, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return GitCommandResult(tuple(args), 124, text_from_process(exc.stdout).strip(), text_from_process(exc.stderr).strip(), True)
    except OSError as exc:
        return GitCommandResult(tuple(args), 127, "", str(exc))
    return GitCommandResult(tuple(args), result.returncode, result.stdout.strip(), result.stderr.strip())


def unavailable_update(message: str) -> UpdateStatus:
    """Return a status for checkouts that cannot be checked safely."""
    return UpdateStatus("unavailable", message)


def parse_ls_remote_head(output: str) -> str:
    """Extract the first object id from git ls-remote output."""
    for line in output.splitlines():
        parts = line.split()
        if parts:
            return parts[0]
    return ""


def short_head(value: str) -> str:
    """Return a compact commit id for update notifications."""
    return value[:12] if value else "unknown"


def check_skill_update(repo_root: Path | None = None, timeout: float = UPDATE_CHECK_TIMEOUT) -> UpdateStatus:
    """Check whether the workflow skill checkout differs from its upstream head."""
    root = (repo_root or skill_repo_root()).expanduser().resolve()
    inside = run_git_command(root, ["rev-parse", "--is-inside-work-tree"], timeout)
    if not inside.ok or inside.stdout != "true":
        return unavailable_update("Workflow skill directory is not a git checkout.")

    branch = run_git_command(root, ["branch", "--show-current"], timeout)
    if not branch.ok or not branch.stdout:
        return unavailable_update("Workflow skill checkout is detached; no upstream branch can be checked.")

    remote = run_git_command(root, ["config", f"branch.{branch.stdout}.remote"], timeout)
    merge = run_git_command(root, ["config", f"branch.{branch.stdout}.merge"], timeout)
    if not remote.ok or not remote.stdout or not merge.ok or not merge.stdout:
        return unavailable_update("Workflow skill checkout has no upstream branch configured.")

    local = run_git_command(root, ["rev-parse", "HEAD"], timeout)
    if not local.ok or not local.stdout:
        return unavailable_update("Could not read the local workflow skill HEAD.")

    upstream = f"{remote.stdout}/{merge.stdout.removeprefix('refs/heads/')}"
    remote_head = run_git_command(root, ["ls-remote", "--heads", remote.stdout, merge.stdout], timeout)
    if remote_head.timed_out:
        return UpdateStatus("unavailable", f"Timed out checking {upstream}.", local_head=local.stdout, upstream=upstream)
    if not remote_head.ok:
        detail = remote_head.summary()
        suffix = f": {detail}" if detail else "."
        return UpdateStatus("unavailable", f"Could not check {upstream}{suffix}", local_head=local.stdout, upstream=upstream)

    remote_sha = parse_ls_remote_head(remote_head.stdout)
    if not remote_sha:
        return UpdateStatus("unavailable", f"Could not resolve upstream branch {upstream}.", local_head=local.stdout, upstream=upstream)
    if remote_sha == local.stdout:
        return UpdateStatus("current", f"Workflow skill is current at {short_head(local.stdout)}.", local_head=local.stdout, remote_head=remote_sha, upstream=upstream)
    message = f"Workflow skill update available: {short_head(local.stdout)} -> {short_head(remote_sha)} from {upstream}."
    return UpdateStatus("available", message, local_head=local.stdout, remote_head=remote_sha, upstream=upstream)


def update_skill_from_git(
    repo_root: Path | None = None,
    timeout: float = UPDATE_PULL_TIMEOUT,
    check_timeout: float = UPDATE_CHECK_TIMEOUT,
) -> UpdateActionResult:
    """Run git pull --ff-only in the workflow skill checkout, then re-check status."""
    root = (repo_root or skill_repo_root()).expanduser().resolve()
    pull = run_git_command(root, ["pull", "--ff-only"], timeout)
    status = check_skill_update(root, timeout=check_timeout)
    if not pull.ok:
        detail = pull.summary()
        message = "git pull --ff-only timed out." if pull.timed_out else "git pull --ff-only failed."
        if detail:
            message = f"{message} {detail}"
        return UpdateActionResult(False, message, status)
    if status.state == "current":
        return UpdateActionResult(True, f"Workflow skill updated to {short_head(status.local_head)}.", status)
    return UpdateActionResult(True, f"git pull --ff-only completed. {status.message}", status)


def workflow_control_action(run_id: str, action: str, reason: str = "TUI command palette") -> WorkflowControlResult:
    """Apply a workflow control action through the durable state layer."""
    commands = {
        "pause": workflow_state.cmd_pause,
        "resume": workflow_state.cmd_resume,
        "stop": workflow_state.cmd_stop,
    }
    if action not in commands:
        return WorkflowControlResult(action, False, f"Unknown workflow action: {action}")
    if not run_id:
        return WorkflowControlResult(action, False, "No workflow run is selected.")
    args = argparse.Namespace(run=run_id, reason=reason, terminate=True)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            commands[action](args)
    except (OSError, SystemExit, KeyError, json.JSONDecodeError) as exc:
        return WorkflowControlResult(action, False, f"{action.title()} failed: {exc}")
    verb = {"pause": "paused", "resume": "resumed", "stop": "stopped"}[action]
    return WorkflowControlResult(action, True, f"Workflow {run_id} {verb}.")


def load_runs() -> list[dict[str, Any]]:
    return workflow_state.load_all_runs()


def parse_local_datetime(value: Any) -> datetime | None:
    """Parse a persisted timestamp and convert it to local time."""
    text = "" if value is None else str(value)
    if not text:
        return None
    timestamp = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    return parsed.astimezone()


def display_timestamp(value: Any) -> str:
    """Return a compact local timestamp for TUI display only."""
    parsed = parse_local_datetime(value)
    if parsed is None:
        text = "" if value is None else str(value)
        return text
    return parsed.astimezone().strftime("%b %d %H:%M %Z").strip()


def snapshot_reference_time() -> datetime | None:
    """Return the optional deterministic clock used by snapshot renderers."""
    return parse_local_datetime(os.environ.get(SNAPSHOT_NOW_ENV))


def run_duration_text(run: dict[str, Any], now: datetime | None = None) -> str:
    return workflow_tui_live.run_duration_text(
        run, parse_local_datetime, format_duration_seconds, workflow_state.TERMINAL_STATUS_VALUES, now, snapshot_reference_time
    )


def agent_duration_text(agent: dict[str, Any], now: datetime | None = None) -> str:
    return workflow_tui_live.agent_duration_text(agent, parse_local_datetime, parse_duration_seconds, format_duration_seconds, workflow_state.TERMINAL_STATUS_VALUES, now, snapshot_reference_time)


def reference_epoch() -> float:
    reference = snapshot_reference_time()
    return reference.timestamp() if reference else time.time()


def display_event_timestamp(value: Any, now: datetime | None = None) -> str:
    """Return a compact local event timestamp, adding a date only for older events."""
    parsed = parse_local_datetime(value)
    if parsed is None:
        return "" if value is None else str(value)
    reference = (now or snapshot_reference_time() or datetime.now(parsed.tzinfo)).astimezone(parsed.tzinfo)
    if abs((reference - parsed).total_seconds()) <= 86_400:
        return parsed.strftime("%H:%M:%S %Z").strip()
    return parsed.strftime("%y-%m-%d %H:%M").strip()


def is_duration_seconds_key(key: str | None) -> bool:
    """Return true for metadata fields that carry elapsed seconds."""
    if not key:
        return False
    normalized = str(key).lower().replace("-", "_").replace(" ", "_")
    return normalized.endswith("_seconds")


def parse_duration_seconds(value: Any) -> float | None:
    """Parse a numeric seconds value without treating booleans as durations."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            seconds = float(text)
        except ValueError:
            return None
    else:
        return None
    return seconds if math.isfinite(seconds) else None


def compact_decimal(value: float, places: int) -> str:
    """Format a decimal without scientific notation or useless trailing zeroes."""
    text = f"{value:.{places}f}".rstrip("0").rstrip(".")
    return text if text not in {"", "-0"} else "0"


def format_duration_seconds(value: Any) -> str | None:
    """Render seconds as a readable duration with the smallest useful unit."""
    seconds = parse_duration_seconds(value)
    if seconds is None:
        return None
    magnitude = abs(seconds)
    if magnitude == 0:
        return "<1 us"
    if magnitude < 0.001:
        micros = seconds * 1_000_000
        places = 2 if abs(micros) < 1 else 1 if abs(micros) < 10 else 0
        return f"{compact_decimal(micros, places)} us"
    if magnitude < 1:
        millis = seconds * 1_000
        places = 2 if abs(millis) < 10 else 1 if abs(millis) < 100 else 0
        return f"{compact_decimal(millis, places)} ms"
    if magnitude < 60:
        places = 2 if magnitude < 10 else 1
        return f"{compact_decimal(seconds, places)} s"
    total_seconds = int(round(magnitude))
    minutes, remainder = divmod(total_seconds, 60)
    sign = "-" if seconds < 0 else ""
    if minutes < 60:
        return f"{sign}{minutes}m {remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{sign}{hours}h {minutes:02d}m"


def display_timestamps_in_detail(value: Any, key: str | None = None) -> Any:
    """Copy a detail value with timestamp and duration fields rendered for humans."""
    if isinstance(value, dict):
        return {
            item_key: display_timestamps_in_detail(item_value, item_key)
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [display_timestamps_in_detail(item) for item in value]
    if isinstance(value, str) and key in TIMESTAMP_KEYS:
        return display_timestamp(value)
    if is_duration_seconds_key(key):
        formatted = format_duration_seconds(value)
        if formatted is not None:
            return formatted
    return value


def window_start(selected: int, total: int, visible: int) -> int:
    """Return the first visible row that keeps selected in view."""
    if total <= 0 or visible <= 0:
        return 0
    selected = min(max(0, selected), total - 1)
    if selected < visible:
        return 0
    return min(selected - visible + 1, max(0, total - visible))


def clamp_index(index: int, total: int) -> int:
    if total <= 0:
        return 0
    return min(max(0, index), total - 1)


def load_fixture(path: str) -> list[dict[str, Any]]:
    """Load a snapshot fixture containing either one run or a list of runs."""
    fixture_path = Path(path).expanduser().resolve()
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "runs" in data:
        data = data["runs"]
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise SystemExit("fixture must be a run object, a {runs: [...]} object, or a run array")
    for run in data:
        if isinstance(run, dict):
            run["_fixture_dir"] = str(fixture_path.parent)
    return data


def status_label(status: str) -> str:
    return STATUS_META.get(status, ("UNKN", "white"))[0]


def status_text(status: str) -> Text:
    label, style = STATUS_META.get(status, ("UNKN", "white"))
    return Text(label, style=f"bold {style}")


def marked_status_text(active: bool, status: str) -> Text:
    """Render status with a durable inline selection marker."""
    label, style = STATUS_META.get(status, ("UNKN", "white"))
    text = Text("▸ " if active else "  ", style="bold bright_white")
    text.append(label, style=f"bold {style}")
    return text


def severity_text(severity: str) -> Text:
    """Render an attention severity label."""
    mapping = {
        "critical": ("CRIT", "bold red"),
        "warning": ("WARN", "bold yellow"),
        "info": ("INFO", "cyan"),
    }
    label, style = mapping.get(str(severity), (str(severity or "?")[:4].upper(), "white"))
    return Text(label, style=style)


def path_text(run: dict[str, Any] | None) -> str:
    if not run:
        return ""
    return str(run.get("paths", {}).get("run_json", ""))


def resolve_workflow_path(run: dict[str, Any] | None, value: Any, fallback_dir: str | None = None) -> Path | None:
    """Resolve a persisted workflow path into a copyable filesystem path."""
    if not value:
        return None
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path
    if not run:
        return path
    fixture_dir = run.get("_fixture_dir")
    if fixture_dir:
        return Path(str(fixture_dir)).expanduser() / path
    run_paths = run.get("paths", {})
    run_dir_value = run_paths.get("run_dir")
    run_dir = Path(str(run_dir_value)).expanduser() if run_dir_value else None
    if path.parts and path.parts[0] in {"artifacts", "logs"} and run_dir:
        return run_dir / path
    if fallback_dir:
        fallback_value = run_paths.get(f"{fallback_dir}_dir")
        if fallback_value:
            return Path(str(fallback_value)).expanduser() / path
    if run_dir:
        return run_dir / path
    return path


def marker_text(active: bool) -> Text:
    return Text("▸" if active else " ", style="bold bright_white")


def compact_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ": "))


def json_renderable(value: Any) -> Syntax:
    payload = json.dumps(value, indent=2, sort_keys=True)
    return Syntax(payload, "json", theme="ansi_dark", word_wrap=True)


def action_enabled_for_tab(tab: str, action: str) -> bool:
    """Return whether a live TUI action is meaningful for the active tab."""
    return tab == "agents" or action not in AGENT_ONLY_ACTIONS


def make_header(tab: str) -> Text:
    header = Text()
    hints = ["↑/↓ rows", "←/→ tabs", "y id", "p path"]
    if tab == "agents":
        hints.extend(["a scope", "v view"])
    hints.append("r/q")
    header.append("Agent Workflows", style="bold bright_cyan")
    header.append(f"  {'  '.join(hints)}", style="dim")
    return header


def compact_path(path: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(path) <= width:
        return path
    if width <= 4:
        return "…"[:width]
    return "…" + path[-(width - 1) :]


def display_path_value(path: Path | None, width: int = 42) -> str:
    """Display a resolved path without hiding the basename in narrow panes."""
    return compact_path(str(path), width) if path else ""


def compact_tool_input(value: Any, width: int = 64) -> str:
    """Render a tool input without letting absolute paths dominate panels."""
    if isinstance(value, dict):
        for key in ("command", "description"):
            if value.get(key):
                return str(value[key])
        for key in ("filePath", "path", "pattern", "cwd"):
            if value.get(key):
                return compact_path(str(value[key]), width)
        return compact_path(compact_json(value), width)
    return compact_path(str(value), width)


def make_footer(run: dict[str, Any] | None, width: int) -> Text:
    label = "path: "
    path = compact_path(path_text(run), max(0, width - len(label)))
    return Text(label + path, style="bright_black", overflow="ellipsis", no_wrap=True)


def make_tabs_title(tab: str, compact: bool = False) -> Text:
    labels = {
        "overview": "ovr",
        "runs": "run",
        "phases": "pha",
        "agents": "agt",
        "events": "evt",
        "decisions": "dec",
        "artifacts": "art",
    }
    title = Text()
    for name in TABS:
        if title:
            title.append("  ")
        label = labels[name] if compact else name
        if name == tab:
            title.append(f"● {label}", style="bold bright_white on dark_green")
        else:
            title.append(label, style="bright_black")
    return title


def make_panel_title(tab: str, *, compact: bool = False, filter_text: str = "") -> Text:
    """Return a tab title with persistent filter state when active."""
    title = make_tabs_title(tab, compact=compact)
    normalized = active_filter(filter_text)
    if normalized:
        title.append("  ")
        title.append(f"filter: {normalized}", style="bold yellow")
    return title


def phase_rank(phase: dict[str, Any], fallback_index: int) -> tuple[int, int]:
    """Sort phases by explicit order, otherwise preserve persisted order."""
    explicit_order = phase.get("order")
    if isinstance(explicit_order, int):
        return (explicit_order, fallback_index)
    return (fallback_index, fallback_index)


def ordered_phases(run: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return phases in top-to-bottom workflow order."""
    if not run:
        return []
    phases = list(run.get("phases", []))
    return [phase for _, phase in sorted(enumerate(phases), key=lambda item: phase_rank(item[1], item[0]))]


def selected_phase(run: dict[str, Any] | None, phase_id: str | None = None) -> dict[str, Any] | None:
    """Resolve a selected phase id, falling back to the first ordered phase."""
    phases = ordered_phases(run)
    if not phases:
        return None
    if phase_id:
        index = index_for_key(phases, "phases", phase_id)
        if item_key("phases", phases[index], index) == phase_id:
            return phases[index]
    for phase in phases:
        if phase.get("status") == "running":
            return phase
    return phases[0]


def agent_kind(agent: dict[str, Any]) -> str:
    """Return the compact worker kind shown in agent lists."""
    return str(agent.get("agent_type") or agent.get("runner") or "agent")


def agent_model(agent: dict[str, Any]) -> str:
    """Return the model/provider label shown in agent lists."""
    return str(agent.get("model") or agent.get("provider_model") or "")


def make_mapping_table(rows: list[tuple[str, Any]]) -> Table:
    """Render structured fields as a compact two-column table."""
    table = Table.grid(expand=True)
    table.add_column(width=14, no_wrap=True)
    table.add_column(ratio=1)
    for label, value in rows:
        display_value = display_timestamps_in_detail(value, str(label))
        if isinstance(display_value, Text):
            rendered = display_value
        elif isinstance(display_value, (dict, list)):
            rendered = compact_json(display_value)
        elif display_value is None:
            rendered = ""
        else:
            rendered = str(display_value)
        table.add_row(Text(label, style="bold bright_black"), rendered)
    return table


def infer_event_kind(event: dict[str, Any]) -> str:
    """Return a readable event kind for new and old event records."""
    kind = str(event.get("kind") or "").strip()
    operation = str(event.get("operation") or "").strip()
    if kind and operation:
        return f"{kind} {operation}".replace("_", " ")
    if kind:
        return kind.replace("_", " ")
    message = str(event.get("message", "")).strip().lower()
    patterns = (
        ("workflow initialized", "workflow initialized"),
        ("run status set", "run status"),
        ("phase added", "phase added"),
        ("phase updated", "phase updated"),
        ("agent added", "agent added"),
        ("agent updated", "agent updated"),
        ("decision recorded", "decision recorded"),
        ("artifact recorded", "artifact recorded"),
    )
    for prefix, label in patterns:
        if message.startswith(prefix):
            return label
    if ":" in message:
        return message.split(":", 1)[0][:24]
    words = message.split()
    return " ".join(words[:2])[:24] if words else "event"


def event_kind_text(event: dict[str, Any]) -> Text:
    """Return styled event kind text for the TUI."""
    kind = infer_event_kind(event)
    style = EVENT_STYLE_BY_KIND.get(kind, "bright_black")
    if kind.startswith("agent"):
        style = EVENT_STYLE_BY_KIND.get(kind, "magenta")
    elif kind.startswith("phase"):
        style = EVENT_STYLE_BY_KIND.get(kind, "cyan")
    return Text(kind, style=f"bold {style}")


def make_attention_table(items: list[dict[str, Any]], selected: int, visible: int) -> Table:
    """Render the overview attention list."""
    table = Table(box=box.SIMPLE_HEAD, expand=True, header_style="bold bright_black")
    table.add_column("", width=1, no_wrap=True)
    table.add_column("Sev", width=4, no_wrap=True)
    table.add_column("Kind", width=9, overflow="ellipsis", no_wrap=True)
    table.add_column("Title", ratio=1, overflow="ellipsis", no_wrap=True)
    if not items:
        table.add_row("", "", "", "No attention items")
        return table
    selected = clamp_index(selected, len(items))
    start = window_start(selected, len(items), visible)
    for index, item in enumerate(items[start : start + visible], start=start):
        style = "reverse" if index == selected else ""
        table.add_row(
            marker_text(index == selected),
            severity_text(str(item.get("severity", ""))),
            str(item.get("kind", "")),
            str(item.get("title", "")),
            style=style,
        )
    return table


def make_attention_detail(item: dict[str, Any]) -> Group:
    """Render one attention item with enough context to act on it."""
    rows = [
        ("severity", item.get("severity", "")),
        ("kind", item.get("kind", "")),
        ("run", item.get("run_id", "")),
        ("entity", item.get("entity_id", "")),
        ("phase", item.get("phase_id", "")),
        ("agent", item.get("agent_id", "")),
        ("artifact", item.get("artifact_id", "")),
        ("check", item.get("check_id", "")),
        ("time", display_timestamp(item.get("ts", ""))),
    ]
    facts = Panel(make_mapping_table(rows), title=str(item.get("title", "Attention")), border_style="yellow", box=box.ROUNDED)
    message = Panel(Text(str(item.get("message") or "No details recorded."), overflow="fold"), title="Details", border_style="cyan", box=box.ROUNDED)
    suggestion = str(item.get("suggestion") or "")
    panels: list[Any] = [facts, message]
    if suggestion:
        panels.append(Panel(Text(suggestion, overflow="fold"), title="Next", border_style="green", box=box.ROUNDED))
    return Group(*panels)


def make_runs_table(runs: list[dict[str, Any]], selected: int, visible: int) -> Table:
    table = Table(box=None, expand=True, show_header=True, header_style="bold bright_black", pad_edge=False)
    table.add_column("", width=1, no_wrap=True)
    table.add_column("Workflow", ratio=1, overflow="ellipsis", no_wrap=False)
    if not runs:
        table.add_row("", "No workflow runs found.")
        return table
    selected = clamp_index(selected, len(runs))
    start = window_start(selected, len(runs), visible)
    for index, run in enumerate(runs[start : start + visible], start=start):
        style = "reverse" if index == selected else ""
        status_label = STATUS_META.get(str(run.get("status", "")), (str(run.get("status", "")).upper()[:4], ""))[0]
        duration = run_duration_text(run)
        summary = Text(str(run.get("title", "")) or "(untitled)")
        summary.append("\n  > ", style="dim")
        summary.append(status_label, style=status_text(run.get("status", "")).style)
        if duration:
            summary.append(" > ", style="dim")
            summary.append(duration, style="bright_black")
        running_text = workflow_tui_live.running_agents_inline_text(run)
        if running_text:
            summary.append(" > ", style="dim")
            summary.append(running_text, style="green")
        table.add_row(
            marker_text(index == selected),
            summary,
            style=style,
        )
    return table


def safe_read_tail_info(path_value: str | Path | None, limit: int = TAIL_BYTES) -> tuple[str, bool]:
    """Read a text tail and report whether the read started mid-file."""
    if not path_value:
        return "", False
    path = Path(path_value).expanduser()
    if not path.exists() or not path.is_file():
        return "", False
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - limit)
            handle.seek(start)
            return handle.read().decode("utf-8", errors="replace"), start > 0
    except OSError:
        return "", False


def safe_read_tail(path_value: str | Path | None, limit: int = TAIL_BYTES) -> str:
    """Read the tail of a text file without assuming it is small or stable."""
    text, _ = safe_read_tail_info(path_value, limit)
    return text


def discard_partial_first_jsonl_record(text: str, truncated: bool) -> str:
    """Drop the first record when a JSONL tail starts in the middle of a file."""
    if not truncated:
        return text
    lines = text.splitlines()
    if not lines:
        return text
    return "\n".join(lines[1:])


def trim_preview(text: str, limit: int = MAX_PREVIEW_CHARS) -> str:
    """Keep a live preview readable in the fixed-height TUI."""
    clean = text.strip()
    if len(clean) <= limit:
        return clean
    return "…" + clean[-(limit - 1) :]


def token_value(value: Any) -> int:
    """Return an integer token value when available."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def empty_token_totals() -> dict[str, Any]:
    """Return the provider-usage shape used by live telemetry."""
    return {
        "total": 0,
        "input": 0,
        "cached_input": 0,
        "output": 0,
        "reasoning": 0,
        "known": False,
        "total_source": "unknown",
    }


def source_has_token_fields(source: dict[str, Any]) -> bool:
    """Return true when a provider event carries usage, even if the values are zero."""
    keys = {
        "total",
        "total_tokens",
        "input",
        "input_tokens",
        "prompt_tokens",
        "cached_input",
        "cached_input_tokens",
        "output",
        "output_tokens",
        "completion_tokens",
        "reasoning",
        "reasoning_tokens",
    }
    if any(key in source for key in keys):
        return True
    nested_keys = ("input_tokens_details", "output_tokens_details", "cache_creation_input_tokens", "cache_read_input_tokens")
    return any(key in source for key in nested_keys)


def merge_token_max(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Use max values so cumulative token events are not double counted."""
    if not source_has_token_fields(source):
        return
    target["known"] = True
    aliases = {
        "total": ("total", "total_tokens"),
        "input": ("input", "input_tokens", "prompt_tokens"),
        "cached_input": ("cached_input", "cached_input_tokens", "cache_read_input_tokens"),
        "output": ("output", "output_tokens", "completion_tokens"),
        "reasoning": ("reasoning", "reasoning_tokens"),
    }
    saw_reported_total = False
    for target_key, source_keys in aliases.items():
        for source_key in source_keys:
            if source_key in source:
                target[target_key] = max(target.get(target_key, 0), token_value(source.get(source_key)))
                if target_key == "total":
                    saw_reported_total = True
    input_details = source.get("input_tokens_details")
    if isinstance(input_details, dict):
        target["cached_input"] = max(target.get("cached_input", 0), token_value(input_details.get("cached_tokens")))
    output_details = source.get("output_tokens_details")
    if isinstance(output_details, dict):
        target["reasoning"] = max(target.get("reasoning", 0), token_value(output_details.get("reasoning_tokens")))
    if saw_reported_total:
        target["total_source"] = "reported_total"
        return
    if target.get("total_source") != "reported_total":
        derived = target.get("input", 0) + target.get("output", 0) + target.get("reasoning", 0)
        target["total"] = max(target.get("total", 0), derived)
        target["total_source"] = "derived_from_provider_parts"


def format_token_total(tokens: dict[str, Any]) -> str:
    """Render token totals without pretending missing usage is zero."""
    if not tokens.get("known"):
        return "unknown"
    label = str(tokens.get("total", 0))
    if tokens.get("unknown_agents"):
        label += "+?"
    if tokens.get("total_source") == "derived_from_provider_parts":
        label += " derived"
    return label


def timestamp_epoch(value: Any) -> float:
    """Return a comparable epoch-ish value for timestamps from different CLIs."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000.0 if number > 10_000_000_000 else number
    text = str(value).strip()
    if not text:
        return 0.0
    try:
        number = float(text)
    except ValueError:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return number / 1000.0 if number > 10_000_000_000 else number


def path_mtime(path: Path | None) -> float:
    """Return a file mtime when it is available."""
    if path is None:
        return 0.0
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def json_event_epoch(event: dict[str, Any]) -> float:
    """Extract the best timestamp from known Codex/OpenCode JSON event shapes."""
    candidates: list[Any] = [
        event.get("timestamp"),
        event.get("ts"),
        event.get("created_at"),
        event.get("updated_at"),
    ]
    part = event.get("part") if isinstance(event.get("part"), dict) else {}
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    for source in (part, item):
        candidates.extend([source.get("timestamp"), source.get("ts"), source.get("created_at"), source.get("updated_at")])
    for time_value in (part.get("time"), state.get("time"), item.get("time")):
        if isinstance(time_value, dict):
            candidates.extend([time_value.get("end"), time_value.get("start")])
        else:
            candidates.append(time_value)
    return max(timestamp_epoch(candidate) for candidate in candidates)


def summarize_tool_call(event: dict[str, Any]) -> str:
    """Return a compact tool-call label for known coding-CLI JSON event shapes."""
    part = event.get("part") if isinstance(event.get("part"), dict) else {}
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    source = part or item or event
    name = source.get("tool") or source.get("name") or source.get("type") or "tool"
    status = state.get("status") or source.get("status") or event.get("status") or ""
    title = compact_tool_input(state.get("title") or source.get("title") or "")
    input_value = state.get("input") or source.get("input") or source.get("arguments") or {}
    if source.get("type") == "command_execution":
        input_value = source.get("command") or input_value
        input_text = str(input_value)
    else:
        input_text = compact_tool_input(input_value)
    pieces = [str(name)]
    if status:
        pieces.append(str(status))
    if title:
        pieces.append(str(title))
    elif input_text:
        pieces.append(str(input_text))
    return " · ".join(piece for piece in pieces if piece)[:220]


def tool_event_key(event: dict[str, Any], fallback: int) -> str | None:
    """Return a stable provider-neutral key for a tool event, if it is one."""
    part = event.get("part") if isinstance(event.get("part"), dict) else {}
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    event_type = str(event.get("type", ""))
    part_type = str(part.get("type", ""))
    item_type = str(item.get("type", ""))
    is_tool = (
        event_type in {"tool_use", "tool_call"}
        or part_type == "tool"
        or item_type in {"command_execution", "tool_use", "tool_call"}
        or "tool" in event_type
    )
    if not is_tool:
        return None
    return str(
        item.get("id")
        or part.get("callID")
        or part.get("id")
        or event.get("id")
        or f"tool-{fallback}"
    )


def parse_json_activity(text: str) -> dict[str, Any]:
    """Extract text, tool calls, and token stats from JSONL-ish runner logs."""
    output_parts: list[str] = []
    tool_calls: dict[str, str] = {}
    tool_order: list[str] = []
    todos: list[dict[str, str]] = []
    thinking_parts: list[str] = []
    tokens = empty_token_totals()
    parse_errors = 0
    last_activity_epoch = 0.0
    for fallback, line in enumerate(text.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            parse_errors += 1
            continue
        if not isinstance(event, dict):
            continue
        last_activity_epoch = max(last_activity_epoch, json_event_epoch(event))
        if isinstance(event.get("response"), str):
            output_parts.append(event["response"])
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        if event.get("type") == "text" and isinstance(part.get("text"), str):
            output_parts.append(part["text"])
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            output_parts.append(item["text"])
        key = tool_event_key(event, fallback)
        if key:
            if key not in tool_calls:
                tool_order.append(key)
            tool_calls[key] = summarize_tool_call(event)
        event_todos = workflow_tui_live.todo_items_from_event(event)
        if event_todos:
            todos = event_todos
        thinking_text = workflow_tui_live.thinking_text_from_event(event)
        if thinking_text:
            thinking_parts.append(thinking_text)
        for token_source in (event.get("tokens"), event.get("usage"), part.get("tokens"), part.get("usage"), item.get("tokens"), item.get("usage")):
            if isinstance(token_source, dict):
                merge_token_max(tokens, token_source)
        if isinstance(event.get("token_usage"), dict):
            merge_token_max(tokens, event["token_usage"])
    ordered_tools = [tool_calls[key] for key in tool_order if key in tool_calls]
    return {
        "latest_output": trim_preview("\n\n".join(output_parts)),
        "tool_calls": ordered_tools[-6:],
        "tool_call_count": len(ordered_tools),
        "todos": todos,
        "latest_thinking": trim_preview("\n\n".join(thinking_parts)),
        "tokens": tokens,
        "parse_errors": parse_errors,
        "last_activity_epoch": last_activity_epoch,
    }


def parse_text_activity(text: str) -> dict[str, Any]:
    """Extract lightweight activity from ccc text transcripts and outputs."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    tool_groups: list[list[str]] = []
    current_tool: list[str] | None = None
    saw_result = False
    assistant_lines: list[str] = []
    for line in lines:
        if line.startswith("[assistant]"):
            assistant_lines.append(line.removeprefix("[assistant]").strip())
            current_tool = None
            saw_result = False
            continue
        if line.startswith("[tool:start]"):
            if current_tool is None or saw_result:
                current_tool = []
                tool_groups.append(current_tool)
                saw_result = False
            current_tool.append(line.removeprefix("[tool:start]").strip())
            continue
        if line.startswith("[tool:result]"):
            if current_tool is None:
                current_tool = []
                tool_groups.append(current_tool)
            current_tool.append(line.removeprefix("[tool:result]").strip())
            saw_result = True
            continue
        kimi_match = re.match(r"^•\s+Used\s+(.+)$", line)
        if kimi_match:
            tool_groups.append([kimi_match.group(1).strip()])
            current_tool = None
            saw_result = False
    tool_lines = [" ".join(group)[:220] for group in tool_groups]
    output_source = assistant_lines or lines[-24:]
    return {
        "latest_output": trim_preview("\n".join(output_source)),
        "tool_calls": tool_lines[-6:],
        "tool_call_count": len(tool_groups),
        "todos": [],
        "latest_thinking": "",
        "tokens": empty_token_totals(),
        "parse_errors": 0,
        "last_activity_epoch": 0.0,
    }


def should_parse_json_activity(text: str, path: Path | None = None) -> bool:
    """Return true when a log tail should be treated as JSONL."""
    json_lines = 0
    other_lines = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("{"):
            other_lines += 1
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            other_lines += 1
            continue
        if isinstance(event, dict) and (event.get("type") or "item" in event or "part" in event):
            json_lines += 1
        else:
            other_lines += 1
    if path and path.suffix.lower() == ".jsonl":
        return json_lines > 0
    return json_lines > 0 and json_lines >= other_lines


def resolve_agent_path(agent: dict[str, Any], key: str, run: dict[str, Any] | None = None) -> Path | None:
    """Resolve absolute and fixture-relative agent paths without touching state."""
    fallback_dir = "logs" if key in {"jsonl_path", "log_path"} else "artifacts" if key == "output_path" else None
    return resolve_workflow_path(run, agent.get(key), fallback_dir)


def resolve_artifact_path(artifact: dict[str, Any], run: dict[str, Any] | None = None) -> Path | None:
    """Resolve an artifact path for detail rendering and copy commands."""
    return resolve_workflow_path(run, artifact.get("path"), "artifacts")


def is_binary_text(text: str) -> bool:
    """Return true when decoded text contains binary-looking control data."""
    allowed_controls = {"\t", "\n", "\r"}
    return any((ord(char) < 32 and char not in allowed_controls) or char == "\ufffd" for char in text)


def read_text_artifact_preview(path: Path | None, limit: int = MAX_ARTIFACT_PREVIEW_BYTES) -> str:
    """Return a bounded preview for files that are readable UTF-8 text."""
    if not path or not path.exists() or not path.is_file():
        return ""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError:
        return ""
    if not data:
        return ""
    truncated = size > limit
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        body = decoder.decode(data[:limit], final=not truncated).rstrip()
    except UnicodeDecodeError:
        return ""
    if is_binary_text(body):
        return ""
    if not body:
        return ""
    if size > limit:
        body = f"{body}\n... truncated after {limit} bytes ..."
    return body


def parse_ccc_output_log(stderr_text: str) -> Path | None:
    """Return ccc's artifact directory from its stderr footer, when present."""
    for line in reversed(stderr_text.splitlines()):
        marker = ">> ccc:output-log >>"
        if marker in line:
            value = line.split(marker, 1)[1].strip()
            return Path(value).expanduser() if value else None
    return None


def agent_activity(agent: dict[str, Any], run: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return best-effort live activity for an agent from its durable logs."""
    transcript_path = resolve_agent_path(agent, "jsonl_path", run)
    output_path = resolve_agent_path(agent, "output_path", run)
    stderr_text = safe_read_tail(resolve_agent_path(agent, "log_path", run))
    ccc_dir = parse_ccc_output_log(stderr_text)
    if ccc_dir:
        ccc_transcript_path = ccc_dir / "transcript.jsonl"
        if not ccc_transcript_path.exists():
            ccc_transcript_path = ccc_dir / "transcript.txt"
        ccc_output_path = ccc_dir / "output.txt"
        if ccc_transcript_path.exists():
            transcript_path = ccc_transcript_path
        if ccc_output_path.exists():
            output_path = ccc_output_path
    transcript_text, transcript_truncated = safe_read_tail_info(transcript_path)
    if transcript_path and transcript_path.suffix.lower() == ".jsonl":
        transcript_text = discard_partial_first_jsonl_record(transcript_text, transcript_truncated)
    output_text = safe_read_tail(output_path)
    if should_parse_json_activity(transcript_text, transcript_path):
        activity = parse_json_activity(transcript_text)
    else:
        activity = parse_text_activity(transcript_text)
    if output_text.strip():
        activity["latest_output"] = trim_preview(output_text)
    fallback_epoch = max(
        timestamp_epoch(agent.get("updated_at")),
        timestamp_epoch(agent.get("completed_at")),
        timestamp_epoch(agent.get("started_at")),
        path_mtime(transcript_path),
        path_mtime(output_path),
    )
    activity["agent_id"] = agent.get("agent_id", "")
    activity["name"] = agent.get("name", "")
    activity["status"] = agent.get("status", "")
    activity["last_activity_epoch"] = max(float(activity.get("last_activity_epoch") or 0.0), fallback_epoch)
    activity["transcript_path"] = str(transcript_path or "")
    activity["output_path"] = str(output_path or "")
    return activity


def activity_sort_key(activity: dict[str, Any]) -> tuple[float, int, str]:
    """Sort live activity by recency with active workers winning timestamp ties."""
    status_rank = {"running": 3, "pending": 2, "paused": 1}.get(str(activity.get("status", "")), 0)
    return (float(activity.get("last_activity_epoch") or 0.0), status_rank, str(activity.get("agent_id", "")))


def collect_run_activity(run: dict[str, Any]) -> dict[str, Any]:
    """Aggregate live telemetry for a run from all agent logs."""
    activities = [agent_activity(agent, run) for agent in run.get("agents", [])]
    ordered_activities = sorted(activities, key=activity_sort_key)
    token_totals = empty_token_totals()
    known_agents = 0
    unknown_agents = 0
    for activity in activities:
        activity_tokens = activity.get("tokens", {})
        if activity_tokens.get("known"):
            known_agents += 1
            token_totals["known"] = True
            for key in ("total", "input", "cached_input", "output", "reasoning"):
                token_totals[key] += token_value(activity_tokens.get(key))
            if token_totals.get("total_source") != "reported_total":
                token_totals["total_source"] = activity_tokens.get("total_source", "unknown")
            if activity_tokens.get("total_source") == "reported_total":
                token_totals["total_source"] = "reported_total"
        else:
            unknown_agents += 1
    token_totals["known_agents"] = known_agents
    token_totals["unknown_agents"] = unknown_agents
    longest_running = None
    running_summaries = workflow_tui_live.running_agent_summaries(run, reference_epoch())
    for agent in running_summaries:
        elapsed = agent.get("elapsed_seconds")
        if elapsed is not None and (longest_running is None or elapsed > longest_running["elapsed_seconds"]):
            longest_running = agent
    longest_completed = None
    for agent in run.get("agents", []):
        duration = parse_duration_seconds(agent.get("duration_seconds"))
        if duration is None:
            continue
        if longest_completed is None or duration > longest_completed["duration_seconds"]:
            longest_completed = {"name": agent.get("name", ""), "agent_id": agent.get("agent_id", ""), "duration_seconds": duration}
    return {
        "activities": activities,
        "tokens": token_totals,
        "tool_call_count": sum(token_value(activity.get("tool_call_count")) for activity in activities),
        "latest_tool_calls": [call for activity in ordered_activities for call in activity.get("tool_calls", [])][-8:],
        "latest_output": next((activity["latest_output"] for activity in reversed(ordered_activities) if activity.get("latest_output")), ""),
        "latest_todos": next((activity["todos"] for activity in reversed(ordered_activities) if activity.get("todos")), []),
        "latest_thinking": next((activity["latest_thinking"] for activity in reversed(ordered_activities) if activity.get("latest_thinking")), ""),
        "longest_running": longest_running,
        "running_agents": running_summaries,
        "longest_completed": longest_completed,
    }


def longest_agent_label(live: dict[str, Any]) -> str:
    """Return a compact label for the run stats longest-agent field."""
    longest_running = live.get("longest_running") or {}
    if longest_running:
        elapsed = format_duration_seconds(longest_running.get("elapsed_seconds")) or ""
        return " ".join(part for part in (str(longest_running.get("name", "")), elapsed) if part)
    longest_completed = live.get("longest_completed") or {}
    if longest_completed:
        duration = format_duration_seconds(longest_completed.get("duration_seconds")) or ""
        return " ".join(part for part in (str(longest_completed.get("name", "")), duration) if part)
    return ""


def copy_value_for_selection(
    run: dict[str, Any] | None,
    tab: str,
    rows: list[dict[str, Any]],
    selected: int,
    mode: str,
) -> tuple[str, str]:
    """Return the label and value copied by the live TUI for the current row."""
    if not rows:
        return ("", "")
    item = rows[clamp_index(selected, len(rows))]
    id_labels = {
        "overview": "attention_id",
        "runs": "run_id",
        "phases": "phase_id",
        "agents": "agent_id",
        "events": "event_id",
        "decisions": "decision_id",
        "artifacts": "artifact_id",
    }
    if mode == "id":
        key = id_labels.get(tab, "id")
        return (key, str(item.get(key) or item.get("id") or ""))
    if mode == "path":
        if tab == "overview":
            run_id = str(item.get("run_id") or "")
            return ("state path", path_text({"paths": {"run_json": str(workflow_state.run_file(run_id))}}) if run_id else "")
        if tab == "runs":
            return ("state path", path_text(item))
        if tab == "agents":
            for key in ("output_path", "jsonl_path", "log_path"):
                resolved = resolve_agent_path(item, key, run)
                if resolved and resolved.exists():
                    return ("agent path", str(resolved))
            return ("agent path", "")
        if tab == "artifacts":
            resolved = resolve_artifact_path(item, run)
            return ("artifact path", str(resolved) if resolved else "")
        return ("state path", path_text(run))
    if mode == "json":
        return (f"{tab[:-1] if tab.endswith('s') else tab} json", json.dumps(item, indent=2, sort_keys=True))
    raise ValueError(f"unknown copy mode {mode!r}")


def make_facts_table(rows: list[tuple[str, Any]]) -> Table:
    table = Table.grid(expand=True)
    table.add_column(width=12, no_wrap=True)
    table.add_column(ratio=1)
    for label, value in rows:
        table.add_row(Text(label, style="bold bright_black"), "" if value is None else str(value))
    return table


def make_facts_grid(rows: list[tuple[str, Any]], columns: int = 3) -> Table:
    """Render short facts across the available width."""
    table = Table.grid(expand=True)
    for _ in range(columns):
        table.add_column(width=12, no_wrap=True)
        table.add_column(ratio=1)
    for offset in range(0, len(rows), columns):
        cells: list[Any] = []
        for cell_index, (label, value) in enumerate(rows[offset : offset + columns]):
            label_text = f" {label}" if cell_index else label
            cells.append(Text(label_text, style="bold bright_black"))
            cells.append(Text("" if value is None else str(value), overflow="ellipsis", no_wrap=True))
        while len(cells) < columns * 2:
            cells.extend(["", ""])
        table.add_row(*cells)
    return table


def make_run_detail(run: dict[str, Any], *, detail_height: int | None = None) -> Group:
    live = collect_run_activity(run)
    metrics = run.get("metrics", {})
    control = run.get("control") or {}
    facts = make_facts_table(
        [
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
    )
    prompt = Panel(
        Text(str(run.get("prompt", "")), overflow="fold"),
        title="Prompt",
        border_style="blue",
        box=box.ROUNDED,
    )
    live_rows = [
        ("tokens", format_token_total(live.get("tokens", {}))),
        ("tail tools", live.get("tool_call_count", 0)),
        ("active", len([agent for agent in run.get("agents", []) if agent.get("status") == "running"])),
        ("longest", longest_agent_label(live)),
        ("agents", metrics.get("agents_total", len(run.get("agents", [])))),
        ("phases", metrics.get("phases_total", len(run.get("phases", [])))),
        ("checks", metrics.get("checks_total", len(run.get("checks", [])))),
    ]
    live_stats = Panel(make_facts_grid(live_rows), title="Live Stats", border_style="magenta", box=box.ROUNDED)
    tool_text = "\n".join(live.get("latest_tool_calls", [])[-8:]) or "No tool calls recorded yet."
    latest = Panel(Text(live.get("latest_output") or "No live output yet.", overflow="fold"), title="Live Output", border_style="yellow", box=box.ROUNDED)
    panels: list[Any] = [facts, live_stats, latest]
    if detail_height is None or detail_height >= 28:
        running_text = workflow_tui_live.running_agents_text(live, format_duration_seconds)
        if running_text:
            panels.append(Panel(Text(running_text, overflow="fold"), title="Running Agents", border_style="green", box=box.ROUNDED))
        if live.get("latest_tool_calls"):
            panels.append(Panel(Text(tool_text, overflow="fold"), title="Latest Tool Calls", border_style="cyan", box=box.ROUNDED))
        if live.get("latest_todos"):
            panels.append(Panel(Text(workflow_tui_live.todo_status_text(live["latest_todos"]), overflow="fold"), title="Todos", border_style="blue", box=box.ROUNDED))
        if live.get("latest_thinking"):
            panels.append(Panel(Text(live["latest_thinking"], overflow="fold"), title="Latest Thinking", border_style="bright_black", box=box.ROUNDED))
    if detail_height is None or detail_height >= 26:
        panels.append(prompt)
    return Group(*panels)


def make_phase_table(phases: list[dict[str, Any]], selected: int, visible: int) -> Table:
    table = Table(box=box.SIMPLE_HEAD, expand=True, header_style="bold bright_black")
    table.add_column("", width=1, no_wrap=True)
    table.add_column("State", width=5, no_wrap=True)
    table.add_column("Name", ratio=1, overflow="ellipsis", no_wrap=True)
    if not phases:
        table.add_row("", "", "No phases")
        return table
    selected = clamp_index(selected, len(phases))
    start = window_start(selected, len(phases), visible)
    for index, phase in enumerate(phases[start : start + visible], start=start):
        style = "reverse" if index == selected else ""
        table.add_row(
            marker_text(index == selected),
            status_text(phase.get("status", "")),
            str(phase.get("name", "")),
            style=style,
        )
    return table


def make_agent_table(agents: list[dict[str, Any]], selected: int, visible: int, empty_message: str = "No agents") -> Table:
    table = Table(box=box.SIMPLE_HEAD, expand=True, header_style="bold bright_black")
    table.add_column("State", width=7, no_wrap=True)
    table.add_column("Time", width=8, no_wrap=True)
    table.add_column("Name", ratio=1, overflow="ellipsis", no_wrap=True)
    if not agents:
        table.add_row("", "", empty_message)
        return table
    selected = clamp_index(selected, len(agents))
    start = window_start(selected, len(agents), visible)
    for index, agent in enumerate(agents[start : start + visible], start=start):
        style = "reverse" if index == selected else ""
        table.add_row(
            marked_status_text(index == selected, agent.get("status", "")),
            Text(agent_duration_text(agent), style="bright_black"),
            str(agent.get("name", "")),
            style=style,
        )
    return table


def make_agent_activity_detail(agent: dict[str, Any], run: dict[str, Any] | None = None, agent_view: str = "live") -> Group:
    activity = agent_activity(agent, run)
    duration_label = "duration" if str(agent.get("status", "")) in workflow_state.TERMINAL_STATUS_VALUES else "elapsed"
    duration_text = agent_duration_text(agent)
    process_id = agent.get("process_id")
    process_group_id = agent.get("process_group_id")
    native_id = agent.get("native_id")
    process_label = "pgid" if process_group_id else "pid"
    process_display = str(process_group_id or process_id or "") if process_id or process_group_id else ""
    stats_rows: list[tuple[str, Any]] = [
        ("tokens", format_token_total(activity.get("tokens", {}))),
        ("tail tools", activity.get("tool_call_count", 0)),
        ("parse errs", activity.get("parse_errors", 0)),
        (duration_label, duration_text),
        ("status", agent.get("status", "")),
        ("thread", agent.get("thread_id", "")),
    ]
    if process_display:
        stats_rows.append((process_label, process_display))
    if native_id:
        stats_rows.append(("native_id", native_id))
    stats = make_facts_grid(stats_rows)
    tool_text = "\n".join(activity.get("tool_calls", [])[-6:]) or "No tool calls recorded yet."
    output_text = activity.get("latest_output") or agent.get("summary") or "No live output yet."
    info_rows = [
        ("agent_id", agent.get("agent_id", "")),
        ("name", agent.get("name", "")),
        ("role", agent.get("role", "")),
        ("type", agent_kind(agent)),
        ("model", agent_model(agent)),
        ("phase", agent.get("phase_id", "")),
    ]
    body = (
        Panel(Text(str(agent.get("prompt", "")) or "No prompt recorded.", overflow="fold"), title="Prompt", border_style="yellow", box=box.ROUNDED)
        if agent_view == "prompt"
        else Panel(Text(str(output_text), overflow="fold"), title="Live Output", border_style="yellow", box=box.ROUNDED)
    )
    panels: list[Any] = [
        Panel(make_mapping_table(info_rows), title="Agent", border_style="blue", box=box.ROUNDED),
        Panel(stats, title="Live Stats", border_style="magenta", box=box.ROUNDED),
    ]
    if activity.get("tool_calls"):
        panels.append(Panel(Text(tool_text, overflow="fold"), title="Latest Tool Calls", border_style="cyan", box=box.ROUNDED))
    if activity.get("todos"):
        panels.append(Panel(Text(workflow_tui_live.todo_status_text(activity["todos"]), overflow="fold"), title="Todos", border_style="blue", box=box.ROUNDED))
    if activity.get("latest_thinking"):
        panels.append(Panel(Text(activity["latest_thinking"], overflow="fold"), title="Latest Thinking", border_style="bright_black", box=box.ROUNDED))
    panels.append(body)
    return Group(*panels)


def make_events_table(events: list[dict[str, Any]], selected: int, visible: int) -> Table:
    table = Table(box=box.SIMPLE_HEAD, expand=True, header_style="bold bright_black")
    table.add_column("Time", width=COMPACT_TIMESTAMP_WIDTH + 2, no_wrap=True)
    table.add_column("Type", ratio=1, overflow="ellipsis", no_wrap=True)
    if not events:
        table.add_row("", "No events.")
        return table
    selected = clamp_index(selected, len(events))
    start = window_start(selected, len(events), visible)
    for index, event in enumerate(events[start : start + visible], start=start):
        style = "reverse" if index == selected else ""
        table.add_row(
            Text(("▸ " if index == selected else "  ") + display_event_timestamp(event.get("ts", ""))),
            event_kind_text(event),
            style=style,
        )
    return table


def collection_label(item: dict[str, Any], index: int) -> str:
    return str(item.get("title") or item.get("decision_id") or item.get("artifact_id") or item.get("kind") or f"row-{index}")


def empty_sidebar_table(message: str) -> Table:
    """Render an empty sidebar without headers that squeeze the message."""
    table = Table.grid(expand=True)
    table.add_column(ratio=1)
    table.add_row(Text(message, style="dim"))
    return table


def make_collection_table(rows: list[dict[str, Any]], selected: int, visible: int, tab: str) -> Table:
    table = Table(box=box.SIMPLE_HEAD, expand=True, header_style="bold bright_black")
    table.add_column("", width=1)
    table.add_column("Summary", ratio=1, overflow="ellipsis", no_wrap=True)
    if not rows:
        return empty_sidebar_table(f"No {tab}.")
    selected = clamp_index(selected, len(rows))
    start = window_start(selected, len(rows), visible)
    for index, item in enumerate(rows[start : start + visible], start=start):
        style = "reverse" if index == selected else ""
        table.add_row(
            marker_text(index == selected),
            collection_label(item, index),
            style=style,
        )
    return table


def phase_agents(run: dict[str, Any], phase: dict[str, Any]) -> list[dict[str, Any]]:
    agent_ids = set(phase.get("agent_ids", []))
    return [agent for agent in run.get("agents", []) if agent.get("agent_id") in agent_ids or agent.get("phase_id") == phase.get("phase_id")]


def make_phase_detail(run: dict[str, Any], phase: dict[str, Any]) -> Group:
    agents = phase_agents(run, phase)
    completed = sum(1 for agent in agents if agent.get("status") == "completed")
    stats = make_facts_table(
        [
            ("phase", phase.get("phase_id", "")),
            ("status", phase.get("status", "")),
            ("agents", f"{completed}/{len(agents)} done" if agents else "0"),
            ("started", display_timestamp(phase.get("started_at", ""))),
            ("done", display_timestamp(phase.get("completed_at", ""))),
        ]
    )
    return Group(
        Panel(stats, title=str(phase.get("name", "Phase")), border_style="blue", box=box.ROUNDED),
        Panel(Text(str(phase.get("goal", "")), overflow="fold"), title="Goal", border_style="green", box=box.ROUNDED),
        make_agent_table(agents, 0, 8, "No agents for this phase"),
    )


def make_event_detail(event: dict[str, Any]) -> Group:
    """Render a selected event without exposing raw JSON as the primary view."""
    rows = [
        ("event_id", event.get("event_id", "")),
        ("type", infer_event_kind(event)),
        ("level", event.get("level", "")),
        ("time", display_event_timestamp(event.get("ts", ""))),
        ("phase", event.get("phase_id", "")),
        ("agent", event.get("agent_id", "")),
        ("source", event.get("source", "")),
    ]
    detail = [Panel(make_mapping_table(rows), title="Event", border_style="blue", box=box.ROUNDED)]
    detail.append(Panel(Text(str(event.get("message", "")), overflow="fold"), title="Message", border_style="green", box=box.ROUNDED))
    if event.get("data"):
        detail.append(Panel(make_mapping_table(list(event["data"].items())), title="Data", border_style="magenta", box=box.ROUNDED))
    return Group(*detail)


def make_decision_detail(decision: dict[str, Any]) -> Group:
    """Render a selected decision as labeled fields and rationale."""
    rows = [
        ("decision_id", decision.get("decision_id", "")),
        ("title", decision.get("title", "")),
        ("made_by", decision.get("made_by", "")),
        ("time", display_event_timestamp(decision.get("ts", ""))),
    ]
    return Group(
        Panel(make_mapping_table(rows), title="Decision", border_style="blue", box=box.ROUNDED),
        Panel(Text(str(decision.get("rationale", "")), overflow="fold"), title="Rationale", border_style="green", box=box.ROUNDED),
    )


def make_artifact_detail(artifact: dict[str, Any], run: dict[str, Any] | None = None) -> Group:
    """Render a selected artifact as labeled fields."""
    resolved_path = resolve_artifact_path(artifact, run)
    rows = [
        ("artifact_id", artifact.get("artifact_id", "")),
        ("title", artifact.get("title", "")),
        ("kind", artifact.get("kind", "")),
        ("path", display_path_value(resolved_path)),
        ("phase", artifact.get("phase_id", "")),
        ("agent", artifact.get("agent_id", "")),
        ("time", display_event_timestamp(artifact.get("ts", ""))),
    ]
    panels: list[Any] = [Panel(make_mapping_table(rows), title="Artifact", border_style="blue", box=box.ROUNDED)]
    preview = read_text_artifact_preview(resolved_path)
    if preview:
        panels.append(
            Panel(
                Text(preview, overflow="fold"),
                title="Artifact Preview",
                border_style="green",
                box=box.ROUNDED,
            )
        )
    return Group(*panels)


def selected_detail(rows: list[dict[str, Any]], selected: int, title: str) -> Panel:
    if not rows:
        return Panel(Text("No row selected.", style="dim"), title=title, border_style="bright_black", box=box.ROUNDED)
    selected = clamp_index(selected, len(rows))
    return Panel(
        json_renderable(display_timestamps_in_detail(rows[selected])),
        title=title,
        border_style="bright_black",
        box=box.ROUNDED,
    )


def rows_for_tab(
    run: dict[str, Any] | None,
    tab: str,
    runs: list[dict[str, Any]],
    *,
    selected_phase_id: str | None = None,
    agent_scope: str = "phase",
) -> list[dict[str, Any]]:
    if tab == "overview":
        return workflow_health.attention_items(runs)
    if tab == "runs":
        return runs
    if not run:
        return []
    if tab == "phases":
        return ordered_phases(run)
    if tab == "agents":
        if agent_scope == "all":
            return run.get("agents", [])
        phase = selected_phase(run, selected_phase_id)
        if not phase:
            return []
        return phase_agents(run, phase)
    if tab == "events":
        return sorted(run.get("events", []), key=lambda item: str(item.get("ts", "")), reverse=True)
    if tab == "decisions":
        return sorted(run.get("decisions", []), key=lambda item: str(item.get("ts", "")), reverse=True)
    if tab == "artifacts":
        return sorted(run.get("artifacts", []), key=lambda item: str(item.get("ts", "")), reverse=True)
    return []


def item_key(tab: str, item: dict[str, Any], index: int) -> str:
    keys = {
        "overview": "attention_id",
        "runs": "run_id",
        "phases": "phase_id",
        "agents": "agent_id",
        "events": "event_id",
        "decisions": "decision_id",
        "artifacts": "artifact_id",
    }
    key_name = keys.get(tab, "")
    return str(item.get(key_name) or item.get("id") or index)


def index_for_key(rows: list[dict[str, Any]], tab: str, key: str | None) -> int:
    if not key:
        return 0
    for index, item in enumerate(rows):
        if item_key(tab, item, index) == key:
            return index
    return 0


def row_matches_filter(row: dict[str, Any], filter_text: str) -> bool:
    """Return whether a row contains all filter words in common display fields."""
    query = filter_text.strip().lower()
    if not query:
        return True
    haystack_parts: list[str] = []
    for key, value in row.items():
        if key.endswith("_path") or key in {"prompt", "result", "suggestion"}:
            continue
        if isinstance(value, (str, int, float, bool)):
            haystack_parts.append(str(value))
        elif isinstance(value, list):
            haystack_parts.extend(str(item) for item in value if isinstance(item, (str, int, float, bool)))
    haystack = " ".join(haystack_parts).lower()
    return all(part in haystack for part in query.split())


def apply_row_filter(rows: list[dict[str, Any]], filter_text: str) -> list[dict[str, Any]]:
    """Filter rows using the shared text matcher."""
    if not filter_text.strip():
        return rows
    return [row for row in rows if row_matches_filter(row, filter_text)]


def active_filter(filter_text: str) -> str:
    """Return the normalized active filter label."""
    return " ".join(filter_text.strip().split())


def filter_empty_message(filter_text: str) -> str:
    """Return a consistent empty state for filtered views."""
    return f"No rows match filter: {active_filter(filter_text)}"


def make_sidebar(tab: str, rows: list[dict[str, Any]], selected: int, visible: int, *, filter_text: str = "") -> Any:
    if not rows and active_filter(filter_text):
        return Text(filter_empty_message(filter_text), style="dim")
    if tab == "overview":
        return make_attention_table(rows, selected, visible)
    if tab == "runs":
        return make_runs_table(rows, selected, visible)
    if tab == "phases":
        return make_phase_table(rows, selected, visible)
    if tab == "agents":
        return make_agent_table(rows, selected, visible)
    if tab == "events":
        return make_events_table(rows, selected, visible)
    return make_collection_table(rows, selected, visible, tab)


def empty_detail_message(tab: str, agent_scope: str) -> str:
    """Return a context-aware empty detail message."""
    if tab == "agents" and agent_scope == "phase":
        return "No agents for this phase."
    return f"No {tab} for this run."


def make_detail_body(
    run: dict[str, Any] | None,
    tab: str,
    rows: list[dict[str, Any]],
    selected: int,
    *,
    detail_height: int | None = None,
    agent_view: str = "live",
    agent_scope: str = "phase",
    filter_text: str = "",
) -> Any:
    if not rows and active_filter(filter_text):
        return Text(filter_empty_message(filter_text), style="dim")
    if tab == "overview":
        if not rows:
            return Text("No attention items.", style="dim")
        return make_attention_detail(rows[clamp_index(selected, len(rows))])
    if tab == "runs":
        if not rows:
            return Text("No run selected.", style="dim")
        return make_run_detail(rows[clamp_index(selected, len(rows))], detail_height=detail_height)
    if not run:
        return Text("No run selected.", style="dim")
    if not rows:
        return Text(empty_detail_message(tab, agent_scope), style="dim")
    item = rows[clamp_index(selected, len(rows))]
    if tab == "phases":
        return make_phase_detail(run, item)
    if tab == "agents":
        return make_agent_activity_detail(item, run, agent_view)
    if tab == "events":
        return make_event_detail(item)
    if tab == "decisions":
        return make_decision_detail(item)
    if tab == "artifacts":
        return make_artifact_detail(item, run)
    title = "Selected Row"
    return selected_detail(rows, selected, title)


def sidebar_title_for(tab: str, run: dict[str, Any] | None, selected_phase_id: str | None, agent_scope: str) -> str:
    """Return the contextual sidebar title."""
    if tab != "agents" or agent_scope == "all":
        if tab == "overview":
            return "Attention"
        return tab.capitalize()
    phase = selected_phase(run, selected_phase_id)
    if not phase:
        return "Agents"
    return f"Agents: {phase.get('name', phase.get('phase_id', 'Phase'))}"


def title_with_filter(title: str, filter_text: str) -> str:
    """Append the active filter to a panel title."""
    normalized = active_filter(filter_text)
    return f"{title} filter: {normalized}" if normalized else title


def render_dashboard(
    runs: list[dict[str, Any]],
    *,
    width: int,
    height: int,
    tab: str,
    run_index: int,
    row_index: int,
    chrome: bool,
    selected_phase_id: str | None = None,
    agent_scope: str = "phase",
    agent_view: str = "live",
    filter_text: str = "",
    focus: bool = False,
) -> Any:
    if tab not in TABS:
        raise SystemExit(f"invalid tab {tab!r}; expected one of {TABS}")
    min_width = MIN_WIDTH if chrome else MIN_WIDTH - 2
    min_height = MIN_HEIGHT if chrome else MIN_HEIGHT - 3
    if height < min_height or width < min_width:
        return Text(f"terminal too small; need at least {MIN_WIDTH}x{MIN_HEIGHT}", style="bold red")

    selected_run_index = clamp_index(run_index, len(runs))
    base_run = runs[selected_run_index] if runs else None
    pane_height = height - 2 if chrome else height
    left_width = max(40, min(46, (width * 2) // 5))
    right_width = max(20, width - left_width)
    visible = max(1, pane_height - 5)
    rows = apply_row_filter(rows_for_tab(base_run, tab, runs, selected_phase_id=selected_phase_id, agent_scope=agent_scope), filter_text)
    if tab == "runs" and not active_filter(filter_text):
        selected_row_index = selected_run_index
    else:
        selected_row_index = clamp_index(row_index, len(rows))
    run = rows[selected_row_index] if tab == "runs" and rows else base_run
    sidebar_title = title_with_filter(sidebar_title_for(tab, run, selected_phase_id, agent_scope), filter_text)
    detail = make_detail_body(
        run,
        tab,
        rows,
        selected_row_index,
        detail_height=pane_height,
        agent_view=agent_view,
        agent_scope=agent_scope,
        filter_text=filter_text,
    )
    if focus:
        focused = Panel(
            detail,
            title=make_panel_title(tab, compact=width < 100, filter_text=filter_text),
            border_style="green",
            box=box.ROUNDED,
            height=pane_height,
        )
        if not chrome:
            return focused
        return Group(make_header(tab), focused, make_footer(run, width))

    layout = Table.grid(expand=True)
    layout.add_column(width=left_width)
    layout.add_column(width=right_width)
    layout.add_row(
        Panel(
            make_sidebar(tab, rows, selected_row_index, visible, filter_text=filter_text),
            title=Text(sidebar_title, style="bold cyan"),
            border_style="cyan",
            box=box.ROUNDED,
            height=pane_height,
        ),
        Panel(
            detail,
            title=make_panel_title(tab, compact=right_width < 72, filter_text=""),
            border_style="green",
            box=box.ROUNDED,
            height=pane_height,
        ),
    )
    if not chrome:
        return layout
    return Group(make_header(tab), layout, make_footer(run, width))


def normalize_snapshot(text: str, width: int, height: int) -> str:
    lines = text.rstrip("\n").splitlines()
    if len(lines) < height:
        lines.extend([""] * (height - len(lines)))
    lines = lines[:height]
    return "\n".join(line[:width] for line in lines) + "\n"


def render_snapshot(
    runs: list[dict[str, Any]],
    width: int = 100,
    height: int = 28,
    tab: str = "runs",
    run_index: int = 0,
    row_index: int = 0,
    scroll: int = 0,
    phase_id: str | None = None,
    phase_index: int | None = None,
    agent_scope: str = "phase",
    agent_view: str = "live",
    filter_text: str = "",
    focus: bool = False,
) -> str:
    """Render the TUI as deterministic text for snapshot tests."""
    if height < MIN_HEIGHT or width < MIN_WIDTH:
        return f"terminal too small; need at least {MIN_WIDTH}x{MIN_HEIGHT}\n"
    effective_row_index = row_index + max(0, scroll)
    selected_phase_id = phase_id
    if selected_phase_id is None and phase_index is not None and runs:
        run = runs[clamp_index(run_index, len(runs))]
        phases = ordered_phases(run)
        if phases:
            selected_phase_id = item_key("phases", phases[clamp_index(phase_index, len(phases))], clamp_index(phase_index, len(phases)))
    sink = io.StringIO()
    console = Console(width=width, color_system=None, force_terminal=False, legacy_windows=False, record=True, file=sink)
    console.print(
        render_dashboard(
            runs,
            width=width,
            height=height,
            tab=tab,
            run_index=run_index,
            row_index=effective_row_index,
            chrome=True,
            selected_phase_id=selected_phase_id,
            agent_scope=agent_scope,
            agent_view=agent_view,
            filter_text=filter_text,
            focus=focus,
        )
    )
    return normalize_snapshot(console.export_text(styles=False), width, height)


def current_rows_for(
    run: dict[str, Any] | None,
    tab: str,
    runs: list[dict[str, Any]],
    *,
    selected_phase_id: str | None = None,
    agent_scope: str = "phase",
) -> list[dict[str, Any]]:
    return rows_for_tab(run, tab, runs, selected_phase_id=selected_phase_id, agent_scope=agent_scope)


def run_textual_app() -> None:
    from workflow_tui_app import run_textual_app as run_app

    run_app(sys.modules[__name__])


def print_summary() -> None:
    runs = load_runs()
    if not runs:
        print("No workflow runs found.")
        return
    for run in runs[:20]:
        print(workflow_state.format_summary(run))
        print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", action="store_true", help="render deterministic text snapshot and exit")
    parser.add_argument("--fixture", help="JSON fixture for snapshot mode")
    parser.add_argument("--tab", choices=TABS, default="overview")
    parser.add_argument("--width", type=int, default=100)
    parser.add_argument("--height", type=int, default=28)
    parser.add_argument("--run-index", type=int, default=0)
    parser.add_argument("--row-index", type=int, default=0)
    parser.add_argument("--scroll", type=int, default=0)
    parser.add_argument("--phase-id", help="phase context for phase-scoped agent snapshots")
    parser.add_argument("--phase-index", type=int, help="ordered phase context for phase-scoped agent snapshots")
    parser.add_argument("--agent-scope", choices=AGENT_SCOPES, default="phase")
    parser.add_argument("--agent-view", choices=AGENT_VIEWS, default="live")
    parser.add_argument("--filter", default="", help="filter rows by text")
    parser.add_argument("--focus", action="store_true", help="render selected detail full-width")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.snapshot:
        runs = load_fixture(args.fixture) if args.fixture else load_runs()
        sys.stdout.write(
            render_snapshot(
                runs,
                width=args.width,
                height=args.height,
                tab=args.tab,
                run_index=args.run_index,
                row_index=args.row_index,
                scroll=args.scroll,
                phase_id=args.phase_id,
                phase_index=args.phase_index,
                agent_scope=args.agent_scope,
                agent_view=args.agent_view,
                filter_text=args.filter,
                focus=args.focus,
            )
        )
        return
    workflow_state.runs_root().mkdir(parents=True, exist_ok=True)
    try:
        run_textual_app()
    except Exception as exc:
        if os.environ.get("WORKFLOW_TUI_DEBUG"):
            raise
        print(f"workflow TUI could not start: {type(exc).__name__}: {exc}", file=sys.stderr)
        print_summary()


if __name__ == "__main__":
    main()

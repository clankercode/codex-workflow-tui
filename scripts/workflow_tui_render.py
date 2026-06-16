"""Rich/Textual widget rendering for the workflow TUI."""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

import workflow_health
import workflow_state
import workflow_tui_live
from workflow_tui_activity import (
    compact_json,
    compact_path,
    display_path_value,
    format_duration_seconds,
    format_token_total,
    is_duration_seconds_key,
    parse_duration_seconds,
    longest_agent_label,
    agent_activity,
    collect_run_activity,
    resolve_artifact_path,
    read_text_artifact_preview,
    event_kind_text,
    infer_event_kind,
)

# ---------------------------------------------------------------------------
# Shared constants (imported by main TUI file)
# ---------------------------------------------------------------------------

STATUS_META = {
    "pending": ("PEND", "magenta"),
    "running": ("RUN", "cyan"),
    "blocked": ("BLCK", "yellow"),
    "completed": ("DONE", "green"),
    "failed": ("FAIL", "red"),
    "cancelled": ("CNCL", "red"),
    "paused": ("PAUS", "yellow"),
}
TABS = ("overview", "runs", "graph", "phases", "agents", "events", "decisions", "artifacts")
AGENT_SCOPES = ("phase", "all")
AGENT_VIEWS = ("live", "prompt")
AGENT_ONLY_ACTIONS = frozenset({"toggle_agent_scope", "toggle_agent_view"})
MIN_WIDTH = 80
MIN_HEIGHT = 12
DISPLAY_TIMESTAMP_WIDTH = 18
COMPACT_TIMESTAMP_WIDTH = 14
TIMESTAMP_KEYS = {"ts", "created_at", "updated_at", "started_at", "completed_at"}
SNAPSHOT_NOW_ENV = "WORKFLOW_TUI_SNAPSHOT_NOW"

AGENT_COLORS = (
    "cyan",
    "magenta",
    "green",
    "yellow",
    "blue",
    "red",
    "bright_cyan",
    "bright_magenta",
    "bright_green",
    "bright_yellow",
)


# ---------------------------------------------------------------------------
# Timestamp utilities (used by render functions)
# ---------------------------------------------------------------------------


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
        return text[:DISPLAY_TIMESTAMP_WIDTH] if text else ""
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


def display_event_timestamp(value: Any, now: datetime | None = None) -> str:
    """Return a compact local event timestamp, adding a date only for older events."""
    parsed = parse_local_datetime(value)
    if parsed is None:
        return "" if value is None else str(value)
    reference = (now or snapshot_reference_time() or datetime.now(parsed.tzinfo)).astimezone(parsed.tzinfo)
    if abs((reference - parsed).total_seconds()) <= 86_400:
        return parsed.strftime("%H:%M:%S %Z").strip()
    return parsed.strftime("%y-%m-%d %H:%M").strip()


def display_timestamps_in_detail(value: Any, key: str | None = None) -> Any:
    """Copy a detail value with timestamp and duration fields rendered for humans."""
    if isinstance(value, dict):
        return {
            item_key_inner: display_timestamps_in_detail(item_value, item_key_inner)
            for item_key_inner, item_value in value.items()
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

# ---------------------------------------------------------------------------
# Shared utility functions (imported by main TUI file)
# ---------------------------------------------------------------------------


def window_start(selected: int, total: int, visible: int) -> int:
    """Return the first visible index for a sliding window."""
    if total <= visible:
        return 0
    half = max(1, visible // 2)
    return max(0, min(selected - half, total - visible))


def clamp_index(index: int, total: int) -> int:
    """Clamp a selection index to valid range."""
    if total <= 0:
        return 0
    return max(0, min(index, total - 1))


def status_label(status: str) -> str:
    return STATUS_META.get(status, ("UNKN", "white"))[0]


def status_text(status: str) -> Text:
    label, style = STATUS_META.get(status, ("UNKN", "white"))
    return Text(label, style=f"bold {style}")


def marked_status_text(active: bool, status: str, *, stale: bool = False) -> Text:
    """Render status with a durable inline selection marker.

    When *stale* is true and the status is ``running``, the label is shown as
    ``RUN!`` with a red style to indicate the worker process has died.
    """
    label, style = STATUS_META.get(status, ("UNKN", "white"))
    if stale and status == "running":
        label = "RUN!"
        style = "red"
    text = Text("\u25b8 " if active else "  ", style="bold bright_white")
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


def marker_text(active: bool) -> Text:
    return Text("\u25b8" if active else " ", style="bold bright_white")


def json_renderable(value: Any) -> Syntax:
    payload = json.dumps(value, indent=2, sort_keys=True)
    return Syntax(payload, "json", theme="ansi_dark", word_wrap=True)


def action_enabled_for_tab(tab: str, action: str) -> bool:
    """Return whether a live TUI action is meaningful for the active tab."""
    return tab == "agents" or action not in AGENT_ONLY_ACTIONS


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


def index_for_key(rows: list[dict[str, Any]], tab: str, key: str | None) -> int:
    """Return the index for a persisted selection key."""
    if not key or not rows:
        return 0
    for index, row in enumerate(rows):
        if item_key(tab, row, index) == key:
            return index
    return 0


def item_key(tab: str, item: dict[str, Any], index: int) -> str:
    """Return a stable selection key for a tab row."""
    if tab == "runs":
        return str(item.get("run_id") or item.get("id") or f"run-{index}")
    if tab == "phases":
        return str(item.get("phase_id") or f"phase-{index}")
    if tab == "agents":
        return str(item.get("agent_id") or f"agent-{index}")
    if tab == "events":
        return str(item.get("event_id") or f"event-{index}")
    if tab == "decisions":
        return str(item.get("decision_id") or f"decision-{index}")
    if tab == "artifacts":
        return str(item.get("artifact_id") or f"artifact-{index}")
    if tab == "overview":
        return str(item.get("attention_id") or f"item-{index}")
    return f"row-{index}"


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


def active_filter(filter_text: str) -> str:
    """Return the normalized active filter label."""
    return " ".join(filter_text.strip().split())


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


def filter_empty_message(filter_text: str) -> str:
    """Return a helpful empty-state message when a filter hides all rows."""
    return f"No rows match filter: {active_filter(filter_text)}"


# ---------------------------------------------------------------------------
# Widget rendering
# ---------------------------------------------------------------------------


def make_header(tab: str) -> Text:
    header = Text()
    hints = ["\u2191/\u2193 rows", "\u2190/\u2192 tabs", "y id", "p path"]
    if tab == "agents":
        hints.extend(["a scope", "v view"])
    hints.append("r/q")
    header.append("Agent Workflows", style="bold bright_cyan")
    header.append(f"  {'  '.join(hints)}", style="dim")
    return header


def make_footer(run: dict[str, Any] | None, width: int) -> Text:
    label = "path: "
    path = compact_path(path_text(run), max(0, width - len(label)))
    return Text(label + path, style="bright_black", overflow="ellipsis", no_wrap=True)


def make_tabs_title(tab: str, compact: bool = False) -> Text:
    labels = {
        "overview": "ovr",
        "runs": "run",
        "graph": "grf",
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
            title.append(f"\u25cf {label}", style="bold bright_white on dark_green")
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
        run_status_label = STATUS_META.get(str(run.get("status", "")), (str(run.get("status", "")).upper()[:4], ""))[0]
        duration = run_duration_text(run)
        summary = Text(str(run.get("title", "")) or "(untitled)")
        summary.append("\n  > ", style="dim")
        summary.append(run_status_label, style=status_text(run.get("status", "")).style)
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


def merged_live_output_text(
    activities: list[dict[str, Any]],
    agents: list[dict[str, Any]],
    *,
    max_lines: int = 80,
) -> Text:
    """Merge live output from multiple agents with colored name prefixes.

    Outputs are grouped per agent (each agent's lines kept together) and
    every line is prefixed with ``[agent-name]`` in a color assigned per
    agent.  When only one agent has output, the prefix is omitted so
    single-agent runs remain clean.
    """
    entries: list[tuple[str, str, str]] = []
    agent_names: dict[str, str] = {
        str(agent.get("agent_id", "")): str(agent.get("name") or agent.get("agent_id") or "agent")
        for agent in agents
    }
    color_map: dict[str, str] = {}
    color_index = 0
    for activity in activities:
        output = str(activity.get("latest_output") or "").strip()
        if not output:
            continue
        agent_id = str(activity.get("agent_id", ""))
        agent_name = agent_names.get(agent_id) or str(activity.get("name") or "") or agent_id or "agent"
        if agent_id not in color_map:
            color_map[agent_id] = AGENT_COLORS[color_index % len(AGENT_COLORS)]
            color_index += 1
        entries.append((agent_name, color_map[agent_id], output))

    if not entries:
        return Text("No live output yet.", style="dim")
    if len(entries) == 1:
        return Text(entries[0][2], overflow="fold")

    result = Text()
    first_line = True
    for agent_name, color, output in entries:
        prefix = f"[{agent_name}] "
        for line in output.splitlines()[:max_lines]:
            if not first_line:
                result.append("\n")
            result.append(prefix, style=f"bold {color}")
            result.append(line)
            first_line = False
    return result


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
    merged_output = merged_live_output_text(
        live.get("activities", []), run.get("agents", [])
    )
    output_label = f"Merged Live Output — {live.get('latest_output_agent', '')}" if live.get("latest_output_agent") else "Merged Live Output"
    latest = Panel(merged_output, title=output_label, border_style="yellow", box=box.ROUNDED)
    panels: list[Any] = [facts, live_stats, latest]
    if detail_height is None or detail_height >= 28:
        running_table = workflow_tui_live.running_agents_table(live, format_duration_seconds)
        if running_table is not None:
            panels.append(Panel(running_table, title="Running Agents", border_style="green", box=box.ROUNDED))
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
        stale = workflow_health.agent_process_is_dead(agent)
        table.add_row(
            marked_status_text(index == selected, agent.get("status", ""), stale=stale),
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
    agent_name = str(agent.get("name") or agent.get("agent_id") or "agent")
    body = (
        Panel(Text(str(agent.get("prompt", "")) or "No prompt recorded.", overflow="fold"), title=f"Prompt — {agent_name}", border_style="yellow", box=box.ROUNDED)
        if agent_view == "prompt"
        else Panel(Text(str(output_text), overflow="fold"), title=f"Live Output — {agent_name}", border_style="yellow", box=box.ROUNDED)
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
    if activity.get("fallback_output") and str(agent.get("status", "")) in workflow_state.TERMINAL_STATUS_VALUES:
        panels.append(Panel(Text(activity["fallback_output"], overflow="fold"), title="Fallback (no final output)", border_style="yellow", box=box.ROUNDED))
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
            Text(("\u25b8 " if index == selected else "  ") + display_event_timestamp(event.get("ts", ""))),
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


# ---------------------------------------------------------------------------
# Graph tab (phart dependency visualization)
# ---------------------------------------------------------------------------

try:
    import networkx as nx
    from phart import ASCIIRenderer, LayoutOptions
    HAS_PHART = True
except ImportError:
    HAS_PHART = False

STATUS_ICONS = {
    "completed": "●",
    "running": "◐",
    "pending": "○",
    "failed": "✗",
    "cancelled": "⊘",
    "blocked": "◉",
    "paused": "‖",
}

# Animated running icons cycle through these on each refresh
_RUNNING_CYCLE = ["◐", "◓", "◑", "◒"]

# ANSI color names for phart node_color attribute
STATUS_COLORS = {
    "completed": "green",
    "running": "cyan",
    "pending": "yellow",
    "failed": "red",
    "cancelled": "bright_black",
    "blocked": "magenta",
    "paused": "yellow",
}


def build_run_graph(run: dict[str, Any]) -> Any | None:
    """Build a NetworkX DiGraph from workflow run state."""
    if not HAS_PHART:
        return None
    import time as _time
    G = nx.DiGraph()
    agents = run.get("agents", [])
    phases = run.get("phases", [])
    if not agents:
        return None
    # Animated running icon cycles based on current time
    cycle_index = int(_time.time()) % len(_RUNNING_CYCLE)
    # Add start node
    title = str(run.get("title", "workflow")[:20])
    G.add_node("▶", label=title, color="white")
    # Build agent nodes with compact labels and colors
    for agent in agents:
        name = str(agent.get("name", ""))
        status = str(agent.get("status", ""))
        if status == "running":
            icon = _RUNNING_CYCLE[cycle_index]
        else:
            icon = STATUS_ICONS.get(status, "?")
        # Short label: just name + icon, no runner type
        label = f"{name} {icon}"
        color = STATUS_COLORS.get(status, "white")
        G.add_node(name, label=label[:24], color=color)
    # Connect start to agents with no dependencies
    for agent in agents:
        depends_on = str(agent.get("depends_on", "")).strip()
        if not depends_on:
            G.add_edge("▶", str(agent.get("name", "")))
    # Connect dependencies
    for agent in agents:
        name = str(agent.get("name", ""))
        depends_on = str(agent.get("depends_on", "")).strip()
        if not depends_on:
            continue
        for dep in depends_on.split(","):
            dep = dep.strip()
            if dep and dep in G:
                G.add_edge(dep, name)
    return G if len(G) > 1 else None


def make_run_graph_panel(run: dict[str, Any]) -> Any:
    """Render a dependency graph for a workflow run using phart."""
    if not HAS_PHART:
        return Panel(
            Text("Install phart for graph view: pip install phart", style="dim"),
            title="Dependency Graph",
            border_style="yellow",
            box=box.ROUNDED,
        )
    G = build_run_graph(run)
    if G is None:
        return Panel(
            Text("No agents to graph.", style="dim"),
            title="Dependency Graph",
            border_style="yellow",
            box=box.ROUNDED,
        )
    opts = LayoutOptions(
        use_labels=True,
        node_label_attr="label",
        bboxes=True,
        hpad=1,
        vpad=0,
        layer_spacing=2,
        use_ascii=False,
        ansi_colors=True,
        target_canvas_width=76,
    )
    renderer = ASCIIRenderer(G, options=opts)
    graph_text = renderer.render()
    return Panel(
        Text(graph_text, overflow="fold"),
        title="Dependency Graph",
        border_style="cyan",
        box=box.ROUNDED,
    )

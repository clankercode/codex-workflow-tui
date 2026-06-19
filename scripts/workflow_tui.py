#!/usr/bin/env python3
"""Textual/Rich TUI for agent workflow state."""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import workflow_state
import workflow_health

# Re-export everything from submodules for backward compatibility.
from workflow_tui_update import (  # noqa: F401
    UPDATE_CHECK_TIMEOUT,
    UPDATE_PULL_TIMEOUT,
    UPDATE_CHECK_INTERVAL,
    GitCommandResult,
    UpdateStatus,
    UpdateActionResult,
    WorkflowControlResult,
    check_skill_update,
    update_skill_from_git,
    workflow_control_action,
    skill_repo_root,
    text_from_process,
    run_git_command,
    unavailable_update,
    parse_ls_remote_head,
    short_head,
    ATTENTION_SEEN_AGE_SECONDS,
    ATTENTION_TOAST_TTL_SECONDS,
    ATTENTION_TOAST_HEIGHT,
    refresh_attention_toasts,
    build_attention_toast_panel,
    attention_unread_keys,
)
from workflow_tui_activity import (  # noqa: F401
    TAIL_BYTES,
    MAX_PREVIEW_CHARS,
    MAX_ARTIFACT_PREVIEW_BYTES,
    EVENT_STYLE_BY_KIND,
    compact_json,
    compact_path,
    compact_tool_input,
    resolve_workflow_path,
    is_duration_seconds_key,
    parse_duration_seconds,
    compact_decimal,
    format_duration_seconds,
    has_event_rollover,
    infer_event_kind,
    event_kind_text,
    safe_read_tail_info,
    safe_read_tail,
    discard_partial_first_jsonl_record,
    trim_preview,
    token_value,
    empty_token_totals,
    source_has_token_fields,
    merge_token_max,
    format_token_total,
    format_token_total_with_throughput,
    compute_tokens_per_second,
    smooth_counter_display,
    timestamp_epoch,
    path_mtime,
    json_event_epoch,
    summarize_tool_call,
    tool_event_key,
    parse_json_activity,
    parse_text_activity,
    should_parse_json_activity,
    resolve_agent_path,
    resolve_artifact_path,
    is_binary_text,
    read_text_artifact_preview,
    parse_ccc_output_log,
    agent_activity,
    activity_sort_key,
    collect_run_activity,
    longest_agent_label,
    reference_epoch,
    display_path_value,
)
from workflow_tui_render import (  # noqa: F401
    STATUS_META,
    LAYOUT_MODES,
    DEFAULT_LAYOUT_MODE,
    TABS,
    TAB_ALIASES,
    AGENT_SCOPES,
    AGENT_VIEWS,
    AGENT_ONLY_ACTIONS,
    MIN_WIDTH,
    MIN_HEIGHT,
    DISPLAY_TIMESTAMP_WIDTH,
    COMPACT_TIMESTAMP_WIDTH,
    TIMESTAMP_KEYS,
    SNAPSHOT_NOW_ENV,
    RUN_ROW_HEIGHT,
    parse_local_datetime,
    display_timestamp,
    snapshot_reference_time,
    run_duration_text,
    agent_duration_text,
    display_event_timestamp,
    display_timestamps_in_detail,
    window_start,
    clamp_index,
    status_label,
    status_text,
    marked_status_text,
    severity_text,
    path_text,
    marker_text,
    json_renderable,
    normalize_tab,
    normalize_layout_mode,
    action_enabled_for_tab,
    phase_rank,
    ordered_phases,
    index_for_key,
    item_key,
    selected_phase,
    agent_kind,
    agent_model,
    active_filter,
    row_matches_filter,
    apply_row_filter,
    filter_empty_message,
    make_header,
    make_footer,
    make_tabs_title,
    make_panel_title,
    detail_panel_title,
    make_mapping_table,
    make_attention_table,
    make_attention_detail,
    make_runs_table,
    make_facts_table,
    make_facts_grid,
    make_run_detail,
    merged_live_output_text,
    make_phase_table,
    make_agent_table,
    make_agent_activity_detail,
    make_events_table,
    collection_label,
    empty_sidebar_table,
    make_collection_table,
    phase_agents,
    make_phase_detail,
    make_event_detail,
    make_decision_detail,
    make_artifact_detail,
    make_rollover_warning,
    selected_detail,
    make_run_graph_panel,
)


# ---------------------------------------------------------------------------
# Core data loading
# ---------------------------------------------------------------------------


def load_runs() -> list[dict[str, Any]]:
    return workflow_state.load_all_runs()


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


# ---------------------------------------------------------------------------
# Copy support
# ---------------------------------------------------------------------------


def copy_value_for_selection(
    run: dict[str, Any] | None,
    tab: str,
    rows: list[dict[str, Any]],
    selected: int,
    mode: str,
) -> tuple[str, str]:
    """Return the label and value copied by the live TUI for the current row."""
    tab = normalize_tab(tab)
    if not rows:
        return ("", "")
    item = rows[clamp_index(selected, len(rows))]
    id_labels = {
        "runs": "run_id",
        "phases": "phase_id",
        "agents": "agent_id",
        "events": "event_id",
        "decisions": "decision_id",
        "artifacts": "artifact_id",
        "attention": "attention_id",
    }
    if mode == "id":
        key = id_labels.get(tab, "id")
        return (key, str(item.get(key) or item.get("id") or ""))
    if mode == "path":
        if tab == "attention":
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


# ---------------------------------------------------------------------------
# Layout: sidebar + detail orchestration
# ---------------------------------------------------------------------------


def rows_for_tab(
    run: dict[str, Any] | None,
    tab: str,
    runs: list[dict[str, Any]],
    *,
    selected_phase_id: str | None = None,
    agent_scope: str = "phase",
) -> list[dict[str, Any]]:
    tab = normalize_tab(tab)
    if tab == "attention":
        return workflow_health.attention_items(runs)
    if tab == "runs":
        return runs
    if tab == "graph":
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


def make_sidebar(tab: str, rows: list[dict[str, Any]], selected: int, visible: int, *, filter_text: str = "", unread_keys: set[str] | None = None) -> Any:
    tab = normalize_tab(tab)
    if not rows and active_filter(filter_text):
        return Text(filter_empty_message(filter_text), style="dim")
    if tab == "attention":
        return make_attention_table(rows, selected, visible, unread_keys=unread_keys)
    if tab in ("runs", "graph"):
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
    detail_width: int | None = None,
    agent_view: str = "live",
    agent_scope: str = "phase",
    filter_text: str = "",
) -> Any:
    tab = normalize_tab(tab)
    if not rows and active_filter(filter_text):
        return Text(filter_empty_message(filter_text), style="dim")
    if tab == "attention":
        if not rows:
            return Text("No attention items.", style="dim")
        return make_attention_detail(rows[clamp_index(selected, len(rows))])
    if tab == "runs":
        if not rows:
            return Text("No run selected.", style="dim")
        return make_run_detail(rows[clamp_index(selected, len(rows))], detail_height=detail_height)
    if tab == "graph":
        if not rows:
            return Text("No run selected.", style="dim")
        return make_run_graph_panel(rows[clamp_index(selected, len(rows))], detail_width=detail_width)
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
        detail = make_event_detail(item)
        rollover_warning = make_rollover_warning(run)
        if rollover_warning:
            return Group(
                Panel(rollover_warning, title="Warning", border_style="yellow", box=box.ROUNDED),
                detail,
            )
        return detail
    if tab == "decisions":
        return make_decision_detail(item)
    if tab == "artifacts":
        return make_artifact_detail(item, run)
    title = "Selected Row"
    return selected_detail(rows, selected, title)


def sidebar_title_for(tab: str, run: dict[str, Any] | None, selected_phase_id: str | None, agent_scope: str) -> str:
    """Return the contextual sidebar title."""
    tab = normalize_tab(tab)
    if tab not in ("agents", "graph") or (tab == "agents" and agent_scope == "all"):
        if tab == "attention":
            return "Attention"
        if tab == "graph":
            return "Runs"
        return tab.capitalize()
    phase = selected_phase(run, selected_phase_id)
    if not phase:
        return "Agents"
    return f"Agents: {phase.get('name', phase.get('phase_id', 'Phase'))}"


def title_with_filter(title: str, filter_text: str) -> str:
    """Append the active filter to a panel title."""
    normalized = active_filter(filter_text)
    return f"{title} filter: {normalized}" if normalized else title


# ---------------------------------------------------------------------------
# Dashboard rendering
# ---------------------------------------------------------------------------


def _clip_content_to_height(content: Any, pane_height: int, scroll_offset: int = 0) -> str:
    """Render content and clip to a fixed number of visible lines with scroll offset."""
    sink = io.StringIO()
    console = Console(file=sink, force_terminal=False, color_system=None, width=200)
    console.print(content)
    all_lines = sink.getvalue().splitlines()
    start = max(0, scroll_offset)
    end = start + max(1, pane_height)
    visible_lines = all_lines[start:end]
    while len(visible_lines) < pane_height:
        visible_lines.append("")
    return "\n".join(visible_lines[:pane_height])


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
    scroll_offset: int = 0,
    layout_mode: str = DEFAULT_LAYOUT_MODE,
) -> Any:
    tab = normalize_tab(tab)
    layout_mode = normalize_layout_mode(layout_mode)
    if tab not in TABS:
        raise SystemExit(f"invalid tab {tab!r}; expected one of {TABS}")
    min_width = MIN_WIDTH if chrome else MIN_WIDTH - 2
    min_height = MIN_HEIGHT if chrome else MIN_HEIGHT - 3
    if height < min_height or width < min_width:
        return Text(f"terminal too small; need at least {MIN_WIDTH}x{MIN_HEIGHT}", style="bold red")

    selected_run_index = clamp_index(run_index, len(runs))
    base_run = runs[selected_run_index] if runs else None

    # Attention toasts appear only on the attention tab, above the header. The
    # panel is single-line so its height is fixed; shrink the panes so the
    # footer stays anchored when a toast is visible.
    toast_panel = None
    toast_height = 0
    attention_unread: set[str] = set()
    if tab == "attention":
        attention_rows = rows_for_tab(base_run, "attention", runs)
        refresh_attention_toasts(attention_rows)
        attention_unread = attention_unread_keys(attention_rows)
        toast_panel = build_attention_toast_panel(width=width)
        if toast_panel is not None:
            toast_height = ATTENTION_TOAST_HEIGHT

    pane_height = (height - 2 if chrome else height) - toast_height
    if tab == "graph":
        # Give the graph tab a narrower sidebar so the DAG has more room.
        left_width = max(28, min(34, width // 3))
    else:
        left_width = max(40, min(46, (width * 2) // 5))
    right_width = max(20, width - left_width)
    visible = max(1, pane_height - 5)
    if tab == "runs":
        visible = max(1, visible // RUN_ROW_HEIGHT)
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
        detail_width=width if focus else right_width,
        agent_view=agent_view,
        agent_scope=agent_scope,
        filter_text=filter_text,
    )
    use_scroll = scroll_offset > 0
    detail_title = detail_panel_title(tab)
    if focus:
        if use_scroll:
            clipped = _clip_content_to_height(detail, pane_height, scroll_offset)
            focused = Panel(
                Text(clipped),
                title=detail_title,
                border_style="green",
                box=box.ROUNDED,
            )
        else:
            focused = Panel(
                detail,
                title=detail_title,
                border_style="green",
                box=box.ROUNDED,
                height=pane_height,
            )
        if not chrome:
            return Group(toast_panel, focused) if toast_panel is not None else focused
        parts: list[Any] = []
        if toast_panel is not None:
            parts.append(toast_panel)
        parts.extend([make_header(tab, width=width, filter_text=filter_text, layout_mode=layout_mode), focused, make_footer(run, width)])
        return Group(*parts)

    layout = Table.grid(expand=True)
    layout.add_column(width=left_width)
    layout.add_column(width=right_width)
    if use_scroll:
        clipped_detail = _clip_content_to_height(detail, pane_height, scroll_offset)
        detail_widget = Text(clipped_detail)
    else:
        detail_widget = detail
    layout.add_row(
        Panel(
            make_sidebar(tab, rows, selected_row_index, visible, filter_text=filter_text, unread_keys=attention_unread),
            title=Text(sidebar_title, style="bold cyan"),
            border_style="cyan",
            box=box.ROUNDED,
            height=pane_height,
        ),
        Panel(
            detail_widget,
            title=detail_title,
            border_style="green",
            box=box.ROUNDED,
            height=pane_height,
        ),
    )
    if not chrome:
        return Group(toast_panel, layout) if toast_panel is not None else layout
    chrome_parts: list[Any] = []
    if toast_panel is not None:
        chrome_parts.append(toast_panel)
    chrome_parts.extend([make_header(tab, width=width, filter_text=filter_text, layout_mode=layout_mode), layout, make_footer(run, width)])
    return Group(*chrome_parts)


# ---------------------------------------------------------------------------
# Snapshot and CLI
# ---------------------------------------------------------------------------


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
    detail_scroll: int = 0,
    layout_mode: str | None = None,
) -> str:
    """Render the TUI as deterministic text for snapshot tests."""
    tab = normalize_tab(tab)
    layout_mode = normalize_layout_mode(layout_mode)
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
            scroll_offset=detail_scroll,
            layout_mode=layout_mode,
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


def canonical_tab(value: str) -> str:
    tab = normalize_tab(value)
    if tab not in TABS:
        raise argparse.ArgumentTypeError(f"invalid tab {value!r}; expected one of {TABS}")
    return tab


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
    parser.add_argument("--tab", type=canonical_tab, default="runs")
    parser.add_argument("--layout", choices=LAYOUT_MODES, default=None)
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
    parser.add_argument("--detail-scroll", type=int, default=0, help="scroll offset for detail pane")
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
                detail_scroll=args.detail_scroll,
                layout_mode=args.layout,
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

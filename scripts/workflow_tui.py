#!/usr/bin/env python3
"""Textual/Rich TUI for agent workflow state."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.pretty import Pretty
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

import workflow_state

TABS = ("runs", "phases", "agents", "events", "decisions", "artifacts")
AGENT_SCOPES = ("phase", "all")
AGENT_VIEWS = ("live", "prompt")
MIN_WIDTH = 80
MIN_HEIGHT = 12
DISPLAY_TIMESTAMP_WIDTH = 18
COMPACT_TIMESTAMP_WIDTH = 14
TAIL_BYTES = 96_000
MAX_PREVIEW_CHARS = 2_400
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


def display_event_timestamp(value: Any, now: datetime | None = None) -> str:
    """Return a compact local event timestamp, adding a date only for older events."""
    parsed = parse_local_datetime(value)
    if parsed is None:
        return "" if value is None else str(value)
    reference = (now or datetime.now(parsed.tzinfo)).astimezone(parsed.tzinfo)
    if abs((reference - parsed).total_seconds()) <= 86_400:
        return parsed.strftime("%H:%M %Z").strip()
    return parsed.strftime("%y-%m-%d %H:%M").strip()


def display_timestamps_in_detail(value: Any, key: str | None = None) -> Any:
    """Copy a detail value with timestamp fields rendered for humans."""
    if isinstance(value, dict):
        return {
            item_key: display_timestamps_in_detail(item_value, item_key)
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [display_timestamps_in_detail(item) for item in value]
    if isinstance(value, str) and key in TIMESTAMP_KEYS:
        return display_timestamp(value)
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


def make_header() -> Text:
    header = Text()
    header.append("Agent Workflows", style="bold bright_cyan")
    header.append("  ↑/↓ rows  ←/→ tabs  y id  p path  a scope  v view  r/q", style="dim")
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


def make_footer(run: dict[str, Any] | None, width: int) -> Text:
    label = "path: "
    path = compact_path(path_text(run), max(0, width - len(label)))
    return Text(label + path, style="bright_black", overflow="ellipsis", no_wrap=True)


def make_tabs_title(tab: str, compact: bool = False) -> Text:
    labels = {
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


def status_counts_text(counts: dict[str, Any]) -> Text:
    """Render status counts as colored compact labels."""
    text = Text()
    for status in STATUS_META:
        count = token_value(counts.get(status))
        if not count:
            continue
        if text:
            text.append("  ")
        label, style = STATUS_META[status]
        text.append(f"{label} {count}", style=f"bold {style}")
    if not text:
        text.append("none", style="dim")
    return text


def make_mapping_table(rows: list[tuple[str, Any]]) -> Table:
    """Render structured fields as a compact two-column table."""
    table = Table.grid(expand=True)
    table.add_column(width=14, no_wrap=True)
    table.add_column(ratio=1)
    for label, value in rows:
        if isinstance(value, Text):
            rendered = value
        elif isinstance(value, (dict, list)):
            rendered = compact_json(display_timestamps_in_detail(value))
        elif value is None:
            rendered = ""
        else:
            rendered = str(value)
        table.add_row(Text(label, style="bold bright_black"), rendered)
    return table


def make_metrics_panel(run: dict[str, Any], live: dict[str, Any]) -> Panel:
    """Render derived run metrics without raw JSON."""
    metrics = run.get("metrics", {})
    rows = [
        ("agents", metrics.get("agents_total", len(run.get("agents", [])))),
        ("agent state", status_counts_text(metrics.get("agents_by_status", {}))),
        ("phases", metrics.get("phases_total", len(run.get("phases", [])))),
        ("phase state", status_counts_text(metrics.get("phases_by_status", {}))),
        ("tokens", live.get("tokens", {}).get("total", 0)),
        ("tool calls", live.get("tool_call_count", 0)),
    ]
    return Panel(make_mapping_table(rows), title="Metrics", border_style="green", box=box.ROUNDED)


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


def make_runs_table(runs: list[dict[str, Any]], selected: int, visible: int) -> Table:
    table = Table(box=None, expand=True, show_header=True, header_style="bold bright_black", pad_edge=False)
    table.add_column("", width=1, no_wrap=True)
    table.add_column("State", width=5, no_wrap=True)
    table.add_column("Title", ratio=1, overflow="ellipsis", no_wrap=True)
    if not runs:
        table.add_row("", Text("NONE", style="dim"), "No workflow runs found.")
        return table
    selected = clamp_index(selected, len(runs))
    start = window_start(selected, len(runs), visible)
    for index, run in enumerate(runs[start : start + visible], start=start):
        metrics = run.get("metrics", {})
        agents_total = metrics.get("agents_total", len(run.get("agents", [])))
        style = "reverse" if index == selected else ""
        table.add_row(
            marker_text(index == selected),
            status_text(run.get("status", "")),
            str(run.get("title", "")),
            style=style,
        )
    return table


def safe_read_tail(path_value: str | Path | None, limit: int = TAIL_BYTES) -> str:
    """Read the tail of a text file without assuming it is small or stable."""
    if not path_value:
        return ""
    path = Path(path_value).expanduser()
    if not path.exists() or not path.is_file():
        return ""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


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


def merge_token_max(target: dict[str, int], source: dict[str, Any]) -> None:
    """Use max values so cumulative token events are not double counted."""
    aliases = {
        "total": ("total", "total_tokens"),
        "input": ("input", "input_tokens", "prompt_tokens"),
        "output": ("output", "output_tokens", "completion_tokens"),
        "reasoning": ("reasoning", "reasoning_tokens"),
    }
    for target_key, source_keys in aliases.items():
        for source_key in source_keys:
            if source_key in source:
                target[target_key] = max(target.get(target_key, 0), token_value(source.get(source_key)))
    if not target.get("total"):
        target["total"] = max(target.get("total", 0), target.get("input", 0) + target.get("output", 0) + target.get("reasoning", 0))


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
    title = state.get("title") or source.get("title") or ""
    input_value = state.get("input") or source.get("input") or source.get("arguments") or {}
    if source.get("type") == "command_execution":
        input_value = source.get("command") or input_value
    if isinstance(input_value, dict):
        input_text = input_value.get("command") or input_value.get("description") or compact_json(input_value)
    else:
        input_text = str(input_value)
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
    tokens = {"total": 0, "input": 0, "output": 0, "reasoning": 0}
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
        if re.search(r"\b(tool|bash|command|called)\b", line, re.IGNORECASE):
            tool_groups.append([line])
            current_tool = None
            saw_result = False
    tool_lines = [" ".join(group)[:220] for group in tool_groups]
    output_source = assistant_lines or lines[-24:]
    return {
        "latest_output": trim_preview("\n".join(output_source)),
        "tool_calls": tool_lines[-6:],
        "tool_call_count": len(tool_groups),
        "tokens": {"total": 0, "input": 0, "output": 0, "reasoning": 0},
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
        transcript_path = ccc_dir / "transcript.jsonl"
        if not transcript_path.exists():
            transcript_path = ccc_dir / "transcript.txt"
        output_path = ccc_dir / "output.txt"
    transcript_text = safe_read_tail(transcript_path)
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
    token_totals = {"total": 0, "input": 0, "output": 0, "reasoning": 0}
    for activity in activities:
        for key in token_totals:
            token_totals[key] += token_value(activity.get("tokens", {}).get(key))
    running_agents = [agent for agent in run.get("agents", []) if agent.get("status") == "running"]
    now_epoch = time.time()
    longest_running = None
    for agent in running_agents:
        started = agent.get("started_epoch")
        if started is None and agent.get("started_at"):
            try:
                started = datetime.fromisoformat(str(agent["started_at"]).replace("Z", "+00:00")).timestamp()
            except ValueError:
                started = None
        if started is None:
            continue
        elapsed = max(0.0, now_epoch - float(started))
        if longest_running is None or elapsed > longest_running["elapsed_seconds"]:
            longest_running = {"name": agent.get("name", ""), "agent_id": agent.get("agent_id", ""), "elapsed_seconds": round(elapsed, 1)}
    return {
        "activities": activities,
        "tokens": token_totals,
        "tool_call_count": sum(token_value(activity.get("tool_call_count")) for activity in activities),
        "latest_tool_calls": [call for activity in ordered_activities for call in activity.get("tool_calls", [])][-8:],
        "latest_output": next((activity["latest_output"] for activity in reversed(ordered_activities) if activity.get("latest_output")), ""),
        "longest_running": longest_running,
    }


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
        if tab == "runs":
            return ("state path", path_text(item))
        if tab == "agents":
            for key in ("output_path", "jsonl_path", "log_path"):
                resolved = resolve_agent_path(item, key, run)
                if resolved:
                    return ("agent path", str(resolved))
            return ("agent path", "")
        if tab == "artifacts":
            resolved = resolve_artifact_path(item, run)
            return ("artifact path", str(resolved) if resolved else "")
        return ("state path", path_text(run))
    if mode == "json":
        return (f"{tab[:-1] if tab.endswith('s') else tab} json", json.dumps(display_timestamps_in_detail(item), indent=2, sort_keys=True))
    raise ValueError(f"unknown copy mode {mode!r}")


def make_facts_table(rows: list[tuple[str, Any]]) -> Table:
    table = Table.grid(expand=True)
    table.add_column(width=12, no_wrap=True)
    table.add_column(ratio=1)
    for label, value in rows:
        table.add_row(Text(label, style="bold bright_black"), "" if value is None else str(value))
    return table


def make_run_detail(run: dict[str, Any]) -> Group:
    live = collect_run_activity(run)
    facts = make_facts_table(
        [
            ("id", run.get("run_id", "")),
            ("title", run.get("title", "")),
            ("status", run.get("status", "")),
            ("mode", run.get("mode", "")),
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
    metrics = make_metrics_panel(run, live)
    live_rows = [
        ("tokens", live["tokens"].get("total", 0)),
        ("tool calls", live.get("tool_call_count", 0)),
        ("active", len([agent for agent in run.get("agents", []) if agent.get("status") == "running"])),
        ("longest", (live.get("longest_running") or {}).get("name", "")),
    ]
    live_stats = Panel(make_facts_table(live_rows), title="Live Stats", border_style="magenta", box=box.ROUNDED)
    tool_text = "\n".join(live.get("latest_tool_calls", [])[-8:]) or "No tool calls recorded yet."
    tools = Panel(Text(tool_text, overflow="fold"), title="Latest Tool Calls", border_style="cyan", box=box.ROUNDED)
    latest = Panel(Text(live.get("latest_output") or "No live output yet.", overflow="fold"), title="Live Output", border_style="yellow", box=box.ROUNDED)
    return Group(facts, live_stats, latest, tools, prompt, metrics)


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
    table.add_column("Name", ratio=1, overflow="ellipsis", no_wrap=True)
    if not agents:
        table.add_row("", empty_message)
        return table
    selected = clamp_index(selected, len(agents))
    start = window_start(selected, len(agents), visible)
    for index, agent in enumerate(agents[start : start + visible], start=start):
        style = "reverse" if index == selected else ""
        table.add_row(
            marked_status_text(index == selected, agent.get("status", "")),
            str(agent.get("name", "")),
            style=style,
        )
    return table


def make_agent_activity_detail(agent: dict[str, Any], run: dict[str, Any] | None = None, agent_view: str = "live") -> Group:
    activity = agent_activity(agent, run)
    stats = make_facts_table(
        [
            ("tokens", activity.get("tokens", {}).get("total", 0)),
            ("tools", activity.get("tool_call_count", 0)),
            ("parse errs", activity.get("parse_errors", 0)),
            ("status", agent.get("status", "")),
            ("thread", agent.get("thread_id", "")),
        ]
    )
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
    return Group(
        Panel(make_mapping_table(info_rows), title="Agent", border_style="blue", box=box.ROUNDED),
        Panel(stats, title="Live Stats", border_style="magenta", box=box.ROUNDED),
        Panel(Text(tool_text, overflow="fold"), title="Latest Tool Calls", border_style="cyan", box=box.ROUNDED),
        body,
    )


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
    table.add_column("Summary", ratio=1, overflow="ellipsis")
    table.add_column("When", width=COMPACT_TIMESTAMP_WIDTH, no_wrap=True)
    if not rows:
        return empty_sidebar_table(f"No {tab}.")
    selected = clamp_index(selected, len(rows))
    start = window_start(selected, len(rows), visible)
    for index, item in enumerate(rows[start : start + visible], start=start):
        style = "reverse" if index == selected else ""
        table.add_row(
            marker_text(index == selected),
            collection_label(item, index),
            display_event_timestamp(item.get("ts", "")),
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
    return Panel(make_mapping_table(rows), title="Artifact", border_style="blue", box=box.ROUNDED)


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


def make_sidebar(tab: str, rows: list[dict[str, Any]], selected: int, visible: int) -> Any:
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
    agent_view: str = "live",
    agent_scope: str = "phase",
) -> Any:
    if tab == "runs":
        if not rows:
            return Text("No run selected.", style="dim")
        return make_run_detail(rows[clamp_index(selected, len(rows))])
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
        return tab.capitalize()
    phase = selected_phase(run, selected_phase_id)
    if not phase:
        return "Agents"
    return f"Agents: {phase.get('name', phase.get('phase_id', 'Phase'))}"


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
) -> Any:
    if tab not in TABS:
        raise SystemExit(f"invalid tab {tab!r}; expected one of {TABS}")
    min_width = MIN_WIDTH if chrome else MIN_WIDTH - 2
    min_height = MIN_HEIGHT if chrome else MIN_HEIGHT - 3
    if height < min_height or width < min_width:
        return Text(f"terminal too small; need at least {MIN_WIDTH}x{MIN_HEIGHT}", style="bold red")

    selected_run_index = clamp_index(run_index, len(runs))
    run = runs[selected_run_index] if runs else None
    pane_height = height - 2 if chrome else height
    left_width = max(36, min(46, (width * 2) // 5))
    right_width = max(20, width - left_width)
    visible = max(1, pane_height - 5)
    rows = rows_for_tab(run, tab, runs, selected_phase_id=selected_phase_id, agent_scope=agent_scope)
    selected_row_index = selected_run_index if tab == "runs" else clamp_index(row_index, len(rows))
    sidebar_title = sidebar_title_for(tab, run, selected_phase_id, agent_scope)

    layout = Table.grid(expand=True)
    layout.add_column(width=left_width)
    layout.add_column(width=right_width)
    layout.add_row(
        Panel(
            make_sidebar(tab, rows, selected_row_index, visible),
            title=Text(sidebar_title, style="bold cyan"),
            border_style="cyan",
            box=box.ROUNDED,
            height=pane_height,
        ),
        Panel(
            make_detail_body(run, tab, rows, selected_row_index, agent_view=agent_view, agent_scope=agent_scope),
            title=make_tabs_title(tab, compact=right_width < 60),
            border_style="green",
            box=box.ROUNDED,
            height=pane_height,
        ),
    )
    if not chrome:
        return layout
    return Group(make_header(), layout, make_footer(run, width))


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


def maybe_reexec_textual_venv() -> None:
    venv_python = workflow_state.workflow_root() / ".venv" / "bin" / "python"
    current = Path(sys.executable).resolve()
    if venv_python.exists() and current != venv_python.resolve():
        os.execv(str(venv_python), [str(venv_python), __file__, *sys.argv[1:]])


def run_textual_app() -> None:
    try:
        from textual.app import App, ComposeResult
        from textual.widgets import Footer, Header, Static
    except ModuleNotFoundError as exc:
        if exc.name == "textual":
            maybe_reexec_textual_venv()
        raise SystemExit("Textual is required for the live TUI. Run `workflow tui` or install the workflow virtualenv.") from exc

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
            ("q", "quit", "Quit"),
            ("escape", "quit", "Quit"),
            ("r", "reload_runs", "Reload"),
            ("a", "toggle_agent_scope", "Agent scope"),
            ("v", "toggle_agent_view", "Agent view"),
            ("y", "copy_selected_id", "Copy id"),
            ("p", "copy_selected_path", "Copy path"),
            ("ctrl+y", "copy_selected_json", "Copy row"),
            ("tab", "next_tab", "Next section"),
            ("shift+tab", "previous_tab", "Prev section"),
            ("right", "next_tab", "Next section"),
            ("left", "previous_tab", "Prev section"),
            ("j", "move_down", "Down"),
            ("k", "move_up", "Up"),
            ("down", "move_down", "Down"),
            ("up", "move_up", "Up"),
            ("space", "page_down", "Page down"),
            ("pagedown", "page_down", "Page down"),
            ("pageup", "page_up", "Page up"),
            ("g", "top", "Top"),
            ("G", "bottom", "Bottom"),
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
            self.selected_run_id: str | None = None
            self.selected_row_ids: dict[str, str | None] = {tab: None for tab in TABS}
            self.fallback_indexes: dict[str, int] = {tab: 0 for tab in TABS}
            self.dashboard: Static | None = None

        @property
        def tab(self) -> str:
            return TABS[self.tab_index]

        @property
        def agent_scope(self) -> str:
            return AGENT_SCOPES[self.agent_scope_index]

        @property
        def agent_view(self) -> str:
            return AGENT_VIEWS[self.agent_view_index]

        @property
        def selected_run(self) -> dict[str, Any] | None:
            if not self.runs:
                return None
            if self.selected_run_id:
                self.run_index = index_for_key(self.runs, "runs", self.selected_run_id)
            else:
                self.run_index = clamp_index(self.run_index, len(self.runs))
                self.selected_run_id = item_key("runs", self.runs[self.run_index], self.run_index)
            return self.runs[self.run_index]

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield Static(id="dashboard")
            yield Footer()

        def on_mount(self) -> None:
            self.dashboard = self.query_one("#dashboard", Static)
            self.reload_state()
            self.set_interval(1.0, self.reload_state)

        def reload_state(self) -> None:
            self.capture_selection()
            self.runs = load_runs()
            self.restore_selection()
            self.update_dashboard()

        def active_rows(self) -> list[dict[str, Any]]:
            return current_rows_for(
                self.selected_run,
                self.tab,
                self.runs,
                selected_phase_id=self.selected_row_ids.get("phases"),
                agent_scope=self.agent_scope,
            )

        def capture_selection(self) -> None:
            if self.runs:
                self.run_index = clamp_index(self.run_index, len(self.runs))
                self.selected_run_id = item_key("runs", self.runs[self.run_index], self.run_index)
            rows = self.active_rows()
            if rows:
                index = self.run_index if self.tab == "runs" else self.row_index
                index = clamp_index(index, len(rows))
                self.selected_row_ids[self.tab] = item_key(self.tab, rows[index], index)
                self.fallback_indexes[self.tab] = index

        def restore_selection(self) -> None:
            self.run_index = index_for_key(self.runs, "runs", self.selected_run_id)
            if self.runs:
                self.selected_run_id = item_key("runs", self.runs[self.run_index], self.run_index)
            rows = self.active_rows()
            if self.tab == "runs":
                self.row_index = 0
                return
            selected_id = self.selected_row_ids.get(self.tab)
            fallback = self.fallback_indexes.get(self.tab, 0)
            self.row_index = index_for_key(rows, self.tab, selected_id) if selected_id else clamp_index(fallback, len(rows))
            if rows:
                self.selected_row_ids[self.tab] = item_key(self.tab, rows[self.row_index], self.row_index)

        def update_dashboard(self) -> None:
            if self.dashboard is None:
                return
            width = max(0, self.dashboard.size.width, self.size.width - 2)
            height = max(0, self.dashboard.size.height, self.size.height - 3)
            self.dashboard.update(
                render_dashboard(
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
                )
            )

        def action_reload_runs(self) -> None:
            self.reload_state()

        def action_toggle_agent_scope(self) -> None:
            self.capture_selection()
            self.agent_scope_index = (self.agent_scope_index + 1) % len(AGENT_SCOPES)
            self.restore_selection()
            self.update_dashboard()

        def action_toggle_agent_view(self) -> None:
            self.agent_view_index = (self.agent_view_index + 1) % len(AGENT_VIEWS)
            self.update_dashboard()

        def copy_selection(self, mode: str) -> None:
            rows = self.active_rows()
            selected = self.run_index if self.tab == "runs" else self.row_index
            label, value = copy_value_for_selection(self.selected_run, self.tab, rows, selected, mode)
            if not value:
                self.notify(f"No {label or mode} to copy", title="Workflow", severity="warning", timeout=0.8)
                return
            self.copy_to_clipboard(value)
            self.notify(f"Copied {label}", title="Workflow", timeout=0.8)

        def action_copy_selected_id(self) -> None:
            self.copy_selection("id")

        def action_copy_selected_path(self) -> None:
            self.copy_selection("path")

        def action_copy_selected_json(self) -> None:
            self.copy_selection("json")

        def action_next_tab(self) -> None:
            self.capture_selection()
            self.tab_index = (self.tab_index + 1) % len(TABS)
            self.restore_selection()
            self.update_dashboard()

        def action_previous_tab(self) -> None:
            self.capture_selection()
            self.tab_index = (self.tab_index - 1) % len(TABS)
            self.restore_selection()
            self.update_dashboard()

        def action_move_down(self) -> None:
            rows = self.active_rows()
            if self.tab == "runs":
                self.run_index = clamp_index(self.run_index + 1, len(self.runs))
                if self.runs:
                    self.selected_run_id = item_key("runs", self.runs[self.run_index], self.run_index)
            else:
                self.row_index = clamp_index(self.row_index + 1, len(rows))
                if rows:
                    self.selected_row_ids[self.tab] = item_key(self.tab, rows[self.row_index], self.row_index)
                    self.fallback_indexes[self.tab] = self.row_index
            self.update_dashboard()

        def action_move_up(self) -> None:
            if self.tab == "runs":
                self.run_index = clamp_index(self.run_index - 1, len(self.runs))
                if self.runs:
                    self.selected_run_id = item_key("runs", self.runs[self.run_index], self.run_index)
            else:
                self.row_index = max(0, self.row_index - 1)
                rows = self.active_rows()
                if rows:
                    self.selected_row_ids[self.tab] = item_key(self.tab, rows[self.row_index], self.row_index)
                    self.fallback_indexes[self.tab] = self.row_index
            self.update_dashboard()

        def action_top(self) -> None:
            if self.tab == "runs":
                self.run_index = 0
                if self.runs:
                    self.selected_run_id = item_key("runs", self.runs[self.run_index], self.run_index)
            else:
                self.row_index = 0
                rows = self.active_rows()
                if rows:
                    self.selected_row_ids[self.tab] = item_key(self.tab, rows[self.row_index], self.row_index)
                    self.fallback_indexes[self.tab] = self.row_index
            self.update_dashboard()

        def action_bottom(self) -> None:
            rows = self.active_rows()
            if self.tab == "runs":
                self.run_index = max(0, len(self.runs) - 1)
                if self.runs:
                    self.selected_run_id = item_key("runs", self.runs[self.run_index], self.run_index)
            else:
                self.row_index = max(0, len(rows) - 1)
                if rows:
                    self.selected_row_ids[self.tab] = item_key(self.tab, rows[self.row_index], self.row_index)
                    self.fallback_indexes[self.tab] = self.row_index
            self.update_dashboard()

        def page_step(self) -> int:
            return max(5, min(12, self.size.height // 4))

        def action_page_down(self) -> None:
            rows = self.active_rows()
            if self.tab == "runs":
                self.run_index = clamp_index(self.run_index + self.page_step(), len(self.runs))
                if self.runs:
                    self.selected_run_id = item_key("runs", self.runs[self.run_index], self.run_index)
            else:
                self.row_index = clamp_index(self.row_index + self.page_step(), len(rows))
                if rows:
                    self.selected_row_ids[self.tab] = item_key(self.tab, rows[self.row_index], self.row_index)
                    self.fallback_indexes[self.tab] = self.row_index
            self.update_dashboard()

        def action_page_up(self) -> None:
            if self.tab == "runs":
                self.run_index = max(0, self.run_index - self.page_step())
                if self.runs:
                    self.selected_run_id = item_key("runs", self.runs[self.run_index], self.run_index)
            else:
                self.row_index = max(0, self.row_index - self.page_step())
                rows = self.active_rows()
                if rows:
                    self.selected_row_ids[self.tab] = item_key(self.tab, rows[self.row_index], self.row_index)
                    self.fallback_indexes[self.tab] = self.row_index
            self.update_dashboard()

    WorkflowDashboardApp().run()


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
    parser.add_argument("--tab", choices=TABS, default="runs")
    parser.add_argument("--width", type=int, default=100)
    parser.add_argument("--height", type=int, default=28)
    parser.add_argument("--run-index", type=int, default=0)
    parser.add_argument("--row-index", type=int, default=0)
    parser.add_argument("--scroll", type=int, default=0)
    parser.add_argument("--phase-id", help="phase context for phase-scoped agent snapshots")
    parser.add_argument("--phase-index", type=int, help="ordered phase context for phase-scoped agent snapshots")
    parser.add_argument("--agent-scope", choices=AGENT_SCOPES, default="phase")
    parser.add_argument("--agent-view", choices=AGENT_VIEWS, default="live")
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

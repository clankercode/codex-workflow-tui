#!/usr/bin/env python3
"""Compact live monitor for workflow runs and agents.

Provides a low-noise status/watch view for lead agents and humans:
- One compact row per run/agent with status, PID, elapsed time,
  last event, latest output excerpt, tool-call count, token delta.
- Optional --json output.
- Avoids direct run.json dumps and broad log tails during active runs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

import workflow_health
import workflow_state

# Status symbols for compact display
STATUS_SYMBOL: dict[str, str] = {
    "pending": "P",
    "running": "R",
    "blocked": "B",
    "completed": "C",
    "failed": "F",
    "cancelled": "X",
    "paused": "~",
}

# Status colors (ANSI)
STATUS_COLOR: dict[str, str] = {
    "pending": "\033[33m",    # yellow
    "running": "\033[32m",    # green
    "blocked": "\033[31m",    # red
    "completed": "\033[90m",  # gray
    "failed": "\033[31m",     # red
    "cancelled": "\033[90m",  # gray
    "paused": "\033[33m",     # yellow
}
RESET = "\033[0m"


def format_elapsed(seconds: float | None) -> str:
    """Format seconds into a compact human-readable elapsed string."""
    if seconds is None or seconds < 0:
        return "--"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds) // 60}m{int(seconds) % 60:02d}s"
    hours = int(seconds) // 3600
    minutes = (int(seconds) % 3600) // 60
    return f"{hours}h{minutes:02d}m"


def parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO timestamp string to a datetime."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def seconds_since(value: Any, *, now: datetime | None = None) -> float | None:
    """Return seconds elapsed since a timestamp, or None if unavailable."""
    parsed = parse_timestamp(value)
    if parsed is None:
        return None
    anchor = now or datetime.now(UTC)
    delta = anchor - parsed
    return max(0.0, delta.total_seconds())


def truncate(text: str, max_len: int) -> str:
    """Truncate text to max_len, adding ellipsis if needed."""
    text = text.replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "\u2026"


def safe_command_basename(command_preview: str) -> str:
    """Extract a safe basename from a command preview string.

    Avoids printing full command lines (worker prompts are huge).
    Shows only the first token (the binary name) unless --verbose.
    """
    if not command_preview:
        return ""
    first_token = command_preview.split()[0] if command_preview.split() else ""
    return Path(first_token).name or first_token


def last_event_summary(events: list[dict[str, Any]]) -> str:
    """Return a compact summary of the most recent event."""
    if not events:
        return ""
    event = events[-1]
    message = event.get("message", "")
    kind = event.get("kind", "")
    if kind and message:
        return truncate(f"[{kind}] {message}", 60)
    return truncate(message, 60)


def build_run_row(run: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Build a compact status row dict for a single run."""
    run_id = run.get("run_id", "")
    status = run.get("status", "?")
    title = run.get("title", "")
    metrics = run.get("metrics", {})
    agents = run.get("agents", [])
    events = run.get("events", [])
    started_at = run.get("started_at") or run.get("created_at")
    updated_at = run.get("updated_at")

    elapsed = seconds_since(started_at, now=now)

    # Agent status summary: counts by status
    agent_counts: dict[str, int] = {}
    total_tool_calls = 0
    total_tokens = 0
    latest_output = ""
    pids: list[str] = []

    for agent in agents:
        agent_status = agent.get("status", "?")
        agent_counts[agent_status] = agent_counts.get(agent_status, 0) + 1
        total_tool_calls += agent.get("tool_call_count", 0)
        total_tokens += agent.get("token_total", 0)
        output = agent.get("latest_output", "")
        if output and len(output) > len(latest_output):
            latest_output = output
        pid = agent.get("process_id")
        if pid and agent_status == "running":
            pids.append(str(pid))

    # Compact agent summary: "2R/1C/1P" style
    agent_parts = []
    for st in ("running", "pending", "paused", "completed", "failed", "cancelled"):
        count = agent_counts.get(st, 0)
        if count > 0:
            agent_parts.append(f"{count}{STATUS_SYMBOL.get(st, '?')}")
    agent_summary = "/".join(agent_parts) if agent_parts else "0"

    pid_text = ",".join(pids[:3]) if pids else "--"
    if len(pids) > 3:
        pid_text += f"+{len(pids) - 3}"

    last_event = last_event_summary(events)
    output_excerpt = truncate(latest_output, 40) if latest_output else ""

    # Runner type from agent_type or mode
    mode = run.get("mode", "")
    runner_type = mode or (agents[0].get("agent_type", "") if agents else "")

    # Health issues summary
    issues = workflow_health.analyze_run(run)
    critical = sum(1 for i in issues if i.get("severity") == workflow_health.CRITICAL)
    warnings = sum(1 for i in issues if i.get("severity") == workflow_health.WARNING)
    health = "ok" if not critical and not warnings else f"{critical}C/{warnings}W"

    return {
        "run_id": run_id,
        "short_id": run_id.rsplit("-", 1)[-1] if "-" in run_id else run_id[:10],
        "status": status,
        "title": truncate(title, 30),
        "agent_summary": agent_summary,
        "pids": pid_text,
        "elapsed": format_elapsed(elapsed),
        "elapsed_seconds": elapsed,
        "last_event": last_event,
        "output_excerpt": output_excerpt,
        "tool_calls": total_tool_calls,
        "token_delta": total_tokens,
        "runner_type": truncate(runner_type, 16),
        "health": health,
        "updated_at": updated_at or "",
    }


def build_agent_row(agent: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Build a compact status row dict for a single agent."""
    status = agent.get("status", "?")
    name = agent.get("name", agent.get("agent_id", ""))
    pid = agent.get("process_id")
    pgid = agent.get("process_group_id")
    started_at = agent.get("started_at")
    completed_at = agent.get("completed_at")
    tool_calls = agent.get("tool_call_count", 0)
    token_total = agent.get("token_total", 0)
    tokens = agent.get("tokens", {})
    token_input = tokens.get("input", 0)
    token_output = tokens.get("output", 0)
    latest_output = agent.get("latest_output", "")
    summary = agent.get("summary", "")
    agent_type = agent.get("agent_type", "")

    # Elapsed: from started_at to completed_at or now
    elapsed = None
    if started_at:
        end = completed_at or (now or datetime.now(UTC)).isoformat()
        elapsed = seconds_since(started_at, now=parse_timestamp(end) or now)

    pid_text = str(pid) if pid else "--"
    if pgid and pgid != pid:
        pid_text += f"/{pgid}"

    # Token delta: show output - input if both available, else total
    token_delta_text = ""
    if token_output > 0 or token_input > 0:
        token_delta_text = f"+{token_output}/-{token_input}"
    elif token_total > 0:
        token_delta_text = str(token_total)
    else:
        token_delta_text = "--"

    last_output = truncate(latest_output, 50) if latest_output else truncate(summary, 50)

    return {
        "agent_id": agent.get("agent_id", ""),
        "name": truncate(name, 20),
        "status": status,
        "pid": pid_text,
        "elapsed": format_elapsed(elapsed),
        "tool_calls": tool_calls,
        "token_delta": token_delta_text,
        "last_output": last_output,
        "agent_type": truncate(agent_type, 12),
    }


def colorize_status(status: str, text: str) -> str:
    """Wrap text with ANSI color for the given status."""
    color = STATUS_COLOR.get(status, "")
    if not color:
        return text
    return f"{color}{text}{RESET}"


def format_run_row_compact(row: dict[str, Any], *, use_color: bool = True) -> str:
    """Format a run row as a single compact terminal line."""
    status_sym = STATUS_SYMBOL.get(row["status"], "?")
    if use_color:
        status_display = colorize_status(row["status"], f"[{status_sym}]")
    else:
        status_display = f"[{status_sym}]"

    parts = [
        f"{row['short_id']:>10}",
        status_display,
        f"agents={row['agent_summary']:<10}",
        f"pid={row['pids']:<8}",
        f"elapsed={row['elapsed']:<8}",
        f"tools={row['tool_calls']:<5}",
        f"tok={row['token_delta']:<8}",
        f"health={row['health']:<6}",
        f"{row['runner_type']:<16}",
        row["title"],
    ]
    return "  ".join(parts)


def format_agent_row_compact(row: dict[str, Any], *, use_color: bool = True) -> str:
    """Format an agent row as a single compact terminal line."""
    status_sym = STATUS_SYMBOL.get(row["status"], "?")
    if use_color:
        status_display = colorize_status(row["status"], f"[{status_sym}]")
    else:
        status_display = f"[{status_sym}]"

    parts = [
        f"  {row['name']:<20}",
        status_display,
        f"pid={row['pid']:<8}",
        f"elapsed={row['elapsed']:<8}",
        f"tools={row['tool_calls']:<5}",
        f"tok={row['token_delta']:<12}",
        row["last_output"],
    ]
    return "  ".join(parts)


def format_run_header(*, use_color: bool = True) -> str:
    """Return a compact header line for the run table."""
    parts = [
        f"{'run_id':>10}",
        f"{'st':<4}",
        f"{'agents':<17}",
        f"{'pids':<15}",
        f"{'elapsed':<15}",
        f"{'tools':<12}",
        f"{'tokens':<15}",
        f"{'health':<13}",
        f"{'runner':<16}",
        "title",
    ]
    header = "  ".join(parts)
    if use_color:
        return f"\033[1m{header}{RESET}"
    return header


def load_runs_filtered(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Load runs filtered by args (cwd, all, limit)."""
    runs = workflow_state.load_all_runs()
    if getattr(args, "cwd", False):
        cwd = str(Path.cwd().resolve())
        runs = [run for run in runs if str(run.get("cwd", "")) == cwd]
    if not getattr(args, "all", False):
        active = [run for run in runs if run.get("status") in {"running", "blocked", "failed", "paused", "pending"}]
        if active:
            runs = active
    return runs[: args.limit]


def render_status_compact(
    runs: list[dict[str, Any]],
    *,
    use_color: bool = True,
    show_agents: bool = False,
) -> str:
    """Render compact status lines for a list of runs."""
    now = datetime.now(UTC)
    lines: list[str] = []
    lines.append(format_run_header(use_color=use_color))
    for run in runs:
        row = build_run_row(run, now=now)
        lines.append(format_run_row_compact(row, use_color=use_color))
        if show_agents:
            for agent in run.get("agents", []):
                agent_row = build_agent_row(agent, now=now)
                lines.append(format_agent_row_compact(agent_row, use_color=use_color))
    return "\n".join(lines)


def render_status_json(runs: list[dict[str, Any]], *, show_agents: bool = False) -> list[dict[str, Any]]:
    """Render compact status as JSON-serializable dicts."""
    now = datetime.now(UTC)
    result = []
    for run in runs:
        row = build_run_row(run, now=now)
        entry: dict[str, Any] = {
            "run_id": row["run_id"],
            "status": row["status"],
            "title": row["title"],
            "agent_summary": row["agent_summary"],
            "pids": row["pids"],
            "elapsed": row["elapsed"],
            "elapsed_seconds": row["elapsed_seconds"],
            "tool_calls": row["tool_calls"],
            "token_delta": row["token_delta"],
            "runner_type": row["runner_type"],
            "health": row["health"],
            "last_event": row["last_event"],
            "output_excerpt": row["output_excerpt"],
            "updated_at": row["updated_at"],
        }
        if show_agents:
            entry["agents"] = [
                build_agent_row(agent, now=now)
                for agent in run.get("agents", [])
            ]
        result.append(entry)
    return result


def cmd_monitor(args: argparse.Namespace) -> None:
    """Print a compact one-shot status view."""
    runs = load_runs_filtered(args)
    if not runs:
        print("No workflow runs found.")
        return
    if args.json:
        print(json.dumps(render_status_json(runs, show_agents=args.agents), indent=2))
        return
    print(render_status_compact(runs, use_color=not args.no_color, show_agents=args.agents))


def cmd_watch(args: argparse.Namespace) -> None:
    """Continuously refresh compact status at the given interval."""
    use_color = not args.no_color and sys.stdout.isatty()
    try:
        while True:
            runs = load_runs_filtered(args)
            if use_color:
                # Clear screen for live update
                sys.stdout.write("\033[2J\033[H")
            if not runs:
                print("No workflow runs found.")
            elif args.json:
                print(json.dumps(render_status_json(runs, show_agents=args.agents), indent=2))
            else:
                now_str = datetime.now(UTC).strftime("%H:%M:%S UTC")
                print(f"workflow watch  {now_str}  (refresh: {args.interval}s)")
                print()
                print(render_status_compact(runs, use_color=use_color, show_agents=args.agents))
                print()
                print("(Ctrl-C to stop)")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative number")
    return parsed


def build_monitor_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="compact workflow monitor")
    sub = parser.add_subparsers(dest="command", required=True)

    monitor = sub.add_parser("monitor", help="compact one-shot status view")
    monitor.add_argument("--limit", type=positive_int, default=12)
    monitor.add_argument("--cwd", action="store_true", help="only show runs for the current cwd")
    monitor.add_argument("--all", action="store_true", help="include completed runs")
    monitor.add_argument("--json", action="store_true")
    monitor.add_argument("--no-color", action="store_true")
    monitor.add_argument("--agents", action="store_true", help="show per-agent detail rows")
    monitor.set_defaults(func=cmd_monitor)

    watch = sub.add_parser("watch", help="continuously refresh compact status")
    watch.add_argument("--limit", type=positive_int, default=12)
    watch.add_argument("--cwd", action="store_true", help="only show runs for the current cwd")
    watch.add_argument("--all", action="store_true", help="include completed runs")
    watch.add_argument("--json", action="store_true")
    watch.add_argument("--no-color", action="store_true")
    watch.add_argument("--agents", action="store_true", help="show per-agent detail rows")
    watch.add_argument("--interval", type=nonnegative_float, default=5.0, help="refresh interval in seconds")
    watch.set_defaults(func=cmd_watch)

    return parser


def main() -> None:
    args = build_monitor_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

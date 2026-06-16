"""Small live-telemetry helpers for workflow_tui."""

from __future__ import annotations

from datetime import datetime
from typing import Any

TODO_TOOL_NAMES = {"todo", "todowrite", "todo_write", "todo-write", "TodoWrite"}
THINKING_TYPES = {"thinking", "reasoning", "reasoning_delta", "thought"}
TODO_STATUS_MARKERS = {
    "complete": "[✓]",
    "completed": "[✓]",
    "done": "[✓]",
    "success": "[✓]",
    "in_progress": "[~]",
    "in-progress": "[~]",
    "running": "[~]",
    "active": "[~]",
    "pending": "[ ]",
    "todo": "[ ]",
    "open": "[ ]",
    "blocked": "[!]",
    "error": "[!]",
    "failed": "[!]",
}


def event_sources(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Return provider payload dictionaries in likely specificity order."""
    part = event.get("part") if isinstance(event.get("part"), dict) else {}
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    return [source for source in (state, part, item, event) if source]


def normalize_tool_name(value: Any) -> str:
    return str(value or "").replace("-", "_").lower()


def run_duration_text(
    run: dict[str, Any],
    parse_datetime: Any,
    format_duration: Any,
    terminal_statuses: set[str],
    now: datetime | None,
    snapshot_now: Any,
) -> str:
    """Return a human-readable duration for a workflow run."""
    start = parse_datetime(run.get("started_at") or run.get("created_at"))
    if start is None:
        return ""
    if str(run.get("status", "")) in terminal_statuses:
        end = parse_datetime(run.get("completed_at") or run.get("updated_at"))
    else:
        effective_now = now or snapshot_now() or datetime.now(start.tzinfo)
        end = effective_now.astimezone(start.tzinfo) if effective_now.tzinfo else effective_now.replace(tzinfo=start.tzinfo)
    if not end:
        return ""
    seconds = max(0.0, (end - start).total_seconds())
    if seconds == 0:
        return "0s"
    return format_duration(seconds) or ""


def agent_duration_text(
    agent: dict[str, Any],
    parse_datetime: Any,
    parse_seconds: Any,
    format_duration: Any,
    terminal_statuses: set[str],
    now: datetime | None,
    snapshot_now: Any,
) -> str:
    """Return a human-readable elapsed or terminal duration for an agent."""
    start = parse_datetime(agent.get("started_at"))
    if start is None:
        epoch = parse_seconds(agent.get("started_epoch"))
        if epoch is not None:
            start = datetime.fromtimestamp(epoch).astimezone()
    if start is None:
        return format_duration(agent.get("duration_seconds")) or ""
    if str(agent.get("status", "")) in terminal_statuses:
        end = parse_datetime(agent.get("completed_at") or agent.get("updated_at"))
        if end is None:
            return format_duration(agent.get("duration_seconds")) or ""
    else:
        effective_now = now or snapshot_now() or datetime.now(start.tzinfo)
        end = effective_now.astimezone(start.tzinfo) if effective_now.tzinfo else effective_now.replace(tzinfo=start.tzinfo)
    seconds = max(0.0, (end - start).total_seconds())
    if seconds == 0:
        return "0s"
    return format_duration(seconds) or ""


def finished_ago_text(
    entity: dict[str, Any],
    parse_datetime: Any,
    format_duration: Any,
    terminal_statuses: set[str],
    now: datetime | None,
    snapshot_now: Any,
) -> str:
    """Return a human-readable 'finished Nm ago' for terminal entities."""
    if str(entity.get("status", "")) not in terminal_statuses:
        return ""
    end = parse_datetime(entity.get("completed_at") or entity.get("updated_at"))
    if end is None:
        return ""
    effective_now = now or snapshot_now() or datetime.now(end.tzinfo)
    reference = effective_now.astimezone(end.tzinfo) if end.tzinfo else effective_now.replace(tzinfo=end.tzinfo)
    seconds = max(0.0, (reference - end).total_seconds())
    if seconds == 0:
        return "just finished"
    duration = format_duration(seconds) or ""
    if not duration:
        return ""
    return f"{duration} ago"


def agent_identity_text(agent: dict[str, Any]) -> str:
    """Return process identity string for an agent.

    External workers (has process_id):  "pid:12345" or "pgid:12345"
    Native subagents (has thread_id and agent_type == "native-subagent"): "thread:subagent-security"
    """
    if agent.get("process_id") is not None:
        return f"pid:{agent['process_id']}"
    if agent.get("process_group_id") is not None:
        return f"pgid:{agent['process_group_id']}"
    if agent.get("agent_type") == "native-subagent" and agent.get("thread_id"):
        return f"thread:{agent['thread_id']}"
    return ""


def running_agent_summaries(run: dict[str, Any], now_epoch: float) -> list[dict[str, Any]]:
    """Return compact state-derived summaries for active agents."""
    summaries: list[dict[str, Any]] = []
    for agent in [item for item in run.get("agents", []) if item.get("status") == "running"]:
        started = agent.get("started_epoch")
        if started is None and agent.get("started_at"):
            try:
                started = datetime.fromisoformat(str(agent["started_at"]).replace("Z", "+00:00")).timestamp()
            except ValueError:
                started = None
        summary = {"name": agent.get("name", ""), "agent_id": agent.get("agent_id", "")}
        identity = agent_identity_text(agent)
        if identity:
            summary["identity_text"] = identity
        if started is not None:
            summary["elapsed_seconds"] = round(max(0.0, now_epoch - float(started)), 1)
        summaries.append(summary)
    return summaries


def running_agents_inline_text(run: dict[str, Any], limit: int = 3) -> str:
    """Return a one-line active-worker preview for the runs overview."""
    agents = [item for item in run.get("agents", []) if item.get("status") == "running"]
    labels = []
    for agent in agents[:limit]:
        name = str(agent.get("name") or agent.get("agent_id") or "agent")
        identity = agent_identity_text(agent)
        if identity:
            labels.append(f"{name} ({identity})")
        else:
            labels.append(name)
    if len(agents) > limit:
        labels.append(f"+{len(agents) - limit}")
    return ", ".join(labels)


def todo_status_text(todos: list[dict[str, str]]) -> str:
    lines = []
    for todo in todos[:8]:
        status = str(todo.get("status") or "todo")
        marker = TODO_STATUS_MARKERS.get(status.lower(), "[ ]")
        content = str(todo.get("content") or "")
        lines.append(f"{marker} {content}"[:220])
    return "\n".join(lines)


def todo_items_from_event(event: dict[str, Any]) -> list[dict[str, str]]:
    """Extract todo rows from provider TodoWrite-like tool events."""
    sources = event_sources(event)
    names = {normalize_tool_name(source.get("tool") or source.get("name") or source.get("type")) for source in sources}
    if not names.intersection({normalize_tool_name(name) for name in TODO_TOOL_NAMES}):
        return []
    for source in sources:
        input_value = source.get("input") or source.get("arguments") or source.get("args")
        todos = input_value.get("todos") if isinstance(input_value, dict) else input_value
        if not isinstance(todos, list):
            continue
        rows: list[dict[str, str]] = []
        for todo in todos:
            if isinstance(todo, dict):
                content = todo.get("content") or todo.get("task") or todo.get("title") or todo.get("text")
                status = todo.get("status") or todo.get("state") or ""
            else:
                content, status = todo, ""
            if content:
                rows.append({"content": str(content), "status": str(status)})
        if rows:
            return rows
    return []


def thinking_text_from_event(event: dict[str, Any]) -> str:
    """Return real provider thinking text when an event actually carries it."""
    event_type = str(event.get("type", ""))
    for source in event_sources(event):
        source_type = str(source.get("type", ""))
        if event_type not in THINKING_TYPES and source_type not in THINKING_TYPES:
            continue
        for key in ("text", "content", "reasoning", "thinking"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""

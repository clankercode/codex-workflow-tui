"""Activity parsing, JSONL tail reading, token stats, and agent data resolution."""

from __future__ import annotations

import codecs
import json
import math
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import workflow_state
import workflow_tui_live

TAIL_BYTES = 96_000
MAX_PREVIEW_CHARS = 2_400
MAX_ARTIFACT_PREVIEW_BYTES = 24_000
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

# ---------------------------------------------------------------------------
# Small shared utilities (used by both activity and render code)
# ---------------------------------------------------------------------------


def compact_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ": "))


def compact_path(path: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(path) <= width:
        return path
    if width <= 4:
        return "\u2026"[:width]
    return "\u2026" + path[-(width - 1) :]


def display_path_value(path: Any, width: int = 42) -> str:
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


# ---------------------------------------------------------------------------
# Duration formatting utilities
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Event kind inference and styling
# ---------------------------------------------------------------------------


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


def event_kind_text(event: dict[str, Any]) -> Any:
    """Return styled event kind text for the TUI."""
    from rich.text import Text

    kind = infer_event_kind(event)
    style = EVENT_STYLE_BY_KIND.get(kind, "bright_black")
    if kind.startswith("agent"):
        style = EVENT_STYLE_BY_KIND.get(kind, "magenta")
    elif kind.startswith("phase"):
        style = EVENT_STYLE_BY_KIND.get(kind, "cyan")
    return Text(kind, style=f"bold {style}")


# ---------------------------------------------------------------------------
# File tail reading
# ---------------------------------------------------------------------------


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
    return "\u2026" + clean[-(limit - 1) :]


# ---------------------------------------------------------------------------
# Token usage tracking
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Throughput estimation and smoothed counter display
# ---------------------------------------------------------------------------


def compute_tokens_per_second(token_total: int, started_at: Any = None, now_epoch: float | None = None) -> float | None:
    """Return estimated tokens-per-second when timing and token data are available."""
    if token_total <= 0 or started_at is None:
        return None
    start_epoch = timestamp_epoch(started_at)
    if start_epoch <= 0:
        return None
    current = now_epoch if now_epoch is not None else reference_epoch()
    elapsed = current - start_epoch
    if elapsed < 1.0:
        return None
    return token_total / elapsed


def smooth_counter_display(base_value: int, rate: float | None, is_live: bool, now_epoch: float | None = None, base_epoch: float | None = None) -> int:
    """Return an interpolated counter value for live display, or the exact value when not live."""
    if not is_live or rate is None or rate <= 0:
        return base_value
    current = now_epoch if now_epoch is not None else reference_epoch()
    base = base_epoch if base_epoch is not None else current
    elapsed = current - base
    if elapsed <= 0:
        return base_value
    return base_value + int(rate * elapsed)


def _format_rate(rate: float) -> str:
    """Format a tokens-per-second rate with compact units."""
    if rate >= 1_000_000:
        return f"{compact_decimal(rate / 1_000_000, 1)}m"
    if rate >= 1_000:
        return f"{compact_decimal(rate / 1_000, 1)}k"
    return compact_decimal(rate, 1)


def format_token_total_with_throughput(
    tokens: dict[str, Any],
    started_at: Any = None,
    is_live: bool = False,
    now_epoch: float | None = None,
) -> str:
    """Render token totals with split upstream/downstream throughput rate when available."""
    base_label = format_token_total(tokens)
    if base_label == "unknown":
        return base_label
    if is_live:
        input_total = token_value(tokens.get("input")) + token_value(tokens.get("cached_input"))
        output_total = token_value(tokens.get("output")) + token_value(tokens.get("reasoning"))
        if input_total > 0 or output_total > 0:
            up_rate = compute_tokens_per_second(input_total, started_at, now_epoch)
            down_rate = compute_tokens_per_second(output_total, started_at, now_epoch)
            up_text = _format_rate(up_rate) if up_rate is not None and up_rate > 0 else None
            down_text = _format_rate(down_rate) if down_rate is not None and down_rate > 0 else None
            if up_text or down_text:
                parts = []
                if up_text:
                    parts.append(f"up {up_text}/s")
                if down_text:
                    parts.append(f"down {down_text}/s")
                return f"{base_label} ({' '.join(parts)})"
        else:
            token_total = token_value(tokens.get("total"))
            if token_total > 0:
                rate = compute_tokens_per_second(token_total, started_at, now_epoch)
                if rate is not None and rate > 0:
                    displayed = smooth_counter_display(token_total, rate, True, now_epoch)
                    per_sec = compact_decimal(rate, 1)
                    return f"{displayed} ({per_sec}/s)"
    return base_label


# ---------------------------------------------------------------------------
# Timestamp utilities
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Tool call summarization and JSONL activity parsing
# ---------------------------------------------------------------------------


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
    return " \u00b7 ".join(piece for piece in pieces if piece)[:220]


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
        kimi_match = re.match(r"^\u2022\s+Used\s+(.+)$", line)
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


# ---------------------------------------------------------------------------
# Agent path resolution and artifact reading
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Agent activity aggregation
# ---------------------------------------------------------------------------


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

    status = str(agent.get("status", ""))
    if status in workflow_state.TERMINAL_STATUS_VALUES:
        has_visible_output = bool(str(activity.get("latest_output") or "").strip())
        has_summary = bool(str(agent.get("summary") or "").strip())
        has_result = bool(str(agent.get("result") or "").strip())
        has_stop_result = isinstance(agent.get("stop_result"), dict)
        if not has_visible_output and not has_result and (not has_summary or has_stop_result):
            fallback_parts: list[str] = []
            if activity.get("latest_thinking"):
                fallback_parts.append(f"[thinking] {activity['latest_thinking']}")
            if activity.get("tool_calls"):
                fallback_parts.append(f"[tools] {'; '.join(activity['tool_calls'][-3:])}")
            if agent.get("latest_output"):
                fallback_parts.append(f"[last output] {agent['latest_output']}")
            stop_result = agent.get("stop_result")
            if isinstance(stop_result, dict) and stop_result.get("reason"):
                fallback_parts.append(f"[termination] {stop_result['reason']}")
            if agent.get("summary"):
                fallback_parts.append(f"[status] {agent['summary']}")
            exit_code = agent.get("exit_code")
            if exit_code is not None:
                fallback_parts.append(f"[exit code] {exit_code}")
            if fallback_parts:
                activity["fallback_output"] = trim_preview("\n".join(fallback_parts))
            elif activity.get("parse_errors"):
                activity["fallback_output"] = f"[no readable output; {activity['parse_errors']} parse errors in transcript]"

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
        "latest_output_agent": next((str(activity.get("name") or activity.get("agent_id") or "") for activity in reversed(ordered_activities) if activity.get("latest_output")), ""),
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


def reference_epoch() -> float:
    """Return a stable reference time for duration calculations."""
    snapshot_now = os.environ.get("WORKFLOW_TUI_SNAPSHOT_NOW")
    if snapshot_now:
        text = snapshot_now.strip()
        if text:
            timestamp = text[:-1] + "+00:00" if text.endswith("Z") else text
            try:
                return datetime.fromisoformat(timestamp).timestamp()
            except ValueError:
                pass
    return __import__("time").time()


def has_event_rollover(run: dict[str, Any]) -> bool:
    """Return True when the run's event log has lost old events due to the bounded history."""
    for event in run.get("events", []):
        if event.get("kind") == "event-log" and event.get("operation") == "rollover":
            return True
    return False

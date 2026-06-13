#!/usr/bin/env python3
"""Manage durable JSON state for workflow runs."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - this workflow system is Unix-oriented today.
    fcntl = None  # type: ignore[assignment]

_FCNTL_WARNING_EMITTED = False

SCHEMA_VERSION = 1
DEFAULT_ROOT = Path.home() / ".agents" / "workflow-system"
STATUS_VALUES = {"pending", "running", "blocked", "completed", "failed", "cancelled", "paused"}
TERMINAL_STATUS_VALUES = {"completed", "failed", "cancelled"}


class AbortMutation(Exception):
    """Return a mutation result while leaving the run file untouched."""

    def __init__(self, result: Any) -> None:
        super().__init__("workflow mutation aborted without save")
        self.result = result


def workflow_root() -> Path:
    return Path(os.environ.get("WORKFLOW_HOME", DEFAULT_ROOT)).expanduser()


def state_root() -> Path:
    return Path(os.environ.get("WORKFLOW_STATE_DIR", workflow_root() / "state")).expanduser()


def runs_root() -> Path:
    return state_root() / "runs"


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def slugify(text: str, fallback: str = "workflow") -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return slug[:48] or fallback


def short_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def run_id(title: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"wf-{stamp}-{slugify(title)}-{uuid.uuid4().hex[:6]}"


def run_dir(identifier: str) -> Path:
    candidate = Path(identifier).expanduser()
    if candidate.is_dir():
        return candidate
    if candidate.is_file():
        return candidate.parent
    return runs_root() / identifier


def run_file(identifier: str) -> Path:
    candidate = Path(identifier).expanduser()
    if candidate.is_file():
        return candidate
    return run_dir(identifier) / "run.json"


def read_text_arg(value: str | None, file_value: str | None) -> str:
    if file_value:
        return Path(file_value).expanduser().read_text(encoding="utf-8").strip()
    return value or ""


def load_run(identifier: str) -> dict[str, Any]:
    path = run_file(identifier)
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)
    try:
        dir_fd = os.open(path.parent, getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def save_run(data: dict[str, Any]) -> Path:
    data["updated_at"] = now()
    refresh_metrics(data)
    path = Path(data["paths"]["run_json"]).expanduser()
    atomic_write(path, data)
    return path


@contextlib.contextmanager
def exclusive_lock(lock_path: Path) -> Any:
    """Hold an advisory lock for one workflow run directory.

    Lock boundary: mutations that load+save run.json should wrap the entire
    read-modify-write in this lock. Writes outside mutate_run (init, verify log
    files) should acquire the per-run lock before touching run state or logs.
    """
    global _FCNTL_WARNING_EMITTED
    if fcntl is None and not _FCNTL_WARNING_EMITTED:
        _FCNTL_WARNING_EMITTED = True
        sys.stderr.write("warning: fcntl unavailable; workflow locking is disabled\n")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def mutate_run(identifier: str, mutator: Any) -> tuple[dict[str, Any], Any, Path]:
    """Load, mutate, and save a run under its per-run lock."""
    path = run_file(identifier)
    with exclusive_lock(path.parent / ".lock"):
        data = load_run(identifier)
        try:
            result = mutator(data)
        except AbortMutation as exc:
            return data, exc.result, path
        saved = save_run(data)
    return data, result, saved


def refresh_metrics(data: dict[str, Any]) -> None:
    agents = data.get("agents", [])
    phases = data.get("phases", [])
    metrics = data.setdefault("metrics", {})
    metrics["agents_total"] = len(agents)
    metrics["phases_total"] = len(phases)
    metrics["agents_by_status"] = count_by_status(agents)
    metrics["phases_by_status"] = count_by_status(phases)
    checks = data.get("checks", [])
    metrics["checks_total"] = len(checks)
    metrics["checks_by_status"] = count_by_status(checks)


def count_by_status(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        status = item.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def add_event(run_data: dict[str, Any], level: str, message: str, **extra: Any) -> dict[str, Any]:
    event_ts = now()
    event = {
        "event_id": short_id("evt"),
        "ts": event_ts,
        "level": level,
        "message": message,
    }
    event.update({key: value for key, value in extra.items() if value not in (None, "", {})})
    events = run_data.setdefault("events", [])
    events.append(event)
    if len(events) > 250:
        # Drop any previous rollover marker so it does not consume a retention slot.
        events[:] = [e for e in events if not (e.get("kind") == "event-log" and e.get("operation") == "rollover")]
        events[:] = events[-249:]
        rollover = {
            "event_id": short_id("evt"),
            "ts": now(),
            "level": "warning",
            "message": "event log rolled over; oldest events discarded",
            "kind": "event-log",
            "operation": "rollover",
        }
        events.append(rollover)
    run_data["last_activity_at"] = event_ts
    return event


def validate_status(status: str) -> str:
    if status not in STATUS_VALUES:
        raise SystemExit(f"invalid status {status!r}; expected one of {sorted(STATUS_VALUES)}")
    return status


def find_item(items: list[dict[str, Any]], key: str, value: str) -> dict[str, Any]:
    for item in items:
        if item.get(key) == value:
            return item
    raise SystemExit(f"no item with {key}={value!r}")


def ensure_unique(items: list[dict[str, Any]], key: str, value: str) -> None:
    if any(item.get(key) == value for item in items):
        raise SystemExit(f"duplicate {key}={value!r}")


def cmd_init(args: argparse.Namespace) -> None:
    rid = run_id(args.title)
    directory = runs_root() / rid
    artifacts = directory / "artifacts"
    logs = directory / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(exist_ok=True)
    logs.mkdir(exist_ok=True)
    data: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": rid,
        "title": args.title,
        "prompt": read_text_arg(args.prompt, args.prompt_file),
        "cwd": str(Path(args.cwd).expanduser().resolve()),
        "mode": args.mode,
        "status": "running",
        "tags": args.tag or [],
        "created_at": now(),
        "updated_at": now(),
        "coordinator": {"tool": getattr(args, "coordinator_tool", None) or "codex-direct", "thread_id": args.thread_id or None},
        "paths": {
            "run_dir": str(directory),
            "run_json": str(directory / "run.json"),
            "artifacts_dir": str(artifacts),
            "logs_dir": str(logs),
        },
        "phases": [],
        "agents": [],
        "events": [],
        "decisions": [],
        "artifacts": [],
        "checks": [],
        "control": {},
        "metrics": {},
    }
    add_event(data, "info", "workflow initialized", kind="workflow", operation="initialized", source="workflow_state.init")
    with exclusive_lock(directory / ".lock"):
        save_run(data)
    print(json.dumps({"run_id": rid, "path": data["paths"]["run_json"]}, indent=2))


def cmd_add_phase(args: argparse.Namespace) -> None:
    initial_status = validate_status(args.status)

    def mutator(data: dict[str, Any]) -> dict[str, Any]:
        phase_id = args.phase_id or short_id("phase")
        phases = data.setdefault("phases", [])
        ensure_unique(phases, "phase_id", phase_id)
        created_at = now()
        phase = {
            "phase_id": phase_id,
            "name": args.name,
            "goal": args.goal or "",
            "status": initial_status,
            "created_at": created_at,
            "started_at": created_at if args.status in {"running", "completed", "failed", "cancelled"} else None,
            "completed_at": created_at if args.status in {"completed", "failed", "cancelled"} else None,
            "agent_ids": [],
        }
        phases.append(phase)
        add_event(
            data,
            "info",
            f"phase added: {args.name}",
            kind="phase",
            operation="added",
            source="workflow_state.add_phase",
            phase_id=phase["phase_id"],
            data={"name": phase["name"], "status": phase["status"]},
        )
        return phase

    _, phase, path = mutate_run(args.run, mutator)
    print(json.dumps({"phase_id": phase["phase_id"], "path": str(path)}, indent=2))


def cmd_update_phase(args: argparse.Namespace) -> None:
    new_status: str | None = validate_status(args.status) if args.status else None

    def mutator(data: dict[str, Any]) -> dict[str, Any]:
        phase = find_item(data.setdefault("phases", []), "phase_id", args.phase)
        if new_status:
            phase["status"] = new_status
            if args.status == "running" and not phase.get("started_at"):
                phase["started_at"] = now()
            if args.status in {"completed", "failed", "cancelled"}:
                phase["completed_at"] = now()
            else:
                phase["completed_at"] = None
        if args.goal is not None:
            phase["goal"] = args.goal
        add_event(
            data,
            "info",
            f"phase updated: {phase['name']}",
            kind="phase",
            operation="updated",
            source="workflow_state.update_phase",
            phase_id=phase["phase_id"],
            data={"name": phase["name"], "status": phase["status"]},
        )
        return phase

    _, phase, _ = mutate_run(args.run, mutator)
    print(json.dumps(phase, indent=2))


def cmd_add_agent(args: argparse.Namespace) -> None:
    initial_status = validate_status(args.status)

    def mutator(data: dict[str, Any]) -> dict[str, Any]:
        agent_id = args.agent_id or short_id("agent")
        agents = data.setdefault("agents", [])
        ensure_unique(agents, "agent_id", agent_id)
        created_at = now()
        agent = {
            "agent_id": agent_id,
            "phase_id": args.phase,
            "name": args.name,
            "role": args.role or "",
            "agent_type": args.agent_type or "",
            "status": initial_status,
            "prompt": read_text_arg(args.prompt, args.prompt_file),
            "cwd": str(Path(args.cwd).expanduser().resolve()) if args.cwd else data.get("cwd"),
            "model": args.model or "",
            "thread_id": args.thread_id or "",
            "process_id": args.process_id,
            "write_scope": args.write_scope or [],
            "jsonl_path": args.jsonl_path or "",
            "log_path": args.log_path or "",
            "output_path": args.output_path or "",
            "summary": "",
            "result": "",
            "exit_code": None,
            "created_at": created_at,
            "started_at": created_at if args.status in {"running", "completed", "failed", "cancelled"} else None,
            "completed_at": created_at if args.status in {"completed", "failed", "cancelled"} else None,
            "updated_at": created_at,
        }
        agents.append(agent)
        if args.phase:
            phase = find_item(data.setdefault("phases", []), "phase_id", args.phase)
            phase.setdefault("agent_ids", []).append(agent["agent_id"])
        add_event(
            data,
            "info",
            f"agent added: {args.name}",
            kind="agent",
            operation="added",
            source="workflow_state.add_agent",
            phase_id=args.phase,
            agent_id=agent["agent_id"],
            data={
                "name": agent["name"],
                "status": agent["status"],
                "agent_type": agent.get("agent_type", ""),
                "model": agent.get("model", ""),
            },
        )
        return agent

    _, agent, path = mutate_run(args.run, mutator)
    print(json.dumps({"agent_id": agent["agent_id"], "path": str(path)}, indent=2))


def cmd_update_agent(args: argparse.Namespace) -> None:
    new_status: str | None = validate_status(args.status) if args.status else None

    def mutator(data: dict[str, Any]) -> dict[str, Any]:
        agent = find_item(data.setdefault("agents", []), "agent_id", args.agent)
        if new_status:
            agent["status"] = new_status
            if args.status == "running" and not agent.get("started_at"):
                agent["started_at"] = now()
            if args.status in {"completed", "failed", "cancelled"}:
                agent["completed_at"] = now()
            else:
                agent["completed_at"] = None
        for attr in ("summary", "result", "thread_id", "jsonl_path", "log_path", "output_path", "model"):
            value = getattr(args, attr)
            file_value = getattr(args, f"{attr}_file", None)
            if value is not None or file_value is not None:
                agent[attr] = read_text_arg(value, file_value)
        if args.exit_code is not None:
            agent["exit_code"] = args.exit_code
        agent["updated_at"] = now()
        add_event(
            data,
            "info",
            f"agent updated: {agent['name']}",
            kind="agent",
            operation="updated",
            source="workflow_state.update_agent",
            phase_id=agent.get("phase_id"),
            agent_id=agent["agent_id"],
            data={
                "name": agent["name"],
                "status": agent["status"],
                "agent_type": agent.get("agent_type", ""),
                "model": agent.get("model", ""),
                "exit_code": agent.get("exit_code"),
            },
        )
        return agent

    _, agent, _ = mutate_run(args.run, mutator)
    print(json.dumps(agent, indent=2))


def cmd_event(args: argparse.Namespace) -> None:
    payload = json.loads(args.data_json) if args.data_json else None

    def mutator(data: dict[str, Any]) -> dict[str, Any]:
        return add_event(
            data,
            args.level,
            read_text_arg(args.message, args.message_file),
            kind=args.kind or "run_note",
            operation=args.operation or "note",
            source=args.source or "workflow_state.event",
            phase_id=args.phase,
            agent_id=args.agent,
            data=payload,
        )

    _, event, _ = mutate_run(args.run, mutator)
    print(json.dumps(event, indent=2))


def cmd_decision(args: argparse.Namespace) -> None:
    def mutator(data: dict[str, Any]) -> dict[str, Any]:
        decision = {
            "decision_id": short_id("dec"),
            "ts": now(),
            "title": args.title,
            "rationale": read_text_arg(args.rationale, args.rationale_file),
            "made_by": args.made_by,
        }
        data.setdefault("decisions", []).append(decision)
        add_event(
            data,
            "info",
            f"decision recorded: {args.title}",
            kind="decision",
            operation="recorded",
            source="workflow_state.decision",
            data={"title": args.title, "made_by": args.made_by},
        )
        return decision

    _, decision, _ = mutate_run(args.run, mutator)
    print(json.dumps(decision, indent=2))


def cmd_artifact(args: argparse.Namespace) -> None:
    def mutator(data: dict[str, Any]) -> dict[str, Any]:
        artifact = {
            "artifact_id": short_id("art"),
            "ts": now(),
            "kind": args.kind,
            "title": args.title or Path(args.path).name,
            "path": str(Path(args.path).expanduser()),
            "agent_id": args.agent,
            "phase_id": args.phase,
        }
        data.setdefault("artifacts", []).append(artifact)
        add_event(
            data,
            "info",
            f"artifact recorded: {artifact['title']}",
            kind="artifact",
            operation="recorded",
            source="workflow_state.artifact",
            phase_id=args.phase,
            agent_id=args.agent,
            data={"title": artifact["title"], "kind": artifact["kind"], "path": artifact["path"]},
        )
        return artifact

    _, artifact, _ = mutate_run(args.run, mutator)
    print(json.dumps(artifact, indent=2))


def cmd_set_status(args: argparse.Namespace) -> None:
    target_status = validate_status(args.status)
    if target_status in {"completed", "failed"} and not (args.force or args.allow_recovery):
        raise SystemExit(
            f"setting status to {target_status!r} requires --force or --allow-recovery; "
            "use `wf done` for guarded completion"
        )

    def mutator(data: dict[str, Any]) -> None:
        data["status"] = target_status
        add_event(
            data,
            "info",
            f"run status set to {args.status}",
            kind="run",
            operation="status_set",
            source="workflow_state.set_status",
            data={"status": args.status},
        )

    data, _, _ = mutate_run(args.run, mutator)
    print(json.dumps({"run_id": data["run_id"], "status": data["status"]}, indent=2))


def active_status(status: Any) -> bool:
    return str(status or "") in {"pending", "running", "blocked", "paused"}


def terminate_worker(agent: dict[str, Any]) -> dict[str, Any]:
    """Best-effort terminate a worker process or process group recorded in state."""
    group_id = agent.get("process_group_id")
    process_id = agent.get("process_id")
    target = group_id or process_id
    if not target:
        return {"sent": False, "reason": "no recorded process id"}
    try:
        if group_id:
            os.killpg(int(group_id), signal.SIGTERM)
            return {"sent": True, "target": "process_group", "pid": int(group_id), "signal": "SIGTERM"}
        os.kill(int(process_id), signal.SIGTERM)
        return {"sent": True, "target": "process", "pid": int(process_id), "signal": "SIGTERM"}
    except ProcessLookupError:
        return {"sent": False, "target": "process_group" if group_id else "process", "pid": int(target), "reason": "not found"}
    except PermissionError:
        return {"sent": False, "target": "process_group" if group_id else "process", "pid": int(target), "reason": "permission denied"}
    except OSError as exc:
        return {"sent": False, "target": "process_group" if group_id else "process", "pid": int(target), "reason": str(exc)}


def cmd_pause(args: argparse.Namespace) -> None:
    def mutator(data: dict[str, Any]) -> None:
        previous_status = data.get("status", "")
        if previous_status in TERMINAL_STATUS_VALUES:
            raise AbortMutation({"run_id": data["run_id"], "status": previous_status, "changed": False})
        control = data.setdefault("control", {})
        control["paused"] = True
        control["pause_requested_at"] = now()
        control["pause_reason"] = args.reason or "operator requested pause"
        data["status"] = "paused"
        add_event(
            data,
            "warning",
            "workflow paused",
            kind="run",
            operation="paused",
            source="workflow_state.pause",
            data={"previous_status": previous_status, "reason": control["pause_reason"]},
        )

    data, result, _ = mutate_run(args.run, mutator)
    payload = result or {"run_id": data["run_id"], "status": data["status"], "changed": True}
    print(json.dumps(payload, indent=2))


def cmd_resume(args: argparse.Namespace) -> None:
    def mutator(data: dict[str, Any]) -> None:
        previous_status = data.get("status", "")
        control = data.setdefault("control", {})
        if control.get("stop_requested") or previous_status in TERMINAL_STATUS_VALUES:
            raise AbortMutation({"run_id": data["run_id"], "status": previous_status, "changed": False})
        control["paused"] = False
        control["resumed_at"] = now()
        control["resume_reason"] = args.reason or "operator requested resume"
        if previous_status == "paused":
            data["status"] = "running"
        for agent in data.get("agents", []):
            if agent.get("status") == "paused" and not agent.get("process_id"):
                agent["status"] = "pending"
                agent["updated_at"] = now()
        add_event(
            data,
            "info",
            "workflow resumed",
            kind="run",
            operation="resumed",
            source="workflow_state.resume",
            data={"previous_status": previous_status, "reason": control["resume_reason"]},
        )

    data, result, _ = mutate_run(args.run, mutator)
    payload = result or {"run_id": data["run_id"], "status": data["status"], "changed": True}
    print(json.dumps(payload, indent=2))


def cmd_stop(args: argparse.Namespace) -> None:
    def mutator(data: dict[str, Any]) -> dict[str, Any]:
        timestamp = now()
        previous_status = data.get("status", "")
        control = data.setdefault("control", {})
        control["stop_requested"] = True
        control["stopped_at"] = timestamp
        control["stop_reason"] = args.reason or "operator requested stop"
        stopped_agents: list[dict[str, Any]] = []
        for agent in data.get("agents", []):
            if active_status(agent.get("status")):
                termination = terminate_worker(agent) if args.terminate else {"sent": False, "reason": "termination disabled"}
                agent["status"] = "cancelled"
                agent["completed_at"] = timestamp
                agent["updated_at"] = timestamp
                agent["summary"] = agent.get("summary") or "cancelled by operator"
                agent["stop_result"] = termination
                stopped_agents.append({"agent_id": agent.get("agent_id", ""), **termination})
        for phase in data.get("phases", []):
            if active_status(phase.get("status")):
                phase["status"] = "cancelled"
                phase["completed_at"] = timestamp
        data["status"] = "cancelled"
        add_event(
            data,
            "warning",
            "workflow stopped",
            kind="run",
            operation="stopped",
            source="workflow_state.stop",
            data={
                "previous_status": previous_status,
                "reason": control["stop_reason"],
                "terminated_agents": stopped_agents,
            },
        )
        return {"run_id": data["run_id"], "status": data["status"], "terminated_agents": stopped_agents, "changed": True}

    _, result, _ = mutate_run(args.run, mutator)
    print(json.dumps(result, indent=2))


def load_all_runs() -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for path in runs_root().glob("*/run.json"):
        try:
            runs.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(runs, key=lambda item: item.get("updated_at", ""), reverse=True)


def cmd_list(args: argparse.Namespace) -> None:
    runs = load_all_runs()
    if args.json:
        print(json.dumps(runs, indent=2))
        return
    for run in runs[: args.limit]:
        metrics = run.get("metrics", {})
        print(
            f"{run['run_id']}  {run.get('status', '?'):10}  "
            f"agents={metrics.get('agents_total', 0):2}  "
            f"updated={run.get('updated_at', '?')}  {run.get('title', '')}"
        )


def cmd_show(args: argparse.Namespace) -> None:
    data = load_run(args.run)
    if args.json:
        print(json.dumps(data, indent=2))
        return
    print(format_summary(data, detail=args.detail))


def format_summary(data: dict[str, Any], detail: bool = False) -> str:
    lines = [
        f"{data['run_id']}  {data.get('status', '?')}  {data.get('title', '')}",
        f"cwd: {data.get('cwd', '')}",
        f"mode: {data.get('mode', '')}",
        f"updated: {data.get('updated_at', '')}",
        "",
        "Phases:",
    ]
    for phase in data.get("phases", []):
        lines.append(f"  {phase['phase_id']}  {phase.get('status', '?'):10}  {phase.get('name', '')}")
    lines.append("")
    lines.append("Agents:")
    for agent in data.get("agents", []):
        lines.append(f"  {agent['agent_id']}  {agent.get('status', '?'):10}  {agent.get('name', '')}  {agent.get('role', '')}")
        if detail and agent.get("summary"):
            lines.append(f"    {agent['summary']}")
    lines.append("")
    lines.append("Recent events:")
    for event in data.get("events", [])[-10:]:
        lines.append(f"  {event.get('ts', '')} {event.get('level', '')}: {event.get('message', '')}")
    return "\n".join(lines)


def cmd_demo(args: argparse.Namespace) -> None:
    demo_args = argparse.Namespace(
        title=args.title,
        prompt="Demonstration workflow state for TUI validation.",
        prompt_file=None,
        cwd=os.getcwd(),
        mode="demo",
        tag=["demo"],
        thread_id=None,
        coordinator_tool=None,
    )
    cmd_init(demo_args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create a workflow run")
    init.add_argument("--title", required=True)
    init.add_argument("--prompt")
    init.add_argument("--prompt-file")
    init.add_argument("--cwd", default=os.getcwd())
    init.add_argument("--mode", default="hybrid", choices=["hybrid", "native-subagents", "external", "lead-local"])
    init.add_argument("--tag", action="append")
    init.add_argument("--thread-id")
    init.add_argument("--coordinator-tool", default="codex-direct")
    init.set_defaults(func=cmd_init)

    phase = sub.add_parser("add-phase", help="add a phase")
    phase.add_argument("run")
    phase.add_argument("--name", required=True)
    phase.add_argument("--goal")
    phase.add_argument("--phase-id")
    phase.add_argument("--status", default="pending")
    phase.set_defaults(func=cmd_add_phase)

    uphase = sub.add_parser("update-phase", help="update a phase")
    uphase.add_argument("run")
    uphase.add_argument("phase")
    uphase.add_argument("--status")
    uphase.add_argument("--goal")
    uphase.set_defaults(func=cmd_update_phase)

    agent = sub.add_parser("add-agent", help="add an agent")
    agent.add_argument("run")
    agent.add_argument("--phase")
    agent.add_argument("--name", required=True)
    agent.add_argument("--role")
    agent.add_argument("--agent-type")
    agent.add_argument("--agent-id")
    agent.add_argument("--status", default="pending")
    agent.add_argument("--prompt")
    agent.add_argument("--prompt-file")
    agent.add_argument("--cwd")
    agent.add_argument("--model")
    agent.add_argument("--thread-id")
    agent.add_argument("--process-id", type=int)
    agent.add_argument("--write-scope", action="append")
    agent.add_argument("--jsonl-path")
    agent.add_argument("--log-path")
    agent.add_argument("--output-path")
    agent.set_defaults(func=cmd_add_agent)

    uagent = sub.add_parser("update-agent", help="update an agent")
    uagent.add_argument("run")
    uagent.add_argument("agent")
    uagent.add_argument("--status")
    uagent.add_argument("--summary")
    uagent.add_argument("--summary-file")
    uagent.add_argument("--result")
    uagent.add_argument("--result-file")
    uagent.add_argument("--thread-id")
    uagent.add_argument("--thread-id-file")
    uagent.add_argument("--jsonl-path")
    uagent.add_argument("--jsonl-path-file")
    uagent.add_argument("--log-path")
    uagent.add_argument("--log-path-file")
    uagent.add_argument("--output-path")
    uagent.add_argument("--output-path-file")
    uagent.add_argument("--model")
    uagent.add_argument("--model-file")
    uagent.add_argument("--exit-code", type=int)
    uagent.set_defaults(func=cmd_update_agent)

    event = sub.add_parser("event", help="append an event")
    event.add_argument("run")
    event.add_argument("--level", default="info")
    event.add_argument("--message")
    event.add_argument("--message-file")
    event.add_argument("--phase")
    event.add_argument("--agent")
    event.add_argument("--data-json")
    event.add_argument("--kind")
    event.add_argument("--operation")
    event.add_argument("--source")
    event.set_defaults(func=cmd_event)

    decision = sub.add_parser("decision", help="record a decision")
    decision.add_argument("run")
    decision.add_argument("--title", required=True)
    decision.add_argument("--rationale")
    decision.add_argument("--rationale-file")
    decision.add_argument("--made-by", default="lead")
    decision.set_defaults(func=cmd_decision)

    artifact = sub.add_parser("artifact", help="record an artifact path")
    artifact.add_argument("run")
    artifact.add_argument("--path", required=True)
    artifact.add_argument("--kind", default="file")
    artifact.add_argument("--title")
    artifact.add_argument("--phase")
    artifact.add_argument("--agent")
    artifact.set_defaults(func=cmd_artifact)

    status = sub.add_parser("set-status", help="set run status")
    status.add_argument("run")
    status.add_argument("status")
    status.add_argument("--force", action="store_true", help="allow setting terminal status without gate checks")
    status.add_argument("--allow-recovery", action="store_true", help="alias for --force when recovering a run")
    status.set_defaults(func=cmd_set_status)

    pause = sub.add_parser("pause", help="pause a run before launching more workers")
    pause.add_argument("run")
    pause.add_argument("--reason")
    pause.set_defaults(func=cmd_pause)

    resume = sub.add_parser("resume", help="resume a paused run")
    resume.add_argument("run")
    resume.add_argument("--reason")
    resume.set_defaults(func=cmd_resume)

    stop = sub.add_parser("stop", help="cancel a run and terminate recorded active workers")
    stop.add_argument("run")
    stop.add_argument("--reason")
    stop.add_argument("--no-terminate", dest="terminate", action="store_false", help="only update state; do not signal processes")
    stop.set_defaults(func=cmd_stop, terminate=True)

    list_cmd = sub.add_parser("list", help="list runs")
    list_cmd.add_argument("--json", action="store_true")
    list_cmd.add_argument("--limit", type=int, default=50)
    list_cmd.set_defaults(func=cmd_list)

    show = sub.add_parser("show", help="show one run")
    show.add_argument("run")
    show.add_argument("--json", action="store_true")
    show.add_argument("--detail", action="store_true")
    show.set_defaults(func=cmd_show)

    demo = sub.add_parser("demo", help="create a minimal demo run")
    demo.add_argument("--title", default="Demo workflow")
    demo.set_defaults(func=cmd_demo)

    return parser


def friendly_missing_run(exc: FileNotFoundError) -> str:
    """Return a friendly run-not-found hint from a raw FileNotFoundError."""
    path = Path(str(exc.filename)) if exc.filename else None
    if path and path.name == "run.json":
        return f"no run {path.parent.name!r} (try: wf list)"
    return str(exc)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except FileNotFoundError as exc:
        raise SystemExit(friendly_missing_run(exc)) from None


if __name__ == "__main__":
    main()

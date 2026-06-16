#!/usr/bin/env python3
"""Emit run/phase/agent state transitions for machine consumption.

Pairs with the Monitor tool: instead of a heartbeat ping, emits structured
state-transition lines so an idle lead agent wakes with full context.

Usage:
  workflow_watch_emit.py [<run-id>] [--interval 30s] [--loop] [--state-dir PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import workflow_state

_SNAPSHOT_NAME = "watch-state.json"
_MAX_EVENT_LINES = 3
_shutdown = False


def _handle_signal(signum: int, frame: Any) -> None:
    global _shutdown
    _shutdown = True


def short_run_id(run_id: str) -> str:
    """Truncate a run_id to its last 8 characters."""
    return run_id[-8:] if len(run_id) > 8 else run_id


def _snapshot_path(run_dir: Path) -> Path:
    return run_dir / _SNAPSHOT_NAME


def _load_snapshot(run_dir: Path) -> dict[str, Any] | None:
    path = _snapshot_path(run_dir)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save_snapshot(run_dir: Path, snap: dict[str, Any]) -> None:
    path = _snapshot_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snap, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _build_snapshot(run: dict[str, Any]) -> dict[str, Any]:
    phases: dict[str, str] = {}
    for phase in run.get("phases", []):
        phases[phase.get("phase_id", "")] = phase.get("status", "unknown")

    agents: dict[str, dict[str, Any]] = {}
    for agent in run.get("agents", []):
        aid = agent.get("agent_id", "")
        agents[aid] = {
            "status": agent.get("status", "unknown"),
            "exit_code": agent.get("exit_code"),
        }

    return {
        "status": run.get("status", "unknown"),
        "phases": phases,
        "agents": agents,
        "event_count": len(run.get("events", [])),
    }


def _diff_and_emit(run: dict[str, Any], prev_snap: dict[str, Any] | None) -> list[str]:
    """Compare run state against snapshot and return emitted transition lines."""
    rid = short_run_id(run.get("run_id", ""))
    lines: list[str] = []
    current_status = run.get("status", "unknown")

    if prev_snap is None:
        lines.append(f"{rid} RUN: \u2192 {current_status}")
        for phase in run.get("phases", []):
            pid = phase.get("name", phase.get("phase_id", ""))
            lines.append(f"{rid} PHASE {pid}: \u2192 {phase.get('status', 'unknown')}")
        for agent in run.get("agents", []):
            name = agent.get("name", agent.get("agent_id", ""))
            status = agent.get("status", "unknown")
            line = f"{rid} AGENT {name}: \u2192 {status}"
            exit_code = agent.get("exit_code")
            if exit_code is not None and status in workflow_state.TERMINAL_STATUS_VALUES:
                line += f" (exit {exit_code})"
            lines.append(line)
        return lines

    old_status = prev_snap.get("status", "")
    if current_status != old_status:
        lines.append(f"{rid} RUN: {old_status} \u2192 {current_status}")

    old_phases = prev_snap.get("phases", {})
    for phase in run.get("phases", []):
        pid = phase.get("phase_id", "")
        name = phase.get("name", pid)
        new_ps = phase.get("status", "unknown")
        old_ps = old_phases.get(pid)
        if old_ps is None:
            lines.append(f"{rid} PHASE {name}: \u2192 {new_ps}")
        elif new_ps != old_ps:
            lines.append(f"{rid} PHASE {name}: {old_ps} \u2192 {new_ps}")

    old_agents = prev_snap.get("agents", {})
    for agent in run.get("agents", []):
        aid = agent.get("agent_id", "")
        name = agent.get("name", aid)
        new_as = agent.get("status", "unknown")
        old_info = old_agents.get(aid)
        if old_info is None:
            line = f"{rid} AGENT {name}: \u2192 {new_as}"
            exit_code = agent.get("exit_code")
            if exit_code is not None and new_as in workflow_state.TERMINAL_STATUS_VALUES:
                line += f" (exit {exit_code})"
            lines.append(line)
        else:
            old_as = old_info.get("status", "")
            if new_as != old_as:
                line = f"{rid} AGENT {name}: {old_as} \u2192 {new_as}"
                exit_code = agent.get("exit_code")
                if exit_code is not None and new_as in workflow_state.TERMINAL_STATUS_VALUES:
                    line += f" (exit {exit_code})"
                lines.append(line)

    events = run.get("events", [])
    old_count = prev_snap.get("event_count", 0)
    if len(events) > old_count:
        new_events = events[old_count:]
        for event in new_events[-_MAX_EVENT_LINES:]:
            msg = event.get("message", "")
            if msg:
                lines.append(f"{rid} EVENT: {msg}")

    return lines


def _is_terminal(run: dict[str, Any]) -> bool:
    return run.get("status", "") in workflow_state.TERMINAL_STATUS_VALUES


def cmd_watch_emit(args: argparse.Namespace) -> None:
    state_dir = Path(args.state_dir).expanduser() if args.state_dir else workflow_state.state_root()
    interval = args.interval
    loop = args.loop
    max_iters = getattr(args, "_max_iters", None)
    if max_iters is None:
        env_max = os.environ.get("WORKFLOW_WATCH_EMIT_MAX_ITERS")
        if env_max is not None:
            max_iters = int(env_max)

    if args.run_id:
        _watch_single(args.run_id, state_dir, interval, loop, max_iters)
    else:
        _watch_all(state_dir, interval, loop, max_iters)


def _watch_single(
    run_id: str,
    state_dir: Path,
    interval: float,
    loop: bool,
    max_iters: int | None,
) -> None:
    runs_dir = state_dir / "runs"
    run_dir = runs_dir / run_id
    run_file = run_dir / "run.json"

    iteration = 0
    while True:
        try:
            run = json.loads(run_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return

        prev_snap = _load_snapshot(run_dir)
        lines = _diff_and_emit(run, prev_snap)
        for line in lines:
            print(line, flush=True)

        _save_snapshot(run_dir, _build_snapshot(run))

        if not loop:
            return

        iteration += 1
        if max_iters is not None and iteration >= max_iters:
            return
        if _shutdown:
            return
        time.sleep(interval)
        if _shutdown:
            return


def _watch_all(
    state_dir: Path,
    interval: float,
    loop: bool,
    max_iters: int | None,
) -> None:
    runs_dir = state_dir / "runs"
    known_run_ids: set[str] = set()

    iteration = 0
    while True:
        all_lines: list[str] = []
        current_run_ids: set[str] = set()

        if runs_dir.is_dir():
            for run_json in runs_dir.glob("*/run.json"):
                run_dir = run_json.parent
                rid = run_dir.name
                current_run_ids.add(rid)

                try:
                    run = json.loads(run_json.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue

                if not loop and _is_terminal(run):
                    pass

                if loop and rid not in known_run_ids and known_run_ids:
                    sr = short_run_id(rid)
                    status = run.get("status", "unknown")
                    all_lines.append(f"{sr} RUN: \u2192 {status} [new]")

                prev_snap = _load_snapshot(run_dir)
                lines = _diff_and_emit(run, prev_snap)
                all_lines.extend(lines)
                _save_snapshot(run_dir, _build_snapshot(run))

        known_run_ids = current_run_ids

        for line in all_lines:
            print(line, flush=True)

        if not loop:
            return

        iteration += 1
        if max_iters is not None and iteration >= max_iters:
            return
        if _shutdown:
            return
        time.sleep(interval)
        if _shutdown:
            return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", nargs="?", default=None, help="watch a specific run (omit to watch all active)")
    parser.add_argument("--interval", type=float, default=30.0, help="poll interval in seconds (default: 30)")
    parser.add_argument("--loop", action="store_true", help="poll repeatedly; emit only on change")
    parser.add_argument("--state-dir", default=None, help="override state directory")
    return parser


def main() -> None:
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    parser = build_parser()
    args = parser.parse_args()
    cmd_watch_emit(args)


if __name__ == "__main__":
    main()

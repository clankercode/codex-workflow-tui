#!/usr/bin/env python3
"""Create a Fibonacci reduction-tree workflow stress run."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

import workflow_state


def utc_now_epoch() -> float:
    return datetime.now(UTC).timestamp()


def fib(n: int) -> int:
    """Return F(n) using a small iterative verifier independent of the tree."""
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a


def term_count(n: int) -> int:
    """Return the number of binomial terms in F(n)."""
    return ((n - 1) // 2) + 1


def expected_agent_count(n: int) -> int:
    """Return the leaf-plus-reducer agent count for the reduction tree."""
    return (2 * term_count(n)) - 1


def make_run(title: str, prompt: str, cwd: Path, tags: list[str]) -> dict[str, Any]:
    """Create the base workflow run object and directories."""
    run_id = workflow_state.run_id(title)
    run_dir = workflow_state.runs_root() / run_id
    artifacts_dir = run_dir / "artifacts"
    logs_dir = run_dir / "logs"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "schema_version": workflow_state.SCHEMA_VERSION,
        "run_id": run_id,
        "title": title,
        "prompt": prompt,
        "cwd": str(cwd),
        "mode": "manual-fibonacci-stress",
        "status": "running",
        "tags": tags,
        "created_at": workflow_state.now(),
        "updated_at": workflow_state.now(),
        "coordinator": {"tool": "workflow_fibonacci_stress.py", "thread_id": None},
        "paths": {
            "run_dir": str(run_dir),
            "run_json": str(run_dir / "run.json"),
            "artifacts_dir": str(artifacts_dir),
            "logs_dir": str(logs_dir),
        },
        "phases": [],
        "agents": [],
        "events": [],
        "decisions": [],
        "artifacts": [],
        "metrics": {},
    }
    workflow_state.add_event(data, "info", "workflow initialized", kind="workflow", operation="initialized", source="workflow_fibonacci_stress.init")
    return data


def add_phase(data: dict[str, Any], phase_id: str, name: str, goal: str, order: int) -> dict[str, Any]:
    """Append a completed phase to the run."""
    now = workflow_state.now()
    phase = {
        "phase_id": phase_id,
        "name": name,
        "goal": goal,
        "order": order,
        "status": "completed",
        "created_at": now,
        "started_at": now,
        "completed_at": now,
        "agent_ids": [],
    }
    data.setdefault("phases", []).append(phase)
    workflow_state.add_event(
        data,
        "info",
        f"phase added: {name}",
        kind="phase",
        operation="added",
        source="workflow_fibonacci_stress.add_phase",
        phase_id=phase_id,
        data={"name": name, "status": "completed", "order": order},
    )
    return phase


def write_agent_files(
    data: dict[str, Any],
    agent_id: str,
    output_text: str,
    payload: dict[str, Any],
    start_epoch: float,
) -> tuple[Path, Path]:
    """Write durable output and a JSONL transcript for a manual agent."""
    artifacts_dir = Path(data["paths"]["artifacts_dir"])
    logs_dir = Path(data["paths"]["logs_dir"])
    output_path = artifacts_dir / f"{agent_id}.txt"
    jsonl_path = logs_dir / f"{agent_id}.jsonl"
    output_path.write_text(output_text + "\n", encoding="utf-8")
    events = [
        {"type": "text", "timestamp": start_epoch, "part": {"type": "text", "text": output_text}},
        {"type": "workflow.manual_result", "timestamp": start_epoch, "item": payload},
        {
            "type": "workflow.manual_usage",
            "timestamp": start_epoch,
            "usage": {"total_tokens": 0, "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0},
        },
    ]
    jsonl_path.write_text("\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n", encoding="utf-8")
    return output_path, jsonl_path


def add_agent(
    data: dict[str, Any],
    phase: dict[str, Any],
    agent_id: str,
    name: str,
    role: str,
    prompt: str,
    output_text: str,
    payload: dict[str, Any],
    duration_seconds: float,
) -> dict[str, Any]:
    """Append one completed manual agent and its update event."""
    start_epoch = utc_now_epoch()
    output_path, jsonl_path = write_agent_files(data, agent_id, output_text, payload, start_epoch)
    now = workflow_state.now()
    agent = {
        "agent_id": agent_id,
        "phase_id": phase["phase_id"],
        "name": name,
        "role": role,
        "agent_type": "manual-fibonacci",
        "status": "completed",
        "prompt": prompt,
        "cwd": data.get("cwd"),
        "model": "manual-script",
        "thread_id": agent_id,
        "process_id": None,
        "write_scope": [],
        "jsonl_path": str(jsonl_path),
        "log_path": "",
        "output_path": str(output_path),
        "summary": output_text,
        "result": output_text,
        "exit_code": 0,
        "created_at": now,
        "started_at": now,
        "completed_at": now,
        "updated_at": now,
        "started_epoch": start_epoch,
        "duration_seconds": duration_seconds,
    }
    data.setdefault("agents", []).append(agent)
    phase.setdefault("agent_ids", []).append(agent_id)
    workflow_state.add_event(
        data,
        "info",
        f"agent updated: {name}",
        kind="agent",
        operation="updated",
        source="workflow_fibonacci_stress.agent",
        phase_id=phase["phase_id"],
        agent_id=agent_id,
        data={"name": name, "status": "completed", "agent_type": "manual-fibonacci", "duration_seconds": round(duration_seconds, 6)},
    )
    return agent


def record_artifact(data: dict[str, Any], kind: str, title: str, path: Path, phase_id: str | None = None) -> dict[str, Any]:
    """Record an artifact without shelling out to the state CLI."""
    artifact = {
        "artifact_id": workflow_state.short_id("art"),
        "ts": workflow_state.now(),
        "kind": kind,
        "title": title,
        "path": str(path),
        "agent_id": "",
        "phase_id": phase_id,
    }
    data.setdefault("artifacts", []).append(artifact)
    workflow_state.add_event(
        data,
        "info",
        f"artifact recorded: {title}",
        kind="artifact",
        operation="recorded",
        source="workflow_fibonacci_stress.artifact",
        phase_id=phase_id,
        data={"title": title, "kind": kind, "path": str(path)},
    )
    return artifact


def record_decision(data: dict[str, Any], title: str, rationale: str) -> None:
    """Record a durable run decision."""
    data.setdefault("decisions", []).append(
        {
            "decision_id": workflow_state.short_id("dec"),
            "ts": workflow_state.now(),
            "title": title,
            "rationale": rationale,
            "made_by": "workflow_fibonacci_stress.py",
        }
    )
    workflow_state.add_event(
        data,
        "info",
        f"decision recorded: {title}",
        kind="decision",
        operation="recorded",
        source="workflow_fibonacci_stress.decision",
        data={"title": title, "made_by": "workflow_fibonacci_stress.py"},
    )


def build_tree(data: dict[str, Any], n: int) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
    """Create leaf and reducer agents for the binomial Fibonacci identity."""
    nodes: list[dict[str, Any]] = []
    timings: list[dict[str, Any]] = []
    leaf_phase = add_phase(data, "phase-leaf-terms", "Leaf Terms", f"Compute {term_count(n)} independent binomial terms for F({n}).", 10)
    for k in range(term_count(n)):
        start = time.perf_counter()
        value = math.comb(n - 1 - k, k)
        duration = time.perf_counter() - start
        agent_id = f"leaf-{k:02d}"
        output = f"term {k}: C({n - 1 - k}, {k}) = {value}"
        prompt = f"Compute exactly one Fibonacci binomial term: C({n - 1 - k}, {k})."
        add_agent(
            data,
            leaf_phase,
            agent_id,
            f"Term {k:02d}",
            "leaf term",
            prompt,
            output,
            {"node_id": agent_id, "kind": "leaf", "k": k, "n_choose": [n - 1 - k, k], "value": value},
            duration,
        )
        timings.append({"agent_id": agent_id, "phase_id": leaf_phase["phase_id"], "doing": prompt, "duration_seconds": duration})
        nodes.append({"node_id": agent_id, "value": value, "kind": "leaf", "k": k})

    tree_nodes = list(nodes)
    current = nodes
    level = 1
    while len(current) > 1:
        phase = add_phase(data, f"phase-reduce-{level}", f"Reduce L{level}", f"Sum pairs from reduction level {level - 1}.", 10 + level)
        next_nodes: list[dict[str, Any]] = []
        pair_index = 0
        cursor = 0
        while cursor + 1 < len(current):
            left = current[cursor]
            right = current[cursor + 1]
            start = time.perf_counter()
            value = int(left["value"]) + int(right["value"])
            duration = time.perf_counter() - start
            agent_id = f"reduce-{level:02d}-{pair_index:02d}"
            output = f"{left['node_id']} + {right['node_id']} = {value}"
            prompt = f"Compute exactly one sum: {left['value']} + {right['value']}."
            add_agent(
                data,
                phase,
                agent_id,
                f"Reduce L{level}.{pair_index:02d}",
                "one sum reducer",
                prompt,
                output,
                {"node_id": agent_id, "kind": "reducer", "left": left["node_id"], "right": right["node_id"], "value": value},
                duration,
            )
            timings.append({"agent_id": agent_id, "phase_id": phase["phase_id"], "doing": prompt, "duration_seconds": duration})
            node = {"node_id": agent_id, "value": value, "kind": "reducer", "left": left["node_id"], "right": right["node_id"], "level": level}
            next_nodes.append(node)
            tree_nodes.append(node)
            pair_index += 1
            cursor += 2
        if cursor < len(current):
            next_nodes.append(current[cursor])
        workflow_state.add_event(
            data,
            "info",
            f"reduction level {level} completed",
            kind="reduction",
            operation="level_completed",
            source="workflow_fibonacci_stress.reduce",
            phase_id=phase["phase_id"],
            data={"level": level, "reducers": pair_index, "carried": len(current) % 2, "next_width": len(next_nodes)},
        )
        current = next_nodes
        level += 1
    return int(current[0]["value"]), tree_nodes, timings


def write_final_artifacts(
    data: dict[str, Any],
    n: int,
    value: int,
    tree_nodes: list[dict[str, Any]],
    timings: list[dict[str, Any]],
    e2e_seconds: float,
    archive_root: Path,
) -> dict[str, Path]:
    """Write final answer, tree, timing, and archived state artifacts."""
    artifacts_dir = Path(data["paths"]["artifacts_dir"])
    answer_path = artifacts_dir / f"fib-{n}-answer.txt"
    tree_path = artifacts_dir / f"fib-{n}-reduction-tree.json"
    timing_path = artifacts_dir / f"fib-{n}-timing.json"
    longest = max(timings, key=lambda item: item["duration_seconds"]) if timings else {}
    timing_data = {
        "run_id": data["run_id"],
        "n": n,
        "agents_total": len(timings),
        "e2e_seconds": e2e_seconds,
        "avg_agent_seconds": sum(item["duration_seconds"] for item in timings) / len(timings) if timings else 0.0,
        "longest_agent": longest,
        "phases": [{"phase_id": phase["phase_id"], "agents": len(phase.get("agent_ids", []))} for phase in data.get("phases", [])],
    }
    answer_path.write_text(f"F({n}) = {value}\n", encoding="utf-8")
    tree_path.write_text(json.dumps({"n": n, "identity": "F(n) = sum(C(n-1-k,k))", "nodes": tree_nodes}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    timing_path.write_text(json.dumps(timing_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    final_phase_id = data.get("phases", [{}])[-1].get("phase_id")
    record_artifact(data, "answer", f"F({n}) answer", answer_path, final_phase_id)
    record_artifact(data, "reduction-tree", f"F({n}) reduction tree", tree_path, final_phase_id)
    record_artifact(data, "timing", f"F({n}) timing", timing_path, final_phase_id)
    archive_dir = archive_root / data["run_id"]
    archive_dir.mkdir(parents=True, exist_ok=True)
    for path in (answer_path, tree_path, timing_path):
        shutil.copy2(path, archive_dir / path.name)
    return {"answer": answer_path, "tree": tree_path, "timing": timing_path, "archive": archive_dir}


def run_stress(args: argparse.Namespace) -> dict[str, Any]:
    """Run the full stress workflow and return a summary."""
    if args.state_dir:
        os.environ["WORKFLOW_STATE_DIR"] = str(Path(args.state_dir).expanduser().resolve())
    output_dir = Path(args.output_dir).expanduser().resolve()
    archive_root = output_dir / "archive"
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_root.mkdir(parents=True, exist_ok=True)
    terms = term_count(args.n)
    agents = expected_agent_count(args.n)
    reducers = agents - terms
    title = args.title or f"Fib({args.n}) {agents}-agent reduction stress"
    prompt = f"Stress test workflow state and TUI with a full binary reduction tree for F({args.n})."
    data = make_run(title, prompt, Path.cwd().resolve(), ["stress", "fibonacci", "manual-agents"])
    record_decision(
        data,
        f"Use {agents} scripted manual agents",
        f"F({args.n}) has {terms} independent binomial terms and therefore {reducers} binary reducers. "
        f"Scripted agents stress workflow state, artifacts, timing, and TUI navigation without spending {agents} model calls.",
    )
    start = time.perf_counter()
    value, tree_nodes, timings = build_tree(data, args.n)
    e2e_seconds = time.perf_counter() - start
    expected = fib(args.n)
    if value != expected:
        data["status"] = "failed"
        workflow_state.add_event(data, "error", f"F({args.n}) verification failed", kind="verification", operation="failed", source="workflow_fibonacci_stress.verify")
        workflow_state.save_run(data)
        raise SystemExit(f"verification failed: tree={value}, expected={expected}")
    paths = write_final_artifacts(data, args.n, value, tree_nodes, timings, e2e_seconds, archive_root)
    workflow_state.add_event(
        data,
        "info",
        f"F({args.n}) verified",
        kind="verification",
        operation="passed",
        source="workflow_fibonacci_stress.verify",
        data={"value": str(value), "agents": len(timings), "e2e_seconds": round(e2e_seconds, 6)},
    )
    data["status"] = "completed"
    saved = workflow_state.save_run(data)
    shutil.copy2(saved, paths["archive"] / "run.json")
    summary = {
        "run_id": data["run_id"],
        "answer": str(value),
        "agents_total": len(timings),
        "phases_total": len(data.get("phases", [])),
        "run_json": str(saved),
        "answer_path": str(paths["answer"]),
        "timing_path": str(paths["timing"]),
        "tree_path": str(paths["tree"]),
        "archive_dir": str(paths["archive"]),
    }
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=100, help="Fibonacci index to compute; default: 100")
    parser.add_argument("--title")
    parser.add_argument("--output-dir", default="~/tmp/custom-wf-test", help="archive/output root; default: ~/tmp/custom-wf-test")
    parser.add_argument("--state-dir", help="optional workflow state dir override")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.n < 2:
        raise SystemExit("--n must be at least 2")
    summary = run_stress(args)
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Operator-friendly workflow commands built on workflow_state plumbing."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

import workflow_health
import workflow_monitor
import workflow_state
import workflow_ops_merge

# Re-export names extracted to workflow_ops_merge for backward compatibility.
ScopeCheckError = workflow_ops_merge.ScopeCheckError
build_merger_prompt = workflow_ops_merge.build_merger_prompt
cmd_merge_conflicts = workflow_ops_merge.cmd_merge_conflicts
cmd_merge_lanes = workflow_ops_merge.cmd_merge_lanes
completed_worktree_lane_agents = workflow_ops_merge.completed_worktree_lane_agents
current_git_branch = workflow_ops_merge.current_git_branch
ensure_clean_git_tree = workflow_ops_merge.ensure_clean_git_tree
find_conflict_context = workflow_ops_merge.find_conflict_context
first_line = workflow_ops_merge.first_line
git_checked = workflow_ops_merge.git_checked
lane_scope_violations = workflow_ops_merge.lane_scope_violations
merge_output = workflow_ops_merge.merge_output
record_merge_check = workflow_ops_merge.record_merge_check


def print_json(value: Any) -> None:
    """Print JSON with stable indentation."""
    print(json.dumps(value, indent=2, sort_keys=True))


def load_runs_for_args(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Load runs and optionally filter by current working directory."""
    runs = workflow_state.load_all_runs()
    if getattr(args, "cwd", False):
        cwd = str(Path.cwd().resolve())
        runs = [run for run in runs if str(run.get("cwd", "")) == cwd]
    return runs


def status_line(run: dict[str, Any]) -> str:
    """Return one compact operator status line for a run."""
    metrics = run.get("metrics", {})
    issues = structural_issues(run) + workflow_health.analyze_run(run)
    critical = sum(1 for item in issues if item.get("severity") == workflow_health.CRITICAL)
    warnings = sum(1 for item in issues if item.get("severity") == workflow_health.WARNING)
    checks = run.get("checks", [])
    passed = sum(1 for check in checks if check.get("status") == "passed")
    failed = sum(1 for check in checks if check.get("status") in {"failed", "error"})
    health = "ok" if not critical and not warnings else f"{critical} critical/{warnings} warn"
    check_text = f"checks={passed} ok/{failed} fail" if checks else "checks=none"
    return (
        f"{run.get('run_id', '')}  {run.get('status', '?'):10}  "
        f"agents={metrics.get('agents_total', 0):2}  phases={metrics.get('phases_total', 0):2}  "
        f"{health:18}  {check_text:18}  {run.get('title', '')}"
    )


def cmd_status(args: argparse.Namespace) -> None:
    """Print active and recent runs with derived health."""
    runs = load_runs_for_args(args)
    if not args.all:
        runs = [run for run in runs if run.get("status") in {"running", "blocked", "failed", "paused"}] or runs
    runs = runs[: args.limit]
    if args.json:
        print_json(
            [
                {
                    "run_id": run.get("run_id", ""),
                    "title": run.get("title", ""),
                    "status": run.get("status", ""),
                    "updated_at": run.get("updated_at", ""),
                    "issues": structural_issues(run) + workflow_health.analyze_run(run),
                    "metrics": run.get("metrics", {}),
                }
                for run in runs
            ]
        )
        return
    if not runs:
        print("No workflow runs found.")
        return
    for run in runs:
        print(status_line(run))
        issues = (structural_issues(run) + workflow_health.analyze_run(run))[:3]
        for item in issues:
            print(f"  {item['severity']}: {item['title']} - {item['message']}")


def latest_run(args: argparse.Namespace) -> dict[str, Any]:
    """Return the latest matching run or exit with a clear error."""
    runs = load_runs_for_args(args)
    if not runs:
        scope = " in this cwd" if getattr(args, "cwd", False) else ""
        raise SystemExit(f"no workflow runs found{scope}")
    return runs[0]


def cmd_last(args: argparse.Namespace) -> None:
    """Show the latest run."""
    run = latest_run(args)
    if args.id_only:
        print(run.get("run_id", ""))
        return
    if args.json:
        print_json(run)
        return
    print(status_line(run))
    print(f"path: {run.get('paths', {}).get('run_json', '')}")


def command_status(name: str, *, required: bool = False) -> dict[str, Any]:
    """Return availability details for one external command."""
    path = shutil.which(name)
    return {"name": name, "ok": bool(path), "path": path or "", "required": required}


def command_points_to_checkout(name: str) -> dict[str, Any]:
    """Check whether an installed workflow wrapper resolves to this checkout."""
    path = shutil.which(name)
    if not path:
        return {"name": f"{name}-in-checkout", "ok": False, "path": "not in PATH", "required": False}
    try:
        resolved = Path(path).resolve()
    except OSError as exc:
        return {"name": f"{name}-in-checkout", "ok": False, "path": str(exc), "required": False}
    expected = Path(__file__).resolve().with_name("wf")
    return {"name": f"{name}-in-checkout", "ok": resolved == expected, "path": str(resolved), "required": False}


def writable_dir(path: Path) -> bool:
    """Return whether a directory can be written."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path, prefix=".workflow-doctor-", delete=True):
            return True
    except OSError:
        return False


def cmd_doctor(args: argparse.Namespace) -> None:
    """Check install, dependency, and state health."""
    checks: list[dict[str, Any]] = []
    state = workflow_state.state_root()
    checks.append({"name": "state-dir", "ok": writable_dir(state), "path": str(state), "required": True})
    checks.append({"name": "workflow-home", "ok": writable_dir(workflow_state.workflow_root()), "path": str(workflow_state.workflow_root()), "required": True})
    for command in ("workflow", "wf", "codex", "ccc", "opencode", "tmux", "git"):
        checks.append(command_status(command, required=False))
    for command in ("workflow", "wf"):
        checks.append(command_points_to_checkout(command))
    try:
        import rich  # noqa: F401  # pylint: disable=import-outside-toplevel,unused-import

        checks.append({"name": "python-rich", "ok": True, "path": "import rich", "required": True})
    except ModuleNotFoundError:
        checks.append({"name": "python-rich", "ok": False, "path": "import rich", "required": True})
    try:
        import textual  # noqa: F401  # pylint: disable=import-outside-toplevel,unused-import

        checks.append({"name": "python-textual", "ok": True, "path": "import textual", "required": False})
    except ModuleNotFoundError:
        venv_python = workflow_state.workflow_root() / ".venv" / "bin" / "python"
        checks.append({"name": "python-textual", "ok": venv_python.exists(), "path": str(venv_python) if venv_python.exists() else "missing", "required": False})
    required_ok = all(check["ok"] for check in checks if check.get("required"))
    if args.json:
        print_json({"ok": required_ok, "checks": checks})
        return
    for check in checks:
        marker = "OK" if check["ok"] else ("FAIL" if check.get("required") else "WARN")
        print(f"{marker:4} {check['name']:<16} {check['path']}")
    if not required_ok:
        raise SystemExit(1)


def cmd_check(args: argparse.Namespace) -> None:
    """Validate one run's consistency and derived health."""
    run = workflow_state.load_run(args.run)
    issues = workflow_health.analyze_run(run, stale_seconds=args.stale_seconds)
    structural = structural_issues(run)
    all_issues = structural + issues
    if args.json:
        print_json({"run_id": run.get("run_id", ""), "issues": all_issues, "ok": not any(item["severity"] == workflow_health.CRITICAL for item in all_issues)})
        return
    if not all_issues:
        print(f"OK {run.get('run_id', '')}: no issues found")
        return
    for item in all_issues:
        print(f"{item['severity'].upper():8} {item['kind']:<22} {item['title']}")
        if item.get("message"):
            print(f"         {item['message']}")
        if item.get("suggestion"):
            print(f"         next: {item['suggestion']}")
    if any(item["severity"] == workflow_health.CRITICAL for item in all_issues):
        raise SystemExit(1)


def _invalid_status_issues(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Return CRITICAL issues for any entity with an invalid status."""
    issues: list[dict[str, Any]] = []
    valid_statuses = workflow_state.STATUS_VALUES
    run_status = run.get("status")
    if run_status not in valid_statuses:
        issues.append(
            workflow_health.issue(
                run=run, severity=workflow_health.CRITICAL, kind="invalid-status",
                title=f"Invalid run status: {run_status}",
                message=f"Expected one of {sorted(valid_statuses)}.",
                entity_type="run", entity_id=str(run.get("run_id", "")),
            )
        )
    for collection_name in ("phases", "agents"):
        for item in run.get(collection_name, []):
            status = item.get("status")
            if status not in valid_statuses:
                issues.append(
                    workflow_health.issue(
                        run=run, severity=workflow_health.CRITICAL, kind="invalid-status",
                        title=f"Invalid {collection_name[:-1]} status: {status}",
                        message=f"Expected one of {sorted(valid_statuses)}.",
                        entity_type=collection_name[:-1],
                        entity_id=str(item.get(f"{collection_name[:-1]}_id", "")),
                    )
                )
    return issues


def _orphan_reference_issues(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Return WARNING issues for phases/agents referencing missing counterparts."""
    issues: list[dict[str, Any]] = []
    phases = workflow_health.index_by(run.get("phases", []), "phase_id")
    agents = workflow_health.index_by(run.get("agents", []), "agent_id")
    for phase in run.get("phases", []):
        for agent_id in phase.get("agent_ids", []):
            if agent_id not in agents:
                issues.append(
                    workflow_health.issue(
                        run=run, severity=workflow_health.WARNING, kind="orphan-phase-agent",
                        title=f"Phase references missing agent: {agent_id}",
                        message=f"Phase {phase.get('phase_id')} has an agent_id not present in agents[].",
                        entity_type="phase", phase_id=str(phase.get("phase_id", "")),
                    )
                )
    for agent in run.get("agents", []):
        phase_id = str(agent.get("phase_id") or "")
        if phase_id and phase_id not in phases:
            issues.append(
                workflow_health.issue(
                    run=run, severity=workflow_health.WARNING, kind="orphan-agent-phase",
                    title=f"Agent references missing phase: {agent.get('name', agent.get('agent_id'))}",
                    message=f"phase_id={phase_id} is not present in phases[].",
                    entity_type="agent", agent_id=str(agent.get("agent_id", "")),
                    phase_id=phase_id,
                )
            )
    return issues


def structural_issues(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Return schema/path linkage issues not covered by live health."""
    return _invalid_status_issues(run) + _orphan_reference_issues(run)


def run_verification_command(command: str, cwd: str | None) -> tuple[int, str, float]:
    """Run a verification command and return exit code, combined output, and duration."""
    import time

    start = time.time()
    result = subprocess.run(command, shell=True, cwd=cwd or None, text=True, capture_output=True)
    duration = time.time() - start
    output = ""
    if result.stdout:
        output += result.stdout
    if result.stderr:
        output += ("\n" if output else "") + result.stderr
    return result.returncode, output, duration


def _validate_verify_args(args: argparse.Namespace, command: str) -> None:
    """Raise SystemExit if verify arguments are inconsistent."""
    if args.record_only:
        if command:
            raise SystemExit("--record-only cannot be combined with --cmd; omit --cmd and put external evidence in --summary")
        if not args.status:
            raise SystemExit("--record-only requires --status")
        if not args.summary:
            raise SystemExit("--record-only requires --summary")
        if not (args.evidence_path or args.external_ref):
            raise SystemExit("--record-only requires evidence provenance via --evidence-path or --external-ref")
    elif not command:
        raise SystemExit("wf verify requires --cmd, or --record-only with --status and --summary")
    elif args.status:
        raise SystemExit("--status is only valid with --record-only; executed checks derive status from exit code")


def _verify_mutator(
    *, check_id: str, name: str, kind: str, status: str, required: bool,
    command: str, cwd: str | None, exit_code: int, duration: float,
    output: str, summary_arg: str, evidence_path: str, external_ref: str,
    record_only: bool,
) -> Any:
    """Return a mutator function for recording a verification check."""

    def mutator(data: dict[str, Any]) -> dict[str, Any]:
        log_path = ""
        if output:
            logs_dir = Path(data.get("paths", {}).get("logs_dir") or Path(data.get("paths", {}).get("run_dir", ".")) / "logs")
            logs_dir.mkdir(parents=True, exist_ok=True)
            log_file = logs_dir / f"{check_id}.log"
            log_file.write_text(output, encoding="utf-8")
            log_path = str(log_file)
        check = {
            "check_id": check_id, "ts": workflow_state.now(), "name": name,
            "kind": kind, "status": status, "required": required,
            "command": command, "cwd": cwd, "exit_code": exit_code,
            "duration_seconds": round(duration, 3),
            "summary": first_line(output) or summary_arg or status,
            "log_path": log_path, "completed_at": workflow_state.now(),
            "evidence_path": evidence_path, "external_ref": external_ref,
        }
        data.setdefault("checks", []).append(check)
        event_kind = "verification: external" if record_only else "check"
        event_data: dict[str, Any] = {"check_id": check_id, "name": name, "status": status, "required": required}
        if evidence_path:
            event_data["evidence_path"] = evidence_path
        if external_ref:
            event_data["external_ref"] = external_ref
        workflow_state.add_event(
            data,
            "error" if status in {"failed", "error"} else "info",
            f"verification {status}: {name}",
            kind=event_kind, operation="recorded", source="workflow_ops.verify",
            data=event_data,
        )
        return check

    return mutator


def cmd_verify(args: argparse.Namespace) -> None:
    """Run or record a verification check."""
    run = workflow_state.load_run(args.run)
    command = args.cmd or ""
    _validate_verify_args(args, command)
    name = args.name or (command.split()[0] if command else "manual verification")
    cwd = args.cwd or run.get("cwd")
    exit_code = 0
    output = args.summary or ""
    duration = 0.0
    if command and not args.record_only:
        exit_code, output, duration = run_verification_command(command, cwd)
    status = args.status if args.record_only else ("passed" if exit_code == 0 else "failed")
    check_id = args.check_id or workflow_state.short_id("chk")
    mutator = _verify_mutator(
        check_id=check_id, name=name, kind=args.kind, status=status,
        required=not args.optional, command=command, cwd=cwd,
        exit_code=exit_code, duration=duration, output=output,
        summary_arg=args.summary or "", evidence_path=args.evidence_path or "",
        external_ref=args.external_ref or "", record_only=args.record_only,
    )
    _, check, _ = workflow_state.mutate_run(args.run, mutator)
    print_json(check)
    if check["status"] in {"failed", "error"}:
        raise SystemExit(exit_code or 1)


def backlog_path(run: dict[str, Any]) -> Path:
    """Return the durable backlog file path for a run."""
    run_dir = Path(run.get("paths", {}).get("run_dir") or ".")
    return run_dir / "artifacts" / "backlog.md"


def ensure_backlog_artifact(run_data: dict[str, Any], path: Path) -> bool:
    """Register the backlog file as an artifact if not already present. Returns True if newly added."""
    artifacts = run_data.setdefault("artifacts", [])
    for artifact in artifacts:
        if artifact.get("kind") == "backlog":
            return False
    artifact = {
        "artifact_id": workflow_state.short_id("art"),
        "ts": workflow_state.now(),
        "kind": "backlog",
        "title": "workflow backlog",
        "path": str(path),
    }
    artifacts.append(artifact)
    workflow_state.add_event(
        run_data,
        "info",
        "backlog artifact registered",
        kind="backlog",
        operation="registered",
        source="workflow_ops.backlog",
        data={"path": str(path)},
    )
    return True


def cmd_backlog(args: argparse.Namespace) -> None:
    """Create, register, and append to a durable workflow backlog."""
    finding = args.append or ""

    def mutator(data: dict[str, Any]) -> dict[str, Any]:
        path = backlog_path(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        is_new_artifact = ensure_backlog_artifact(data, path)
        existed = path.exists()
        if finding:
            timestamp = workflow_state.now()
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"\n## [{timestamp}] finding\n\n{finding}\n")
            workflow_state.add_event(
                data,
                "info",
                "backlog entry appended",
                kind="backlog",
                operation="appended",
                source="workflow_ops.backlog",
                data={"finding": finding[:500]},
            )
        elif not existed:
            path.write_text("# Workflow Backlog\n\nDurable findings and discoveries for this run.\n", encoding="utf-8")
        return {
            "path": str(path),
            "is_new_artifact": is_new_artifact,
            "appended": bool(finding),
        }

    _, result, _ = workflow_state.mutate_run(args.run, mutator)
    print_json(result)


def cmd_done(args: argparse.Namespace) -> None:
    """Safely mark a workflow run completed."""
    def mutator(data: dict[str, Any]) -> dict[str, Any]:
        structural_blockers = [item for item in structural_issues(data) if item.get("severity") == workflow_health.CRITICAL]
        lifecycle_blockers = [] if args.force else workflow_health.completion_blockers(data, allow_unverified=args.allow_unverified)
        blockers = structural_blockers + lifecycle_blockers
        if blockers:
            raise workflow_state.AbortMutation({"run_id": data.get("run_id", args.run), "completed": False, "blockers": blockers})
        data["status"] = "completed"
        data["status_reason"] = args.reason or "completed via wf done"
        data["status_message"] = args.message or data["status_reason"]
        for phase in data.get("phases", []):
            if phase.get("status") in {"pending", "running", "paused"}:
                phase["status"] = "completed"
                phase["completed_at"] = workflow_state.now()
        workflow_state.add_event(
            data,
            "info",
            "workflow completed via wf done",
            kind="run",
            operation="completed",
            source="workflow_ops.done",
            data={"allow_unverified": args.allow_unverified, "force": args.force},
        )
        return {"run_id": data.get("run_id", args.run), "completed": True, "blockers": []}

    _, outcome, _ = workflow_state.mutate_run(args.run, mutator)
    if outcome["blockers"]:
        if args.json:
            print_json(outcome)
        else:
            print(f"Refusing to complete {outcome['run_id']}; blockers remain:")
            for item in outcome["blockers"]:
                print(f"  {item['kind']}: {item['title']}")
                if item.get("suggestion"):
                    print(f"    next: {item['suggestion']}")
        raise SystemExit(1)
    result = {"run_id": outcome["run_id"], "status": "completed"}
    print_json(result) if args.json else print(f"completed {result['run_id']}")


def cmd_block(args: argparse.Namespace) -> None:
    """Mark a workflow blocked with a durable reason."""
    def mutator(data: dict[str, Any]) -> None:
        data["status"] = "blocked"
        data["status_reason"] = args.reason
        data["status_message"] = args.message or args.reason
        data["blocked_by"] = args.blocked_by or "operator"
        workflow_state.add_event(
            data,
            "warning",
            f"workflow blocked: {args.reason}",
            kind="run",
            operation="blocked",
            source="workflow_ops.block",
            data={"reason": args.reason, "blocked_by": data["blocked_by"]},
        )

    workflow_state.mutate_run(args.run, mutator)
    print_json({"run_id": args.run, "status": "blocked", "reason": args.reason})


def cmd_pause(args: argparse.Namespace) -> None:
    """Pause a run through the operator interface."""
    workflow_state.cmd_pause(args)


def cmd_replace_agent(args: argparse.Namespace) -> None:
    """Replace a failed/cancelled agent with a fresh clone in the same phase.

    Copies prompt, write_scope, depends_on, worktree, and agent_type from the
    original. The original is marked cancelled. The replacement starts as
    ``pending`` unless ``--launch`` is given (which marks it ``running`` but
    does not actually start a process — use ``workflow run --attach-run`` or
    ``workflow apply`` for that).
    """
    import workflow_state as _ws

    def mutator(data: dict[str, Any]) -> dict[str, Any]:
        agents = data.setdefault("agents", [])
        original = None
        for candidate in agents:
            if candidate.get("agent_id") == args.agent or candidate.get("name") == args.agent:
                original = candidate
                break
        if original is None:
            raise SystemExit(f"agent not found: {args.agent!r} (by id or name)")
        original_status = str(original.get("status", ""))
        if args.allow_active is False and original_status not in ("failed", "cancelled", "running", "blocked"):
            raise SystemExit(
                f"agent {original.get('name', original.get('agent_id', ''))!r} is {original_status!r}; "
                "only failed/cancelled/running/blocked agents can be replaced "
                "(use --allow-active to override)"
            )
        original["status"] = "cancelled"
        original["summary"] = args.reason or "cancelled: replaced by retry agent"
        original["exit_code"] = original.get("exit_code")
        ts = workflow_state.now()
        original["completed_at"] = ts
        original["updated_at"] = ts

        new_id = args.new_agent_id or workflow_state.short_id("agent")
        workflow_state.ensure_unique(agents, "agent_id", new_id)
        suffix = args.suffix or "retry"
        new_name = args.name or f"{original.get('name', 'agent')}-{suffix}"
        clone: dict[str, Any] = {
            "agent_id": new_id,
            "phase_id": original.get("phase_id", args.phase or ""),
            "name": new_name,
            "role": original.get("role", ""),
            "agent_type": args.agent_type or original.get("agent_type", ""),
            "status": "running" if args.launch else "pending",
            "prompt": original.get("prompt", ""),
            "cwd": original.get("cwd", data.get("cwd")),
            "model": args.model or original.get("model", ""),
            "thread_id": "",
            "process_id": None,
            "process_group_id": None,
            "native_id": "",
            "write_scope": list(original.get("write_scope", [])),
            "jsonl_path": "",
            "log_path": "",
            "output_path": "",
            "summary": "replacement agent for " + str(original.get("name", original.get("agent_id", ""))),
            "result": "",
            "exit_code": None,
            "created_at": ts,
            "started_at": ts if args.launch else None,
            "completed_at": None,
            "updated_at": ts,
        }
        # Preserve dependency edges and worktree lane metadata
        if original.get("depends_on"):
            clone["depends_on"] = original["depends_on"]
        if original.get("worktree"):
            clone["worktree"] = dict(original["worktree"])
            clone["worktree"]["branch"] = clone["worktree"].get("branch", "") + f"-{suffix}"
        agents.append(clone)
        phase_id = clone.get("phase_id")
        if phase_id:
            phase = workflow_state.find_item(data.setdefault("phases", []), "phase_id", phase_id)
            if phase:
                phase.setdefault("agent_ids", []).append(clone["agent_id"])
        workflow_state.add_event(
            data,
            "info",
            f"agent replaced: {original.get('name', original.get('agent_id', ''))} → {new_name}",
            kind="agent",
            operation="replaced",
            source="workflow_ops.replace_agent",
            phase_id=phase_id,
            agent_id=clone["agent_id"],
            data={
                "original_agent_id": original.get("agent_id", ""),
                "original_name": original.get("name", ""),
                "new_agent_id": clone["agent_id"],
                "new_name": new_name,
                "reason": args.reason or "",
            },
        )
        return clone

    _, clone, path = workflow_state.mutate_run(args.run, mutator)
    print_json({
        "original_reference": args.agent,
        "new_agent_id": clone["agent_id"],
        "new_name": clone["name"],
        "status": clone["status"],
        "depends_on": clone.get("depends_on", ""),
        "path": str(path),
    })


def cmd_resume(args: argparse.Namespace) -> None:
    """Resume a paused run through the operator interface."""
    workflow_state.cmd_resume(args)


def cmd_stop(args: argparse.Namespace) -> None:
    """Stop a run through the operator interface."""
    workflow_state.cmd_stop(args)


def cmd_preview(args: argparse.Namespace) -> None:
    """Preview a worker launch without writing state."""
    jobs = []
    for item in args.job or []:
        name, prompt = item.split("::", 1) if "::" in item else (f"job-{len(jobs) + 1}", item)
        jobs.append({"name": name.strip(), "prompt": prompt.strip()})
    if args.jobs_file:
        loaded = json.loads(Path(args.jobs_file).expanduser().read_text(encoding="utf-8"))
        if not isinstance(loaded, list):
            raise SystemExit("--jobs-file must contain a JSON array")
        for item in loaded:
            jobs.append({"name": str(item.get("name") or item.get("role") or f"job-{len(jobs) + 1}"), "prompt": str(item.get("prompt", ""))})
    preview = {
        "title": args.title,
        "runner": args.runner,
        "ccc_runner": args.ccc_runner,
        "cwd": str(Path(args.cwd).expanduser().resolve()),
        "max_agents": args.max_agents,
        "startup_delay": args.startup_delay,
        "jobs": [{"name": job["name"], "prompt_chars": len(job["prompt"])} for job in jobs],
        "writes_state": False,
    }
    print_json(preview)


def cmd_open(args: argparse.Namespace) -> None:
    """Open the live TUI."""
    script = Path(__file__).resolve().with_name("workflow_tui.py")
    os.execv(sys.executable, [sys.executable, str(script), *args.tui_args])


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


def _add_status_parsers(sub: Any) -> None:
    """Add status, last, doctor, and check subparsers."""
    status = sub.add_parser("status", help="show active/recent workflow health")
    status.add_argument("--limit", type=positive_int, default=12)
    status.add_argument("--cwd", action="store_true", help="only show runs for the current cwd")
    status.add_argument("--all", action="store_true", help="include completed runs even when active runs exist")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    last = sub.add_parser("last", help="show the latest workflow run")
    last.add_argument("--cwd", action="store_true")
    last.add_argument("--json", action="store_true")
    last.add_argument("--id-only", action="store_true")
    last.set_defaults(func=cmd_last)

    doctor = sub.add_parser("doctor", help="check workflow install and dependency health")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    check = sub.add_parser("check", help="validate one workflow run")
    check.add_argument("run")
    check.add_argument("--json", action="store_true")
    check.add_argument("--stale-seconds", type=nonnegative_float, default=workflow_health.DEFAULT_STALE_SECONDS)
    check.set_defaults(func=cmd_check)


def _add_verify_parser(sub: Any) -> None:
    """Add verify subparser."""
    verify = sub.add_parser("verify", help="record or run a verification check")
    verify.add_argument("run")
    verify.add_argument("--cmd", help="shell command to run as verification")
    verify.add_argument("--name")
    verify.add_argument("--kind", default="verification")
    verify.add_argument("--cwd")
    verify.add_argument("--status", choices=["passed", "failed", "error", "skipped"])
    verify.add_argument("--summary")
    verify.add_argument("--check-id")
    verify.add_argument("--optional", action="store_true")
    verify.add_argument("--record-only", action="store_true")
    verify.add_argument("--evidence-path", help="path to external evidence for record-only verification")
    verify.add_argument("--external-ref", help="external reference URI or ticket for record-only verification")
    verify.set_defaults(func=cmd_verify)


def _add_merge_parsers(sub: Any) -> None:
    """Add merge-lanes and merge-conflicts subparsers."""
    merge_lanes = sub.add_parser("merge-lanes", help="merge completed worktree lane branches into the run cwd")
    merge_lanes.add_argument("run")
    merge_lanes.add_argument("--agent", action="append", help="agent id or name to merge; repeat to select several")
    merge_lanes.add_argument("--target", help="target branch to require instead of the current branch")
    merge_lanes.add_argument("--leave-conflicts", action="store_true", help="leave conflicted merge state in place instead of aborting")
    merge_lanes.add_argument("--no-resolve", action="store_true", help="skip automatic conflict resolution agent")
    merge_lanes.add_argument("--dry-run", action="store_true")
    merge_lanes.set_defaults(func=cmd_merge_lanes)

    merge_conflicts = sub.add_parser("merge-conflicts", help="prepare conflict context and merger-agent prompt for a failed merge")
    merge_conflicts.add_argument("run")
    merge_conflicts.add_argument("--agent", help="specific conflicted agent id to target (defaults to the first conflicted agent)")
    merge_conflicts.set_defaults(func=cmd_merge_conflicts)


def _add_lifecycle_parsers(sub: Any) -> None:
    """Add done, block, pause, resume, stop, and replace-agent subparsers."""
    done = sub.add_parser("done", help="complete a workflow after safety checks")
    done.add_argument("run")
    done.add_argument("--allow-unverified", action="store_true")
    done.add_argument("--force", action="store_true")
    done.add_argument("--reason")
    done.add_argument("--message")
    done.add_argument("--json", action="store_true")
    done.set_defaults(func=cmd_done)

    block = sub.add_parser("block", help="block a workflow with a durable reason")
    block.add_argument("run")
    block.add_argument("reason")
    block.add_argument("--message")
    block.add_argument("--blocked-by")
    block.set_defaults(func=cmd_block)

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

    replace = sub.add_parser("replace-agent", help="replace a failed/cancelled agent with a clone (same prompt/scope/deps)")
    replace.add_argument("run")
    replace.add_argument("agent", help="agent id or name to replace")
    replace.add_argument("--name", help="name for the replacement agent (default: <original>-retry)")
    replace.add_argument("--suffix", default="retry", help="suffix for auto-generated name and worktree branch (default: retry)")
    replace.add_argument("--agent-type", help="override agent_type for the replacement")
    replace.add_argument("--model", help="override model for the replacement")
    replace.add_argument("--phase", help="override phase_id for the replacement")
    replace.add_argument("--new-agent-id", help="explicit agent_id for the replacement (default: auto-generated)")
    replace.add_argument("--reason", help="reason for replacement (recorded in events)")
    replace.add_argument("--launch", action="store_true", help="mark the replacement as 'running' (does not start a process; use workflow run --attach-run or workflow apply)")
    replace.add_argument("--allow-active", action="store_true", help="allow replacing a running agent without cancelling first")
    replace.set_defaults(func=cmd_replace_agent)


def _add_utility_parsers(sub: Any) -> None:
    """Add preview, backlog, open, monitor, and watch subparsers."""
    preview = sub.add_parser("preview", help="preview a worker launch without writing state")
    preview.add_argument("--title", required=True)
    preview.add_argument("--runner", default="codex-direct", choices=["codex-direct", "ccc-codex", "ccc-opencode", "ccc", "opencode-direct", "kimi-direct"])
    preview.add_argument("--ccc-runner")
    preview.add_argument("--cwd", default=os.getcwd())
    preview.add_argument("--job", action="append")
    preview.add_argument("--jobs-file")
    preview.add_argument("--max-agents", type=positive_int, default=4)
    preview.add_argument("--startup-delay", type=nonnegative_float, default=1.0)
    preview.set_defaults(func=cmd_preview)

    backlog_cmd = sub.add_parser("backlog", help="create, register, and append to a durable workflow backlog")
    backlog_cmd.add_argument("run")
    backlog_cmd.add_argument("--append", help="finding text to append to the backlog")
    backlog_cmd.set_defaults(func=cmd_backlog)

    open_cmd = sub.add_parser("open", help="open the live workflow TUI")
    open_cmd.add_argument("tui_args", nargs=argparse.REMAINDER)
    open_cmd.set_defaults(func=cmd_open)

    monitor_cmd = sub.add_parser("monitor", help="compact one-shot status view")
    monitor_cmd.add_argument("--limit", type=workflow_monitor.positive_int, default=12)
    monitor_cmd.add_argument("--cwd", action="store_true", help="only show runs for the current cwd")
    monitor_cmd.add_argument("--all", action="store_true", help="include completed runs")
    monitor_cmd.add_argument("--json", action="store_true")
    monitor_cmd.add_argument("--no-color", action="store_true")
    monitor_cmd.add_argument("--agents", action="store_true", help="show per-agent detail rows")
    monitor_cmd.set_defaults(func=workflow_monitor.cmd_monitor)

    watch_cmd = sub.add_parser("watch", help="continuously refresh compact status")
    watch_cmd.add_argument("--limit", type=workflow_monitor.positive_int, default=12)
    watch_cmd.add_argument("--cwd", action="store_true", help="only show runs for the current cwd")
    watch_cmd.add_argument("--all", action="store_true", help="include completed runs")
    watch_cmd.add_argument("--json", action="store_true")
    watch_cmd.add_argument("--no-color", action="store_true")
    watch_cmd.add_argument("--agents", action="store_true", help="show per-agent detail rows")
    watch_cmd.add_argument("--interval", type=workflow_monitor.nonnegative_float, default=5.0, help="refresh interval in seconds")
    watch_cmd.set_defaults(func=workflow_monitor.cmd_watch)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    _add_status_parsers(sub)
    _add_verify_parser(sub)
    _add_merge_parsers(sub)
    _add_lifecycle_parsers(sub)
    _add_utility_parsers(sub)
    return parser


def friendly_missing_run(exc: FileNotFoundError) -> str:
    """Return a friendly run-not-found hint from a raw FileNotFoundError."""
    path = Path(str(exc.filename)) if exc.filename else None
    if path and path.name == "run.json":
        return f"no run {path.parent.name!r} (try: wf list)"
    return str(exc)


def main() -> None:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except FileNotFoundError as exc:
        raise SystemExit(friendly_missing_run(exc)) from None


if __name__ == "__main__":
    main()

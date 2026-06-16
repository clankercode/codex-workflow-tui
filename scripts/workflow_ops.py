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
import time
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

import workflow_health
import workflow_monitor
import workflow_state


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
    # The installed `workflow`/`wf` wrappers are typically symlinks to scripts/wf.
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


def structural_issues(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Return schema/path linkage issues not covered by live health."""
    issues: list[dict[str, Any]] = []
    phases = workflow_health.index_by(run.get("phases", []), "phase_id")
    agents = workflow_health.index_by(run.get("agents", []), "agent_id")
    valid_statuses = workflow_state.STATUS_VALUES
    run_status = run.get("status")
    if run_status not in valid_statuses:
        issues.append(
            workflow_health.issue(
                run=run,
                severity=workflow_health.CRITICAL,
                kind="invalid-status",
                title=f"Invalid run status: {run_status}",
                message=f"Expected one of {sorted(valid_statuses)}.",
                entity_type="run",
                entity_id=str(run.get("run_id", "")),
            )
        )
    for collection_name in ("phases", "agents"):
        for item in run.get(collection_name, []):
            status = item.get("status")
            if status not in valid_statuses:
                issues.append(
                    workflow_health.issue(
                        run=run,
                        severity=workflow_health.CRITICAL,
                        kind="invalid-status",
                        title=f"Invalid {collection_name[:-1]} status: {status}",
                        message=f"Expected one of {sorted(valid_statuses)}.",
                        entity_type=collection_name[:-1],
                        entity_id=str(item.get(f"{collection_name[:-1]}_id", "")),
                    )
                )
    for phase in run.get("phases", []):
        for agent_id in phase.get("agent_ids", []):
            if agent_id not in agents:
                issues.append(
                    workflow_health.issue(
                        run=run,
                        severity=workflow_health.WARNING,
                        kind="orphan-phase-agent",
                        title=f"Phase references missing agent: {agent_id}",
                        message=f"Phase {phase.get('phase_id')} has an agent_id not present in agents[].",
                        entity_type="phase",
                        phase_id=str(phase.get("phase_id", "")),
                    )
                )
    for agent in run.get("agents", []):
        phase_id = str(agent.get("phase_id") or "")
        if phase_id and phase_id not in phases:
            issues.append(
                workflow_health.issue(
                    run=run,
                    severity=workflow_health.WARNING,
                    kind="orphan-agent-phase",
                    title=f"Agent references missing phase: {agent.get('name', agent.get('agent_id'))}",
                    message=f"phase_id={phase_id} is not present in phases[].",
                    entity_type="agent",
                    agent_id=str(agent.get("agent_id", "")),
                    phase_id=phase_id,
                )
            )
    return issues


def run_verification_command(command: str, cwd: str | None) -> tuple[int, str, float]:
    """Run a verification command and return exit code, combined output, and duration."""
    start = time.time()
    result = subprocess.run(command, shell=True, cwd=cwd or None, text=True, capture_output=True)
    duration = time.time() - start
    output = ""
    if result.stdout:
        output += result.stdout
    if result.stderr:
        output += ("\n" if output else "") + result.stderr
    return result.returncode, output, duration


def cmd_verify(args: argparse.Namespace) -> None:
    """Run or record a verification check."""
    run = workflow_state.load_run(args.run)
    command = args.cmd or ""
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
    name = args.name or (command.split()[0] if command else "manual verification")
    cwd = args.cwd or run.get("cwd")
    exit_code = 0
    output = args.summary or ""
    duration = 0.0
    if command and not args.record_only:
        exit_code, output, duration = run_verification_command(command, cwd)
    status = args.status if args.record_only else ("passed" if exit_code == 0 else "failed")
    check_id = args.check_id or workflow_state.short_id("chk")
    evidence_path = args.evidence_path or ""
    external_ref = args.external_ref or ""

    def mutator(data: dict[str, Any]) -> dict[str, Any]:
        log_path = ""
        if output:
            logs_dir = Path(data.get("paths", {}).get("logs_dir") or Path(data.get("paths", {}).get("run_dir", ".")) / "logs")
            logs_dir.mkdir(parents=True, exist_ok=True)
            log_file = logs_dir / f"{check_id}.log"
            log_file.write_text(output, encoding="utf-8")
            log_path = str(log_file)
        check = {
            "check_id": check_id,
            "ts": workflow_state.now(),
            "name": name,
            "kind": args.kind,
            "status": status,
            "required": not args.optional,
            "command": command,
            "cwd": cwd,
            "exit_code": exit_code,
            "duration_seconds": round(duration, 3),
            "summary": first_line(output) or args.summary or status,
            "log_path": log_path,
            "completed_at": workflow_state.now(),
            "evidence_path": evidence_path,
            "external_ref": external_ref,
        }
        data.setdefault("checks", []).append(check)
        event_kind = "verification: external" if args.record_only else "check"
        event_data: dict[str, Any] = {"check_id": check_id, "name": name, "status": status, "required": check["required"]}
        if evidence_path:
            event_data["evidence_path"] = evidence_path
        if external_ref:
            event_data["external_ref"] = external_ref
        workflow_state.add_event(
            data,
            "error" if status in {"failed", "error"} else "info",
            f"verification {status}: {name}",
            kind=event_kind,
            operation="recorded",
            source="workflow_ops.verify",
            data=event_data,
        )
        return check

    _, check, _ = workflow_state.mutate_run(args.run, mutator)
    print_json(check)
    if check["status"] in {"failed", "error"}:
        raise SystemExit(exit_code or 1)


def completed_worktree_lane_agents(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Return completed agents with unmerged worktree branch metadata."""
    lanes = []
    for agent in run.get("agents", []):
        worktree = agent.get("worktree")
        if agent.get("status") != "completed" or not isinstance(worktree, dict):
            continue
        if not worktree.get("enabled", True) or not str(worktree.get("branch") or "").strip() or worktree.get("merged_at"):
            continue
        lanes.append(agent)
    return lanes


class ScopeCheckError(RuntimeError):
    """Raised when lane write_scope verification cannot be computed.

    A failed ``git diff`` (e.g. an unresolvable base ref) must not look like a
    clean "no violations" pass; the caller surfaces it as a visible warning.
    """


def lane_scope_violations(agent: dict[str, Any], cwd: str) -> list[str]:
    """Return files changed in a lane that fall outside declared write_scope.

    Returns a list of changed file paths that are not covered by any
    write_scope entry.  An empty write_scope means no scope restriction.

    Raises :class:`ScopeCheckError` when the underlying ``git diff`` cannot
    run, so the caller can record a visible "check skipped" warning instead of
    silently treating the failure as a clean pass.
    """
    write_scope = agent.get("write_scope") or []
    if not write_scope:
        return []
    worktree = agent.get("worktree") or {}
    branch = str(worktree.get("branch") or "").strip()
    base = str(worktree.get("base") or "HEAD").strip()
    if not branch:
        return []
    result = git_checked(cwd, "diff", "--name-only", f"{base}...{branch}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ScopeCheckError(detail or f"git diff {base}...{branch} failed (exit {result.returncode})")
    changed_files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    violations: list[str] = []
    for file_path in changed_files:
        covered = False
        for scope in write_scope:
            scope_str = str(scope).strip().rstrip("/")
            if not scope_str:
                continue
            if file_path == scope_str or file_path.startswith(scope_str + "/"):
                covered = True
                break
        if not covered:
            violations.append(file_path)
    return violations


def git_checked(cwd: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git and return captured output without raising."""
    return subprocess.run(["git", "-C", cwd, *args], text=True, capture_output=True, check=False)


def ensure_clean_git_tree(cwd: str) -> None:
    """Abort before merging lanes into a dirty checkout."""
    result = git_checked(cwd, "status", "--porcelain")
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or result.stdout.strip() or "failed to inspect git status")
    if result.stdout.strip():
        raise SystemExit("run cwd has uncommitted changes; commit/stash them before merge-lanes")


def current_git_branch(cwd: str) -> str:
    """Return the current branch for a checkout."""
    result = git_checked(cwd, "branch", "--show-current")
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or result.stdout.strip() or "failed to inspect git branch")
    return result.stdout.strip()


def merge_output(result: subprocess.CompletedProcess[str], *, status_output: str = "") -> str:
    """Return a durable merge log body."""
    parts = []
    if result.stdout:
        parts.append(result.stdout)
    if result.stderr:
        parts.append(result.stderr)
    if status_output:
        parts.append("git status --porcelain:\n" + status_output)
    return "\n".join(part.strip() for part in parts if part.strip())


def record_merge_check(
    run_id: str,
    agent_id: str,
    branch: str,
    *,
    cwd: str,
    status: str,
    exit_code: int,
    output: str,
    merge_commit: str = "",
) -> str:
    """Record one lane merge as a check and return its id."""
    check_id = workflow_state.short_id("chk")

    def mutator(data: dict[str, Any]) -> str:
        logs_dir = Path(data.get("paths", {}).get("logs_dir") or Path(data.get("paths", {}).get("run_dir", ".")) / "logs")
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = logs_dir / f"{check_id}.log"
        log_path.write_text(output or status, encoding="utf-8")
        check = {
            "check_id": check_id,
            "ts": workflow_state.now(),
            "name": f"merge lane {agent_id}",
            "kind": "merge",
            "status": status,
            "required": True,
            "command": f"git merge --no-edit {branch}",
            "cwd": cwd,
            "exit_code": exit_code,
            "duration_seconds": 0.0,
            "summary": first_line(output) or status,
            "log_path": str(log_path),
            "completed_at": workflow_state.now(),
            "evidence_path": "",
            "external_ref": merge_commit,
        }
        data.setdefault("checks", []).append(check)
        return check_id

    _, recorded_id, _ = workflow_state.mutate_run(run_id, mutator)
    return recorded_id


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


def cmd_merge_lanes(args: argparse.Namespace) -> None:
    """Merge completed workflow worktree branches back into the run cwd."""
    run = workflow_state.load_run(args.run)
    cwd = str(run.get("cwd") or "")
    if not cwd:
        raise SystemExit("run has no cwd; cannot merge worktree lanes")
    lanes = completed_worktree_lane_agents(run)
    if args.agent:
        selected = set(args.agent)
        lanes = [agent for agent in lanes if agent.get("agent_id") in selected or agent.get("name") in selected]
    if not lanes:
        print_json({"run_id": args.run, "merged": [], "skipped": [], "conflicts": []})
        return

    ensure_clean_git_tree(cwd)
    target_branch = args.target or current_git_branch(cwd)
    merged: list[str] = []
    conflicts: list[dict[str, str]] = []
    skipped: list[str] = []
    scope_warnings: list[dict[str, Any]] = []
    for agent in lanes:
        agent_id = str(agent.get("agent_id") or "")
        worktree = agent.get("worktree", {})
        branch = str(worktree.get("branch") or "").strip()
        merge_target = str(worktree.get("merge_target") or target_branch).strip()
        if current_git_branch(cwd) != target_branch:
            raise SystemExit(f"run cwd is not on target branch {target_branch!r}")
        if merge_target and target_branch != merge_target:
            raise SystemExit(f"lane {agent_id} targets {merge_target!r}; current target is {target_branch!r}")
        violations: list[str] = []
        scope_error = ""
        try:
            violations = lane_scope_violations(agent, cwd)
        except ScopeCheckError as exc:
            scope_error = str(exc)
        if violations or scope_error:
            scope_entry: dict[str, Any] = {"agent_id": agent_id, "violations": violations}
            if scope_error:
                scope_entry["error"] = scope_error
            scope_warnings.append(scope_entry)
            if not args.dry_run:
                if scope_error:
                    workflow_state.mutate_run(
                        args.run,
                        lambda data, agent_id=agent_id, msg=scope_error: workflow_state.add_event(
                            data,
                            "warning",
                            f"lane scope check could not run for {agent_id}: {msg}",
                            kind="worktree",
                            operation="scope-check-error",
                            source="workflow_ops.merge_lanes",
                            agent_id=agent_id,
                            data={"error": msg[:500]},
                        ),
                    )
                else:
                    workflow_state.mutate_run(
                        args.run,
                        lambda data, agent_id=agent_id, violations=violations: workflow_state.add_event(
                            data,
                            "warning",
                            f"lane scope violation: {agent_id} changed {len(violations)} file(s) outside write_scope",
                            kind="worktree",
                            operation="scope-violation",
                            source="workflow_ops.merge_lanes",
                            agent_id=agent_id,
                            data={"violations": violations[:20]},
                        ),
                    )
        if args.dry_run:
            skipped.append(agent_id)
            continue
        workflow_state.mutate_run(
            args.run,
            lambda data, agent_id=agent_id, branch=branch: workflow_state.add_event(
                data,
                "info",
                f"worktree lane merge started: {agent_id}",
                kind="worktree",
                operation="merge-started",
                source="workflow_ops.merge_lanes",
                agent_id=agent_id,
                data={"branch": branch, "target": target_branch},
            ),
        )
        result = git_checked(cwd, "merge", "--no-edit", branch)
        if result.returncode != 0:
            status_result = git_checked(cwd, "status", "--porcelain")
            output = merge_output(result, status_output=status_result.stdout)
            if not args.leave_conflicts:
                git_checked(cwd, "merge", "--abort")
            check_id = record_merge_check(args.run, agent_id, branch, cwd=cwd, status="failed", exit_code=result.returncode, output=output)
            conflicts.append({"agent_id": agent_id, "branch": branch, "message": first_line(output), "check_id": check_id})

            def conflict_agent_mutator(data: dict[str, Any]) -> None:
                state_agent = workflow_state.find_item(data.setdefault("agents", []), "agent_id", agent_id)
                worktree = state_agent.setdefault("worktree", {})
                worktree["merge_status"] = "conflicted"
                worktree["merge_check_id"] = check_id

            workflow_state.mutate_run(args.run, conflict_agent_mutator)
            break
        merge_commit = git_checked(cwd, "rev-parse", "HEAD").stdout.strip()
        check_id = record_merge_check(
            args.run,
            agent_id,
            branch,
            cwd=cwd,
            status="passed",
            exit_code=0,
            output=merge_output(result) or f"merged {branch}",
            merge_commit=merge_commit,
        )
        merged.append(agent_id)

        def mutator(data: dict[str, Any]) -> None:
            state_agent = workflow_state.find_item(data.setdefault("agents", []), "agent_id", agent_id)
            worktree = state_agent.setdefault("worktree", {})
            worktree["merged_at"] = workflow_state.now()
            worktree["merge_status"] = "merged"
            worktree["merge_commit"] = merge_commit
            worktree["merge_check_id"] = check_id
            workflow_state.add_event(
                data,
                "info",
                f"worktree lane merge succeeded: {state_agent.get('name', agent_id)}",
                kind="worktree",
                operation="merge-succeeded",
                source="workflow_ops.merge_lanes",
                agent_id=agent_id,
                phase_id=state_agent.get("phase_id"),
                data={"branch": branch, "merge_target": worktree.get("merge_target", ""), "check_id": check_id, "merge_commit": merge_commit},
            )

        workflow_state.mutate_run(args.run, mutator)

    if conflicts:
        conflict = conflicts[0]

        def conflict_mutator(data: dict[str, Any]) -> None:
            workflow_state.add_event(
                data,
                "warning",
                f"worktree lane merge conflict: {conflict['agent_id']}",
                kind="worktree",
                operation="merge-conflicted",
                source="workflow_ops.merge_lanes",
                agent_id=conflict["agent_id"],
                data={"branch": conflict["branch"], "message": conflict["message"], "check_id": conflict["check_id"]},
            )

        workflow_state.mutate_run(args.run, conflict_mutator)

        # Auto-resolve: dispatch an agent to fix the conflict
        no_resolve = getattr(args, "no_resolve", False) or os.environ.get("WORKFLOW_NO_RESOLVE") == "1"
        if not no_resolve:
            resolve_agent = next((a for a in lanes if a.get("agent_id") == conflict["agent_id"]), None)
            if resolve_agent:
                check = next((c for c in run.get("checks", []) if c.get("check_id") == conflict.get("check_id")), {})
                conflict_files: list[str] = []
                status_result = git_checked(cwd, "status", "--porcelain")
                if status_result.returncode == 0:
                    for line in status_result.stdout.strip().splitlines():
                        line = line.strip()
                        if line and line[:2] in {"UU", "AA", "DD", "AU", "UA", "DU", "UD"}:
                            conflict_files.append(line[3:])
                prompt = build_merger_prompt(run, resolve_agent, check, conflict_files, cwd)
                print(f"auto-resolving merge conflict for {conflict['agent_id']}...", file=sys.stderr)
                runner = getattr(args, "ccc_runner", None) or "@mimo25p"
                cmd = ["ccc", "--yolo", runner, "--", prompt]
                try:
                    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=300)
                    if result.returncode == 0:
                        # Re-attempt the merge (should be clean now)
                        retry = git_checked(cwd, "merge", "--no-edit", conflict["branch"])
                        if retry.returncode == 0:
                            merge_commit = git_checked(cwd, "rev-parse", "HEAD").stdout.strip()
                            record_merge_check(args.run, conflict["agent_id"], conflict["branch"], cwd=cwd, status="passed", exit_code=0, output=f"auto-resolved merge conflict", merge_commit=merge_commit)
                            def resolved_mutator(data: dict[str, Any]) -> None:
                                state_agent = workflow_state.find_item(data.setdefault("agents", []), "agent_id", conflict["agent_id"])
                                worktree = state_agent.setdefault("worktree", {})
                                worktree["merged_at"] = workflow_state.now()
                                worktree["merge_status"] = "merged"
                                worktree["merge_commit"] = merge_commit
                                workflow_state.add_event(data, "info", f"merge conflict auto-resolved: {state_agent.get('name', conflict['agent_id'])}", kind="worktree", operation="merge-resolved", source="workflow_ops.merge_lanes", agent_id=conflict["agent_id"], data={"branch": conflict["branch"], "merge_commit": merge_commit})
                            workflow_state.mutate_run(args.run, resolved_mutator)
                            merged.append(conflict["agent_id"])
                            conflicts.clear()
                        else:
                            print(f"auto-resolve succeeded but re-merge still conflicts", file=sys.stderr)
                    else:
                        print(f"auto-resolve agent failed (exit {result.returncode})", file=sys.stderr)
                except (subprocess.TimeoutExpired, OSError) as exc:
                    print(f"auto-resolve error: {exc}", file=sys.stderr)

    result_payload: dict[str, Any] = {"run_id": args.run, "merged": merged, "skipped": skipped, "conflicts": conflicts}
    if scope_warnings:
        result_payload["scope_warnings"] = scope_warnings
    print_json(result_payload)
    if conflicts:
        raise SystemExit(1)


def find_conflict_context(run: dict[str, Any], agent_id: str = "") -> tuple[dict[str, Any], dict[str, Any]]:
    """Find a conflicted agent and its failed merge check.

    Returns (agent, check) or raises SystemExit if no conflict is found.
    If ``agent_id`` is given, the matching conflicted agent must be found;
    otherwise the first conflicted agent is returned.
    """
    agents = run.get("agents", [])
    selected: dict[str, Any] | None = None
    if agent_id:
        for agent in agents:
            if agent.get("agent_id") != agent_id:
                continue
            if agent.get("worktree", {}).get("merge_status") != "conflicted":
                raise SystemExit(f"agent {agent_id!r} is not in conflicted state")
            selected = agent
            break
        if selected is None:
            raise SystemExit(f"agent {agent_id!r} not found in run")
    else:
        for agent in agents:
            if agent.get("worktree", {}).get("merge_status") == "conflicted":
                selected = agent
                break
    if selected is None:
        raise SystemExit("no conflicted merge found in run; run merge-lanes first and expect a conflict")
    check_id = selected.get("worktree", {}).get("merge_check_id") or ""
    if not check_id:
        raise SystemExit(
            f"conflicted agent {selected.get('agent_id')!r} has no merge_check_id; "
            "the recorded conflict is malformed and cannot be safely resolved"
        )
    for c in run.get("checks", []):
        if c.get("check_id") == check_id:
            return selected, c
    raise SystemExit(
        f"conflicted agent {selected.get('agent_id')!r} references missing check {check_id!r}; "
        "the recorded conflict is malformed and cannot be safely resolved"
    )


def build_merger_prompt(
    run: dict[str, Any],
    agent: dict[str, Any],
    check: dict[str, Any],
    conflict_files: list[str],
    cwd: str,
) -> str:
    """Build a bounded merger-agent prompt from conflict context."""
    agent_id = agent.get("agent_id", "")
    agent_name = agent.get("name", agent_id)
    branch = agent.get("worktree", {}).get("branch", "")
    target = agent.get("worktree", {}).get("merge_target", "")
    original_prompt = agent.get("prompt", "")
    merge_log = check.get("summary", "")
    check_log_path = check.get("log_path", "")
    log_content = ""
    if check_log_path and Path(check_log_path).is_file():
        try:
            log_content = Path(check_log_path).read_text(encoding="utf-8")[:4000]
        except OSError:
            pass

    parts = [
        "# Merge Conflict Resolution",
        "",
        "## Context",
        f"- Run: {run.get('run_id', '')}",
        f"- Agent: {agent_name} ({agent_id})",
        f"- Branch: `{branch}` -> `{target}`",
        f"- Working directory: {cwd}",
        "",
        "## Original Task",
        original_prompt or "(no prompt recorded)",
        "",
        "## Conflict Summary",
        merge_log or "(no merge summary)",
        "",
    ]
    if log_content:
        parts += [
            "## Merge Log (truncated)",
            "```",
            log_content,
            "```",
            "",
        ]
    if conflict_files:
        parts += [
            "## Conflicted Files",
            *(f"- `{f}`" for f in conflict_files),
            "",
        ]
    parts += [
        "## Instructions",
        "1. Examine each conflicted file and understand both sides of the change.",
        "2. Resolve conflicts preserving the intent of both the lane work and the target branch.",
        "3. After resolving, run tests or verification appropriate to the changed files.",
        "4. Stage resolved files and commit with a message like: `merge: resolve conflicts from {branch}`.",
        "5. Do NOT force-push or rewrite history. The resolution commit should be a normal merge commit.",
        "",
        "## Verification",
        "After resolution, record a passing verification:",
        f"  wf verify {run.get('run_id', '')} --record-only --status passed \\",
        "    --summary \"merge resolved\" --evidence-path <path to resolution evidence>",
        f"Then re-run `wf merge-lanes {run.get('run_id', '')}` so the lane is marked merged.",
        "The lane is NOT auto-skipped after a conflict: re-running merge-lanes re-attempts the merge,",
        "which completes cleanly (\"Already up to date\") once the resolution is committed, and then",
        "records the lane as merged.",
        "",
        "## Safety",
        "- If you cannot resolve a conflict cleanly, leave it staged and report what is blocked.",
        "- Do not silently drop either side's changes.",
        "- Human verification is required before marking the workflow complete.",
    ]
    return "\n".join(parts)


def cmd_merge_conflicts(args: argparse.Namespace) -> None:
    """Prepare conflict context and a merger-agent prompt for a failed merge."""
    run = workflow_state.load_run(args.run)
    cwd = str(run.get("cwd") or "")
    if not cwd:
        raise SystemExit("run has no cwd; cannot prepare merge conflict context")

    target_agent_id = str(getattr(args, "agent", "") or "")
    agent, check = find_conflict_context(run, agent_id=target_agent_id)
    agent_id = str(agent.get("agent_id") or "")
    worktree = agent.get("worktree", {})
    branch = str(worktree.get("branch") or "")

    conflict_files: list[str] = []
    git_status = git_checked(cwd, "status", "--porcelain")
    cwd_status = git_status.stdout if git_status.returncode == 0 else ""
    if cwd_status.strip():
        for line in cwd_status.strip().splitlines():
            line = line.strip()
            if line and line[:2] in {"UU", "AA", "DD", "AU", "UA", "DU", "UD"}:
                conflict_files.append(line[3:])

    cwd_has_conflict_markers = bool(conflict_files)
    cwd_has_unrelated_changes = bool(cwd_status.strip()) and not cwd_has_conflict_markers
    merge_in_progress = cwd_has_conflict_markers

    prompt = build_merger_prompt(run, agent, check, conflict_files, cwd)

    run_dir = Path(run.get("paths", {}).get("run_dir") or workflow_state.run_dir(args.run))
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    prompt_filename = f"merger-prompt-{agent_id}.md"
    prompt_path = artifacts_dir / prompt_filename
    prompt_path.write_text(prompt, encoding="utf-8")

    context = {
        "run_id": args.run,
        "agent_id": agent_id,
        "branch": branch,
        "target": worktree.get("merge_target", ""),
        "conflict_files": conflict_files,
        "cwd_has_conflict_markers": cwd_has_conflict_markers,
        "cwd_has_unrelated_changes": cwd_has_unrelated_changes,
        "merge_in_progress": merge_in_progress,
        "merge_check_id": check.get("check_id", ""),
        "prompt_path": str(prompt_path),
    }
    context_filename = f"merger-context-{agent_id}.json"
    context_path = artifacts_dir / context_filename
    context_path.write_text(json.dumps(context, indent=2, sort_keys=True), encoding="utf-8")

    def mutator(data: dict[str, Any]) -> None:
        data.setdefault("artifacts", []).append({
            "artifact_id": workflow_state.short_id("art"),
            "ts": workflow_state.now(),
            "kind": "merger-prompt",
            "title": f"merger prompt for {agent_id}",
            "path": str(prompt_path),
            "agent_id": agent_id,
        })
        data.setdefault("artifacts", []).append({
            "artifact_id": workflow_state.short_id("art"),
            "ts": workflow_state.now(),
            "kind": "merger-context",
            "title": f"merger context for {agent_id}",
            "path": str(context_path),
            "agent_id": agent_id,
        })
        workflow_state.add_event(
            data,
            "warning",
            f"merge conflict assist prepared: {agent_id}",
            kind="worktree",
            operation="merge-conflict-assist",
            source="workflow_ops.merge_conflicts",
            agent_id=agent_id,
            data={
                "branch": branch,
                "conflict_files": conflict_files,
                "cwd_has_conflict_markers": cwd_has_conflict_markers,
                "cwd_has_unrelated_changes": cwd_has_unrelated_changes,
                "merge_in_progress": merge_in_progress,
                "prompt_path": str(prompt_path),
                "context_path": str(context_path),
            },
        )

    workflow_state.mutate_run(args.run, mutator)
    result = {
        "run_id": args.run,
        "agent_id": agent_id,
        "branch": branch,
        "conflict_files": conflict_files,
        "cwd_has_conflict_markers": cwd_has_conflict_markers,
        "cwd_has_unrelated_changes": cwd_has_unrelated_changes,
        "merge_in_progress": merge_in_progress,
        "prompt_path": str(prompt_path),
        "context_path": str(context_path),
    }
    if not cwd_has_conflict_markers:
        if cwd_has_unrelated_changes:
            result["hint"] = (
                "No unmerged conflict markers found in the run cwd, but it has other uncommitted changes. "
                "`merge-lanes` refuses a dirty tree, so first commit or stash these unrelated changes, "
                "then re-create the conflict markers with `merge-lanes --leave-conflicts` (or "
                "`git merge --no-edit <branch>`) and re-run `merge-conflicts`."
            )
        else:
            result["hint"] = (
                "The run cwd is clean: the previous merge appears to have been aborted. "
                "Re-run `merge-lanes --leave-conflicts` (or `git merge --no-edit <branch>`) so the "
                "conflict markers are present, then re-run `merge-conflicts`."
            )
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


def first_line(text: str) -> str:
    """Return a compact first non-empty line."""
    for line in text.splitlines():
        clean = line.strip()
        if clean:
            return clean[:240]
    return ""


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

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

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
    issues = workflow_health.analyze_run(run)
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
                    "issues": workflow_health.analyze_run(run),
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
        issues = workflow_health.analyze_run(run)[:3]
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

    open_cmd = sub.add_parser("open", help="open the live workflow TUI")
    open_cmd.add_argument("tui_args", nargs=argparse.REMAINDER)
    open_cmd.set_defaults(func=cmd_open)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

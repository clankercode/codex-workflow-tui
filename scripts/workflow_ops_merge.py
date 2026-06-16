#!/usr/bin/env python3
"""Merge-lanes, conflict resolution, and scope-checking logic for workflow_ops."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

import workflow_state


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


def git_checked(cwd: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git and return captured output without raising."""
    return subprocess.run(["git", "-C", cwd, *args], text=True, capture_output=True, check=False)


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
    return _filter_scope_violations(changed_files, write_scope)


def _filter_scope_violations(changed_files: list[str], write_scope: list[Any]) -> list[str]:
    """Return files from *changed_files* not covered by any *write_scope* entry."""
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


def first_line(text: str) -> str:
    """Return a compact first non-empty line."""
    for line in text.splitlines():
        clean = line.strip()
        if clean:
            return clean[:240]
    return ""


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


def _prompt_header(run: dict[str, Any], agent: dict[str, Any], cwd: str) -> list[str]:
    """Return the header and context sections of a merger prompt."""
    agent_id = agent.get("agent_id", "")
    agent_name = agent.get("name", agent_id)
    branch = agent.get("worktree", {}).get("branch", "")
    target = agent.get("worktree", {}).get("merge_target", "")
    original_prompt = agent.get("prompt", "")
    return [
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
    ]


def _prompt_log_and_files(check: dict[str, Any], conflict_files: list[str]) -> list[str]:
    """Return the conflict summary, log, and file sections."""
    merge_log = check.get("summary", "")
    check_log_path = check.get("log_path", "")
    log_content = ""
    if check_log_path and Path(check_log_path).is_file():
        try:
            log_content = Path(check_log_path).read_text(encoding="utf-8")[:4000]
        except OSError:
            pass
    parts = ["## Conflict Summary", merge_log or "(no merge summary)", ""]
    if log_content:
        parts += ["## Merge Log (truncated)", "```", log_content, "```", ""]
    if conflict_files:
        parts += ["## Conflicted Files", *(f"- `{f}`" for f in conflict_files), ""]
    return parts


def _prompt_instructions(run: dict[str, Any], branch: str) -> list[str]:
    """Return the instructions and safety sections of a merger prompt."""
    return [
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


def build_merger_prompt(
    run: dict[str, Any],
    agent: dict[str, Any],
    check: dict[str, Any],
    conflict_files: list[str],
    cwd: str,
) -> str:
    """Build a bounded merger-agent prompt from conflict context."""
    branch = agent.get("worktree", {}).get("branch", "")
    parts = _prompt_header(run, agent, cwd)
    parts += _prompt_log_and_files(check, conflict_files)
    parts += _prompt_instructions(run, branch)
    return "\n".join(parts)


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


def _check_lane_scope(
    agent: dict[str, Any],
    run_id: str,
    *,
    cwd: str,
    dry_run: bool,
) -> tuple[list[str], str, list[dict[str, Any]]]:
    """Check scope violations for one lane; return (violations, error, warnings)."""
    agent_id = str(agent.get("agent_id") or "")
    violations: list[str] = []
    scope_error = ""
    try:
        violations = lane_scope_violations(agent, cwd)
    except ScopeCheckError as exc:
        scope_error = str(exc)
    warnings: list[dict[str, Any]] = []
    if violations or scope_error:
        entry: dict[str, Any] = {"agent_id": agent_id, "violations": violations}
        if scope_error:
            entry["error"] = scope_error
        warnings.append(entry)
        if not dry_run:
            _record_scope_warning(run_id, agent_id, violations, scope_error)
    return violations, scope_error, warnings


def _record_scope_warning(
    run_id: str,
    agent_id: str,
    violations: list[str],
    scope_error: str,
) -> None:
    """Persist a scope-violation or scope-check-error event."""
    if scope_error:
        workflow_state.mutate_run(
            run_id,
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
            run_id,
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


def _merge_single_lane(
    agent: dict[str, Any],
    run_id: str,
    target_branch: str,
    *,
    cwd: str,
    dry_run: bool,
    leave_conflicts: bool = False,
) -> dict[str, Any]:
    """Execute the merge for one lane; return outcome dict."""
    agent_id = str(agent.get("agent_id") or "")
    worktree = agent.get("worktree", {})
    branch = str(worktree.get("branch") or "").strip()
    merge_target = str(worktree.get("merge_target") or target_branch).strip()
    if current_git_branch(cwd) != target_branch:
        raise SystemExit(f"run cwd is not on target branch {target_branch!r}")
    if merge_target and target_branch != merge_target:
        raise SystemExit(f"lane {agent_id} targets {merge_target!r}; current target is {target_branch!r}")
    if dry_run:
        return {"status": "skipped", "agent_id": agent_id}
    _emit_merge_event(run_id, agent_id, branch, target_branch, "merge-started", f"worktree lane merge started: {agent_id}")
    result = git_checked(cwd, "merge", "--no-edit", branch)
    if result.returncode != 0:
        return _handle_merge_failure(run_id, agent_id, branch, result, cwd, leave_conflicts=leave_conflicts)
    return _handle_merge_success(run_id, agent_id, branch, result, cwd)


def _emit_merge_event(
    run_id: str,
    agent_id: str,
    branch: str,
    target_branch: str,
    operation: str,
    message: str,
) -> None:
    """Record a merge lifecycle event."""
    workflow_state.mutate_run(
        run_id,
        lambda data, agent_id=agent_id, branch=branch, target=target_branch, op=operation, msg=message: workflow_state.add_event(
            data,
            "info",
            msg,
            kind="worktree",
            operation=op,
            source="workflow_ops.merge_lanes",
            agent_id=agent_id,
            data={"branch": branch, "target": target},
        ),
    )


def _handle_merge_failure(
    run_id: str,
    agent_id: str,
    branch: str,
    result: subprocess.CompletedProcess[str],
    cwd: str,
    *,
    leave_conflicts: bool = False,
) -> dict[str, Any]:
    """Record a failed merge and return its outcome."""
    status_result = git_checked(cwd, "status", "--porcelain")
    output = merge_output(result, status_output=status_result.stdout)
    if not leave_conflicts:
        git_checked(cwd, "merge", "--abort")
    check_id = record_merge_check(
        run_id, agent_id, branch,
        cwd=cwd, status="failed", exit_code=result.returncode, output=output,
    )

    def mutator(data: dict[str, Any]) -> None:
        state_agent = workflow_state.find_item(data.setdefault("agents", []), "agent_id", agent_id)
        wt = state_agent.setdefault("worktree", {})
        wt["merge_status"] = "conflicted"
        wt["merge_check_id"] = check_id

    workflow_state.mutate_run(run_id, mutator)
    return {"status": "conflict", "agent_id": agent_id, "branch": branch, "message": first_line(output), "check_id": check_id}


def _handle_merge_success(
    run_id: str,
    agent_id: str,
    branch: str,
    result: subprocess.CompletedProcess[str],
    cwd: str,
) -> dict[str, Any]:
    """Record a successful merge and return its outcome."""
    merge_commit = git_checked(cwd, "rev-parse", "HEAD").stdout.strip()
    check_id = record_merge_check(
        run_id, agent_id, branch,
        cwd=cwd, status="passed", exit_code=0,
        output=merge_output(result) or f"merged {branch}",
        merge_commit=merge_commit,
    )

    def mutator(data: dict[str, Any]) -> None:
        state_agent = workflow_state.find_item(data.setdefault("agents", []), "agent_id", agent_id)
        wt = state_agent.setdefault("worktree", {})
        wt["merged_at"] = workflow_state.now()
        wt["merge_status"] = "merged"
        wt["merge_commit"] = merge_commit
        wt["merge_check_id"] = check_id
        workflow_state.add_event(
            data,
            "info",
            f"worktree lane merge succeeded: {state_agent.get('name', agent_id)}",
            kind="worktree",
            operation="merge-succeeded",
            source="workflow_ops.merge_lanes",
            agent_id=agent_id,
            phase_id=state_agent.get("phase_id"),
            data={"branch": branch, "merge_target": wt.get("merge_target", ""), "check_id": check_id, "merge_commit": merge_commit},
        )

    workflow_state.mutate_run(run_id, mutator)
    return {"status": "merged", "agent_id": agent_id}


def _try_auto_resolve(
    conflict: dict[str, Any],
    lanes: list[dict[str, Any]],
    run: dict[str, Any],
    args: argparse.Namespace,
) -> bool:
    """Attempt to auto-resolve a merge conflict. Returns True if resolved."""
    no_resolve = getattr(args, "no_resolve", False) or os.environ.get("WORKFLOW_NO_RESOLVE") == "1"
    if no_resolve:
        return False
    resolve_agent = next((a for a in lanes if a.get("agent_id") == conflict["agent_id"]), None)
    if not resolve_agent:
        return False
    cwd = str(run.get("cwd") or "")
    check = next((c for c in run.get("checks", []) if c.get("check_id") == conflict.get("check_id")), {})
    conflict_files = _collect_conflict_files(cwd)
    prompt = build_merger_prompt(run, resolve_agent, check, conflict_files, cwd)
    print(f"auto-resolving merge conflict for {conflict['agent_id']}...", file=sys.stderr)
    runner = getattr(args, "ccc_runner", None) or "@mimo25p"
    cmd = ["ccc", "--yolo", runner, "--", prompt]
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            return _attempt_post_resolve_merge(conflict, args, cwd)
        print(f"auto-resolve agent failed (exit {result.returncode})", file=sys.stderr)
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"auto-resolve error: {exc}", file=sys.stderr)
    return False


def _collect_conflict_files(cwd: str) -> list[str]:
    """Return conflict-marker file paths from git status."""
    conflict_files: list[str] = []
    status_result = git_checked(cwd, "status", "--porcelain")
    if status_result.returncode == 0:
        for line in status_result.stdout.strip().splitlines():
            line = line.strip()
            if line and line[:2] in {"UU", "AA", "DD", "AU", "UA", "DU", "UD"}:
                conflict_files.append(line[3:])
    return conflict_files


def _attempt_post_resolve_merge(
    conflict: dict[str, Any],
    args: argparse.Namespace,
    cwd: str,
) -> bool:
    """Re-attempt merge after auto-resolve; return True if it succeeds."""
    retry = git_checked(cwd, "merge", "--no-edit", conflict["branch"])
    if retry.returncode != 0:
        print("auto-resolve succeeded but re-merge still conflicts", file=sys.stderr)
        return False
    merge_commit = git_checked(cwd, "rev-parse", "HEAD").stdout.strip()
    record_merge_check(
        args.run, conflict["agent_id"], conflict["branch"],
        cwd=cwd, status="passed", exit_code=0,
        output="auto-resolved merge conflict", merge_commit=merge_commit,
    )

    def resolved_mutator(data: dict[str, Any]) -> None:
        state_agent = workflow_state.find_item(data.setdefault("agents", []), "agent_id", conflict["agent_id"])
        wt = state_agent.setdefault("worktree", {})
        wt["merged_at"] = workflow_state.now()
        wt["merge_status"] = "merged"
        wt["merge_commit"] = merge_commit
        workflow_state.add_event(
            data, "info",
            f"merge conflict auto-resolved: {state_agent.get('name', conflict['agent_id'])}",
            kind="worktree", operation="merge-resolved",
            source="workflow_ops.merge_lanes", agent_id=conflict["agent_id"],
            data={"branch": conflict["branch"], "merge_commit": merge_commit},
        )

    workflow_state.mutate_run(args.run, resolved_mutator)
    return True


def cmd_merge_lanes(args: argparse.Namespace) -> None:
    """Merge completed workflow worktree branches back into the run cwd."""
    run = workflow_state.load_run(args.run)
    cwd = str(run.get("cwd") or "")
    if not cwd:
        raise SystemExit("run has no cwd; cannot merge worktree lanes")
    lanes = completed_worktree_lane_agents(run)
    if args.agent:
        selected = set(args.agent)
        lanes = [a for a in lanes if a.get("agent_id") in selected or a.get("name") in selected]
    if not lanes:
        print(json.dumps({"run_id": args.run, "merged": [], "skipped": [], "conflicts": []}, indent=2, sort_keys=True))
        return
    ensure_clean_git_tree(cwd)
    target_branch = args.target or current_git_branch(cwd)
    merged: list[str] = []
    conflicts: list[dict[str, str]] = []
    skipped: list[str] = []
    scope_warnings: list[dict[str, Any]] = []
    for agent in lanes:
        agent_id = str(agent.get("agent_id") or "")
        _, _, sw = _check_lane_scope(agent, args.run, cwd=cwd, dry_run=args.dry_run)
        scope_warnings.extend(sw)
        if args.dry_run:
            skipped.append(agent_id)
            continue
        outcome = _merge_single_lane(agent, args.run, target_branch, cwd=cwd, dry_run=False, leave_conflicts=args.leave_conflicts)
        if outcome["status"] == "conflict":
            conflicts.append(outcome)
            break
        merged.append(agent_id)
    if conflicts:
        _emit_conflict_event(args.run, conflicts[0])
        if _try_auto_resolve(conflicts[0], lanes, run, args):
            merged.append(conflicts[0]["agent_id"])
            conflicts.clear()
    result_payload: dict[str, Any] = {"run_id": args.run, "merged": merged, "skipped": skipped, "conflicts": conflicts}
    if scope_warnings:
        result_payload["scope_warnings"] = scope_warnings
    print(json.dumps(result_payload, indent=2, sort_keys=True))
    if conflicts:
        raise SystemExit(1)


def _emit_conflict_event(run_id: str, conflict: dict[str, Any]) -> None:
    """Record a merge-conflict event."""
    def mutator(data: dict[str, Any]) -> None:
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
    workflow_state.mutate_run(run_id, mutator)


def _register_merger_artifacts(
    data: dict[str, Any],
    agent_id: str,
    prompt_path: Path,
    context_path: Path,
) -> None:
    """Append merger-prompt and merger-context artifacts to run data."""
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


def _check_cwd_conflicts(cwd: str) -> tuple[list[str], bool, bool, bool]:
    """Check cwd for conflict markers and unrelated changes.

    Returns (conflict_files, cwd_has_conflict_markers,
             cwd_has_unrelated_changes, merge_in_progress).
    """
    git_status = git_checked(cwd, "status", "--porcelain")
    cwd_status = git_status.stdout if git_status.returncode == 0 else ""
    conflict_files: list[str] = []
    if cwd_status.strip():
        for line in cwd_status.strip().splitlines():
            line = line.strip()
            if line and line[:2] in {"UU", "AA", "DD", "AU", "UA", "DU", "UD"}:
                conflict_files.append(line[3:])
    has_markers = bool(conflict_files)
    has_unrelated = bool(cwd_status.strip()) and not has_markers
    return conflict_files, has_markers, has_unrelated, has_markers


def _conflict_hint(
    cwd_has_conflict_markers: bool,
    cwd_has_unrelated_changes: bool,
) -> str:
    """Return a hint string for non-conflicted cwd states."""
    if cwd_has_unrelated_changes:
        return (
            "No unmerged conflict markers found in the run cwd, but it has other uncommitted changes. "
            "`merge-lanes` refuses a dirty tree, so first commit or stash these unrelated changes, "
            "then re-create the conflict markers with `merge-lanes --leave-conflicts` (or "
            "`git merge --no-edit <branch>`) and re-run `merge-conflicts`."
        )
    return (
        "The run cwd is clean: the previous merge appears to have been aborted. "
        "Re-run `merge-lanes --leave-conflicts` (or `git merge --no-edit <branch>`) so the "
        "conflict markers are present, then re-run `merge-conflicts`."
    )


def _write_conflict_artifacts(
    run: dict[str, Any],
    agent_id: str,
    prompt: str,
    context: dict[str, Any],
) -> tuple[Path, Path]:
    """Write merger prompt and context files; return their paths."""
    run_dir = Path(run.get("paths", {}).get("run_dir") or workflow_state.run_dir(run["run_id"]))
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = artifacts_dir / f"merger-prompt-{agent_id}.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    context_path = artifacts_dir / f"merger-context-{agent_id}.json"
    context_path.write_text(json.dumps(context, indent=2, sort_keys=True), encoding="utf-8")
    return prompt_path, context_path


def cmd_merge_conflicts(args: argparse.Namespace) -> None:
    """Prepare conflict context and a merger-agent prompt for a failed merge."""
    run = workflow_state.load_run(args.run)
    cwd = str(run.get("cwd") or "")
    if not cwd:
        raise SystemExit("run has no cwd; cannot prepare merge conflict context")
    target_agent_id = str(getattr(args, "agent", "") or "")
    agent, check = find_conflict_context(run, agent_id=target_agent_id)
    agent_id = str(agent.get("agent_id") or "")
    branch = str(agent.get("worktree", {}).get("branch") or "")
    conflict_files, has_markers, has_unrelated, merge_in_progress = _check_cwd_conflicts(cwd)
    prompt = build_merger_prompt(run, agent, check, conflict_files, cwd)
    context = {
        "run_id": args.run, "agent_id": agent_id, "branch": branch,
        "target": agent.get("worktree", {}).get("merge_target", ""),
        "conflict_files": conflict_files, "cwd_has_conflict_markers": has_markers,
        "cwd_has_unrelated_changes": has_unrelated, "merge_in_progress": merge_in_progress,
        "merge_check_id": check.get("check_id", ""),
    }
    prompt_path, context_path = _write_conflict_artifacts(run, agent_id, prompt, context)
    context["prompt_path"] = str(prompt_path)

    def mutator(data: dict[str, Any]) -> None:
        _register_merger_artifacts(data, agent_id, prompt_path, context_path)
        workflow_state.add_event(
            data, "warning", f"merge conflict assist prepared: {agent_id}",
            kind="worktree", operation="merge-conflict-assist",
            source="workflow_ops.merge_conflicts", agent_id=agent_id,
            data={"branch": branch, "conflict_files": conflict_files,
                  "cwd_has_conflict_markers": has_markers,
                  "cwd_has_unrelated_changes": has_unrelated,
                  "merge_in_progress": merge_in_progress,
                  "prompt_path": str(prompt_path), "context_path": str(context_path)},
        )

    workflow_state.mutate_run(args.run, mutator)
    result = dict(context)
    result["context_path"] = str(context_path)
    if not has_markers:
        result["hint"] = _conflict_hint(has_markers, has_unrelated)
    print(json.dumps(result, indent=2, sort_keys=True))


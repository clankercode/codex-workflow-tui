#!/usr/bin/env python3
"""Derived workflow health and attention helpers."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CRITICAL = "critical"
WARNING = "warning"
INFO = "info"
SEVERITY_RANK = {CRITICAL: 0, WARNING: 1, INFO: 2}
ACTIVE_STATUSES = {"pending", "running", "paused"}
BAD_STATUSES = {"failed", "blocked", "cancelled"}
LIFECYCLE_BLOCKING_KINDS = {
    "run-blocked",
    "run-cancelled",
    "phase-blocked",
    "phase-cancelled",
    "agent-blocked",
    "agent-cancelled",
}
DEFAULT_STALE_SECONDS = 30 * 60


def parse_timestamp(value: Any) -> datetime | None:
    """Parse a workflow timestamp into an aware UTC datetime."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def now_utc() -> datetime:
    """Return the current UTC time."""
    return datetime.now(UTC)


def seconds_since(value: Any, *, now: datetime | None = None) -> float | None:
    """Return age in seconds for a timestamp-like value."""
    parsed = parse_timestamp(value)
    if parsed is None:
        return None
    return max(0.0, ((now or now_utc()) - parsed).total_seconds())


def index_by(items: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    """Index workflow entities by one string key."""
    return {str(item.get(key)): item for item in items if item.get(key)}


def resolve_run_path(run: dict[str, Any], path_value: Any, fallback_dir: str | None = None) -> Path | None:
    """Resolve a path stored in run state relative to the run directory."""
    if not isinstance(path_value, str) or not path_value:
        return None
    path = Path(path_value).expanduser()
    if path.is_absolute():
        return path
    run_dir = Path(run.get("paths", {}).get("run_dir") or ".").expanduser()
    if fallback_dir and not str(path).startswith(f"{fallback_dir}/"):
        return run_dir / fallback_dir / path
    return run_dir / path


def path_has_content(path: Path | None) -> bool:
    """Return true when a path exists and is a non-empty file.

    An empty (zero-byte) final-output artifact is not useful output; treating
    it as "has output" would hide agents that finished with a blank artifact.
    """
    try:
        return path is not None and path.exists() and path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def agent_has_liveness_source(agent: dict[str, Any]) -> bool:
    """Return true when a running agent has enough metadata to inspect liveness."""
    if agent.get("unmanaged") is True:
        return True
    if agent.get("process_id") or agent.get("process_group_id"):
        return True
    if str(agent.get("jsonl_path") or "").strip():
        return True
    agent_type = str(agent.get("agent_type") or "")
    if agent_type == "native-subagent" and str(agent.get("thread_id") or "").strip():
        return True
    return False


def agent_is_effectively_unmanaged(agent: dict[str, Any]) -> bool:
    """Return true when an agent should be treated as unmanaged by the runner.

    Native subagents without a recorded process id are host-sidecar helpers
    that the workflow runner cannot inspect for liveness.  They should be
    classified as effectively unmanaged so health checks do not demand
    process transcripts that will never exist.
    """
    if agent.get("unmanaged") is True:
        return True
    agent_type = str(agent.get("agent_type") or "")
    if agent_type != "native-subagent":
        return False
    has_process = bool(agent.get("process_id") or agent.get("process_group_id"))
    has_jsonl = bool(str(agent.get("jsonl_path") or "").strip())
    return not has_process and not has_jsonl


def process_is_alive(pid: int) -> bool:
    """Return true when a process with *pid* is still running.

    Uses ``os.kill(pid, 0)`` — a POSIX-standard liveness probe that does not
    actually send a signal.  Returns ``False`` when the process does not exist
    or when the caller lacks permission to signal it.
    """
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def agent_process_is_dead(agent: dict[str, Any]) -> bool:
    """Return true when a running agent has a recorded PID that is no longer alive.

    Checks ``process_group_id`` first (the primary handle used by the runner),
    then falls back to ``process_id``.  Returns ``False`` when the agent has
    no recorded PID (the caller must handle opaque-running separately) or when
    the process is still alive.
    """
    if agent.get("status") != "running":
        return False
    pgid = agent.get("process_group_id")
    pid = agent.get("process_id")
    target = pgid or pid
    if not target:
        return False
    try:
        target_int = int(target)
    except (TypeError, ValueError):
        return False
    return not process_is_alive(target_int)


def issue(
    *,
    run: dict[str, Any],
    severity: str,
    kind: str,
    title: str,
    message: str,
    suggestion: str = "",
    entity_type: str = "run",
    entity_id: str | None = None,
    phase_id: str | None = None,
    agent_id: str | None = None,
    artifact_id: str | None = None,
    check_id: str | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
    """Build a normalized issue/attention row."""
    entity_id = entity_id or agent_id or phase_id or artifact_id or check_id or str(run.get("run_id", ""))
    return {
        "attention_id": f"{kind}:{run.get('run_id', '')}:{entity_id}",
        "severity": severity,
        "kind": kind,
        "title": title,
        "message": message,
        "suggestion": suggestion,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "run_id": run.get("run_id", ""),
        "run_title": run.get("title", ""),
        "phase_id": phase_id or "",
        "agent_id": agent_id or "",
        "artifact_id": artifact_id or "",
        "check_id": check_id or "",
        "ts": ts or run.get("updated_at") or run.get("created_at") or "",
    }


def latest_activity_at(run: dict[str, Any]) -> str:
    """Return the latest activity timestamp known for a run."""
    candidates = [run.get("last_activity_at"), run.get("updated_at"), run.get("created_at")]
    candidates.extend(event.get("ts") for event in run.get("events", []) if isinstance(event, dict))
    candidates.extend(agent.get("updated_at") for agent in run.get("agents", []) if isinstance(agent, dict))
    parsed = [(parse_timestamp(value), str(value)) for value in candidates if value]
    parsed = [(stamp, raw) for stamp, raw in parsed if stamp is not None]
    if not parsed:
        return ""
    return max(parsed, key=lambda item: item[0] or datetime.min.replace(tzinfo=UTC))[1]


def check_identity(check: dict[str, Any]) -> tuple[str, str, str, str]:
    """Return the verification identity used for rerun supersession."""
    return (
        str(check.get("kind") or "verification"),
        str(check.get("name") or ""),
        str(check.get("command") or ""),
        str(check.get("cwd") or ""),
    )


def check_sort_stamp(check: dict[str, Any], index: int = 0) -> tuple[float, int]:
    """Return a stable ordering key for check records."""
    stamp = parse_timestamp(check.get("completed_at") or check.get("ts"))
    return (stamp.timestamp() if stamp else 0.0, index)


def latest_checks(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the newest check for each verification identity."""
    by_identity: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    sort_keys: dict[tuple[str, str, str, str], tuple[float, int]] = {}
    for index, check in enumerate(run.get("checks", [])):
        identity = check_identity(check)
        sort_key = check_sort_stamp(check, index)
        if identity not in by_identity or sort_key >= sort_keys[identity]:
            by_identity[identity] = check
            sort_keys[identity] = sort_key
    return list(by_identity.values())


def check_is_valid_evidence(check: dict[str, Any]) -> bool:
    """Return whether a check can count as completion evidence."""
    if check.get("status") != "passed" or not bool(check.get("required", True)):
        return False
    command = str(check.get("command") or "")
    if command:
        return check.get("exit_code") in (0, "0")
    if not bool(str(check.get("summary") or "").strip()):
        return False
    # Commandless record-only passes must carry evidence provenance.
    return bool(check.get("evidence_path") or check.get("external_ref"))


def analyze_run(run: dict[str, Any], *, stale_seconds: float = DEFAULT_STALE_SECONDS, now: datetime | None = None) -> list[dict[str, Any]]:
    """Return actionable health findings for one workflow run."""
    now = now or now_utc()
    findings: list[dict[str, Any]] = []
    run_status = str(run.get("status", ""))
    if run_status == "failed":
        findings.append(
            issue(
                run=run,
                severity=CRITICAL,
                kind="run-failed",
                title="Run failed",
                message=str(run.get("status_message") or run.get("status_reason") or "The workflow run is failed."),
                suggestion="Inspect failed phases, agents, checks, and artifacts.",
            )
        )
    elif run_status in {"blocked", "cancelled"}:
        title = "Run blocked" if run_status == "blocked" else "Run cancelled"
        findings.append(
            issue(
                run=run,
                severity=WARNING,
                kind=f"run-{run_status}",
                title=title,
                message=str(run.get("status_message") or run.get("status_reason") or f"The workflow run is {run_status}."),
                suggestion="Resolve the lifecycle state or use --force if completing intentionally.",
            )
        )

    # A phase is "audited" if at least one agent or artifact is tied to it.
    # A completed phase with neither hides who (or whether anyone) did the work —
    # SKILL.md asks the lead to leave a lead-local agent (or artifact) trail.
    audited_phase_ids = {
        str(agent.get("phase_id", "")) for agent in run.get("agents", []) if agent.get("phase_id")
    } | {
        str(artifact.get("phase_id", "")) for artifact in run.get("artifacts", []) if artifact.get("phase_id")
    }

    for phase in run.get("phases", []):
        status = str(phase.get("status", ""))
        if status in BAD_STATUSES:
            severity = CRITICAL if status == "failed" else WARNING
            findings.append(
                issue(
                    run=run,
                    severity=severity,
                    kind=f"phase-{status}",
                    title=f"Phase {status}: {phase.get('name', phase.get('phase_id', 'phase'))}",
                    message=str(phase.get("status_message") or phase.get("status_reason") or phase.get("goal") or ""),
                    suggestion="Open the phase and inspect assigned agents.",
                    entity_type="phase",
                    entity_id=str(phase.get("phase_id", "")),
                    phase_id=str(phase.get("phase_id", "")),
                    ts=str(phase.get("completed_at") or phase.get("updated_at") or phase.get("started_at") or run.get("updated_at") or ""),
                )
            )
        elif status == "completed":
            phase_id = str(phase.get("phase_id", ""))
            if phase_id and phase_id not in audited_phase_ids:
                findings.append(
                    issue(
                        run=run,
                        severity=WARNING,
                        kind="phase-empty",
                        title=f"Phase completed with no agents: {phase.get('name', phase.get('phase_id', 'phase'))}",
                        message="This phase is completed but has no agents or artifacts recording who did the work.",
                        suggestion="Add a lead-local agent (or an artifact) before completing, or reopen the phase.",
                        entity_type="phase",
                        entity_id=phase_id,
                        phase_id=phase_id,
                        ts=str(phase.get("completed_at") or phase.get("updated_at") or run.get("updated_at") or ""),
                    )
                )

    for agent in run.get("agents", []):
        status = str(agent.get("status", ""))
        agent_id = str(agent.get("agent_id", ""))
        phase_id = str(agent.get("phase_id", ""))
        name = str(agent.get("name") or agent_id or "agent")
        if status in BAD_STATUSES:
            severity = CRITICAL if status == "failed" else WARNING
            findings.append(
                issue(
                    run=run,
                    severity=severity,
                    kind=f"agent-{status}",
                    title=f"Agent {status}: {name}",
                    message=str(agent.get("summary") or agent.get("result") or agent.get("status_message") or ""),
                    suggestion="Open the agent output and decide whether to retry, waive, or fix locally.",
                    entity_type="agent",
                    entity_id=agent_id,
                    phase_id=phase_id,
                    agent_id=agent_id,
                    ts=str(agent.get("updated_at") or agent.get("completed_at") or run.get("updated_at") or ""),
                )
            )
        if status == "running":
            if agent_process_is_dead(agent):
                findings.append(
                    issue(
                        run=run,
                        severity=CRITICAL,
                        kind="agent-process-dead",
                        title=f"Process dead: {name}",
                        message=f"Running agent has a recorded PID ({agent.get('process_group_id') or agent.get('process_id')}) but the process is no longer alive.",
                        suggestion="The worker was likely killed externally. Mark as failed and retry if needed.",
                        entity_type="agent",
                        entity_id=agent_id,
                        phase_id=phase_id,
                        agent_id=agent_id,
                        ts=str(agent.get("updated_at") or agent.get("started_at") or run.get("updated_at") or ""),
                    )
                )
            if not agent_has_liveness_source(agent) and not agent_is_effectively_unmanaged(agent):
                findings.append(
                    issue(
                        run=run,
                        severity=WARNING,
                        kind="agent-opaque-running",
                        title=f"Agent running without liveness source: {name}",
                        message="Running managed agent has no process id, transcript path, native id, or unmanaged marker.",
                        suggestion="Record process_id/jsonl_path/thread_id, mark it unmanaged, or update the agent status.",
                        entity_type="agent",
                        entity_id=agent_id,
                        phase_id=phase_id,
                        agent_id=agent_id,
                        ts=str(agent.get("updated_at") or agent.get("started_at") or run.get("updated_at") or ""),
                    )
                )
            last_update = agent.get("updated_at") or agent.get("started_at")
            age = seconds_since(last_update, now=now)
            if age is not None and age >= stale_seconds:
                findings.append(
                    issue(
                        run=run,
                        severity=WARNING,
                        kind="agent-stale",
                        title=f"Agent stale: {name}",
                        message=f"No recorded update since {last_update}.",
                        suggestion="Check the process/logs, then update or mark the agent blocked.",
                        entity_type="agent",
                        entity_id=agent_id,
                        phase_id=phase_id,
                        agent_id=agent_id,
                        ts=str(agent.get("updated_at") or agent.get("started_at") or ""),
                    )
                )
        output_path = resolve_run_path(run, agent.get("output_path"), "artifacts")
        output_missing = status == "completed" and output_path is not None and not output_path.exists()
        if output_missing:
            findings.append(
                issue(
                    run=run,
                    severity=WARNING,
                    kind="agent-output-missing",
                    title=f"Output missing: {name}",
                    message=f"Completed agent output is missing: {output_path}",
                    suggestion="Restore the artifact or update the agent output path.",
                    entity_type="agent",
                    entity_id=agent_id,
                    phase_id=phase_id,
                    agent_id=agent_id,
                    ts=str(agent.get("updated_at") or ""),
                )
            )
        if status in {"completed", "failed", "cancelled"}:
            has_output_text = bool(str(agent.get("result") or "").strip())
            has_summary = bool(str(agent.get("summary") or "").strip())
            has_output_file = path_has_content(output_path)
            has_jsonl = bool(str(agent.get("jsonl_path") or "").strip())
            has_latest_output = bool(str(agent.get("latest_output") or "").strip())
            # When the declared output file is missing, agent-output-missing already
            # covers the root cause; don't double-report it as an empty-output warning.
            if not output_missing and not has_output_text and not has_summary and not has_output_file and not has_latest_output:
                fallback_sources = []
                if has_jsonl:
                    fallback_sources.append("transcript")
                if agent.get("latest_tool_calls"):
                    fallback_sources.append("tool calls")
                if agent.get("exit_code") is not None:
                    fallback_sources.append("exit code")
                stop_result = agent.get("stop_result")
                if isinstance(stop_result, dict) and stop_result.get("reason"):
                    fallback_sources.append("termination result")
                if fallback_sources:
                    findings.append(
                        issue(
                            run=run,
                            severity=WARNING,
                            kind="agent-output-empty",
                            title=f"Empty output with fallback data: {name}",
                            message=f"Agent {status} with no final output; available fallback: {', '.join(fallback_sources)}.",
                            suggestion="Review transcript/tool events or update the agent result from available data.",
                            entity_type="agent",
                            entity_id=agent_id,
                            phase_id=phase_id,
                            agent_id=agent_id,
                            ts=str(agent.get("updated_at") or agent.get("completed_at") or ""),
                        )
                    )

    for check in latest_checks(run):
        status = str(check.get("status", ""))
        required = bool(check.get("required", True))
        if required and status == "passed" and not check_is_valid_evidence(check):
            findings.append(
                issue(
                    run=run,
                    severity=CRITICAL,
                    kind="check-invalid",
                    title=f"Check invalid: {check.get('name', check.get('check_id', 'check'))}",
                    message="Passing check evidence is malformed or contradicts the command exit status.",
                    suggestion="Rerun the verification or record external evidence with a non-empty summary.",
                    entity_type="check",
                    entity_id=str(check.get("check_id", "")),
                    check_id=str(check.get("check_id", "")),
                    ts=str(check.get("completed_at") or check.get("ts") or run.get("updated_at") or ""),
                )
            )
        if status in {"failed", "error"}:
            findings.append(
                issue(
                    run=run,
                    severity=CRITICAL if required else WARNING,
                    kind="check-failed",
                    title=f"Check failed: {check.get('name', check.get('check_id', 'check'))}",
                    message=str(check.get("summary") or check.get("command") or ""),
                    suggestion="Open the check log, fix the issue, and record a passing verification.",
                    entity_type="check",
                    entity_id=str(check.get("check_id", "")),
                    check_id=str(check.get("check_id", "")),
                    ts=str(check.get("completed_at") or check.get("ts") or run.get("updated_at") or ""),
                )
            )

    for artifact in run.get("artifacts", []):
        path = resolve_run_path(run, artifact.get("path"), "artifacts")
        if path is not None and not path.exists():
            findings.append(
                issue(
                    run=run,
                    severity=WARNING,
                    kind="artifact-missing",
                    title=f"Artifact missing: {artifact.get('title', artifact.get('artifact_id', 'artifact'))}",
                    message=f"Artifact path does not exist: {path}",
                    suggestion="Restore the file or update/remove the artifact record.",
                    entity_type="artifact",
                    entity_id=str(artifact.get("artifact_id", "")),
                    phase_id=str(artifact.get("phase_id", "")),
                    agent_id=str(artifact.get("agent_id", "")),
                    artifact_id=str(artifact.get("artifact_id", "")),
                    ts=str(artifact.get("ts") or run.get("updated_at") or ""),
                )
            )

    for event in run.get("events", []):
        if event.get("kind") == "event-log" and event.get("operation") == "rollover":
            findings.append(
                issue(
                    run=run,
                    severity=WARNING,
                    kind="event-log-rollover",
                    title="Event history rolled over",
                    message="Some older events were discarded due to the bounded event history (250 events).",
                    suggestion="Check durable artifacts and logs for the complete event history.",
                    ts=str(event.get("ts") or run.get("updated_at") or ""),
                )
            )
            break

    return sorted(findings, key=attention_sort_key)


def passed_checks(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Return checks that count as successful verification evidence."""
    return [check for check in latest_checks(run) if check_is_valid_evidence(check)]


def completion_blockers(run: dict[str, Any], *, allow_unverified: bool = False) -> list[dict[str, Any]]:
    """Return issues that should block `wf done`."""
    findings = [
        item
        for item in analyze_run(run)
        if item.get("severity") == CRITICAL or item.get("kind") in LIFECYCLE_BLOCKING_KINDS
    ]
    active_agents = [agent for agent in run.get("agents", []) if agent.get("status") in ACTIVE_STATUSES]
    for agent in active_agents:
        findings.append(
            issue(
                run=run,
                severity=CRITICAL,
                kind="agent-active",
                title=f"Agent still {agent.get('status')}: {agent.get('name', agent.get('agent_id', 'agent'))}",
                message="All agents must be completed or explicitly forced before workflow completion.",
                suggestion="Wait for the agent or update its status.",
                entity_type="agent",
                entity_id=str(agent.get("agent_id", "")),
                phase_id=str(agent.get("phase_id", "")),
                agent_id=str(agent.get("agent_id", "")),
                ts=str(agent.get("updated_at") or run.get("updated_at") or ""),
            )
        )
    active_phases = [phase for phase in run.get("phases", []) if phase.get("status") in ACTIVE_STATUSES]
    for phase in active_phases:
        findings.append(
            issue(
                run=run,
                severity=CRITICAL,
                kind="phase-active",
                title=f"Phase still {phase.get('status')}: {phase.get('name', phase.get('phase_id', 'phase'))}",
                message="All phases must be completed or explicitly forced before workflow completion.",
                suggestion="Finish the phase or update its status.",
                entity_type="phase",
                entity_id=str(phase.get("phase_id", "")),
                phase_id=str(phase.get("phase_id", "")),
                ts=str(phase.get("started_at") or run.get("updated_at") or ""),
            )
        )
    if not allow_unverified and not passed_checks(run):
        findings.append(
            issue(
                run=run,
                severity=CRITICAL,
                kind="verification-missing",
                title="Verification missing",
                message="No passing verification check is recorded for this run.",
                suggestion="Run `wf verify <run> --cmd '<verification command>'` or pass --allow-unverified.",
            )
        )
    return sorted(findings, key=attention_sort_key)


def attention_items(runs: list[dict[str, Any]], *, stale_seconds: float = DEFAULT_STALE_SECONDS) -> list[dict[str, Any]]:
    """Return attention rows for all runs, with an informational fallback."""
    items: list[dict[str, Any]] = []
    now = now_utc()
    for run in runs:
        items.extend(analyze_run(run, stale_seconds=stale_seconds, now=now))
    if items:
        return sorted(items, key=attention_sort_key)
    for run in runs[:10]:
        latest = latest_activity_at(run)
        items.append(
            issue(
                run=run,
                severity=INFO,
                kind="run-healthy",
                title=f"No blockers: {run.get('title', run.get('run_id', 'run'))}",
                message=f"Status {run.get('status', 'unknown')}; latest activity {latest or 'unknown'}.",
                suggestion="Continue monitoring or open the run detail.",
                ts=latest,
            )
        )
    return sorted(items, key=attention_sort_key)


def attention_sort_key(item: dict[str, Any]) -> tuple[int, float, str]:
    """Sort attention by severity, newest timestamp, then stable id."""
    stamp = parse_timestamp(item.get("ts"))
    epoch = stamp.timestamp() if stamp else 0.0
    return (SEVERITY_RANK.get(str(item.get("severity")), 99), -epoch, str(item.get("attention_id", "")))


def status_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    """Count workflow items by status."""
    counts: dict[str, int] = {}
    for item in items:
        status = str(item.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
    return counts

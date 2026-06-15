#!/usr/bin/env python3
"""Shared workflow-plan loading and normalization helpers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import workflow_state

EXECUTION_FIELDS = {
    "cwd",
    "runner",
    "ccc_runner",
    "ccc_control",
    "ccc_output_mode",
    "permission_mode",
    "cli_agent",
    "timeout_secs",
    "quota_retries",
    "quota_retry_buffer_secs",
    "failure_retries",
    "result_schema",
    "kimi_max_steps_per_turn",
    "model",
    "sandbox",
    "approval",
    "max_agents",
    "max_round",
    "max_job",
    "startup_delay",
    "dry_run",
    "mock",
}


def normalize_workflow(raw: dict[str, Any], *, fallback_title: str) -> dict[str, Any]:
    """Validate and normalize a workflow-plan object."""
    if isinstance(raw.get("phases"), list):
        phases_raw = raw["phases"]
    else:
        phases_raw = [{"name": "main", "jobs": raw.get("jobs")}]
    phases: list[dict[str, Any]] = []
    for phase_index, phase_raw in enumerate(phases_raw):
        if not isinstance(phase_raw, dict):
            raise SystemExit("each workflow phase must be an object")
        phase_name = workflow_state.slugify(str(phase_raw.get("name") or phase_raw.get("phase") or f"phase-{phase_index + 1}"), fallback=f"phase-{phase_index + 1}")
        jobs_raw = phase_raw.get("jobs")
        if not isinstance(jobs_raw, list) or not jobs_raw:
            raise SystemExit("each workflow phase must contain a non-empty jobs array")
        jobs = [normalize_job(item, index) for index, item in enumerate(jobs_raw)]
        phase = {
                "name": phase_name,
                "title": str(phase_raw.get("title") or phase_raw.get("name") or phase_name).strip(),
                "summary": str(phase_raw.get("summary") or "").strip(),
                "goal": str(phase_raw.get("goal") or phase_raw.get("summary") or "").strip(),
                "gates": normalize_object_list(phase_raw.get("gates"), "phase gates"),
                "planned_checks": normalize_object_list(phase_raw.get("checks") or phase_raw.get("planned_checks"), "phase checks"),
                "decisions": normalize_object_list(phase_raw.get("decisions"), "phase decisions"),
                "jobs": jobs,
            }
        for field in EXECUTION_FIELDS:
            if field in phase_raw:
                phase[field] = phase_raw[field]
        if "ccc_control" in phase:
            phase["ccc_control"] = normalize_string_list(phase["ccc_control"], "phase ccc_control")
        phases.append(phase)
    plan = {
        "schema_version": int(raw.get("schema_version") or 1),
        "kind": str(raw.get("kind") or "workflow-plan"),
        "title": str(raw.get("title") or fallback_title).strip()[:120],
        "summary": str(raw.get("summary") or "").strip(),
        "goal": str(raw.get("goal") or "").strip(),
        "output_subdir": normalize_output_subdir(raw.get("output_subdir")),
        "phases": phases,
        "jobs": phases[0]["jobs"] if len(phases) == 1 else [],
    }
    tags = raw.get("tags", raw.get("tag"))
    if isinstance(tags, list):
        plan["tags"] = [str(tag) for tag in tags if str(tag).strip()]
    elif tags:
        plan["tags"] = [str(tags)]
    for field in EXECUTION_FIELDS:
        if field in raw:
            plan[field] = raw[field]
    plan["decisions"] = normalize_object_list(raw.get("decisions"), "workflow decisions")
    plan["gates"] = normalize_object_list(raw.get("gates"), "workflow gates")
    if "ccc_control" in plan:
        plan["ccc_control"] = normalize_string_list(plan["ccc_control"], "ccc_control")
    return plan


def normalize_job(item: Any, index: int) -> dict[str, Any]:
    """Validate and normalize one workflow job."""
    if not isinstance(item, dict):
        raise SystemExit("each workflow job must be an object")
    prompt = str(item.get("prompt") or "").strip()
    if not prompt:
        raise SystemExit("each workflow job must include a non-empty prompt")
    name = str(item.get("name") or item.get("role") or f"job-{index + 1}").strip()
    job = {"name": name, "role": str(item.get("role") or name).strip() or name, "prompt": prompt}
    for field in ("stage", "depends_on", "schema", "cwd", "write_scope", "worktree"):
        if item.get(field):
            job[field] = item[field]
    for field in EXECUTION_FIELDS:
        if field in item:
            job[field] = item[field]
    if "ccc_control" in job:
        job["ccc_control"] = normalize_string_list(job["ccc_control"], "job ccc_control")
    return job


def normalize_output_subdir(value: Any) -> str:
    """Return a safe relative output directory from workflow metadata."""
    text = str(value or "planning-output").strip() or "planning-output"
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        raise SystemExit("workflow output_subdir must be a relative path without '..'")
    return path.as_posix()


def normalize_string_list(value: Any, field_name: str) -> list[str]:
    """Normalize a string-or-list field to a list of non-empty strings."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    raise SystemExit(f"workflow {field_name} must be a string or list of strings")


def normalize_object_list(value: Any, field_name: str) -> list[dict[str, Any]]:
    """Normalize optional metadata lists while keeping their structured fields."""
    if value in (None, ""):
        return []
    if isinstance(value, dict):
        return [dict(value)]
    if not isinstance(value, list):
        raise SystemExit(f"{field_name} must be an object or list of objects")
    items: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise SystemExit(f"{field_name} entries must be objects")
        items.append(dict(item))
    return items


def load_workflow_source(source: str, *, fallback_title: str, cwd: Path | None = None) -> dict[str, Any]:
    """Load and normalize a saved workflow plan or script that prints one."""
    path = Path(source).expanduser().resolve()
    if not path.exists():
        raise SystemExit(f"workflow plan source does not exist: {path}")
    raw = _load_json_file(path)
    if raw is None:
        raw = _run_workflow_script(path, cwd=cwd)
    if not isinstance(raw, dict):
        raise SystemExit("workflow plan source must produce a JSON object")
    return normalize_workflow(raw, fallback_title=fallback_title)


def _load_json_file(path: Path) -> Any | None:
    with path.open(encoding="utf-8") as handle:
        text = handle.read()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _run_workflow_script(path: Path, *, cwd: Path | None) -> Any:
    if not os.access(path, os.X_OK) and path.suffix != ".py":
        raise SystemExit(f"workflow plan source is neither JSON nor executable: {path}")
    command = [str(path)]
    if path.suffix == ".py":
        command = [sys.executable, str(path)]
    result = subprocess.run(command, text=True, capture_output=True, cwd=str(cwd) if cwd else None, check=False)
    if result.returncode != 0:
        raise SystemExit(f"workflow plan script failed: {result.stderr.strip() or result.stdout.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"workflow plan script returned invalid JSON: {exc}") from exc

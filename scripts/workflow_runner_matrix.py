#!/usr/bin/env python3
"""Run the same reusable smoke workflow across multiple runner targets."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

import workflow_run_codex
import workflow_state


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
COMMON_TARGETS = ["codex-direct", "kimi-direct", "opencode-direct", "ccc:opencode", "ccc:kimi"]
DIRECT_RUNNERS = {"codex-direct", "ccc-codex", "ccc-opencode", "opencode-direct", "kimi-direct"}
DEFAULT_JOBS = [
    {
        "name": "identity",
        "role": "smoke-test",
        "prompt": "Do not modify files. Reply with exactly: WORKFLOW_RUNNER_SMOKE_ALPHA",
    },
    {
        "name": "arithmetic",
        "role": "smoke-test",
        "prompt": "Do not modify files. Compute 17 * 23 and return the final integer.",
    },
    {
        "name": "structured",
        "role": "smoke-test",
        "prompt": 'Do not modify files. Return a JSON object with "runner_smoke": true and "answer": "ok".',
    },
]


@dataclass(frozen=True)
class MatrixTarget:
    """One runner target in a reusable smoke matrix."""

    label: str
    runner: str
    ccc_runner: str | None = None


@dataclass(frozen=True)
class WorkflowSpec:
    """One concrete workflow job list ready to execute for a target."""

    title: str
    workflow_file: Path
    phases: list[dict[str, Any]]
    output_subdir: str = "planning-output"


def parse_target(value: str) -> MatrixTarget:
    """Parse target specs like `kimi-direct`, `ccc:@mm`, or `label=ccc:kimi`."""
    label: str | None = None
    body = value.strip()
    if not body:
        raise argparse.ArgumentTypeError("target must not be empty")
    if "=" in body:
        label, body = body.split("=", 1)
        label = workflow_state.slugify(label, fallback="target")
    if body.startswith("ccc:"):
        selector = body.split(":", 1)[1].strip()
        if not selector:
            raise argparse.ArgumentTypeError("ccc targets must include a selector, e.g. ccc:kimi or ccc:@mm")
        default_label = f"ccc-{workflow_state.slugify(selector[1:] if selector.startswith('@') else selector, fallback='target')}"
        return MatrixTarget(label or default_label, "ccc", selector)
    if body in DIRECT_RUNNERS or body == "ccc":
        if body == "ccc":
            raise argparse.ArgumentTypeError("generic ccc targets must use ccc:<selector>")
        return MatrixTarget(label or body, body)
    raise argparse.ArgumentTypeError(f"unknown target {value!r}")


def parse_created_run(stdout: str) -> dict[str, Any]:
    """Parse the leading JSON object emitted by workflow_run.py."""
    text = stdout.lstrip()
    if not text:
        return {}
    with contextlib.suppress(json.JSONDecodeError):
        loaded, _ = json.JSONDecoder().raw_decode(text)
        if isinstance(loaded, dict):
            return loaded
    return {}


def unique_targets(raw_targets: list[str]) -> list[MatrixTarget]:
    """Parse target specs and reject duplicate labels."""
    targets = [parse_target(item) for item in raw_targets]
    seen: set[str] = set()
    for target in targets:
        if target.label in seen:
            raise SystemExit(f"duplicate target label: {target.label}")
        seen.add(target.label)
    return targets


def parse_target_max(values: list[str] | None) -> dict[str, int]:
    """Parse per-target concurrency overrides like `kimi=4`."""
    parsed: dict[str, int] = {}
    for value in values or []:
        if "=" not in value:
            raise argparse.ArgumentTypeError("--target-max must use label=N")
        label, raw_count = value.split("=", 1)
        label = workflow_state.slugify(label, fallback="target")
        try:
            count = workflow_run_codex.positive_int(raw_count)
        except (TypeError, ValueError, argparse.ArgumentTypeError) as exc:
            raise argparse.ArgumentTypeError(f"invalid --target-max {value!r}: {exc}") from exc
        parsed[label] = count
    return parsed


def format_arg(value: str, *, target: MatrixTarget, workdir: Path, output_dir: Path) -> str:
    """Expand script argument placeholders for one target."""
    return value.format(
        target=target.label,
        label=target.label,
        project_dir=str(workdir),
        workdir=str(workdir),
        output_dir=str(output_dir),
    )


def normalize_workflow(raw: dict[str, Any], *, fallback_title: str) -> dict[str, Any]:
    """Validate and normalize a script-generated workflow object."""
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
        phases.append(
            {
                "name": phase_name,
                "title": str(phase_raw.get("title") or phase_raw.get("name") or phase_name).strip(),
                "summary": str(phase_raw.get("summary") or "").strip(),
                "jobs": jobs,
            }
        )
    return {
        "schema_version": int(raw.get("schema_version") or 1),
        "kind": str(raw.get("kind") or "workflow-plan"),
        "title": str(raw.get("title") or fallback_title).strip()[:120],
        "summary": str(raw.get("summary") or "").strip(),
        "goal": str(raw.get("goal") or "").strip(),
        "output_subdir": normalize_output_subdir(raw.get("output_subdir")),
        "phases": phases,
        "jobs": phases[0]["jobs"] if len(phases) == 1 else [],
    }


def normalize_job(item: Any, index: int) -> dict[str, Any]:
    """Validate and normalize one workflow job."""
    if not isinstance(item, dict):
        raise SystemExit("each workflow job must be an object")
    prompt = str(item.get("prompt") or "").strip()
    if not prompt:
        raise SystemExit("each workflow job must include a non-empty prompt")
    name = str(item.get("name") or item.get("role") or f"job-{index + 1}").strip()
    return {"name": name, "role": str(item.get("role") or name).strip() or name, "prompt": prompt}


def normalize_output_subdir(value: Any) -> str:
    """Return a safe relative output directory from workflow metadata."""
    text = str(value or "planning-output").strip() or "planning-output"
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        raise SystemExit("workflow output_subdir must be a relative path without '..'")
    return path.as_posix()


def write_default_workflow(args: argparse.Namespace, output_dir: Path) -> WorkflowSpec:
    """Return a jobs file, creating the built-in reusable smoke workflow if needed."""
    if args.jobs_file:
        source_jobs_file = Path(args.jobs_file).expanduser().resolve()
        loaded = json.loads(source_jobs_file.read_text(encoding="utf-8"))
        raw_workflow = loaded if isinstance(loaded, dict) else {"title": args.title, "jobs": loaded}
        workflow = normalize_workflow(raw_workflow, fallback_title=args.title)
        workflow_file = output_dir / "workflow.json"
        workflow_file.write_text(json.dumps(workflow, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return WorkflowSpec(workflow["title"], workflow_file, write_phase_jobs(workflow, output_dir / "workflows", "main"), str(workflow.get("output_subdir") or "planning-output"))
    bundled = SKILL_DIR / "examples" / "runner-smoke-jobs.json"
    jobs_path = output_dir / "runner-smoke-jobs.json"
    if bundled.exists():
        shutil.copy2(bundled, jobs_path)
    else:
        jobs_path.write_text(json.dumps(DEFAULT_JOBS, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    workflow = normalize_workflow({"title": args.title, "jobs": json.loads(jobs_path.read_text(encoding="utf-8"))}, fallback_title=args.title)
    workflow_file = output_dir / "workflow.json"
    workflow_file.write_text(json.dumps(workflow, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    workflow["phases"][0]["jobs_file"] = str(jobs_path)
    return WorkflowSpec(workflow["title"], workflow_file, workflow["phases"], str(workflow.get("output_subdir") or "planning-output"))


def write_phase_jobs(workflow: dict[str, Any], workflow_dir: Path, target_label: str) -> list[dict[str, Any]]:
    """Write one jobs-array file per workflow phase."""
    workflow_dir.mkdir(parents=True, exist_ok=True)
    phases: list[dict[str, Any]] = []
    for phase in workflow["phases"]:
        phase_copy = dict(phase)
        jobs_file = workflow_dir / f"{target_label}.{phase['name']}.jobs.json"
        jobs_file.write_text(json.dumps(phase["jobs"], indent=2, sort_keys=True) + "\n", encoding="utf-8")
        phase_copy["jobs_file"] = str(jobs_file)
        phases.append(phase_copy)
    return phases


def run_workflow_script(args: argparse.Namespace, target: MatrixTarget, workdir: Path, output_dir: Path) -> WorkflowSpec:
    """Run a workflow-generator script and save the exact JSON used for execution."""
    script = Path(args.workflow_script).expanduser().resolve()
    script_args = [format_arg(item, target=target, workdir=workdir, output_dir=output_dir) for item in args.workflow_script_arg or []]
    command = [str(script), *script_args]
    if script.suffix == ".py":
        command = [sys.executable, str(script), *script_args]
    result = subprocess.run(command, text=True, capture_output=True, cwd=str(workdir))
    if result.returncode != 0:
        raise SystemExit(f"workflow script failed for {target.label}: {result.stderr.strip() or result.stdout.strip()}")
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"workflow script returned invalid JSON for {target.label}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SystemExit(f"workflow script must return a JSON object for {target.label}")
    workflow = normalize_workflow(raw, fallback_title=args.title)
    workflow_dir = output_dir / "workflows"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    workflow_file = workflow_dir / f"{target.label}.workflow.json"
    workflow_file.write_text(json.dumps(workflow, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    phases = write_phase_jobs(workflow, workflow_dir, target.label)
    return WorkflowSpec(workflow["title"], workflow_file, phases, str(workflow.get("output_subdir") or "planning-output"))


def copy_project_for_target(args: argparse.Namespace, target: MatrixTarget, output_dir: Path) -> Path:
    """Return the working directory for a target, copying --project-src when provided."""
    if not args.project_src:
        return Path(args.cwd).expanduser().resolve()
    source = Path(args.project_src).expanduser().resolve()
    workdir = output_dir / "workdirs" / target.label
    if workdir.exists():
        shutil.rmtree(workdir)
    shutil.copytree(source, workdir, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", ".mypy_cache"))
    return workdir


def build_command(target: MatrixTarget, args: argparse.Namespace, spec: WorkflowSpec, phase: dict[str, Any], workdir: Path, max_agents: int) -> list[str]:
    """Build a workflow_run.py command for one target."""
    command = [
        sys.executable,
        str(SCRIPT_DIR / "workflow_run.py"),
        "--title",
        f"{spec.title} [{target.label}] {phase['title']}",
        "--cwd",
        str(workdir),
        "--runner",
        target.runner,
        "--jobs-file",
        str(phase["jobs_file"]),
        "--max-agents",
        str(max_agents),
        "--startup-delay",
        str(args.startup_delay),
    ]
    if target.ccc_runner:
        command.extend(["--ccc-runner", target.ccc_runner])
    if args.tag:
        for tag in args.tag:
            command.extend(["--tag", tag])
    if args.model:
        command.extend(["--model", args.model])
    if args.sandbox:
        command.extend(["--sandbox", args.sandbox])
    if args.approval:
        command.extend(["--approval", args.approval])
    if args.permission_mode:
        command.extend(["--permission-mode", args.permission_mode])
    if args.timeout_secs:
        command.extend(["--timeout-secs", str(args.timeout_secs)])
    if args.quota_retries is not None:
        command.extend(["--quota-retries", str(args.quota_retries)])
    if args.quota_retry_buffer_secs is not None:
        command.extend(["--quota-retry-buffer-secs", str(args.quota_retry_buffer_secs)])
    if args.failure_retries is not None:
        command.extend(["--failure-retries", str(args.failure_retries)])
    if args.kimi_max_steps_per_turn is not None:
        command.extend(["--kimi-max-steps-per-turn", str(args.kimi_max_steps_per_turn)])
    if args.ccc_output_mode:
        command.extend(["--ccc-output-mode", args.ccc_output_mode])
    for control in args.ccc_control or []:
        command.extend(["--ccc-control", control])
    if args.mock:
        command.append("--mock")
    if args.dry_run:
        command.append("--dry-run")
    return command


def copy_run_archive(run_json: Path, archive_dir: Path) -> None:
    """Copy run.json plus logs/artifacts into one archive directory."""
    run_dir = run_json.parent
    archive_dir.mkdir(parents=True, exist_ok=True)
    for name in ("logs", "artifacts"):
        source = run_dir / name
        if source.exists():
            destination = archive_dir / name
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(source, destination)
    run_data = json.loads(run_json.read_text(encoding="utf-8"))
    rebase_archived_run_paths(run_data, run_dir)
    (archive_dir / "run.json").write_text(json.dumps(run_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def archive_relative_path(value: Any, original_run_dir: Path) -> str:
    """Return an archive-local relative path when value points inside a run dir."""
    if not value:
        return ""
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.relative_to(original_run_dir).as_posix()
    except ValueError:
        return str(path)


def rebase_archived_run_paths(run: dict[str, Any], original_run_dir: Path) -> None:
    """Make archived run.json paths resolve inside the archive directory."""
    run["paths"] = {
        "run_dir": ".",
        "run_json": "run.json",
        "artifacts_dir": "artifacts",
        "logs_dir": "logs",
    }
    for agent in run.get("agents", []):
        if not isinstance(agent, dict):
            continue
        for key in ("jsonl_path", "log_path", "output_path"):
            if key in agent:
                agent[key] = archive_relative_path(agent.get(key), original_run_dir)
        if agent.get("jsonl_path"):
            agent["transcript_path"] = agent["jsonl_path"]
        elif "transcript_path" in agent:
            agent["transcript_path"] = archive_relative_path(agent.get("transcript_path"), original_run_dir)
        if agent.get("output_path"):
            agent["activity_output_path"] = agent["output_path"]
        elif "activity_output_path" in agent:
            agent["activity_output_path"] = archive_relative_path(agent.get("activity_output_path"), original_run_dir)
    for artifact in run.get("artifacts", []):
        if isinstance(artifact, dict) and "path" in artifact:
            artifact["path"] = archive_relative_path(artifact.get("path"), original_run_dir)


def workflow_spec_for_target(args: argparse.Namespace, target: MatrixTarget, workdir: Path, output_dir: Path, default_spec: WorkflowSpec | None) -> WorkflowSpec:
    """Return the concrete workflow spec for one target."""
    if args.workflow_script:
        return run_workflow_script(args, target, workdir, output_dir)
    assert default_spec is not None
    return default_spec


def run_phase(
    target: MatrixTarget,
    args: argparse.Namespace,
    spec: WorkflowSpec,
    phase: dict[str, Any],
    workdir: Path,
    max_agents: int,
    archive_root: Path,
) -> dict[str, Any]:
    """Run one phase for one target and archive its workflow state."""
    started = time.time()
    command = build_command(target, args, spec, phase, workdir, max_agents)
    result = subprocess.run(command, text=True, capture_output=True, env=os.environ.copy())
    created = parse_created_run(result.stdout)
    run_id = str(created.get("run_id") or f"failed-{int(started)}")
    archive_dir = archive_root / target.label / phase["name"] / run_id
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / "stdout.log").write_text(result.stdout, encoding="utf-8")
    (archive_dir / "stderr.log").write_text(result.stderr, encoding="utf-8")
    parsed_path = str(created.get("path") or "")
    run_json = Path(parsed_path) if parsed_path else None
    run_status = "failed"
    jobs = 0
    if run_json is not None and run_json.is_file():
        copy_run_archive(run_json, archive_dir)
        run_data = json.loads(run_json.read_text(encoding="utf-8"))
        run_status = str(run_data.get("status") or run_status)
        jobs = int(run_data.get("metrics", {}).get("agents_total") or created.get("jobs") or 0)
    return {
        "name": phase["name"],
        "title": phase["title"],
        "status": "completed" if result.returncode == 0 and run_status == "completed" else "failed",
        "run_status": run_status,
        "returncode": result.returncode,
        "run_id": created.get("run_id", ""),
        "run_json": str(run_json) if run_json is not None and run_json.is_file() else "",
        "archive_dir": str(archive_dir),
        "jobs_file": str(phase["jobs_file"]),
        "jobs": jobs,
        "duration_seconds": round(time.time() - started, 3),
        "command": command,
    }


def run_target(target: MatrixTarget, args: argparse.Namespace, output_dir: Path, archive_root: Path, default_spec: WorkflowSpec | None, target_max: dict[str, int]) -> dict[str, Any]:
    """Run one target's workflow phases and archive stdout, stderr, state, and artifacts."""
    started = time.time()
    workdir = copy_project_for_target(args, target, output_dir)
    spec = workflow_spec_for_target(args, target, workdir, output_dir, default_spec)
    max_agents = target_max.get(target.label, args.max_agents)
    phases: list[dict[str, Any]] = []
    for phase in spec.phases:
        phase_result = run_phase(target, args, spec, phase, workdir, max_agents, archive_root)
        phases.append(phase_result)
        if phase_result["status"] != "completed":
            break
    output_artifact_dir = archive_root / target.label / "workdir-output"
    output_artifact_dir.parent.mkdir(parents=True, exist_ok=True)
    source_output = workdir / spec.output_subdir
    if source_output.exists():
        if output_artifact_dir.exists():
            shutil.rmtree(output_artifact_dir)
        shutil.copytree(source_output, output_artifact_dir)
    status = "completed" if len(phases) == len(spec.phases) and all(item["status"] == "completed" for item in phases) else "failed"
    jobs_total = sum(int(item.get("jobs") or 0) for item in phases)
    return {
        "label": target.label,
        "runner": target.runner,
        "ccc_runner": target.ccc_runner,
        "status": status,
        "run_status": phases[-1]["run_status"] if phases else "failed",
        "returncode": phases[-1]["returncode"] if phases else 1,
        "run_id": phases[-1]["run_id"] if phases else "",
        "run_json": phases[-1]["run_json"] if phases else "",
        "archive_dir": phases[0]["archive_dir"] if len(phases) == 1 else str(archive_root / target.label),
        "workdir": str(workdir),
        "workflow_file": str(spec.workflow_file),
        "workdir_output": str(output_artifact_dir) if output_artifact_dir.exists() else "",
        "jobs_file": str(spec.phases[0]["jobs_file"]) if spec.phases else "",
        "max_agents": max_agents,
        "jobs": jobs_total,
        "phases": phases,
        "duration_seconds": round(time.time() - started, 3),
        "command": phases[0]["command"] if phases else [],
        "commands": [phase["command"] for phase in phases],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", default="Runner Matrix Smoke")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--output-dir", default="~/tmp/workflow-runner-matrix")
    parser.add_argument("--project-src", help="copy this project directory into one isolated workdir per target")
    parser.add_argument("--jobs-file", help="JSON array of reusable smoke jobs; defaults to built-in runner smoke jobs")
    parser.add_argument("--workflow-script", help="executable script that prints workflow JSON with a jobs array")
    parser.add_argument("--workflow-script-arg", action="append", help="argument passed to --workflow-script; placeholders: {project_dir}, {target}, {output_dir}")
    parser.add_argument("--target", action="append", help="runner target, e.g. kimi-direct, ccc:kimi, or minimax=ccc:@mm")
    parser.add_argument("--target-max", action="append", help="per-target max agents override, e.g. kimi=4")
    parser.add_argument("--all-common", action="store_true", help="use the common direct and ccc runner target set")
    parser.add_argument("--tag", action="append", default=["runner-matrix"])
    parser.add_argument("--max-agents", type=workflow_run_codex.positive_int, default=3)
    parser.add_argument("--startup-delay", type=workflow_run_codex.nonnegative_float, default=1.0)
    parser.add_argument("--model")
    parser.add_argument("--sandbox", default="read-only", choices=["read-only", "workspace-write", "danger-full-access"])
    parser.add_argument("--approval", default="never", choices=["never", "on-request", "untrusted", "on-failure"])
    parser.add_argument("--permission-mode", choices=["safe", "auto", "yolo", "plan"])
    parser.add_argument("--timeout-secs", type=workflow_run_codex.positive_int, help="forwarded to ccc-backed runner phases")
    parser.add_argument("--quota-retries", type=workflow_run_codex.nonnegative_int, default=2, help="quota/rate-limit retries per worker; default: 2")
    parser.add_argument("--quota-retry-buffer-secs", type=workflow_run_codex.nonnegative_float, default=5.0, help="seconds added after the next :00/:30 retry window; default: 5.0")
    parser.add_argument("--failure-retries", type=workflow_run_codex.nonnegative_int, default=0, help="non-quota worker retries per worker; default: 0")
    parser.add_argument("--kimi-max-steps-per-turn", type=workflow_run_codex.positive_int, default=9999, help="Kimi max steps/tool calls per turn; default: 9999")
    parser.add_argument("--ccc-control", action="append")
    parser.add_argument("--ccc-output-mode", default="stream-json", choices=["formatted", "stream-formatted", "text", "stream-text", "json", "stream-json", "pass-text", "pass-json"])
    parser.add_argument("--mock", action="store_true", help="exercise workflow state without launching model CLIs")
    parser.add_argument("--dry-run", action="store_true", help="record workers but do not launch model CLIs")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    raw_targets = [*(args.target or [])]
    if args.all_common:
        raw_targets.extend(COMMON_TARGETS)
    if not raw_targets:
        raise SystemExit("provide at least one --target or use --all-common")
    args.cwd = str(Path(args.cwd).expanduser().resolve())
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_root = output_dir / "archive"
    archive_root.mkdir(parents=True, exist_ok=True)
    targets = unique_targets(raw_targets)
    target_max = parse_target_max(args.target_max)
    unknown_max = sorted(set(target_max) - {target.label for target in targets})
    if unknown_max:
        raise SystemExit(f"--target-max label(s) did not match targets: {', '.join(unknown_max)}")
    default_spec = None if args.workflow_script else write_default_workflow(args, output_dir)
    started = time.time()
    target_results = [run_target(target, args, output_dir, archive_root, default_spec, target_max) for target in targets]
    status = "completed" if all(item["status"] == "completed" for item in target_results) else "failed"
    workflow_files = [item["workflow_file"] for item in target_results]
    if default_spec is not None:
        workflow_files = [str(default_spec.workflow_file)]
    summary = {
        "schema_version": 1,
        "title": args.title,
        "status": status,
        "cwd": args.cwd,
        "project_src": str(Path(args.project_src).expanduser().resolve()) if args.project_src else "",
        "workflow_file": workflow_files[0] if workflow_files else "",
        "workflow_files": workflow_files,
        "jobs_file": str(default_spec.phases[0]["jobs_file"]) if default_spec is not None and default_spec.phases else "",
        "output_dir": str(output_dir),
        "archive_root": str(archive_root),
        "targets": target_results,
        "duration_seconds": round(time.time() - started, 3),
    }
    summary_path = output_dir / "runner-matrix-summary.json"
    summary["summary_path"] = str(summary_path)
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    if status != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()

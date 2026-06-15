#!/usr/bin/env python3
"""Launch a saved workflow-plan JSON file or generator script."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

import workflow_plan
import workflow_run_codex
import workflow_state

DEFAULT_RUNNER = "codex-direct"
DEFAULT_CCC_OUTPUT_MODE = "stream-json"
DEFAULT_SANDBOX = "read-only"
DEFAULT_APPROVAL = "never"


def source_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def record_plan_artifact(run_id: str, plan: dict[str, Any]) -> Path:
    """Save the normalized plan inside the run and record it as an artifact."""
    run = workflow_state.load_run(run_id)
    artifacts_dir = Path(run["paths"]["artifacts_dir"])
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    plan_path = artifacts_dir / "workflow-plan.json"
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    artifact_args = argparse.Namespace(
        run=run_id,
        kind="workflow-plan",
        title="Normalized workflow plan",
        path=str(plan_path),
        agent="",
        phase="",
    )
    with contextlib.redirect_stdout(io.StringIO()):
        workflow_state.cmd_artifact(artifact_args)
    return plan_path


def resolve_cwd(value: Any, *, source_dir: Path) -> str:
    """Resolve a plan or CLI cwd, using the workflow source directory for relatives."""
    if value in (None, ""):
        return str(Path.cwd().resolve())
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = source_dir / path
    return str(path.resolve())


def merge_plan_options(args: argparse.Namespace, plan: dict[str, Any], source_dir: Path) -> None:
    """Apply plan execution metadata, with explicit CLI args taking precedence."""
    _cli_explicit: dict[str, Any] = {}
    for field in ("runner", "model", "sandbox", "approval", "ccc_runner", "permission_mode", "cli_agent"):
        value = getattr(args, field, None)
        if value is not None:
            _cli_explicit[field] = value
    for field in ("timeout_secs", "kimi_max_steps_per_turn"):
        value = getattr(args, field, None)
        if value is not None:
            _cli_explicit[field] = value
    args._cli_explicit = _cli_explicit
    args.cwd = resolve_cwd(args.cwd if args.cwd is not None else plan.get("cwd"), source_dir=source_dir)
    args.title = args.title or plan["title"]
    args.prompt = workflow_state.read_text_arg(args.prompt, args.prompt_file) or plan["goal"] or plan["summary"] or f"Run workflow plan from {args.plan_source}."
    args.runner = args.runner or str(plan.get("runner") or DEFAULT_RUNNER)
    args.ccc_runner = args.ccc_runner or plan.get("ccc_runner")
    args.ccc_control = args.ccc_control if args.ccc_control is not None else plan.get("ccc_control")
    args.ccc_output_mode = args.ccc_output_mode or str(plan.get("ccc_output_mode") or DEFAULT_CCC_OUTPUT_MODE)
    args.permission_mode = args.permission_mode or plan.get("permission_mode")
    args.cli_agent = args.cli_agent or plan.get("cli_agent")
    args.timeout_secs = args.timeout_secs if args.timeout_secs is not None else plan.get("timeout_secs")
    args.quota_retries = args.quota_retries if args.quota_retries is not None else int(plan.get("quota_retries", 2))
    args.quota_retry_buffer_secs = args.quota_retry_buffer_secs if args.quota_retry_buffer_secs is not None else float(plan.get("quota_retry_buffer_secs", 5.0))
    args.failure_retries = args.failure_retries if args.failure_retries is not None else int(plan.get("failure_retries", 0))
    args.result_schema = args.result_schema or plan.get("result_schema")
    args.kimi_max_steps_per_turn = args.kimi_max_steps_per_turn if args.kimi_max_steps_per_turn is not None else int(plan.get("kimi_max_steps_per_turn", 9999))
    args.model = args.model or plan.get("model")
    args.sandbox = args.sandbox or str(plan.get("sandbox") or DEFAULT_SANDBOX)
    args.approval = args.approval or str(plan.get("approval") or DEFAULT_APPROVAL)
    args.max_agents = args.max_agents if args.max_agents is not None else int(plan.get("max_agents", 4))
    args.max_round = args.max_round if args.max_round is not None else int(plan.get("max_round", 3))
    args.max_job = args.max_job if args.max_job is not None else plan.get("max_job")
    args.startup_delay = args.startup_delay if args.startup_delay is not None else float(plan.get("startup_delay", 1.0))
    args.dry_run = args.dry_run if args.dry_run is not None else bool(plan.get("dry_run", False))
    args.mock = args.mock if args.mock is not None else bool(plan.get("mock", False))
    plan_tags = list(plan.get("tags") or [])
    cli_tags = list(args.tag or [])
    args.tag = [*plan_tags, *cli_tags] or None


def phase_jobs(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten workflow phases while preserving phase stage and order dependencies."""
    jobs: list[dict[str, Any]] = []
    previous_phase_names: list[str] = []
    for phase in plan["phases"]:
        phase_id = phase_id_for_plan_phase(phase)
        current_phase_names: list[str] = []
        phase_execution = {field: phase[field] for field in workflow_plan.EXECUTION_FIELDS if field in phase}
        for job in phase["jobs"]:
            flattened = dict(job)
            flattened["stage"] = flattened.get("stage") or phase["name"]
            flattened["phase_id"] = phase_id
            if phase_execution:
                flattened["_phase_execution"] = phase_execution
            dependencies = []
            raw_depends = flattened.get("depends_on")
            if isinstance(raw_depends, list):
                dependencies.extend(str(item) for item in raw_depends if str(item).strip())
            elif raw_depends:
                dependencies.extend(piece.strip() for piece in str(raw_depends).split(",") if piece.strip())
            dependencies.extend(previous_phase_names)
            if dependencies:
                flattened["depends_on"] = ", ".join(dict.fromkeys(dependencies))
            jobs.append(flattened)
            current_phase_names.append(str(flattened["name"]))
        previous_phase_names = current_phase_names
    return jobs


def resolve_job_execution_fields(
    plan: dict[str, Any],
    job: dict[str, Any],
) -> dict[str, Any]:
    """Return effective execution fields for one job: root plan < phase < job."""
    result: dict[str, Any] = {}
    phase_exec = job.get("_phase_execution") or {}
    for field in workflow_plan.EXECUTION_FIELDS:
        if field in job and job[field] not in (None, ""):
            result[field] = job[field]
        elif field in phase_exec and phase_exec[field] not in (None, ""):
            result[field] = phase_exec[field]
        elif field in plan and plan[field] not in (None, ""):
            result[field] = plan[field]
    return result


def prepare_worktree_lanes(run_id: str, jobs: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    """Resolve and optionally create per-job worktree lanes before workers launch."""
    run = workflow_state.load_run(run_id)
    worktree_root = Path(run["paths"]["run_dir"]) / "worktrees"
    prepared: list[dict[str, Any]] = []
    for job in jobs:
        lane = resolve_worktree_lane(job, args, run_id, worktree_root)
        if lane is None:
            prepared.append(job)
            continue
        updated = dict(job)
        updated["worktree"] = lane
        updated["cwd"] = lane["path"]
        if not args.dry_run and lane.get("create", True):
            create_worktree_lane(args.cwd, lane)
            record_worktree_lane_event(run_id, updated, lane, operation="created")
        else:
            record_worktree_lane_event(run_id, updated, lane, operation="planned")
        prepared.append(updated)
    return prepared


def resolve_worktree_lane(job: dict[str, Any], args: argparse.Namespace, run_id: str, worktree_root: Path) -> dict[str, Any] | None:
    """Return normalized worktree metadata for one job, or None when disabled."""
    raw = job.get("worktree")
    if raw in (None, False, "", 0):
        return None
    if raw is True:
        spec: dict[str, Any] = {}
    elif isinstance(raw, dict):
        spec = dict(raw)
        if spec.get("enabled") is False:
            return None
    else:
        raise SystemExit("job worktree must be true, false, or an object")

    slug = workflow_state.slugify(str(job.get("name") or "job"), fallback="job")
    path_value = spec.get("path")
    if path_value:
        path = Path(str(path_value)).expanduser()
        if not path.is_absolute():
            path = Path(args.cwd) / path
    else:
        path = worktree_root / slug
    branch = str(spec.get("branch") or f"workflow/{run_id}/{slug}").strip()
    base = str(spec.get("base") or "HEAD").strip()
    merge_target = str(spec.get("merge_target") or current_branch(args.cwd) or "HEAD").strip()
    lane = {
        "enabled": True,
        "create": bool(spec.get("create", True)),
        "path": str(path.resolve()),
        "branch": branch,
        "base": base,
        "merge_target": merge_target,
        "source_cwd": args.cwd,
    }
    for key in ("owner", "label", "notes"):
        if spec.get(key):
            lane[key] = spec[key]
    return lane


def current_branch(cwd: str) -> str:
    """Return the current branch name for merge-target metadata when available."""
    result = subprocess.run(["git", "-C", cwd, "branch", "--show-current"], text=True, capture_output=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def create_worktree_lane(cwd: str, lane: dict[str, Any]) -> None:
    """Create one git worktree lane."""
    path = Path(str(lane["path"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    command = ["git", "-C", cwd, "worktree", "add", "-b", str(lane["branch"]), str(path), str(lane["base"])]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"failed to create worktree lane {lane['branch']}: {result.stderr.strip() or result.stdout.strip()}")


def record_worktree_lane_event(run_id: str, job: dict[str, Any], lane: dict[str, Any], *, operation: str) -> None:
    """Record lane creation/planning in run state."""
    def mutator(run: dict[str, Any]) -> None:
        workflow_state.add_event(
            run,
            "info",
            f"worktree lane {operation}: {job['name']}",
            kind="worktree",
            operation=operation,
            source="workflow_apply.worktree",
            phase_id=job.get("phase_id"),
            data={
                "job": job.get("name", ""),
                "path": lane.get("path", ""),
                "branch": lane.get("branch", ""),
                "base": lane.get("base", ""),
                "merge_target": lane.get("merge_target", ""),
            },
        )

    workflow_state.mutate_run(run_id, mutator)


def phase_id_for_plan_phase(phase: dict[str, Any]) -> str:
    """Return the run phase id for one normalized plan phase."""
    return f"phase-{workflow_state.slugify(str(phase['name']), fallback='plan')}"


def install_plan_phases(run_id: str, plan: dict[str, Any]) -> None:
    """Replace the default worker phase with declared workflow-plan phases."""
    timestamp = workflow_state.now()

    def mutator(run: dict[str, Any]) -> None:
        run["phases"] = [phase for phase in run.get("phases", []) if phase.get("phase_id") != workflow_run_codex.PHASE_ID]
        existing = {phase.get("phase_id") for phase in run.setdefault("phases", [])}
        for plan_phase in plan["phases"]:
            phase_id = phase_id_for_plan_phase(plan_phase)
            if phase_id in existing:
                raise SystemExit(f"duplicate declared phase id: {phase_id}")
            phase = {
                "phase_id": phase_id,
                "name": plan_phase.get("title") or plan_phase["name"],
                "goal": plan_phase.get("goal") or plan_phase.get("summary") or "",
                "status": "running",
                "created_at": timestamp,
                "started_at": timestamp,
                "completed_at": None,
                "agent_ids": [],
                "plan_phase": plan_phase["name"],
            }
            if plan_phase.get("summary"):
                phase["summary"] = plan_phase["summary"]
            if plan_phase.get("gates"):
                phase["gates"] = plan_phase["gates"]
            if plan_phase.get("planned_checks"):
                phase["planned_checks"] = plan_phase["planned_checks"]
            run["phases"].append(phase)
            existing.add(phase_id)
            workflow_state.add_event(
                run,
                "info",
                f"phase declared: {phase['name']}",
                kind="phase",
                operation="declared",
                source="workflow_apply.apply",
                phase_id=phase_id,
                data={"name": phase["name"], "plan_phase": plan_phase["name"]},
            )
        if plan.get("gates"):
            run.setdefault("metadata", {})["workflow_gates"] = plan["gates"]

    workflow_state.mutate_run(run_id, mutator)


def record_plan_decisions(run_id: str, plan: dict[str, Any]) -> None:
    """Record top-level and phase-local plan decisions as durable decisions."""
    decision_specs: list[tuple[dict[str, Any], str]] = [(decision, "") for decision in plan.get("decisions", [])]
    for phase in plan["phases"]:
        decision_specs.extend((decision, phase_id_for_plan_phase(phase)) for decision in phase.get("decisions", []))
    if not decision_specs:
        return

    def mutator(run: dict[str, Any]) -> None:
        decisions = run.setdefault("decisions", [])
        for index, (spec, phase_id) in enumerate(decision_specs, start=1):
            title = str(spec.get("title") or spec.get("name") or f"Plan decision {index}").strip()
            decision = {
                "decision_id": f"dec-plan-{index:02d}-{workflow_state.slugify(title, fallback='decision')}",
                "ts": workflow_state.now(),
                "title": title,
                "rationale": str(spec.get("rationale") or spec.get("summary") or spec.get("reason") or "").strip(),
                "made_by": "workflow_apply.py",
            }
            if phase_id:
                decision["phase_id"] = phase_id
            decisions.append(decision)
            workflow_state.add_event(
                run,
                "info",
                f"decision recorded: {title}",
                kind="decision",
                operation="recorded",
                source="workflow_apply.apply",
                phase_id=phase_id,
                data={"title": title, "made_by": decision["made_by"]},
            )

    workflow_state.mutate_run(run_id, mutator)


def apply_workflow(args: argparse.Namespace) -> int:
    """Create a normal workflow run from one normalized plan."""
    source = source_path(args.plan_source)
    initial_cwd = resolve_cwd(args.cwd, source_dir=source.parent) if args.cwd is not None else str(Path.cwd().resolve())
    plan = workflow_plan.load_workflow_source(args.plan_source, fallback_title=args.title or "Workflow Plan", cwd=Path(initial_cwd))
    merge_plan_options(args, plan, source.parent)
    args.result_schema_obj = workflow_run_codex._resolve_schema(args.result_schema) if args.result_schema else None
    jobs = phase_jobs(plan)
    default_provider = workflow_run_codex.build_provider(args)
    run = workflow_run_codex.create_run(args, jobs, default_provider)
    install_plan_phases(run["run_id"], plan)
    record_plan_decisions(run["run_id"], plan)
    record_plan_artifact(run["run_id"], plan)
    jobs = prepare_worktree_lanes(run["run_id"], jobs, args)
    for index, job in enumerate(jobs):
        job_fields = resolve_job_execution_fields(plan, job)
        cli_explicit = getattr(args, "_cli_explicit", {})
        job_args = _build_job_args(args, job_fields)
        job_provider = _build_job_provider(args, job_args, default_provider)
        effective_model = cli_explicit.get("model", job_fields.get("model"))
        run = workflow_run_codex.add_agent(
            run,
            job,
            args,
            job_provider,
            index,
            stage=job.get("stage", ""),
            depends_on=job.get("depends_on", ""),
            phase_id=job.get("phase_id"),
            model_override=effective_model,
            job_args=job_args,
        )
    print(json.dumps({"run_id": run["run_id"], "path": run["paths"]["run_json"], "jobs": len(jobs)}, indent=2))
    if args.dry_run:
        print("dry run: workers were recorded but not launched")
    else:
        replay = ["python3", __file__, "--runner", args.runner, args.plan_source]
        if args.runner == "ccc" and args.ccc_runner:
            replay.extend(["--ccc-runner", args.ccc_runner])
        print("command:", shlex.join(replay))
    status = asyncio.run(workflow_run_codex.run_all(workflow_state.load_run(run["run_id"]), args, default_provider))
    return 0 if status == "completed" else 1


JOB_EXECUTION_OVERRIDE_FIELDS = (
    "runner",
    "model",
    "sandbox",
    "approval",
    "ccc_runner",
    "ccc_control",
    "ccc_output_mode",
    "permission_mode",
    "cli_agent",
    "timeout_secs",
    "kimi_max_steps_per_turn",
)


def _build_job_args(
    args: argparse.Namespace,
    job_fields: dict[str, Any],
) -> argparse.Namespace:
    """Return a per-job args namespace with CLI > root > phase > job precedence."""
    cli_explicit = getattr(args, "_cli_explicit", {})
    job_args = argparse.Namespace(**vars(args))
    for field in JOB_EXECUTION_OVERRIDE_FIELDS:
        if field in cli_explicit and cli_explicit[field] is not None:
            setattr(job_args, field, cli_explicit[field])
        elif field in job_fields:
            value = job_fields[field]
            if field in ("timeout_secs", "kimi_max_steps_per_turn"):
                if value is not None:
                    setattr(job_args, field, value)
            elif value:
                setattr(job_args, field, value)
    return job_args


def _job_execution_differs(args: argparse.Namespace, job_args: argparse.Namespace) -> bool:
    """Return True when job_args overrides any per-job execution field relative to args."""
    for field in JOB_EXECUTION_OVERRIDE_FIELDS:
        jv = getattr(job_args, field, None)
        av = getattr(args, field, None)
        if field in ("timeout_secs", "kimi_max_steps_per_turn"):
            if jv is not None and jv != av:
                return True
        elif (jv or av) and jv != av:
            return True
    return False


def _build_job_provider(
    args: argparse.Namespace,
    job_args: argparse.Namespace,
    default_provider: workflow_run_codex.RunnerProvider,
) -> workflow_run_codex.RunnerProvider:
    """Return a per-job provider when the job's execution fields diverge from the run default."""
    if not _job_execution_differs(args, job_args):
        return default_provider
    return workflow_run_codex.build_provider(job_args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan_source", help="JSON workflow plan, or executable/Python script that prints one")
    parser.add_argument("--title")
    parser.add_argument("--prompt")
    parser.add_argument("--prompt-file")
    parser.add_argument("--cwd")
    parser.add_argument("--tag", action="append")
    parser.add_argument(
        "--runner",
        default=None,
        choices=["codex-direct", "ccc-codex", "ccc-opencode", "ccc", "opencode-direct", "kimi-direct"],
        help="coding CLI provider to use for worker processes",
    )
    parser.add_argument("--ccc-runner")
    parser.add_argument("--ccc-control", action="append")
    parser.add_argument("--ccc-output-mode", choices=["formatted", "stream-formatted", "text", "stream-text", "json", "stream-json", "pass-text", "pass-json"])
    parser.add_argument("--permission-mode", choices=["safe", "auto", "yolo", "plan"])
    parser.add_argument("--cli-agent")
    parser.add_argument("--timeout-secs", type=workflow_run_codex.positive_int)
    parser.add_argument("--quota-retries", type=workflow_run_codex.nonnegative_int)
    parser.add_argument("--quota-retry-buffer-secs", type=workflow_run_codex.nonnegative_float)
    parser.add_argument("--failure-retries", type=workflow_run_codex.nonnegative_int)
    parser.add_argument("--result-schema")
    parser.add_argument("--kimi-max-steps-per-turn", type=workflow_run_codex.positive_int)
    parser.add_argument("--model")
    parser.add_argument("--sandbox", choices=["read-only", "workspace-write", "danger-full-access"])
    parser.add_argument("--approval", choices=["never", "on-request", "untrusted", "on-failure"])
    parser.add_argument("--max-agents", "--concurrency", dest="max_agents", type=workflow_run_codex.positive_int)
    parser.add_argument("--max-round", type=workflow_run_codex.positive_int)
    parser.add_argument("--max-job", type=workflow_run_codex.positive_int)
    parser.add_argument("--startup-delay", type=workflow_run_codex.nonnegative_float)
    parser.add_argument("--dry-run", action="store_true", default=None)
    parser.add_argument("--mock", action="store_true", default=None)
    return parser


def main() -> None:
    raise SystemExit(apply_workflow(build_parser().parse_args()))


if __name__ == "__main__":
    main()

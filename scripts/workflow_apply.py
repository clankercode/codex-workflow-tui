#!/usr/bin/env python3
"""Launch a saved workflow-plan JSON file or generator script."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import shlex
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
        current_phase_names: list[str] = []
        for job in phase["jobs"]:
            flattened = dict(job)
            flattened["stage"] = flattened.get("stage") or phase["name"]
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


def apply_workflow(args: argparse.Namespace) -> int:
    """Create a normal workflow run from one normalized plan."""
    source = source_path(args.plan_source)
    initial_cwd = resolve_cwd(args.cwd, source_dir=source.parent) if args.cwd is not None else str(Path.cwd().resolve())
    plan = workflow_plan.load_workflow_source(args.plan_source, fallback_title=args.title or "Workflow Plan", cwd=Path(initial_cwd))
    merge_plan_options(args, plan, source.parent)
    args.result_schema_obj = workflow_run_codex._resolve_schema(args.result_schema) if args.result_schema else None
    jobs = phase_jobs(plan)
    provider = workflow_run_codex.build_provider(args)
    run = workflow_run_codex.create_run(args, jobs, provider)
    record_plan_artifact(run["run_id"], plan)
    for index, job in enumerate(jobs):
        run = workflow_run_codex.add_agent(run, job, args, provider, index, stage=job.get("stage", ""), depends_on=job.get("depends_on", ""))
    print(json.dumps({"run_id": run["run_id"], "path": run["paths"]["run_json"], "jobs": len(jobs)}, indent=2))
    if args.dry_run:
        print("dry run: workers were recorded but not launched")
    else:
        replay = ["python3", __file__, "--runner", args.runner, args.plan_source]
        if args.runner == "ccc" and args.ccc_runner:
            replay.extend(["--ccc-runner", args.ccc_runner])
        print("command:", shlex.join(replay))
    status = asyncio.run(workflow_run_codex.run_all(workflow_state.load_run(run["run_id"]), args, provider))
    return 0 if status == "completed" else 1


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

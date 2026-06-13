#!/usr/bin/env python3
"""Start a workflow from one natural-language goal."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

import workflow_run_codex
import workflow_state


PLANNER_PROMPT = """\
You are planning a stateful multi-agent workflow for a coding CLI orchestrator.

Goal:
{goal}

Current working directory:
{cwd}

Return ONLY a JSON object with this shape:
{{
  "title": "short workflow title",
  "summary": "one sentence workflow objective",
  "jobs": [
    {{"name": "research", "role": "researcher", "prompt": "bounded worker prompt"}},
    {{"name": "implementation", "role": "implementer", "prompt": "bounded worker prompt"}}
  ]
}}

Constraints:
- Create between 1 and {max_jobs} jobs.
- Use jobs only when they can run mostly independently.
- Each prompt must be self-contained and include the original goal context.
- Include synthesis/review work when useful.
- Do not include markdown fences or commentary outside the JSON.
"""


def extract_json_object(text: str) -> dict[str, Any]:
    """Extract the first JSON object from plain or fenced planner output."""
    stripped = text.strip()
    if not stripped:
        raise SystemExit("planner returned no output")
    with contextlib.suppress(json.JSONDecodeError):
        loaded = json.loads(stripped)
        if isinstance(loaded, dict):
            return loaded
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", stripped, re.DOTALL | re.IGNORECASE)
    if fence:
        with contextlib.suppress(json.JSONDecodeError):
            loaded = json.loads(fence.group(1))
            if isinstance(loaded, dict):
                return loaded
    start = stripped.find("{")
    if start < 0:
        raise SystemExit("planner output did not contain a JSON object")
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(stripped[start:], start=start):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                loaded = json.loads(stripped[start : index + 1])
                if isinstance(loaded, dict):
                    return loaded
                break
    raise SystemExit("planner output did not contain a valid JSON object")


def title_from_goal(goal: str) -> str:
    """Create a compact title when the planner omits one."""
    words = re.findall(r"[A-Za-z0-9]+", goal)[:8]
    return " ".join(words).title() or "Workflow"


def unique_job_name(raw: str, used: set[str], index: int) -> str:
    """Return a stable unique display name for one generated job."""
    base = workflow_state.slugify(raw, fallback=f"job-{index + 1}")
    name = base
    suffix = 2
    while name in used:
        name = f"{base}-{suffix}"
        suffix += 1
    used.add(name)
    return name


def parse_planner_output(text: str, *, goal: str, max_jobs: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """Parse and normalize planner JSON into workflow-run jobs and truncation metadata."""
    raw = extract_json_object(text)
    jobs_raw = raw.get("jobs")
    if not isinstance(jobs_raw, list) or not jobs_raw:
        raise SystemExit("planner JSON must contain a non-empty jobs array")
    original_count = len(jobs_raw)
    truncated = original_count > max_jobs
    jobs: list[dict[str, str]] = []
    used: set[str] = set()
    for index, item in enumerate(jobs_raw[:max_jobs]):
        if not isinstance(item, dict):
            raise SystemExit("each planner job must be an object")
        prompt = str(item.get("prompt") or "").strip()
        if not prompt:
            raise SystemExit("each planner job must include a non-empty prompt")
        raw_name = str(item.get("name") or item.get("role") or f"job-{index + 1}")
        name = unique_job_name(raw_name, used, index)
        jobs.append(
            {
                "name": name,
                "role": str(item.get("role") or raw_name).strip() or name,
                "prompt": prompt,
            }
        )
    plan = {
        "schema_version": 1,
        "title": str(raw.get("title") or title_from_goal(goal)).strip()[:120],
        "summary": str(raw.get("summary") or goal).strip(),
        "goal": goal,
        "jobs": jobs,
    }
    return plan, {"truncated": truncated, "original_count": original_count, "max_jobs": max_jobs}


def mock_plan(goal: str, *, title: str | None, max_jobs: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a deterministic no-model plan for tests and dry operator rehearsals."""
    templates = [
        ("research", "researcher", "Research the goal, existing constraints, and prior art."),
        ("design", "designer", "Design a coherent approach and identify independent work lanes."),
        ("draft", "writer", "Produce the main deliverable from the research and design context."),
        ("review", "reviewer", "Critique the deliverable for gaps, risks, and unsupported claims."),
    ]
    original_count = len(templates)
    truncated = original_count > max_jobs
    jobs = [
        {
            "name": name,
            "role": role,
            "prompt": f"{instruction}\n\nOriginal goal:\n{goal}",
        }
        for name, role, instruction in templates[:max_jobs]
    ]
    return {
        "schema_version": 1,
        "title": title or title_from_goal(goal),
        "summary": goal,
        "goal": goal,
        "jobs": jobs,
    }, {"truncated": truncated, "original_count": original_count, "max_jobs": max_jobs}


def planner_namespace(args: argparse.Namespace, prompt: str) -> argparse.Namespace:
    """Build the provider args used for the planning agent."""
    runner = args.planner_runner or args.runner
    ccc_runner = args.planner_ccc_runner if args.planner_ccc_runner is not None else args.ccc_runner
    return argparse.Namespace(
        runner=runner,
        ccc_runner=ccc_runner,
        ccc_control=args.planner_ccc_control or [],
        ccc_output_mode=args.planner_ccc_output_mode,
        permission_mode=args.permission_mode,
        timeout_secs=args.planner_timeout_secs or args.timeout_secs,
        cwd=args.cwd,
        sandbox=args.sandbox,
        approval=args.approval,
        model=args.planner_model or args.model,
        cli_agent=args.planner_cli_agent or args.cli_agent,
        quota_retries=args.quota_retries,
        quota_retry_buffer_secs=args.quota_retry_buffer_secs,
        result_schema=args.result_schema,
        kimi_max_steps_per_turn=args.kimi_max_steps_per_turn,
        prompt=prompt,
    )


def run_planner(args: argparse.Namespace, goal: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run or mock the planner and return normalized plan plus metadata."""
    if args.mock or args.mock_plan:
        plan, truncation = mock_plan(goal, title=args.title, max_jobs=args.max_jobs)
        return plan, {"planner": "mock", **truncation}

    prompt = PLANNER_PROMPT.format(goal=goal, cwd=args.cwd, max_jobs=args.max_jobs)
    planner_args = planner_namespace(args, prompt)
    provider = workflow_run_codex.build_provider(planner_args)
    with tempfile.TemporaryDirectory(prefix="workflow-start-planner-") as tmp:
        tmp_path = Path(tmp)
        agent = {
            "name": "Workflow planner",
            "prompt": prompt,
            "jsonl_path": str(tmp_path / "planner.stdout"),
            "log_path": str(tmp_path / "planner.stderr"),
            "output_path": str(tmp_path / "planner.output.md"),
        }
        command = provider.build_command(agent, planner_args)
        planner_timeout = getattr(args, "planner_timeout_secs", None)
        worker_timeout = getattr(args, "timeout_secs", None)
        timeout = planner_timeout if planner_timeout is not None else worker_timeout
        result = subprocess.run(command, input=provider.stdin_payload(agent, planner_args), text=False, capture_output=True, timeout=timeout)
        Path(agent["jsonl_path"]).write_bytes(result.stdout)
        Path(agent["log_path"]).write_bytes(result.stderr)
        extracted = provider.extract_result(agent, result.returncode)
        raw_output = extracted.result or result.stdout.decode("utf-8", errors="replace")
        if result.returncode != 0:
            raise SystemExit(f"planner failed with exit code {result.returncode}: {extracted.summary}")
        plan, truncation = parse_planner_output(raw_output, goal=goal, max_jobs=args.max_jobs)
        return plan, {"planner": provider.name, **truncation}


def worker_namespace(args: argparse.Namespace, plan: dict[str, Any], goal: str) -> argparse.Namespace:
    """Build the args expected by workflow_run_codex helpers."""
    return argparse.Namespace(
        title=args.title or plan["title"],
        prompt=goal,
        prompt_file=None,
        cwd=args.cwd,
        job=None,
        jobs_file=None,
        tag=[*(args.tag or []), "start"],
        runner=args.runner,
        ccc_runner=args.ccc_runner,
        ccc_control=args.ccc_control,
        ccc_output_mode=args.ccc_output_mode,
        permission_mode=args.permission_mode,
        cli_agent=args.cli_agent,
        timeout_secs=args.timeout_secs,
        quota_retries=args.quota_retries,
        quota_retry_buffer_secs=args.quota_retry_buffer_secs,
        kimi_max_steps_per_turn=args.kimi_max_steps_per_turn,
        model=args.model,
        sandbox=args.sandbox,
        approval=args.approval,
        max_agents=args.max_agents,
        max_round=args.max_round,
        max_job=args.max_job,
        startup_delay=args.startup_delay,
        dry_run=args.dry_run,
        mock=args.mock,
    )


def record_start_plan(run_id: str, plan: dict[str, Any], planner_meta: dict[str, Any]) -> str:
    """Persist the generated plan as a first-class artifact and decision."""
    run = workflow_state.load_run(run_id)
    plan_path = Path(run["paths"]["artifacts_dir"]) / "workflow-start-plan.json"
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def mutator(data: dict[str, Any]) -> None:
        decision = {
            "decision_id": "dec-workflow-start-plan",
            "ts": workflow_state.now(),
            "title": "Workflow start plan generated",
            "rationale": f"Planner {planner_meta['planner']} decomposed the goal into {len(plan['jobs'])} job(s).",
            "made_by": "workflow_start.py",
        }
        data.setdefault("decisions", []).append(decision)
        if planner_meta.get("truncated"):
            truncation_decision = {
                "decision_id": "dec-workflow-start-truncation",
                "ts": workflow_state.now(),
                "title": "Planner job list truncated",
                "rationale": (
                    f"Planner returned {planner_meta['original_count']} jobs; "
                    f"truncated to {planner_meta['max_jobs']} by --max-jobs."
                ),
                "made_by": "workflow_start.py",
            }
            data.setdefault("decisions", []).append(truncation_decision)
            workflow_state.add_event(
                data,
                "warning",
                f"planner truncated job list from {planner_meta['original_count']} to {planner_meta['max_jobs']}",
                kind="planning",
                operation="truncated",
                source="workflow_start.record_start_plan",
                data={"original_count": planner_meta["original_count"], "max_jobs": planner_meta["max_jobs"]},
            )
        artifact = {
            "artifact_id": "art-workflow-start-plan",
            "ts": workflow_state.now(),
            "kind": "generated-plan",
            "title": "Workflow start generated plan",
            "path": str(plan_path),
            "phase_id": workflow_run_codex.PHASE_ID,
            "agent_id": "",
        }
        data.setdefault("artifacts", []).append(artifact)
        workflow_state.add_event(
            data,
            "info",
            "workflow start plan generated",
            kind="decision",
            operation="recorded",
            source="workflow_start.record_start_plan",
            data={"planner": planner_meta["planner"], "jobs": len(plan["jobs"]), "plan_path": str(plan_path)},
        )

    workflow_state.mutate_run(run_id, mutator)
    return str(plan_path)


def start_workflow(args: argparse.Namespace) -> int:
    """Plan and launch a workflow from a natural-language goal."""
    goal = " ".join(args.goal).strip()
    if not goal:
        raise SystemExit("goal must not be empty")
    args.cwd = str(Path(args.cwd).expanduser().resolve())
    plan, planner_meta = run_planner(args, goal)
    run_args = worker_namespace(args, plan, goal)
    provider = workflow_run_codex.build_provider(run_args)
    run = workflow_run_codex.create_run(run_args, plan["jobs"], provider)
    plan_path = record_start_plan(run["run_id"], plan, planner_meta)
    print(json.dumps(
            {
                "run_id": run["run_id"],
                "path": run["paths"]["run_json"],
                "jobs": len(plan["jobs"]),
                "planner": planner_meta["planner"],
                "plan_path": plan_path,
            },
            indent=2,
        )
    )
    for index, job in enumerate(plan["jobs"]):
        run = workflow_run_codex.add_agent(run, job, run_args, provider, index)
    replay = ["workflow", "start", goal, "--runner", args.runner]
    if args.runner == "ccc" and args.ccc_runner:
        replay.extend(["--ccc-runner", args.ccc_runner])
    replay.extend(["--title", run_args.title, "..."])
    print("command:", shlex.join(replay))
    status = asyncio.run(workflow_run_codex.run_all(workflow_state.load_run(run["run_id"]), run_args, provider))
    return 0 if status == "completed" else 1


def positive_int(value: str) -> int:
    return workflow_run_codex.positive_int(value)


def nonnegative_float(value: str) -> float:
    return workflow_run_codex.nonnegative_float(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("goal", nargs="+", help="natural-language goal to decompose into workflow jobs")
    parser.add_argument("--title")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--tag", action="append")
    parser.add_argument("--max-jobs", type=positive_int, default=4, help="maximum planner jobs; default: 4")
    parser.add_argument("--mock-plan", action="store_true", help="use deterministic local planning without model calls")
    parser.add_argument("--planner-runner", choices=["codex-direct", "ccc-codex", "ccc-opencode", "ccc", "opencode-direct", "kimi-direct"])
    parser.add_argument("--planner-ccc-runner")
    parser.add_argument("--planner-ccc-control", action="append")
    parser.add_argument("--planner-ccc-output-mode", default="stream-json", choices=["formatted", "stream-formatted", "text", "stream-text", "json", "stream-json", "pass-text", "pass-json"])
    parser.add_argument("--planner-cli-agent")
    parser.add_argument("--planner-model")
    parser.add_argument("--planner-timeout-secs", type=positive_int)
    parser.add_argument("--runner", default="codex-direct", choices=["codex-direct", "ccc-codex", "ccc-opencode", "ccc", "opencode-direct", "kimi-direct"])
    parser.add_argument("--ccc-runner")
    parser.add_argument("--ccc-control", action="append")
    parser.add_argument("--ccc-output-mode", default="stream-json", choices=["formatted", "stream-formatted", "text", "stream-text", "json", "stream-json", "pass-text", "pass-json"])
    parser.add_argument("--permission-mode", choices=["safe", "auto", "yolo", "plan"])
    parser.add_argument("--cli-agent")
    parser.add_argument("--timeout-secs", type=positive_int)
    parser.add_argument("--result-schema", help="path to a JSON schema file applied to worker output")
    parser.add_argument("--quota-retries", type=workflow_run_codex.nonnegative_int, default=2)
    parser.add_argument("--quota-retry-buffer-secs", type=nonnegative_float, default=5.0)
    parser.add_argument("--kimi-max-steps-per-turn", type=positive_int, default=9999)
    parser.add_argument("--model")
    parser.add_argument("--sandbox", default="read-only", choices=["read-only", "workspace-write", "danger-full-access"])
    parser.add_argument("--approval", default="never", choices=["never", "on-request", "untrusted", "on-failure"])
    parser.add_argument("--max-agents", "--concurrency", dest="max_agents", type=positive_int, default=4)
    parser.add_argument("--max-round", type=positive_int, default=3, help="maximum worker expansion round depth; default: 3")
    parser.add_argument("--max-job", type=positive_int, default=None, help="maximum total workers including expansions; default: unlimited")
    parser.add_argument("--startup-delay", type=nonnegative_float, default=1.0)
    parser.add_argument("--dry-run", action="store_true", help="record workers but do not launch worker CLIs")
    parser.add_argument("--mock", action="store_true", help="mock planner and workers without model calls")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(start_workflow(args))


if __name__ == "__main__":
    main()

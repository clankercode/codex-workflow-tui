#!/usr/bin/env python3
"""Run parallel coding-CLI workers and mirror their progress into workflow state."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
import re
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

import workflow_state

PHASE_ID = "phase-cli-workers"
CCC_FOOTER_RE = re.compile(r"^>> ccc:output-log >> (.+)$", re.MULTILINE)


@dataclass(frozen=True)
class WorkerResult:
    """Normalized result extracted from one coding-CLI worker run."""

    result: str
    summary: str
    thread_id: str = ""
    jsonl_path: str | None = None
    log_path: str | None = None
    output_path: str | None = None


class RunnerProvider:
    """Build and interpret commands for one worker backend."""

    def __init__(self, name: str, agent_type: str) -> None:
        self.name = name
        self.agent_type = agent_type

    def build_command(self, agent: dict[str, Any], args: argparse.Namespace) -> list[str]:
        raise NotImplementedError

    def extract_result(self, agent: dict[str, Any], exit_code: int) -> WorkerResult:
        output_path = Path(agent["output_path"])
        text = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
        return WorkerResult(result=text, summary=first_line(text) or f"{self.name} exited {exit_code}")


class CodexDirectProvider(RunnerProvider):
    """Use Codex directly through `codex exec --json`."""

    def __init__(self) -> None:
        super().__init__("codex-direct", "codex-exec")

    def build_command(self, agent: dict[str, Any], args: argparse.Namespace) -> list[str]:
        command = [
            "codex",
            "--ask-for-approval",
            args.approval,
            "exec",
            "--json",
            "--cd",
            args.cwd,
            "--sandbox",
            args.sandbox,
        ]
        if args.model:
            command.extend(["--model", args.model])
        command.append(agent["prompt"])
        return command

    def extract_result(self, agent: dict[str, Any], exit_code: int) -> WorkerResult:
        final_message = ""
        thread_id = ""
        jsonl_path = Path(agent["jsonl_path"])
        if jsonl_path.exists():
            for line in jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "thread.started":
                    thread_id = event.get("thread_id", "")
                item = event.get("item") or {}
                if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                    final_message = item.get("text", final_message)
        Path(agent["output_path"]).write_text(final_message, encoding="utf-8")
        return WorkerResult(
            result=final_message,
            summary=first_line(final_message) or f"codex exec exited {exit_code}",
            thread_id=thread_id,
        )


class OpencodeDirectProvider(RunnerProvider):
    """Use OpenCode directly through `opencode run --format json`."""

    def __init__(self) -> None:
        super().__init__("opencode-direct", "opencode-run")

    def build_command(self, agent: dict[str, Any], args: argparse.Namespace) -> list[str]:
        command = [
            "opencode",
            "run",
            "--format",
            "json",
            "--dir",
            args.cwd,
            "--title",
            agent["name"],
        ]
        if args.model:
            command.extend(["--model", args.model])
        if args.cli_agent:
            command.extend(["--agent", args.cli_agent])
        command.append(agent["prompt"])
        return command

    def extract_result(self, agent: dict[str, Any], exit_code: int) -> WorkerResult:
        final_message = ""
        session_id = ""
        jsonl_path = Path(agent["jsonl_path"])
        if jsonl_path.exists():
            for line in jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event.get("response"), str):
                    final_message = event["response"]
                part = event.get("part") or {}
                if event.get("type") == "text" and isinstance(part.get("text"), str):
                    final_message = part["text"]
                if isinstance(event.get("sessionID"), str):
                    session_id = event["sessionID"]
                if isinstance(event.get("session_id"), str):
                    session_id = event["session_id"]
        Path(agent["output_path"]).write_text(final_message, encoding="utf-8")
        return WorkerResult(
            result=final_message,
            summary=first_line(final_message) or f"opencode run exited {exit_code}",
            thread_id=session_id,
        )


class CccProvider(RunnerProvider):
    """Use `ccc` as a stable adapter around a coding CLI."""

    def __init__(self, selector: str, agent_type: str, selector_kind: str = "runner") -> None:
        label_source = selector[1:] if selector.startswith("@") else selector
        self.selector_label = workflow_state.slugify(label_source, fallback="target")
        super().__init__(f"ccc-{self.selector_label}", agent_type)
        self.selector = selector
        self.selector_kind = selector_kind

    def build_command(self, agent: dict[str, Any], args: argparse.Namespace) -> list[str]:
        command = [
            "ccc",
            "--no-show-thinking",
            "--cleanup-session",
            "--output-log-path",
            "--output-mode",
            args.ccc_output_mode,
        ]
        if args.permission_mode:
            command.extend(["--permission-mode", args.permission_mode])
        if args.timeout_secs:
            command.extend(["--timeout-secs", str(args.timeout_secs)])
        command.append(self.selector)
        command.extend(args.ccc_control or [])
        command.extend(["--", agent["prompt"]])
        return command

    def extract_result(self, agent: dict[str, Any], exit_code: int) -> WorkerResult:
        stderr_path = Path(agent["log_path"])
        stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
        match = CCC_FOOTER_RE.search(stderr_text)
        if not match:
            stdout_path = Path(agent["jsonl_path"])
            stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else ""
            return WorkerResult(result=stdout_text, summary=first_line(stdout_text) or f"ccc exited {exit_code}")

        run_dir = Path(match.group(1)).expanduser()
        output_path = run_dir / "output.txt"
        transcript_path = run_dir / "transcript.jsonl"
        if not transcript_path.exists():
            transcript_path = run_dir / "transcript.txt"
        result = output_path.read_text(encoding="utf-8", errors="replace") if output_path.exists() else ""
        return WorkerResult(
            result=result,
            summary=first_line(result) or f"ccc exited {exit_code}",
            thread_id=run_dir.name,
            jsonl_path=str(transcript_path) if transcript_path.exists() else None,
            output_path=str(output_path) if output_path.exists() else None,
        )


def build_provider(args: argparse.Namespace) -> RunnerProvider:
    if args.runner == "codex-direct":
        return CodexDirectProvider()
    if args.runner == "opencode-direct":
        return OpencodeDirectProvider()
    if args.runner == "ccc-codex":
        return CccProvider("codex", "ccc-codex")
    if args.runner == "ccc-opencode":
        return CccProvider("opencode", "ccc-opencode")
    if args.runner == "ccc":
        selector = args.ccc_runner or "opencode"
        selector_kind = "preset" if selector.startswith("@") else "runner"
        label_source = selector[1:] if selector.startswith("@") else selector
        return CccProvider(selector, f"ccc-{selector_kind}-{workflow_state.slugify(label_source, fallback='target')}", selector_kind)
    raise SystemExit(f"unknown runner {args.runner!r}")


def parse_job(value: str) -> dict[str, str]:
    if "::" in value:
        name, prompt = value.split("::", 1)
    else:
        name, prompt = f"job-{abs(hash(value)) % 10000}", value
    return {"name": name.strip(), "role": name.strip(), "prompt": prompt.strip()}


def load_jobs(args: argparse.Namespace) -> list[dict[str, str]]:
    jobs = [parse_job(item) for item in args.job or []]
    if args.jobs_file:
        path = Path(args.jobs_file).expanduser()
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, list):
            raise SystemExit("--jobs-file must contain a JSON array")
        for item in loaded:
            if not isinstance(item, dict) or "prompt" not in item:
                raise SystemExit("each job object must contain at least a prompt")
            name = str(item.get("name") or item.get("role") or f"job-{len(jobs) + 1}")
            jobs.append({"name": name, "role": str(item.get("role") or name), "prompt": str(item["prompt"])})
    if not jobs:
        raise SystemExit("provide at least one --job or --jobs-file entry")
    return jobs


def create_run(args: argparse.Namespace, jobs: list[dict[str, str]], provider: RunnerProvider) -> dict[str, Any]:
    init_args = argparse.Namespace(
        title=args.title,
        prompt=args.prompt or f"Run {len(jobs)} {provider.name} workflow workers.",
        prompt_file=args.prompt_file,
        cwd=args.cwd,
        mode=provider.name,
        tag=args.tag or [],
        thread_id=None,
        coordinator_tool=provider.name,
    )
    init_output = io.StringIO()
    with contextlib.redirect_stdout(init_output):
        workflow_state.cmd_init(init_args)
    created = json.loads(init_output.getvalue())
    run = workflow_state.load_run(created["run_id"])
    phase_args = argparse.Namespace(
        run=run["run_id"],
        name="Coding CLI workers",
        goal=f"Run independent {provider.name} workers and collect durable results.",
        phase_id=PHASE_ID,
        status="running",
    )
    with open(os.devnull, "w", encoding="utf-8") as sink, contextlib.redirect_stdout(sink):
        workflow_state.cmd_add_phase(phase_args)
    record_runner_decision(run["run_id"], args, provider, len(jobs))
    return workflow_state.load_run(run["run_id"])


def record_runner_decision(run_id: str, args: argparse.Namespace, provider: RunnerProvider, job_count: int) -> None:
    """Record the provider/concurrency choice as a first-class workflow decision."""
    def mutator(run: dict[str, Any]) -> None:
        decision_id = f"dec-runner-{workflow_state.slugify(provider.name, fallback='runner')}"
        if any(item.get("decision_id") == decision_id for item in run.setdefault("decisions", [])):
            return
        decision = {
            "decision_id": decision_id,
            "ts": workflow_state.now(),
            "title": f"Runner selected: {provider.name}",
            "rationale": (
                f"Run {job_count} coding-CLI worker(s) with max_agents={args.max_agents}, "
                f"startup_delay={args.startup_delay}, sandbox={args.sandbox}."
            ),
            "made_by": "workflow_run.py",
        }
        run.setdefault("decisions", []).append(decision)
        workflow_state.add_event(
            run,
            "info",
            f"decision recorded: {decision['title']}",
            kind="decision",
            operation="recorded",
            source="workflow_run.create_run",
            data={"title": decision["title"], "made_by": decision["made_by"], "runner": provider.name},
        )

    workflow_state.mutate_run(run_id, mutator)


def add_agent(run: dict[str, Any], job: dict[str, str], args: argparse.Namespace, provider: RunnerProvider, index: int) -> dict[str, Any]:
    prefix = workflow_state.slugify(provider.name, fallback="worker")
    agent_id = f"{prefix}-{index + 1:02d}-{workflow_state.slugify(job['name'])}"
    artifacts = Path(run["paths"]["artifacts_dir"])
    logs = Path(run["paths"]["logs_dir"])
    prompt_path = artifacts / f"{agent_id}.prompt.md"
    jsonl_path = logs / f"{agent_id}.jsonl"
    stderr_path = logs / f"{agent_id}.stderr.log"
    output_path = artifacts / f"{agent_id}.final.md"
    prompt_path.write_text(job["prompt"] + "\n", encoding="utf-8")

    agent_args = argparse.Namespace(
        run=run["run_id"],
        phase=PHASE_ID,
        name=job["name"],
        role=job["role"],
        agent_type=provider.agent_type,
        agent_id=agent_id,
        status="pending",
        prompt=None,
        prompt_file=str(prompt_path),
        cwd=args.cwd,
        model=args.model or "",
        thread_id=None,
        process_id=None,
        write_scope=[],
        jsonl_path=str(jsonl_path),
        log_path=str(stderr_path),
        output_path=str(output_path),
    )
    with open(os.devnull, "w", encoding="utf-8") as sink, contextlib.redirect_stdout(sink):
        workflow_state.cmd_add_agent(agent_args)
    return workflow_state.load_run(run["run_id"])


def update_agent(run_id: str, agent_id: str, **values: Any) -> None:
    def mutator(run: dict[str, Any]) -> None:
        agent = workflow_state.find_item(run.setdefault("agents", []), "agent_id", agent_id)
        for key, value in values.items():
            if value is not None:
                agent[key] = value
        status = values.get("status")
        if status in {"running", "completed", "failed", "cancelled"} and not agent.get("started_at"):
            agent["started_at"] = workflow_state.now()
        if status in {"completed", "failed", "cancelled"}:
            agent["completed_at"] = workflow_state.now()
        elif status:
            agent["completed_at"] = None
        agent["updated_at"] = workflow_state.now()
        workflow_state.add_event(
            run,
            "info",
            f"worker {agent['name']} {agent['status']}",
            kind="agent",
            operation="updated",
            source="workflow_run.update_agent",
            agent_id=agent_id,
            phase_id=agent.get("phase_id"),
            data={
                "name": agent.get("name", ""),
                "status": agent.get("status", ""),
                "agent_type": agent.get("agent_type", ""),
                "model": agent.get("model", ""),
                "exit_code": agent.get("exit_code"),
            },
        )

    workflow_state.mutate_run(run_id, mutator)


class StartupRateLimiter:
    """Serialize worker starts so external CLIs are not launched in a burst."""

    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self._lock = asyncio.Lock()
        self._last_start = 0.0

    async def _wait_locked(self) -> None:
        if self.delay_seconds > 0 and self._last_start:
            wait_seconds = max(0.0, self._last_start + self.delay_seconds - time.monotonic())
            if wait_seconds:
                await asyncio.sleep(wait_seconds)

    async def mark_virtual_start(self) -> None:
        """Pace mock and dry-run workers without launching a process."""
        async with self._lock:
            await self._wait_locked()
            self._last_start = time.monotonic()

    async def create_process(self, command: list[str]) -> asyncio.subprocess.Process:
        """Launch a subprocess while holding the rate-limit slot."""
        async with self._lock:
            await self._wait_locked()
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._last_start = time.monotonic()
            return proc


def timing_fields(started_epoch: float) -> dict[str, float]:
    completed_epoch = time.time()
    return {
        "completed_epoch": round(completed_epoch, 3),
        "duration_seconds": round(completed_epoch - started_epoch, 3),
    }


def command_preview(command: list[str]) -> str:
    preview = list(command)
    if preview:
        preview[-1] = "<prompt>"
    return shlex.join(preview)


async def run_worker(
    run_id: str,
    agent: dict[str, Any],
    args: argparse.Namespace,
    provider: RunnerProvider,
    semaphore: asyncio.Semaphore,
    startup_limiter: StartupRateLimiter,
) -> None:
    started_epoch: float | None = None
    async with semaphore:
        try:
            command = [] if args.mock or args.dry_run else provider.build_command(agent, args)
            if args.mock:
                await startup_limiter.mark_virtual_start()
                started_epoch = time.time()
                update_agent(
                    run_id,
                    agent["agent_id"],
                    status="running",
                    summary="mock worker running",
                    started_epoch=round(started_epoch, 3),
                )
                await asyncio.sleep(0.2)
                Path(agent["output_path"]).write_text(f"Mock result for {agent['name']}\n", encoding="utf-8")
                update_agent(
                    run_id,
                    agent["agent_id"],
                    status="completed",
                    result=f"Mock result for {agent['name']}",
                    summary="mock worker completed",
                    exit_code=0,
                    **timing_fields(started_epoch),
                )
                return
            if args.dry_run:
                await startup_limiter.mark_virtual_start()
                started_epoch = time.time()
                message = f"Dry run only; {provider.name} was not launched."
                Path(agent["output_path"]).write_text(message + "\n", encoding="utf-8")
                update_agent(
                    run_id,
                    agent["agent_id"],
                    status="completed",
                    summary="dry run; worker not launched",
                    result=message,
                    exit_code=0,
                    started_epoch=round(started_epoch, 3),
                    **timing_fields(started_epoch),
                )
                return

            proc = await startup_limiter.create_process(command)
            started_epoch = time.time()
            update_agent(
                run_id,
                agent["agent_id"],
                status="running",
                summary="process started",
                started_epoch=round(started_epoch, 3),
                command_preview=command_preview(command),
            )
            update_agent(run_id, agent["agent_id"], process_id=proc.pid)

            jsonl_path = Path(agent["jsonl_path"])
            stderr_path = Path(agent["log_path"])
            assert proc.stdout is not None
            assert proc.stderr is not None

            async def read_stdout() -> None:
                with jsonl_path.open("wb") as handle:
                    async for line in proc.stdout:
                        handle.write(line)
                        handle.flush()

            async def read_stderr() -> None:
                with stderr_path.open("wb") as handle:
                    async for line in proc.stderr:
                        handle.write(line)
                        handle.flush()

            await asyncio.gather(read_stdout(), read_stderr())
            exit_code = await proc.wait()
            extracted = provider.extract_result(agent, exit_code)
            status = "completed" if exit_code == 0 else "failed"
            update_agent(
                run_id,
                agent["agent_id"],
                status=status,
                result=extracted.result,
                summary=extracted.summary,
                exit_code=exit_code,
                thread_id=extracted.thread_id,
                jsonl_path=extracted.jsonl_path,
                log_path=extracted.log_path,
                output_path=extracted.output_path,
                **timing_fields(started_epoch),
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            output_path = agent.get("output_path")
            if output_path:
                Path(output_path).write_text(message + "\n", encoding="utf-8")
            fields = timing_fields(started_epoch) if started_epoch is not None else {}
            update_agent(run_id, agent["agent_id"], status="failed", result=message, summary=message, exit_code=1, **fields)


def first_line(text: str) -> str:
    for line in text.splitlines():
        clean = line.strip()
        if clean:
            return clean[:240]
    return ""


async def run_all(run: dict[str, Any], args: argparse.Namespace, provider: RunnerProvider) -> str:
    semaphore = asyncio.Semaphore(args.max_agents)
    startup_limiter = StartupRateLimiter(args.startup_delay)
    agents = list(run.get("agents", []))
    results = await asyncio.gather(
        *(run_worker(run["run_id"], agent, args, provider, semaphore, startup_limiter) for agent in agents),
        return_exceptions=True,
    )

    def mutator(final: dict[str, Any]) -> str:
        for agent, result in zip(agents, results, strict=True):
            if isinstance(result, Exception):
                state_agent = workflow_state.find_item(final.setdefault("agents", []), "agent_id", agent["agent_id"])
                if state_agent.get("status") in {"pending", "running"}:
                    message = f"{type(result).__name__}: {result}"
                    state_agent["status"] = "failed"
                    state_agent["summary"] = message
                    state_agent["result"] = message
                    state_agent["exit_code"] = 1
                    state_agent["completed_at"] = workflow_state.now()
                    state_agent["updated_at"] = workflow_state.now()
                    workflow_state.add_event(
                        final,
                        "info",
                        f"worker {state_agent['name']} failed",
                        kind="agent",
                        operation="updated",
                        source="workflow_run.run_all",
                        agent_id=state_agent["agent_id"],
                        phase_id=state_agent.get("phase_id"),
                        data={"name": state_agent.get("name", ""), "status": "failed"},
                    )
        failed = [agent for agent in final.get("agents", []) if agent.get("status") == "failed"]
        pending = [agent for agent in final.get("agents", []) if agent.get("status") in {"pending", "running"}]
        final["status"] = "failed" if failed else "completed"
        if pending:
            final["status"] = "running"
        record_worker_artifacts(final)
        phase = workflow_state.find_item(final.setdefault("phases", []), "phase_id", PHASE_ID)
        phase["status"] = final["status"]
        phase["completed_at"] = workflow_state.now() if final["status"] in {"completed", "failed"} else None
        workflow_state.add_event(
            final,
            "info",
            f"{provider.name} worker phase {final['status']}",
            kind="phase",
            operation="updated",
            source="workflow_run.run_all",
            phase_id=phase["phase_id"],
            data={"status": final["status"], "runner": provider.name},
        )
        return final["status"]

    _, status, _ = workflow_state.mutate_run(run["run_id"], mutator)
    return status


def record_worker_artifacts(run: dict[str, Any]) -> None:
    """Expose each worker final output path through the Artifacts tab."""
    artifacts = run.setdefault("artifacts", [])
    existing_ids = {artifact.get("artifact_id") for artifact in artifacts}
    for agent in run.get("agents", []):
        output_path = str(agent.get("output_path") or "")
        if not output_path:
            continue
        artifact_id = f"art-{workflow_state.slugify(str(agent.get('agent_id', 'worker')), fallback='worker')}-output"
        if artifact_id in existing_ids:
            continue
        artifact = {
            "artifact_id": artifact_id,
            "ts": workflow_state.now(),
            "kind": "worker-output",
            "title": f"{agent.get('name', agent.get('agent_id', 'Worker'))} final output",
            "path": output_path,
            "agent_id": agent.get("agent_id", ""),
            "phase_id": agent.get("phase_id", ""),
        }
        artifacts.append(artifact)
        existing_ids.add(artifact_id)
        workflow_state.add_event(
            run,
            "info",
            f"artifact recorded: {artifact['title']}",
            kind="artifact",
            operation="recorded",
            source="workflow_run.worker_artifact",
            phase_id=artifact["phase_id"],
            agent_id=artifact["agent_id"],
            data={"title": artifact["title"], "kind": artifact["kind"], "path": artifact["path"]},
        )


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
    parser.add_argument("--title", required=True)
    parser.add_argument("--prompt")
    parser.add_argument("--prompt-file")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--job", action="append", help="name::prompt; repeat for multiple workers")
    parser.add_argument("--jobs-file", help="JSON array of {name, role, prompt}")
    parser.add_argument("--tag", action="append")
    parser.add_argument(
        "--runner",
        default="codex-direct",
        choices=["codex-direct", "ccc-codex", "ccc-opencode", "ccc", "opencode-direct"],
        help="coding CLI provider to use for worker processes",
    )
    parser.add_argument("--ccc-runner", help="ccc target for --runner ccc: a CLI selector like kimi or opencode, or a preset like @mm")
    parser.add_argument("--ccc-control", action="append", help="extra ccc control token before the prompt; repeatable, e.g. @reviewer or +3")
    parser.add_argument("--ccc-output-mode", default="stream-json", choices=["formatted", "stream-formatted", "text", "stream-text", "json", "stream-json", "pass-text", "pass-json"])
    parser.add_argument("--permission-mode", choices=["safe", "auto", "yolo", "plan"], help="forwarded to ccc providers")
    parser.add_argument("--cli-agent", help="direct OpenCode agent name for --runner opencode-direct")
    parser.add_argument("--timeout-secs", type=positive_int, help="forwarded to ccc providers")
    parser.add_argument("--model")
    parser.add_argument("--sandbox", default="read-only", choices=["read-only", "workspace-write", "danger-full-access"])
    parser.add_argument("--approval", default="never", choices=["never", "on-request", "untrusted", "on-failure"])
    parser.add_argument("--max-agents", "--concurrency", dest="max_agents", type=positive_int, default=4, help="maximum worker processes running at once; default: 4")
    parser.add_argument("--startup-delay", type=nonnegative_float, default=1.0, help="minimum seconds between worker starts; default: 1.0")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--mock", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    jobs = load_jobs(args)
    provider = build_provider(args)
    run = create_run(args, jobs, provider)
    for index, job in enumerate(jobs):
        run = add_agent(run, job, args, provider, index)
    print(json.dumps({"run_id": run["run_id"], "path": run["paths"]["run_json"], "jobs": len(jobs)}, indent=2))
    if args.dry_run:
        print("dry run: workers were recorded but not launched")
    else:
        replay = ["python3", __file__, "--runner", args.runner]
        if args.runner == "ccc" and args.ccc_runner:
            replay.extend(["--ccc-runner", args.ccc_runner])
        replay.extend(["--title", args.title, "..."])
        print("command:", shlex.join(replay))
    status = asyncio.run(run_all(workflow_state.load_run(run["run_id"]), args, provider))
    if status != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()

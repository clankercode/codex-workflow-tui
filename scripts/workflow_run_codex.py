#!/usr/bin/env python3
"""Run parallel coding-CLI workers and mirror their progress into workflow state."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import io
import json
import os
import re
import signal
import shlex
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

import workflow_state

try:
    import jsonschema
except Exception:  # pragma: no cover - optional runtime dependency
    jsonschema = None  # type: ignore[assignment]

PHASE_ID = "phase-cli-workers"
CCC_FOOTER_RE = re.compile(r"^>> ccc:output-log >> (.+)$", re.MULTILINE)
KIMI_RESUME_RE = re.compile(r"\bkimi\s+-r\s+(\S+)")
QUOTA_LIMIT_RE = re.compile(
    r"\b(429|quota|rate\s*limit|usage\s*limit|too many requests|resource_exhausted|limit for this period)\b",
    re.IGNORECASE,
)


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

    def stdin_payload(self, agent: dict[str, Any], args: argparse.Namespace) -> bytes | None:
        """Return stdin content for providers that do not accept large prompts on argv."""
        return None

    def preview_command(self, command: list[str], agent: dict[str, Any], args: argparse.Namespace) -> str:
        """Return the sanitized command shown in workflow state."""
        return command_preview(command)

    def extract_result(
        self,
        agent: dict[str, Any],
        exit_code: int,
        *,
        stdout_text: str | None = None,
        stderr_text: str | None = None,
    ) -> WorkerResult:
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

    def extract_result(
        self,
        agent: dict[str, Any],
        exit_code: int,
        *,
        stdout_text: str | None = None,
        stderr_text: str | None = None,
    ) -> WorkerResult:
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

    def extract_result(
        self,
        agent: dict[str, Any],
        exit_code: int,
        *,
        stdout_text: str | None = None,
        stderr_text: str | None = None,
    ) -> WorkerResult:
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


class KimiDirectProvider(RunnerProvider):
    """Use Kimi directly in quiet print mode with the prompt on stdin."""

    def __init__(self) -> None:
        super().__init__("kimi-direct", "kimi-cli")

    def build_command(self, agent: dict[str, Any], args: argparse.Namespace) -> list[str]:
        command = [
            "kimi",
            "--quiet",
            "--input-format",
            "text",
            "--work-dir",
            args.cwd,
        ]
        if args.model:
            command.extend(["--model", args.model])
        if args.kimi_max_steps_per_turn:
            command.extend(["--max-steps-per-turn", str(args.kimi_max_steps_per_turn)])
        return command

    def stdin_payload(self, agent: dict[str, Any], args: argparse.Namespace) -> bytes:
        prompt = str(agent["prompt"])
        if not prompt.endswith("\n"):
            prompt += "\n"
        return prompt.encode("utf-8")

    def preview_command(self, command: list[str], agent: dict[str, Any], args: argparse.Namespace) -> str:
        return f"{shlex.join(command)} <prompt-on-stdin>"

    def extract_result(
        self,
        agent: dict[str, Any],
        exit_code: int,
        *,
        stdout_text: str | None = None,
        stderr_text: str | None = None,
    ) -> WorkerResult:
        jsonl_path = Path(agent["jsonl_path"])
        stderr_path = Path(agent["log_path"])
        final_message = jsonl_path.read_text(encoding="utf-8", errors="replace") if jsonl_path.exists() else ""
        stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
        match = KIMI_RESUME_RE.search(stderr_text)
        Path(agent["output_path"]).write_text(final_message, encoding="utf-8")
        return WorkerResult(
            result=final_message,
            summary=first_line(final_message) or f"kimi exited {exit_code}",
            thread_id=match.group(1) if match else "",
        )


class CccProvider(RunnerProvider):
    """Use `ccc` as a stable adapter around a coding CLI."""

    OPENCODE_SELECTOR_LABELS = {
        "opencode",
        "oc",
        "mm",
        "mm3",
        "mm27",
        "mimo25p",
        "glm5t",
        "glm51",
    }
    CODEX_SELECTOR_LABELS = {"codex", "claude", "c", "cx", "cx-coder", "cx-reviewer"}

    def __init__(self, selector: str, agent_type: str, selector_kind: str = "runner") -> None:
        label_source = selector[1:] if selector.startswith("@") else selector
        self.selector_label = workflow_state.slugify(label_source, fallback="target")
        super().__init__(f"ccc-{self.selector_label}", agent_type)
        self.selector = selector
        self.selector_kind = selector_kind

    def is_kimi_selector(self) -> bool:
        """Return true for ccc selectors that dispatch to Kimi."""
        return self.selector_label in {"kimi", "k"}

    def is_opencode_selector(self) -> bool:
        """Return true for ccc selectors known to dispatch to OpenCode."""
        return self.selector_label in self.OPENCODE_SELECTOR_LABELS

    def is_codex_selector(self) -> bool:
        """Return true for ccc selectors known to dispatch to Codex."""
        return self.selector_label in self.CODEX_SELECTOR_LABELS

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
        if self.is_opencode_selector():
            command.extend(["--runner-arg", "--dir", "--runner-arg", args.cwd])
        elif self.is_kimi_selector():
            command.extend(["--runner-arg", "--work-dir", "--runner-arg", args.cwd])
        elif self.is_codex_selector():
            command.extend(["--runner-arg", "--cd", "--runner-arg", args.cwd])
        if self.is_kimi_selector() and args.kimi_max_steps_per_turn:
            command.extend(["--runner-arg", "--max-steps-per-turn", "--runner-arg", str(args.kimi_max_steps_per_turn)])
        command.append(self.selector)
        command.extend(args.ccc_control or [])
        command.extend(["--", agent["prompt"]])
        return command

    def extract_result(
        self,
        agent: dict[str, Any],
        exit_code: int,
        *,
        stdout_text: str | None = None,
        stderr_text: str | None = None,
    ) -> WorkerResult:
        stderr_path = Path(agent["log_path"])
        footer_text = stderr_text
        if footer_text is None:
            footer_text = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
        matches = list(CCC_FOOTER_RE.finditer(footer_text))
        match = matches[-1] if matches else None
        if not match:
            stdout_path = Path(agent["jsonl_path"])
            output_text = stdout_text
            if output_text is None:
                output_text = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else ""
            return WorkerResult(result=output_text, summary=first_line(output_text) or f"ccc exited {exit_code}")

        run_dir = Path(match.group(1)).expanduser()
        output_path = run_dir / "output.txt"
        transcript_path = run_dir / "transcript.jsonl"
        if not transcript_path.exists():
            transcript_path = run_dir / "transcript.txt"
        result = output_path.read_text(encoding="utf-8", errors="replace") if output_path.exists() else ""
        archived_output_path = Path(agent["output_path"])
        archived_transcript_path = Path(agent["jsonl_path"])
        archived_output_path.parent.mkdir(parents=True, exist_ok=True)
        archived_transcript_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.exists():
            shutil.copy2(output_path, archived_output_path)
        if transcript_path.exists():
            shutil.copy2(transcript_path, archived_transcript_path)
        return WorkerResult(
            result=result,
            summary=first_line(result) or f"ccc exited {exit_code}",
            thread_id=run_dir.name,
            jsonl_path=str(archived_transcript_path) if transcript_path.exists() else None,
            output_path=str(archived_output_path) if output_path.exists() else None,
        )


def build_provider(args: argparse.Namespace) -> RunnerProvider:
    if args.runner == "codex-direct":
        return CodexDirectProvider()
    if args.runner == "opencode-direct":
        return OpencodeDirectProvider()
    if args.runner == "kimi-direct":
        return KimiDirectProvider()
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
    """Parse a job from a CLI value.

    Supports the legacy ``name::prompt`` and bare-prompt forms, plus a JSON
    object form that carries ``stage``/``depends_on``/``schema`` metadata:
    ``'{"name":"x","prompt":"y","stage":"s","depends_on":"a"}'``.
    """
    value = value.strip()
    if value.startswith("{"):
        try:
            item = json.loads(value)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"invalid JSON job: {exc}")
        if not isinstance(item, dict) or "prompt" not in item:
            raise SystemExit("JSON job must be an object with a prompt")
        prompt = str(item["prompt"]).strip()
        if not prompt:
            raise SystemExit("job prompt must be non-empty")
        raw_name = str(item.get("name") or item.get("role") or "job")
        name = workflow_state.slugify(raw_name, fallback="job")
        return {
            "name": name,
            "role": str(item.get("role") or raw_name).strip() or name,
            "prompt": prompt,
            "stage": str(item.get("stage") or "").strip(),
            "depends_on": item.get("depends_on") or "",
        }
    if "::" in value:
        name, prompt = value.split("::", 1)
    else:
        digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
        name, prompt = f"job-{digest}", value
    return {"name": name.strip(), "role": name.strip(), "prompt": prompt.strip(), "stage": "", "depends_on": ""}


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
            jobs.append(
                {
                    "name": name,
                    "role": str(item.get("role") or name),
                    "prompt": str(item["prompt"]),
                    "stage": str(item.get("stage") or ""),
                    "depends_on": item.get("depends_on") or "",
                }
            )
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
    parent_run = getattr(args, "parent_run", None)
    if parent_run:
        link_parent_run(run["run_id"], parent_run)
    return workflow_state.load_run(run["run_id"])


def attach_run(args: argparse.Namespace, jobs: list[dict[str, str]], provider: RunnerProvider) -> dict[str, Any]:
    """Load an existing run and prepare it for coding-CLI workers."""
    run_id = args.attach_run

    def mutator(run: dict[str, Any]) -> None:
        run["status"] = "running"
        phases = run.setdefault("phases", [])
        phase = next((item for item in phases if item.get("phase_id") == PHASE_ID), None)
        timestamp = workflow_state.now()
        if phase is None:
            phase = {
                "phase_id": PHASE_ID,
                "name": "Coding CLI workers",
                "goal": f"Run independent {provider.name} workers and collect durable results.",
                "status": "running",
                "created_at": timestamp,
                "started_at": timestamp,
                "completed_at": None,
                "agent_ids": [],
            }
            phases.append(phase)
            workflow_state.add_event(
                run,
                "info",
                "phase added: Coding CLI workers",
                kind="phase",
                operation="added",
                source="workflow_run.attach_run",
                phase_id=PHASE_ID,
                data={"name": phase["name"], "status": phase["status"]},
            )
        else:
            phase["status"] = "running"
            phase["started_at"] = phase.get("started_at") or timestamp
            phase["completed_at"] = None

    workflow_state.mutate_run(run_id, mutator)
    record_runner_decision(run_id, args, provider, len(jobs))
    return workflow_state.load_run(run_id)


def link_parent_run(child_run_id: str, parent_run_id: str) -> None:
    """Persist child metadata and add one parent event linking to the child."""
    def child_mutator(run: dict[str, Any]) -> None:
        run.setdefault("metadata", {})["parent_run_id"] = parent_run_id

    workflow_state.mutate_run(child_run_id, child_mutator)

    def parent_mutator(run: dict[str, Any]) -> None:
        workflow_state.add_event(
            run,
            "info",
            f"child workflow launched: {child_run_id}",
            kind="workflow",
            operation="child_run_linked",
            source="workflow_run.create_run",
            data={"child_run_id": child_run_id},
        )

    workflow_state.mutate_run(parent_run_id, parent_mutator)


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
            "made_by": "workflow_run_codex.py",
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


def add_agent(
    run: dict[str, Any],
    job: dict[str, str],
    args: argparse.Namespace,
    provider: RunnerProvider,
    index: int,
    stage: str = "",
    depends_on: str = "",
    phase_id: str | None = None,
) -> dict[str, Any]:
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
        phase=phase_id or PHASE_ID,
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
    extra: dict[str, Any] = {}
    if stage:
        extra["stage"] = stage
    if depends_on:
        extra["depends_on"] = depends_on
    if job.get("schema"):
        extra["schema"] = _resolve_schema(job["schema"])
    if extra:
        update_agent(run["run_id"], agent_id, **extra)
    return workflow_state.load_run(run["run_id"])


def update_agent(run_id: str, agent_id: str, *, emit_event: bool | None = None, **values: Any) -> None:
    def mutator(run: dict[str, Any]) -> None:
        agent = workflow_state.find_item(run.setdefault("agents", []), "agent_id", agent_id)
        previous_status = agent.get("status")
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
        run["last_activity_at"] = agent["updated_at"]
        should_emit = emit_event if emit_event is not None else bool(status and status != previous_status)
        if not should_emit:
            return
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


def telemetry_fields_for_agent(agent: dict[str, Any]) -> dict[str, Any]:
    """Return best-effort live telemetry parsed from an agent's durable logs."""
    try:
        import workflow_tui  # pylint: disable=import-outside-toplevel
    except Exception:
        return {}
    try:
        activity = workflow_tui.agent_activity(agent)
    except Exception:
        return {}
    fields: dict[str, Any] = {
        "tool_call_count": activity.get("tool_call_count", 0),
        "latest_tool_calls": activity.get("tool_calls", []),
        "latest_output": activity.get("latest_output", ""),
        "last_activity_epoch": activity.get("last_activity_epoch", 0.0),
        "transcript_path": activity.get("transcript_path", ""),
        "activity_output_path": activity.get("output_path", ""),
    }
    tokens = activity.get("tokens")
    if isinstance(tokens, dict):
        fields["tokens"] = tokens
        fields["token_total"] = tokens.get("total") if tokens.get("known") else None
    return fields


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

    async def create_process(self, command: list[str], cwd: str | None = None) -> asyncio.subprocess.Process:
        """Launch a subprocess while holding the rate-limit slot."""
        async with self._lock:
            await self._wait_locked()
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd,
                start_new_session=True,
                stdin=asyncio.subprocess.PIPE,
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


def quota_limit_detected(*texts: str) -> bool:
    """Return true when provider output looks like a quota/rate-limit failure."""
    return any(QUOTA_LIMIT_RE.search(text or "") for text in texts)


def seconds_until_next_quota_window(now: datetime | None = None, *, buffer_seconds: float = 5.0) -> float:
    """Return seconds until the next wall-clock :00 or :30 retry window."""
    current = now or datetime.now().astimezone()
    current = current.replace(microsecond=0)
    if current.minute < 30:
        target = current.replace(minute=30, second=0)
    else:
        target = (current.replace(minute=0, second=0) + timedelta(hours=1))
    return max(0.0, (target - current).total_seconds() + buffer_seconds)


def quota_retry_sleep_seconds(args: argparse.Namespace) -> float:
    """Return the quota retry sleep duration, with a test-only env override."""
    override = os.environ.get("WORKFLOW_QUOTA_RETRY_SLEEP_OVERRIDE_SECS")
    if override is not None:
        return max(0.0, float(override))
    return seconds_until_next_quota_window(buffer_seconds=float(getattr(args, "quota_retry_buffer_secs", 5.0)))


def test_interval_env(name: str, default: float) -> float:
    """Return a test-only polling/sleep interval override."""
    override = os.environ.get(name)
    if override is None:
        return default
    return max(0.0, float(override))


async def sleep_until_quota_retry_allowed(run_id: str, agent_id: str, sleep_seconds: float) -> bool:
    """Sleep for quota backoff, returning false if workflow control cancels it."""
    remaining = max(0.0, sleep_seconds)
    while remaining > 0:
        if stop_requested(run_control(run_id)):
            update_agent(run_id, agent_id, status="cancelled", summary="cancelled during quota retry backoff", exit_code=75)
            return False
        interval = min(remaining, test_interval_env("WORKFLOW_QUOTA_RETRY_POLL_SECS", 5.0))
        await asyncio.sleep(interval)
        remaining -= interval
    return not stop_requested(run_control(run_id))


def append_attempt_marker(path: Path, attempt: int, stream_name: str) -> None:
    """Append a visible retry attempt separator to a worker stream log."""
    with path.open("ab") as handle:
        handle.write(f"\n--- workflow attempt {attempt} {stream_name} ---\n".encode("utf-8"))


def file_size(path: Path) -> int:
    """Return the current file size, treating missing logs as empty."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def read_text_from_offset(path: Path, offset: int) -> str:
    """Read new text appended to a log after a recorded byte offset."""
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


async def append_stream_to_file(stream: asyncio.StreamReader, path: Path, on_chunk: Any | None = None) -> None:
    """Append a subprocess stream without using readline's 64 KiB record limit."""
    with path.open("ab") as handle:
        while True:
            chunk = await stream.read(16 * 1024)
            if not chunk:
                return
            handle.write(chunk)
            handle.flush()
            if on_chunk is not None:
                on_chunk()


async def terminate_process_group(proc: asyncio.subprocess.Process, *, grace_seconds: float = 3.0) -> None:
    """Terminate a worker process group and escalate if it does not exit."""
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
        return
    except asyncio.TimeoutError:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=grace_seconds)


def run_control(run_id: str) -> dict[str, Any]:
    """Return the current control flags for a run."""
    try:
        run = workflow_state.load_run(run_id)
    except (OSError, json.JSONDecodeError):
        return {}
    control = dict(run.get("control") or {})
    control["status"] = run.get("status", "")
    return control


def stop_requested(control: dict[str, Any]) -> bool:
    return bool(control.get("stop_requested")) or str(control.get("status", "")) == "cancelled"


async def poll_stop(run_id: str, interval: float = 1.0) -> None:
    """Return once a workflow stop is observed for the run."""
    while True:
        if stop_requested(run_control(run_id)):
            return
        await asyncio.sleep(interval)


async def wait_for_process_completion(
    run_id: str,
    proc: asyncio.subprocess.Process,
    stdout_task: asyncio.Task,
    stderr_task: asyncio.Task,
    wait_task: asyncio.Task,
    timeout: float | None,
) -> tuple[bool, bool, int | None]:
    """Race process completion against stop request and timeout.

    Returns (timed_out, stopped, exit_code). The process group is terminated
    on timeout or stop. The stop-poll task is always cancelled and joined.
    """
    poll_task = asyncio.create_task(poll_stop(run_id))
    worker_gather = asyncio.gather(stdout_task, stderr_task, wait_task)
    timed_out = False
    stopped = False
    try:
        if timeout is not None:
            first_done, _pending = await asyncio.wait_for(
                asyncio.wait({worker_gather, poll_task}, return_when=asyncio.FIRST_COMPLETED),
                timeout=timeout,
            )
        else:
            first_done, _pending = await asyncio.wait({worker_gather, poll_task}, return_when=asyncio.FIRST_COMPLETED)
        if poll_task in first_done:
            stopped = True
            await terminate_process_group(proc)
            worker_gather.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_gather
            return timed_out, stopped, None
        # Worker finished first: cancel poll task, drain worker, and return.
        poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poll_task
        await worker_gather
        return timed_out, stopped, wait_task.result()
    except asyncio.TimeoutError:
        timed_out = True
        await terminate_process_group(proc)
        worker_gather.cancel()
        poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_gather
        with contextlib.suppress(asyncio.CancelledError):
            await poll_task
        return timed_out, stopped, None
    finally:
        poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poll_task


async def wait_until_launch_allowed(run_id: str, agent_id: str) -> bool:
    """Block while paused, returning false if the run is stopped before launch."""
    marked_paused = False
    while True:
        control = run_control(run_id)
        if stop_requested(control):
            update_agent(run_id, agent_id, status="cancelled", summary="cancelled before worker launch", exit_code=130)
            return False
        if not control.get("paused") and control.get("status") != "paused":
            if marked_paused:
                update_agent(run_id, agent_id, status="pending", summary="resumed; waiting for worker slot")
            return True
        if not marked_paused:
            update_agent(run_id, agent_id, status="paused", summary="paused before worker launch")
            marked_paused = True
        await asyncio.sleep(test_interval_env("WORKFLOW_PAUSE_POLL_SECS", 1.0))


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
    proc: asyncio.subprocess.Process | None = None
    last_telemetry_update = 0.0

    def update_live_telemetry(force: bool = False, extra: dict[str, Any] | None = None) -> None:
        nonlocal last_telemetry_update
        now = time.monotonic()
        if not force and now - last_telemetry_update < 2.0:
            return
        telemetry_agent = dict(agent)
        if extra:
            telemetry_agent.update({key: value for key, value in extra.items() if value is not None})
        fields = telemetry_fields_for_agent(telemetry_agent)
        if fields:
            update_agent(run_id, agent["agent_id"], emit_event=False, **fields)
            last_telemetry_update = now

    if not await wait_until_launch_allowed(run_id, agent["agent_id"]):
        return
    async with semaphore:
        try:
            if not await wait_until_launch_allowed(run_id, agent["agent_id"]):
                return
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

            jsonl_path = Path(agent["jsonl_path"])
            stderr_path = Path(agent["log_path"])
            quota_retries = max(0, int(getattr(args, "quota_retries", 2)))
            failure_retries = max(0, int(getattr(args, "failure_retries", 0)))
            quota_retry_count = 0
            failure_retry_count = 0
            attempt = 0
            last_timed_out = False
            last_validation_error: str | None = None
            result_json: Any | None = None
            while True:
                if not await wait_until_launch_allowed(run_id, agent["agent_id"]):
                    return
                attempt += 1
                command = provider.build_command(agent, args)
                proc = await startup_limiter.create_process(command, cwd=args.cwd)
                if started_epoch is None:
                    started_epoch = time.time()
                if attempt > 1:
                    append_attempt_marker(jsonl_path, attempt, "stdout")
                    append_attempt_marker(stderr_path, attempt, "stderr")
                stdout_attempt_offset = file_size(jsonl_path)
                stderr_attempt_offset = file_size(stderr_path)
                update_agent(
                    run_id,
                    agent["agent_id"],
                    status="running",
                    summary="process started" if attempt == 1 else f"retry attempt {attempt} started",
                    started_epoch=round(started_epoch, 3),
                    command_preview=provider.preview_command(command, agent, args),
                    quota_retry_count=quota_retry_count,
                    failure_retry_count=failure_retry_count,
                    process_id=proc.pid,
                    process_group_id=proc.pid,
                    emit_event=True,
                )

                assert proc.stdout is not None
                assert proc.stderr is not None
                if proc.stdin is not None:
                    payload = provider.stdin_payload(agent, args)
                    if payload is not None:
                        proc.stdin.write(payload)
                        await proc.stdin.drain()
                    proc.stdin.close()

                timeout = getattr(args, "timeout_secs", None)
                stdout_task = asyncio.create_task(append_stream_to_file(proc.stdout, jsonl_path, update_live_telemetry))
                stderr_task = asyncio.create_task(append_stream_to_file(proc.stderr, stderr_path, update_live_telemetry))
                wait_task = asyncio.create_task(proc.wait())
                timed_out, stopped, exit_code = await wait_for_process_completion(
                    run_id, proc, stdout_task, stderr_task, wait_task, timeout
                )
                if stopped:
                    update_agent(
                        run_id,
                        agent["agent_id"],
                        status="cancelled",
                        summary="cancelled by workflow stop request",
                        exit_code=130,
                    )
                    return
                if timed_out:
                    last_timed_out = True
                proc = None
                stdout_text = read_text_from_offset(jsonl_path, stdout_attempt_offset)
                stderr_text = read_text_from_offset(stderr_path, stderr_attempt_offset)
                if timed_out:
                    exit_code = 124
                    stderr_text += f"\nworkflow timeout after {timeout}s\n"

                extracted = provider.extract_result(agent, exit_code, stdout_text=stdout_text, stderr_text=stderr_text)
                validation_error: str | None = None
                if exit_code == 0:
                    schema = agent.get("schema") or getattr(args, "result_schema_obj", None)
                    if schema is not None and jsonschema is not None:
                        try:
                            parsed = json.loads(extracted.result)
                            jsonschema.validate(parsed, schema)
                            result_json = parsed
                        except Exception as exc:
                            validation_error = f"{type(exc).__name__}: {exc}"
                            last_validation_error = validation_error
                            exit_code = 1
                            stderr_text += f"\nschema validation failed: {validation_error}\n"
                    elif schema is not None and jsonschema is None:
                        validation_error = "jsonschema package is not installed"
                        last_validation_error = validation_error
                        exit_code = 1
                        stderr_text += f"\nschema validation failed: {validation_error}\n"

                if exit_code != 0 and quota_retry_count < quota_retries and quota_limit_detected(stdout_text, stderr_text):
                    quota_retry_count += 1
                    sleep_seconds = quota_retry_sleep_seconds(args)
                    update_agent(
                        run_id,
                        agent["agent_id"],
                        status="running",
                        summary=f"quota limit detected; retry {quota_retry_count}/{quota_retries} after next :00/:30 window",
                        exit_code=exit_code,
                        quota_retry_count=quota_retry_count,
                        quota_sleep_seconds=round(sleep_seconds, 3),
                        emit_event=True,
                    )
                    if not await sleep_until_quota_retry_allowed(run_id, agent["agent_id"], sleep_seconds):
                        return
                    continue
                if exit_code != 0 and failure_retry_count < failure_retries:
                    failure_retry_count += 1
                    if validation_error:
                        prompt_hint = (
                            f"\n\nPrior attempt failed schema validation: {validation_error}\n"
                            "Correct the output shape and try again."
                        )
                        agent["prompt"] = agent.get("prompt", "") + prompt_hint
                        prompt_path = agent.get("prompt_file")
                        if prompt_path:
                            Path(prompt_path).write_text(agent["prompt"] + "\n", encoding="utf-8")
                    update_agent(
                        run_id,
                        agent["agent_id"],
                        status="running",
                        summary=(
                            f"schema validation failed; retry {failure_retry_count}/{failure_retries}"
                            if validation_error
                            else f"worker failed; retry {failure_retry_count}/{failure_retries}"
                        ),
                        exit_code=exit_code,
                        failure_retry_count=failure_retry_count,
                        emit_event=True,
                    )
                    await asyncio.sleep(test_interval_env("WORKFLOW_FAILURE_RETRY_SLEEP_SECS", 1.0))
                    continue
                break

            status = "completed" if exit_code == 0 else "failed"
            summary = extracted.summary
            result = extracted.result
            if last_validation_error and status == "failed":
                summary = "schema validation failed"
                result = f"schema validation failed: {last_validation_error}"
            if last_timed_out and status == "failed":
                timeout_message = f"workflow timeout after {timeout}s"
                summary = timeout_message
                result = result or timeout_message
            if stop_requested(run_control(run_id)):
                status = "cancelled"
                summary = summary or "cancelled by workflow stop request"
            update_fields: dict[str, Any] = {
                "status": status,
                "result": result,
                "summary": summary,
                "exit_code": exit_code,
                "thread_id": extracted.thread_id,
                "jsonl_path": extracted.jsonl_path,
                "log_path": extracted.log_path,
                "output_path": extracted.output_path,
            }
            if result_json is not None:
                update_fields["result_json"] = result_json
            update_agent(
                run_id,
                agent["agent_id"],
                **update_fields,
                **telemetry_fields_for_agent(
                    {
                        **agent,
                        "jsonl_path": extracted.jsonl_path or agent.get("jsonl_path"),
                        "log_path": extracted.log_path or agent.get("log_path"),
                        "output_path": extracted.output_path or agent.get("output_path"),
                    }
                ),
                **timing_fields(started_epoch),
            )
        except Exception as exc:
            if proc is not None:
                await terminate_process_group(proc)
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


def _depends_on_names(agent: dict[str, Any]) -> set[str]:
    """Return the set of dependency names an agent is waiting for."""
    raw = agent.get("depends_on") or ""
    if isinstance(raw, list):
        return {str(value).strip() for value in raw if str(value).strip()}
    return {piece.strip() for piece in str(raw).split(",") if piece.strip()}


def _resolve_schema(value: Any) -> dict[str, Any] | None:
    """Resolve a schema value to a JSON schema dict.

    Accepts an inline dict, a JSON string, or a path to a JSON schema file.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    text = str(value).strip()
    if text.startswith("{"):
        return json.loads(text)
    path = Path(text).expanduser()
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    raise SystemExit(f"schema not found or invalid: {value}")


def _normalize_expansion_job(item: Any, index: int) -> dict[str, str] | None:
    """Validate and normalize one job from a workflow-expansion envelope."""
    if not isinstance(item, dict):
        return None
    prompt = str(item.get("prompt") or "").strip()
    if not prompt:
        return None
    raw_name = str(item.get("name") or item.get("role") or f"job-{index + 1}")
    name = workflow_state.slugify(raw_name, fallback=f"job-{index + 1}")
    return {
        "name": name,
        "role": str(item.get("role") or raw_name).strip() or name,
        "prompt": prompt,
        "stage": str(item.get("stage") or "").strip(),
        "depends_on": item.get("depends_on") or "",
    }


def _parse_expansion_jobs(text: str) -> list[dict[str, str]]:
    """Extract jobs from a ``workflow-expansion`` envelope in worker output."""
    if not text:
        return []
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    if data.get("kind") != "workflow-expansion" or data.get("schema_version") != 1:
        return []
    jobs = data.get("jobs", [])
    if not isinstance(jobs, list):
        return []
    return [job for job in (_normalize_expansion_job(item, index) for index, item in enumerate(jobs)) if job is not None]


def _add_expansion_agent(
    run_id: str,
    job: dict[str, str],
    args: argparse.Namespace,
    provider: RunnerProvider,
    index: int,
    phase_id: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Create a new agent from an expansion job and persist it to state."""
    prefix = workflow_state.slugify(provider.name, fallback="worker")
    agent_id = f"{prefix}-{index + 1:02d}-{workflow_state.slugify(job['name'])}"
    run = workflow_state.load_run(run_id)
    artifacts = Path(run["paths"]["artifacts_dir"])
    logs = Path(run["paths"]["logs_dir"])
    prompt_path = artifacts / f"{agent_id}.prompt.md"
    jsonl_path = logs / f"{agent_id}.jsonl"
    stderr_path = logs / f"{agent_id}.stderr.log"
    output_path = artifacts / f"{agent_id}.final.md"
    prompt_path.write_text(job["prompt"] + "\n", encoding="utf-8")

    agent_args = argparse.Namespace(
        run=run_id,
        phase=phase_id or PHASE_ID,
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
    extra: dict[str, Any] = {}
    stage = job.get("stage", "")
    depends_on = job.get("depends_on", "")
    if stage:
        extra["stage"] = stage
    if depends_on:
        extra["depends_on"] = depends_on
    if job.get("schema"):
        extra["schema"] = _resolve_schema(job["schema"])
    if extra:
        update_agent(run_id, agent_id, **extra)
    return workflow_state.load_run(run_id), agent_id


def _record_expansion(run_id: str, parent_agent: dict[str, Any], added: int, round_num: int) -> None:
    def mutator(data: dict[str, Any]) -> None:
        decision_id = f"dec-expansion-{parent_agent['agent_id']}-r{round_num}"
        decision = {
            "decision_id": decision_id,
            "ts": workflow_state.now(),
            "title": f"Workflow expansion from {parent_agent['name']}",
            "rationale": f"Worker output contained a workflow-expansion envelope; added {added} job(s) at round {round_num}.",
            "made_by": "workflow_run.run_all",
        }
        data.setdefault("decisions", []).append(decision)
        workflow_state.add_event(
            data,
            "info",
            f"workflow expansion: {parent_agent['name']} added {added} job(s) at round {round_num}",
            kind="expansion",
            operation="added",
            source="workflow_run.run_all",
            agent_id=parent_agent["agent_id"],
            phase_id=parent_agent.get("phase_id"),
            data={"added": added, "round": round_num, "parent_name": parent_agent.get("name", "")},
        )

    workflow_state.mutate_run(run_id, mutator)


def _record_expansion_truncation(run_id: str, parent_agent: dict[str, Any], cap: str, dropped: int, round_num: int) -> None:
    def mutator(data: dict[str, Any]) -> None:
        workflow_state.add_event(
            data,
            "warning",
            f"workflow expansion truncated by {cap}: {dropped} job(s) from {parent_agent['name']} at round {round_num}",
            kind="expansion",
            operation="truncated",
            source="workflow_run.run_all",
            agent_id=parent_agent["agent_id"],
            phase_id=parent_agent.get("phase_id"),
            data={"cap": cap, "dropped": dropped, "round": round_num, "parent_name": parent_agent.get("name", "")},
        )

    workflow_state.mutate_run(run_id, mutator)


def _record_unmet_dependencies(run_id: str, pending: dict[str, tuple[dict[str, Any], int]]) -> None:
    if not pending:
        return

    def mutator(data: dict[str, Any]) -> None:
        for agent, _round in pending.values():
            state_agent = workflow_state.find_item(data.setdefault("agents", []), "agent_id", agent["agent_id"])
            deps = ", ".join(sorted(_depends_on_names(state_agent)))
            message = f"dependency never satisfied: {deps}"
            state_agent["status"] = "failed"
            state_agent["summary"] = message
            state_agent["result"] = message
            state_agent["exit_code"] = 1
            state_agent["completed_at"] = workflow_state.now()
            state_agent["updated_at"] = workflow_state.now()
            workflow_state.add_event(
                data,
                "warning",
                f"worker {state_agent['name']} failed: {message}",
                kind="agent",
                operation="updated",
                source="workflow_run.run_all",
                agent_id=state_agent["agent_id"],
                phase_id=state_agent.get("phase_id"),
                data={"name": state_agent.get("name", ""), "status": "failed", "unmet_dependencies": deps},
            )

    workflow_state.mutate_run(run_id, mutator)


def _phase_status_from_agents(agents: list[dict[str, Any]], run_status: str) -> str:
    """Derive one phase status from its attached worker agents."""
    if not agents:
        return run_status
    if any(agent.get("status") == "cancelled" for agent in agents):
        return "cancelled"
    if any(agent.get("status") == "failed" for agent in agents):
        return "failed"
    if any(agent.get("status") in {"pending", "running", "paused"} for agent in agents):
        return "running"
    return "completed"


def update_worker_phases(final: dict[str, Any], provider: RunnerProvider) -> None:
    """Refresh every worker-owning phase after a runner finishes."""
    phases = final.setdefault("phases", [])
    agents = final.get("agents", [])
    timestamp = workflow_state.now()
    for phase in phases:
        phase_agents = [agent for agent in agents if agent.get("phase_id") == phase.get("phase_id")]
        if not phase_agents:
            continue
        phase_status = _phase_status_from_agents(phase_agents, final["status"])
        phase["status"] = phase_status
        phase["completed_at"] = timestamp if phase_status in {"completed", "failed", "cancelled"} else None
        workflow_state.add_event(
            final,
            "info",
            f"{provider.name} worker phase {phase_status}",
            kind="phase",
            operation="updated",
            source="workflow_run.run_all",
            phase_id=phase["phase_id"],
            data={"status": phase_status, "runner": provider.name},
        )


async def run_all(run: dict[str, Any], args: argparse.Namespace, provider: RunnerProvider) -> str:
    """Run a dynamic worker pool that accepts new agents mid-flight."""
    run_id = run["run_id"]
    semaphore = asyncio.Semaphore(args.max_agents)
    startup_limiter = StartupRateLimiter(args.startup_delay)
    max_round = getattr(args, "max_round", 3)
    max_job = getattr(args, "max_job", None)

    queue: asyncio.Queue[tuple[dict[str, Any], int] | None] = asyncio.Queue()
    pending_by_id: dict[str, tuple[dict[str, Any], int]] = {}
    completed_names: set[str] = set()
    active_workers = 0
    next_index = len(run.get("agents", []))
    total_jobs = 0
    state_lock = asyncio.Lock()
    shutdown_sent = False

    def enqueue_agent(agent: dict[str, Any], round_num: int) -> None:
        deps = _depends_on_names(agent) - completed_names
        if deps:
            pending_by_id[agent["agent_id"]] = (agent, round_num)
        else:
            queue.put_nowait((agent, round_num))

    def release_pending() -> None:
        ready = []
        for agent_id, (pending_agent, pending_round) in list(pending_by_id.items()):
            if not (_depends_on_names(pending_agent) - completed_names):
                ready.append(agent_id)
        for agent_id in ready:
            pending_agent, pending_round = pending_by_id.pop(agent_id)
            queue.put_nowait((pending_agent, pending_round))

    async def worker() -> None:
        nonlocal active_workers, next_index, total_jobs, shutdown_sent
        while True:
            item = await queue.get()
            if item is None:
                queue.task_done()
                return
            agent, round_num = item
            async with state_lock:
                active_workers += 1
            try:
                await run_worker(run_id, agent, args, provider, semaphore, startup_limiter)
                run_snapshot = workflow_state.load_run(run_id)
                updated_agent = workflow_state.find_item(run_snapshot["agents"], "agent_id", agent["agent_id"])
                async with state_lock:
                    status = updated_agent.get("status")
                    if status == "completed":
                        completed_names.add(updated_agent["name"])
                        expansion_jobs = _parse_expansion_jobs(updated_agent.get("result") or "")
                        added = 0
                        dropped = 0
                        for job in expansion_jobs:
                            if max_job is not None and total_jobs >= max_job:
                                dropped = len(expansion_jobs) - added
                                _record_expansion_truncation(run_id, updated_agent, "max-job", dropped, round_num + 1)
                                break
                            if round_num + 1 > max_round:
                                dropped = len(expansion_jobs) - added
                                _record_expansion_truncation(run_id, updated_agent, "max-round", dropped, round_num + 1)
                                break
                            total_jobs += 1
                            _run, new_agent_id = _add_expansion_agent(run_id, job, args, provider, next_index, phase_id=updated_agent.get("phase_id"))
                            next_index += 1
                            new_agent = workflow_state.find_item(_run["agents"], "agent_id", new_agent_id)
                            enqueue_agent(new_agent, round_num + 1)
                            added += 1
                        if added:
                            _record_expansion(run_id, updated_agent, added, round_num + 1)
                    release_pending()
            except Exception as exc:
                run_snapshot = workflow_state.load_run(run_id)
                updated_agent = workflow_state.find_item(run_snapshot["agents"], "agent_id", agent["agent_id"])
                async with state_lock:
                    if updated_agent.get("status") in {"pending", "running"}:
                        message = f"{type(exc).__name__}: {exc}"
                        update_agent(
                            run_id,
                            agent["agent_id"],
                            status="failed",
                            result=message,
                            summary=message,
                            exit_code=1,
                        )
                    release_pending()
            finally:
                async with state_lock:
                    active_workers -= 1
                    should_shutdown = active_workers == 0 and queue.empty() and not shutdown_sent
                    if should_shutdown:
                        shutdown_sent = True
                        for _ in range(args.max_agents):
                            queue.put_nowait(None)
                queue.task_done()

    for agent in run.get("agents", []):
        total_jobs += 1
        enqueue_agent(agent, 0)
    release_pending()

    workers = [asyncio.create_task(worker()) for _ in range(args.max_agents)]
    await asyncio.gather(*workers, return_exceptions=True)

    _record_unmet_dependencies(run_id, pending_by_id)

    def mutator(final: dict[str, Any]) -> str:
        failed = [agent for agent in final.get("agents", []) if agent.get("status") == "failed"]
        cancelled = [agent for agent in final.get("agents", []) if agent.get("status") == "cancelled"]
        pending = [agent for agent in final.get("agents", []) if agent.get("status") in {"pending", "running", "paused"}]
        if final.get("control", {}).get("stop_requested") or cancelled:
            final["status"] = "cancelled"
        else:
            final["status"] = "failed" if failed else "completed"
        if pending:
            final["status"] = "running"
        record_worker_artifacts(final)
        update_worker_phases(final, provider)
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


def nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


def nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative number")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title")
    parser.add_argument("--run", "--attach-run", dest="attach_run", help="attach workers to an existing workflow run")
    parser.add_argument("--parent-run", help="record a newly created run as a child of this existing workflow run")
    parser.add_argument("--prompt")
    parser.add_argument("--prompt-file")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--job", action="append", help="name::prompt; repeat for multiple workers")
    parser.add_argument("--jobs-file", help="JSON array of {name, role, prompt}")
    parser.add_argument("--tag", action="append")
    parser.add_argument(
        "--runner",
        default="codex-direct",
        choices=["codex-direct", "ccc-codex", "ccc-opencode", "ccc", "opencode-direct", "kimi-direct"],
        help="coding CLI provider to use for worker processes",
    )
    parser.add_argument("--ccc-runner", help="ccc target for --runner ccc: a CLI selector like kimi or opencode, or a preset like @mm")
    parser.add_argument("--ccc-control", action="append", help="extra ccc control token before the prompt; repeatable, e.g. @reviewer or +3")
    parser.add_argument("--ccc-output-mode", default="stream-json", choices=["formatted", "stream-formatted", "text", "stream-text", "json", "stream-json", "pass-text", "pass-json"])
    parser.add_argument("--permission-mode", choices=["safe", "auto", "yolo", "plan"], help="forwarded to ccc providers")
    parser.add_argument("--cli-agent", help="direct OpenCode agent name for --runner opencode-direct")
    parser.add_argument("--timeout-secs", type=positive_int, help="forwarded to ccc providers")
    parser.add_argument("--quota-retries", type=nonnegative_int, default=2, help="quota/rate-limit retries; default: 2")
    parser.add_argument("--quota-retry-buffer-secs", type=nonnegative_float, default=5.0, help="seconds added after the next :00/:30 retry window; default: 5.0")
    parser.add_argument("--failure-retries", type=nonnegative_int, default=0, help="non-quota worker retries; default: 0")
    parser.add_argument("--result-schema", help="path to a JSON schema file applied to worker output")
    parser.add_argument("--kimi-max-steps-per-turn", type=positive_int, default=9999, help="Kimi max steps/tool calls per turn; default: 9999")
    parser.add_argument("--model")
    parser.add_argument("--sandbox", default="read-only", choices=["read-only", "workspace-write", "danger-full-access"])
    parser.add_argument("--approval", default="never", choices=["never", "on-request", "untrusted", "on-failure"])
    parser.add_argument("--max-agents", "--concurrency", dest="max_agents", type=positive_int, default=4, help="maximum worker processes running at once; default: 4")
    parser.add_argument("--max-round", type=positive_int, default=3, help="maximum expansion round depth; default: 3")
    parser.add_argument("--max-job", type=positive_int, default=None, help="maximum total jobs including expansions; default: unlimited")
    parser.add_argument("--startup-delay", type=nonnegative_float, default=1.0, help="minimum seconds between worker starts; default: 1.0")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--mock", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not args.attach_run and not args.title:
        raise SystemExit("--title is required unless --run/--attach-run is supplied")
    if args.attach_run and args.parent_run:
        raise SystemExit("--parent-run only applies when creating a new run")
    args.cwd = str(Path(args.cwd).expanduser().resolve())
    if args.result_schema:
        args.result_schema_obj = _resolve_schema(args.result_schema)
    else:
        args.result_schema_obj = None
    jobs = load_jobs(args)
    provider = build_provider(args)
    run = attach_run(args, jobs, provider) if args.attach_run else create_run(args, jobs, provider)
    for index, job in enumerate(jobs):
        run = add_agent(run, job, args, provider, index, stage=job.get("stage", ""), depends_on=job.get("depends_on", ""))
    print(json.dumps({"run_id": run["run_id"], "path": run["paths"]["run_json"], "jobs": len(jobs)}, indent=2))
    if args.dry_run:
        print("dry run: workers were recorded but not launched")
    else:
        replay = ["python3", __file__, "--runner", args.runner]
        if args.runner == "ccc" and args.ccc_runner:
            replay.extend(["--ccc-runner", args.ccc_runner])
        if args.attach_run:
            replay.extend(["--attach-run", args.attach_run, "..."])
        else:
            replay.extend(["--title", args.title, "..."])
        print("command:", shlex.join(replay))
    status = asyncio.run(run_all(workflow_state.load_run(run["run_id"]), args, provider))
    if status != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()

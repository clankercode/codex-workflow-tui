"""Runner provider implementations for the workflow worker runner."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import workflow_state


# ---------------------------------------------------------------------------
# Shared constants and utilities
# ---------------------------------------------------------------------------

CCC_FOOTER_RE = re.compile(r"^>> ccc:output-log >> (.+)$", re.MULTILINE)
KIMI_RESUME_RE = re.compile(r"\bkimi\s+-r\s+(\S+)")


@dataclass(frozen=True)
class WorkerResult:
    """Normalized result extracted from one coding-CLI worker run."""

    result: str
    summary: str
    thread_id: str = ""
    jsonl_path: str | None = None
    log_path: str | None = None
    output_path: str | None = None


def first_line(text: str) -> str:
    for line in text.splitlines():
        clean = line.strip()
        if clean:
            return clean[:240]
    return ""


def command_preview(command: list[str]) -> str:
    preview = list(command)
    if preview:
        preview[-1] = "<prompt>"
    return shlex.join(preview)


def agent_cwd(agent: dict[str, Any], args: argparse.Namespace) -> str:
    """Return the effective working directory for one worker agent."""
    return str(agent.get("cwd") or args.cwd)


# ---------------------------------------------------------------------------
# Provider base class
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Concrete providers
# ---------------------------------------------------------------------------


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
            agent_cwd(agent, args),
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
            agent_cwd(agent, args),
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
            agent_cwd(agent, args),
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
        stderr_text_resolved = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
        match = KIMI_RESUME_RE.search(stderr_text_resolved)
        Path(agent["output_path"]).write_text(final_message, encoding="utf-8")
        return WorkerResult(
            result=final_message,
            summary=first_line(final_message) or f"kimi exited {exit_code}",
            thread_id=match.group(1) if match else "",
        )


class PiDirectProvider(RunnerProvider):
    """Use pi directly through `pi -p --mode json`."""

    def __init__(self) -> None:
        super().__init__("pi-direct", "pi")

    def build_command(self, agent: dict[str, Any], args: argparse.Namespace) -> list[str]:
        command = [
            "pi",
            "-p",
            "--mode",
            "json",
            "--approve",
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
        session_id = ""
        # Try parsing JSON output from pi
        if stdout_text:
            try:
                data = json.loads(stdout_text)
                if isinstance(data, dict):
                    final_message = str(data.get("response") or data.get("text") or data.get("content") or "")
                    session_id = str(data.get("session_id") or data.get("sessionId") or "")
                elif isinstance(data, str):
                    final_message = data
            except json.JSONDecodeError:
                # Plain text output
                final_message = stdout_text.strip()
        # Fallback: read output file
        if not final_message:
            output_path = Path(agent["output_path"])
            final_message = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
        Path(agent["output_path"]).write_text(final_message, encoding="utf-8")
        return WorkerResult(
            result=final_message,
            summary=first_line(final_message) or f"pi exited {exit_code}",
            thread_id=session_id,
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
            command.extend(["--runner-arg", "--dir", "--runner-arg", agent_cwd(agent, args)])
        elif self.is_kimi_selector():
            command.extend(["--runner-arg", "--work-dir", "--runner-arg", agent_cwd(agent, args)])
        elif self.is_codex_selector():
            command.extend(["--runner-arg", "--cd", "--runner-arg", agent_cwd(agent, args)])
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


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------


def build_provider(args: argparse.Namespace) -> RunnerProvider:
    if args.runner == "codex-direct":
        return CodexDirectProvider()
    if args.runner == "opencode-direct":
        return OpencodeDirectProvider()
    if args.runner == "kimi-direct":
        return KimiDirectProvider()
    if args.runner == "pi-direct":
        return PiDirectProvider()
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

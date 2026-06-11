#!/usr/bin/env python3
"""Drive the workflow TUI in tmux and capture a single visual QA log."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

DEFAULT_ACTIONS = [
    ("capture", "initial-runs"),
    ("send", "Right"),
    ("capture", "phases"),
    ("send", "Down"),
    ("capture", "phase-next"),
    ("send", "Right"),
    ("capture", "agents-phase"),
    ("send", "a"),
    ("capture", "agents-all"),
    ("send", "v"),
    ("capture", "agent-prompt"),
    ("send", "y"),
    ("capture", "copy-id"),
    ("send", "p"),
    ("capture", "copy-path"),
    ("send", "Right"),
    ("capture", "events"),
    ("send", "Right"),
    ("capture", "decisions"),
    ("send", "Right"),
    ("capture", "artifacts"),
]

EXPECTED_CAPTURE_TEXT = {
    "initial-runs": ("Agent Workflows", "Workflow With Parallel Review", "Live Stats"),
    "phases": ("phase-research", "Research", "No agents for this phase"),
    "phase-next": ("phase-review", "Security reviewer", "Test coverage reviewer"),
    "agents-phase": ("Agents: Review", "agent-security", "Live Output"),
    "agents-all": ("Synthesis writer", "Test coverage reviewer"),
    "agent-prompt": ("Prompt", "Review authentication and secret handling."),
    "copy-id": ("Copied agent_id",),
    "copy-path": ("Copied agent path",),
    "events": ("evt-synthesis", "artifact recorded"),
    "decisions": ("dec-default", "Default workers to read-only"),
    "artifacts": ("art-report", "Final report", "final-report.md"),
}
FAILURE_TEXT = ("Traceback", "terminal too small", "No rows")
ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def workflow_root() -> Path:
    return Path(__file__).resolve().parents[3]


def default_fixture() -> Path:
    return Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "rich-workflow.json"


def default_output_dir() -> Path:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return Path(tempfile.gettempdir()) / f"workflow-tui-qa-{stamp}"


def load_fixture(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "runs" in data:
        data = data["runs"]
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise SystemExit("fixture must be a run object, {runs: [...]}, or a run array")
    return [run for run in data if isinstance(run, dict)]


def prepare_state(fixture: Path, output_dir: Path) -> Path:
    state_dir = output_dir / "state"
    runs_dir = state_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    for index, run in enumerate(load_fixture(fixture)):
        run_id = str(run.get("run_id") or f"wf-fixture-{index}")
        run_dir = runs_dir / run_id
        artifacts_dir = run_dir / "artifacts"
        logs_dir = run_dir / "logs"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)
        copied = dict(run)
        copied.pop("_fixture_dir", None)
        copied["paths"] = dict(copied.get("paths", {}))
        copied["paths"].update(
            {
                "run_dir": str(run_dir),
                "run_json": str(run_dir / "run.json"),
                "artifacts_dir": str(artifacts_dir),
                "logs_dir": str(logs_dir),
            }
        )
        (run_dir / "run.json").write_text(json.dumps(copied, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return state_dir


def run(command: list[str], *, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, env=env, check=check)


def action_plan_lines(session: str, actions: list[tuple[str, str]]) -> list[str]:
    lines = [f"session {session}"]
    for kind, value in actions:
        if kind == "send":
            lines.append(f"send-key {value}")
        else:
            lines.append(f"capture {value}")
    return lines


def capture(session: str) -> str:
    result = run(["tmux", "capture-pane", "-pt", session, "-e", "-S", "-"], check=True)
    return result.stdout.rstrip() + "\n"


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def append_capture(log: Path, label: str, session: str) -> str:
    screen = capture(session)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"\n===== {label} =====\n")
        handle.write(screen)
    return screen


def assert_capture(label: str, screen: str) -> None:
    plain = strip_ansi(screen)
    missing = [text for text in EXPECTED_CAPTURE_TEXT.get(label, ()) if text not in plain]
    failures = [text for text in FAILURE_TEXT if text in plain]
    if missing or failures:
        details = []
        if missing:
            details.append(f"missing expected text for {label}: {', '.join(missing)}")
        if failures:
            details.append(f"unexpected failure text for {label}: {', '.join(failures)}")
        raise SystemExit("; ".join(details))


def wait_for_paint(session: str, timeout: float = 5.0) -> None:
    """Wait until the live TUI has painted its first useful frame."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if "Agent Workflows" in capture(session):
            return
        time.sleep(0.1)


def drive(args: argparse.Namespace) -> Path:
    if shutil.which("tmux") is None:
        raise SystemExit("tmux is required for interactive TUI QA")
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    state_dir = prepare_state(Path(args.fixture).expanduser().resolve(), output_dir)
    log = output_dir / "workflow-tui-tmux.log"
    env = os.environ.copy()
    env["WORKFLOW_STATE_DIR"] = str(state_dir)
    env["TZ"] = args.timezone
    command = f"env WORKFLOW_STATE_DIR={shlex_quote(str(state_dir))} TZ={shlex_quote(args.timezone)} workflow tui"
    run(["tmux", "kill-session", "-t", args.session], check=False)
    run(["tmux", "new-session", "-d", "-s", args.session, "-x", str(args.width), "-y", str(args.height), command], env=env)
    try:
        time.sleep(args.settle)
        run(["tmux", "send-keys", "-t", args.session, "r"], check=False)
        time.sleep(args.delay)
        wait_for_paint(args.session)
        log.write_text(
            "\n".join(
                [
                    f"workflow TUI tmux QA",
                    f"session: {args.session}",
                    f"fixture: {Path(args.fixture).expanduser().resolve()}",
                    f"state: {state_dir}",
                    f"size: {args.width}x{args.height}",
                    "",
                    "Actions:",
                    *action_plan_lines(args.session, DEFAULT_ACTIONS),
                    "",
                ]
            ),
            encoding="utf-8",
        )
        for kind, value in DEFAULT_ACTIONS:
            if kind == "send":
                run(["tmux", "send-keys", "-t", args.session, value])
                time.sleep(args.copy_delay if value in {"y", "p"} else args.delay)
            else:
                screen = append_capture(log, value, args.session)
                if not args.no_assertions:
                    assert_capture(value, screen)
        return log
    finally:
        if not args.keep_session:
            run(["tmux", "send-keys", "-t", args.session, "q"], check=False)
            time.sleep(0.1)
            run(["tmux", "kill-session", "-t", args.session], check=False)


def shlex_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", default=str(default_fixture()))
    parser.add_argument("--output-dir", default=str(default_output_dir()))
    parser.add_argument("--session", default="workflow-tui-qa")
    parser.add_argument("--width", type=int, default=120)
    parser.add_argument("--height", type=int, default=36)
    parser.add_argument("--timezone", default="Australia/Sydney")
    parser.add_argument("--settle", type=float, default=1.0)
    parser.add_argument("--delay", type=float, default=0.9)
    parser.add_argument("--copy-delay", type=float, default=0.15)
    parser.add_argument("--keep-session", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-assertions", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.dry_run:
        print("\n".join(action_plan_lines(args.session, DEFAULT_ACTIONS)))
        return
    log = drive(args)
    print(log)


if __name__ == "__main__":
    main()

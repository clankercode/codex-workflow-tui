#!/usr/bin/env python3
"""Tests for the workflow skill scripts and TUI snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock
import argparse
import contextlib
import io
import textwrap
import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FIXTURE = ROOT / "tests" / "fixtures" / "rich-workflow.json"
MANY_FIXTURE = ROOT / "tests" / "fixtures" / "many-rows.json"
E2E_FIXTURE = ROOT / "tests" / "fixtures" / "e2e-workflow" / "run.json"
SNAPSHOTS = ROOT / "tests" / "snapshots"


def slow_test(func: Any) -> Any:
    """Skip integration/timing-heavy tests when WORKFLOW_TEST_MODE=fast."""
    return unittest.skipIf(os.environ.get("WORKFLOW_TEST_MODE") == "fast", "slow workflow integration test")(func)


class WorkflowScriptTests(unittest.TestCase):
    """Verify workflow state, worker mocking, and snapshot rendering."""

    def run_script(self, script: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, str(SCRIPTS / script), *args]
        return subprocess.run(command, check=True, text=True, capture_output=True, env=env)

    def run_wf(self, *args: str, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run the shell entrypoint exactly as users do."""
        command = [str(SCRIPTS / "wf"), *args]
        return subprocess.run(command, check=check, text=True, capture_output=True, env=env)

    def git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        """Run git in tests with local-only repositories and stable author config."""
        command = [
            "git",
            "-C",
            str(cwd),
            "-c",
            "user.name=Workflow Test",
            "-c",
            "user.email=workflow-test@example.invalid",
            *args,
        ]
        return subprocess.run(command, check=True, text=True, capture_output=True)

    def write_commit(self, cwd: Path, text: str) -> str:
        """Create one deterministic commit in a temporary git repository."""
        note = cwd / "note.txt"
        note.write_text(text, encoding="utf-8")
        self.git(cwd, "add", "note.txt")
        self.git(cwd, "commit", "-m", text)
        return self.git(cwd, "rev-parse", "HEAD").stdout.strip()

    def make_update_repos(self, tmp_path: Path) -> tuple[Path, Path, str]:
        """Create a local origin, source checkout, and skill checkout for update tests."""
        origin = tmp_path / "origin.git"
        source = tmp_path / "source"
        skill = tmp_path / "skill"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, text=True, capture_output=True)
        subprocess.run(["git", "clone", str(origin), str(source)], check=True, text=True, capture_output=True)
        first_head = self.write_commit(source, "initial")
        self.git(source, "push", "-u", "origin", "main")
        subprocess.run(["git", "clone", str(origin), str(skill)], check=True, text=True, capture_output=True)
        return source, skill, first_head

    def parse_apply_summary(self, result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
        """Extract the JSON summary from workflow apply stdout robustly."""
        # Try parsing the full stdout first (handles pretty-printed JSON)
        text = result.stdout.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Try parsing up to the first non-JSON line
        for i, line in enumerate(text.splitlines()):
            if line.strip().startswith(("command:", "dry run:", "error:")):
                chunk = "\n".join(text.splitlines()[:i])
                try:
                    return json.loads(chunk)
                except json.JSONDecodeError:
                    pass
        self.fail(f"no JSON summary in apply stdout: {result.stdout[:300]}")

    def snapshot_env(self) -> dict[str, str]:
        """Return a deterministic local timezone and clock for TUI snapshots."""
        env = os.environ.copy()
        env["TZ"] = "Australia/Sydney"
        env["WORKFLOW_TUI_SNAPSHOT_NOW"] = "2026-06-11T00:06:00Z"
        return env

    def fast_timing_env(self, env: dict[str, str]) -> dict[str, str]:
        """Use shorter test-only sleeps while preserving production defaults."""
        env.setdefault("WORKFLOW_PAUSE_POLL_SECS", "0.02")
        env.setdefault("WORKFLOW_FAILURE_RETRY_SLEEP_SECS", "0")
        env.setdefault("WORKFLOW_QUOTA_RETRY_POLL_SECS", "0.02")
        return env

    def install_timed_fake_ccc(self, fake_bin: Path) -> Path:
        """Install a fake ccc binary that records starts, stops, args, and artifacts."""
        fake_bin.mkdir(parents=True, exist_ok=True)
        fake_ccc = fake_bin / "ccc"
        fake_ccc.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import fcntl
                import json
                import os
                import sys
                import time
                from pathlib import Path

                events_path = Path(os.environ["CCC_FAKE_EVENTS"])
                active_path = Path(os.environ["CCC_FAKE_ACTIVE"])
                max_path = Path(os.environ["CCC_FAKE_MAX"])
                lock_path = Path(os.environ["CCC_FAKE_LOCK"])
                run_root = Path(os.environ["CCC_FAKE_RUN_ROOT"])
                sleep_seconds = float(os.environ.get("CCC_FAKE_SLEEP", "0"))
                events_path.parent.mkdir(parents=True, exist_ok=True)
                run_root.mkdir(parents=True, exist_ok=True)

                def update(kind):
                    with lock_path.open("w", encoding="utf-8") as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX)
                        active = int(active_path.read_text(encoding="utf-8")) if active_path.exists() else 0
                        active = active + 1 if kind == "start" else active - 1
                        active_path.write_text(str(active), encoding="utf-8")
                        high = int(max_path.read_text(encoding="utf-8")) if max_path.exists() else 0
                        max_path.write_text(str(max(high, active)), encoding="utf-8")
                        with events_path.open("a", encoding="utf-8") as handle:
                            handle.write(json.dumps({
                                "event": kind,
                                "time": time.monotonic(),
                                "active": active,
                                "args": sys.argv[1:],
                            }) + "\\n")
                        fcntl.flock(lock, fcntl.LOCK_UN)

                update("start")
                try:
                    time.sleep(sleep_seconds)
                    run_dir = run_root / f"ccc-{os.getpid()}"
                    run_dir.mkdir(parents=True, exist_ok=True)
                    output = "fake ccc result for " + " ".join(sys.argv[1:]) + "\\n"
                    (run_dir / "output.txt").write_text(output, encoding="utf-8")
                    (run_dir / "transcript.txt").write_text("[assistant] " + output, encoding="utf-8")
                    sys.stdout.write("[assistant] " + output)
                    sys.stderr.write(f">> ccc:output-log >> {run_dir}\\n")
                finally:
                    update("stop")
                """
            ),
            encoding="utf-8",
        )
        fake_ccc.chmod(0o755)
        return fake_ccc

    def install_fake_codex(self, fake_bin: Path) -> Path:
        """Install a fake codex binary that returns deterministic JSONL output."""
        fake_bin.mkdir(parents=True, exist_ok=True)
        fake_codex = fake_bin / "codex"
        fake_codex.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                import sys
                from pathlib import Path

                state_path = os.environ.get("CODEX_FAKE_STATE")
                args_path = os.environ.get("CODEX_FAKE_ARGS")
                if args_path:
                    Path(args_path).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
                attempt = 0
                if state_path:
                    path = Path(state_path)
                    if path.exists():
                        attempt = int(path.read_text(encoding="utf-8").strip() or "0")
                    path.write_text(str(attempt + 1), encoding="utf-8")
                output = os.environ.get(f"CODEX_FAKE_OUTPUT_{attempt}") or os.environ.get("CODEX_FAKE_OUTPUT", "fake codex result")
                exit_code = int(os.environ.get("CODEX_FAKE_EXIT_CODE", "0"))
                event = {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": output},
                }
                sys.stdout.write(json.dumps(event) + "\\n")
                sys.exit(exit_code)
                """
            ),
            encoding="utf-8",
        )
        fake_codex.chmod(0o755)
        return fake_codex

    def codex_fake_env(self, tmp_path: Path) -> dict[str, str]:
        """Return an environment wired to the deterministic fake codex binary."""
        fake_bin = tmp_path / "bin"
        self.install_fake_codex(fake_bin)
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}:{env['PATH']}"
        env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
        return self.fast_timing_env(env)

    def ccc_fake_env(self, tmp_path: Path) -> dict[str, str]:
        """Return an environment wired to the timed fake ccc binary."""
        fake_bin = tmp_path / "bin"
        self.install_timed_fake_ccc(fake_bin)
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}:{env['PATH']}"
        env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
        env["CCC_FAKE_EVENTS"] = str(tmp_path / "ccc-events.jsonl")
        env["CCC_FAKE_ACTIVE"] = str(tmp_path / "ccc-active.txt")
        env["CCC_FAKE_MAX"] = str(tmp_path / "ccc-max.txt")
        env["CCC_FAKE_LOCK"] = str(tmp_path / "ccc.lock")
        env["CCC_FAKE_RUN_ROOT"] = str(tmp_path / "ccc-runs")
        return self.fast_timing_env(env)

    def read_ccc_events(self, tmp_path: Path) -> list[dict[str, object]]:
        """Read fake ccc event records created by install_timed_fake_ccc."""
        events_path = tmp_path / "ccc-events.jsonl"
        return [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]

    def install_fake_kimi(self, fake_bin: Path, *, output: str = "fake kimi result\n") -> tuple[Path, Path]:
        """Install a fake kimi binary that records argv and stdin."""
        fake_bin.mkdir(parents=True, exist_ok=True)
        fake_kimi = fake_bin / "kimi"
        args_path = fake_bin.parent / "kimi-args.json"
        fake_kimi.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                import sys
                from pathlib import Path

                payload = {
                    "args": sys.argv[1:],
                    "stdin": sys.stdin.read(),
                }
                Path(os.environ["KIMI_ARGS_PATH"]).write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
                sys.stdout.write(os.environ.get("KIMI_FAKE_OUTPUT", "fake kimi result\\n"))
                sys.stderr.write("To resume this session: kimi -r ses_fake\\n")
                """
            ),
            encoding="utf-8",
        )
        fake_kimi.chmod(0o755)
        return fake_kimi, args_path

    def kimi_fake_env(self, tmp_path: Path, *, output: str = "fake kimi result\n") -> tuple[dict[str, str], Path]:
        """Return an environment wired to a fake kimi binary."""
        fake_bin = tmp_path / "bin"
        _, args_path = self.install_fake_kimi(fake_bin)
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}:{env['PATH']}"
        env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
        env["KIMI_ARGS_PATH"] = str(args_path)
        env["KIMI_FAKE_OUTPUT"] = output
        return env, args_path

    def test_state_cli_tracks_phases_agents_and_metrics(self) -> None:
        """Ensure state commands create a run and keep derived metrics current."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_script(
                "workflow_state.py",
                "init",
                "--title",
                "Unit State",
                "--prompt",
                "exercise state commands",
                "--cwd",
                str(ROOT),
                env=env,
            )
            run_id = json.loads(created.stdout)["run_id"]
            self.run_script(
                "workflow_state.py",
                "add-phase",
                run_id,
                "--phase-id",
                "phase-test",
                "--name",
                "Test",
                "--status",
                "running",
                env=env,
            )
            self.run_script(
                "workflow_state.py",
                "add-agent",
                run_id,
                "--phase",
                "phase-test",
                "--agent-id",
                "agent-test",
                "--name",
                "Agent Test",
                "--status",
                "running",
                env=env,
            )
            self.run_script(
                "workflow_state.py",
                "update-agent",
                run_id,
                "agent-test",
                "--status",
                "completed",
                "--summary",
                "agent completed",
                env=env,
            )
            shown = self.run_script("workflow_state.py", "show", run_id, "--json", env=env)
            data = json.loads(shown.stdout)
            self.assertEqual(data["metrics"]["agents_total"], 1)
            self.assertEqual(data["metrics"]["agents_by_status"]["completed"], 1)
            self.assertEqual(data["phases"][0]["agent_ids"], ["agent-test"])

    def test_running_agent_process_identity_is_visible(self) -> None:
        """Ensure process identity fields are stored and retrievable for running agents."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_script(
                "workflow_state.py",
                "init",
                "--title",
                "Process Identity Test",
                "--prompt",
                "exercise process identity",
                "--cwd",
                str(ROOT),
                env=env,
            )
            run_id = json.loads(created.stdout)["run_id"]
            self.run_script(
                "workflow_state.py",
                "add-phase",
                run_id,
                "--phase-id",
                "phase-pid",
                "--name",
                "Process Identity Phase",
                "--status",
                "running",
                env=env,
            )
            self.run_script(
                "workflow_state.py",
                "add-agent",
                run_id,
                "--phase",
                "phase-pid",
                "--agent-id",
                "agent-pid",
                "--name",
                "PID Agent",
                "--status",
                "running",
                "--process-id",
                "12345",
                "--process-group-id",
                "12345",
                env=env,
            )
            shown = self.run_script("workflow_state.py", "show", run_id, "--json", env=env)
            data = json.loads(shown.stdout)
            agent = data["agents"][0]
            self.assertEqual(agent["process_id"], 12345)
            self.assertEqual(agent["process_group_id"], 12345)

            self.run_script(
                "workflow_state.py",
                "update-agent",
                run_id,
                "agent-pid",
                "--status",
                "completed",
                "--summary",
                "done",
                "--native-id",
                "native-abc",
                env=env,
            )
            completed = self.run_script("workflow_state.py", "show", run_id, "--json", env=env)
            completed_data = json.loads(completed.stdout)
            completed_agent = completed_data["agents"][0]
            self.assertEqual(completed_agent["process_id"], 12345)
            self.assertEqual(completed_agent["process_group_id"], 12345)
            self.assertEqual(completed_agent["native_id"], "native-abc")

    def test_wf_wrapper_defaults_state_to_user_agents_root(self) -> None:
        """Installed command wrappers should share the documented user state root."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            skill_scripts = tmp_path / ".agents" / "skills" / "workflow" / "scripts"
            skill_scripts.mkdir(parents=True)
            shutil.copy2(SCRIPTS / "wf", skill_scripts / "wf")
            shutil.copy2(SCRIPTS / "workflow_state.py", skill_scripts / "workflow_state.py")
            (skill_scripts / "wf").chmod(0o755)

            env = os.environ.copy()
            env.pop("WORKFLOW_HOME", None)
            env.pop("WORKFLOW_STATE_DIR", None)
            env["HOME"] = str(tmp_path / "home")
            created = subprocess.run(
                [
                    str(skill_scripts / "wf"),
                    "init",
                    "--title",
                    "Portable Install",
                    "--prompt",
                    "portable install state root",
                    "--cwd",
                    str(ROOT),
                ],
                check=True,
                text=True,
                capture_output=True,
                env=env,
            )
            data = json.loads(created.stdout)
            run = json.loads(Path(data["path"]).read_text(encoding="utf-8"))
            self.assertTrue(str(run["paths"]["run_dir"]).startswith(str(tmp_path / "home" / ".agents" / "workflow-system")))

    def test_direct_state_script_defaults_to_user_agents_state(self) -> None:
        """Direct script usage should use a portable Codex user-scope state root."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env = os.environ.copy()
            env.pop("WORKFLOW_HOME", None)
            env.pop("WORKFLOW_STATE_DIR", None)
            env["HOME"] = str(tmp_path)
            created = self.run_script(
                "workflow_state.py",
                "init",
                "--title",
                "Direct Install",
                "--prompt",
                "direct script state root",
                "--cwd",
                str(ROOT),
                env=env,
            )
            data = json.loads(created.stdout)
            run = json.loads(Path(data["path"]).read_text(encoding="utf-8"))
            self.assertTrue(str(run["paths"]["run_dir"]).startswith(str(tmp_path / ".agents" / "workflow-system")))

    def test_init_rejects_invalid_mode(self) -> None:
        """--mode must be one of the documented vocabulary values."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            denied = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "workflow_state.py"),
                    "init",
                    "--title",
                    "Bad Mode",
                    "--prompt",
                    "mode test",
                    "--cwd",
                    str(ROOT),
                    "--mode",
                    "bogus",
                ],
                check=False,
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("invalid choice", denied.stderr)

    def test_add_agent_with_terminal_status_records_completion_time(self) -> None:
        """Backfilled/local completed agents should have lifecycle timestamps immediately."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_script(
                "workflow_state.py",
                "init",
                "--title",
                "Completed Agent",
                "--prompt",
                "exercise completed agent creation",
                "--cwd",
                str(ROOT),
                env=env,
            )
            run_id = json.loads(created.stdout)["run_id"]
            self.run_script(
                "workflow_state.py",
                "add-phase",
                run_id,
                "--phase-id",
                "phase-impl",
                "--name",
                "Implementation",
                "--status",
                "completed",
                env=env,
            )
            self.run_script(
                "workflow_state.py",
                "add-agent",
                run_id,
                "--phase",
                "phase-impl",
                "--agent-id",
                "agent-local-impl",
                "--name",
                "Lead local implementation",
                "--agent-type",
                "lead-local",
                "--status",
                "completed",
                env=env,
            )
            data = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
            agent = data["agents"][0]
            self.assertTrue(agent["started_at"])
            self.assertTrue(agent["completed_at"])
            self.assertEqual(data["phases"][0]["agent_ids"], ["agent-local-impl"])

    @slow_test
    def test_state_cli_clears_terminal_timestamps_when_reopened(self) -> None:
        """Ensure reopened phases and agents do not retain stale completed_at values."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_script(
                "workflow_state.py",
                "init",
                "--title",
                "Lifecycle Reopen",
                "--prompt",
                "exercise lifecycle reopening",
                "--cwd",
                str(ROOT),
                env=env,
            )
            run_id = json.loads(created.stdout)["run_id"]
            self.run_script(
                "workflow_state.py",
                "add-phase",
                run_id,
                "--phase-id",
                "phase-life",
                "--name",
                "Lifecycle",
                "--status",
                "running",
                env=env,
            )
            self.run_script(
                "workflow_state.py",
                "add-agent",
                run_id,
                "--phase",
                "phase-life",
                "--agent-id",
                "agent-life",
                "--name",
                "Lifecycle Agent",
                "--status",
                "running",
                env=env,
            )
            self.run_script("workflow_state.py", "update-phase", run_id, "phase-life", "--status", "completed", env=env)
            self.run_script("workflow_state.py", "update-agent", run_id, "agent-life", "--status", "completed", env=env)
            completed = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
            self.assertTrue(completed["phases"][0]["completed_at"])
            self.assertTrue(completed["agents"][0]["completed_at"])
            self.run_script("workflow_state.py", "update-phase", run_id, "phase-life", "--status", "running", env=env)
            self.run_script("workflow_state.py", "update-agent", run_id, "agent-life", "--status", "paused", env=env)
            reopened = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
            self.assertIsNone(reopened["phases"][0]["completed_at"])
            self.assertIsNone(reopened["agents"][0]["completed_at"])

    def test_pause_resume_stop_update_workflow_control_state(self) -> None:
        """Persist cooperative workflow control flags and cancel unfinished work on stop."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_script(
                "workflow_state.py",
                "init",
                "--title",
                "Control Lifecycle",
                "--prompt",
                "exercise pause resume stop",
                "--cwd",
                str(ROOT),
                env=env,
            )
            run_id = json.loads(created.stdout)["run_id"]
            self.run_script(
                "workflow_state.py",
                "add-phase",
                run_id,
                "--phase-id",
                "phase-control",
                "--name",
                "Control",
                "--status",
                "running",
                env=env,
            )
            self.run_script(
                "workflow_state.py",
                "add-agent",
                run_id,
                "--phase",
                "phase-control",
                "--agent-id",
                "agent-control",
                "--name",
                "Control Agent",
                "--status",
                "pending",
                env=env,
            )

            paused = json.loads(self.run_script("workflow_state.py", "pause", run_id, "--reason", "test pause", env=env).stdout)
            self.assertTrue(paused["changed"])
            paused_run = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
            self.assertEqual(paused_run["status"], "paused")
            self.assertTrue(paused_run["control"]["paused"])
            self.assertEqual(paused_run["control"]["pause_reason"], "test pause")

            resumed = json.loads(self.run_script("workflow_state.py", "resume", run_id, env=env).stdout)
            self.assertTrue(resumed["changed"])
            resumed_run = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
            self.assertEqual(resumed_run["status"], "running")
            self.assertFalse(resumed_run["control"]["paused"])

            stopped = json.loads(self.run_script("workflow_state.py", "stop", run_id, "--no-terminate", env=env).stdout)
            self.assertTrue(stopped["changed"])
            stopped_run = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
            self.assertEqual(stopped_run["status"], "cancelled")
            self.assertTrue(stopped_run["control"]["stop_requested"])
            self.assertEqual(stopped_run["phases"][0]["status"], "cancelled")
            self.assertEqual(stopped_run["agents"][0]["status"], "cancelled")

    def test_wf_wrapper_dispatches_pause_resume_stop(self) -> None:
        """Expose lifecycle controls from the installed short workflow command."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_wf(
                "init",
                "--title",
                "WF Control",
                "--prompt",
                "exercise wf controls",
                "--cwd",
                str(ROOT),
                env=env,
            )
            run_id = json.loads(created.stdout)["run_id"]
            self.run_wf("pause", run_id, env=env)
            self.assertEqual(json.loads(self.run_wf("show", run_id, "--json", env=env).stdout)["status"], "paused")
            self.run_wf("resume", run_id, env=env)
            self.assertEqual(json.loads(self.run_wf("show", run_id, "--json", env=env).stdout)["status"], "running")
            self.run_wf("stop", run_id, "--no-terminate", env=env)
            self.assertEqual(json.loads(self.run_wf("show", run_id, "--json", env=env).stdout)["status"], "cancelled")

    def test_missing_run_emits_friendly_message(self) -> None:
        """A missing run id should suggest wf list instead of a raw traceback."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            for cmd in ("show", "check", "done"):
                with self.subTest(cmd=cmd):
                    result = self.run_wf(cmd, "wf-missing-run-id", env=env, check=False)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("no run 'wf-missing-run-id'", result.stderr)
                    self.assertIn("try: wf list", result.stderr)

    def test_workflow_ops_pause_resume_stop_json(self) -> None:
        """Lifecycle controls are available directly through workflow_ops.py."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_script(
                "workflow_state.py",
                "init",
                "--title",
                "Ops Control",
                "--prompt",
                "exercise ops controls",
                "--cwd",
                str(ROOT),
                env=env,
            )
            run_id = json.loads(created.stdout)["run_id"]
            pause = json.loads(self.run_script("workflow_ops.py", "pause", run_id, "--reason", "ops pause", env=env).stdout)
            self.assertEqual(pause["status"], "paused")
            resume = json.loads(self.run_script("workflow_ops.py", "resume", run_id, env=env).stdout)
            self.assertEqual(resume["status"], "running")
            stop = json.loads(self.run_script("workflow_ops.py", "stop", run_id, "--no-terminate", env=env).stdout)
            self.assertEqual(stop["status"], "cancelled")

    @slow_test
    def test_mock_worker_waits_while_run_is_paused(self) -> None:
        """Paused cooperative runs should not launch pending workers until resumed."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env = self.fast_timing_env(os.environ.copy())
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            old_env = os.environ.get("WORKFLOW_STATE_DIR")
            os.environ["WORKFLOW_STATE_DIR"] = env["WORKFLOW_STATE_DIR"]
            sys.path.insert(0, str(SCRIPTS))
            try:
                import workflow_run_codex  # pylint: disable=import-outside-toplevel
                import workflow_state  # pylint: disable=import-outside-toplevel

                created = self.run_script(
                    "workflow_state.py",
                    "init",
                    "--title",
                    "Paused Mock Worker",
                    "--prompt",
                    "exercise paused runner",
                    "--cwd",
                    str(ROOT),
                    env=env,
                )
                run_id = json.loads(created.stdout)["run_id"]
                self.run_script(
                    "workflow_state.py",
                    "add-phase",
                    run_id,
                    "--phase-id",
                    workflow_run_codex.PHASE_ID,
                    "--name",
                    "Workers",
                    "--status",
                    "running",
                    env=env,
                )
                output_path = tmp_path / "worker.final.md"
                self.run_script(
                    "workflow_state.py",
                    "add-agent",
                    run_id,
                    "--phase",
                    workflow_run_codex.PHASE_ID,
                    "--agent-id",
                    "agent-paused",
                    "--name",
                    "Paused Agent",
                    "--status",
                    "pending",
                    "--prompt",
                    "do mock work",
                    "--jsonl-path",
                    str(tmp_path / "worker.jsonl"),
                    "--log-path",
                    str(tmp_path / "worker.stderr.log"),
                    "--output-path",
                    str(output_path),
                    env=env,
                )
                self.run_script("workflow_state.py", "pause", run_id, env=env)
                args = argparse.Namespace(mock=True, dry_run=False, max_agents=1, startup_delay=0.0)

                async def exercise() -> str:
                    run = workflow_state.load_run(run_id)
                    task = asyncio.create_task(workflow_run_codex.run_all(run, args, workflow_run_codex.CodexDirectProvider()))
                    await asyncio.sleep(0.05)
                    paused_run = workflow_state.load_run(run_id)
                    self.assertEqual(paused_run["agents"][0]["status"], "paused")
                    self.assertFalse(output_path.exists())
                    with contextlib.redirect_stdout(io.StringIO()):
                        workflow_state.cmd_resume(argparse.Namespace(run=run_id, reason=None))
                    return await asyncio.wait_for(task, timeout=4.0)

                status = asyncio.run(exercise())
                self.assertEqual(status, "completed")
                self.assertIn("Mock result", output_path.read_text(encoding="utf-8"))
            finally:
                if old_env is None:
                    os.environ.pop("WORKFLOW_STATE_DIR", None)
                else:
                    os.environ["WORKFLOW_STATE_DIR"] = old_env

    def test_quota_retry_backoff_stops_when_workflow_is_stopped(self) -> None:
        """Stop requests should interrupt quota backoff before the next launch."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            old_env = os.environ.get("WORKFLOW_STATE_DIR")
            os.environ["WORKFLOW_STATE_DIR"] = env["WORKFLOW_STATE_DIR"]
            sys.path.insert(0, str(SCRIPTS))
            try:
                import workflow_run_codex  # pylint: disable=import-outside-toplevel

                created = self.run_script(
                    "workflow_state.py",
                    "init",
                    "--title",
                    "Quota Stop",
                    "--prompt",
                    "exercise quota stop",
                    "--cwd",
                    str(ROOT),
                    env=env,
                )
                run_id = json.loads(created.stdout)["run_id"]
                self.run_script(
                    "workflow_state.py",
                    "add-agent",
                    run_id,
                    "--agent-id",
                    "agent-quota-stop",
                    "--name",
                    "Quota Agent",
                    "--status",
                    "running",
                    "--prompt",
                    "do quota work",
                    "--jsonl-path",
                    str(tmp_path / "worker.jsonl"),
                    "--log-path",
                    str(tmp_path / "worker.stderr.log"),
                    "--output-path",
                    str(tmp_path / "worker.final.md"),
                    env=env,
                )

                async def exercise() -> bool:
                    task = asyncio.create_task(workflow_run_codex.sleep_until_quota_retry_allowed(run_id, "agent-quota-stop", 0.1))
                    await asyncio.sleep(0.02)
                    self.run_script("workflow_state.py", "stop", run_id, "--no-terminate", env=env)
                    return await asyncio.wait_for(task, timeout=1.0)

                self.assertFalse(asyncio.run(exercise()))
                stopped_run = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
                self.assertEqual(stopped_run["agents"][0]["status"], "cancelled")
            finally:
                if old_env is None:
                    os.environ.pop("WORKFLOW_STATE_DIR", None)
                else:
                    os.environ["WORKFLOW_STATE_DIR"] = old_env

    def test_state_cli_rejects_duplicate_ids(self) -> None:
        """Protect tooling consumers from ambiguous phase and agent identifiers."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_script(
                "workflow_state.py",
                "init",
                "--title",
                "Duplicate Guard",
                "--prompt",
                "exercise duplicate id rejection",
                "--cwd",
                str(ROOT),
                env=env,
            )
            run_id = json.loads(created.stdout)["run_id"]
            self.run_script(
                "workflow_state.py",
                "add-phase",
                run_id,
                "--phase-id",
                "phase-dup",
                "--name",
                "Duplicate Phase",
                env=env,
            )
            duplicate_phase = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "workflow_state.py"),
                    "add-phase",
                    run_id,
                    "--phase-id",
                    "phase-dup",
                    "--name",
                    "Duplicate Phase Again",
                ],
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertNotEqual(duplicate_phase.returncode, 0)
            self.assertIn("duplicate phase_id", duplicate_phase.stderr)
            self.run_script(
                "workflow_state.py",
                "add-agent",
                run_id,
                "--phase",
                "phase-dup",
                "--agent-id",
                "agent-dup",
                "--name",
                "Duplicate Agent",
                env=env,
            )
            duplicate_agent = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "workflow_state.py"),
                    "add-agent",
                    run_id,
                    "--phase",
                    "phase-dup",
                    "--agent-id",
                    "agent-dup",
                    "--name",
                    "Duplicate Agent Again",
                ],
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertNotEqual(duplicate_agent.returncode, 0)
            self.assertIn("duplicate agent_id", duplicate_agent.stderr)

    def test_mock_runner_creates_completed_run(self) -> None:
        """Verify mocked workers update state without making model calls."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            launched = self.run_script(
                "workflow_run_codex.py",
                "--title",
                "Mocked Workers",
                "--cwd",
                str(ROOT),
                "--mock",
                "--startup-delay",
                "0",
                "--job",
                "alpha::Alpha prompt",
                "--job",
                "beta::Beta prompt",
                env=env,
            )
            run_id = json.loads(launched.stdout.split("\ncommand:", 1)[0])["run_id"]
            shown = self.run_script("workflow_state.py", "show", run_id, "--json", env=env)
            data = json.loads(shown.stdout)
            self.assertEqual(data["status"], "completed")
            self.assertEqual(data["metrics"]["agents_total"], 2)
            self.assertEqual(data["metrics"]["agents_by_status"]["completed"], 2)
            self.assertTrue(all(agent["started_at"] for agent in data["agents"]))
            self.assertTrue(all(agent["completed_at"] for agent in data["agents"]))
            self.assertTrue(Path(data["agents"][0]["output_path"]).exists())
            self.assertTrue(data["decisions"])
            self.assertEqual(data["decisions"][0]["title"], "Runner selected: codex-direct")
            self.assertEqual(len(data["artifacts"]), 2)
            self.assertEqual({artifact["kind"] for artifact in data["artifacts"]}, {"worker-output"})
            self.assertTrue(all(Path(artifact["path"]).exists() for artifact in data["artifacts"]))

    def test_mock_runner_attaches_workers_to_existing_run(self) -> None:
        """--attach-run should add workers to an existing workflow run."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_script(
                "workflow_state.py",
                "init",
                "--title",
                "Existing Run",
                "--prompt",
                "accept worker attachments",
                "--cwd",
                str(ROOT),
                env=env,
            )
            run_id = json.loads(created.stdout)["run_id"]
            launched = self.run_script(
                "workflow_run_codex.py",
                "--attach-run",
                run_id,
                "--cwd",
                str(ROOT),
                "--mock",
                "--startup-delay",
                "0",
                "--job",
                "alpha::Alpha prompt",
                env=env,
            )
            payload = json.loads(launched.stdout.split("\ncommand:", 1)[0])
            self.assertEqual(payload["run_id"], run_id)
            runs_dir = Path(env["WORKFLOW_STATE_DIR"]) / "runs"
            self.assertEqual(len(list(runs_dir.glob("*/run.json"))), 1)
            data = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
            self.assertEqual(data["status"], "completed")
            self.assertEqual(data["phases"][0]["phase_id"], "phase-cli-workers")
            self.assertEqual(data["phases"][0]["status"], "completed")
            self.assertEqual(data["agents"][0]["name"], "alpha")
            self.assertEqual(data["agents"][0]["status"], "completed")
            self.assertEqual(data["decisions"][0]["title"], "Runner selected: codex-direct")

    def test_update_agent_suppresses_unchanged_running_refresh_events(self) -> None:
        """Live telemetry refreshes should not append durable events."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            old_env = os.environ.get("WORKFLOW_STATE_DIR")
            os.environ["WORKFLOW_STATE_DIR"] = env["WORKFLOW_STATE_DIR"]
            sys.path.insert(0, str(SCRIPTS))
            try:
                import workflow_run_codex  # pylint: disable=import-outside-toplevel

                created = self.run_script(
                    "workflow_state.py",
                    "init",
                    "--title",
                    "Event Quieting",
                    "--prompt",
                    "quiet telemetry updates",
                    "--cwd",
                    str(ROOT),
                    env=env,
                )
                run_id = json.loads(created.stdout)["run_id"]
                self.run_script(
                    "workflow_state.py",
                    "add-agent",
                    run_id,
                    "--agent-id",
                    "agent-quiet",
                    "--name",
                    "Quiet Agent",
                    "--status",
                    "pending",
                    "--prompt",
                    "do quiet work",
                    env=env,
                )
                before = workflow_run_codex.workflow_state.load_run(run_id)
                workflow_run_codex.update_agent(run_id, "agent-quiet", status="running", latest_output="first")
                after_transition = workflow_run_codex.workflow_state.load_run(run_id)
                workflow_run_codex.update_agent(run_id, "agent-quiet", status="running", latest_output="second")
                workflow_run_codex.update_agent(run_id, "agent-quiet", latest_output="third", tool_call_count=3)
                after_refresh = workflow_run_codex.workflow_state.load_run(run_id)
                self.assertEqual(len(after_transition["events"]), len(before["events"]) + 1)
                self.assertEqual(len(after_refresh["events"]), len(after_transition["events"]))
                self.assertEqual(after_refresh["agents"][0]["latest_output"], "third")
                self.assertEqual(after_refresh["agents"][0]["tool_call_count"], 3)
            finally:
                if old_env is None:
                    os.environ.pop("WORKFLOW_STATE_DIR", None)
                else:
                    os.environ["WORKFLOW_STATE_DIR"] = old_env

    @slow_test
    def test_parent_run_links_new_child_run_metadata_and_event(self) -> None:
        """--parent-run should link a newly created run back to its parent."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_script(
                "workflow_state.py",
                "init",
                "--title",
                "Parent Run",
                "--prompt",
                "own child workflows",
                "--cwd",
                str(ROOT),
                env=env,
            )
            parent_id = json.loads(created.stdout)["run_id"]
            launched = self.run_script(
                "workflow_run_codex.py",
                "--title",
                "Child Run",
                "--parent-run",
                parent_id,
                "--cwd",
                str(ROOT),
                "--mock",
                "--startup-delay",
                "0",
                "--job",
                "alpha::Alpha prompt",
                env=env,
            )
            child_id = json.loads(launched.stdout.split("\ncommand:", 1)[0])["run_id"]
            child = json.loads(self.run_script("workflow_state.py", "show", child_id, "--json", env=env).stdout)
            parent = json.loads(self.run_script("workflow_state.py", "show", parent_id, "--json", env=env).stdout)
            self.assertEqual(child["metadata"]["parent_run_id"], parent_id)
            self.assertTrue(
                any(
                    event.get("operation") == "child_run_linked"
                    and event.get("data", {}).get("child_run_id") == child_id
                    for event in parent["events"]
                )
            )

    def test_opencode_direct_extracts_text_events(self) -> None:
        """Extract final OpenCode answers from the JSON text event shape."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_run_codex  # pylint: disable=import-outside-toplevel

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            jsonl_path = tmp_path / "opencode.jsonl"
            output_path = tmp_path / "final.md"
            jsonl_path.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "step_start", "sessionID": "ses_test"}),
                        json.dumps(
                            {
                                "type": "text",
                                "sessionID": "ses_test",
                                "part": {"text": "F(100) = 354224848179261915075"},
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            provider = workflow_run_codex.OpencodeDirectProvider()
            result = provider.extract_result({"jsonl_path": str(jsonl_path), "output_path": str(output_path)}, 0)
            self.assertEqual(result.thread_id, "ses_test")
            self.assertEqual(result.result, "F(100) = 354224848179261915075")
            self.assertEqual(output_path.read_text(encoding="utf-8"), "F(100) = 354224848179261915075")

    def test_kimi_direct_runner_pipes_prompt_and_records_output(self) -> None:
        """Run Kimi directly in quiet stdin mode and persist the final answer."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env, args_path = self.kimi_fake_env(tmp_path, output="KIMI_DIRECT_OK\n")
            launched = self.run_script(
                "workflow_run.py",
                "--title",
                "Fake Kimi Workers",
                "--cwd",
                str(ROOT),
                "--runner",
                "kimi-direct",
                "--model",
                "kimi-code/kimi-for-coding",
                "--startup-delay",
                "0",
                "--job",
                "alpha::Alpha prompt",
                env=env,
            )
            run_id = json.loads(launched.stdout.split("\ncommand:", 1)[0])["run_id"]
            data = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
            agent = data["agents"][0]
            fake_call = json.loads(args_path.read_text(encoding="utf-8"))
            self.assertEqual(data["status"], "completed")
            self.assertEqual(data["mode"], "kimi-direct")
            self.assertEqual(agent["agent_type"], "kimi-cli")
            self.assertEqual(agent["thread_id"], "ses_fake")
            self.assertEqual(agent["result"], "KIMI_DIRECT_OK\n")
            self.assertEqual(Path(agent["output_path"]).read_text(encoding="utf-8"), "KIMI_DIRECT_OK\n")
            self.assertIn("--quiet", fake_call["args"])
            self.assertIn("--input-format", fake_call["args"])
            self.assertIn("--work-dir", fake_call["args"])
            self.assertIn(str(ROOT), fake_call["args"])
            self.assertIn("kimi-code/kimi-for-coding", fake_call["args"])
            self.assertIn("--max-steps-per-turn", fake_call["args"])
            self.assertIn("9999", fake_call["args"])
            self.assertEqual(fake_call["stdin"], "Alpha prompt\n")
            self.assertIn("<prompt-on-stdin>", agent["command_preview"])

    def test_start_can_use_kimi_direct_as_planner(self) -> None:
        """Allow wf start to ask Kimi directly for the workflow plan."""
        planner_json = json.dumps(
            {
                "title": "Kimi Planned Workflow",
                "summary": "one fake Kimi-planned job",
                "jobs": [{"name": "alpha", "role": "tester", "prompt": "Alpha worker prompt"}],
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env, args_path = self.kimi_fake_env(tmp_path, output=planner_json + "\n")
            launched = self.run_script(
                "workflow_start.py",
                "plan with kimi",
                "--planner-runner",
                "kimi-direct",
                "--runner",
                "codex-direct",
                "--dry-run",
                "--startup-delay",
                "0",
                env=env,
            )
            created = json.loads(launched.stdout.split("\ncommand:", 1)[0])
            data = json.loads(self.run_script("workflow_state.py", "show", created["run_id"], "--json", env=env).stdout)
            fake_call = json.loads(args_path.read_text(encoding="utf-8"))
            self.assertEqual(created["planner"], "kimi-direct")
            self.assertEqual(data["title"], "Kimi Planned Workflow")
            self.assertEqual(data["agents"][0]["name"], "alpha")
            self.assertIn("Return ONLY a JSON object", fake_call["stdin"])
            self.assertIn("--quiet", fake_call["args"])

    def test_ccc_opencode_runner_records_ccc_artifacts(self) -> None:
        """Verify ccc providers use ccc's artifact footer as the result contract."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "bin"
            fake_bin.mkdir()
            fake_ccc = fake_bin / "ccc"
            fake_run_dir = tmp_path / "ccc-run"
            args_path = tmp_path / "ccc-args.txt"
            fake_ccc.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "printf '%s\\n' \"$@\" > \"$CCC_ARGS_PATH\"\n"
                "mkdir -p \"$CCC_FAKE_RUN_DIR\"\n"
                "printf 'final from fake ccc\\n' > \"$CCC_FAKE_RUN_DIR/output.txt\"\n"
                "printf '[assistant] final from fake ccc\\n' > \"$CCC_FAKE_RUN_DIR/transcript.txt\"\n"
                "printf '[assistant] final from fake ccc\\n'\n"
                "printf '>> ccc:output-log >> %s\\n' \"$CCC_FAKE_RUN_DIR\" >&2\n",
                encoding="utf-8",
            )
            fake_ccc.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            env["CCC_FAKE_RUN_DIR"] = str(fake_run_dir)
            env["CCC_ARGS_PATH"] = str(args_path)
            launched = self.run_script(
                "workflow_run.py",
                "--title",
                "Fake Ccc Workers",
                "--cwd",
                str(ROOT),
                "--runner",
                "ccc-opencode",
                "--job",
                "alpha::Alpha prompt",
                env=env,
            )
            run_id = json.loads(launched.stdout.split("\ncommand:", 1)[0])["run_id"]
            data = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
            agent = data["agents"][0]
            self.assertEqual(data["status"], "completed")
            self.assertEqual(data["mode"], "ccc-opencode")
            self.assertEqual(agent["agent_type"], "ccc-opencode")
            self.assertEqual(agent["result"], "final from fake ccc\n")
            self.assertTrue(agent["output_path"].endswith(".final.md"))
            self.assertTrue(agent["jsonl_path"].endswith(".jsonl"))
            self.assertEqual(Path(agent["output_path"]).read_text(encoding="utf-8"), "final from fake ccc\n")
            self.assertEqual(Path(agent["jsonl_path"]).read_text(encoding="utf-8"), "[assistant] final from fake ccc\n")
            self.assertEqual(agent["thread_id"], fake_run_dir.name)
            self.assertEqual(data["artifacts"][0]["path"], agent["output_path"])
            fake_args = args_path.read_text(encoding="utf-8").splitlines()
            self.assertIn("--output-mode", fake_args)
            self.assertIn("stream-json", fake_args)
            self.assertIn("opencode", fake_args)
            self.assertIn("--", fake_args)
            self.assertEqual(fake_args[-1], "Alpha prompt")

    def test_ccc_opencode_runner_records_live_telemetry_in_state(self) -> None:
        """Summarize ccc/OpenCode transcript tokens and tools back into run state."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "bin"
            fake_bin.mkdir()
            fake_ccc = fake_bin / "ccc"
            fake_run_dir = tmp_path / "ccc-run"
            fake_ccc.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import os
                    import sys
                    from pathlib import Path

                    run_dir = Path(os.environ["CCC_FAKE_RUN_DIR"])
                    run_dir.mkdir(parents=True, exist_ok=True)
                    (run_dir / "output.txt").write_text("final telemetry answer\\n", encoding="utf-8")
                    events = [
                        {"type": "text", "part": {"text": "Checking the project."}},
                        {
                            "type": "tool_use",
                            "part": {
                                "type": "tool",
                                "tool": "read",
                                "callID": "call_1",
                                "state": {"status": "completed", "input": {"filePath": "README.md"}},
                            },
                        },
                        {"type": "step_finish", "part": {"type": "step-finish", "tokens": {"total": 777, "input": 700, "output": 77}}},
                    ]
                    transcript = "\\n".join(json.dumps(event) for event in events) + "\\n"
                    (run_dir / "transcript.jsonl").write_text(transcript, encoding="utf-8")
                    sys.stdout.write(transcript)
                    sys.stderr.write(f">> ccc:output-log >> {run_dir}\\n")
                    """
                ),
                encoding="utf-8",
            )
            fake_ccc.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            env["CCC_FAKE_RUN_DIR"] = str(fake_run_dir)
            launched = self.run_script(
                "workflow_run.py",
                "--title",
                "Fake Ccc Telemetry",
                "--cwd",
                str(ROOT),
                "--runner",
                "ccc-opencode",
                "--startup-delay",
                "0",
                "--job",
                "alpha::Alpha prompt",
                env=env,
            )
            run_id = json.loads(launched.stdout.split("\ncommand:", 1)[0])["run_id"]
            data = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
            agent = data["agents"][0]
            self.assertEqual(data["status"], "completed")
            self.assertTrue(agent["jsonl_path"].endswith(".jsonl"))
            self.assertIn("Checking the project.", Path(agent["jsonl_path"]).read_text(encoding="utf-8"))
            self.assertEqual(agent["tool_call_count"], 1)
            self.assertEqual(agent["tokens"]["total"], 777)
            self.assertEqual(agent["token_total"], 777)
            self.assertIn("read", "\n".join(agent["latest_tool_calls"]))
            self.assertIn("final telemetry answer", agent["latest_output"])

    @slow_test
    def test_ccc_runner_persists_live_telemetry_while_worker_is_running(self) -> None:
        """Write tool/token telemetry into run.json before a long worker exits."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "bin"
            fake_bin.mkdir()
            fake_ccc = fake_bin / "ccc"
            fake_run_dir = tmp_path / "ccc-run"
            fake_ccc.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import os
                    import sys
                    import time
                    from pathlib import Path

                    event = {
                        "type": "tool_use",
                        "part": {
                            "type": "tool",
                            "tool": "read",
                            "callID": "call_live",
                            "state": {"status": "completed", "input": {"filePath": "README.md"}},
                        },
                    }
                    sys.stdout.write(json.dumps(event) + "\\n")
                    sys.stdout.write(json.dumps({"type": "step_finish", "part": {"tokens": {"total": 42, "input": 40, "output": 2}}}) + "\\n")
                    sys.stdout.flush()
                    time.sleep(float(os.environ.get("CCC_FAKE_LIVE_SLEEP", "0.5")))
                    run_dir = Path(os.environ["CCC_FAKE_RUN_DIR"])
                    run_dir.mkdir(parents=True, exist_ok=True)
                    (run_dir / "output.txt").write_text("live telemetry done\\n", encoding="utf-8")
                    (run_dir / "transcript.jsonl").write_text(json.dumps(event) + "\\n", encoding="utf-8")
                    sys.stderr.write(f">> ccc:output-log >> {run_dir}\\n")
                    """
                ),
                encoding="utf-8",
            )
            fake_ccc.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            env["CCC_FAKE_RUN_DIR"] = str(fake_run_dir)
            env["CCC_FAKE_LIVE_SLEEP"] = "0.5"
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(SCRIPTS / "workflow_run.py"),
                    "--title",
                    "Live Telemetry",
                    "--cwd",
                    str(ROOT),
                    "--runner",
                    "ccc-opencode",
                    "--startup-delay",
                    "0",
                    "--job",
                    "alpha::Alpha prompt",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            try:
                run_file = None
                deadline = time.time() + 8
                while time.time() < deadline:
                    candidates = list((tmp_path / "state" / "runs").glob("*/run.json"))
                    if candidates:
                        run_file = candidates[0]
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(run_file)
                observed_live = False
                while time.time() < deadline:
                    data = json.loads(run_file.read_text(encoding="utf-8"))  # type: ignore[union-attr]
                    agent = data["agents"][0]
                    if agent.get("status") == "running" and agent.get("tool_call_count", 0) >= 1:
                        observed_live = True
                        self.assertEqual(agent["tokens"]["total"], 42)
                        break
                    time.sleep(0.05)
                self.assertTrue(observed_live, msg=run_file.read_text(encoding="utf-8"))  # type: ignore[union-attr]
                stdout, stderr = proc.communicate(timeout=10)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        proc.communicate(timeout=2)
            self.assertEqual(proc.returncode, 0, msg=(stdout if "stdout" in locals() else "") + (stderr if "stderr" in locals() else ""))

    def test_runner_captures_json_lines_larger_than_asyncio_line_limit(self) -> None:
        """Capture large OpenCode JSONL records without readline-limit failures."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "bin"
            fake_bin.mkdir()
            fake_ccc = fake_bin / "ccc"
            fake_run_dir = tmp_path / "ccc-run"
            fake_ccc.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import os
                    import sys
                    from pathlib import Path

                    run_dir = Path(os.environ["CCC_FAKE_RUN_DIR"])
                    run_dir.mkdir(parents=True, exist_ok=True)
                    (run_dir / "output.txt").write_text("large line survived\\n", encoding="utf-8")
                    event = {"type": "text", "part": {"text": "x" * 70000}}
                    sys.stdout.write(json.dumps(event) + "\\n")
                    sys.stderr.write(f">> ccc:output-log >> {run_dir}\\n")
                    """
                ),
                encoding="utf-8",
            )
            fake_ccc.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            env["CCC_FAKE_RUN_DIR"] = str(fake_run_dir)
            launched = self.run_script(
                "workflow_run.py",
                "--title",
                "Large Jsonl Line",
                "--cwd",
                str(ROOT),
                "--runner",
                "ccc-opencode",
                "--startup-delay",
                "0",
                "--job",
                "alpha::Alpha prompt",
                env=env,
            )
            run_id = json.loads(launched.stdout.split("\ncommand:", 1)[0])["run_id"]
            data = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
            agent = data["agents"][0]
            self.assertEqual(data["status"], "completed")
            self.assertEqual(agent["result"], "large line survived\n")
            self.assertGreater(Path(data["agents"][0]["jsonl_path"]).stat().st_size, 64 * 1024)

    def test_runner_captures_stderr_lines_larger_than_asyncio_line_limit(self) -> None:
        """Capture large stderr records without readline-limit failures."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "bin"
            fake_bin.mkdir()
            fake_ccc = fake_bin / "ccc"
            fake_run_dir = tmp_path / "ccc-run"
            fake_ccc.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import os
                    import sys
                    from pathlib import Path

                    run_dir = Path(os.environ["CCC_FAKE_RUN_DIR"])
                    run_dir.mkdir(parents=True, exist_ok=True)
                    (run_dir / "output.txt").write_text("large stderr survived\\n", encoding="utf-8")
                    (run_dir / "transcript.txt").write_text("[assistant] ok\\n", encoding="utf-8")
                    sys.stderr.write("e" * 70000 + "\\n")
                    sys.stderr.write(f">> ccc:output-log >> {run_dir}\\n")
                    """
                ),
                encoding="utf-8",
            )
            fake_ccc.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            env["CCC_FAKE_RUN_DIR"] = str(fake_run_dir)
            launched = self.run_script(
                "workflow_run.py",
                "--title",
                "Large Stderr Line",
                "--cwd",
                str(ROOT),
                "--runner",
                "ccc-opencode",
                "--startup-delay",
                "0",
                "--job",
                "alpha::Alpha prompt",
                env=env,
            )
            run_id = json.loads(launched.stdout.split("\ncommand:", 1)[0])["run_id"]
            data = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
            agent = data["agents"][0]
            self.assertEqual(data["status"], "completed")
            self.assertEqual(agent["result"], "large stderr survived\n")
            self.assertGreater(Path(agent["log_path"]).stat().st_size, 64 * 1024)

    def test_terminate_process_group_stops_worker_children(self) -> None:
        """Ensure failed stream handling can clean up the launched worker group."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_run_codex  # pylint: disable=import-outside-toplevel

        async def run_case() -> int:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "import time; time.sleep(60)",
                start_new_session=True,
            )
            await workflow_run_codex.terminate_process_group(proc, grace_seconds=0.2)
            return await proc.wait()

        self.assertNotEqual(asyncio.run(run_case()), 0)

    @slow_test
    def test_stop_promptly_terminates_in_flight_worker(self) -> None:
        """A cooperative wf stop must terminate a running worker process."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "bin"
            fake_bin.mkdir()
            fake_codex = fake_bin / "codex"
            fake_codex.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import signal
                    import sys
                    import time

                    def on_term(signum, frame):
                        sys.exit(0)

                    signal.signal(signal.SIGTERM, on_term)
                    time.sleep(60)
                    sys.stdout.write("should not finish\\n")
                    """
                ),
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            command = [
                sys.executable,
                str(SCRIPTS / "workflow_run.py"),
                "--title",
                "Stop In Flight",
                "--cwd",
                str(ROOT),
                "--runner",
                "codex-direct",
                "--startup-delay",
                "0",
                "--timeout-secs",
                "60",
                "--job",
                "alpha::Alpha prompt",
            ]
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            run_id: str | None = None
            try:
                state_dir = tmp_path / "state" / "runs"
                for _ in range(50):
                    if proc.poll() is not None:
                        break
                    if run_id is None and state_dir.exists():
                        run_dirs = [d for d in state_dir.iterdir() if d.is_dir()]
                        if run_dirs:
                            candidate = run_dirs[0] / "run.json"
                            if candidate.exists():
                                run_id = json.loads(candidate.read_text(encoding="utf-8"))["run_id"]
                    if run_id:
                        run = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
                        if run["agents"] and run["agents"][0].get("status") == "running":
                            break
                    time.sleep(0.05)
                self.assertIsNotNone(run_id)
                self.run_script("workflow_state.py", "stop", run_id, "--no-terminate", env=env)
                stdout, stderr = proc.communicate(timeout=10)
                self.assertNotEqual(proc.returncode, 0, msg=f"stdout={stdout}\nstderr={stderr}")
                run = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
                self.assertEqual(run["status"], "cancelled")
                self.assertEqual(run["agents"][0]["status"], "cancelled")
                self.assertIn("stop request", run["agents"][0]["summary"].lower())
            except Exception:
                proc.kill()
                raise

    def test_telemetry_fields_are_optional_when_tui_parser_cannot_load(self) -> None:
        """Do not fail workers when the optional TUI telemetry parser is unavailable."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_run_codex  # pylint: disable=import-outside-toplevel

        original_import = __import__

        def failing_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "workflow_tui":
                raise ModuleNotFoundError("workflow_tui")
            return original_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=failing_import):
            self.assertEqual(workflow_run_codex.telemetry_fields_for_agent({}), {})

    def test_quota_retry_waits_until_next_half_hour_window(self) -> None:
        """Retry quota-limit failures at the next :00 or :30 wall-clock window."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_run_codex

        before_half = datetime(2026, 6, 13, 3, 10, 0, tzinfo=timezone.utc)
        after_half = datetime(2026, 6, 13, 3, 45, 0, tzinfo=timezone.utc)
        self.assertEqual(workflow_run_codex.seconds_until_next_quota_window(before_half, buffer_seconds=5), 20 * 60 + 5)
        self.assertEqual(workflow_run_codex.seconds_until_next_quota_window(after_half, buffer_seconds=5), 15 * 60 + 5)
        self.assertTrue(workflow_run_codex.quota_limit_detected("provider returned 429 usage limit for this period"))

    def test_quota_retry_detection_uses_only_current_attempt_output(self) -> None:
        """Do not let a previous quota line make a later unrelated failure retry."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_run_codex

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "worker.stderr.log"
            log_path.write_text("429 usage limit for this period\n", encoding="utf-8")
            offset = workflow_run_codex.file_size(log_path)
            log_path.write_text(log_path.read_text(encoding="utf-8") + "syntax error\n", encoding="utf-8")
            current_attempt = workflow_run_codex.read_text_from_offset(log_path, offset)
            self.assertEqual(current_attempt, "syntax error\n")
            self.assertFalse(workflow_run_codex.quota_limit_detected(current_attempt))

    def test_ccc_runner_retries_once_after_quota_limit(self) -> None:
        """Recover a ccc-backed worker when the first attempt exits with quota text."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "bin"
            fake_bin.mkdir()
            fake_ccc = fake_bin / "ccc"
            fake_ccc.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import os
                    import sys
                    from pathlib import Path

                    counter = Path(os.environ["CCC_COUNTER"])
                    count = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
                    count += 1
                    counter.write_text(str(count), encoding="utf-8")
                    if count == 1:
                        stale_dir = Path(os.environ["CCC_STALE_RUN_DIR"])
                        stale_dir.mkdir(parents=True, exist_ok=True)
                        (stale_dir / "output.txt").write_text("stale quota output\\n", encoding="utf-8")
                        (stale_dir / "transcript.txt").write_text("[assistant] stale quota output\\n", encoding="utf-8")
                        sys.stderr.write("429 usage limit for this period\\n")
                        sys.stderr.write(f">> ccc:output-log >> {stale_dir}\\n")
                        raise SystemExit(1)
                    run_dir = Path(os.environ["CCC_RUN_DIR"])
                    run_dir.mkdir(parents=True, exist_ok=True)
                    (run_dir / "output.txt").write_text("recovered after quota\\n", encoding="utf-8")
                    (run_dir / "transcript.txt").write_text("[assistant] recovered after quota\\n", encoding="utf-8")
                    sys.stdout.write("[assistant] recovered after quota\\n")
                    sys.stderr.write(f">> ccc:output-log >> {run_dir}\\n")
                    """
                ),
                encoding="utf-8",
            )
            fake_ccc.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            env["WORKFLOW_QUOTA_RETRY_SLEEP_OVERRIDE_SECS"] = "0"
            env["CCC_COUNTER"] = str(tmp_path / "counter.txt")
            env["CCC_STALE_RUN_DIR"] = str(tmp_path / "ccc-stale-run")
            env["CCC_RUN_DIR"] = str(tmp_path / "ccc-run")
            launched = self.run_script(
                "workflow_run.py",
                "--title",
                "Quota Retry",
                "--cwd",
                str(ROOT),
                "--runner",
                "ccc",
                "--ccc-runner",
                "@kimi",
                "--startup-delay",
                "0",
                "--quota-retries",
                "1",
                "--job",
                "alpha::Alpha prompt",
                env=env,
            )
            run_id = json.loads(launched.stdout.split("\ncommand:", 1)[0])["run_id"]
            data = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
            agent = data["agents"][0]
            self.assertEqual(data["status"], "completed")
            self.assertEqual((tmp_path / "counter.txt").read_text(encoding="utf-8"), "2")
            self.assertEqual(agent["quota_retry_count"], 1)
            self.assertEqual(agent["result"], "recovered after quota\n")
            self.assertEqual(Path(agent["output_path"]).read_text(encoding="utf-8"), "recovered after quota\n")
            self.assertIn("429 usage limit", Path(agent["log_path"]).read_text(encoding="utf-8"))

    @slow_test
    def test_ccc_runner_retries_once_after_non_quota_failure(self) -> None:
        """Recover from one transient provider failure without quota backoff."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "bin"
            fake_bin.mkdir()
            fake_ccc = fake_bin / "ccc"
            fake_ccc.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import os
                    import sys
                    from pathlib import Path

                    counter = Path(os.environ["CCC_COUNTER"])
                    count = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
                    count += 1
                    counter.write_text(str(count), encoding="utf-8")
                    if count == 1:
                        sys.stdout.write('{"type":"error","error":{"name":"UnknownError","data":{"message":"Failed to execute statement"}}}\\n')
                        raise SystemExit(1)
                    run_dir = Path(os.environ["CCC_RUN_DIR"])
                    run_dir.mkdir(parents=True, exist_ok=True)
                    (run_dir / "output.txt").write_text("recovered after transient failure\\n", encoding="utf-8")
                    (run_dir / "transcript.txt").write_text("[assistant] recovered after transient failure\\n", encoding="utf-8")
                    sys.stdout.write("[assistant] recovered after transient failure\\n")
                    sys.stderr.write(f">> ccc:output-log >> {run_dir}\\n")
                    """
                ),
                encoding="utf-8",
            )
            fake_ccc.chmod(0o755)
            env = self.fast_timing_env(os.environ.copy())
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            env["CCC_COUNTER"] = str(tmp_path / "counter.txt")
            env["CCC_RUN_DIR"] = str(tmp_path / "ccc-run")
            launched = self.run_script(
                "workflow_run.py",
                "--title",
                "Failure Retry",
                "--cwd",
                str(ROOT),
                "--runner",
                "ccc-opencode",
                "--startup-delay",
                "0",
                "--failure-retries",
                "1",
                "--job",
                "alpha::Alpha prompt",
                env=env,
            )
            run_id = json.loads(launched.stdout.split("\ncommand:", 1)[0])["run_id"]
            data = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
            agent = data["agents"][0]
            self.assertEqual(data["status"], "completed")
            self.assertEqual((tmp_path / "counter.txt").read_text(encoding="utf-8"), "2")
            self.assertEqual(agent["failure_retry_count"], 1)
            self.assertEqual(agent["result"], "recovered after transient failure\n")

    @slow_test
    def test_worker_timeout_marks_agent_failed(self) -> None:
        """A worker that exceeds --timeout-secs is terminated and marked failed."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "bin"
            fake_bin.mkdir()
            fake_ccc = fake_bin / "ccc"
            fake_ccc.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import signal
                    import sys
                    import time

                    def on_term(signum, frame):
                        sys.exit(0)

                    signal.signal(signal.SIGTERM, on_term)
                    time.sleep(10)
                    sys.stdout.write("should not finish\\n")
                    """
                ),
                encoding="utf-8",
            )
            fake_ccc.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            launched = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "workflow_run.py"),
                    "--title",
                    "Worker Timeout",
                    "--cwd",
                    str(ROOT),
                    "--runner",
                    "ccc-opencode",
                    "--startup-delay",
                    "0",
                    "--timeout-secs",
                    "1",
                    "--job",
                    "alpha::Alpha prompt",
                ],
                check=False,
                text=True,
                capture_output=True,
                env=env,
            )
            run_id = json.loads(launched.stdout.split("\ncommand:", 1)[0])["run_id"]
            data = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
            agent = data["agents"][0]
            self.assertEqual(data["status"], "failed")
            self.assertEqual(agent["status"], "failed")
            self.assertEqual(agent["exit_code"], 124)
            self.assertIn("timeout", agent["summary"].lower())

    @slow_test
    def test_worker_timeout_is_retried_with_failure_retries(self) -> None:
        """A timed-out worker is retried when --failure-retries is configured."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "bin"
            fake_bin.mkdir()
            fake_ccc = fake_bin / "ccc"
            fake_ccc.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import os
                    import signal
                    import sys
                    import time
                    from pathlib import Path

                    def on_term(signum, frame):
                        sys.exit(0)

                    signal.signal(signal.SIGTERM, on_term)
                    counter = Path(os.environ["CCC_COUNTER"])
                    count = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
                    count += 1
                    counter.write_text(str(count), encoding="utf-8")
                    if count == 1:
                        time.sleep(10)
                        sys.stdout.write("should not finish\\n")
                        sys.exit(1)
                    run_dir = Path(os.environ["CCC_RUN_DIR"])
                    run_dir.mkdir(parents=True, exist_ok=True)
                    (run_dir / "output.txt").write_text("recovered after timeout\\n", encoding="utf-8")
                    (run_dir / "transcript.txt").write_text("[assistant] recovered after timeout\\n", encoding="utf-8")
                    sys.stdout.write("[assistant] recovered after timeout\\n")
                    sys.stderr.write(f">> ccc:output-log >> {run_dir}\\n")
                    """
                ),
                encoding="utf-8",
            )
            fake_ccc.chmod(0o755)
            env = self.fast_timing_env(os.environ.copy())
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            env["CCC_COUNTER"] = str(tmp_path / "counter.txt")
            env["CCC_RUN_DIR"] = str(tmp_path / "ccc-run")
            launched = self.run_script(
                "workflow_run.py",
                "--title",
                "Timeout Retry",
                "--cwd",
                str(ROOT),
                "--runner",
                "ccc-opencode",
                "--startup-delay",
                "0",
                "--timeout-secs",
                "1",
                "--failure-retries",
                "1",
                "--job",
                "alpha::Alpha prompt",
                env=env,
            )
            run_id = json.loads(launched.stdout.split("\ncommand:", 1)[0])["run_id"]
            data = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
            agent = data["agents"][0]
            self.assertEqual(data["status"], "completed")
            self.assertEqual((tmp_path / "counter.txt").read_text(encoding="utf-8"), "2")
            self.assertEqual(agent["failure_retry_count"], 1)
            self.assertEqual(agent["result"], "recovered after timeout\n")

    def test_event_log_writes_rollover_marker_at_cap(self) -> None:
        """add_event must not silently drop old events; it should record a rollover."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_state  # pylint: disable=import-outside-toplevel

        run: dict[str, Any] = {
            "run_id": "wf-events",
            "events": [],
            "metrics": {},
            "paths": {"run_json": "/dev/null"},
        }
        for index in range(251):
            workflow_state.add_event(run, "info", f"event {index}")
        self.assertEqual(len(run["events"]), 250)
        rollover = next(event for event in run["events"] if event.get("kind") == "event-log")
        self.assertEqual(rollover["level"], "warning")
        self.assertIn("rolled over", rollover["message"])
        retained_messages = [event["message"] for event in run["events"] if event.get("kind") != "event-log"]
        self.assertEqual(retained_messages[0], "event 2")
        self.assertEqual(retained_messages[-1], "event 250")

    def test_mock_plan_records_truncation_when_max_jobs_cuts_list(self) -> None:
        """Planner output truncated by --max-jobs is recorded as a decision and event."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            launched = self.run_script(
                "workflow_start.py",
                "write a phd thesis",
                "--title",
                "Truncated Plan",
                "--mock-plan",
                "--mock",
                "--max-jobs",
                "2",
                "--startup-delay",
                "0",
                env=env,
            )
            run_id = json.loads(launched.stdout.split("\ncommand:", 1)[0])["run_id"]
            data = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
            self.assertEqual(len(data["agents"]), 2)
            decision_titles = [decision["title"] for decision in data["decisions"]]
            self.assertIn("Planner job list truncated", decision_titles)
            self.assertTrue(any(event.get("kind") == "planning" and event.get("operation") == "truncated" for event in data["events"]))

    def test_bare_prompt_job_name_is_stable_sha1(self) -> None:
        """parse_job without '::' must use a stable, collision-resistant id."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_run_codex  # pylint: disable=import-outside-toplevel

        prompt = "find bugs in the auth module"
        job = workflow_run_codex.parse_job(prompt)
        expected = f"job-{hashlib.sha1(prompt.encode('utf-8')).hexdigest()[:8]}"
        self.assertEqual(job["name"], expected)
        self.assertEqual(job["prompt"], prompt)
        # Same prompt yields same name across calls.
        self.assertEqual(workflow_run_codex.parse_job(prompt)["name"], expected)

    def test_parse_job_json_object_carries_stage_and_depends_on(self) -> None:
        """JSON --job values should preserve stage/depends_on metadata."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_run_codex  # pylint: disable=import-outside-toplevel

        raw = json.dumps(
            {
                "name": "reviewer",
                "role": "review",
                "prompt": "Review the plan.",
                "stage": "stage-2",
                "depends_on": "planner",
            }
        )
        job = workflow_run_codex.parse_job(raw)
        self.assertEqual(job["name"], "reviewer")
        self.assertEqual(job["stage"], "stage-2")
        self.assertEqual(job["depends_on"], "planner")

    def test_pipeline_respects_dependencies_across_stages(self) -> None:
        """A multi-stage pipeline completes with dependent jobs advancing independently."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env = self.codex_fake_env(tmp_path)
            env["CODEX_FAKE_OUTPUT"] = "done"
            launched = self.run_script(
                "workflow_run.py",
                "--title",
                "Pipeline",
                "--cwd",
                str(ROOT),
                "--runner",
                "codex-direct",
                "--startup-delay",
                "0",
                "--job",
                json.dumps({"name": "a", "prompt": "A", "stage": "1"}),
                "--job",
                json.dumps({"name": "b", "prompt": "B", "stage": "2", "depends_on": "a"}),
                "--job",
                json.dumps({"name": "c", "prompt": "C", "stage": "3", "depends_on": "b"}),
                env=env,
            )
            run_id = json.loads(launched.stdout.split("\ncommand:", 1)[0])["run_id"]
            data = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
            self.assertEqual(data["status"], "completed")
            by_name = {agent["name"]: agent for agent in data["agents"]}
            self.assertEqual(len(by_name), 3)
            for name in ("a", "b", "c"):
                self.assertEqual(by_name[name]["status"], "completed")
            # Dependencies enforced order: a before b before c.
            self.assertLessEqual(
                datetime.fromisoformat(by_name["a"]["completed_at"]),
                datetime.fromisoformat(by_name["b"]["started_at"]),
            )
            self.assertLessEqual(
                datetime.fromisoformat(by_name["b"]["completed_at"]),
                datetime.fromisoformat(by_name["c"]["started_at"]),
            )

    def test_expansion_envelope_enqueues_and_runs_new_jobs(self) -> None:
        """A worker returning a workflow-expansion envelope adds new jobs mid-run."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env = self.codex_fake_env(tmp_path)
            envelope = json.dumps(
                {
                    "kind": "workflow-expansion",
                    "schema_version": 1,
                    "jobs": [
                        {"name": "child-1", "prompt": "Child one"},
                        {"name": "child-2", "prompt": "Child two"},
                    ],
                }
            )
            env["CODEX_FAKE_OUTPUT"] = envelope
            launched = self.run_script(
                "workflow_run.py",
                "--title",
                "Expansion",
                "--cwd",
                str(ROOT),
                "--runner",
                "codex-direct",
                "--startup-delay",
                "0",
                "--max-round",
                "2",
                "--job",
                json.dumps({"name": "parent", "prompt": "Expand"}),
                env=env,
            )
            run_id = json.loads(launched.stdout.split("\ncommand:", 1)[0])["run_id"]
            data = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
            self.assertEqual(data["status"], "completed")
            names = {agent["name"] for agent in data["agents"]}
            self.assertEqual(names, {"parent", "child-1", "child-2"})
            self.assertTrue(
                any(
                    event.get("kind") == "expansion" and event.get("operation") == "added" and event.get("data", {}).get("added") == 2
                    for event in data["events"]
                )
            )

    def test_expansion_caps_hold_and_are_logged(self) -> None:
        """max-job and max-round caps truncate expansion and log a warning."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env = self.codex_fake_env(tmp_path)
            envelope = json.dumps(
                {
                    "kind": "workflow-expansion",
                    "schema_version": 1,
                    "jobs": [{"name": f"child-{index}", "prompt": f"Child {index}"} for index in range(5)],
                }
            )
            env["CODEX_FAKE_OUTPUT"] = envelope
            launched = self.run_script(
                "workflow_run.py",
                "--title",
                "Expansion Caps",
                "--cwd",
                str(ROOT),
                "--runner",
                "codex-direct",
                "--startup-delay",
                "0",
                "--max-job",
                "3",
                "--max-round",
                "2",
                "--job",
                json.dumps({"name": "parent", "prompt": "Expand"}),
                env=env,
            )
            run_id = json.loads(launched.stdout.split("\ncommand:", 1)[0])["run_id"]
            data = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
            self.assertEqual(data["status"], "completed")
            self.assertEqual(len(data["agents"]), 3)
            truncation_events = [
                event
                for event in data["events"]
                if event.get("kind") == "expansion" and event.get("operation") == "truncated"
            ]
            self.assertTrue(truncation_events)
            self.assertTrue(
                any(event["data"]["cap"] == "max-job" and event["data"]["dropped"] == 3 for event in truncation_events),
                msg=f"expected max-job truncation with 3 dropped, got {truncation_events}",
            )

    def test_schema_validation_passes_and_stores_result_json(self) -> None:
        """A worker output matching --result-schema stores the parsed object."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            schema_path = tmp_path / "schema.json"
            schema_path.write_text(
                json.dumps({"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}),
                encoding="utf-8",
            )
            env = self.codex_fake_env(tmp_path)
            env["CODEX_FAKE_OUTPUT"] = json.dumps({"answer": "yes"})
            launched = self.run_script(
                "workflow_run.py",
                "--title",
                "Schema Valid",
                "--cwd",
                str(ROOT),
                "--runner",
                "codex-direct",
                "--startup-delay",
                "0",
                "--result-schema",
                str(schema_path),
                "--job",
                "ask::Return JSON.",
                env=env,
            )
            run_id = json.loads(launched.stdout.split("\ncommand:", 1)[0])["run_id"]
            data = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
            self.assertEqual(data["status"], "completed")
            agent = data["agents"][0]
            self.assertEqual(agent["status"], "completed")
            self.assertEqual(agent["result_json"], {"answer": "yes"})

    def test_schema_validation_failure_marks_agent_failed(self) -> None:
        """A worker output that violates the schema is marked failed."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            schema_path = tmp_path / "schema.json"
            schema_path.write_text(
                json.dumps({"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}),
                encoding="utf-8",
            )
            env = self.codex_fake_env(tmp_path)
            env["CODEX_FAKE_OUTPUT"] = "not valid json"
            launched = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "workflow_run.py"),
                    "--title",
                    "Schema Invalid",
                    "--cwd",
                    str(ROOT),
                    "--runner",
                    "codex-direct",
                    "--startup-delay",
                    "0",
                    "--result-schema",
                    str(schema_path),
                    "--job",
                    "ask::Return JSON.",
                ],
                check=False,
                text=True,
                capture_output=True,
                env=env,
            )
            run_id = json.loads(launched.stdout.split("\ncommand:", 1)[0])["run_id"]
            data = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
            self.assertEqual(data["status"], "failed")
            agent = data["agents"][0]
            self.assertEqual(agent["status"], "failed")
            self.assertIn("schema validation failed", agent["result"])

    @slow_test
    def test_schema_validation_failure_is_retried_with_error_in_prompt(self) -> None:
        """Schema failures consume failure retries and include the error in the retry prompt."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            schema_path = tmp_path / "schema.json"
            schema_path.write_text(
                json.dumps({"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}),
                encoding="utf-8",
            )
            state_path = tmp_path / "codex-state.txt"
            state_path.write_text("0", encoding="utf-8")
            env = self.codex_fake_env(tmp_path)
            env["CODEX_FAKE_STATE"] = str(state_path)
            env["CODEX_FAKE_OUTPUT_0"] = json.dumps({"answer": 123})
            env["CODEX_FAKE_OUTPUT_1"] = json.dumps({"answer": "yes"})
            launched = self.run_script(
                "workflow_run.py",
                "--title",
                "Schema Retry",
                "--cwd",
                str(ROOT),
                "--runner",
                "codex-direct",
                "--startup-delay",
                "0",
                "--failure-retries",
                "1",
                "--result-schema",
                str(schema_path),
                "--job",
                "ask::Return JSON.",
                env=env,
            )
            run_id = json.loads(launched.stdout.split("\ncommand:", 1)[0])["run_id"]
            data = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
            self.assertEqual(data["status"], "completed")
            agent = data["agents"][0]
            self.assertEqual(agent["status"], "completed")
            self.assertEqual(agent["result_json"], {"answer": "yes"})
            self.assertEqual(agent["failure_retry_count"], 1)

    @slow_test
    def test_generic_ccc_runner_accepts_presets_and_cli_names(self) -> None:
        """Allow --ccc-runner to target both @presets and plain ccc CLI selectors."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env = self.ccc_fake_env(tmp_path)
            for target, expected_mode, expected_type, expected_arg in (
                ("@mm", "ccc-mm", "ccc-preset-mm", "@mm"),
                ("kimi", "ccc-kimi", "ccc-runner-kimi", "kimi"),
            ):
                with self.subTest(target=target):
                    launched = self.run_script(
                        "workflow_run.py",
                        "--title",
                        f"Fake Ccc {target}",
                        "--cwd",
                        str(ROOT),
                        "--runner",
                        "ccc",
                        "--ccc-runner",
                        target,
                        "--startup-delay",
                        "0",
                        "--job",
                        "alpha::Alpha prompt",
                        env=env,
                    )
                    run_id = json.loads(launched.stdout.split("\ncommand:", 1)[0])["run_id"]
                    data = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
                    agent = data["agents"][0]
                    self.assertEqual(data["mode"], expected_mode)
                    self.assertEqual(agent["agent_type"], expected_type)
                    self.assertIn(expected_arg, agent["command_preview"])
                    self.assertIn("fake ccc result", agent["result"])

            start_args = [event["args"] for event in self.read_ccc_events(tmp_path) if event["event"] == "start"]
            for expected_arg in ("@mm", "kimi"):
                self.assertTrue(any(expected_arg in args for args in start_args), msg=f"missing {expected_arg} in {start_args}")

    def test_ccc_claude_selector_forwards_cwd(self) -> None:
        """The 'claude' ccc selector should receive explicit cwd forwarding."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env = self.ccc_fake_env(tmp_path)
            launched = self.run_script(
                "workflow_run.py",
                "--title",
                "Fake Ccc Claude",
                "--cwd",
                str(ROOT),
                "--runner",
                "ccc",
                "--ccc-runner",
                "claude",
                "--startup-delay",
                "0",
                "--job",
                "alpha::Alpha prompt",
                env=env,
            )
            run_id = json.loads(launched.stdout.split("\ncommand:", 1)[0])["run_id"]
            data = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
            agent = data["agents"][0]
            self.assertEqual(data["mode"], "ccc-claude")
            self.assertIn("--cd", agent["command_preview"])
            start_args = [event["args"] for event in self.read_ccc_events(tmp_path) if event["event"] == "start"]
            self.assertTrue(any("--runner-arg" in args and "--cd" in args for args in start_args))

    @slow_test
    def test_ccc_kimi_runner_passes_large_kimi_step_limit(self) -> None:
        """Raise Kimi's per-turn step/tool-call limit for ccc-backed Kimi workers."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env = self.ccc_fake_env(tmp_path)
            for selector in ("@kimi", "kimi", "k"):
                with self.subTest(selector=selector):
                    self.run_script(
                        "workflow_run.py",
                        "--title",
                        f"Fake Ccc Kimi {selector}",
                        "--cwd",
                        str(ROOT),
                        "--runner",
                        "ccc",
                        "--ccc-runner",
                        selector,
                        "--startup-delay",
                        "0",
                        "--job",
                        "alpha::Alpha prompt",
                        env=env,
            )
            start_args = [event["args"] for event in self.read_ccc_events(tmp_path) if event["event"] == "start"]
            def has_subsequence(args: list[str], expected: list[str]) -> bool:
                return any(args[index : index + len(expected)] == expected for index in range(0, len(args) - len(expected) + 1))

            for selector in ("@kimi", "kimi", "k"):
                matching = [args for args in start_args if selector in args]
                self.assertTrue(matching, msg=f"missing start for {selector}: {start_args}")
                self.assertTrue(
                    any(has_subsequence(args, ["--runner-arg", "--max-steps-per-turn", "--runner-arg", "9999"]) for args in matching),
                    msg=f"missing Kimi step limit for {selector}: {matching}",
                )
                self.assertTrue(any("--work-dir" in args and str(ROOT) in args for args in matching), msg=f"missing Kimi work dir for {selector}: {matching}")

    def test_ccc_opencode_presets_receive_explicit_workdir(self) -> None:
        """Keep ccc/OpenCode workers inside the workflow target cwd."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env = self.ccc_fake_env(tmp_path)
            target_cwd = tmp_path / "target-workdir"
            target_cwd.mkdir()
            self.run_script(
                "workflow_run.py",
                "--title",
                "Fake Ccc Mimo",
                "--cwd",
                str(target_cwd),
                "--runner",
                "ccc",
                "--ccc-runner",
                "@mimo25p",
                "--startup-delay",
                "0",
                "--job",
                "alpha::Alpha prompt",
                env=env,
            )
            start_args = [event["args"] for event in self.read_ccc_events(tmp_path) if event["event"] == "start"]
            self.assertTrue(start_args)
            self.assertTrue(any("--runner-arg" in args and "--dir" in args and str(target_cwd) in args for args in start_args), msg=start_args)

    def test_start_mock_plan_launches_mock_workflow_from_goal(self) -> None:
        """Create a workflow from one natural-language goal without model calls."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            launched = self.run_script(
                "workflow_start.py",
                "write a phd thesis on a new improved construction theory for the pyramids",
                "--title",
                "Pyramid Thesis",
                "--mock",
                "--startup-delay",
                "0",
                env=env,
            )
            created = json.loads(launched.stdout.split("\ncommand:", 1)[0])
            run_id = created["run_id"]
            self.assertEqual(created["jobs"], 4)
            self.assertEqual(created["planner"], "mock")
            data = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
            self.assertEqual(data["status"], "completed")
            self.assertEqual(data["title"], "Pyramid Thesis")
            self.assertEqual(data["prompt"], "write a phd thesis on a new improved construction theory for the pyramids")
            self.assertEqual(data["metrics"]["agents_total"], 4)
            self.assertEqual({agent["status"] for agent in data["agents"]}, {"completed"})
            self.assertTrue(any(decision["title"] == "Workflow start plan generated" for decision in data["decisions"]))
            plan_artifacts = [artifact for artifact in data["artifacts"] if artifact["kind"] == "generated-plan"]
            self.assertEqual(len(plan_artifacts), 1)
            plan = json.loads(Path(plan_artifacts[0]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(plan["goal"], "write a phd thesis on a new improved construction theory for the pyramids")
            self.assertEqual([job["name"] for job in plan["jobs"]], ["research", "design", "draft", "review"])

    def test_wf_start_routes_to_start_script(self) -> None:
        """Expose the natural-language start command through the installed shell entrypoint."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            launched = self.run_wf(
                "start",
                "summarize the current repository architecture",
                "--title",
                "Repo Summary",
                "--mock",
                "--startup-delay",
                "0",
                env=env,
            )
            created = json.loads(launched.stdout.split("\ncommand:", 1)[0])
            self.assertEqual(created["jobs"], 4)
            data = json.loads(self.run_script("workflow_state.py", "show", created["run_id"], "--json", env=env).stdout)
            self.assertEqual(data["status"], "completed")
            self.assertEqual(data["title"], "Repo Summary")

    def test_start_extracts_planner_json_from_markdown_fence(self) -> None:
        """Accept common planner output with a fenced JSON object."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_start  # pylint: disable=import-outside-toplevel

        text = textwrap.dedent(
            """\
            Here is the plan:

            ```json
            {
              "title": "Example",
              "jobs": [
                {"name": "research", "role": "researcher", "prompt": "Research the topic."}
              ]
            }
            ```
            """
        )
        plan, truncation = workflow_start.parse_planner_output(text, goal="Research topic", max_jobs=4)
        self.assertEqual(plan["title"], "Example")
        self.assertEqual(plan["jobs"][0]["name"], "research")
        self.assertFalse(truncation["truncated"])

    def test_start_rejects_empty_goal(self) -> None:
        """Reject empty natural-language goals before creating workflow state."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            result = self.run_wf("start", "   ", "--mock", env=env, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("goal must not be empty", result.stderr)
            self.assertFalse((Path(tmp) / "state").exists())

    @slow_test
    def test_runner_respects_max_agents_limit(self) -> None:
        """Ensure the launcher never runs more than --max-agents workers concurrently."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env = self.ccc_fake_env(tmp_path)
            env["CCC_FAKE_SLEEP"] = "0.25"
            args = [
                "workflow_run.py",
                "--title",
                "Max Agent Gate",
                "--cwd",
                str(ROOT),
                "--runner",
                "ccc-opencode",
                "--max-agents",
                "2",
                "--startup-delay",
                "0",
            ]
            for index in range(5):
                args.extend(["--job", f"job-{index}::Prompt {index}"])
            self.run_script(*args, env=env)
            observed_max = int((tmp_path / "ccc-max.txt").read_text(encoding="utf-8"))
            self.assertEqual(observed_max, 2)

    @slow_test
    def test_runner_respects_startup_delay(self) -> None:
        """Ensure observable worker starts are paced rather than launched in a burst."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env = self.ccc_fake_env(tmp_path)
            env["CCC_FAKE_SLEEP"] = "0.01"
            args = [
                "workflow_run.py",
                "--title",
                "Startup Delay Gate",
                "--cwd",
                str(ROOT),
                "--runner",
                "ccc-opencode",
                "--max-agents",
                "3",
                "--startup-delay",
                "0.2",
            ]
            for index in range(3):
                args.extend(["--job", f"job-{index}::Prompt {index}"])
            self.run_script(*args, env=env)
            starts = [event for event in self.read_ccc_events(tmp_path) if event["event"] == "start"]
            gaps = [float(starts[index + 1]["time"]) - float(starts[index]["time"]) for index in range(len(starts) - 1)]
            self.assertEqual(len(starts), 3)
            self.assertTrue(all(gap >= 0.12 for gap in gaps), msg=f"startup gaps too small: {gaps}")

    def test_startup_limiter_wraps_actual_process_creation(self) -> None:
        """Ensure slow pre-launch work cannot consume the subprocess start delay."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_run_codex  # pylint: disable=import-outside-toplevel

        original_create = workflow_run_codex.asyncio.create_subprocess_exec
        launch_times: list[float] = []

        class DummyProcess:
            """Minimal process object returned by the monkeypatched launcher."""

            pid = 12345

        async def fake_create_subprocess_exec(*_command: str, **_kwargs: object) -> DummyProcess:
            launch_times.append(time.monotonic())
            await asyncio.sleep(0)
            return DummyProcess()

        async def scenario() -> None:
            limiter = workflow_run_codex.StartupRateLimiter(0.1)
            workflow_run_codex.asyncio.create_subprocess_exec = fake_create_subprocess_exec
            try:
                async def delayed_launch() -> None:
                    await asyncio.sleep(0.15)
                    await limiter.create_process(["fake-runner"])

                await asyncio.gather(delayed_launch(), delayed_launch(), delayed_launch())
            finally:
                workflow_run_codex.asyncio.create_subprocess_exec = original_create

        asyncio.run(scenario())
        gaps = [launch_times[index + 1] - launch_times[index] for index in range(len(launch_times) - 1)]
        self.assertEqual(len(launch_times), 3)
        self.assertTrue(all(gap >= 0.09 for gap in gaps), msg=f"process launch gaps too small: {gaps}")

    def test_concurrent_event_updates_are_not_lost(self) -> None:
        """Append events from many processes and verify per-run locking preserves them."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_script(
                "workflow_state.py",
                "init",
                "--title",
                "Concurrent Events",
                "--prompt",
                "exercise workflow locking",
                "--cwd",
                str(ROOT),
                env=env,
            )
            run_id = json.loads(created.stdout)["run_id"]
            procs: list[subprocess.Popen[str]] = []
            for index in range(24):
                command = [
                    sys.executable,
                    str(SCRIPTS / "workflow_state.py"),
                    "event",
                    run_id,
                    "--message",
                    f"event-{index:02d}",
                ]
                procs.append(subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env))
            for proc in procs:
                stdout, stderr = proc.communicate(timeout=20)
                self.assertEqual(proc.returncode, 0, msg=f"stdout={stdout}\nstderr={stderr}")
            shown = self.run_script("workflow_state.py", "show", run_id, "--json", env=env)
            data = json.loads(shown.stdout)
            messages = {event["message"] for event in data["events"]}
            expected = {f"event-{index:02d}" for index in range(24)}
            self.assertTrue(expected.issubset(messages))

    def test_missing_codex_binary_marks_worker_and_run_failed(self) -> None:
        """Ensure subprocess launch failures do not strand runs in running state."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            empty_path = Path(tmp) / "empty-bin"
            empty_path.mkdir()
            env["PATH"] = str(empty_path)
            launched = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "workflow_run_codex.py"),
                    "--title",
                    "Missing Codex",
                    "--cwd",
                    str(ROOT),
                    "--job",
                    "alpha::Alpha prompt",
                ],
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertEqual(launched.returncode, 1, msg=f"stdout={launched.stdout}\nstderr={launched.stderr}")
            run_id = json.loads(launched.stdout.split("\ncommand:", 1)[0])["run_id"]
            shown = self.run_script("workflow_state.py", "show", run_id, "--json", env=env)
            data = json.loads(shown.stdout)
            self.assertEqual(data["status"], "failed")
            self.assertEqual(data["agents"][0]["status"], "failed")
            self.assertTrue(data["agents"][0]["started_at"])
            self.assertTrue(data["agents"][0]["completed_at"])
            self.assertIn("FileNotFoundError", data["agents"][0]["summary"])

    def test_dry_run_records_completed_non_active_state(self) -> None:
        """Verify dry-run workers do not leave fake active workflow state behind."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            launched = self.run_script(
                "workflow_run_codex.py",
                "--title",
                "Dry Run Workers",
                "--cwd",
                str(ROOT),
                "--dry-run",
                "--job",
                "alpha::Alpha prompt",
                env=env,
            )
            run_id = json.loads(launched.stdout.split("\ndry run:", 1)[0])["run_id"]
            shown = self.run_script("workflow_state.py", "show", run_id, "--json", env=env)
            data = json.loads(shown.stdout)
            self.assertEqual(data["status"], "completed")
            self.assertEqual(data["phases"][0]["status"], "completed")
            self.assertEqual(data["agents"][0]["status"], "completed")
            self.assertIn("dry run", data["agents"][0]["summary"])
            self.assertEqual(data["agents"][0]["exit_code"], 0)

    def test_operator_preview_does_not_write_state(self) -> None:
        """Preview worker launches without creating workflow state."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            result = self.run_wf(
                "preview",
                "--title",
                "Preview Only",
                "--runner",
                "ccc-opencode",
                "--job",
                "alpha::Alpha prompt",
                env=env,
            )
            preview = json.loads(result.stdout)
            self.assertFalse(preview["writes_state"])
            self.assertEqual(preview["jobs"][0]["name"], "alpha")
            self.assertFalse((Path(tmp) / "state").exists())

    @slow_test
    def test_operator_verify_and_done_gate_completion(self) -> None:
        """Require passing verification before safe workflow completion."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_wf("init", "--title", "Verify Gate", "--prompt", "gate", "--cwd", str(ROOT), env=env)
            run_id = json.loads(created.stdout)["run_id"]
            self.run_wf("add-phase", run_id, "--phase-id", "phase-verify", "--name", "Verify", "--status", "completed", env=env)

            denied = self.run_wf("done", run_id, env=env, check=False)
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("verification-missing", denied.stdout)

            check = self.run_wf("verify", run_id, "--cmd", "python3 -c 'print(\"ok\")'", "--name", "unit smoke", env=env)
            check_data = json.loads(check.stdout)
            self.assertEqual(check_data["status"], "passed")
            completed = self.run_wf("done", run_id, env=env)
            self.assertIn("completed", completed.stdout)
            shown = self.run_script("workflow_state.py", "show", run_id, "--json", env=env)
            data = json.loads(shown.stdout)
            self.assertEqual(data["status"], "completed")
            self.assertEqual(data["metrics"]["checks_by_status"]["passed"], 1)

    def test_operator_check_rejects_invalid_top_level_run_status(self) -> None:
        """Validate the top-level run status, not only phases and agents."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_wf("init", "--title", "Bad Run Status", "--prompt", "bad", "--cwd", str(ROOT), env=env)
            created_data = json.loads(created.stdout)
            run_id = created_data["run_id"]
            run_path = Path(created_data["path"])
            data = json.loads(run_path.read_text(encoding="utf-8"))
            data["status"] = "bogus"
            run_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

            checked = self.run_wf("check", run_id, env=env, check=False)
            denied = self.run_wf("done", run_id, "--allow-unverified", env=env, check=False)

            self.assertNotEqual(checked.returncode, 0)
            self.assertIn("invalid-status", checked.stdout)
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("invalid-status", denied.stdout)

    def test_status_line_includes_structural_issues(self) -> None:
        """wf status must surface orphan links and invalid statuses that wf check enforces."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_wf("init", "--title", "Structural Status", "--prompt", "struct", "--cwd", str(ROOT), env=env)
            created_data = json.loads(created.stdout)
            run_id = created_data["run_id"]
            run_path = Path(created_data["path"])
            data = json.loads(run_path.read_text(encoding="utf-8"))
            data["phases"].append({
                "phase_id": "phase-orphan",
                "name": "Orphan",
                "status": "completed",
                "agent_ids": ["missing-agent"],
                "created_at": data["created_at"],
                "started_at": None,
                "completed_at": data["created_at"],
            })
            run_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

            status = self.run_wf("status", "--all", env=env)
            self.assertIn("Phase references missing agent", status.stdout)
            check = self.run_wf("check", run_id, env=env, check=False)
            self.assertIn("Phase references missing agent", check.stdout)

    def test_operator_done_evaluates_blockers_inside_mutation_lock(self) -> None:
        """Recheck completion blockers against the locked state just before writing done."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_ops  # pylint: disable=import-outside-toplevel

        original_load_run = workflow_ops.workflow_state.load_run
        original_mutate_run = workflow_ops.workflow_state.mutate_run
        base_run = {
            "run_id": "wf-race",
            "status": "running",
            "phases": [{"phase_id": "phase-done", "name": "Done", "status": "completed"}],
            "agents": [],
            "checks": [{"check_id": "chk-pass", "status": "passed", "required": True, "command": "true", "exit_code": 0, "summary": "pass"}],
            "paths": {"run_json": "/tmp/wf-race/run.json"},
        }
        locked_state = dict(base_run)
        locked_state["phases"] = [dict(base_run["phases"][0]), {"phase_id": "phase-race", "name": "Race", "status": "running"}]

        def fake_load_run(_identifier: str) -> dict[str, object]:
            return dict(base_run)

        def fake_mutate_run(_identifier: str, mutator: object) -> tuple[dict[str, object], object, Path]:
            try:
                result = mutator(locked_state)  # type: ignore[misc]
            except workflow_ops.workflow_state.AbortMutation as exc:
                result = exc.result
            return locked_state, result, Path("/tmp/wf-race/run.json")

        workflow_ops.workflow_state.load_run = fake_load_run  # type: ignore[assignment]
        workflow_ops.workflow_state.mutate_run = fake_mutate_run  # type: ignore[assignment]
        try:
            args = argparse.Namespace(run="wf-race", force=False, allow_unverified=False, json=False, reason=None, message=None)
            with self.assertRaises(SystemExit):
                workflow_ops.cmd_done(args)
        finally:
            workflow_ops.workflow_state.load_run = original_load_run  # type: ignore[assignment]
            workflow_ops.workflow_state.mutate_run = original_mutate_run  # type: ignore[assignment]

        self.assertEqual(locked_state["status"], "running")

    def test_operator_record_only_optional_skipped_check_exits_successfully(self) -> None:
        """Allow optional skipped evidence records without breaking automation."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_wf("init", "--title", "Skipped Optional", "--prompt", "skip", "--cwd", str(ROOT), env=env)
            run_id = json.loads(created.stdout)["run_id"]

            result = self.run_wf("verify", run_id, "--record-only", "--status", "skipped", "--optional", "--summary", "not applicable", "--external-ref", "https://example.invalid/skip", env=env)

            self.assertEqual(json.loads(result.stdout)["status"], "skipped")

    def test_operator_block_and_check_surface_reason(self) -> None:
        """Persist blocked reasons and expose them through check output."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_wf("init", "--title", "Blocked Run", "--prompt", "blocked", "--cwd", str(ROOT), env=env)
            run_id = json.loads(created.stdout)["run_id"]
            self.run_wf("block", run_id, "waiting for credentials", "--blocked-by", "operator", env=env)
            checked = self.run_wf("check", run_id, env=env)
            self.assertIn("run-blocked", checked.stdout)
            self.assertIn("waiting for credentials", checked.stdout)
            shown = self.run_script("workflow_state.py", "show", run_id, "--json", env=env)
            data = json.loads(shown.stdout)
            self.assertEqual(data["status"], "blocked")
            self.assertEqual(data["blocked_by"], "operator")

    def test_operator_done_rejects_blocked_runs_even_with_checks(self) -> None:
        """Prevent safe completion while a workflow is explicitly blocked."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_wf("init", "--title", "Blocked Done", "--prompt", "blocked", "--cwd", str(ROOT), env=env)
            run_id = json.loads(created.stdout)["run_id"]
            self.run_wf("add-phase", run_id, "--phase-id", "phase-done", "--name", "Done", "--status", "completed", env=env)
            self.run_wf("verify", run_id, "--record-only", "--status", "passed", "--summary", "manual pass", "--external-ref", "https://example.invalid/review", env=env)
            self.run_wf("block", run_id, "waiting for reviewer", env=env)

            denied = self.run_wf("done", run_id, env=env, check=False)

            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("run-blocked", denied.stdout)

    def test_operator_done_refusal_does_not_rewrite_run_state(self) -> None:
        """A refused completion should not refresh updated_at or save a no-op mutation."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_wf("init", "--title", "No Rewrite Done", "--prompt", "blocked", "--cwd", str(ROOT), env=env)
            created_data = json.loads(created.stdout)
            run_id = created_data["run_id"]
            run_path = Path(created_data["path"])
            self.run_wf("add-phase", run_id, "--phase-id", "phase-active", "--name", "Active", "--status", "running", env=env)
            self.run_wf("verify", run_id, "--record-only", "--status", "passed", "--summary", "manual pass", "--external-ref", "https://example.invalid/manual", env=env)
            before = run_path.read_text(encoding="utf-8")

            denied = self.run_wf("done", run_id, env=env, check=False)
            after = run_path.read_text(encoding="utf-8")

            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("phase-active", denied.stdout)
            self.assertEqual(after, before)

    def test_operator_verify_requires_explicit_evidence(self) -> None:
        """Reject bare manual verification records that would mint fake evidence."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_wf("init", "--title", "Evidence Gate", "--prompt", "evidence", "--cwd", str(ROOT), env=env)
            run_id = json.loads(created.stdout)["run_id"]
            self.run_wf("add-phase", run_id, "--phase-id", "phase-done", "--name", "Done", "--status", "completed", env=env)

            verify = self.run_wf("verify", run_id, env=env, check=False)
            done = self.run_wf("done", run_id, env=env, check=False)

            self.assertNotEqual(verify.returncode, 0)
            self.assertIn("requires --cmd", verify.stderr)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("verification-missing", done.stdout)

    def test_operator_record_only_verify_rejects_command(self) -> None:
        """Prevent record-only checks from implying a command was executed."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_wf("init", "--title", "Record Only Command", "--prompt", "record-only", "--cwd", str(ROOT), env=env)
            run_id = json.loads(created.stdout)["run_id"]
            self.run_wf("add-phase", run_id, "--phase-id", "phase-done", "--name", "Done", "--status", "completed", env=env)

            verify = self.run_wf("verify", run_id, "--record-only", "--cmd", "false", "--status", "passed", "--summary", "not run", env=env, check=False)
            done = self.run_wf("done", run_id, env=env, check=False)

            self.assertNotEqual(verify.returncode, 0)
            self.assertIn("--record-only cannot be combined with --cmd", verify.stderr)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("verification-missing", done.stdout)

    def test_operator_verify_rejects_status_override_for_executed_command(self) -> None:
        """Ensure executed checks cannot be manually overridden into passing evidence."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_wf("init", "--title", "Status Override", "--prompt", "override", "--cwd", str(ROOT), env=env)
            run_id = json.loads(created.stdout)["run_id"]
            self.run_wf("add-phase", run_id, "--phase-id", "phase-done", "--name", "Done", "--status", "completed", env=env)

            verify = self.run_wf("verify", run_id, "--cmd", "false", "--status", "passed", "--summary", "should not pass", env=env, check=False)
            done = self.run_wf("done", run_id, env=env, check=False)

            self.assertNotEqual(verify.returncode, 0)
            self.assertIn("--status is only valid with --record-only", verify.stderr)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("verification-missing", done.stdout)

    def test_operator_done_ignores_passed_command_check_with_nonzero_exit(self) -> None:
        """Reject legacy or hand-edited passed checks that contradict command exit status."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_wf("init", "--title", "Bad Legacy Check", "--prompt", "legacy", "--cwd", str(ROOT), env=env)
            created_data = json.loads(created.stdout)
            run_id = created_data["run_id"]
            self.run_wf("add-phase", run_id, "--phase-id", "phase-done", "--name", "Done", "--status", "completed", env=env)
            run_path = Path(created_data["path"])
            data = json.loads(run_path.read_text(encoding="utf-8"))
            data["checks"].append(
                {
                    "check_id": "chk-bad-legacy",
                    "ts": "2026-06-11T00:00:00Z",
                    "name": "bad legacy",
                    "kind": "verification",
                    "status": "passed",
                    "required": True,
                    "command": "false",
                    "exit_code": 1,
                    "summary": "bad override",
                    "completed_at": "2026-06-11T00:00:01Z",
                }
            )
            run_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

            denied = self.run_wf("done", run_id, env=env, check=False)

            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("verification-missing", denied.stdout)

    def test_operator_exact_later_pass_resolves_failed_check_identity(self) -> None:
        """Use check identity to supersede a failed command with a later passing same command."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_wf("init", "--title", "Exact Rerun", "--prompt", "rerun", "--cwd", str(ROOT), env=env)
            run_id = json.loads(created.stdout)["run_id"]
            self.run_wf("add-phase", run_id, "--phase-id", "phase-done", "--name", "Done", "--status", "completed", env=env)
            command_file = Path(tmp) / "exit-code.txt"
            command_file.write_text("1", encoding="utf-8")
            command = f"python3 -c \"import pathlib,sys; sys.exit(int(pathlib.Path({str(command_file)!r}).read_text()))\""

            self.run_wf("verify", run_id, "--cmd", command, "--name", "same check", env=env, check=False)
            command_file.write_text("0", encoding="utf-8")
            self.run_wf("verify", run_id, "--cmd", command, "--name", "same check", env=env)
            completed = self.run_wf("done", run_id, env=env)

            self.assertIn("completed", completed.stdout)

    def test_operator_done_rejects_manual_pass_without_summary(self) -> None:
        """Reject malformed commandless passed checks without external evidence text."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_wf("init", "--title", "Malformed Manual", "--prompt", "manual", "--cwd", str(ROOT), env=env)
            created_data = json.loads(created.stdout)
            run_id = created_data["run_id"]
            self.run_wf("add-phase", run_id, "--phase-id", "phase-done", "--name", "Done", "--status", "completed", env=env)
            run_path = Path(created_data["path"])
            data = json.loads(run_path.read_text(encoding="utf-8"))
            data["checks"].append(
                {
                    "check_id": "chk-empty-summary",
                    "ts": "2026-06-11T00:00:00Z",
                    "name": "empty manual",
                    "kind": "verification",
                    "status": "passed",
                    "required": True,
                    "command": "",
                    "exit_code": 0,
                    "summary": "",
                    "completed_at": "2026-06-11T00:00:01Z",
                }
            )
            run_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

            denied = self.run_wf("done", run_id, env=env, check=False)
            checked = self.run_wf("check", run_id, env=env, check=False)

            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("verification-missing", denied.stdout)
            self.assertNotEqual(checked.returncode, 0)
            self.assertIn("check-invalid", checked.stdout)

    def test_latest_check_uses_append_order_for_same_second_pass_then_fail(self) -> None:
        """Treat later same-second failed reruns as authoritative over earlier passes."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_health  # pylint: disable=import-outside-toplevel

        run = {
            "run_id": "wf-test",
            "checks": [
                {
                    "check_id": "chk-z",
                    "ts": "2026-06-11T00:00:00Z",
                    "completed_at": "2026-06-11T00:00:00Z",
                    "kind": "verification",
                    "name": "same",
                    "command": "python3 test.py",
                    "cwd": "/repo",
                    "status": "passed",
                    "required": True,
                    "exit_code": 0,
                    "summary": "passed",
                },
                {
                    "check_id": "chk-a",
                    "ts": "2026-06-11T00:00:00Z",
                    "completed_at": "2026-06-11T00:00:00Z",
                    "kind": "verification",
                    "name": "same",
                    "command": "python3 test.py",
                    "cwd": "/repo",
                    "status": "failed",
                    "required": True,
                    "exit_code": 1,
                    "summary": "failed",
                },
            ],
        }

        blockers = workflow_health.completion_blockers(run)

        self.assertTrue(any(item["kind"] == "check-failed" for item in blockers))
        self.assertFalse(workflow_health.passed_checks(run))

    def test_latest_check_uses_append_order_for_same_second_fail_then_pass(self) -> None:
        """Treat later same-second passing reruns as authoritative over earlier failures."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_health  # pylint: disable=import-outside-toplevel

        run = {
            "run_id": "wf-test",
            "checks": [
                {
                    "check_id": "chk-z",
                    "ts": "2026-06-11T00:00:00Z",
                    "completed_at": "2026-06-11T00:00:00Z",
                    "kind": "verification",
                    "name": "same",
                    "command": "python3 test.py",
                    "cwd": "/repo",
                    "status": "failed",
                    "required": True,
                    "exit_code": 1,
                    "summary": "failed",
                },
                {
                    "check_id": "chk-a",
                    "ts": "2026-06-11T00:00:00Z",
                    "completed_at": "2026-06-11T00:00:00Z",
                    "kind": "verification",
                    "name": "same",
                    "command": "python3 test.py",
                    "cwd": "/repo",
                    "status": "passed",
                    "required": True,
                    "exit_code": 0,
                    "summary": "passed",
                },
            ],
        }

        blockers = workflow_health.completion_blockers(run)

        self.assertFalse(any(item["kind"] == "check-failed" for item in blockers))
        self.assertTrue(workflow_health.passed_checks(run))

    def test_completed_phase_without_agents_warns_but_does_not_block(self) -> None:
        """A completed phase with no agents/artifacts surfaces a non-blocking phase-empty nudge."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_health  # pylint: disable=import-outside-toplevel

        run = {
            "run_id": "wf-test",
            "status": "running",
            "phases": [{"phase_id": "phase-impl", "name": "Implementation", "status": "completed"}],
            "agents": [],
            "artifacts": [],
        }

        findings = workflow_health.analyze_run(run)
        self.assertTrue(any(item["kind"] == "phase-empty" for item in findings))
        empty = next(item for item in findings if item["kind"] == "phase-empty")
        self.assertEqual(empty["severity"], workflow_health.WARNING)

        # It is a nudge, not a gate: it must not block `wf done`.
        blockers = workflow_health.completion_blockers(run)
        self.assertFalse(any(item["kind"] == "phase-empty" for item in blockers))

    def test_completed_phase_with_lead_local_agent_or_artifact_is_not_flagged_empty(self) -> None:
        """A lead-local agent (or an artifact) tied to the phase satisfies the audit trail."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_health  # pylint: disable=import-outside-toplevel

        with_agent = {
            "run_id": "wf-test",
            "status": "running",
            "phases": [{"phase_id": "phase-impl", "status": "completed"}],
            "agents": [{"agent_id": "a1", "phase_id": "phase-impl", "agent_type": "lead-local", "status": "completed"}],
            "artifacts": [],
        }
        with_artifact = {
            "run_id": "wf-test",
            "status": "running",
            "phases": [{"phase_id": "phase-impl", "status": "completed"}],
            "agents": [],
            "artifacts": [{"artifact_id": "art1", "phase_id": "phase-impl", "path": __file__}],
        }

        for run in (with_agent, with_artifact):
            findings = workflow_health.analyze_run(run)
            self.assertFalse(any(item["kind"] == "phase-empty" for item in findings))

    def test_running_agent_without_liveness_source_warns(self) -> None:
        """A running managed agent needs a PID, transcript, native id, or explicit unmanaged marker."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_health  # pylint: disable=import-outside-toplevel
        import workflow_ops  # pylint: disable=import-outside-toplevel

        run = {
            "run_id": "wf-test",
            "title": "Opaque",
            "status": "running",
            "phases": [{"phase_id": "phase-impl", "status": "running"}],
            "agents": [{"agent_id": "agent-opaque", "name": "Opaque", "phase_id": "phase-impl", "status": "running", "agent_type": "codex-exec"}],
            "artifacts": [],
        }

        findings = workflow_health.analyze_run(run)
        opaque = next(item for item in findings if item["kind"] == "agent-opaque-running")
        self.assertEqual(opaque["severity"], workflow_health.WARNING)
        self.assertIn("no process id, transcript path, native id, or unmanaged marker", opaque["message"])
        self.assertIn("1 warn", workflow_ops.status_line(run))

    def test_running_agent_with_liveness_source_is_not_flagged_opaque(self) -> None:
        """Known liveness sources keep running managed agents from looking opaque."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_health  # pylint: disable=import-outside-toplevel

        agents = [
            {"agent_id": "with-pid", "status": "running", "process_id": 123},
            {"agent_id": "with-transcript", "status": "running", "jsonl_path": "logs/worker.jsonl"},
            {"agent_id": "with-native-id", "status": "running", "agent_type": "native-subagent", "thread_id": "native-123"},
            {"agent_id": "unmanaged-sidecar", "status": "running", "unmanaged": True},
        ]
        for agent in agents:
            run = {"run_id": "wf-test", "status": "running", "phases": [], "agents": [agent], "artifacts": []}
            findings = workflow_health.analyze_run(run)
            self.assertFalse(any(item["kind"] == "agent-opaque-running" for item in findings), msg=agent)

    def test_operator_done_requires_required_passing_check(self) -> None:
        """Ensure optional checks do not satisfy the default completion gate."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_wf("init", "--title", "Optional Check", "--prompt", "optional", "--cwd", str(ROOT), env=env)
            run_id = json.loads(created.stdout)["run_id"]
            self.run_wf("add-phase", run_id, "--phase-id", "phase-done", "--name", "Done", "--status", "completed", env=env)
            self.run_wf(
                "verify",
                run_id,
                "--record-only",
                "--status",
                "passed",
                "--optional",
                "--summary",
                "non-blocking manual note",
                "--external-ref",
                "https://example.invalid/optional",
                env=env,
            )

            denied = self.run_wf("done", run_id, env=env, check=False)

            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("verification-missing", denied.stdout)

    def test_operator_done_rejects_cancelled_runs_even_with_checks(self) -> None:
        """Prevent accidental revival of cancelled workflows through wf done."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_wf("init", "--title", "Cancelled Done", "--prompt", "cancelled", "--cwd", str(ROOT), env=env)
            run_id = json.loads(created.stdout)["run_id"]
            self.run_wf("add-phase", run_id, "--phase-id", "phase-done", "--name", "Done", "--status", "completed", env=env)
            self.run_wf("verify", run_id, "--record-only", "--status", "passed", "--summary", "manual pass", "--external-ref", "https://example.invalid/manual", env=env)
            self.run_wf("set-status", run_id, "cancelled", env=env)

            denied = self.run_wf("done", run_id, env=env, check=False)

            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("run-cancelled", denied.stdout)

    def test_set_status_completed_requires_force(self) -> None:
        """Block accidental direct completion via set-status without recovery flag."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_wf("init", "--title", "Force Status", "--prompt", "status", "--cwd", str(ROOT), env=env)
            run_id = json.loads(created.stdout)["run_id"]

            denied = self.run_wf("set-status", run_id, "completed", env=env, check=False)
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("requires --force", denied.stderr)

            self.run_wf("set-status", run_id, "completed", "--force", env=env)
            data = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
            self.assertEqual(data["status"], "completed")

    def test_record_only_pass_requires_provenance(self) -> None:
        """Reject record-only passes that do not carry evidence provenance."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_wf("init", "--title", "Provenance Gate", "--prompt", "gate", "--cwd", str(ROOT), env=env)
            run_id = json.loads(created.stdout)["run_id"]
            self.run_wf("add-phase", run_id, "--phase-id", "phase-done", "--name", "Done", "--status", "completed", env=env)

            denied = self.run_wf("verify", run_id, "--record-only", "--status", "passed", "--summary", "manual pass", env=env, check=False)
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("requires evidence provenance", denied.stderr)

            self.run_wf(
                "verify",
                run_id,
                "--record-only",
                "--status",
                "passed",
                "--summary",
                "manual pass",
                "--external-ref",
                "https://example.invalid/evidence",
                env=env,
            )
            data = json.loads(self.run_script("workflow_state.py", "show", run_id, "--json", env=env).stdout)
            self.assertEqual(data["checks"][0]["external_ref"], "https://example.invalid/evidence")
            self.assertTrue(any(event.get("kind") == "verification: external" for event in data["events"]))

    def test_invalid_status_rejected_before_mutation(self) -> None:
        """Invalid status arguments must not touch the run file."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_wf("init", "--title", "Validate First", "--prompt", "validate", "--cwd", str(ROOT), env=env)
            created_data = json.loads(created.stdout)
            run_id = created_data["run_id"]
            run_path = Path(created_data["path"])
            before = run_path.read_text(encoding="utf-8")

            denied = self.run_wf("set-status", run_id, "bogus", env=env, check=False)
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("invalid status", denied.stderr)
            self.assertEqual(run_path.read_text(encoding="utf-8"), before)

    def test_record_only_pass_without_provenance_cannot_satisfy_gate(self) -> None:
        """Legacy record-only passes without provenance no longer satisfy wf done."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_wf("init", "--title", "Legacy Bypass", "--prompt", "bypass", "--cwd", str(ROOT), env=env)
            created_data = json.loads(created.stdout)
            run_id = created_data["run_id"]
            run_path = Path(created_data["path"])
            self.run_wf("add-phase", run_id, "--phase-id", "phase-done", "--name", "Done", "--status", "completed", env=env)
            data = json.loads(run_path.read_text(encoding="utf-8"))
            data["checks"].append(
                {
                    "check_id": "chk-legacy",
                    "ts": data["created_at"],
                    "name": "legacy manual pass",
                    "kind": "verification",
                    "status": "passed",
                    "required": True,
                    "command": "",
                    "cwd": str(ROOT),
                    "exit_code": 0,
                    "duration_seconds": 0.0,
                    "summary": "manual pass",
                    "log_path": "",
                    "completed_at": data["created_at"],
                }
            )
            run_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

            denied = self.run_wf("done", run_id, env=env, check=False)
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("verification-missing", denied.stdout)

    def test_fcntl_unavailable_emits_one_time_warning(self) -> None:
        """Warn the operator once when advisory locking is not available."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_state  # pylint: disable=import-outside-toplevel

        original_fcntl = workflow_state.fcntl
        original_flag = workflow_state._FCNTL_WARNING_EMITTED
        try:
            workflow_state.fcntl = None
            workflow_state._FCNTL_WARNING_EMITTED = False
            run = {
                "run_id": "wf-lock-warning",
                "status": "running",
                "phases": [],
                "agents": [],
                "events": [],
                "decisions": [],
                "artifacts": [],
                "checks": [],
                "control": {},
                "metrics": {},
                "paths": {"run_json": "/dev/null"},
                "updated_at": "2026-01-01T00:00:00Z",
            }
            stderr_capture = io.StringIO()
            with contextlib.redirect_stderr(stderr_capture):

                def mutator(data: dict[str, object]) -> None:
                    data["updated_at"] = "2026-01-01T00:00:01Z"

                with workflow_state.exclusive_lock(Path("/tmp/wf-lock-warning.lock")):
                    mutator(run)
                with workflow_state.exclusive_lock(Path("/tmp/wf-lock-warning.lock")):
                    mutator(run)
            self.assertIn("fcntl unavailable", stderr_capture.getvalue())
            self.assertEqual(stderr_capture.getvalue().count("fcntl unavailable"), 1)
        finally:
            workflow_state.fcntl = original_fcntl
            workflow_state._FCNTL_WARNING_EMITTED = original_flag

    def test_doctor_reports_wf_symlink_resolves_to_checkout(self) -> None:
        """doctor reports whether the installed wf/workflow wrappers point here."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            fake_bin = Path(tmp) / "bin"
            fake_bin.mkdir()
            local_wf = ROOT / "scripts" / "wf"
            (fake_bin / "wf").symlink_to(local_wf)
            (fake_bin / "workflow").symlink_to(local_wf)
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            result = self.run_wf("doctor", "--json", env=env)
            data = json.loads(result.stdout)
            by_name = {check["name"]: check for check in data["checks"]}
            self.assertTrue(by_name["wf-in-checkout"]["ok"])
            self.assertTrue(by_name["workflow-in-checkout"]["ok"])
            self.assertIn(str(local_wf), by_name["wf-in-checkout"]["path"])

    def test_operator_doctor_distinguishes_required_and_optional_checks(self) -> None:
        """Keep optional provider commands from making doctor report a broken install."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            result = self.run_wf("doctor", "--json", env=env)
            data = json.loads(result.stdout)
            by_name = {check["name"]: check for check in data["checks"]}
            self.assertTrue(data["ok"])
            self.assertTrue(by_name["state-dir"]["required"])
            self.assertFalse(by_name["opencode"]["required"])

    def test_runner_rejects_non_positive_concurrency(self) -> None:
        """Reject zero concurrency before asyncio can hang forever."""
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "workflow_run_codex.py"),
                "--title",
                "Bad Concurrency",
                "--cwd",
                str(ROOT),
                "--mock",
                "--concurrency",
                "0",
                "--job",
                "alpha::Alpha prompt",
            ],
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("positive integer", result.stderr)

    @slow_test
    def test_snapshot_fixtures_match_checked_in_screens(self) -> None:
        """Render every TUI tab from fixtures and compare to text snapshots."""
        cases = [
            ("overview", "snapshot-overview.txt", "0"),
            ("runs", "snapshot-runs.txt", "0"),
            ("phases", "snapshot-phases.txt", "1"),
            ("agents", "snapshot-agents.txt", "1"),
            ("events", "snapshot-events.txt", "0"),
            ("decisions", "snapshot-decisions.txt", "0"),
            ("artifacts", "snapshot-artifacts.txt", "0"),
        ]
        for tab, snapshot_name, row_index in cases:
            with self.subTest(tab=tab):
                rendered = self.run_script(
                    "workflow_tui.py",
                    "--snapshot",
                    "--fixture",
                    str(FIXTURE),
                    "--tab",
                    tab,
                    "--width",
                    "110",
                    "--height",
                    "30",
                    "--row-index",
                    row_index,
                    env=self.snapshot_env(),
                ).stdout
                expected = (SNAPSHOTS / snapshot_name).read_text(encoding="utf-8")
                self.assertEqual(rendered, expected)

    @slow_test
    def test_snapshot_filter_and_focus_modes(self) -> None:
        """Expose deterministic filter and focus views for visual review."""
        filtered = self.run_script(
            "workflow_tui.py",
            "--snapshot",
            "--fixture",
            str(FIXTURE),
            "--tab",
            "overview",
            "--filter",
            "blocked",
            "--width",
            "110",
            "--height",
            "30",
            env=self.snapshot_env(),
        ).stdout
        self.assertIn("Run blocked", filtered)
        self.assertNotIn("Run failed", filtered)
        empty_filtered = self.run_script(
            "workflow_tui.py",
            "--snapshot",
            "--fixture",
            str(FIXTURE),
            "--tab",
            "overview",
            "--filter",
            "definitely-no-match",
            "--width",
            "110",
            "--height",
            "30",
            env=self.snapshot_env(),
        ).stdout
        self.assertIn("Attention filter: definitely-no-match", empty_filtered)
        self.assertIn("No rows match filter: definitely-no-match", empty_filtered)
        focus_filtered = self.run_script(
            "workflow_tui.py",
            "--snapshot",
            "--fixture",
            str(FIXTURE),
            "--tab",
            "overview",
            "--filter",
            "run-blocked",
            "--focus",
            "--width",
            "110",
            "--height",
            "30",
            env=self.snapshot_env(),
        ).stdout
        self.assertIn("filter: run-blocked", focus_filtered)
        self.assertIn("Run blocked", focus_filtered)
        filtered_runs = self.run_script(
            "workflow_tui.py",
            "--snapshot",
            "--fixture",
            str(FIXTURE),
            "--tab",
            "runs",
            "--filter",
            "blocked",
            "--width",
            "110",
            "--height",
            "30",
            env=self.snapshot_env(),
        ).stdout
        self.assertIn("wf-fixture-blocked", filtered_runs)
        self.assertIn("wf-fixture-blocked/run.json", filtered_runs)
        self.assertNotIn("wf-fixture-rich/run.json", filtered_runs)
        focused = self.run_script(
            "workflow_tui.py",
            "--snapshot",
            "--fixture",
            str(FIXTURE),
            "--tab",
            "agents",
            "--row-index",
            "0",
            "--focus",
            "--width",
            "110",
            "--height",
            "30",
            env=self.snapshot_env(),
        ).stdout
        self.assertIn("Security reviewer", focused)
        self.assertNotIn("Agents: Review", focused)
        self.assertIn("Live Stats", focused)

    @slow_test
    def test_snapshot_dimensions_are_stable(self) -> None:
        """Ensure snapshot output has deterministic dimensions for visual review."""
        rendered = self.run_script(
            "workflow_tui.py",
            "--snapshot",
            "--fixture",
            str(FIXTURE),
            "--tab",
            "agents",
            "--width",
            "110",
            "--height",
            "30",
            "--row-index",
            "1",
            env=self.snapshot_env(),
        ).stdout
        lines = rendered.rstrip("\n").splitlines()
        self.assertEqual(len(lines), 30)
        self.assertTrue(all(len(line) <= 110 for line in lines))
        self.assertIn("╭", rendered)
        self.assertIn("╰", rendered)
        self.assertIn("│", rendered)
        self.assertNotIn("+ runs", rendered)

    @slow_test
    def test_snapshot_panels_are_not_cropped_mid_box(self) -> None:
        """Supported visual-review sizes should render complete panels, not clipped boxes."""
        cases = [
            ("rich-runs", str(FIXTURE), "runs", "110", "30"),
            ("narrow-runs", str(FIXTURE), "runs", "80", "24"),
            ("narrow-agents", str(FIXTURE), "agents", "80", "24"),
            ("live-runs", str(E2E_FIXTURE), "runs", "120", "36"),
        ]
        for label, fixture, tab, width, height in cases:
            with self.subTest(label=label):
                rendered = self.run_script(
                    "workflow_tui.py",
                    "--snapshot",
                    "--fixture",
                    fixture,
                    "--tab",
                    tab,
                    "--width",
                    width,
                    "--height",
                    height,
                    env=self.snapshot_env(),
                ).stdout
                self.assertEqual(rendered.count("╭"), rendered.count("╰"), rendered)
                self.assertNotIn("Metrics", rendered.splitlines()[-5:])

    @slow_test
    def test_snapshot_header_shows_agent_actions_only_on_agents_tab(self) -> None:
        """Keep agent-specific header shortcuts scoped to the agents tab."""
        runs = self.run_script(
            "workflow_tui.py",
            "--snapshot",
            "--fixture",
            str(FIXTURE),
            "--tab",
            "runs",
            "--width",
            "110",
            "--height",
            "30",
            env=self.snapshot_env(),
        ).stdout
        agents = self.run_script(
            "workflow_tui.py",
            "--snapshot",
            "--fixture",
            str(FIXTURE),
            "--tab",
            "agents",
            "--width",
            "110",
            "--height",
            "30",
            env=self.snapshot_env(),
        ).stdout
        self.assertIn("←/→ tabs", runs)
        self.assertIn("y id", runs)
        self.assertIn("p path", runs)
        self.assertNotIn("a scope", runs)
        self.assertNotIn("v view", runs)
        self.assertIn("a scope", agents)
        self.assertIn("v view", agents)
        self.assertNotIn("tab side/main", runs)
        self.assertNotIn("→ main", runs)
        self.assertNotIn("← main tabs", runs)

    def test_agent_only_actions_are_enabled_only_on_agents_tab(self) -> None:
        """Match live TUI key handling to the header's agent-only shortcuts."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        agent_actions = ["toggle_agent_scope", "toggle_agent_view"]
        for action in agent_actions:
            with self.subTest(tab="agents", action=action):
                self.assertTrue(workflow_tui.action_enabled_for_tab("agents", action))
            for tab in ("runs", "phases", "events", "decisions", "artifacts"):
                with self.subTest(tab=tab, action=action):
                    self.assertFalse(workflow_tui.action_enabled_for_tab(tab, action))
        self.assertTrue(workflow_tui.action_enabled_for_tab("runs", "copy_selected_id"))

    def test_live_footer_refreshes_agent_bindings_on_tab_change(self) -> None:
        """Refresh live Textual help when agent-only shortcuts enter or leave scope."""
        import types

        sys.path.insert(0, str(SCRIPTS))
        module_names = [
            "textual",
            "textual.app",
            "textual.screen",
            "textual.worker",
            "textual.widgets",
            "workflow_tui_app",
        ]
        original_modules = {name: sys.modules.get(name) for name in module_names}
        for name in module_names:
            sys.modules.pop(name, None)

        observations: dict[str, int | bool | None] = {}

        class FakeApp:
            def __init__(self) -> None:
                self.refresh_binding_calls = 0
                self.size = types.SimpleNamespace(width=110, height=30)

            def refresh_bindings(self) -> None:
                self.refresh_binding_calls += 1

            def run(self) -> None:
                observations["initial_scope"] = self.check_action("toggle_agent_scope", ())
                self.tab_index = FakeTui.TABS.index("phases")
                self.action_next_tab()
                observations["enter_agents_refreshes"] = self.refresh_binding_calls
                observations["agents_scope"] = self.check_action("toggle_agent_scope", ())
                self.action_next_tab()
                observations["leave_agents_refreshes"] = self.refresh_binding_calls
                observations["events_scope"] = self.check_action("toggle_agent_scope", ())
                self.action_previous_tab()
                observations["return_agents_refreshes"] = self.refresh_binding_calls
                observations["return_agents_scope"] = self.check_action("toggle_agent_scope", ())
                self.action_previous_tab()
                observations["back_phases_refreshes"] = self.refresh_binding_calls
                observations["phases_scope"] = self.check_action("toggle_agent_scope", ())

        class FakeSystemCommand:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

        class FakeStatic:
            pass

        class FakeTui:
            TABS = ("runs", "phases", "agents", "events", "decisions", "artifacts")
            AGENT_SCOPES = ("phase", "all")
            AGENT_VIEWS = ("live", "prompt")
            UPDATE_CHECK_INTERVAL = 999.0
            UPDATE_CHECK_TIMEOUT = 1.0
            UPDATE_PULL_TIMEOUT = 1.0

            @staticmethod
            def action_enabled_for_tab(tab: str, action: str) -> bool:
                return tab == "agents" or action not in {"toggle_agent_scope", "toggle_agent_view"}

            @staticmethod
            def current_rows_for(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
                return []

            @staticmethod
            def index_for_key(*_args: object, **_kwargs: object) -> int:
                return 0

            @staticmethod
            def clamp_index(*_args: object, **_kwargs: object) -> int:
                return 0

        try:
            sys.modules["textual"] = types.ModuleType("textual")
            app_module = types.ModuleType("textual.app")
            app_module.App = FakeApp
            app_module.ComposeResult = object
            app_module.SystemCommand = FakeSystemCommand
            sys.modules["textual.app"] = app_module
            screen_module = types.ModuleType("textual.screen")
            screen_module.Screen = object
            sys.modules["textual.screen"] = screen_module
            worker_module = types.ModuleType("textual.worker")
            worker_module.Worker = type("Worker", (), {"StateChanged": object})
            worker_module.WorkerState = types.SimpleNamespace(ERROR="error", SUCCESS="success")
            sys.modules["textual.worker"] = worker_module
            widgets_module = types.ModuleType("textual.widgets")
            widgets_module.Footer = FakeStatic
            widgets_module.Header = FakeStatic
            widgets_module.Static = FakeStatic
            sys.modules["textual.widgets"] = widgets_module

            import workflow_tui_app  # pylint: disable=import-outside-toplevel

            workflow_tui_app.run_textual_app(FakeTui)
        finally:
            for name, original in original_modules.items():
                if original is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = original

        self.assertFalse(observations["initial_scope"])
        self.assertEqual(observations["enter_agents_refreshes"], 1)
        self.assertTrue(observations["agents_scope"])
        self.assertEqual(observations["leave_agents_refreshes"], 2)
        self.assertFalse(observations["events_scope"])
        self.assertEqual(observations["return_agents_refreshes"], 3)
        self.assertTrue(observations["return_agents_scope"])
        self.assertEqual(observations["back_phases_refreshes"], 4)
        self.assertFalse(observations["phases_scope"])

    def test_live_tui_palette_exposes_workflow_control_actions(self) -> None:
        """Make pause, resume, and stop discoverable through the Textual command palette."""
        import types

        sys.path.insert(0, str(SCRIPTS))
        module_names = [
            "textual",
            "textual.app",
            "textual.screen",
            "textual.worker",
            "textual.widgets",
            "workflow_tui_app",
        ]
        original_modules = {name: sys.modules.get(name) for name in module_names}
        for name in module_names:
            sys.modules.pop(name, None)

        labels: list[str] = []

        class FakeApp:
            def __init__(self) -> None:
                self.size = types.SimpleNamespace(width=110, height=30)

            def get_system_commands(self, _screen: object) -> list[object]:
                return []

            def run(self) -> None:
                list(self.get_system_commands(None))

        class FakeSystemCommand:
            def __init__(self, title: str, *_args: object, **_kwargs: object) -> None:
                labels.append(title)

        class FakeStatic:
            pass

        class FakeTui:
            TABS = ("runs", "phases", "agents", "events", "decisions", "artifacts")
            AGENT_SCOPES = ("phase", "all")
            AGENT_VIEWS = ("live", "prompt")
            UPDATE_CHECK_INTERVAL = 999.0
            UPDATE_CHECK_TIMEOUT = 1.0
            UPDATE_PULL_TIMEOUT = 1.0

        try:
            sys.modules["textual"] = types.ModuleType("textual")
            app_module = types.ModuleType("textual.app")
            app_module.App = FakeApp
            app_module.ComposeResult = object
            app_module.SystemCommand = FakeSystemCommand
            sys.modules["textual.app"] = app_module
            screen_module = types.ModuleType("textual.screen")
            screen_module.Screen = object
            sys.modules["textual.screen"] = screen_module
            worker_module = types.ModuleType("textual.worker")
            worker_module.Worker = type("Worker", (), {"StateChanged": object})
            worker_module.WorkerState = types.SimpleNamespace(ERROR="error", SUCCESS="success")
            sys.modules["textual.worker"] = worker_module
            widgets_module = types.ModuleType("textual.widgets")
            widgets_module.Footer = FakeStatic
            widgets_module.Header = FakeStatic
            widgets_module.Static = FakeStatic
            sys.modules["textual.widgets"] = widgets_module

            import workflow_tui_app  # pylint: disable=import-outside-toplevel

            workflow_tui_app.run_textual_app(FakeTui)
        finally:
            for name, original in original_modules.items():
                if original is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = original

        self.assertIn("Workflow: Pause selected run", labels)
        self.assertIn("Workflow: Resume selected run", labels)
        self.assertIn("Workflow: Stop selected run", labels)

    def test_system_clipboard_helper_uses_x11_clipboard_selection(self) -> None:
        """The live copy action should have a Ctrl+V clipboard fallback, not primary selection."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui_app  # pylint: disable=import-outside-toplevel

        calls: list[tuple[list[str], str]] = []

        def fake_run(command: list[str], **kwargs: object) -> object:
            calls.append((command, str(kwargs.get("input", ""))))
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(workflow_tui_app.shutil, "which", side_effect=lambda name: "/usr/bin/xclip" if name == "xclip" else None):
            with mock.patch.object(workflow_tui_app.subprocess, "run", side_effect=fake_run):
                copied, method = workflow_tui_app.copy_to_system_clipboard("wf-123")

        self.assertTrue(copied)
        self.assertEqual(method, "xclip")
        self.assertEqual(calls, [(["/usr/bin/xclip", "-selection", "clipboard"], "wf-123")])

    def test_textual_venv_reexec_uses_cli_entrypoint(self) -> None:
        """Re-entering the workflow venv should launch the CLI backend, not the app helper."""
        import builtins

        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel
        import workflow_tui_app  # pylint: disable=import-outside-toplevel

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            venv_python = tmp_path / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("", encoding="utf-8")

            calls: list[tuple[str, list[str]]] = []
            original_execv = os.execv
            original_executable = sys.executable
            original_argv = sys.argv
            original_import = builtins.__import__
            original_workflow_root = workflow_tui_app.workflow_state.workflow_root

            def import_without_textual(name: str, *args: object, **kwargs: object) -> object:
                """Force the Textual import path through workflow venv re-exec handling."""
                if name == "textual" or name.startswith("textual."):
                    raise ModuleNotFoundError("No module named 'textual'", name="textual")
                return original_import(name, *args, **kwargs)

            try:
                os.execv = lambda path, args: calls.append((path, args))  # type: ignore[assignment]
                sys.executable = str(tmp_path / "system-python")
                sys.argv = [str(SCRIPTS / "workflow_tui.py"), "--tab", "agents"]
                builtins.__import__ = import_without_textual  # type: ignore[assignment]
                workflow_tui_app.workflow_state.workflow_root = lambda: tmp_path

                with self.assertRaises(SystemExit):
                    workflow_tui_app.run_textual_app(workflow_tui)
            finally:
                workflow_tui_app.workflow_state.workflow_root = original_workflow_root
                builtins.__import__ = original_import  # type: ignore[assignment]
                sys.argv = original_argv
                sys.executable = original_executable
                os.execv = original_execv  # type: ignore[assignment]

            self.assertEqual(
                calls,
                [
                    (
                        str(venv_python),
                        [str(venv_python), str(Path(workflow_tui.__file__).resolve()), "--tab", "agents"],
                    )
                ],
            )

    @slow_test
    def test_snapshot_phase_sidebar_matches_selected_run(self) -> None:
        """Show phases in the left column while the phases section is active."""
        rendered = self.run_script(
            "workflow_tui.py",
            "--snapshot",
            "--fixture",
            str(FIXTURE),
            "--tab",
            "phases",
            "--width",
            "110",
            "--height",
            "30",
            "--row-index",
            "1",
            env=self.snapshot_env(),
        ).stdout
        self.assertIn("Phases", rendered)
        self.assertIn("Review", rendered)
        self.assertIn("Synthesis", rendered)
        self.assertIn("phase       phase-review", rendered)
        self.assertNotIn("Completed Smoke Fixture", rendered)

    def test_phase_rows_preserve_workflow_order(self) -> None:
        """Sort phases top-to-bottom in expected workflow execution order."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        run = workflow_tui.load_fixture(str(FIXTURE))[0]
        phases = workflow_tui.rows_for_tab(run, "phases", [run])
        self.assertEqual([phase["phase_id"] for phase in phases], ["phase-research", "phase-review", "phase-synthesis"])

    def test_phase_rows_preserve_persisted_order_for_custom_names(self) -> None:
        """Do not reorder phases by guessed lifecycle names."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        run = {
            "phases": [
                {"phase_id": "phase-verify", "name": "Verify first"},
                {"phase_id": "phase-research", "name": "Research second"},
                {"phase_id": "phase-custom", "name": "Custom third"},
            ],
            "agents": [],
        }
        phases = workflow_tui.rows_for_tab(run, "phases", [run])
        self.assertEqual([phase["phase_id"] for phase in phases], ["phase-verify", "phase-research", "phase-custom"])

    def test_agents_tab_scopes_to_selected_phase(self) -> None:
        """Default agent rows show only the selected/current phase workers."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        run = workflow_tui.load_fixture(str(FIXTURE))[0]
        review_agents = workflow_tui.rows_for_tab(run, "agents", [run], selected_phase_id="phase-review")
        synthesis_agents = workflow_tui.rows_for_tab(run, "agents", [run], selected_phase_id="phase-synthesis")
        all_agents = workflow_tui.rows_for_tab(run, "agents", [run], selected_phase_id="phase-review", agent_scope="all")
        self.assertEqual([agent["agent_id"] for agent in review_agents], ["agent-security", "agent-tests"])
        self.assertEqual([agent["agent_id"] for agent in synthesis_agents], ["agent-synthesis"])
        self.assertEqual({agent["agent_id"] for agent in all_agents}, {"agent-security", "agent-tests", "agent-synthesis"})

    def test_agent_snapshot_shows_phase_scope_and_all_scope(self) -> None:
        """Lock in phase-scoped and all-agent sidebar behavior."""
        review = self.run_script(
            "workflow_tui.py",
            "--snapshot",
            "--fixture",
            str(FIXTURE),
            "--tab",
            "agents",
            "--phase-id",
            "phase-review",
            "--row-index",
            "1",
            "--width",
            "120",
            "--height",
            "34",
            env=self.snapshot_env(),
        ).stdout
        all_agents = self.run_script(
            "workflow_tui.py",
            "--snapshot",
            "--fixture",
            str(FIXTURE),
            "--tab",
            "agents",
            "--phase-id",
            "phase-review",
            "--agent-scope",
            "all",
            "--row-index",
            "2",
            "--width",
            "120",
            "--height",
            "34",
            env=self.snapshot_env(),
        ).stdout
        self.assertIn("Agents: Review", review)
        self.assertIn("Security", review)
        self.assertIn("Test coverage", review)
        self.assertNotIn("agent-synthesis", review)
        self.assertIn("Synthesis", all_agents)
        self.assertIn("MiniMa", all_agents)

    @slow_test
    def test_snapshot_events_are_newest_first(self) -> None:
        """Render newest workflow events first so active runs stay visible."""
        rendered = self.run_script(
            "workflow_tui.py",
            "--snapshot",
            "--fixture",
            str(FIXTURE),
            "--tab",
            "events",
            "--width",
            "110",
            "--height",
            "30",
            env=self.snapshot_env(),
        ).stdout
        self.assertIn("event_id      evt-synthesis", rendered)
        self.assertIn("artifact recorded", rendered)

    def test_event_timestamps_are_compact_when_recent(self) -> None:
        """Recent event rows should show local time, with compact dates only for older events."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel
        from datetime import timezone

        reference = datetime.fromisoformat("2026-06-11T08:00:00+10:00")
        self.assertEqual(workflow_tui.display_event_timestamp("2026-06-11T00:05:00Z", now=reference), "10:05:00 AEST")
        self.assertEqual(workflow_tui.display_event_timestamp("2026-06-09T21:05:00Z", now=reference), "26-06-10 07:05")
        self.assertEqual(workflow_tui.display_event_timestamp("", now=reference.astimezone(timezone.utc)), "")

    def test_run_duration_uses_live_and_terminal_timestamps(self) -> None:
        """Run overview durations should use the right start, end, and deterministic clocks."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        now = datetime.fromisoformat("2026-06-11T00:06:00+00:00")
        self.assertEqual(
            workflow_tui.run_duration_text(
                {
                    "status": "running",
                    "created_at": "2026-06-11T00:00:00Z",
                    "started_at": "2026-06-11T00:01:00Z",
                    "updated_at": "2026-06-11T00:02:00Z",
                },
                now=now,
            ),
            "5m 00s",
        )
        self.assertEqual(
            workflow_tui.run_duration_text(
                {
                    "status": "completed",
                    "created_at": "2026-06-11T00:00:00Z",
                    "started_at": "2026-06-11T00:01:00Z",
                    "completed_at": "2026-06-11T00:03:30Z",
                    "updated_at": "2026-06-11T00:05:00Z",
                },
                now=now,
            ),
            "2m 30s",
        )
        self.assertEqual(
            workflow_tui.run_duration_text(
                {
                    "status": "failed",
                    "created_at": "2026-06-11T00:00:30Z",
                    "started_at": None,
                    "updated_at": "2026-06-11T00:03:00Z",
                },
                now=now,
            ),
            "2m 30s",
        )
        self.assertEqual(
            workflow_tui.run_duration_text(
                {
                    "status": "running",
                    "created_at": "2026-06-11T00:07:00Z",
                    "started_at": "2026-06-11T00:08:00Z",
                },
                now=now,
            ),
            "0s",
        )

    def test_runs_table_uses_two_line_title_state_duration_rows(self) -> None:
        """The runs overview keeps title, state, duration, and active agents compact."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        old_now = os.environ.get("WORKFLOW_TUI_SNAPSHOT_NOW")
        os.environ["WORKFLOW_TUI_SNAPSHOT_NOW"] = "2026-06-11T00:06:00Z"
        try:
            sink = io.StringIO()
            console = Console(file=sink, width=52, force_terminal=False, color_system=None)
            console.print(
                workflow_tui.make_runs_table(
                    [
                        {
                            "title": "Short Workflow",
                            "status": "running",
                            "created_at": "2026-06-11T00:00:00Z",
                            "started_at": "2026-06-11T00:01:00Z",
                            "agents": [
                                {"agent_id": "a", "name": "Alpha", "status": "running", "started_at": "2026-06-11T00:01:00Z"},
                                {"agent_id": "b", "name": "Beta", "status": "running", "started_at": "2026-06-11T00:04:30Z"},
                            ],
                        }
                    ],
                    0,
                    4,
                )
            )
        finally:
            if old_now is None:
                os.environ.pop("WORKFLOW_TUI_SNAPSHOT_NOW", None)
            else:
                os.environ["WORKFLOW_TUI_SNAPSHOT_NOW"] = old_now
        rendered = sink.getvalue()
        self.assertIn("Short Workflow", rendered)
        self.assertIn("> RUN > 5m 00s", rendered)
        self.assertIn("Alpha", rendered)
        self.assertIn("Beta", rendered)
        self.assertNotIn("Duration", rendered)

    def test_duration_fields_render_with_human_units(self) -> None:
        """Tiny duration fields should not leak scientific notation or bare zeroes."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        sink = io.StringIO()
        console = Console(file=sink, width=80, force_terminal=False, color_system=None)
        console.print(
            workflow_tui.make_mapping_table(
                [
                    ("duration_seconds", 0.0000024),
                    ("zero_duration_seconds", 0),
                    ("elapsed_seconds", 0.0312),
                    ("e2e_seconds", 2.5),
                    ("count", 0),
                ]
            )
        )
        rendered = sink.getvalue()
        self.assertIn("2.4 us", rendered)
        self.assertIn("<1 us", rendered)
        self.assertIn("31.2 ms", rendered)
        self.assertIn("2.5 s", rendered)
        self.assertIn("count", rendered)
        self.assertNotIn("e-", rendered.lower())
        self.assertNotIn("0.0", rendered)

    def test_agents_table_shows_live_and_terminal_durations(self) -> None:
        """Agent rows should expose compact live elapsed and terminal duration values."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        old_now = os.environ.get("WORKFLOW_TUI_SNAPSHOT_NOW")
        os.environ["WORKFLOW_TUI_SNAPSHOT_NOW"] = "2026-06-11T00:06:00Z"
        try:
            sink = io.StringIO()
            console = Console(file=sink, width=84, force_terminal=False, color_system=None)
            console.print(
                workflow_tui.make_agent_table(
                    [
                        {"agent_id": "a", "name": "Alpha", "status": "running", "started_at": "2026-06-11T00:01:00Z"},
                        {
                            "agent_id": "b",
                            "name": "Beta",
                            "status": "completed",
                            "started_at": "2026-06-11T00:02:00Z",
                            "completed_at": "2026-06-11T00:04:15Z",
                        },
                        {
                            "agent_id": "c",
                            "name": "Gamma",
                            "status": "failed",
                            "started_epoch": 1781136120,
                            "updated_at": "2026-06-11T00:04:00Z",
                        },
                    ],
                    0,
                    8,
                )
            )
        finally:
            if old_now is None:
                os.environ.pop("WORKFLOW_TUI_SNAPSHOT_NOW", None)
            else:
                os.environ["WORKFLOW_TUI_SNAPSHOT_NOW"] = old_now

        rendered = sink.getvalue()
        self.assertIn("Time", rendered)
        self.assertIn("5m 00s", rendered)
        self.assertIn("2m 15s", rendered)
        self.assertIn("2m 00s", rendered)

    def test_agent_detail_shows_elapsed_stat_for_running_worker(self) -> None:
        """Selected-agent detail should expose elapsed time with the other live stats."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        old_now = os.environ.get("WORKFLOW_TUI_SNAPSHOT_NOW")
        os.environ["WORKFLOW_TUI_SNAPSHOT_NOW"] = "2026-06-11T00:06:00Z"
        try:
            sink = io.StringIO()
            console = Console(file=sink, width=100, force_terminal=False, color_system=None)
            console.print(
                workflow_tui.make_agent_activity_detail(
                    {"agent_id": "a", "name": "Alpha", "status": "running", "started_at": "2026-06-11T00:01:00Z"},
                    {"agents": []},
                )
            )
        finally:
            if old_now is None:
                os.environ.pop("WORKFLOW_TUI_SNAPSHOT_NOW", None)
            else:
                os.environ["WORKFLOW_TUI_SNAPSHOT_NOW"] = old_now

        rendered = sink.getvalue()
        self.assertIn("elapsed", rendered)
        self.assertIn("5m 00s", rendered)

    def test_agent_detail_shows_process_identity_for_running_worker(self) -> None:
        """Selected-agent detail should expose process and native identity values."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        sink = io.StringIO()
        console = Console(file=sink, width=100, force_terminal=False, color_system=None)
        console.print(
            workflow_tui.make_agent_activity_detail(
                {
                    "agent_id": "a",
                    "name": "Alpha",
                    "status": "running",
                    "process_id": 12345,
                    "process_group_id": 23456,
                    "native_id": "native-abc",
                },
                {"agents": []},
            )
        )
        rendered = sink.getvalue()

        self.assertIn("pgid", rendered)
        self.assertIn("23456", rendered)
        self.assertIn("native_id", rendered)
        self.assertIn("native-abc", rendered)

    def test_event_type_cells_are_colorized(self) -> None:
        """Use styled event type cells instead of plain event text only."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        run = workflow_tui.load_fixture(str(FIXTURE))[0]
        events = workflow_tui.rows_for_tab(run, "events", [run])
        self.assertEqual([event["event_id"] for event in events[:2]], ["evt-synthesis", "evt-tests"])
        table = workflow_tui.make_events_table(events, 0, 4)
        type_cells = table.columns[1]._cells  # pylint: disable=protected-access
        self.assertTrue(all(isinstance(cell, workflow_tui.Text) for cell in type_cells))
        self.assertIn("bright_blue", type_cells[0].style)

    @slow_test
    def test_snapshot_status_labels_cover_all_workflow_states(self) -> None:
        """Keep compact status labels visible for every persisted state."""
        rendered = self.run_script(
            "workflow_tui.py",
            "--snapshot",
            "--fixture",
            str(FIXTURE),
            "--tab",
            "runs",
            "--width",
            "110",
            "--height",
            "30",
            env=self.snapshot_env(),
        ).stdout
        for label in ("RUN", "DONE", "FAIL", "BLCK", "PAUS", "CNCL", "PEND"):
            self.assertIn(label, rendered)

    @slow_test
    def test_snapshot_timestamps_render_as_local_pretty_labels(self) -> None:
        """Show TUI timestamps in local time without mutating persisted state fields."""
        env = self.snapshot_env()
        runs_rendered = self.run_script(
            "workflow_tui.py",
            "--snapshot",
            "--fixture",
            str(FIXTURE),
            "--tab",
            "runs",
            "--width",
            "110",
            "--height",
            "30",
            env=env,
        ).stdout
        events_rendered = self.run_script(
            "workflow_tui.py",
            "--snapshot",
            "--fixture",
            str(FIXTURE),
            "--tab",
            "events",
            "--width",
            "110",
            "--height",
            "30",
            env=env,
        ).stdout
        decisions_rendered = self.run_script(
            "workflow_tui.py",
            "--snapshot",
            "--fixture",
            str(FIXTURE),
            "--tab",
            "decisions",
            "--width",
            "110",
            "--height",
            "30",
            env=env,
        ).stdout

        self.assertIn("updated     Jun 11 10:05 AEST", runs_rendered)
        self.assertIn("10:05:00 AEST", events_rendered)
        self.assertIn("time          10:05:00 AEST", events_rendered)
        self.assertIn("10:04:00 AEST", decisions_rendered)
        self.assertIn("time          10:04:00 AEST", decisions_rendered)
        self.assertNotIn("2026-06-11T00:00:…", events_rendered)
        self.assertNotIn("2026-06-11T00:04:…", decisions_rendered)

    def test_sidebar_tables_prioritize_readable_labels(self) -> None:
        """Sidebar tables should hide low-value columns before truncating important names."""
        phases = self.run_script(
            "workflow_tui.py",
            "--snapshot",
            "--fixture",
            str(FIXTURE),
            "--tab",
            "phases",
            "--width",
            "100",
            "--height",
            "24",
            env=self.snapshot_env(),
        ).stdout
        decisions = self.run_script(
            "workflow_tui.py",
            "--snapshot",
            "--fixture",
            str(FIXTURE),
            "--tab",
            "decisions",
            "--width",
            "80",
            "--height",
            "24",
            env=self.snapshot_env(),
        ).stdout
        self.assertIn("Research", phases)
        self.assertIn("Review", phases)
        self.assertIn("Synthesis", phases)
        self.assertNotIn("phase-synthesis", phases.split("╭", 2)[1])
        self.assertIn("Default workers to read-only", decisions)
        self.assertNotIn("Default   10:04", decisions)

    def test_empty_decision_and_artifact_sidebars_do_not_wrap_no_rows(self) -> None:
        """Empty collection sidebars should render a single clear line."""
        fixture = {
            "run_id": "wf-empty-collections",
            "title": "Empty Collections",
            "status": "running",
            "cwd": str(ROOT),
            "updated_at": "2026-06-11T00:00:00Z",
            "paths": {"run_json": "/tmp/wf-empty-collections/run.json"},
            "phases": [],
            "agents": [],
            "events": [],
            "decisions": [],
            "artifacts": [],
            "metrics": {"agents_total": 0, "phases_total": 0, "agents_by_status": {}, "phases_by_status": {}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            fixture_path = Path(tmp) / "empty.json"
            fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
            for tab, message in (("decisions", "No decisions."), ("artifacts", "No artifacts.")):
                with self.subTest(tab=tab):
                    rendered = self.run_script(
                        "workflow_tui.py",
                        "--snapshot",
                        "--fixture",
                        str(fixture_path),
                        "--tab",
                        tab,
                        "--width",
                        "80",
                        "--height",
                        "16",
                        env=self.snapshot_env(),
                    ).stdout
                    self.assertIn(message, rendered)
                    self.assertNotIn("No rows", rendered)

    def test_copy_value_helpers_return_selected_ids_paths_and_json(self) -> None:
        """Copy commands should expose stable ids and useful paths for selected rows."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        runs = workflow_tui.load_fixture(str(FIXTURE))
        run = runs[0]
        agent_rows = workflow_tui.rows_for_tab(run, "agents", runs, selected_phase_id="phase-review")
        artifact_rows = workflow_tui.rows_for_tab(run, "artifacts", runs)

        label, value = workflow_tui.copy_value_for_selection(run, "agents", agent_rows, 1, "id")
        self.assertEqual(label, "agent_id")
        self.assertEqual(value, "agent-tests")

        label, value = workflow_tui.copy_value_for_selection(run, "artifacts", artifact_rows, 0, "path")
        self.assertEqual(label, "artifact path")
        self.assertEqual(value, str(FIXTURE.parent / "artifacts" / "final-report.md"))

        staged = dict(run)
        staged.pop("_fixture_dir", None)
        staged["paths"] = {
            "run_dir": "/tmp/wf-fixture-rich",
            "artifacts_dir": "/tmp/wf-fixture-rich/artifacts",
            "logs_dir": "/tmp/wf-fixture-rich/logs",
        }
        label, value = workflow_tui.copy_value_for_selection(staged, "artifacts", artifact_rows, 0, "path")
        self.assertEqual(label, "artifact path")
        self.assertEqual(value, "/tmp/wf-fixture-rich/artifacts/final-report.md")

        label, value = workflow_tui.copy_value_for_selection(run, "runs", runs, 0, "json")
        self.assertEqual(label, "run json")
        self.assertIn('"run_id": "wf-fixture-rich"', value)

        raw_run = dict(run)
        raw_run["duration_seconds"] = 12.4
        label, value = workflow_tui.copy_value_for_selection(raw_run, "runs", [raw_run], 0, "json")
        copied = json.loads(value)
        self.assertEqual(label, "run json")
        self.assertEqual(copied["updated_at"], "2026-06-11T00:05:00Z")
        self.assertEqual(copied["duration_seconds"], 12.4)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_path = tmp_path / "artifacts" / "agent.md"
            output_path.parent.mkdir(parents=True)
            output_path.write_text("agent output\n", encoding="utf-8")
            staged_run = {"paths": {"run_dir": str(tmp_path), "artifacts_dir": str(tmp_path / "artifacts"), "logs_dir": str(tmp_path / "logs")}}
            staged_agent = {"agent_id": "agent-output", "output_path": "artifacts/agent.md", "jsonl_path": "logs/missing.jsonl", "log_path": "logs/missing.log"}
            label, value = workflow_tui.copy_value_for_selection(staged_run, "agents", [staged_agent], 0, "path")
        self.assertEqual(label, "agent path")
        self.assertEqual(value, str(output_path))

    def test_agent_path_copy_does_not_return_dead_paths(self) -> None:
        """Avoid copying agent paths that cannot be opened from the current state."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        run = {"paths": {"run_dir": "/tmp/wf-missing", "artifacts_dir": "/tmp/wf-missing/artifacts", "logs_dir": "/tmp/wf-missing/logs"}}
        agent = {"agent_id": "agent-missing", "output_path": "artifacts/missing.md", "jsonl_path": "logs/missing.jsonl", "log_path": "logs/missing.log"}

        label, value = workflow_tui.copy_value_for_selection(run, "agents", [agent], 0, "path")

        self.assertEqual(label, "agent path")
        self.assertEqual(value, "")

    def test_text_artifact_detail_renders_file_preview(self) -> None:
        """Artifact detail should render readable UTF-8 artifact files inline."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        runs = workflow_tui.load_fixture(str(FIXTURE))
        run = runs[0]
        artifact = workflow_tui.rows_for_tab(run, "artifacts", runs)[0]
        sink = io.StringIO()
        console = Console(file=sink, width=100, force_terminal=False, color_system=None)
        console.print(workflow_tui.make_artifact_detail(artifact, run))
        rendered = sink.getvalue()
        self.assertIn("Artifact Preview", rendered)
        self.assertIn("Final synthesis report", rendered)
        self.assertIn("Security: no critical issues", rendered)

        with tempfile.TemporaryDirectory() as tmp:
            utf8_path = Path(tmp) / "artifact.md"
            utf8_path.write_text("UTF-8 report: ready -> ship\nCost: 10 euros\n", encoding="utf-8")
            artifact = {"artifact_id": "art-utf8", "title": "UTF8", "kind": "markdown", "path": str(utf8_path)}
            sink = io.StringIO()
            console = Console(file=sink, width=100, force_terminal=False, color_system=None)
            console.print(workflow_tui.make_artifact_detail(artifact))
            rendered = sink.getvalue()
        self.assertIn("Artifact Preview", rendered)
        self.assertIn("UTF-8 report: ready -> ship", rendered)
        self.assertIn("10 euros", rendered)

        with tempfile.TemporaryDirectory() as tmp:
            utf8_path = Path(tmp) / "split.md"
            utf8_path.write_text("aaaa€bbbb", encoding="utf-8")
            preview = workflow_tui.read_text_artifact_preview(utf8_path, limit=6)
        self.assertTrue(preview.startswith("aaaa"))
        self.assertIn("truncated after 6 bytes", preview)

        with tempfile.TemporaryDirectory() as tmp:
            invalid_path = Path(tmp) / "invalid.md"
            invalid_path.write_bytes(b"valid prefix \xe2")
            preview = workflow_tui.read_text_artifact_preview(invalid_path)
        self.assertEqual(preview, "")

    def test_binary_artifact_detail_does_not_render_file_preview(self) -> None:
        """Binary artifacts should keep the detail pane readable by omitting previews."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        with tempfile.TemporaryDirectory() as tmp:
            binary_path = Path(tmp) / "artifact.bin"
            binary_path.write_bytes(b"\x00\xff\xfePNG-ish")
            artifact = {"artifact_id": "art-bin", "title": "Binary", "kind": "binary", "path": str(binary_path)}
            sink = io.StringIO()
            console = Console(file=sink, width=100, force_terminal=False, color_system=None)
            console.print(workflow_tui.make_artifact_detail(artifact))
            rendered = sink.getvalue()
        self.assertIn("art-bin", rendered)
        self.assertNotIn("Artifact Preview", rendered)
        self.assertNotIn("PNG-ish", rendered)

    def test_selection_helpers_preserve_ids_after_inserted_rows(self) -> None:
        """Keep selection attached to row ids after live reload inserts new rows."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        runs = workflow_tui.load_fixture(str(FIXTURE))
        selected_run_key = workflow_tui.item_key("runs", runs[1], 1)
        inserted_run = dict(runs[0], run_id="wf-newer", title="Newer inserted workflow")
        self.assertEqual(workflow_tui.index_for_key([inserted_run, *runs], "runs", selected_run_key), 2)

        phases = workflow_tui.rows_for_tab(runs[0], "phases", runs)
        selected_phase_key = workflow_tui.item_key("phases", phases[1], 1)
        inserted_phase = dict(phases[0], phase_id="phase-newer", name="Newer phase")
        self.assertEqual(workflow_tui.index_for_key([inserted_phase, *phases], "phases", selected_phase_key), 2)

        events = workflow_tui.rows_for_tab(runs[0], "events", runs)
        selected_event_key = workflow_tui.item_key("events", events[1], 1)
        inserted_event = dict(events[0], event_id="evt-newer", ts="2026-06-11T00:06:00Z")
        updated_events = sorted([inserted_event, *events], key=lambda item: str(item.get("ts", "")), reverse=True)
        self.assertEqual(workflow_tui.index_for_key(updated_events, "events", selected_event_key), 2)

    def test_live_telemetry_fixture_extracts_output_tools_and_tokens(self) -> None:
        """Parse fixture logs into live output, latest tool calls, and token stats."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        run = workflow_tui.load_fixture(str(E2E_FIXTURE))[0]
        parsed = workflow_tui.parse_json_activity((E2E_FIXTURE.parent / "logs" / "tests.jsonl").read_text(encoding="utf-8"))
        activity = workflow_tui.agent_activity(run["agents"][0], run)
        aggregate = workflow_tui.collect_run_activity(run)
        self.assertIn("verified by the tool call", parsed["latest_output"])
        self.assertEqual(activity["tool_call_count"], 1)
        self.assertEqual(activity["tokens"]["total"], 1234)
        self.assertTrue(activity["tokens"]["known"])
        self.assertEqual(activity["tokens"]["total_source"], "reported_total")
        self.assertIn("/usr/bin/python3", "\n".join(activity["tool_calls"]))
        self.assertIn("F(20) = 6765", activity["latest_output"])
        self.assertEqual(aggregate["tool_call_count"], 1)
        self.assertEqual(aggregate["tokens"]["total"], 1234)
        self.assertEqual(workflow_tui.format_token_total(aggregate["tokens"]), "1234")

    def test_tool_call_summary_compacts_path_inputs(self) -> None:
        """Keep file tool calls readable by compacting path-like arguments."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        event = {
            "type": "tool_use",
            "part": {
                "type": "tool",
                "tool": "read",
                "state": {
                    "status": "completed",
                    "input": {"filePath": "/home/xertrov/tmp/workflow/very/long/tree/planning-output/draft/08-implementation-task-dag.md"},
                },
            },
        }

        summary = workflow_tui.summarize_tool_call(event)

        self.assertIn("read · completed", summary)
        self.assertIn("08-implementation-task-dag.md", summary)
        self.assertNotIn("filePath", summary)

    def test_token_totals_are_unknown_without_provider_usage(self) -> None:
        """Do not present missing provider usage as a real zero-token count."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        activity = workflow_tui.parse_text_activity("[assistant] completed without usage metadata")
        self.assertFalse(activity["tokens"]["known"])
        self.assertEqual(workflow_tui.format_token_total(activity["tokens"]), "unknown")

    def test_token_totals_derive_from_provider_parts_when_total_absent(self) -> None:
        """Derive totals only from provider-reported token parts, and label the result."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        activity = workflow_tui.parse_json_activity(
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "input_tokens_details": {"cached_tokens": 4},
                        "output_tokens_details": {"reasoning_tokens": 2},
                    },
                }
            )
        )
        self.assertTrue(activity["tokens"]["known"])
        self.assertEqual(activity["tokens"]["total"], 17)
        self.assertEqual(activity["tokens"]["cached_input"], 4)
        self.assertEqual(activity["tokens"]["reasoning"], 2)
        self.assertEqual(activity["tokens"]["total_source"], "derived_from_provider_parts")
        self.assertEqual(workflow_tui.format_token_total(activity["tokens"]), "17 derived")

    def test_json_activity_extracts_todos_from_todo_tool_events(self) -> None:
        """Provider TodoWrite-like tool payloads should feed the live todo panel."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel
        import workflow_tui_live  # pylint: disable=import-outside-toplevel

        activity = workflow_tui.parse_json_activity(
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {
                        "type": "tool",
                        "tool": "TodoWrite",
                        "state": {
                            "status": "completed",
                            "input": {
                                "todos": [
                                    {"content": "Inspect parser", "status": "completed"},
                                    {"content": "Update snapshots", "status": "in_progress"},
                                ]
                            },
                        },
                    },
                }
            )
        )

        self.assertEqual(activity["todos"][0]["content"], "Inspect parser")
        self.assertEqual(activity["todos"][1]["status"], "in_progress")
        self.assertEqual(
            workflow_tui_live.todo_status_text(activity["todos"]),
            "[✓] Inspect parser\n[~] Update snapshots",
        )

    def test_json_activity_extracts_actual_thinking_text_only_when_present(self) -> None:
        """Show real provider reasoning text without inventing it from token counts."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        with_text = workflow_tui.parse_json_activity(
            json.dumps({"type": "reasoning", "part": {"type": "reasoning", "text": "Checking edge cases."}})
        )
        without_text = workflow_tui.parse_json_activity(
            json.dumps({"type": "turn.completed", "usage": {"output_tokens_details": {"reasoning_tokens": 12}}})
        )

        self.assertEqual(with_text["latest_thinking"], "Checking edge cases.")
        self.assertEqual(without_text["tokens"]["reasoning"], 12)
        self.assertEqual(without_text["latest_thinking"], "")

    def test_aggregate_token_total_marks_unknown_agents(self) -> None:
        """Mark run token totals incomplete when any agent has no usage telemetry."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            known = tmp_path / "known.jsonl"
            known.write_text(json.dumps({"type": "turn.completed", "usage": {"total_tokens": 10}}) + "\n", encoding="utf-8")
            run = {
                "agents": [
                    {"agent_id": "known", "name": "Known", "status": "completed", "jsonl_path": str(known), "output_path": ""},
                    {"agent_id": "unknown", "name": "Unknown", "status": "completed", "jsonl_path": str(tmp_path / "missing.jsonl"), "output_path": ""},
                ]
            }
            aggregate = workflow_tui.collect_run_activity(run)
        self.assertEqual(aggregate["tokens"]["total"], 10)
        self.assertEqual(aggregate["tokens"]["known_agents"], 1)
        self.assertEqual(aggregate["tokens"]["unknown_agents"], 1)
        self.assertEqual(workflow_tui.format_token_total(aggregate["tokens"]), "10+?")

    def test_run_activity_reports_longest_completed_agent(self) -> None:
        """Show the slowest completed agent when no worker is active."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        run = {
            "agents": [
                {"agent_id": "fast", "name": "Fast", "status": "completed", "duration_seconds": 12.0},
                {"agent_id": "slow", "name": "Slow", "status": "completed", "duration_seconds": 125.0},
            ],
        }

        aggregate = workflow_tui.collect_run_activity(run)

        self.assertEqual(aggregate["longest_completed"]["name"], "Slow")
        self.assertIn("Slow", workflow_tui.longest_agent_label(aggregate))
        self.assertIn("2m", workflow_tui.longest_agent_label(aggregate))

    def test_run_detail_lists_all_running_agents_with_elapsed_time(self) -> None:
        """Run detail should not hide active workers behind the single longest label."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        run = {
            "run_id": "wf-running",
            "title": "Running Workers",
            "status": "running",
            "agents": [
                {"agent_id": "a", "name": "Alpha", "status": "running", "started_at": "2026-06-11T00:01:00Z"},
                {"agent_id": "b", "name": "Beta", "status": "running", "started_at": "2026-06-11T00:04:30Z"},
            ],
            "metrics": {"agents_total": 2, "phases_total": 1, "checks_total": 0},
        }
        old_now = os.environ.get("WORKFLOW_TUI_SNAPSHOT_NOW")
        os.environ["WORKFLOW_TUI_SNAPSHOT_NOW"] = "2026-06-11T00:06:00Z"
        try:
            sink = io.StringIO()
            console = Console(file=sink, width=100, force_terminal=False, color_system=None)
            console.print(workflow_tui.make_run_detail(run))
        finally:
            if old_now is None:
                os.environ.pop("WORKFLOW_TUI_SNAPSHOT_NOW", None)
            else:
                os.environ["WORKFLOW_TUI_SNAPSHOT_NOW"] = old_now

        rendered = sink.getvalue()
        self.assertIn("Running Agents", rendered)
        self.assertIn("Alpha", rendered)
        self.assertIn("5m 00s", rendered)
        self.assertIn("Beta", rendered)
        self.assertIn("1m 30s", rendered)

    @slow_test
    def test_skill_update_check_reports_remote_git_update(self) -> None:
        """Detect a newer upstream commit without mutating the local checkout."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        with tempfile.TemporaryDirectory() as tmp:
            source, skill, first_head = self.make_update_repos(Path(tmp))
            latest_head = self.write_commit(source, "remote update")
            self.git(source, "push")

            status = workflow_tui.check_skill_update(skill, timeout=5.0)

            self.assertEqual(status.state, "available")
            self.assertEqual(status.local_head, first_head)
            self.assertEqual(status.remote_head, latest_head)
            self.assertEqual(self.git(skill, "rev-parse", "HEAD").stdout.strip(), first_head)

    @slow_test
    def test_skill_update_action_pulls_ff_only_and_rechecks_status(self) -> None:
        """Update the skill checkout with git pull --ff-only and return current status."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        with tempfile.TemporaryDirectory() as tmp:
            source, skill, _first_head = self.make_update_repos(Path(tmp))
            latest_head = self.write_commit(source, "second")
            self.git(source, "push")

            result = workflow_tui.update_skill_from_git(skill, timeout=5.0, check_timeout=5.0)

            self.assertTrue(result.success)
            self.assertEqual(result.status.state, "current")
            self.assertEqual(result.status.local_head, latest_head)
            self.assertEqual(self.git(skill, "rev-parse", "HEAD").stdout.strip(), latest_head)

    @slow_test
    def test_skill_update_check_reports_missing_upstream_as_unavailable(self) -> None:
        """Explain when a checkout cannot be checked because no upstream branch exists."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "skill"
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, text=True, capture_output=True)
            self.write_commit(repo, "local only")

            status = workflow_tui.check_skill_update(repo, timeout=5.0)

            self.assertEqual(status.state, "unavailable")
            self.assertIn("upstream", status.message)

    def test_jsonl_tail_detection_survives_mid_line_prefix(self) -> None:
        """Treat tailed JSONL as JSON even when the first line is truncated."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        text = "\n".join(
            [
                'truncated prefix that is not json"}',
                json.dumps({"type": "item.completed", "item": {"id": "cmd-1", "type": "command_execution", "command": "echo ok", "status": "completed"}}),
                json.dumps({"type": "item.completed", "item": {"id": "msg-1", "type": "agent_message", "text": "json parser survived"}}),
            ]
        )
        self.assertTrue(workflow_tui.should_parse_json_activity(text, Path("worker.jsonl")))
        activity = workflow_tui.parse_json_activity(text)
        self.assertEqual(activity["tool_call_count"], 1)
        self.assertIn("json parser survived", activity["latest_output"])

    def test_agent_activity_drops_partial_jsonl_tail_record(self) -> None:
        """Do not count a normal tail boundary as a malformed JSONL event."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            jsonl_path = tmp_path / "worker.jsonl"
            output_path = tmp_path / "worker.final.md"
            output_path.write_text("final answer\n", encoding="utf-8")
            long_event = json.dumps({"type": "text", "part": {"text": "x" * 2000}})
            valid_event = json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "cmd-1", "type": "command_execution", "command": "echo ok", "status": "completed"},
                }
            )
            final_event = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "tail parser survived"}})
            jsonl_path.write_text(f"{long_event}\n{valid_event}\n{final_event}\n", encoding="utf-8")

            old_tail_bytes = workflow_tui.TAIL_BYTES
            workflow_tui.TAIL_BYTES = 512
            try:
                activity = workflow_tui.agent_activity({"jsonl_path": str(jsonl_path), "output_path": str(output_path), "log_path": ""})
            finally:
                workflow_tui.TAIL_BYTES = old_tail_bytes

            self.assertEqual(activity["parse_errors"], 0)
            self.assertEqual(activity["tool_call_count"], 1)
            self.assertEqual(activity["latest_output"], "final answer")

    def test_formatted_ccc_text_in_jsonl_path_uses_text_parser(self) -> None:
        """Do not discard ccc formatted stdout just because workflow captured it as .jsonl."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        text = "\n".join(["[tool:start] bash: echo ok", "[tool:result] ok", "[assistant] done"])
        self.assertFalse(workflow_tui.should_parse_json_activity(text, Path("ccc-worker.jsonl")))
        activity = workflow_tui.parse_text_activity(text)
        self.assertEqual(activity["tool_call_count"], 1)
        self.assertIn("done", activity["latest_output"])

    def test_ccc_footer_prefers_jsonl_transcript_for_activity(self) -> None:
        """Use ccc transcript.jsonl when the footer exposes a completed artifact dir."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ccc_dir = tmp_path / "ccc-run"
            ccc_dir.mkdir()
            stderr_path = tmp_path / "worker.stderr.log"
            captured_path = tmp_path / "worker.jsonl"
            output_path = tmp_path / "fallback.md"
            stderr_path.write_text(f">> ccc:output-log >> {ccc_dir}\n", encoding="utf-8")
            captured_path.write_text("[assistant] stale formatted capture\n", encoding="utf-8")
            (ccc_dir / "transcript.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({"type": "text", "part": {"text": "live json transcript"}}),
                        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 4, "output_tokens": 6}}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (ccc_dir / "output.txt").write_text("final ccc output\n", encoding="utf-8")
            activity = workflow_tui.agent_activity(
                {
                    "agent_id": "agent-ccc",
                    "name": "CCC",
                    "status": "completed",
                    "jsonl_path": str(captured_path),
                    "log_path": str(stderr_path),
                    "output_path": str(output_path),
                }
            )
            self.assertEqual(activity["transcript_path"], str(ccc_dir / "transcript.jsonl"))
            self.assertEqual(activity["tokens"]["total"], 10)
            self.assertEqual(activity["tokens"]["total_source"], "derived_from_provider_parts")
            self.assertIn("final ccc output", activity["latest_output"])

    def test_opencode_tool_use_events_feed_tui_telemetry(self) -> None:
        """Parse OpenCode tool_use and step_finish token records for the TUI."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        activity = workflow_tui.parse_json_activity(
            "\n".join(
                [
                    json.dumps({"type": "text", "timestamp": 1781158880831, "part": {"type": "text", "text": "Checking with bash."}}),
                    json.dumps(
                        {
                            "type": "tool_use",
                            "timestamp": 1781158882007,
                            "part": {
                                "type": "tool",
                                "tool": "bash",
                                "callID": "call_1",
                                "state": {"status": "completed", "input": {"command": "python3 -c 'print(6765)'"}, "title": "Verify F(20)"},
                            },
                        }
                    ),
                    json.dumps({"type": "step_finish", "part": {"type": "step-finish", "tokens": {"total": 777, "input": 700, "output": 77}}}),
                ]
            )
        )
        self.assertEqual(activity["tool_call_count"], 1)
        self.assertEqual(activity["tokens"]["total"], 777)
        self.assertIn("bash", "\n".join(activity["tool_calls"]))
        self.assertIn("Checking with bash", activity["latest_output"])

    def test_text_transcript_groups_multiline_tool_blocks(self) -> None:
        """Count one ccc tool call per grouped start/result block, not per line."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        activity = workflow_tui.parse_text_activity(
            "\n".join(
                [
                    "[tool:start] bash: python3 -c \"",
                    "[tool:start] print(6765)",
                    "[tool:result] bash (ok): python3 -c \"",
                    "[tool:result] 6765",
                    "[assistant] F(20) = 6765",
                ]
            )
        )
        self.assertEqual(activity["tool_call_count"], 1)
        self.assertIn("F(20) = 6765", activity["latest_output"])

    def test_kimi_text_transcript_counts_only_used_tool_lines(self) -> None:
        """Avoid treating Kimi chat/thinking lines that mention tools as tool calls."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        activity = workflow_tui.parse_text_activity(
            "\n".join(
                [
                    "• I need to use the command broker docs, but this is just thinking.",
                    "• Used ReadFile (docs/10_filesystem_command_model.md)",
                    "• This command model has several constraints.",
                    "• Used Shell (mkdir -p planning-output/draft)",
                    "• The tool call list should not include this chat line.",
                ]
            )
        )
        self.assertEqual(activity["tool_call_count"], 2)
        joined_tools = "\n".join(activity["tool_calls"])
        self.assertIn("ReadFile", joined_tools)
        self.assertIn("Shell", joined_tools)
        self.assertNotIn("chat line", joined_tools)

    def test_live_stats_grid_uses_horizontal_space(self) -> None:
        """Render live stats as multiple label/value pairs per row."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        console = Console(width=100, record=True, file=io.StringIO())
        console.print(workflow_tui.make_facts_grid([("tokens", "10"), ("tools", "2"), ("active", "1"), ("agents", "4")], columns=4))
        rendered = console.export_text()
        self.assertIn("tokens", rendered)
        self.assertIn("tools", rendered.splitlines()[0])
        self.assertIn("active", rendered.splitlines()[0])
        self.assertIn("agents", rendered.splitlines()[0])

    def test_run_detail_labels_tool_count_as_tail_window_count(self) -> None:
        """Do not present tailed transcript tool counts as a cumulative total."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        run = workflow_tui.load_fixture(str(E2E_FIXTURE))[0]
        sink = io.StringIO()
        console = Console(file=sink, width=120, force_terminal=False, color_system=None)
        console.print(workflow_tui.make_run_detail(run))
        rendered = sink.getvalue()

        self.assertIn("tail tools", rendered)
        self.assertNotIn("tool calls", rendered)

    @slow_test
    def test_snapshot_agent_live_panels_match_fixture(self) -> None:
        """Snapshot live output and latest tool-call panels from a static fixture."""
        rendered = self.run_script(
            "workflow_tui.py",
            "--snapshot",
            "--fixture",
            str(E2E_FIXTURE),
            "--tab",
            "agents",
            "--width",
            "120",
            "--height",
            "36",
            env=self.snapshot_env(),
        ).stdout
        expected = (SNAPSHOTS / "snapshot-agent-live-panels.txt").read_text(encoding="utf-8")
        self.assertEqual(rendered, expected)
        self.assertIn("Live Output", rendered)
        self.assertIn("Latest Tool Calls", rendered)
        self.assertIn("/usr/bin/python3", rendered)
        self.assertIn("model         gpt-5.5", rendered)
        self.assertIn("F(20) = 6765", rendered)

    @slow_test
    def test_snapshot_agent_prompt_view_matches_fixture(self) -> None:
        """Prompt mode shows the agent prompt instead of the live output panel."""
        rendered = self.run_script(
            "workflow_tui.py",
            "--snapshot",
            "--fixture",
            str(E2E_FIXTURE),
            "--tab",
            "agents",
            "--agent-view",
            "prompt",
            "--width",
            "120",
            "--height",
            "36",
            env=self.snapshot_env(),
        ).stdout
        expected = (SNAPSHOTS / "snapshot-agent-prompt-panel.txt").read_text(encoding="utf-8")
        self.assertEqual(rendered, expected)
        self.assertIn("Prompt", rendered)
        self.assertIn("Compute a small independent verification", rendered)
        self.assertNotIn("Live Output", rendered)

    @slow_test
    def test_snapshot_run_live_panels_match_fixture(self) -> None:
        """Keep run-level live output visible above prompt and metrics panels."""
        rendered = self.run_script(
            "workflow_tui.py",
            "--snapshot",
            "--fixture",
            str(E2E_FIXTURE),
            "--tab",
            "runs",
            "--width",
            "120",
            "--height",
            "36",
            env=self.snapshot_env(),
        ).stdout
        expected = (SNAPSHOTS / "snapshot-run-live-panels.txt").read_text(encoding="utf-8")
        self.assertEqual(rendered, expected)
        self.assertLess(rendered.index("Live Output"), rendered.index("Latest Tool Calls"))
        self.assertLess(rendered.index("Live Output"), rendered.index("Prompt"))
        self.assertIn("F(20) = 6765", rendered)

    def test_e2e_fixture_paths_resolve_under_fixture_dir(self) -> None:
        """Keep telemetry fixture paths self-contained and portable."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        run = workflow_tui.load_fixture(str(E2E_FIXTURE))[0]
        fixture_dir = E2E_FIXTURE.parent.resolve()
        for key in ("jsonl_path", "log_path", "output_path"):
            resolved = workflow_tui.resolve_agent_path(run["agents"][0], key, run)
            self.assertIsNotNone(resolved)
            self.assertTrue(Path(resolved).resolve().is_relative_to(fixture_dir))
        missing = dict(run["agents"][0], jsonl_path="logs/not-yet-written.jsonl")
        resolved_missing = workflow_tui.resolve_agent_path(missing, "jsonl_path", run)
        self.assertTrue(Path(resolved_missing).resolve().is_relative_to(fixture_dir))

    def test_state_event_cli_records_kind_operation_and_source(self) -> None:
        """Manual events carry first-class metadata for future TUI/tooling."""
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(Path(tmp) / "state")
            created = self.run_script(
                "workflow_state.py",
                "init",
                "--title",
                "Event Metadata",
                "--prompt",
                "exercise rich event metadata",
                "--cwd",
                str(ROOT),
                env=env,
            )
            run_id = json.loads(created.stdout)["run_id"]
            event = self.run_script(
                "workflow_state.py",
                "event",
                run_id,
                "--message",
                "Claude docs researched for agent-view navigation",
                "--kind",
                "research",
                "--operation",
                "docs_researched",
                "--source",
                "unit-test",
                "--data-json",
                '{"ui_area":"agent_view"}',
                env=env,
            )
            data = json.loads(event.stdout)
            self.assertEqual(data["kind"], "research")
            self.assertEqual(data["operation"], "docs_researched")
            self.assertEqual(data["source"], "unit-test")
            self.assertEqual(data["data"]["ui_area"], "agent_view")

    @slow_test
    def test_snapshot_minimum_size_matches_live_tui(self) -> None:
        """Reject snapshot terminals smaller than the live TUI can draw cleanly."""
        rendered = self.run_script(
            "workflow_tui.py",
            "--snapshot",
            "--fixture",
            str(FIXTURE),
            "--width",
            "79",
            "--height",
            "12",
            env=self.snapshot_env(),
        ).stdout
        self.assertEqual(rendered, "terminal too small; need at least 80x12\n")

    def test_live_dashboard_minimum_matches_documented_terminal_size(self) -> None:
        """Ensure an 80x12 live terminal has enough dashboard content space."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        runs = workflow_tui.load_fixture(str(FIXTURE))
        sink = io.StringIO()
        console = Console(width=80, color_system=None, force_terminal=False, legacy_windows=False, file=sink)
        console.print(
            workflow_tui.render_dashboard(
                runs,
                width=78,
                height=9,
                tab="runs",
                run_index=0,
                row_index=0,
                chrome=False,
            )
        )
        rendered = sink.getvalue()
        self.assertIn("Runs", rendered)
        self.assertNotIn("terminal too small", rendered)

        small_sink = io.StringIO()
        small_console = Console(width=80, color_system=None, force_terminal=False, legacy_windows=False, file=small_sink)
        small_console.print(
            workflow_tui.render_dashboard(
                runs,
                width=77,
                height=8,
                tab="runs",
                run_index=0,
                row_index=0,
                chrome=False,
            )
        )
        self.assertIn("terminal too small; need at least 80x12", small_sink.getvalue())

    def test_narrow_snapshot_uses_compact_tab_labels(self) -> None:
        """Keep tab labels readable in a normal 80-column terminal."""
        rendered = self.run_script(
            "workflow_tui.py",
            "--snapshot",
            "--fixture",
            str(FIXTURE),
            "--tab",
            "events",
            "--width",
            "80",
            "--height",
            "24",
            env=self.snapshot_env(),
        ).stdout
        expected = (SNAPSHOTS / "snapshot-narrow-events.txt").read_text(encoding="utf-8")
        self.assertEqual(rendered, expected)
        self.assertIn("● evt", rendered)
        self.assertIn("time          10:05:00 AEST", rendered)
        self.assertIn("dec  art", rendered)
        self.assertIn("wf-fixture-rich/run.json", rendered)

    def test_selected_row_stays_visible_in_snapshot_tables(self) -> None:
        """Render a long table and confirm a selected lower row is kept on screen."""
        rendered = self.run_script(
            "workflow_tui.py",
            "--snapshot",
            "--fixture",
            str(MANY_FIXTURE),
            "--tab",
            "agents",
            "--width",
            "110",
            "--height",
            "30",
            "--row-index",
            "11",
            env=self.snapshot_env(),
        ).stdout
        expected = (SNAPSHOTS / "snapshot-many-agents.txt").read_text(encoding="utf-8")
        self.assertEqual(rendered, expected)
        self.assertIn("▸", rendered)
        self.assertIn("agent-11", rendered)
        self.assertIn("agent_id      agent-11", rendered)

    @slow_test
    def test_snapshot_scroll_selects_later_rows(self) -> None:
        """Exercise snapshot scroll offsets used by fixture-driven visual review."""
        rendered = self.run_script(
            "workflow_tui.py",
            "--snapshot",
            "--fixture",
            str(MANY_FIXTURE),
            "--tab",
            "agents",
            "--width",
            "110",
            "--height",
            "30",
            "--row-index",
            "0",
            "--scroll",
            "8",
            env=self.snapshot_env(),
        ).stdout
        expected = (SNAPSHOTS / "snapshot-many-agents-scroll.txt").read_text(encoding="utf-8")
        self.assertEqual(rendered, expected)
        self.assertIn("agent-08", rendered)
        self.assertIn("agent_id      agent-08", rendered)

    def test_tmux_qa_script_dry_run_lists_navigation_actions(self) -> None:
        """Keep the tmux visual QA harness easy to inspect before it drives a terminal."""
        result = self.run_script(
            "workflow_tui_tmux_qa.py",
            "--dry-run",
            "--fixture",
            str(FIXTURE),
            "--session",
            "workflow-tui-test",
        )
        self.assertIn("workflow-tui-test", result.stdout)
        self.assertIn("capture initial-overview", result.stdout)
        self.assertIn("send-key Right", result.stdout)
        self.assertIn("send-key y", result.stdout)
        self.assertIn("send-key p", result.stdout)

    def test_tmux_qa_default_command_targets_local_checkout(self) -> None:
        """Keep tmux QA from accidentally testing a stale installed workflow command."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui_tmux_qa  # pylint: disable=import-outside-toplevel

        command = workflow_tui_tmux_qa.workflow_command(None)

        self.assertEqual(command, str(SCRIPTS / "wf"))

    def test_tmux_qa_staging_copies_fixture_assets_and_logs(self) -> None:
        """Keep staged tmux QA runs backed by fixture-relative artifacts and logs."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui_tmux_qa  # pylint: disable=import-outside-toplevel

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = workflow_tui_tmux_qa.prepare_state(FIXTURE, tmp_path / "rich")
            rich_run_dir = state_dir / "runs" / "wf-fixture-rich"
            staged_report = rich_run_dir / "artifacts" / "final-report.md"
            expected_report = FIXTURE.parent / "artifacts" / "final-report.md"
            self.assertEqual(staged_report.read_text(encoding="utf-8"), expected_report.read_text(encoding="utf-8"))

            e2e_state_dir = workflow_tui_tmux_qa.prepare_state(E2E_FIXTURE, tmp_path / "e2e")
            e2e_run_dir = e2e_state_dir / "runs" / "wf-fixture-e2e-live"
            for relative_path in ("artifacts/tests.md", "logs/tests.jsonl", "logs/tests.stderr.log"):
                with self.subTest(relative_path=relative_path):
                    staged_asset = e2e_run_dir / relative_path
                    fixture_asset = E2E_FIXTURE.parent / relative_path
                    self.assertEqual(staged_asset.read_text(encoding="utf-8"), fixture_asset.read_text(encoding="utf-8"))

        expected_artifact_text = workflow_tui_tmux_qa.EXPECTED_CAPTURE_TEXT["artifacts"]
        self.assertIn("Artifact Preview", expected_artifact_text)
        self.assertIn("Final synthesis report", expected_artifact_text)
        self.assertIn("Security: no critical issues", expected_artifact_text)

    def test_fibonacci_stress_creates_full_99_agent_tree(self) -> None:
        """Exercise the full F(100) manual-agent tree in isolated workflow state."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result = self.run_script(
                "workflow_fibonacci_stress.py",
                "--n",
                "100",
                "--output-dir",
                str(tmp_path / "out"),
                "--state-dir",
                str(tmp_path / "state"),
            )
            summary = json.loads(result.stdout)
            run = json.loads(Path(summary["run_json"]).read_text(encoding="utf-8"))
            timing = json.loads(Path(summary["timing_path"]).read_text(encoding="utf-8"))
            self.assertEqual(summary["answer"], "354224848179261915075")
            self.assertEqual(summary["agents_total"], 99)
            self.assertEqual(run["status"], "completed")
            self.assertEqual(run["metrics"]["agents_total"], 99)
            self.assertEqual(len(run["phases"]), 7)
            self.assertEqual(len(run["phases"][0]["agent_ids"]), 50)
            self.assertEqual([len(phase["agent_ids"]) for phase in run["phases"][1:]], [25, 12, 6, 3, 2, 1])
            self.assertEqual({artifact["kind"] for artifact in run["artifacts"]}, {"answer", "reduction-tree", "timing"})
            self.assertEqual(timing["agents_total"], 99)
            self.assertTrue((Path(summary["archive_dir"]) / "run.json").exists())

    def test_fibonacci_stress_labels_non_100_runs_dynamically(self) -> None:
        """Keep stress-run titles and decisions accurate for smaller Fibonacci runs."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result = self.run_script(
                "workflow_fibonacci_stress.py",
                "--n",
                "20",
                "--output-dir",
                str(tmp_path / "out"),
                "--state-dir",
                str(tmp_path / "state"),
            )
            summary = json.loads(result.stdout)
            run = json.loads(Path(summary["run_json"]).read_text(encoding="utf-8"))

            self.assertEqual(summary["agents_total"], 19)
            self.assertIn("19-agent", run["title"])
            self.assertIn("Use 19 scripted manual agents", run["decisions"][0]["title"])
            self.assertIn("F(20) has 10 independent binomial terms", run["decisions"][0]["rationale"])
            self.assertNotIn("F(100)", run["decisions"][0]["rationale"])

    def test_runner_matrix_parses_direct_and_ccc_targets(self) -> None:
        """Normalize direct runners and ccc runner or preset targets."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_runner_matrix  # pylint: disable=import-outside-toplevel

        direct = workflow_runner_matrix.parse_target("kimi-direct")
        ccc_runner = workflow_runner_matrix.parse_target("ccc:kimi")
        ccc_preset = workflow_runner_matrix.parse_target("minimax=ccc:@mm")

        self.assertEqual(direct.label, "kimi-direct")
        self.assertEqual(direct.runner, "kimi-direct")
        self.assertIsNone(direct.ccc_runner)
        self.assertEqual(ccc_runner.label, "ccc-kimi")
        self.assertEqual(ccc_runner.runner, "ccc")
        self.assertEqual(ccc_runner.ccc_runner, "kimi")
        self.assertEqual(ccc_preset.label, "minimax")
        self.assertEqual(ccc_preset.runner, "ccc")
        self.assertEqual(ccc_preset.ccc_runner, "@mm")

    @slow_test
    def test_runner_matrix_mock_run_archives_each_target(self) -> None:
        """Run the reusable runner matrix without model calls and archive every run."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            result = self.run_script(
                "workflow_runner_matrix.py",
                "--title",
                "Reusable Matrix",
                "--output-dir",
                str(tmp_path / "out"),
                "--target",
                "kimi-direct",
                "--target",
                "minimax=ccc:@mm",
                "--mock",
                "--startup-delay",
                "0",
                "--max-agents",
                "2",
                env=env,
            )
            summary = json.loads(result.stdout)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual([item["label"] for item in summary["targets"]], ["kimi-direct", "minimax"])
            self.assertTrue(Path(summary["summary_path"]).exists())
            for item in summary["targets"]:
                self.assertEqual(item["status"], "completed")
                self.assertEqual(item["jobs"], 3)
                self.assertTrue((Path(item["archive_dir"]) / "run.json").exists())
                self.assertTrue((Path(item["archive_dir"]) / "stdout.log").exists())
                self.assertTrue((Path(item["archive_dir"]) / "stderr.log").exists())
            first_run = json.loads((Path(summary["targets"][0]["archive_dir"]) / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(first_run["mode"], "kimi-direct")
            self.assertEqual(first_run["metrics"]["agents_total"], 3)

    @slow_test
    def test_runner_matrix_archive_run_json_is_portable_for_tui_replay(self) -> None:
        """Archived ccc runs should still render after live state and ccc dirs vanish."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_runner_matrix  # pylint: disable=import-outside-toplevel
        import workflow_tui  # pylint: disable=import-outside-toplevel

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "bin"
            fake_bin.mkdir()
            fake_ccc = fake_bin / "ccc"
            fake_run_dir = tmp_path / "ccc-run"
            fake_ccc.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import os
                    import sys
                    from pathlib import Path

                    run_dir = Path(os.environ["CCC_FAKE_RUN_DIR"])
                    run_dir.mkdir(parents=True, exist_ok=True)
                    (run_dir / "output.txt").write_text("archived final output\\n", encoding="utf-8")
                    events = [
                        {"type": "text", "part": {"text": "archived live output"}},
                        {
                            "type": "tool_use",
                            "part": {
                                "type": "tool",
                                "tool": "read",
                                "callID": "call_archive",
                                "state": {"status": "completed", "input": {"filePath": "README.md"}},
                            },
                        },
                        {"type": "step_finish", "part": {"tokens": {"total": 99, "input": 90, "output": 9}}},
                    ]
                    transcript = "\\n".join(json.dumps(event) for event in events) + "\\n"
                    (run_dir / "transcript.jsonl").write_text(transcript, encoding="utf-8")
                    sys.stdout.write(transcript)
                    sys.stderr.write(f">> ccc:output-log >> {run_dir}\\n")
                    """
                ),
                encoding="utf-8",
            )
            fake_ccc.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            env["CCC_FAKE_RUN_DIR"] = str(fake_run_dir)
            launched = self.run_script(
                "workflow_run.py",
                "--title",
                "Portable Archive",
                "--cwd",
                str(ROOT),
                "--runner",
                "ccc-opencode",
                "--startup-delay",
                "0",
                "--job",
                "alpha::Alpha prompt",
                env=env,
            )
            run_id = json.loads(launched.stdout.split("\ncommand:", 1)[0])["run_id"]
            live_run_json = tmp_path / "state" / "runs" / run_id / "run.json"
            archive_dir = tmp_path / "archive" / run_id
            workflow_runner_matrix.copy_run_archive(live_run_json, archive_dir)
            shutil.rmtree(live_run_json.parent)
            shutil.rmtree(fake_run_dir)

            archived_run = json.loads((archive_dir / "run.json").read_text(encoding="utf-8"))
            agent = archived_run["agents"][0]
            self.assertEqual(archived_run["paths"]["run_json"], "run.json")
            self.assertEqual(agent["jsonl_path"], f"logs/{agent['agent_id']}.jsonl")
            self.assertEqual(agent["output_path"], f"artifacts/{agent['agent_id']}.final.md")
            self.assertEqual(agent["transcript_path"], agent["jsonl_path"])
            self.assertEqual(agent["activity_output_path"], agent["output_path"])
            self.assertTrue(all(str(artifact["path"]).startswith("artifacts/") for artifact in archived_run["artifacts"]))
            loaded_run = workflow_tui.load_fixture(str(archive_dir / "run.json"))[0]
            activity = workflow_tui.agent_activity(loaded_run["agents"][0], loaded_run)
            self.assertEqual(activity["tool_call_count"], 1)
            self.assertEqual(activity["tokens"]["total"], 99)
            self.assertIn("archived final output", activity["latest_output"])

    @slow_test
    def test_runner_matrix_failed_phase_without_run_path_archives_logs(self) -> None:
        """Killed phases without a parsed run path should fail cleanly, not copy '.'."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_runner_matrix  # pylint: disable=import-outside-toplevel

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            phase = {"name": "draft", "title": "Draft", "jobs_file": tmp_path / "jobs.json"}
            phase["jobs_file"].write_text("[]\n", encoding="utf-8")
            spec = workflow_runner_matrix.WorkflowSpec("Spec", tmp_path / "workflow.json", [phase])
            target = workflow_runner_matrix.MatrixTarget("killed", "ccc", "@kimi")
            fake_result = subprocess.CompletedProcess(["fake"], -15, stdout="", stderr="terminated\n")
            with mock.patch.object(workflow_runner_matrix, "build_command", return_value=["fake"]), mock.patch.object(workflow_runner_matrix.subprocess, "run", return_value=fake_result):
                result = workflow_runner_matrix.run_phase(
                    target,
                    argparse.Namespace(),
                    spec,
                    phase,
                    tmp_path,
                    1,
                    tmp_path / "archive",
                )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["returncode"], -15)
            self.assertEqual(result["run_json"], "")
            self.assertEqual((Path(result["archive_dir"]) / "stderr.log").read_text(encoding="utf-8"), "terminated\n")

    @slow_test
    def test_runner_matrix_archives_script_declared_output_subdir(self) -> None:
        """Copy the workflow spec's output_subdir instead of hard-coding planning-output."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_runner_matrix  # pylint: disable=import-outside-toplevel

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "workdir"
            workdir.mkdir()
            phase = {"name": "draft", "title": "Draft", "jobs_file": tmp_path / "jobs.json"}
            phase["jobs_file"].write_text('[{"name":"a","prompt":"p"}]\n', encoding="utf-8")
            spec = workflow_runner_matrix.WorkflowSpec("Spec", tmp_path / "workflow.json", [phase], "custom-output")
            target = workflow_runner_matrix.MatrixTarget("mocked", "kimi-direct")
            args = argparse.Namespace(max_agents=1)

            def fake_run_phase(*_args: object, **_kwargs: object) -> dict[str, object]:
                (workdir / "custom-output").mkdir()
                (workdir / "custom-output" / "plan.md").write_text("custom\n", encoding="utf-8")
                return {"name": "draft", "title": "Draft", "status": "completed", "run_status": "completed", "returncode": 0, "run_id": "run-1", "run_json": "", "archive_dir": str(tmp_path / "archive" / "mocked" / "draft" / "run-1"), "jobs": 1, "duration_seconds": 0.0, "command": []}

            with (
                mock.patch.object(workflow_runner_matrix, "copy_project_for_target", return_value=workdir),
                mock.patch.object(workflow_runner_matrix, "workflow_spec_for_target", return_value=spec),
                mock.patch.object(workflow_runner_matrix, "run_phase", side_effect=fake_run_phase),
            ):
                result = workflow_runner_matrix.run_target(target, args, tmp_path / "out", tmp_path / "archive", None, {})

            self.assertEqual(result["status"], "completed")
            self.assertEqual((Path(result["workdir_output"]) / "plan.md").read_text(encoding="utf-8"), "custom\n")

    @slow_test
    def test_runner_matrix_uses_script_generated_workflow_and_project_copies(self) -> None:
        """Save script-generated workflow JSON and run each target in its own project copy."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            project_src = tmp_path / "project-src"
            project_src.mkdir()
            (project_src / "README.md").write_text("# Example Project\n", encoding="utf-8")
            (project_src / ".git").mkdir()
            (project_src / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            generator = tmp_path / "workflow_generator.py"
            generator.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import sys

                    project_dir = sys.argv[sys.argv.index("--project-dir") + 1]
                    print(json.dumps({
                        "title": "Generated Planning Workflow",
                        "phases": [
                            {"name": "draft", "jobs": [{
                                "name": "architecture",
                                "role": "planner",
                                "prompt": f"Read {project_dir} and produce an architecture plan."
                            }]},
                            {"name": "review", "jobs": [{
                                "name": "architecture-review",
                                "role": "reviewer",
                                "prompt": f"Review plans in {project_dir}."
                            }]}
                        ]
                    }))
                    """
                ),
                encoding="utf-8",
            )
            generator.chmod(0o755)
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            result = self.run_script(
                "workflow_runner_matrix.py",
                "--output-dir",
                str(tmp_path / "out"),
                "--project-src",
                str(project_src),
                "--workflow-script",
                str(generator),
                "--workflow-script-arg=--project-dir",
                "--workflow-script-arg",
                "{project_dir}",
                "--target",
                "kimi-direct",
                "--mock",
                "--startup-delay",
                "0",
                env=env,
            )
            summary = json.loads(result.stdout)
            workflow_file = Path(summary["workflow_file"])
            target = summary["targets"][0]
            workdir = Path(target["workdir"])
            self.assertTrue(workflow_file.exists())
            self.assertEqual(json.loads(workflow_file.read_text(encoding="utf-8"))["title"], "Generated Planning Workflow")
            self.assertTrue((workdir / "README.md").exists())
            self.assertFalse((workdir / ".git").exists())
            self.assertEqual(target["jobs"], 2)
            self.assertEqual([phase["name"] for phase in target["phases"]], ["draft", "review"])
            first_phase_archive = Path(target["phases"][0]["archive_dir"])
            run = json.loads((first_phase_archive / "run.json").read_text(encoding="utf-8"))
            self.assertIn(str(workdir), run["agents"][0]["prompt"])

    @slow_test
    def test_runner_matrix_applies_per_target_max_agents(self) -> None:
        """Allow runner families to use different concurrency caps."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            result = self.run_script(
                "workflow_runner_matrix.py",
                "--output-dir",
                str(tmp_path / "out"),
                "--target",
                "kimi=ccc:@kimi",
                "--target",
                "mimo25p=ccc:@mimo25p",
                "--target-max",
                "kimi=4",
                "--target-max",
                "mimo25p=8",
                "--mock",
                "--startup-delay",
                "0",
                env=env,
            )
            summary = json.loads(result.stdout)
            by_label = {target["label"]: target for target in summary["targets"]}
            self.assertEqual(by_label["kimi"]["max_agents"], 4)
            self.assertEqual(by_label["mimo25p"]["max_agents"], 8)
            self.assertIn("--max-agents", by_label["mimo25p"]["command"])
            self.assertEqual(by_label["mimo25p"]["command"][by_label["mimo25p"]["command"].index("--max-agents") + 1], "8")

    def test_project_planning_workflow_example_emits_workflow_plan(self) -> None:
        """Keep the reusable architecture-to-task workflow generator executable."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "examples" / "project_planning_workflow.py"),
                    "--project-dir",
                    str(tmp_path),
                ],
                check=True,
                text=True,
                capture_output=True,
            )
            plan = json.loads(result.stdout)
            self.assertEqual(plan["kind"], "workflow-plan")
            self.assertEqual([phase["name"] for phase in plan["phases"]], ["draft", "review", "correct", "synthesize", "final-review", "final-fix", "final-rereview"])
            self.assertGreaterEqual(sum(len(phase["jobs"]) for phase in plan["phases"]), 26)
            self.assertIn("individual implementation tasks", plan["goal"])
            self.assertEqual(plan["output_subdir"], "planning-output")
            self.assertTrue(all(str(tmp_path) not in job["prompt"] for phase in plan["phases"] for job in phase["jobs"]))
            self.assertTrue(all("Current working directory: `.`" in job["prompt"] for phase in plan["phases"] for job in phase["jobs"]))
            self.assertTrue(all("Do not launch nested agents" in job["prompt"] for phase in plan["phases"] for job in phase["jobs"]))

    def test_workflow_plan_normalization_is_shared_with_runner_matrix(self) -> None:
        """Keep workflow-file launchers and runner-matrix on one schema."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_plan  # pylint: disable=import-outside-toplevel
        import workflow_runner_matrix  # pylint: disable=import-outside-toplevel

        self.assertIs(workflow_runner_matrix.normalize_workflow, workflow_plan.normalize_workflow)

    def test_workflow_plan_preserves_phase_gate_metadata(self) -> None:
        """Keep declared phase gates/checks/decisions available to workflow apply."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_plan  # pylint: disable=import-outside-toplevel

        plan = workflow_plan.normalize_workflow(
            {
                "title": "Gated Plan",
                "decisions": [{"title": "Use worktrees", "rationale": "Parallel writes."}],
                "gates": [{"name": "merge", "kind": "integration"}],
                "phases": [
                    {
                        "name": "review",
                        "goal": "Review the draft.",
                        "gates": [{"name": "review-fix", "kind": "review-fix"}],
                        "checks": [{"name": "pytest", "cmd": "python3 -m unittest"}],
                        "decisions": [{"title": "Review model", "rationale": "Use codex."}],
                        "jobs": [{"name": "reviewer", "prompt": "Review."}],
                    }
                ],
            },
            fallback_title="fallback",
        )

        self.assertEqual(plan["decisions"][0]["title"], "Use worktrees")
        self.assertEqual(plan["gates"][0]["kind"], "integration")
        self.assertEqual(plan["phases"][0]["goal"], "Review the draft.")
        self.assertEqual(plan["phases"][0]["gates"][0]["kind"], "review-fix")
        self.assertEqual(plan["phases"][0]["planned_checks"][0]["name"], "pytest")
        self.assertEqual(plan["phases"][0]["decisions"][0]["title"], "Review model")

    def test_workflow_plan_preserves_job_lane_metadata(self) -> None:
        """Keep job-level cwd, write scope, and worktree lane metadata intact."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_plan  # pylint: disable=import-outside-toplevel

        plan = workflow_plan.normalize_workflow(
            {
                "title": "Lane Plan",
                "jobs": [
                    {
                        "name": "impl",
                        "prompt": "Implement.",
                        "cwd": "lane-a",
                        "write_scope": ["src/a"],
                        "worktree": {"branch": "lane/impl", "base": "main", "merge_target": "main"},
                    }
                ],
            },
            fallback_title="fallback",
        )

        job = plan["jobs"][0]
        self.assertEqual(job["cwd"], "lane-a")
        self.assertEqual(job["write_scope"], ["src/a"])
        self.assertEqual(job["worktree"]["branch"], "lane/impl")

    def test_wf_apply_mock_launches_saved_single_phase_plan_and_records_artifact(self) -> None:
        """Launch a saved workflow-plan JSON without model calls and persist the normalized plan."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_path = tmp_path / "plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "title": "Apply Plan",
                        "jobs": [
                            {"name": "alpha", "role": "tester", "prompt": "Say alpha."},
                            {"name": "beta", "prompt": "Say beta."},
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            result = self.run_wf(
                "apply",
                str(plan_path),
                "--mock",
                "--startup-delay",
                "0",
                "--max-agents",
                "2",
                env=env,
            )
            summary = json.loads(result.stdout.split("\ncommand:", 1)[0])
            run = json.loads(Path(summary["path"]).read_text(encoding="utf-8"))
            artifacts = [item for item in run["artifacts"] if item["kind"] == "workflow-plan"]
            recorded_plan = json.loads(Path(artifacts[0]["path"]).read_text(encoding="utf-8"))

            self.assertEqual(summary["jobs"], 2)
            self.assertEqual(run["title"], "Apply Plan")
            self.assertEqual(run["status"], "completed")
            self.assertEqual(recorded_plan["kind"], "workflow-plan")
            self.assertEqual([job["name"] for job in recorded_plan["jobs"]], ["alpha", "beta"])

    def test_wf_apply_accepts_python_plan_generator(self) -> None:
        """Accept executable or Python scripts that print workflow-plan JSON."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            generator = tmp_path / "generate_plan.py"
            generator.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    print(json.dumps({
                        "title": "Generated Apply Plan",
                        "jobs": [{"name": "generated", "prompt": "Generated prompt."}]
                    }))
                    """
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            result = self.run_wf("exec", str(generator), "--mock", "--startup-delay", "0", env=env)
            summary = json.loads(result.stdout.split("\ncommand:", 1)[0])
            run = json.loads(Path(summary["path"]).read_text(encoding="utf-8"))

            self.assertEqual(run["title"], "Generated Apply Plan")
            self.assertEqual(run["metrics"]["agents_total"], 1)

    def test_wf_apply_launches_multi_phase_plan_as_ordered_stages(self) -> None:
        """Launch phased workflow-plan JSON by preserving stage order in one run."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_path = tmp_path / "multi.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "title": "Too Much",
                        "phases": [
                            {"name": "draft", "jobs": [{"name": "a", "prompt": "A"}]},
                            {"name": "review", "jobs": [{"name": "b", "prompt": "B"}]},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            result = self.run_wf("apply", str(plan_path), "--mock", "--startup-delay", "0", env=env)
            summary = json.loads(result.stdout.split("\ncommand:", 1)[0])
            run = json.loads(Path(summary["path"]).read_text(encoding="utf-8"))
            agents_by_name = {agent["name"]: agent for agent in run["agents"]}

            self.assertEqual(summary["jobs"], 2)
            self.assertEqual(run["status"], "completed")
            self.assertEqual([phase["phase_id"] for phase in run["phases"]], ["phase-draft", "phase-review"])
            self.assertEqual([phase["status"] for phase in run["phases"]], ["completed", "completed"])
            self.assertEqual(agents_by_name["a"]["phase_id"], "phase-draft")
            self.assertEqual(agents_by_name["b"]["phase_id"], "phase-review")
            self.assertEqual(agents_by_name["a"]["stage"], "draft")
            self.assertEqual(agents_by_name["b"]["stage"], "review")
            self.assertEqual(agents_by_name["b"]["depends_on"], "a")

    def test_wf_apply_records_declared_phase_gates_and_plan_decisions(self) -> None:
        """Make workflow-plan phases/gates visible as first-class run state."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_path = tmp_path / "gated.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "title": "Gated Apply",
                        "decisions": [{"title": "Use ccc", "rationale": "Normalize worker artifacts."}],
                        "gates": [{"name": "merge-back", "kind": "integration"}],
                        "phases": [
                            {
                                "name": "draft",
                                "title": "Draft",
                                "goal": "Produce the first draft.",
                                "gates": [{"name": "artifact-quality", "kind": "artifact"}],
                                "checks": [{"name": "draft file exists", "kind": "artifact"}],
                                "jobs": [{"name": "draft", "prompt": "Draft."}],
                            },
                            {
                                "name": "review",
                                "title": "Review",
                                "gates": [{"name": "review-fix", "kind": "review-fix"}],
                                "decisions": [{"title": "Reviewer model", "rationale": "Use codex."}],
                                "jobs": [{"name": "review", "prompt": "Review."}],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            result = self.run_wf("apply", str(plan_path), "--mock", "--startup-delay", "0", env=env)
            summary = json.loads(result.stdout.split("\ncommand:", 1)[0])
            run = json.loads(Path(summary["path"]).read_text(encoding="utf-8"))
            phases_by_id = {phase["phase_id"]: phase for phase in run["phases"]}
            decisions_by_title = {decision["title"]: decision for decision in run["decisions"]}

            self.assertNotIn("phase-cli-workers", phases_by_id)
            self.assertEqual(phases_by_id["phase-draft"]["gates"][0]["name"], "artifact-quality")
            self.assertEqual(phases_by_id["phase-draft"]["planned_checks"][0]["name"], "draft file exists")
            self.assertEqual(phases_by_id["phase-review"]["gates"][0]["kind"], "review-fix")
            self.assertEqual(run["metadata"]["workflow_gates"][0]["name"], "merge-back")
            self.assertEqual(decisions_by_title["Use ccc"]["made_by"], "workflow_apply.py")
            self.assertEqual(decisions_by_title["Reviewer model"]["phase_id"], "phase-review")

    def test_wf_apply_expansion_agents_inherit_declared_phase(self) -> None:
        """Runtime expansion should not depend on the removed default worker phase."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "bin"
            self.install_fake_codex(fake_bin)
            plan_path = tmp_path / "expand.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "title": "Apply Expansion",
                        "phases": [
                            {
                                "name": "draft",
                                "title": "Draft",
                                "jobs": [{"name": "parent", "prompt": "Expand."}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            envelope = json.dumps(
                {
                    "kind": "workflow-expansion",
                    "schema_version": 1,
                    "jobs": [{"name": "child", "prompt": "Child."}],
                }
            )
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            env["CODEX_FAKE_OUTPUT"] = envelope
            result = self.run_wf(
                "apply",
                str(plan_path),
                "--runner",
                "codex-direct",
                "--startup-delay",
                "0",
                "--max-round",
                "2",
                "--max-job",
                "2",
                env=env,
            )
            summary = json.loads(result.stdout.split("\ncommand:", 1)[0])
            run = json.loads(Path(summary["path"]).read_text(encoding="utf-8"))
            agents_by_name = {agent["name"]: agent for agent in run["agents"]}

            self.assertEqual(run["status"], "completed")
            self.assertEqual(run["phases"][0]["phase_id"], "phase-draft")
            self.assertEqual(run["phases"][0]["status"], "completed")
            self.assertEqual(agents_by_name["parent"]["phase_id"], "phase-draft")
            self.assertEqual(agents_by_name["child"]["phase_id"], "phase-draft")
            self.assertTrue(any(event.get("kind") == "expansion" and event.get("operation") == "added" for event in run["events"]))

    def test_wf_apply_creates_worktree_lane_and_launches_worker_there(self) -> None:
        """Worktree jobs should create a lane, record it, and pass its cwd to the runner."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, text=True, capture_output=True)
            (repo / "note.txt").write_text("base\n", encoding="utf-8")
            self.git(repo, "add", "note.txt")
            self.git(repo, "commit", "-m", "base")
            fake_bin = tmp_path / "bin"
            self.install_fake_codex(fake_bin)
            args_path = tmp_path / "codex-args.json"
            plan_path = tmp_path / "worktree.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "title": "Worktree Apply",
                        "cwd": str(repo),
                        "jobs": [
                            {
                                "name": "impl",
                                "prompt": "Implement in a lane.",
                                "write_scope": ["src"],
                                "worktree": {"branch": "workflow/test-impl", "merge_target": "main"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            env["CODEX_FAKE_ARGS"] = str(args_path)
            result = self.run_wf("apply", str(plan_path), "--runner", "codex-direct", "--startup-delay", "0", env=env)
            summary = json.loads(result.stdout.split("\ncommand:", 1)[0])
            run = json.loads(Path(summary["path"]).read_text(encoding="utf-8"))
            agent = run["agents"][0]
            codex_args = json.loads(args_path.read_text(encoding="utf-8"))
            lane_path = Path(agent["cwd"])

            self.assertEqual(run["status"], "completed")
            self.assertTrue(lane_path.exists())
            self.assertEqual(agent["worktree"]["branch"], "workflow/test-impl")
            self.assertEqual(agent["worktree"]["merge_target"], "main")
            self.assertEqual(agent["write_scope"], ["src"])
            self.assertEqual(codex_args[codex_args.index("--cd") + 1], str(lane_path))
            self.assertTrue(any(event.get("kind") == "worktree" and event.get("operation") == "created" for event in run["events"]))

    def test_worktree_lane_shared_branch_across_phase_does_not_fail(self) -> None:
        """Three jobs in one phase sharing the same worktree branch must not crash."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, text=True, capture_output=True)
            self.write_commit(repo, "base")
            plan_path = tmp_path / "shared.json"
            plan_path.write_text(
                json.dumps({
                    "title": "Shared Branch Phase",
                    "cwd": str(repo),
                    "phases": [{
                        "name": "implement",
                        "jobs": [
                            {"name": "impl", "prompt": "Implement.", "worktree": {"branch": "workflow/shared-feature"}},
                            {"name": "review-1", "prompt": "Review 1.", "worktree": {"branch": "workflow/shared-feature"}},
                            {"name": "review-2", "prompt": "Review 2.", "worktree": {"branch": "workflow/shared-feature"}},
                        ],
                    }],
                }),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            result = self.run_wf("apply", str(plan_path), "--mock", "--startup-delay", "0", "--max-agents", "3", env=env)
            summary = self.parse_apply_summary(result)
            run = json.loads(Path(summary["path"]).read_text(encoding="utf-8"))
            self.assertEqual(run["status"], "completed")
            agents_by_name = {a["name"]: a for a in run["agents"]}
            self.assertEqual(len(agents_by_name), 3)
            for name in ("impl", "review-1", "review-2"):
                self.assertEqual(agents_by_name[name]["worktree"]["branch"], "workflow/shared-feature")
                self.assertEqual(agents_by_name[name]["status"], "completed")
            paths = {agents_by_name[n]["cwd"] for n in ("impl", "review-1", "review-2")}
            # Shared branch means shared worktree path
            self.assertEqual(len(paths), 1, "jobs sharing a branch should share one worktree path")
            for name in ("impl", "review-1", "review-2"):
                self.assertTrue(Path(agents_by_name[name]["cwd"]).exists())

    def test_worktree_lane_dependent_reviewer_branches_from_impl(self) -> None:
        """A reviewer with depends_on branches from the impl worktree's branch."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, text=True, capture_output=True)
            self.write_commit(repo, "base")
            plan_path = tmp_path / "dep.json"
            plan_path.write_text(
                json.dumps({
                    "title": "Dependent Lane",
                    "cwd": str(repo),
                    "phases": [
                        {"name": "implement", "jobs": [{"name": "impl", "prompt": "Implement.", "worktree": {"branch": "workflow/dep-impl"}}]},
                        {"name": "review", "jobs": [{"name": "reviewer", "prompt": "Review.", "depends_on": "impl", "worktree": {"branch": "workflow/dep-review"}}]},
                    ],
                }),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            result = self.run_wf("apply", str(plan_path), "--mock", "--startup-delay", "0", env=env)
            summary = self.parse_apply_summary(result)
            run = json.loads(Path(summary["path"]).read_text(encoding="utf-8"))
            self.assertEqual(run["status"], "completed")
            impl_agent = next(a for a in run["agents"] if a["name"] == "impl")
            review_agent = next(a for a in run["agents"] if a["name"] == "reviewer")
            self.assertEqual(impl_agent["worktree"]["branch"], "workflow/dep-impl")
            self.assertEqual(review_agent["worktree"]["branch"], "workflow/dep-review")
            self.assertTrue(Path(impl_agent["cwd"]).exists())
            self.assertTrue(Path(review_agent["cwd"]).exists())

    def test_worktree_lane_chain_dependencies_each_branches_from_previous(self) -> None:
        """impl -> review-a -> review-b: each worktree branches from the previous."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, text=True, capture_output=True)
            self.write_commit(repo, "base")
            plan_path = tmp_path / "chain.json"
            plan_path.write_text(
                json.dumps({
                    "title": "Chain Deps",
                    "cwd": str(repo),
                    "phases": [
                        {"name": "impl-phase", "jobs": [{"name": "impl", "prompt": "Implement.", "worktree": {"branch": "workflow/chain-impl"}}]},
                        {"name": "review-a-phase", "jobs": [{"name": "review-a", "prompt": "Review A.", "depends_on": "impl", "worktree": {"branch": "workflow/chain-review-a"}}]},
                        {"name": "review-b-phase", "jobs": [{"name": "review-b", "prompt": "Review B.", "depends_on": "review-a", "worktree": {"branch": "workflow/chain-review-b"}}]},
                    ],
                }),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            result = self.run_wf("apply", str(plan_path), "--mock", "--startup-delay", "0", env=env)
            summary = self.parse_apply_summary(result)
            run = json.loads(Path(summary["path"]).read_text(encoding="utf-8"))
            self.assertEqual(run["status"], "completed")
            agents = {a["name"]: a for a in run["agents"]}
            self.assertIn("impl", agents["review-a"]["depends_on"])
            self.assertIn("review-a", agents["review-b"]["depends_on"])
            for name in ("impl", "review-a", "review-b"):
                self.assertTrue(Path(agents[name]["cwd"]).exists(), f"{name} worktree missing")

    def test_worktree_lane_parallel_independent_tasks_run_concurrently(self) -> None:
        """Two jobs with separate branches and no dependency run concurrently."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, text=True, capture_output=True)
            self.write_commit(repo, "base")
            plan_path = tmp_path / "parallel.json"
            plan_path.write_text(
                json.dumps({
                    "title": "Parallel Independent",
                    "cwd": str(repo),
                    "jobs": [
                        {"name": "task-a", "prompt": "Task A.", "worktree": {"branch": "workflow/parallel-a"}},
                        {"name": "task-b", "prompt": "Task B.", "worktree": {"branch": "workflow/parallel-b"}},
                    ],
                }),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            result = self.run_wf("apply", str(plan_path), "--mock", "--startup-delay", "0", "--max-agents", "2", env=env)
            summary = self.parse_apply_summary(result)
            run = json.loads(Path(summary["path"]).read_text(encoding="utf-8"))
            self.assertEqual(run["status"], "completed")
            agents = {a["name"]: a for a in run["agents"]}
            self.assertEqual(agents["task-a"]["status"], "completed")
            self.assertEqual(agents["task-b"]["status"], "completed")
            self.assertEqual(agents["task-a"]["worktree"]["branch"], "workflow/parallel-a")
            self.assertEqual(agents["task-b"]["worktree"]["branch"], "workflow/parallel-b")
            self.assertNotEqual(agents["task-a"]["cwd"], agents["task-b"]["cwd"])

    def test_worktree_lane_existing_branch_recovery_checkout_instead_of_create(self) -> None:
        """A pre-existing branch from a prior run should be checked out, not created."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, text=True, capture_output=True)
            self.write_commit(repo, "base")
            # Pre-create the branch
            self.git(repo, "checkout", "-b", "workflow/recovery-lane")
            (repo / "prior.txt").write_text("prior run\n", encoding="utf-8")
            self.git(repo, "add", "prior.txt")
            self.git(repo, "commit", "-m", "prior run change")
            self.git(repo, "checkout", "main")
            plan_path = tmp_path / "recovery.json"
            plan_path.write_text(
                json.dumps({
                    "title": "Recovery Lane",
                    "cwd": str(repo),
                    "jobs": [{"name": "impl", "prompt": "Retry.", "worktree": {"branch": "workflow/recovery-lane"}}],
                }),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            result = self.run_wf("apply", str(plan_path), "--mock", "--startup-delay", "0", env=env)
            summary = self.parse_apply_summary(result)
            run = json.loads(Path(summary["path"]).read_text(encoding="utf-8"))
            self.assertEqual(run["status"], "completed")
            agent = run["agents"][0]
            self.assertEqual(agent["worktree"]["branch"], "workflow/recovery-lane")
            lane_path = Path(agent["cwd"])
            self.assertTrue(lane_path.exists())
            # The prior run's commit should be visible
            prior_file = lane_path / "prior.txt"
            self.assertTrue(prior_file.exists())
            self.assertEqual(prior_file.read_text(encoding="utf-8"), "prior run\n")

    def test_worktree_lane_dry_run_plans_metadata_but_skips_creation(self) -> None:
        """Dry run records worktree metadata but does not create directories."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, text=True, capture_output=True)
            self.write_commit(repo, "base")
            plan_path = tmp_path / "dry.json"
            plan_path.write_text(
                json.dumps({
                    "title": "Dry Run Lanes",
                    "cwd": str(repo),
                    "jobs": [{"name": "impl", "prompt": "Implement.", "worktree": {"branch": "workflow/dry-run-lane"}}],
                }),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            result = self.run_wf("apply", str(plan_path), "--dry-run", "--startup-delay", "0", env=env)
            summary = self.parse_apply_summary(result)
            run = json.loads(Path(summary["path"]).read_text(encoding="utf-8"))
            agent = run["agents"][0]
            self.assertEqual(agent["worktree"]["branch"], "workflow/dry-run-lane")
            self.assertTrue(any(e.get("kind") == "worktree" and e.get("operation") == "planned" for e in run["events"]))
            self.assertFalse(any(e.get("kind") == "worktree" and e.get("operation") == "created" for e in run["events"]))

    def test_merge_lanes_merges_completed_worktree_branch(self) -> None:
        """Merge completed worktree lane branches back into the workflow run cwd."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, text=True, capture_output=True)
            (repo / "note.txt").write_text("base\n", encoding="utf-8")
            self.git(repo, "add", "note.txt")
            self.git(repo, "commit", "-m", "base")
            self.git(repo, "checkout", "-b", "workflow/test-lane")
            (repo / "note.txt").write_text("lane\n", encoding="utf-8")
            self.git(repo, "commit", "-am", "lane change")
            self.git(repo, "checkout", "main")

            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            created = self.run_wf("init", "--title", "Merge Lanes", "--prompt", "merge", "--cwd", str(repo), env=env)
            created_data = json.loads(created.stdout)
            run_id = created_data["run_id"]
            run_path = Path(created_data["path"])
            self.run_wf("add-phase", run_id, "--phase-id", "phase-impl", "--name", "Implementation", "--status", "completed", env=env)
            self.run_wf(
                "add-agent",
                run_id,
                "--phase",
                "phase-impl",
                "--agent-id",
                "agent-lane",
                "--name",
                "lane",
                "--agent-type",
                "codex-exec",
                "--status",
                "completed",
                "--cwd",
                str(repo),
                env=env,
            )
            run = json.loads(run_path.read_text(encoding="utf-8"))
            run["agents"][0]["worktree"] = {"branch": "workflow/test-lane", "path": str(tmp_path / "lane"), "merge_target": "main"}
            run_path.write_text(json.dumps(run, indent=2), encoding="utf-8")

            result = self.run_wf("merge-lanes", run_id, env=env)
            merged = json.loads(result.stdout)
            run_after = json.loads(run_path.read_text(encoding="utf-8"))

            self.assertEqual(merged["merged"], ["agent-lane"])
            self.assertEqual((repo / "note.txt").read_text(encoding="utf-8"), "lane\n")
            self.assertEqual(self.git(repo, "branch", "--show-current").stdout.strip(), "main")
            self.assertTrue(run_after["agents"][0]["worktree"]["merged_at"])
            self.assertTrue(run_after["agents"][0]["worktree"]["merge_commit"])
            self.assertTrue(any(check.get("kind") == "merge" and check.get("status") == "passed" for check in run_after["checks"]))
            self.assertTrue(any(event.get("kind") == "worktree" and event.get("operation") == "merge-succeeded" for event in run_after["events"]))

    def test_merge_lanes_records_conflict_and_aborts_by_default(self) -> None:
        """Conflicted lane merges should leave a failed check and clean target checkout."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, text=True, capture_output=True)
            (repo / "note.txt").write_text("base\n", encoding="utf-8")
            self.git(repo, "add", "note.txt")
            self.git(repo, "commit", "-m", "base")
            self.git(repo, "checkout", "-b", "workflow/conflict-lane")
            (repo / "note.txt").write_text("lane\n", encoding="utf-8")
            self.git(repo, "commit", "-am", "lane change")
            self.git(repo, "checkout", "main")
            (repo / "note.txt").write_text("main\n", encoding="utf-8")
            self.git(repo, "commit", "-am", "main change")

            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            created = self.run_wf("init", "--title", "Conflict Lanes", "--prompt", "merge", "--cwd", str(repo), env=env)
            created_data = json.loads(created.stdout)
            run_id = created_data["run_id"]
            run_path = Path(created_data["path"])
            self.run_wf("add-phase", run_id, "--phase-id", "phase-impl", "--name", "Implementation", "--status", "completed", env=env)
            self.run_wf(
                "add-agent",
                run_id,
                "--phase",
                "phase-impl",
                "--agent-id",
                "agent-conflict",
                "--name",
                "lane",
                "--agent-type",
                "codex-exec",
                "--status",
                "completed",
                "--cwd",
                str(repo),
                env=env,
            )
            run = json.loads(run_path.read_text(encoding="utf-8"))
            run["agents"][0]["worktree"] = {"branch": "workflow/conflict-lane", "path": str(tmp_path / "lane"), "merge_target": "main"}
            run_path.write_text(json.dumps(run, indent=2), encoding="utf-8")

            result = self.run_wf("merge-lanes", run_id, env=env, check=False)
            run_after = json.loads(run_path.read_text(encoding="utf-8"))

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.git(repo, "status", "--porcelain").stdout.strip(), "")
            self.assertEqual((repo / "note.txt").read_text(encoding="utf-8"), "main\n")
            self.assertTrue(any(check.get("kind") == "merge" and check.get("status") == "failed" for check in run_after["checks"]))
            self.assertTrue(any(event.get("kind") == "worktree" and event.get("operation") == "merge-conflicted" for event in run_after["events"]))

    def test_wf_apply_cli_overrides_plan_cwd_tags_and_caps(self) -> None:
        """Workflow-plan execution metadata should not be silently discarded."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_cwd = tmp_path / "from-plan"
            cli_cwd = tmp_path / "from-cli"
            plan_cwd.mkdir()
            cli_cwd.mkdir()
            plan_path = tmp_path / "plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "title": "Execution Fields",
                        "cwd": "from-plan",
                        "runner": "codex-direct",
                        "tags": ["from-plan"],
                        "max_agents": 4,
                        "startup_delay": 9,
                        "jobs": [{"name": "alpha", "prompt": "Alpha prompt."}],
                    }
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            result = self.run_wf(
                "apply",
                str(plan_path),
                "--cwd",
                str(cli_cwd),
                "--max-agents",
                "1",
                "--mock",
                "--startup-delay",
                "0",
                env=env,
            )
            summary = json.loads(result.stdout.split("\ncommand:", 1)[0])
            run = json.loads(Path(summary["path"]).read_text(encoding="utf-8"))
            decision = next(item for item in run["decisions"] if item["title"] == "Runner selected: codex-direct")

            self.assertEqual(run["cwd"], str(cli_cwd.resolve()))
            self.assertIn("from-plan", run["tags"])
            self.assertIn("max_agents=1", decision["rationale"])

    def test_wf_apply_does_not_launch_dependent_phase_after_failure(self) -> None:
        """A failed phase should leave dependent phase jobs unlaunched."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "bin"
            self.install_fake_codex(fake_bin)
            state_path = tmp_path / "attempts.txt"
            plan_path = tmp_path / "multi.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "title": "Failing Phases",
                        "phases": [
                            {"name": "draft", "jobs": [{"name": "a", "prompt": "A"}]},
                            {"name": "review", "jobs": [{"name": "b", "prompt": "B"}]},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            env["CODEX_FAKE_STATE"] = str(state_path)
            env["CODEX_FAKE_EXIT_CODE"] = "1"
            result = self.run_wf("apply", str(plan_path), "--startup-delay", "0", env=env, check=False)
            summary = json.loads(result.stdout.split("\ncommand:", 1)[0])
            run = json.loads(Path(summary["path"]).read_text(encoding="utf-8"))
            agents_by_name = {agent["name"]: agent for agent in run["agents"]}

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(state_path.read_text(encoding="utf-8"), "1")
            self.assertEqual(agents_by_name["a"]["status"], "failed")
            self.assertEqual(agents_by_name["b"]["status"], "failed")
            self.assertIn("dependency never satisfied", agents_by_name["b"]["summary"])

    def test_workflow_plan_normalizes_scalar_ccc_control_to_list(self) -> None:
        """Plan ccc_control should not become character-by-character argv."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_plan  # pylint: disable=import-outside-toplevel

        plan = workflow_plan.normalize_workflow(
            {
                "title": "Ccc Control",
                "ccc_control": "@reviewer",
                "jobs": [{"name": "a", "prompt": "A"}],
            },
            fallback_title="fallback",
        )

        self.assertEqual(plan["ccc_control"], ["@reviewer"])

    def test_workflow_plan_normalizes_phase_and_job_ccc_control_to_list(self) -> None:
        """Phase/job ccc_control should not become character-by-character argv."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_plan  # pylint: disable=import-outside-toplevel

        plan = workflow_plan.normalize_workflow(
            {
                "title": "Ccc Control",
                "phases": [
                    {
                        "name": "review",
                        "ccc_control": "@phase-reviewer",
                        "jobs": [
                            {
                                "name": "a",
                                "prompt": "A",
                                "ccc_control": "@job-reviewer",
                            }
                        ],
                    }
                ],
            },
            fallback_title="fallback",
        )

        phase = plan["phases"][0]
        self.assertEqual(phase["ccc_control"], ["@phase-reviewer"])
        self.assertEqual(phase["jobs"][0]["ccc_control"], ["@job-reviewer"])

    def test_wf_apply_honors_job_runner_overrides(self) -> None:
        """Per-job execution fields must override phase/root defaults and be visible in agent state."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "bin"
            self.install_fake_codex(fake_bin)
            self.install_timed_fake_ccc(fake_bin)
            plan_path = tmp_path / "mixed.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "title": "Mixed Runners",
                        "runner": "codex-direct",
                        "model": "root-model",
                        "sandbox": "read-only",
                        "phases": [
                            {
                                "name": "research",
                                "runner": "ccc",
                                "ccc_runner": "@mm",
                                "model": "phase-model",
                                "jobs": [
                                    {"name": "deep-research", "prompt": "Research deeply.", "model": "job-model"},
                                    {"name": "quick-scan", "prompt": "Quick scan."},
                                ],
                            },
                            {
                                "name": "implement",
                                "jobs": [
                                    {"name": "impl-a", "prompt": "Implement A.", "runner": "ccc", "ccc_runner": "@kimi"},
                                    {"name": "impl-b", "prompt": "Implement B."},
                                ],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            env["CCC_FAKE_EVENTS"] = str(tmp_path / "ccc-events.jsonl")
            env["CCC_FAKE_ACTIVE"] = str(tmp_path / "ccc-active.txt")
            env["CCC_FAKE_MAX"] = str(tmp_path / "ccc-max.txt")
            env["CCC_FAKE_LOCK"] = str(tmp_path / "ccc.lock")
            env["CCC_FAKE_RUN_ROOT"] = str(tmp_path / "ccc-runs")
            result = self.run_wf(
                "apply",
                str(plan_path),
                "--startup-delay",
                "0",
                env=env,
            )
            summary = json.loads(result.stdout.split("\ncommand:", 1)[0])
            run = json.loads(Path(summary["path"]).read_text(encoding="utf-8"))
            agents_by_name = {agent["name"]: agent for agent in run["agents"]}

            self.assertEqual(run["status"], "completed")

            # deep-research: job overrides model, phase overrides runner
            deep = agents_by_name["deep-research"]
            self.assertEqual(deep["model"], "job-model")
            self.assertIn("ccc", deep["agent_type"])
            self.assertIn("mm", deep["agent_type"])

            # quick-scan: inherits phase runner/model
            quick = agents_by_name["quick-scan"]
            self.assertEqual(quick["model"], "phase-model")
            self.assertIn("ccc", quick["agent_type"])
            self.assertIn("mm", quick["agent_type"])

            # impl-a: job overrides runner within a root-runner phase
            impl_a = agents_by_name["impl-a"]
            self.assertIn("ccc", impl_a["agent_type"])
            self.assertIn("kimi", impl_a["agent_type"])

            # impl-b: inherits root runner (codex-direct)
            impl_b = agents_by_name["impl-b"]
            self.assertEqual(impl_b["agent_type"], "codex-exec")

            # Verify the normalized plan artifact preserves execution fields
            plan_artifact = next(a for a in run["artifacts"] if a["kind"] == "workflow-plan")
            saved_plan = json.loads(Path(plan_artifact["path"]).read_text(encoding="utf-8"))
            research_phase = saved_plan["phases"][0]
            self.assertEqual(research_phase["runner"], "ccc")
            self.assertEqual(research_phase["ccc_runner"], "@mm")
            self.assertEqual(research_phase["model"], "phase-model")
            deep_job = research_phase["jobs"][0]
            self.assertEqual(deep_job["model"], "job-model")
            impl_a_job = saved_plan["phases"][1]["jobs"][0]
            self.assertEqual(impl_a_job["runner"], "ccc")
            self.assertEqual(impl_a_job["ccc_runner"], "@kimi")

    def test_wf_apply_honors_plan_execution_fields_with_cli_overrides(self) -> None:
        """CLI flags must still override plan-provided per-job execution fields."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "bin"
            self.install_fake_codex(fake_bin)
            self.install_timed_fake_ccc(fake_bin)
            plan_path = tmp_path / "cli-override.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "title": "CLI Override",
                        "phases": [
                            {
                                "name": "work",
                                "runner": "ccc",
                                "ccc_runner": "@mm",
                                "model": "phase-model",
                                "jobs": [
                                    {"name": "job-a", "prompt": "Job A.", "runner": "codex-direct", "model": "job-model"},
                                ],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            env["CCC_FAKE_EVENTS"] = str(tmp_path / "ccc-events.jsonl")
            env["CCC_FAKE_ACTIVE"] = str(tmp_path / "ccc-active.txt")
            env["CCC_FAKE_MAX"] = str(tmp_path / "ccc-max.txt")
            env["CCC_FAKE_LOCK"] = str(tmp_path / "ccc.lock")
            env["CCC_FAKE_RUN_ROOT"] = str(tmp_path / "ccc-runs")
            # CLI --model overrides the job's model; --runner overrides the job's runner
            result = self.run_wf(
                "apply",
                str(plan_path),
                "--runner",
                "ccc-opencode",
                "--model",
                "cli-model",
                "--startup-delay",
                "0",
                env=env,
            )
            summary = json.loads(result.stdout.split("\ncommand:", 1)[0])
            run = json.loads(Path(summary["path"]).read_text(encoding="utf-8"))
            agent = run["agents"][0]

            self.assertEqual(run["status"], "completed")
            # CLI model overrides job model
            self.assertEqual(agent["model"], "cli-model")
            # CLI runner overrides job runner
            self.assertEqual(agent["agent_type"], "ccc-opencode")

    def test_wf_apply_cli_overrides_plan_ccc_control_and_output_mode(self) -> None:
        """Explicit CLI ccc control/output flags should override phase and job values."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "bin"
            self.install_timed_fake_ccc(fake_bin)
            plan_path = tmp_path / "ccc-cli-override.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "title": "CCC CLI Override",
                        "runner": "ccc",
                        "ccc_runner": "@mm",
                        "ccc_output_mode": "text",
                        "ccc_control": ["@root-control"],
                        "phases": [
                            {
                                "name": "review",
                                "ccc_output_mode": "json",
                                "ccc_control": ["@phase-control"],
                                "jobs": [
                                    {
                                        "name": "check",
                                        "prompt": "Check.",
                                        "ccc_control": ["@job-control"],
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            env["CCC_FAKE_EVENTS"] = str(tmp_path / "ccc-events.jsonl")
            env["CCC_FAKE_ACTIVE"] = str(tmp_path / "ccc-active.txt")
            env["CCC_FAKE_MAX"] = str(tmp_path / "ccc-max.txt")
            env["CCC_FAKE_LOCK"] = str(tmp_path / "ccc.lock")
            env["CCC_FAKE_RUN_ROOT"] = str(tmp_path / "ccc-runs")
            result = self.run_wf(
                "apply",
                str(plan_path),
                "--ccc-control",
                "@cli-control",
                "--ccc-output-mode",
                "stream-json",
                "--startup-delay",
                "0",
                env=env,
                check=False,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)

            ccc_starts = [
                ev["args"]
                for ev in self.read_ccc_events(tmp_path)
                if ev["event"] == "start"
            ]
            self.assertEqual(len(ccc_starts), 1, msg=ccc_starts)
            args = ccc_starts[0]
            self.assertEqual(args[args.index("--output-mode") + 1], "stream-json")
            self.assertIn("@cli-control", args)
            self.assertNotIn("@root-control", args)
            self.assertNotIn("@phase-control", args)
            self.assertNotIn("@job-control", args)

    def test_wf_apply_honors_phase_result_schema_override(self) -> None:
        """Phase result_schema should validate output for jobs in that phase."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            schema_path = tmp_path / "schema.json"
            schema_path.write_text(
                json.dumps({"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}),
                encoding="utf-8",
            )
            plan_path = tmp_path / "phase-schema.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "title": "Phase Schema",
                        "runner": "codex-direct",
                        "phases": [
                            {
                                "name": "validated",
                                "result_schema": str(schema_path),
                                "jobs": [{"name": "ask", "prompt": "Return JSON."}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            env = self.codex_fake_env(tmp_path)
            env["CODEX_FAKE_OUTPUT"] = json.dumps({"answer": "yes"})
            result = self.run_wf("apply", str(plan_path), "--startup-delay", "0", env=env, check=False)
            self.assertEqual(result.returncode, 0, msg=result.stderr)

            summary = json.loads(result.stdout.split("\ncommand:", 1)[0])
            run = json.loads(Path(summary["path"]).read_text(encoding="utf-8"))
            self.assertEqual(run["status"], "completed")
            agent = run["agents"][0]
            self.assertEqual(agent["status"], "completed")
            self.assertEqual(agent["result_json"], {"answer": "yes"})

    def test_wf_apply_phase_cwd_reaches_actual_worker_command(self) -> None:
        """Phase cwd should affect the real launched worker command."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root_cwd = tmp_path / "root-cwd"
            phase_cwd = tmp_path / "phase-cwd"
            root_cwd.mkdir()
            phase_cwd.mkdir()
            fake_bin = tmp_path / "bin"
            self.install_fake_codex(fake_bin)
            args_log = tmp_path / "codex-args.json"
            plan_path = tmp_path / "phase-cwd.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "title": "Phase Cwd",
                        "cwd": str(root_cwd),
                        "runner": "codex-direct",
                        "phases": [
                            {
                                "name": "phase",
                                "cwd": str(phase_cwd),
                                "jobs": [{"name": "ask", "prompt": "Return text."}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            env["CODEX_FAKE_ARGS"] = str(args_log)
            result = self.run_wf("apply", str(plan_path), "--startup-delay", "0", env=env, check=False)
            self.assertEqual(result.returncode, 0, msg=result.stderr)

            codex_args = json.loads(args_log.read_text(encoding="utf-8"))
            self.assertEqual(codex_args[codex_args.index("--cd") + 1], str(phase_cwd.resolve()))

    def test_wf_apply_relative_cli_cwd_overrides_phase_cwd_at_launch(self) -> None:
        """Relative CLI cwd should stay resolved when applied as a per-job override."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "plans"
            plan_dir.mkdir()
            cli_cwd = plan_dir / "cli-rel"
            phase_cwd = plan_dir / "phase-rel"
            cli_cwd.mkdir()
            phase_cwd.mkdir()
            fake_bin = tmp_path / "bin"
            self.install_fake_codex(fake_bin)
            args_log = tmp_path / "codex-args.json"
            plan_path = plan_dir / "relative-cli-cwd.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "title": "Relative CLI Cwd",
                        "runner": "codex-direct",
                        "phases": [
                            {
                                "name": "phase",
                                "cwd": "phase-rel",
                                "jobs": [{"name": "ask", "prompt": "Return text."}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            env["CODEX_FAKE_ARGS"] = str(args_log)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "workflow_apply.py"),
                    str(plan_path),
                    "--cwd",
                    "cli-rel",
                    "--startup-delay",
                    "0",
                ],
                check=False,
                text=True,
                capture_output=True,
                env=env,
                cwd=plan_dir,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)

            codex_args = json.loads(args_log.read_text(encoding="utf-8"))
            self.assertEqual(codex_args[codex_args.index("--cd") + 1], str(cli_cwd.resolve()))

    def test_wf_apply_phase_mock_and_dry_run_do_not_launch_workers(self) -> None:
        """Phase mock/dry_run should avoid real worker process launches."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "bin"
            self.install_fake_codex(fake_bin)
            args_log = tmp_path / "codex-args.json"
            plan_path = tmp_path / "virtual-workers.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "title": "Virtual Workers",
                        "runner": "codex-direct",
                        "phases": [
                            {
                                "name": "mocked",
                                "mock": True,
                                "jobs": [{"name": "mock-job", "prompt": "Should not launch."}],
                            },
                            {
                                "name": "dry",
                                "dry_run": True,
                                "jobs": [{"name": "dry-job", "prompt": "Should not launch."}],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            env["CODEX_FAKE_ARGS"] = str(args_log)
            result = self.run_wf("apply", str(plan_path), "--startup-delay", "0", env=env, check=False)
            self.assertEqual(result.returncode, 0, msg=result.stderr)

            summary = json.loads(result.stdout.split("\ncommand:", 1)[0])
            run = json.loads(Path(summary["path"]).read_text(encoding="utf-8"))
            agents_by_name = {agent["name"]: agent for agent in run["agents"]}
            self.assertFalse(args_log.exists(), msg="fake codex should not have launched")
            self.assertEqual(agents_by_name["mock-job"]["status"], "completed")
            self.assertEqual(agents_by_name["mock-job"]["summary"], "mock worker completed")
            self.assertEqual(agents_by_name["dry-job"]["status"], "completed")
            self.assertEqual(agents_by_name["dry-job"]["summary"], "dry run; worker not launched")
            self.assertIn("Dry run only", agents_by_name["dry-job"]["result"])

    def test_wf_apply_runs_per_job_with_plan_runner(self) -> None:
        """Per-job providers must actually launch the configured runner, not just stamp agent_type."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "bin"
            self.install_fake_codex(fake_bin)
            self.install_timed_fake_ccc(fake_bin)
            plan_path = tmp_path / "mixed.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "title": "Mixed Runners",
                        "runner": "codex-direct",
                        "model": "root-model",
                        "sandbox": "read-only",
                        "phases": [
                            {
                                "name": "research",
                                "runner": "ccc",
                                "ccc_runner": "@mm",
                                "model": "phase-model",
                                "jobs": [
                                    {"name": "deep-research", "prompt": "Research deeply.", "model": "job-model"},
                                    {"name": "quick-scan", "prompt": "Quick scan."},
                                ],
                            },
                            {
                                "name": "implement",
                                "jobs": [
                                    {"name": "impl-a", "prompt": "Implement A.", "runner": "ccc", "ccc_runner": "@kimi"},
                                    {"name": "impl-b", "prompt": "Implement B."},
                                ],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            env["CCC_FAKE_EVENTS"] = str(tmp_path / "ccc-events.jsonl")
            env["CCC_FAKE_ACTIVE"] = str(tmp_path / "ccc-active.txt")
            env["CCC_FAKE_MAX"] = str(tmp_path / "ccc-max.txt")
            env["CCC_FAKE_LOCK"] = str(tmp_path / "ccc.lock")
            env["CCC_FAKE_RUN_ROOT"] = str(tmp_path / "ccc-runs")
            result = self.run_wf(
                "apply",
                str(plan_path),
                "--startup-delay",
                "0",
                env=env,
                check=False,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)

            summary = json.loads(result.stdout.split("\ncommand:", 1)[0])
            run = json.loads(Path(summary["path"]).read_text(encoding="utf-8"))
            self.assertEqual(run["status"], "completed")

            ccc_starts = [
                " ".join(ev["args"])
                for ev in self.read_ccc_events(tmp_path)
                if ev["event"] == "start"
            ]
            self.assertEqual(len(ccc_starts), 3, msg=f"expected 3 ccc starts, got {ccc_starts}")
            self.assertTrue(
                any("@mm" in args and "Research deeply." in args for args in ccc_starts),
                msg=f"deep-research must run via ccc @mm: {ccc_starts}",
            )
            self.assertTrue(
                any("@mm" in args and "Quick scan." in args for args in ccc_starts),
                msg=f"quick-scan must run via ccc @mm: {ccc_starts}",
            )
            self.assertTrue(
                any("@kimi" in args and "Implement A." in args for args in ccc_starts),
                msg=f"impl-a must run via ccc @kimi: {ccc_starts}",
            )

            for agent in run["agents"]:
                if agent["name"] == "impl-b":
                    self.assertEqual(agent["agent_type"], "codex-exec")
                    # impl-b inherits every field from the run defaults, so no
                    # per-agent execution_args override is recorded.
                    self.assertNotIn("execution_args", agent, msg=agent)
                else:
                    self.assertIn("ccc", agent["agent_type"])
                    self.assertIn("execution_args", agent, msg=agent)

    def test_wf_apply_same_runner_different_ccc_runner_per_phase(self) -> None:
        """A phase that keeps the same runner but overrides ccc_runner must still apply ccc_runner."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "bin"
            self.install_fake_codex(fake_bin)
            self.install_timed_fake_ccc(fake_bin)
            plan_path = tmp_path / "same-runner.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "title": "Same Runner, Different Ccc",
                        "runner": "ccc",
                        "ccc_runner": "@mm",
                        "phases": [
                            {
                                "name": "default",
                                "jobs": [{"name": "job-mm", "prompt": "MM phase job."}],
                            },
                            {
                                "name": "kimi",
                                "ccc_runner": "@kimi",
                                "jobs": [{"name": "job-kimi", "prompt": "Kimi phase job."}],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            env["CCC_FAKE_EVENTS"] = str(tmp_path / "ccc-events.jsonl")
            env["CCC_FAKE_ACTIVE"] = str(tmp_path / "ccc-active.txt")
            env["CCC_FAKE_MAX"] = str(tmp_path / "ccc-max.txt")
            env["CCC_FAKE_LOCK"] = str(tmp_path / "ccc.lock")
            env["CCC_FAKE_RUN_ROOT"] = str(tmp_path / "ccc-runs")
            result = self.run_wf("apply", str(plan_path), "--startup-delay", "0", env=env, check=False)
            self.assertEqual(result.returncode, 0, msg=result.stderr)

            summary = json.loads(result.stdout.split("\ncommand:", 1)[0])
            run = json.loads(Path(summary["path"]).read_text(encoding="utf-8"))
            self.assertEqual(run["status"], "completed")

            ccc_starts = [
                " ".join(ev["args"])
                for ev in self.read_ccc_events(tmp_path)
                if ev["event"] == "start"
            ]
            self.assertEqual(len(ccc_starts), 2, msg=ccc_starts)
            self.assertTrue(
                any("@mm" in args and "MM phase job" in args for args in ccc_starts),
                msg=f"default phase must use @mm: {ccc_starts}",
            )
            self.assertTrue(
                any("@kimi" in args and "Kimi phase job" in args for args in ccc_starts),
                msg=f"kimi phase must use @kimi: {ccc_starts}",
            )

    def test_wf_apply_same_runner_different_model_per_phase(self) -> None:
        """A phase that keeps the same runner but overrides model/sandbox must be honored at launch."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "bin"
            self.install_fake_codex(fake_bin)
            self.install_timed_fake_ccc(fake_bin)
            plan_path = tmp_path / "same-runner-model.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "title": "Same Runner, Different Model",
                        "runner": "codex-direct",
                        "model": "root-model",
                        "sandbox": "read-only",
                        "phases": [
                            {
                                "name": "default",
                                "jobs": [{"name": "job-root", "prompt": "Root phase job."}],
                            },
                            {
                                "name": "phase-two",
                                "model": "phase-model",
                                "sandbox": "workspace-write",
                                "jobs": [{"name": "job-phase2", "prompt": "Phase two job."}],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            codex_args_log = tmp_path / "codex-args.json"
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            env["CCC_FAKE_EVENTS"] = str(tmp_path / "ccc-events.jsonl")
            env["CCC_FAKE_ACTIVE"] = str(tmp_path / "ccc-active.txt")
            env["CCC_FAKE_MAX"] = str(tmp_path / "ccc-max.txt")
            env["CCC_FAKE_LOCK"] = str(tmp_path / "ccc.lock")
            env["CCC_FAKE_RUN_ROOT"] = str(tmp_path / "ccc-runs")
            # The default fake codex overwrites CODEX_FAKE_ARGS on every call; install a
            # custom binary that appends so we can assert on every per-job invocation.
            fake_codex = fake_bin / "codex"
            fake_codex.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import os
                    import sys
                    from pathlib import Path
                    args_log = Path(os.environ.get("CODEX_FAKE_ARGS", "/tmp/_codex_args.json"))
                    log = json.loads(args_log.read_text(encoding="utf-8")) if args_log.exists() else []
                    log.append(sys.argv[1:])
                    args_log.write_text(json.dumps(log), encoding="utf-8")
                    event = {"type": "item.completed", "item": {"type": "agent_message", "text": "fake codex result"}}
                    sys.stdout.write(json.dumps(event) + "\\n")
                    sys.exit(0)
                    """
                ),
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            env["CODEX_FAKE_ARGS"] = str(codex_args_log)
            result = self.run_wf("apply", str(plan_path), "--startup-delay", "0", env=env, check=False)
            self.assertEqual(result.returncode, 0, msg=result.stderr)

            summary = json.loads(result.stdout.split("\ncommand:", 1)[0])
            run = json.loads(Path(summary["path"]).read_text(encoding="utf-8"))
            self.assertEqual(run["status"], "completed")

            calls = json.loads(codex_args_log.read_text())
            self.assertEqual(len(calls), 2, msg=calls)
            root_call = next(
                (c for c in calls if any("Root phase job" in str(x) for x in c)),
                None,
            )
            phase2_call = next(
                (c for c in calls if any("Phase two job" in str(x) for x in c)),
                None,
            )
            self.assertIsNotNone(root_call, msg=calls)
            self.assertIsNotNone(phase2_call, msg=calls)
            self.assertIn("root-model", root_call, msg=root_call)
            self.assertIn("read-only", root_call, msg=root_call)
            self.assertIn("phase-model", phase2_call, msg=phase2_call)
            self.assertIn("workspace-write", phase2_call, msg=phase2_call)
            self.assertNotIn("phase-model", root_call, msg=root_call)
            self.assertNotIn("root-model", phase2_call, msg=phase2_call)


    @slow_test
    def test_wf_runner_matrix_dispatches_to_script(self) -> None:
        """Expose the reusable runner matrix through the installed shell entrypoint."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            result = self.run_wf(
                "runner-matrix",
                "--output-dir",
                str(tmp_path / "out"),
                "--target",
                "codex-direct",
                "--mock",
                "--startup-delay",
                "0",
                env=env,
            )
            summary = json.loads(result.stdout)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["targets"][0]["runner"], "codex-direct")

    def test_merge_conflicts_prepares_context_after_aborted_conflict(self) -> None:
        """merge-conflicts should produce prompt/context artifacts after a conflict with aborted merge."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, text=True, capture_output=True)
            (repo / "note.txt").write_text("base\n", encoding="utf-8")
            self.git(repo, "add", "note.txt")
            self.git(repo, "commit", "-m", "base")
            self.git(repo, "checkout", "-b", "workflow/conflict-lane")
            (repo / "note.txt").write_text("lane\n", encoding="utf-8")
            self.git(repo, "commit", "-am", "lane change")
            self.git(repo, "checkout", "main")
            (repo / "note.txt").write_text("main\n", encoding="utf-8")
            self.git(repo, "commit", "-am", "main change")

            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            created = self.run_wf("init", "--title", "Conflict Assist", "--prompt", "merge", "--cwd", str(repo), env=env)
            created_data = json.loads(created.stdout)
            run_id = created_data["run_id"]
            run_path = Path(created_data["path"])
            self.run_wf("add-phase", run_id, "--phase-id", "phase-impl", "--name", "Implementation", "--status", "completed", env=env)
            self.run_wf(
                "add-agent",
                run_id,
                "--phase",
                "phase-impl",
                "--agent-id",
                "agent-conflict",
                "--name",
                "lane",
                "--agent-type",
                "codex-exec",
                "--status",
                "completed",
                "--cwd",
                str(repo),
                "--prompt",
                "implement feature X in note.txt",
                env=env,
            )
            run = json.loads(run_path.read_text(encoding="utf-8"))
            run["agents"][0]["worktree"] = {"branch": "workflow/conflict-lane", "path": str(tmp_path / "lane"), "merge_target": "main"}
            run_path.write_text(json.dumps(run, indent=2), encoding="utf-8")

            self.run_wf("merge-lanes", run_id, env=env, check=False)

            result = self.run_wf("merge-conflicts", run_id, env=env)
            output = json.loads(result.stdout)
            run_after = json.loads(run_path.read_text(encoding="utf-8"))

            self.assertEqual(output["run_id"], run_id)
            self.assertEqual(output["agent_id"], "agent-conflict")
            self.assertEqual(output["branch"], "workflow/conflict-lane")
            self.assertFalse(output["cwd_has_conflict_markers"])
            self.assertFalse(output["merge_in_progress"])
            self.assertIn("hint", output)
            self.assertTrue(Path(output["prompt_path"]).is_file())
            self.assertTrue(Path(output["context_path"]).is_file())

            prompt_text = Path(output["prompt_path"]).read_text(encoding="utf-8")
            self.assertIn("Merge Conflict Resolution", prompt_text)
            self.assertIn("implement feature X in note.txt", prompt_text)
            self.assertIn("agent-conflict", prompt_text)

            context_data = json.loads(Path(output["context_path"]).read_text(encoding="utf-8"))
            self.assertEqual(context_data["agent_id"], "agent-conflict")
            self.assertFalse(context_data["cwd_has_conflict_markers"])
            self.assertFalse(context_data["merge_in_progress"])

            merger_artifacts = [a for a in run_after["artifacts"] if a["kind"] in {"merger-prompt", "merger-context"}]
            self.assertEqual(len(merger_artifacts), 2)
            self.assertTrue(any(
                e.get("operation") == "merge-conflict-assist" for e in run_after["events"]
            ))
            # merge-conflicts must not create a pending check: there is no clean
            # way to complete it (verify appends rather than upserts), and the
            # documented --check-id reuse would duplicate the id.
            assist_checks = [c for c in run_after["checks"] if c.get("kind") == "merge-conflict-assist"]
            self.assertEqual(assist_checks, [])

    def test_merge_conflicts_detects_conflicted_files_when_left_in_place(self) -> None:
        """merge-conflicts should list conflicted files when merge-lanes used --leave-conflicts."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, text=True, capture_output=True)
            (repo / "note.txt").write_text("base\n", encoding="utf-8")
            self.git(repo, "add", "note.txt")
            self.git(repo, "commit", "-m", "base")
            self.git(repo, "checkout", "-b", "workflow/conflict-lane")
            (repo / "note.txt").write_text("lane\n", encoding="utf-8")
            self.git(repo, "commit", "-am", "lane change")
            self.git(repo, "checkout", "main")
            (repo / "note.txt").write_text("main\n", encoding="utf-8")
            self.git(repo, "commit", "-am", "main change")

            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            created = self.run_wf("init", "--title", "Leave Conflicts", "--prompt", "merge", "--cwd", str(repo), env=env)
            created_data = json.loads(created.stdout)
            run_id = created_data["run_id"]
            run_path = Path(created_data["path"])
            self.run_wf("add-phase", run_id, "--phase-id", "phase-impl", "--name", "Implementation", "--status", "completed", env=env)
            self.run_wf(
                "add-agent",
                run_id,
                "--phase",
                "phase-impl",
                "--agent-id",
                "agent-conflict",
                "--name",
                "lane",
                "--agent-type",
                "codex-exec",
                "--status",
                "completed",
                "--cwd",
                str(repo),
                env=env,
            )
            run = json.loads(run_path.read_text(encoding="utf-8"))
            run["agents"][0]["worktree"] = {"branch": "workflow/conflict-lane", "path": str(tmp_path / "lane"), "merge_target": "main"}
            run_path.write_text(json.dumps(run, indent=2), encoding="utf-8")

            self.run_wf("merge-lanes", run_id, "--leave-conflicts", env=env, check=False)

            dirty = self.git(repo, "status", "--porcelain")
            self.assertIn("UU", dirty.stdout)

            result = self.run_wf("merge-conflicts", run_id, env=env)
            output = json.loads(result.stdout)

            self.assertTrue(output["cwd_has_conflict_markers"])
            self.assertTrue(output["merge_in_progress"])
            self.assertFalse(output["cwd_has_unrelated_changes"])
            self.assertIn("note.txt", output["conflict_files"])
            self.assertNotIn("hint", output)

    def test_merge_conflicts_refuses_without_conflict(self) -> None:
        """merge-conflicts should refuse when no conflicted merge exists in the run."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, text=True, capture_output=True)
            (repo / "note.txt").write_text("base\n", encoding="utf-8")
            self.git(repo, "add", "note.txt")
            self.git(repo, "commit", "-m", "base")

            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            created = self.run_wf("init", "--title", "No Conflict", "--prompt", "no conflict here", "--cwd", str(repo), env=env)
            created_data = json.loads(created.stdout)
            run_id = created_data["run_id"]

            result = self.run_wf("merge-conflicts", run_id, env=env, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no conflicted merge found", result.stderr)

    def test_merge_conflicts_refuses_when_conflicted_agent_missing_check_id(self) -> None:
        """merge-conflicts should refuse if the conflicted agent has no merge_check_id.

        Otherwise the prompt would silently borrow another agent's check as evidence,
        which is unsafe and misleading.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, text=True, capture_output=True)
            (repo / "note.txt").write_text("base\n", encoding="utf-8")
            self.git(repo, "add", "note.txt")
            self.git(repo, "commit", "-m", "base")

            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            created = self.run_wf("init", "--title", "Corrupt Conflict", "--prompt", "merge", "--cwd", str(repo), env=env)
            created_data = json.loads(created.stdout)
            run_id = created_data["run_id"]
            run_path = Path(created_data["path"])
            self.run_wf("add-phase", run_id, "--phase-id", "phase-impl", "--name", "Implementation", "--status", "completed", env=env)
            self.run_wf(
                "add-agent",
                run_id,
                "--phase",
                "phase-impl",
                "--agent-id",
                "agent-broken",
                "--name",
                "broken",
                "--agent-type",
                "codex-exec",
                "--status",
                "completed",
                "--cwd",
                str(repo),
                env=env,
            )
            self.run_wf(
                "add-agent",
                run_id,
                "--phase",
                "phase-impl",
                "--agent-id",
                "agent-other",
                "--name",
                "other",
                "--agent-type",
                "codex-exec",
                "--status",
                "completed",
                "--cwd",
                str(repo),
                env=env,
            )
            run = json.loads(run_path.read_text(encoding="utf-8"))
            run["agents"][0]["worktree"] = {"branch": "workflow/broken-lane", "path": str(tmp_path / "broken"), "merge_target": "main", "merge_status": "conflicted"}
            run["agents"][1]["worktree"] = {"branch": "workflow/other-lane", "path": str(tmp_path / "other"), "merge_target": "main", "merge_status": "conflicted", "merge_check_id": "chk-other-fake"}
            run.setdefault("checks", []).append({
                "check_id": "chk-other-fake",
                "kind": "merge",
                "status": "failed",
                "summary": "merge from agent-other-lane failed",
                "agent_id": "agent-other",
            })
            run_path.write_text(json.dumps(run, indent=2), encoding="utf-8")

            result = self.run_wf("merge-conflicts", run_id, env=env, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no merge_check_id", result.stderr)
            self.assertIn("agent-broken", result.stderr)

    def test_merge_conflicts_reports_unrelated_dirty_changes_separately(self) -> None:
        """merge-conflicts should distinguish conflict markers from unrelated dirty changes."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, text=True, capture_output=True)
            (repo / "note.txt").write_text("base\n", encoding="utf-8")
            self.git(repo, "add", "note.txt")
            self.git(repo, "commit", "-m", "base")
            self.git(repo, "checkout", "-b", "workflow/conflict-lane")
            (repo / "note.txt").write_text("lane\n", encoding="utf-8")
            self.git(repo, "commit", "-am", "lane change")
            self.git(repo, "checkout", "main")
            (repo / "note.txt").write_text("main\n", encoding="utf-8")
            self.git(repo, "commit", "-am", "main change")

            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            created = self.run_wf("init", "--title", "Unrelated Dirty", "--prompt", "merge", "--cwd", str(repo), env=env)
            created_data = json.loads(created.stdout)
            run_id = created_data["run_id"]
            run_path = Path(created_data["path"])
            self.run_wf("add-phase", run_id, "--phase-id", "phase-impl", "--name", "Implementation", "--status", "completed", env=env)
            self.run_wf(
                "add-agent",
                run_id,
                "--phase",
                "phase-impl",
                "--agent-id",
                "agent-conflict",
                "--name",
                "lane",
                "--agent-type",
                "codex-exec",
                "--status",
                "completed",
                "--cwd",
                str(repo),
                env=env,
            )
            run = json.loads(run_path.read_text(encoding="utf-8"))
            run["agents"][0]["worktree"] = {"branch": "workflow/conflict-lane", "path": str(tmp_path / "lane"), "merge_target": "main"}
            run_path.write_text(json.dumps(run, indent=2), encoding="utf-8")

            self.run_wf("merge-lanes", run_id, env=env, check=False)
            self.assertEqual(self.git(repo, "status", "--porcelain").stdout.strip(), "")

            (repo / "unrelated.txt").write_text("user's own work\n", encoding="utf-8")
            dirty = self.git(repo, "status", "--porcelain").stdout
            self.assertIn("?? unrelated.txt", dirty)
            self.assertNotIn("UU", dirty)

            result = self.run_wf("merge-conflicts", run_id, env=env)
            output = json.loads(result.stdout)

            self.assertFalse(output["cwd_has_conflict_markers"])
            self.assertTrue(output["cwd_has_unrelated_changes"])
            self.assertFalse(output["merge_in_progress"])
            self.assertIn("hint", output)
            self.assertIn("uncommitted", output["hint"].lower())
            self.assertEqual(output["conflict_files"], [])

    def test_merge_conflicts_target_specific_agent_with_flag(self) -> None:
        """merge-conflicts --agent should target the named conflicted agent."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, text=True, capture_output=True)
            (repo / "note.txt").write_text("base\n", encoding="utf-8")
            self.git(repo, "add", "note.txt")
            self.git(repo, "commit", "-m", "base")
            self.git(repo, "checkout", "-b", "workflow/first-lane")
            (repo / "note.txt").write_text("first\n", encoding="utf-8")
            self.git(repo, "commit", "-am", "first change")
            self.git(repo, "checkout", "main")
            (repo / "note.txt").write_text("main\n", encoding="utf-8")
            self.git(repo, "commit", "-am", "main change")
            self.git(repo, "checkout", "-b", "workflow/second-lane")
            (repo / "note.txt").write_text("second\n", encoding="utf-8")
            self.git(repo, "commit", "-am", "second change")
            self.git(repo, "checkout", "main")

            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            created = self.run_wf("init", "--title", "Multi Agent", "--prompt", "merge", "--cwd", str(repo), env=env)
            created_data = json.loads(created.stdout)
            run_id = created_data["run_id"]
            run_path = Path(created_data["path"])
            self.run_wf("add-phase", run_id, "--phase-id", "phase-impl", "--name", "Implementation", "--status", "completed", env=env)
            for agent_id, name, branch in (
                ("agent-first", "first", "workflow/first-lane"),
                ("agent-second", "second", "workflow/second-lane"),
            ):
                self.run_wf(
                    "add-agent",
                    run_id,
                    "--phase",
                    "phase-impl",
                    "--agent-id",
                    agent_id,
                    "--name",
                    name,
                    "--agent-type",
                    "codex-exec",
                    "--status",
                    "completed",
                    "--cwd",
                    str(repo),
                    env=env,
                )
            run = json.loads(run_path.read_text(encoding="utf-8"))
            run["agents"][0]["worktree"] = {"branch": "workflow/first-lane", "path": str(tmp_path / "first"), "merge_target": "main", "merge_status": "conflicted", "merge_check_id": "chk-first"}
            run["agents"][1]["worktree"] = {"branch": "workflow/second-lane", "path": str(tmp_path / "second"), "merge_target": "main", "merge_status": "conflicted", "merge_check_id": "chk-second"}
            run.setdefault("checks", []).extend([
                {"check_id": "chk-first", "kind": "merge", "status": "failed", "summary": "first merge failed", "agent_id": "agent-first"},
                {"check_id": "chk-second", "kind": "merge", "status": "failed", "summary": "second merge failed", "agent_id": "agent-second"},
            ])
            run_path.write_text(json.dumps(run, indent=2), encoding="utf-8")

            result = self.run_wf("merge-conflicts", run_id, "--agent", "agent-second", env=env)
            output = json.loads(result.stdout)
            context = json.loads(Path(output["context_path"]).read_text(encoding="utf-8"))

            self.assertEqual(output["agent_id"], "agent-second")
            self.assertEqual(context["agent_id"], "agent-second")
            self.assertEqual(context["merge_check_id"], "chk-second")
            self.assertIn("second merge failed", Path(output["prompt_path"]).read_text(encoding="utf-8"))

    def test_merge_conflicts_prompt_does_not_claim_lane_is_skipped(self) -> None:
        """The merger prompt must not claim the lane is auto-skipped after resolution.

        After a conflict the lane stays 'conflicted' (no merged_at), so re-running
        merge-lanes re-attempts the merge rather than skipping the lane. The prompt
        previously asserted the lane would be skipped, which is false and misleading.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, text=True, capture_output=True)
            (repo / "note.txt").write_text("base\n", encoding="utf-8")
            self.git(repo, "add", "note.txt")
            self.git(repo, "commit", "-m", "base")
            self.git(repo, "checkout", "-b", "workflow/conflict-lane")
            (repo / "note.txt").write_text("lane\n", encoding="utf-8")
            self.git(repo, "commit", "-am", "lane change")
            self.git(repo, "checkout", "main")
            (repo / "note.txt").write_text("main\n", encoding="utf-8")
            self.git(repo, "commit", "-am", "main change")

            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            created = self.run_wf("init", "--title", "Prompt Wording", "--prompt", "merge", "--cwd", str(repo), env=env)
            run_id = json.loads(created.stdout)["run_id"]
            run_path = Path(json.loads(created.stdout)["path"])
            self.run_wf("add-phase", run_id, "--phase-id", "phase-impl", "--name", "Implementation", "--status", "completed", env=env)
            self.run_wf("add-agent", run_id, "--phase", "phase-impl", "--agent-id", "agent-c", "--name", "lane", "--agent-type", "codex-exec", "--status", "completed", "--cwd", str(repo), env=env)
            run = json.loads(run_path.read_text(encoding="utf-8"))
            run["agents"][0]["worktree"] = {"branch": "workflow/conflict-lane", "path": str(tmp_path / "lane"), "merge_target": "main"}
            run_path.write_text(json.dumps(run, indent=2), encoding="utf-8")

            self.run_wf("merge-lanes", run_id, "--leave-conflicts", env=env, check=False)
            result = self.run_wf("merge-conflicts", run_id, env=env)
            prompt = Path(json.loads(result.stdout)["prompt_path"]).read_text(encoding="utf-8")

            self.assertNotIn("will be skipped", prompt)
            self.assertIn("NOT auto-skipped", prompt)
            self.assertIn("Already up to date", prompt)

    def test_merge_conflicts_dirty_hint_is_actionable(self) -> None:
        """The dirty-changes hint must not advise re-running merge-lanes, which refuses a dirty tree."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, text=True, capture_output=True)
            (repo / "note.txt").write_text("base\n", encoding="utf-8")
            self.git(repo, "add", "note.txt")
            self.git(repo, "commit", "-m", "base")
            self.git(repo, "checkout", "-b", "workflow/conflict-lane")
            (repo / "note.txt").write_text("lane\n", encoding="utf-8")
            self.git(repo, "commit", "-am", "lane change")
            self.git(repo, "checkout", "main")
            (repo / "note.txt").write_text("main\n", encoding="utf-8")
            self.git(repo, "commit", "-am", "main change")

            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            created = self.run_wf("init", "--title", "Dirty Hint", "--prompt", "merge", "--cwd", str(repo), env=env)
            run_id = json.loads(created.stdout)["run_id"]
            run_path = Path(json.loads(created.stdout)["path"])
            self.run_wf("add-phase", run_id, "--phase-id", "phase-impl", "--name", "Implementation", "--status", "completed", env=env)
            self.run_wf("add-agent", run_id, "--phase", "phase-impl", "--agent-id", "agent-c", "--name", "lane", "--agent-type", "codex-exec", "--status", "completed", "--cwd", str(repo), env=env)
            run = json.loads(run_path.read_text(encoding="utf-8"))
            run["agents"][0]["worktree"] = {"branch": "workflow/conflict-lane", "path": str(tmp_path / "lane"), "merge_target": "main"}
            run_path.write_text(json.dumps(run, indent=2), encoding="utf-8")

            self.run_wf("merge-lanes", run_id, env=env, check=False)
            self.assertEqual(self.git(repo, "status", "--porcelain").stdout.strip(), "")
            (repo / "unrelated.txt").write_text("user work\n", encoding="utf-8")

            result = self.run_wf("merge-conflicts", run_id, env=env)
            output = json.loads(result.stdout)
            self.assertTrue(output["cwd_has_unrelated_changes"])
            hint = output["hint"]
            self.assertIn("stash", hint.lower())
            # The hint must warn that merge-lanes refuses a dirty tree, and tell
            # the operator to commit/stash first, rather than sending them straight
            # to merge-lanes (which would be rejected by the clean-tree guard).
            self.assertIn("refuses a dirty tree", hint)

    def test_merge_conflicts_recovery_marks_lane_merged_without_duplicate_check_id(self) -> None:
        """Full recovery: resolve, record a fresh passing verify, re-run merge-lanes.

        Guards against two regressions:
        - the documented verify flow must not produce a duplicate check_id;
        - re-running merge-lanes after resolution must mark the lane merged.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, text=True, capture_output=True)
            (repo / "note.txt").write_text("base\n", encoding="utf-8")
            self.git(repo, "add", "note.txt")
            self.git(repo, "commit", "-m", "base")
            self.git(repo, "checkout", "-b", "workflow/conflict-lane")
            (repo / "note.txt").write_text("lane\n", encoding="utf-8")
            self.git(repo, "commit", "-am", "lane change")
            self.git(repo, "checkout", "main")
            (repo / "note.txt").write_text("main\n", encoding="utf-8")
            self.git(repo, "commit", "-am", "main change")

            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            created = self.run_wf("init", "--title", "Recovery", "--prompt", "merge", "--cwd", str(repo), env=env)
            run_id = json.loads(created.stdout)["run_id"]
            run_path = Path(json.loads(created.stdout)["path"])
            self.run_wf("add-phase", run_id, "--phase-id", "phase-impl", "--name", "Implementation", "--status", "completed", env=env)
            self.run_wf("add-agent", run_id, "--phase", "phase-impl", "--agent-id", "agent-c", "--name", "lane", "--agent-type", "codex-exec", "--status", "completed", "--cwd", str(repo), env=env)
            run = json.loads(run_path.read_text(encoding="utf-8"))
            run["agents"][0]["worktree"] = {"branch": "workflow/conflict-lane", "path": str(tmp_path / "lane"), "merge_target": "main"}
            run_path.write_text(json.dumps(run, indent=2), encoding="utf-8")

            self.run_wf("merge-lanes", run_id, "--leave-conflicts", env=env, check=False)
            self.run_wf("merge-conflicts", run_id, env=env)

            # Merger agent resolves the conflict and completes the merge commit.
            self.git(repo, "add", "note.txt")
            self.git(repo, "commit", "--no-edit")
            self.assertEqual(self.git(repo, "status", "--porcelain").stdout.strip(), "")

            # Record a fresh passing verification (no --check-id reuse).
            self.run_wf(
                "verify", run_id, "--record-only", "--status", "passed",
                "--summary", "merge resolved", "--evidence-path", str(repo / "note.txt"),
                env=env,
            )

            # Re-running merge-lanes marks the lane merged.
            result = self.run_wf("merge-lanes", run_id, env=env)
            self.assertEqual(json.loads(result.stdout)["merged"], ["agent-c"])
            run_after = json.loads(run_path.read_text(encoding="utf-8"))
            self.assertEqual(run_after["agents"][0]["worktree"]["merge_status"], "merged")
            self.assertTrue(run_after["agents"][0]["worktree"]["merged_at"])

            # check_id uniqueness invariant across all checks.
            check_ids = [c["check_id"] for c in run_after.get("checks", [])]
            self.assertEqual(len(check_ids), len(set(check_ids)))

    # ------------------------------------------------------------------
    # Tool-call parsing: comprehensive format coverage
    # ------------------------------------------------------------------

    def test_ccc_command_execution_text_transcript(self) -> None:
        """Parse a realistic ccc/codex text transcript with command_execution markers."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        text = "\n".join(
            [
                "[assistant] I'll read the plan first.",
                "[tool:start] command_execution: /usr/bin/bash -lc \"sed -n '1,220p' CLAUDE.md\"",
                "[tool:result] command_execution (ok): /usr/bin/bash -lc \"sed -n '1,220p' CLAUDE.md\"",
                "[tool:result] # Topic tree orientation ...",
                "[tool:start] command_execution: /usr/bin/bash -lc 'wc -l server/src/ws.rs'",
                "[tool:result] command_execution (ok): /usr/bin/bash -lc 'wc -l server/src/ws.rs'",
                "[tool:result] 2817 server/src/ws.rs",
                "[assistant] Now I'll implement the refactor.",
            ]
        )

        activity = workflow_tui.parse_text_activity(text)
        self.assertEqual(activity["tool_call_count"], 2)
        joined = "\n".join(activity["tool_calls"])
        self.assertIn("command_execution", joined)
        self.assertIn("Now I'll implement", activity["latest_output"])
        self.assertEqual(activity["parse_errors"], 0)

    def test_ccc_multiline_result_groups_into_single_tool_call(self) -> None:
        """Multi-line [tool:result] blocks count as one tool call per start/result group."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        text = "\n".join(
            [
                "[tool:start] read: src/main.rs",
                "[tool:result] read (ok): src/main.rs",
                "[tool:result] fn main() {",
                "[tool:result]     println!(\"hello\");",
                "[tool:result] }",
                "[tool:start] write: src/main.rs",
                "[tool:result] write (ok): src/main.rs",
                "[assistant] Updated main.rs",
            ]
        )

        activity = workflow_tui.parse_text_activity(text)
        self.assertEqual(activity["tool_call_count"], 2)
        self.assertIn("read", activity["tool_calls"][0])
        self.assertIn("write", activity["tool_calls"][1])

    def test_kimi_transcript_with_mixed_thinking_and_tools(self) -> None:
        """Kimi transcripts mix bullet-point thinking with • Used markers."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        text = "\n".join(
            [
                "• We need to inspect the codebase first.",
                "  Need be efficient. Let's start.",
                "• Used SetTodoList",
                "  • Inspect branch status ←",
                "  • Run tests",
                "• Used Shell (cd /home/xertrov/src/proj && git status --short)",
                "• The repo is clean. Now let's build.",
                "• Used Shell (cd /home/xertrov/src/proj && cargo build 2>&1)",
                "• Build succeeded. Now run tests.",
                "• Used Shell (cd /home/xertrov/src/proj && cargo test 2>&1)",
            ]
        )

        activity = workflow_tui.parse_text_activity(text)
        self.assertEqual(activity["tool_call_count"], 4)
        joined = "\n".join(activity["tool_calls"])
        self.assertIn("SetTodoList", joined)
        self.assertIn("Shell", joined)
        # Thinking/chat lines should NOT appear as tool calls
        self.assertNotIn("inspect the codebase", joined)
        self.assertNotIn("repo is clean", joined)

    def test_kimi_readfile_and_grep_tool_calls(self) -> None:
        """Kimi ReadFile and Grep tools produce tool-call entries."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        text = "\n".join(
            [
                "• Used ReadFile (src/main.rs)",
                "• Used Grep (pattern: 'fn main', path: src/)",
                "• Used ReadFile (src/lib.rs)",
            ]
        )

        activity = workflow_tui.parse_text_activity(text)
        self.assertEqual(activity["tool_call_count"], 3)
        self.assertIn("ReadFile", activity["tool_calls"][0])
        self.assertIn("Grep", activity["tool_calls"][1])

    def test_codex_jsonl_step_finish_extracts_token_usage(self) -> None:
        """Codex step_finish events carry cumulative token usage."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        text = "\n".join(
            [
                json.dumps({"type": "step_start", "timestamp": 1781580291000, "part": {"type": "step-start"}}),
                json.dumps({
                    "type": "tool_use",
                    "timestamp": 1781580291500,
                    "part": {
                        "type": "tool",
                        "tool": "read",
                        "callID": "call_1",
                        "state": {"status": "completed", "input": {"filePath": "src/main.rs"}, "title": "Read main.rs"},
                    },
                }),
                json.dumps({
                    "type": "step_finish",
                    "timestamp": 1781580292000,
                    "part": {
                        "type": "step-finish",
                        "tokens": {"total": 80585, "input": 27158, "output": 95, "reasoning": 20},
                        "cost": 0,
                    },
                }),
            ]
        )

        activity = workflow_tui.parse_json_activity(text)
        self.assertEqual(activity["tool_call_count"], 1)
        self.assertIn("read", activity["tool_calls"][0])
        self.assertEqual(activity["tokens"]["total"], 80585)
        self.assertEqual(activity["tokens"]["input"], 27158)
        self.assertEqual(activity["tokens"]["output"], 95)
        self.assertEqual(activity["tokens"]["reasoning"], 20)
        self.assertTrue(activity["tokens"]["known"])
        self.assertEqual(activity["tokens"]["total_source"], "reported_total")

    def test_codex_jsonl_cache_read_tokens_gap(self) -> None:
        """Codex ccc step_finish tokens with cache.read are NOT captured as cached_input.

n        This documents a known gap: merge_token_max does not handle ccc's
        cache.read/cache.write nested format. If this test starts failing,
        the gap has been fixed and the assertion should be updated.
        """
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        text = json.dumps({
            "type": "step_finish",
            "timestamp": 1781580291731,
            "part": {
                "type": "step-finish",
                "tokens": {
                    "total": 80585,
                    "input": 27158,
                    "output": 95,
                    "reasoning": 20,
                    "cache": {"write": 0, "read": 53312},
                },
                "cost": 0,
            },
        })

        activity = workflow_tui.parse_json_activity(text)
        self.assertTrue(activity["tokens"]["known"])
        self.assertEqual(activity["tokens"]["total"], 80585)
        # KNOWN GAP: cache.read 53312 is not captured as cached_input
        self.assertEqual(activity["tokens"]["cached_input"], 0)

    def test_codex_jsonl_multi_step_token_accumulation(self) -> None:
        """Multiple step_finish events should accumulate with max semantics."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        text = "\n".join(
            [
                json.dumps({
                    "type": "step_finish",
                    "timestamp": 1781580291000,
                    "part": {"type": "step-finish", "tokens": {"total": 50000, "input": 20000, "output": 50}},
                }),
                json.dumps({
                    "type": "step_finish",
                    "timestamp": 1781580292000,
                    "part": {"type": "step-finish", "tokens": {"total": 80585, "input": 27158, "output": 95, "reasoning": 20}},
                }),
            ]
        )

        activity = workflow_tui.parse_json_activity(text)
        # Should use max values, not sum
        self.assertEqual(activity["tokens"]["total"], 80585)
        self.assertEqual(activity["tokens"]["input"], 27158)
        self.assertEqual(activity["tokens"]["output"], 95)
        self.assertEqual(activity["tokens"]["reasoning"], 20)

    def test_opencode_tool_use_with_callid_and_state(self) -> None:
        """OpenCode format uses part.callID and part.state for tool tracking."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        text = "\n".join(
            [
                json.dumps({
                    "type": "text",
                    "timestamp": 1781158880831,
                    "part": {"type": "text", "text": "Checking the file."},
                }),
                json.dumps({
                    "type": "tool_use",
                    "timestamp": 1781158882007,
                    "part": {
                        "type": "tool",
                        "tool": "bash",
                        "callID": "call_abc123",
                        "state": {
                            "status": "completed",
                            "input": {"command": "python3 -c 'print(42)'"},
                            "title": "Run Python",
                        },
                    },
                }),
            ]
        )

        activity = workflow_tui.parse_json_activity(text)
        self.assertEqual(activity["tool_call_count"], 1)
        self.assertIn("bash", activity["tool_calls"][0])
        self.assertIn("completed", activity["tool_calls"][0])
        self.assertIn("Run Python", activity["tool_calls"][0])
        self.assertIn("Checking the file", activity["latest_output"])

    def test_opencode_step_finish_tokens_use_max_not_sum(self) -> None:
        """OpenCode step_finish tokens are cumulative; merge should use max."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        text = "\n".join(
            [
                json.dumps({"type": "step_finish", "part": {"type": "step-finish", "tokens": {"total": 500, "input": 400, "output": 100}}}),
                json.dumps({"type": "step_finish", "part": {"type": "step-finish", "tokens": {"total": 1200, "input": 900, "output": 300}}}),
            ]
        )

        activity = workflow_tui.parse_json_activity(text)
        self.assertEqual(activity["tokens"]["total"], 1200)
        self.assertEqual(activity["tokens"]["input"], 900)
        self.assertEqual(activity["tokens"]["output"], 300)

    def test_json_activity_extracts_anthropic_token_format(self) -> None:
        """Anthropic uses input_tokens/output_tokens with cache_creation_input_tokens."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        text = json.dumps({
            "type": "message",
            "usage": {
                "input_tokens": 5000,
                "output_tokens": 1500,
                "cache_creation_input_tokens": 200,
                "cache_read_input_tokens": 3000,
            },
        })

        activity = workflow_tui.parse_json_activity(text)
        self.assertTrue(activity["tokens"]["known"])
        self.assertEqual(activity["tokens"]["input"], 5000)
        self.assertEqual(activity["tokens"]["output"], 1500)
        self.assertEqual(activity["tokens"]["cached_input"], 3000)

    def test_json_activity_extracts_openai_token_format(self) -> None:
        """OpenAI uses prompt_tokens/completion_tokens with input_tokens_details."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        text = json.dumps({
            "type": "response.completed",
            "usage": {
                "prompt_tokens": 4000,
                "completion_tokens": 800,
                "input_tokens_details": {"cached_tokens": 2500},
                "output_tokens_details": {"reasoning_tokens": 200},
            },
        })

        activity = workflow_tui.parse_json_activity(text)
        self.assertTrue(activity["tokens"]["known"])
        self.assertEqual(activity["tokens"]["input"], 4000)
        self.assertEqual(activity["tokens"]["output"], 800)
        self.assertEqual(activity["tokens"]["cached_input"], 2500)
        self.assertEqual(activity["tokens"]["reasoning"], 200)

    def test_json_activity_extracts_gemini_token_format(self) -> None:
        """Gemini uses total_tokens/prompt_tokens/completion_tokens."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        text = json.dumps({
            "type": "generateContent",
            "usage": {
                "total_tokens": 6000,
                "prompt_tokens": 5000,
                "completion_tokens": 1000,
            },
        })

        activity = workflow_tui.parse_json_activity(text)
        self.assertTrue(activity["tokens"]["known"])
        self.assertEqual(activity["tokens"]["total"], 6000)
        self.assertEqual(activity["tokens"]["input"], 5000)
        self.assertEqual(activity["tokens"]["output"], 1000)
        self.assertEqual(activity["tokens"]["total_source"], "reported_total")

    def test_json_activity_extracts_tokens_from_part_level(self) -> None:
        """Tokens nested inside part.tokens should be found."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        text = json.dumps({
            "type": "step_finish",
            "part": {
                "type": "step-finish",
                "tokens": {"total": 999, "input": 800, "output": 199},
            },
        })

        activity = workflow_tui.parse_json_activity(text)
        self.assertEqual(activity["tokens"]["total"], 999)
        self.assertEqual(activity["tokens"]["input"], 800)

    def test_json_activity_extracts_tokens_from_item_level(self) -> None:
        """Tokens nested inside item.usage should be found."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        text = json.dumps({
            "type": "item.completed",
            "item": {
                "type": "turn",
                "usage": {"total_tokens": 2000, "input_tokens": 1500, "output_tokens": 500},
            },
        })

        activity = workflow_tui.parse_json_activity(text)
        self.assertEqual(activity["tokens"]["total"], 2000)
        self.assertEqual(activity["tokens"]["input"], 1500)
        self.assertEqual(activity["tokens"]["output"], 500)

    def test_empty_text_transcript_returns_empty_activity(self) -> None:
        """An empty text transcript should return empty activity gracefully."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        activity = workflow_tui.parse_text_activity("")
        self.assertEqual(activity["tool_call_count"], 0)
        self.assertEqual(activity["tool_calls"], [])
        self.assertEqual(activity["latest_output"], "")
        self.assertFalse(activity["tokens"]["known"])

    def test_empty_jsonl_transcript_returns_empty_activity(self) -> None:
        """An empty JSONL transcript should return empty activity gracefully."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        activity = workflow_tui.parse_json_activity("")
        self.assertEqual(activity["tool_call_count"], 0)
        self.assertEqual(activity["tool_calls"], [])
        self.assertEqual(activity["latest_output"], "")
        self.assertEqual(activity["parse_errors"], 0)

    def test_malformed_json_lines_counted_as_parse_errors(self) -> None:
        """Lines that look like JSON but fail to parse should increment parse_errors."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        text = "\n".join(
            [
                "{not valid json",
                json.dumps({"type": "text", "part": {"text": "valid line"}}),
                "{also bad json but starts with brace",
            ]
        )

        activity = workflow_tui.parse_json_activity(text)
        self.assertEqual(activity["parse_errors"], 2)
        self.assertIn("valid line", activity["latest_output"])

    def test_mixed_json_and_non_json_lines_detected_as_json(self) -> None:
        """When most lines are valid JSON objects, treat as JSONL."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        text = "\n".join(
            [
                json.dumps({"type": "text", "part": {"text": "line one"}}),
                json.dumps({"type": "text", "part": {"text": "line two"}}),
                "some non-json output",
            ]
        )

        self.assertTrue(workflow_tui.should_parse_json_activity(text, Path("mixed.jsonl")))

    def test_jsonl_file_extension_prefers_json_parser_even_with_few_json_lines(self) -> None:
        """A .jsonl extension should trigger JSON parsing even if few lines match."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        text = "\n".join(
            [
                "lots of plain text here",
                "more plain text",
                json.dumps({"type": "text", "part": {"text": "one json line"}}),
            ]
        )

        # Without .jsonl extension, would return False (1 json < 2 other)
        self.assertFalse(workflow_tui.should_parse_json_activity(text, Path("output.txt")))
        # With .jsonl extension, returns True (any json lines > 0)
        self.assertTrue(workflow_tui.should_parse_json_activity(text, Path("transcript.jsonl")))

    def test_tool_event_key_extracts_various_id_fields(self) -> None:
        """tool_event_key should find IDs from different provider formats."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        # Codex format: part.callID
        key1 = workflow_tui.tool_event_key({"type": "tool_use", "part": {"type": "tool", "callID": "call_abc"}}, 0)
        self.assertEqual(key1, "call_abc")

        # OpenCode format: part.id
        key2 = workflow_tui.tool_event_key({"type": "tool_use", "part": {"type": "tool", "id": "tool_xyz"}}, 0)
        self.assertEqual(key2, "tool_xyz")

        # Item format: item.id
        key3 = workflow_tui.tool_event_key({"item": {"id": "cmd_123", "type": "command_execution"}}, 0)
        self.assertEqual(key3, "cmd_123")

        # Fallback to index
        key4 = workflow_tui.tool_event_key({"type": "tool_use"}, 42)
        self.assertEqual(key4, "tool-42")

        # Non-tool event returns None
        key5 = workflow_tui.tool_event_key({"type": "text", "part": {"text": "hello"}}, 0)
        self.assertIsNone(key5)

    def test_summarize_tool_call_compacts_various_input_shapes(self) -> None:
        """summarize_tool_call should produce readable labels from different shapes."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        # command_execution format
        cmd_event = {
            "item": {
                "type": "command_execution",
                "command": "python3 -c 'print(42)'",
                "status": "completed",
            }
        }
        cmd_summary = workflow_tui.summarize_tool_call(cmd_event)
        self.assertIn("command_execution", cmd_summary)
        self.assertIn("python3", cmd_summary)

        # tool_use with arguments dict
        arg_event = {
            "type": "tool_use",
            "part": {
                "type": "tool",
                "tool": "grep",
                "state": {
                    "status": "running",
                    "input": {"pattern": "TODO", "path": "src/"},
                },
            },
        }
        arg_summary = workflow_tui.summarize_tool_call(arg_event)
        self.assertIn("grep", arg_summary)
        self.assertIn("running", arg_summary)

    def test_json_activity_preserves_tool_call_order(self) -> None:
        """Tool calls should appear in the order they were first seen."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        text = "\n".join(
            [
                json.dumps({
                    "type": "tool_use",
                    "part": {"type": "tool", "tool": "read", "callID": "c1", "state": {"status": "completed", "input": {"path": "a.rs"}}},
                }),
                json.dumps({
                    "type": "tool_use",
                    "part": {"type": "tool", "tool": "bash", "callID": "c2", "state": {"status": "completed", "input": {"command": "ls"}}},
                }),
                json.dumps({
                    "type": "tool_use",
                    "part": {"type": "tool", "tool": "write", "callID": "c3", "state": {"status": "completed", "input": {"path": "b.rs"}}},
                }),
            ]
        )

        activity = workflow_tui.parse_json_activity(text)
        self.assertEqual(activity["tool_call_count"], 3)
        self.assertIn("read", activity["tool_calls"][0])
        self.assertIn("bash", activity["tool_calls"][1])
        self.assertIn("write", activity["tool_calls"][2])

    def test_json_activity_deduplicates_same_tool_call_id(self) -> None:
        """Multiple events with the same callID should update, not duplicate."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        text = "\n".join(
            [
                json.dumps({
                    "type": "tool_use",
                    "part": {"type": "tool", "tool": "bash", "callID": "call_1", "state": {"status": "running", "input": {"command": "sleep 5"}}},
                }),
                json.dumps({
                    "type": "tool_use",
                    "part": {"type": "tool", "tool": "bash", "callID": "call_1", "state": {"status": "completed", "input": {"command": "sleep 5"}}},
                }),
            ]
        )

        activity = workflow_tui.parse_json_activity(text)
        self.assertEqual(activity["tool_call_count"], 1)
        self.assertIn("completed", activity["tool_calls"][0])

    def test_should_parse_json_activity_rejects_pure_text(self) -> None:
        """Pure text without JSON markers should not be parsed as JSONL."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        text = "\n".join(
            [
                "[assistant] Starting work",
                "[tool:start] bash: echo ok",
                "[tool:result] ok",
            ]
        )

        self.assertFalse(workflow_tui.should_parse_json_activity(text, Path("output.txt")))

    def test_tool_call_count_limited_to_last_six_in_output(self) -> None:
        """Only the last 6 tool calls appear in the activity output."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        lines = []
        for i in range(10):
            lines.append(json.dumps({
                "type": "tool_use",
                "part": {
                    "type": "tool",
                    "tool": f"tool_{i}",
                    "callID": f"call_{i}",
                    "state": {"status": "completed", "input": {}},
                },
            }))

        activity = workflow_tui.parse_json_activity("\n".join(lines))
        self.assertEqual(activity["tool_call_count"], 10)
        self.assertEqual(len(activity["tool_calls"]), 6)
        # Should be the last 6 (tool_4 through tool_9)
        self.assertIn("tool_4", activity["tool_calls"][0])
        self.assertIn("tool_9", activity["tool_calls"][5])

    def test_json_activity_increments_last_activity_epoch(self) -> None:
        """last_activity_epoch should be the max timestamp across all events."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        text = "\n".join(
            [
                json.dumps({"type": "text", "timestamp": 1781580291000, "part": {"text": "first"}}),
                json.dumps({"type": "text", "timestamp": 1781580295000, "part": {"text": "second"}}),
                json.dumps({"type": "text", "timestamp": 1781580293000, "part": {"text": "third"}}),
            ]
        )

        activity = workflow_tui.parse_json_activity(text)
        # 1781580295000 ms = 1781580295.0 seconds
        self.assertAlmostEqual(activity["last_activity_epoch"], 1781580295.0, places=0)

    def test_token_totals_with_zero_values_are_still_known(self) -> None:
        """Provider events with zero token counts should still be marked known."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        text = json.dumps({
            "type": "turn.completed",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        })

        activity = workflow_tui.parse_json_activity(text)
        self.assertTrue(activity["tokens"]["known"])
        self.assertEqual(activity["tokens"]["total"], 0)

    def test_text_activity_only_returns_last_six_tool_calls(self) -> None:
        """Text parser should also limit tool_calls to last 6."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        lines = []
        for i in range(8):
            lines.append(f"[tool:start] tool_{i}: cmd")
            lines.append(f"[tool:result] tool_{i} (ok)")

        activity = workflow_tui.parse_text_activity("\n".join(lines))
        self.assertEqual(activity["tool_call_count"], 8)
        self.assertEqual(len(activity["tool_calls"]), 6)

    def test_json_event_epoch_handles_iso_timestamps(self) -> None:
        """ISO 8601 timestamps should be converted to epoch seconds."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        epoch = workflow_tui.json_event_epoch({"created_at": "2026-06-16T03:25:12Z"})
        self.assertGreater(epoch, 0)
        self.assertAlmostEqual(epoch, 1781580312.0, delta=2.0)

    def test_json_event_epoch_handles_millisecond_timestamps(self) -> None:
        """Millisecond timestamps should be divided by 1000."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        epoch = workflow_tui.json_event_epoch({"timestamp": 1781580291731})
        self.assertAlmostEqual(epoch, 1781580291.731, places=1)

    def test_json_event_epoch_handles_nested_time_dicts(self) -> None:
        """Time dicts with start/end in part.state should be found."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        event = {
            "part": {
                "state": {
                    "time": {"start": 1781580291000, "end": 1781580295000},
                }
            }
        }
        epoch = workflow_tui.json_event_epoch(event)
        self.assertAlmostEqual(epoch, 1781580295.0, places=0)

    def test_should_parse_json_activity_with_tool_type_field(self) -> None:
        """Events with type containing 'tool' should be detected as JSONL."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        text = "\n".join(
            [
                json.dumps({"type": "tool_use", "part": {"type": "tool", "tool": "bash"}}),
                json.dumps({"type": "text", "part": {"text": "hello"}}),
            ]
        )

        self.assertTrue(workflow_tui.should_parse_json_activity(text))

    def test_should_parse_json_activity_rejects_when_mostly_text(self) -> None:
        """When most lines are non-JSON, should return False even with some JSON."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        text = "\n".join(
            [
                "plain text line 1",
                "plain text line 2",
                "plain text line 3",
                json.dumps({"type": "text", "part": {"text": "one json"}}),
            ]
        )

        self.assertFalse(workflow_tui.should_parse_json_activity(text))

    def test_ccc_text_transcript_with_no_assistant_lines_uses_tail(self) -> None:
        """When no [assistant] lines exist, output falls back to last 24 lines."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        text = "\n".join(
            [
                "[tool:start] bash: echo hello",
                "[tool:result] hello",
                "some output line",
                "another output line",
            ]
        )

        activity = workflow_tui.parse_text_activity(text)
        self.assertEqual(activity["tool_call_count"], 1)
        # Should contain the tail lines as output
        self.assertIn("some output line", activity["latest_output"])

    def test_json_activity_with_item_type_command_execution(self) -> None:
        """Codex CLI items of type command_execution should be detected as tools."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        text = json.dumps({
            "type": "item.completed",
            "item": {
                "id": "cmd-1",
                "type": "command_execution",
                "command": "cargo test",
                "status": "completed",
            },
        })

        activity = workflow_tui.parse_json_activity(text)
        self.assertEqual(activity["tool_call_count"], 1)
        self.assertIn("cargo test", activity["tool_calls"][0])

    def test_json_activity_with_item_type_tool_call(self) -> None:
        """Items with type tool_call should also be detected."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        text = json.dumps({
            "type": "item.completed",
            "item": {
                "id": "tc-1",
                "type": "tool_call",
                "name": "read_file",
                "arguments": {"path": "src/main.rs"},
                "status": "completed",
            },
        })

        activity = workflow_tui.parse_json_activity(text)
        self.assertEqual(activity["tool_call_count"], 1)
        self.assertIn("read_file", activity["tool_calls"][0])


class StateTruthfulnessTests(unittest.TestCase):
    """Focused tests for P1 state truthfulness improvements."""

    def test_native_subagent_without_liveness_is_classified_as_unmanaged(self) -> None:
        """Native subagents without process/transcript should be treated as unmanaged sidecars."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_health  # pylint: disable=import-outside-toplevel
        import workflow_state  # pylint: disable=import-outside-toplevel

        agent = {"agent_id": "ns-1", "status": "running", "agent_type": "native-subagent", "thread_id": ""}
        self.assertTrue(workflow_health.agent_is_effectively_unmanaged(agent))
        self.assertTrue(workflow_state.is_native_subagent(agent))

    def test_native_subagent_with_thread_id_has_liveness(self) -> None:
        """Native subagents with thread_id have a liveness source and are not flagged opaque."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_health  # pylint: disable=import-outside-toplevel

        agent = {"agent_id": "ns-2", "status": "running", "agent_type": "native-subagent", "thread_id": "ses_abc"}
        run = {"run_id": "wf-test", "status": "running", "phases": [], "agents": [agent], "artifacts": []}
        findings = workflow_health.analyze_run(run)
        self.assertFalse(any(item["kind"] == "agent-opaque-running" for item in findings))

    def test_native_subagent_without_liveness_does_not_warn_opaque(self) -> None:
        """Native subagents without liveness should use unmanaged classification, not opaque warning."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_health  # pylint: disable=import-outside-toplevel

        agent = {"agent_id": "ns-3", "status": "running", "agent_type": "native-subagent"}
        run = {"run_id": "wf-test", "status": "running", "phases": [], "agents": [agent], "artifacts": []}
        findings = workflow_health.analyze_run(run)
        self.assertFalse(any(item["kind"] == "agent-opaque-running" for item in findings))

    def test_external_worker_without_liveness_warns_opaque(self) -> None:
        """External workers (non-native-subagent) without liveness should still warn opaque."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_health  # pylint: disable=import-outside-toplevel

        agent = {"agent_id": "ext-1", "status": "running", "agent_type": "codex-exec"}
        run = {"run_id": "wf-test", "status": "running", "phases": [], "agents": [agent], "artifacts": []}
        findings = workflow_health.analyze_run(run)
        self.assertTrue(any(item["kind"] == "agent-opaque-running" for item in findings))

    def test_unmanaged_flag_overrides_native_subagent_check(self) -> None:
        """An explicit unmanaged=true flag should always make an agent effectively unmanaged."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_health  # pylint: disable=import-outside-toplevel

        agent = {"agent_id": "um-1", "status": "running", "agent_type": "codex-exec", "unmanaged": True}
        self.assertTrue(workflow_health.agent_is_effectively_unmanaged(agent))

    def test_completed_agent_with_empty_output_shows_fallback_in_tui(self) -> None:
        """A completed agent with empty output but useful transcript should produce fallback output."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            jsonl_path = tmp_path / "worker.jsonl"
            jsonl_path.write_text(
                json.dumps({"type": "step_finish", "part": {"tokens": {"total": 42, "input": 40, "output": 2}}}) + "\n",
                encoding="utf-8",
            )
            agent = {
                "agent_id": "agent-fallback",
                "name": "Fallback Agent",
                "status": "completed",
                "jsonl_path": str(jsonl_path),
                "output_path": str(tmp_path / "missing.final.md"),
                "log_path": "",
                "result": "",
                "summary": "",
                "exit_code": 0,
            }
            activity = workflow_tui.agent_activity(agent)
            self.assertIn("fallback_output", activity)
            self.assertIn("exit code", activity["fallback_output"])

    def test_cancelled_agent_with_stop_reason_shows_fallback(self) -> None:
        """A cancelled agent with a stop_result should surface the termination reason."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_tui  # pylint: disable=import-outside-toplevel

        agent = {
            "agent_id": "agent-cancelled",
            "name": "Cancelled Agent",
            "status": "cancelled",
            "jsonl_path": "",
            "output_path": "",
            "log_path": "",
            "result": "",
            "summary": "cancelled by operator",
            "stop_result": {"sent": True, "reason": "SIGTERM", "target": "process_group"},
        }
        activity = workflow_tui.agent_activity(agent)
        self.assertIn("fallback_output", activity)
        self.assertIn("SIGTERM", activity["fallback_output"])

    def test_agent_output_empty_health_check_warns_for_completed_with_fallback_data(self) -> None:
        """Health check should warn when a completed agent has empty output but fallback data exists."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_health  # pylint: disable=import-outside-toplevel

        agent = {
            "agent_id": "agent-empty",
            "name": "Empty Agent",
            "status": "completed",
            "phase_id": "phase-impl",
            "result": "",
            "summary": "",
            "jsonl_path": "logs/worker.jsonl",
            "output_path": "",
            "exit_code": 0,
        }
        run = {
            "run_id": "wf-test",
            "status": "running",
            "phases": [{"phase_id": "phase-impl", "status": "running"}],
            "agents": [agent],
            "artifacts": [],
        }
        findings = workflow_health.analyze_run(run)
        empty_warning = [item for item in findings if item["kind"] == "agent-output-empty"]
        self.assertEqual(len(empty_warning), 1)
        self.assertIn("transcript", empty_warning[0]["message"])
        self.assertIn("exit code", empty_warning[0]["message"])

    def test_agent_output_empty_health_check_silent_when_no_fallback(self) -> None:
        """No output-empty warning when no fallback data is available either."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_health  # pylint: disable=import-outside-toplevel

        agent = {
            "agent_id": "agent-nothing",
            "name": "Nothing Agent",
            "status": "completed",
            "phase_id": "phase-impl",
            "result": "",
            "summary": "done",
            "jsonl_path": "",
            "output_path": "",
        }
        run = {
            "run_id": "wf-test",
            "status": "running",
            "phases": [{"phase_id": "phase-impl", "status": "running"}],
            "agents": [agent],
            "artifacts": [],
        }
        findings = workflow_health.analyze_run(run)
        self.assertFalse(any(item["kind"] == "agent-output-empty" for item in findings))

    def test_lane_scope_violations_detects_out_of_scope_changes(self) -> None:
        """merge-lanes should record scope-violation events when files outside write_scope change."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, text=True, capture_output=True)
            (repo / "src").mkdir()
            (repo / "src" / "main.py").write_text("print('hello')\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True, text=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=t@t", "commit", "-m", "base"],
                check=True, text=True, capture_output=True,
            )
            subprocess.run(["git", "-C", str(repo), "checkout", "-b", "workflow/lane"], check=True, text=True, capture_output=True)
            (repo / "src" / "main.py").write_text("print('changed')\n", encoding="utf-8")
            (repo / "tests").mkdir()
            (repo / "tests" / "test_main.py").write_text("assert True\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True, text=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=t@t", "commit", "-m", "lane change"],
                check=True, text=True, capture_output=True,
            )
            subprocess.run(["git", "-C", str(repo), "checkout", "main"], check=True, text=True, capture_output=True)

            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            scripts = str(SCRIPTS)
            wf = str(SCRIPTS / "wf")

            created = subprocess.run(
                [wf, "init", "--title", "Scope Check", "--prompt", "scope", "--cwd", str(repo)],
                check=True, text=True, capture_output=True, env=env,
            )
            run_id = json.loads(created.stdout)["run_id"]
            subprocess.run(
                [wf, "add-phase", run_id, "--phase-id", "phase-impl", "--name", "Impl", "--status", "completed"],
                check=True, text=True, capture_output=True, env=env,
            )
            subprocess.run(
                [wf, "add-agent", run_id, "--phase", "phase-impl", "--agent-id", "agent-scope",
                 "--name", "scoped", "--agent-type", "codex-exec", "--status", "completed",
                 "--write-scope", "src", "--cwd", str(repo)],
                check=True, text=True, capture_output=True, env=env,
            )

            run_path = Path(json.loads(created.stdout)["path"])
            run = json.loads(run_path.read_text(encoding="utf-8"))
            run["agents"][0]["worktree"] = {
                "branch": "workflow/lane",
                "path": str(tmp_path / "lane"),
                "merge_target": "main",
                "base": "HEAD",
            }
            run_path.write_text(json.dumps(run, indent=2), encoding="utf-8")

            result = subprocess.run(
                [wf, "merge-lanes", run_id],
                check=False, text=True, capture_output=True, env=env,
            )
            merged = json.loads(result.stdout)
            self.assertIn("scope_warnings", merged)
            self.assertTrue(any(w["agent_id"] == "agent-scope" for w in merged["scope_warnings"]))
            violations = next(w["violations"] for w in merged["scope_warnings"] if w["agent_id"] == "agent-scope")
            self.assertIn("tests/test_main.py", violations)

            run_after = json.loads(run_path.read_text(encoding="utf-8"))
            self.assertTrue(
                any(event.get("operation") == "scope-violation" for event in run_after["events"]),
                msg="expected scope-violation event in run state",
            )

    def test_lane_scope_violations_empty_when_no_write_scope(self) -> None:
        """No scope violations when write_scope is empty (no restriction)."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_ops  # pylint: disable=import-outside-toplevel

        agent = {
            "agent_id": "agent-unscoped",
            "write_scope": [],
            "worktree": {"branch": "workflow/lane", "base": "HEAD"},
        }
        violations = workflow_ops.lane_scope_violations(agent, "/tmp")
        self.assertEqual(violations, [])

    def test_lane_scope_violations_covers_nested_paths(self) -> None:
        """write_scope entries should cover files in subdirectories."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_ops  # pylint: disable=import-outside-toplevel

        agent = {
            "agent_id": "agent-nested",
            "write_scope": ["src/module"],
            "worktree": {"branch": "workflow/lane", "base": "HEAD"},
        }

        def fake_git_checked(cwd: str, *args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 0, stdout="src/module/deep/file.py\nsrc/other.py\n", stderr="")

        original = workflow_ops.git_checked
        workflow_ops.git_checked = fake_git_checked  # type: ignore[assignment]
        try:
            violations = workflow_ops.lane_scope_violations(agent, "/tmp")
        finally:
            workflow_ops.git_checked = original  # type: ignore[assignment]

        self.assertEqual(violations, ["src/other.py"])

    def test_lane_scope_violations_does_not_match_prefix_collisions(self) -> None:
        """write_scope must not match path strings that share a prefix without a directory separator."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_ops  # pylint: disable=import-outside-toplevel

        agent = {
            "agent_id": "agent-prefix",
            "write_scope": ["src", "tests"],
            "worktree": {"branch": "workflow/lane", "base": "HEAD"},
        }

        def fake_git_checked(cwd: str, *args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout="src/main.py\nsrcs/sibling.py\nsrc_test.py\nsrc.md\ntests/ok.py\ntests_old/legacy.py\n",
                stderr="",
            )

        original = workflow_ops.git_checked
        workflow_ops.git_checked = fake_git_checked  # type: ignore[assignment]
        try:
            violations = workflow_ops.lane_scope_violations(agent, "/tmp")
        finally:
            workflow_ops.git_checked = original  # type: ignore[assignment]

        self.assertEqual(violations, ["srcs/sibling.py", "src_test.py", "src.md", "tests_old/legacy.py"])

    def test_lane_scope_violations_handles_blank_scope_entries(self) -> None:
        """Blank scope strings should not match every file path."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_ops  # pylint: disable=import-outside-toplevel

        agent = {
            "agent_id": "agent-blank-scope",
            "write_scope": ["", "src"],
            "worktree": {"branch": "workflow/lane", "base": "HEAD"},
        }

        def fake_git_checked(cwd: str, *args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 0, stdout="src/main.py\nother/foo.py\n", stderr="")

        original = workflow_ops.git_checked
        workflow_ops.git_checked = fake_git_checked  # type: ignore[assignment]
        try:
            violations = workflow_ops.lane_scope_violations(agent, "/tmp")
        finally:
            workflow_ops.git_checked = original  # type: ignore[assignment]

        self.assertEqual(violations, ["other/foo.py"])

    def test_merge_lanes_dry_run_does_not_mutate_state_with_scope_warnings(self) -> None:
        """merge-lanes --dry-run must surface scope_warnings without writing scope-violation events."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, text=True, capture_output=True)
            (repo / "src").mkdir()
            (repo / "src" / "main.py").write_text("print('hello')\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True, text=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=t@t", "commit", "-m", "base"],
                check=True, text=True, capture_output=True,
            )
            subprocess.run(["git", "-C", str(repo), "checkout", "-b", "workflow/lane"], check=True, text=True, capture_output=True)
            (repo / "tests").mkdir()
            (repo / "tests" / "test_main.py").write_text("assert True\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True, text=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=t@t", "commit", "-m", "lane change"],
                check=True, text=True, capture_output=True,
            )
            subprocess.run(["git", "-C", str(repo), "checkout", "main"], check=True, text=True, capture_output=True)

            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            wf = str(SCRIPTS / "wf")

            created = subprocess.run(
                [wf, "init", "--title", "Dry Run Scope", "--prompt", "scope", "--cwd", str(repo)],
                check=True, text=True, capture_output=True, env=env,
            )
            run_id = json.loads(created.stdout)["run_id"]
            subprocess.run(
                [wf, "add-phase", run_id, "--phase-id", "phase-impl", "--name", "Impl", "--status", "completed"],
                check=True, text=True, capture_output=True, env=env,
            )
            subprocess.run(
                [wf, "add-agent", run_id, "--phase", "phase-impl", "--agent-id", "agent-scope",
                 "--name", "scoped", "--agent-type", "codex-exec", "--status", "completed",
                 "--write-scope", "src", "--cwd", str(repo)],
                check=True, text=True, capture_output=True, env=env,
            )

            run_path = Path(json.loads(created.stdout)["path"])
            run = json.loads(run_path.read_text(encoding="utf-8"))
            run["agents"][0]["worktree"] = {
                "branch": "workflow/lane",
                "path": str(tmp_path / "lane"),
                "merge_target": "main",
                "base": "HEAD",
            }
            run_path.write_text(json.dumps(run, indent=2), encoding="utf-8")
            events_before = list(json.loads(run_path.read_text(encoding="utf-8")).get("events", []))

            for _ in range(3):
                result = subprocess.run(
                    [wf, "merge-lanes", run_id, "--dry-run"],
                    check=False, text=True, capture_output=True, env=env,
                )
                payload = json.loads(result.stdout)
                self.assertIn("scope_warnings", payload)
                self.assertEqual(payload["skipped"], ["agent-scope"])
                self.assertEqual(payload["merged"], [])

            events_after = json.loads(run_path.read_text(encoding="utf-8")).get("events", [])
            scope_events = [e for e in events_after if e.get("operation") == "scope-violation"]
            self.assertEqual(
                scope_events, [], msg=f"dry-run must not write scope-violation events; got {len(scope_events)}",
            )
            self.assertEqual(
                len(events_after), len(events_before),
                msg="dry-run must not mutate the run event log",
            )

    def test_empty_output_file_is_treated_as_no_output(self) -> None:
        """A zero-byte final-output artifact must not count as having output."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_health  # pylint: disable=import-outside-toplevel

        with tempfile.TemporaryDirectory() as tmp:
            empty_out = Path(tmp) / "empty.md"
            empty_out.write_text("", encoding="utf-8")
            agent = {
                "agent_id": "agent-empty-file",
                "name": "Empty File Agent",
                "status": "completed",
                "phase_id": "phase-impl",
                "result": "",
                "summary": "",
                "jsonl_path": str(Path(tmp) / "worker.jsonl"),
                "output_path": str(empty_out),
                "exit_code": 0,
            }
            run = {
                "run_id": "wf-test",
                "status": "running",
                "phases": [{"phase_id": "phase-impl", "status": "running"}],
                "agents": [agent],
                "artifacts": [],
                "paths": {"run_dir": tmp},
            }
            findings = workflow_health.analyze_run(run)
            self.assertTrue(any(item["kind"] == "agent-output-empty" for item in findings))

    def test_missing_output_file_does_not_double_report_empty(self) -> None:
        """A missing output file reports agent-output-missing, not also agent-output-empty."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_health  # pylint: disable=import-outside-toplevel

        with tempfile.TemporaryDirectory() as tmp:
            agent = {
                "agent_id": "agent-missing-file",
                "name": "Missing File Agent",
                "status": "completed",
                "phase_id": "phase-impl",
                "result": "",
                "summary": "",
                "jsonl_path": str(Path(tmp) / "worker.jsonl"),
                "output_path": str(Path(tmp) / "absent.md"),
                "exit_code": 0,
            }
            run = {
                "run_id": "wf-test",
                "status": "running",
                "phases": [{"phase_id": "phase-impl", "status": "running"}],
                "agents": [agent],
                "artifacts": [],
                "paths": {"run_dir": tmp},
            }
            findings = workflow_health.analyze_run(run)
            kinds = {item["kind"] for item in findings if item.get("agent_id") == "agent-missing-file"}
            self.assertIn("agent-output-missing", kinds)
            self.assertNotIn("agent-output-empty", kinds)

    def test_stop_result_without_reason_is_not_promised_as_fallback(self) -> None:
        """A stop_result dict without a reason must not be advertised as a termination fallback."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_health  # pylint: disable=import-outside-toplevel

        agent = {
            "agent_id": "agent-no-reason",
            "name": "No Reason Agent",
            "status": "cancelled",
            "phase_id": "phase-impl",
            "result": "",
            "summary": "",
            "jsonl_path": "logs/worker.jsonl",
            "output_path": "",
            "exit_code": None,
            "stop_result": {"sent": False},
        }
        run = {
            "run_id": "wf-test",
            "status": "running",
            "phases": [{"phase_id": "phase-impl", "status": "running"}],
            "agents": [agent],
            "artifacts": [],
        }
        findings = workflow_health.analyze_run(run)
        empty_warning = [item for item in findings if item["kind"] == "agent-output-empty"]
        self.assertEqual(len(empty_warning), 1)
        self.assertNotIn("termination result", empty_warning[0]["message"])
        self.assertIn("transcript", empty_warning[0]["message"])

    def test_lane_scope_violations_raises_on_unresolvable_diff(self) -> None:
        """A failed git diff must surface as a ScopeCheckError, not a silent clean pass."""
        sys.path.insert(0, str(SCRIPTS))
        import workflow_ops  # pylint: disable=import-outside-toplevel

        agent = {
            "agent_id": "agent-bad-base",
            "write_scope": ["src"],
            "worktree": {"branch": "workflow/lane", "base": "nonexistent-ref-xyz"},
        }
        with self.assertRaises(workflow_ops.ScopeCheckError):
            workflow_ops.lane_scope_violations(agent, "/tmp")

    def test_merge_lanes_surfaces_scope_check_error_instead_of_silent_pass(self) -> None:
        """merge-lanes must record a scope-check-error warning when the diff ref is unresolvable."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, text=True, capture_output=True)
            (repo / "src").mkdir()
            (repo / "src" / "main.py").write_text("print('hello')\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True, text=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=t@t", "commit", "-m", "base"],
                check=True, text=True, capture_output=True,
            )

            env = os.environ.copy()
            env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
            wf = str(SCRIPTS / "wf")

            created = subprocess.run(
                [wf, "init", "--title", "Bad Base", "--prompt", "scope", "--cwd", str(repo)],
                check=True, text=True, capture_output=True, env=env,
            )
            run_id = json.loads(created.stdout)["run_id"]
            subprocess.run(
                [wf, "add-phase", run_id, "--phase-id", "phase-impl", "--name", "Impl", "--status", "completed"],
                check=True, text=True, capture_output=True, env=env,
            )
            subprocess.run(
                [wf, "add-agent", run_id, "--phase", "phase-impl", "--agent-id", "agent-bad",
                 "--name", "badbase", "--agent-type", "codex-exec", "--status", "completed",
                 "--write-scope", "src", "--cwd", str(repo)],
                check=True, text=True, capture_output=True, env=env,
            )

            run_path = Path(json.loads(created.stdout)["path"])
            run = json.loads(run_path.read_text(encoding="utf-8"))
            run["agents"][0]["worktree"] = {
                "branch": "workflow/missing-lane",
                "path": str(tmp_path / "lane"),
                "merge_target": "main",
                "base": "HEAD",
            }
            run_path.write_text(json.dumps(run, indent=2), encoding="utf-8")

            result = subprocess.run(
                [wf, "merge-lanes", run_id, "--dry-run"],
                check=False, text=True, capture_output=True, env=env,
            )
            payload = json.loads(result.stdout)
            self.assertIn("scope_warnings", payload)
            warning = next(w for w in payload["scope_warnings"] if w["agent_id"] == "agent-bad")
            self.assertIn("error", warning)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Tests for the workflow skill scripts and TUI snapshots."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import io
import textwrap
import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FIXTURE = ROOT / "tests" / "fixtures" / "rich-workflow.json"
MANY_FIXTURE = ROOT / "tests" / "fixtures" / "many-rows.json"
E2E_FIXTURE = ROOT / "tests" / "fixtures" / "e2e-workflow" / "run.json"
SNAPSHOTS = ROOT / "tests" / "snapshots"


class WorkflowScriptTests(unittest.TestCase):
    """Verify workflow state, worker mocking, and snapshot rendering."""

    def run_script(self, script: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, str(SCRIPTS / script), *args]
        return subprocess.run(command, check=True, text=True, capture_output=True, env=env)

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

    def snapshot_env(self) -> dict[str, str]:
        """Return a deterministic local timezone for TUI snapshot rendering."""
        env = os.environ.copy()
        env["TZ"] = "Australia/Sydney"
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
        return env

    def read_ccc_events(self, tmp_path: Path) -> list[dict[str, object]]:
        """Read fake ccc event records created by install_timed_fake_ccc."""
        events_path = tmp_path / "ccc-events.jsonl"
        return [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]

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

    def test_wf_wrapper_defaults_state_to_checkout_parent(self) -> None:
        """A user-scope checkout under ~/.agents/skills should keep state beside it."""
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
            self.assertTrue(str(run["paths"]["run_dir"]).startswith(str(tmp_path / ".agents" / "workflow-system")))

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
            self.assertEqual(agent["output_path"], str(fake_run_dir / "output.txt"))
            self.assertEqual(agent["jsonl_path"], str(fake_run_dir / "transcript.txt"))
            self.assertEqual(agent["thread_id"], fake_run_dir.name)
            self.assertEqual(data["artifacts"][0]["path"], str(fake_run_dir / "output.txt"))
            fake_args = args_path.read_text(encoding="utf-8").splitlines()
            self.assertIn("--output-mode", fake_args)
            self.assertIn("stream-json", fake_args)
            self.assertIn("opencode", fake_args)
            self.assertIn("--", fake_args)
            self.assertEqual(fake_args[-1], "Alpha prompt")

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

    def test_snapshot_fixtures_match_checked_in_screens(self) -> None:
        """Render every TUI tab from fixtures and compare to text snapshots."""
        cases = [
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

    def test_snapshot_header_describes_pane_and_tab_navigation(self) -> None:
        """Keep the TUI header explicit about section and row navigation."""
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
        self.assertIn("←/→ tabs", rendered)
        self.assertIn("y id", rendered)
        self.assertIn("p path", rendered)
        self.assertIn("a scope", rendered)
        self.assertIn("v view", rendered)
        self.assertNotIn("tab side/main", rendered)
        self.assertNotIn("→ main", rendered)
        self.assertNotIn("← main tabs", rendered)

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
        self.assertEqual(workflow_tui.display_event_timestamp("2026-06-11T00:05:00Z", now=reference), "10:05 AEST")
        self.assertEqual(workflow_tui.display_event_timestamp("2026-06-09T21:05:00Z", now=reference), "26-06-10 07:05")
        self.assertEqual(workflow_tui.display_event_timestamp("", now=reference.astimezone(timezone.utc)), "")

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
        self.assertIn("10:05 AEST", events_rendered)
        self.assertIn("time          10:05 AEST", events_rendered)
        self.assertIn("10:04 AEST", decisions_rendered)
        self.assertIn("time          10:04 AEST", decisions_rendered)
        self.assertNotIn("2026-06-11T00:00:…", events_rendered)
        self.assertNotIn("2026-06-11T00:04:…", decisions_rendered)

    def test_sidebar_tables_prioritize_readable_labels(self) -> None:
        """Sidebar tables should hide low-value columns before truncating important names."""
        rendered = self.run_script(
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
        self.assertIn("Research", rendered)
        self.assertIn("Review", rendered)
        self.assertIn("Synthesis", rendered)
        self.assertNotIn("phase-synthesis", rendered.split("╭", 2)[1])

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
        self.assertIn("time          10:05 AEST", rendered)
        self.assertIn(" art ", rendered)
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
        self.assertIn("capture initial-runs", result.stdout)
        self.assertIn("send-key Right", result.stdout)
        self.assertIn("send-key y", result.stdout)
        self.assertIn("send-key p", result.stdout)

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


if __name__ == "__main__":
    unittest.main()

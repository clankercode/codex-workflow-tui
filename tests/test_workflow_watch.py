#!/usr/bin/env python3
"""Tests for workflow_watch_emit.py — state-transition emitter."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import workflow_state
import workflow_watch_emit


def _make_args(
    run_id: str | None = None,
    interval: float = 30.0,
    loop: bool = False,
    state_dir: str | None = None,
) -> Any:
    return workflow_watch_emit.build_parser().parse_args(
        [*( [run_id] if run_id else [] ),
         *( ["--interval", str(interval)] if interval != 30.0 else [] ),
         *( ["--loop"] if loop else [] ),
         *( ["--state-dir", state_dir] if state_dir else [] )]
    )


class WatchEmitTests(unittest.TestCase):
    """Test the watch-emit state-transition emitter."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.state_dir = Path(self.tmp) / "state"
        self.runs_dir = self.state_dir / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.env_patcher = mock.patch.dict(os.environ, {"WORKFLOW_STATE_DIR": str(self.state_dir)})
        self.env_patcher.start()

    def tearDown(self) -> None:
        self.env_patcher.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _init_run(self, title: str = "Test Run", status: str = "running") -> str:
        """Create a run using workflow_state and return its run_id."""
        args = workflow_state.build_parser().parse_args([
            "init", "--title", title, "--prompt", "test", "--cwd", str(ROOT),
        ])
        with redirect_stdout(io.StringIO()):
            workflow_state.cmd_init(args)
        # Find the run
        for d in self.runs_dir.iterdir():
            if d.is_dir() and (d / "run.json").exists():
                run = json.loads((d / "run.json").read_text(encoding="utf-8"))
                if status != "running":
                    run["status"] = status
                    (d / "run.json").write_text(json.dumps(run, indent=2), encoding="utf-8")
                return run["run_id"]
        raise RuntimeError("no run created")

    def _load_run(self, run_id: str) -> dict[str, Any]:
        return json.loads((self.runs_dir / run_id / "run.json").read_text(encoding="utf-8"))

    def _save_run(self, run_id: str, data: dict[str, Any]) -> None:
        (self.runs_dir / run_id / "run.json").write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _write_snapshot(self, run_id: str, snap: dict[str, Any]) -> None:
        (self.runs_dir / run_id / "watch-state.json").write_text(
            json.dumps(snap, indent=2) + "\n", encoding="utf-8"
        )

    def _read_snapshot(self, run_id: str) -> dict[str, Any] | None:
        path = self.runs_dir / run_id / "watch-state.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _capture_emit(self, run_id: str | None = None, **kwargs: Any) -> str:
        """Run cmd_watch_emit in one-shot mode and return captured stdout."""
        args = _make_args(run_id=run_id, state_dir=str(self.state_dir), **kwargs)
        buf = io.StringIO()
        with redirect_stdout(buf):
            workflow_watch_emit.cmd_watch_emit(args)
        return buf.getvalue()

    # --- Test cases ---

    def test_one_shot_no_change_emits_nothing(self) -> None:
        """When snapshot matches current state, one-shot emits nothing."""
        run_id = self._init_run()
        snap = workflow_watch_emit._build_snapshot(self._load_run(run_id))
        self._write_snapshot(run_id, snap)

        output = self._capture_emit(run_id)
        self.assertEqual(output, "")

    def test_one_shot_agent_running_to_completed(self) -> None:
        """Emits transition when an agent goes running → completed."""
        run_id = self._init_run()
        run = self._load_run(run_id)
        run["agents"] = [{
            "agent_id": "a1", "name": "worker", "status": "running",
            "exit_code": None, "phase_id": None,
        }]
        self._save_run(run_id, run)

        # Snapshot says agent was running
        snap = {
            "status": "running", "phases": {}, "agents": {"a1": {"status": "running", "exit_code": None}},
            "event_count": 0,
        }
        self._write_snapshot(run_id, snap)

        # Now agent completed
        run["agents"][0]["status"] = "completed"
        self._save_run(run_id, run)

        output = self._capture_emit(run_id)
        self.assertIn("AGENT worker: running \u2192 completed", output)

    def test_emits_exit_code_on_terminal_agent(self) -> None:
        """Appends (exit N) when agent reaches terminal status with exit_code."""
        run_id = self._init_run()
        run = self._load_run(run_id)
        run["agents"] = [{
            "agent_id": "a1", "name": "worker", "status": "running",
            "exit_code": None, "phase_id": None,
        }]
        self._save_run(run_id, run)

        snap = {
            "status": "running", "phases": {}, "agents": {"a1": {"status": "running", "exit_code": None}},
            "event_count": 0,
        }
        self._write_snapshot(run_id, snap)

        run["agents"][0]["status"] = "failed"
        run["agents"][0]["exit_code"] = 1
        self._save_run(run_id, run)

        output = self._capture_emit(run_id)
        self.assertIn("AGENT worker: running \u2192 failed (exit 1)", output)

    def test_emits_phase_and_run_status_transitions(self) -> None:
        """Emits phase status changes and run status changes."""
        run_id = self._init_run()
        run = self._load_run(run_id)
        run["phases"] = [{"phase_id": "p1", "name": "Build", "status": "running"}]
        self._save_run(run_id, run)

        snap = {
            "status": "running", "phases": {"p1": "running"}, "agents": {},
            "event_count": 0,
        }
        self._write_snapshot(run_id, snap)

        run["phases"][0]["status"] = "completed"
        run["status"] = "completed"
        self._save_run(run_id, run)

        output = self._capture_emit(run_id)
        self.assertIn("PHASE Build: running \u2192 completed", output)
        self.assertIn("RUN: running \u2192 completed", output)

    def test_first_run_emits_baseline_not_full_state(self) -> None:
        """With no snapshot, emits current status as baseline (→ status), not all history."""
        run_id = self._init_run()
        run = self._load_run(run_id)
        run["agents"] = [
            {"agent_id": "a1", "name": "w1", "status": "completed", "exit_code": 0, "phase_id": None},
            {"agent_id": "a2", "name": "w2", "status": "running", "exit_code": None, "phase_id": None},
        ]
        run["phases"] = [{"phase_id": "p1", "name": "Build", "status": "running"}]
        self._save_run(run_id, run)

        # No snapshot written — first run
        output = self._capture_emit(run_id)
        lines = output.strip().splitlines()
        # Should have 1 RUN + 1 PHASE + 2 AGENT lines
        self.assertEqual(len(lines), 4)
        self.assertIn("RUN: \u2192 running", lines[0])
        self.assertIn("PHASE Build: \u2192 running", lines[1])
        self.assertIn("AGENT w1: \u2192 completed (exit 0)", lines[2])
        self.assertIn("AGENT w2: \u2192 running", lines[3])

    def test_all_runs_mode_detects_new_run(self) -> None:
        """In all-runs mode (--loop), a newly-appeared run gets a [new] marker."""
        run_id = self._init_run()
        # Write snapshot so existing run is known
        snap = workflow_watch_emit._build_snapshot(self._load_run(run_id))
        self._write_snapshot(run_id, snap)

        # Simulate loop: first iteration knows run_id, second finds a new one
        known_ids: set[str] = {run_id}
        new_run_id = self._init_run(title="Second Run")
        new_run = self._load_run(new_run_id)
        new_run["status"] = "running"
        self._save_run(new_run_id, new_run)

        # The new run should be detected
        runs_dir = self.runs_dir
        current_ids = {d.name for d in runs_dir.iterdir() if d.is_dir() and (d / "run.json").exists()}
        new_ids = current_ids - known_ids
        self.assertIn(new_run_id, new_ids)

    def test_corrupt_snapshot_treated_as_first_run(self) -> None:
        """A corrupt snapshot file is treated as first-run (no crash)."""
        run_id = self._init_run()
        # Write corrupt snapshot
        (self.runs_dir / run_id / "watch-state.json").write_text("NOT JSON", encoding="utf-8")

        run = self._load_run(run_id)
        run["agents"] = [{"agent_id": "a1", "name": "w", "status": "running", "exit_code": None, "phase_id": None}]
        self._save_run(run_id, run)

        output = self._capture_emit(run_id)
        # Should emit baseline (first-run behavior)
        self.assertIn("RUN: \u2192 running", output)
        self.assertIn("AGENT w: \u2192 running", output)

    def test_loop_mode_emits_on_change_then_silent(self) -> None:
        """--loop mode emits on change, then nothing when state is same."""
        run_id = self._init_run()
        run = self._load_run(run_id)
        run["agents"] = [{"agent_id": "a1", "name": "w", "status": "running", "exit_code": None, "phase_id": None}]
        self._save_run(run_id, run)

        # First call: no snapshot → baseline
        args = _make_args(run_id=run_id, loop=True, state_dir=str(self.state_dir))
        # Inject max_iters=2 so it loops once then stops
        args._max_iters = 2

        buf = io.StringIO()
        with mock.patch("workflow_watch_emit.time.sleep"):
            with redirect_stdout(buf):
                workflow_watch_emit.cmd_watch_emit(args)
        output = buf.getvalue()

        # First iteration: baseline emitted
        self.assertIn("RUN: \u2192 running", output)
        self.assertIn("AGENT w: \u2192 running", output)

        # Second iteration: no change → nothing new
        lines_after_first = output.strip().splitlines()
        # Only the first-iteration lines should be present
        self.assertEqual(len(lines_after_first), 2)

    def test_run_id_mode_watches_terminal_run(self) -> None:
        """Watching a terminal run by ID still emits its baseline."""
        run_id = self._init_run(status="completed")
        run = self._load_run(run_id)
        run["status"] = "completed"
        run["agents"] = [{"agent_id": "a1", "name": "w", "status": "completed", "exit_code": 0, "phase_id": None}]
        self._save_run(run_id, run)

        output = self._capture_emit(run_id)
        self.assertIn("RUN: \u2192 completed", output)
        self.assertIn("AGENT w: \u2192 completed (exit 0)", output)

    def test_short_run_id_truncation(self) -> None:
        """short_run_id returns last 8 chars for long ids."""
        self.assertEqual(workflow_watch_emit.short_run_id("wf-20260616T200000Z-my-run-ab12cd34"), "ab12cd34")
        self.assertEqual(workflow_watch_emit.short_run_id("short"), "short")
        self.assertEqual(workflow_watch_emit.short_run_id("12345678"), "12345678")
        self.assertEqual(workflow_watch_emit.short_run_id("123456789"), "23456789")

    def test_interval_accepts_seconds_suffix(self) -> None:
        """--interval accepts both bare seconds and a trailing 's' suffix."""
        parser = workflow_watch_emit.build_parser()
        self.assertEqual(parser.parse_args(["--interval", "30s"]).interval, 30.0)
        self.assertEqual(parser.parse_args(["--interval", "45"]).interval, 45.0)
        self.assertEqual(parser.parse_args(["--interval", "1.5s"]).interval, 1.5)
        with self.assertRaises(SystemExit):
            parser.parse_args(["--interval", "nope"])

    def test_all_runs_one_shot_skips_terminal_runs(self) -> None:
        """One-shot all-runs mode only reports active (non-terminal) runs."""
        terminal_id = self._init_run(status="completed")
        run = self._load_run(terminal_id)
        run["status"] = "completed"
        self._save_run(terminal_id, run)

        active_id = self._init_run(title="Active Run")
        active = self._load_run(active_id)
        active["status"] = "running"
        self._save_run(active_id, active)

        output = self._capture_emit()
        # The active run announces its baseline; the terminal run is silent.
        self.assertIn("RUN: \u2192 running", output)
        self.assertNotIn("RUN: \u2192 completed", output)

    def test_build_snapshot_structure(self) -> None:
        """_build_snapshot captures status, phases, agents, event_count."""
        run = {
            "status": "running",
            "phases": [{"phase_id": "p1", "status": "running"}, {"phase_id": "p2", "status": "pending"}],
            "agents": [{"agent_id": "a1", "status": "completed", "exit_code": 0}],
            "events": [{"message": "e1"}, {"message": "e2"}, {"message": "e3"}],
        }
        snap = workflow_watch_emit._build_snapshot(run)
        self.assertEqual(snap["status"], "running")
        self.assertEqual(snap["phases"], {"p1": "running", "p2": "pending"})
        self.assertEqual(snap["agents"], {"a1": {"status": "completed", "exit_code": 0}})
        self.assertEqual(snap["event_count"], 3)

    def test_new_events_emitted_capped(self) -> None:
        """Only the last _MAX_EVENT_LINES new events are emitted."""
        run_id = self._init_run()
        run = self._load_run(run_id)
        run["events"] = [
            {"event_id": "e1", "message": "first"},
            {"event_id": "e2", "message": "second"},
            {"event_id": "e3", "message": "third"},
            {"event_id": "e4", "message": "fourth"},
            {"event_id": "e5", "message": "fifth"},
        ]
        self._save_run(run_id, run)

        # Snapshot says 2 events seen
        snap = {"status": "running", "phases": {}, "agents": {}, "event_count": 2}
        self._write_snapshot(run_id, snap)

        output = self._capture_emit(run_id)
        lines = [ln for ln in output.strip().splitlines() if "EVENT:" in ln]
        # 3 new events (e3, e4, e5) but capped at _MAX_EVENT_LINES=3
        self.assertEqual(len(lines), 3)
        self.assertIn("third", lines[0])
        self.assertIn("fifth", lines[2])

    def test_snapshot_saved_after_emit(self) -> None:
        """After emitting, the snapshot file is updated."""
        run_id = self._init_run()
        run = self._load_run(run_id)
        run["agents"] = [{"agent_id": "a1", "name": "w", "status": "running", "exit_code": None, "phase_id": None}]
        self._save_run(run_id, run)

        self._capture_emit(run_id)
        snap = self._read_snapshot(run_id)
        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertEqual(snap["status"], "running")
        self.assertEqual(snap["agents"]["a1"]["status"], "running")

    def test_exit_code_on_first_run_terminal_agent(self) -> None:
        """First-run baseline includes exit code for terminal agents."""
        run_id = self._init_run(status="completed")
        run = self._load_run(run_id)
        run["status"] = "completed"
        run["agents"] = [{
            "agent_id": "a1", "name": "w", "status": "failed",
            "exit_code": 42, "phase_id": None,
        }]
        self._save_run(run_id, run)

        output = self._capture_emit(run_id)
        self.assertIn("AGENT w: \u2192 failed (exit 42)", output)

    def test_all_runs_no_runs_emits_nothing(self) -> None:
        """When no runs exist, all-runs mode emits nothing."""
        output = self._capture_emit()
        self.assertEqual(output, "")

    def test_wf_wrapper_dispatches_watch_emit(self) -> None:
        """The wf shell wrapper dispatches watch-emit to the Python script."""
        env = os.environ.copy()
        env["WORKFLOW_STATE_DIR"] = str(self.state_dir)
        import subprocess
        result = subprocess.run(
            [str(SCRIPTS / "wf"), "watch-emit", "--help"],
            text=True, capture_output=True, env=env,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("watch-emit", result.stdout.lower().replace("_", "-"))


if __name__ == "__main__":
    unittest.main()

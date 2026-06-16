#!/usr/bin/env python3
"""Tests for workflow_run_codex worker runner fixes."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import argparse
import asyncio
import os
import subprocess
import tempfile
from unittest import mock

import workflow_run_codex


def _init_git_repo(path: Path) -> None:
    """Initialize a git repo with an initial commit so HEAD exists."""
    subprocess.run(["git", "init"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(path), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path), capture_output=True, check=True,
    )
    seed = path / "seed.txt"
    seed.write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=str(path), capture_output=True, check=True,
    )


def _head_sha(path: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        text=True, capture_output=True, check=True,
    )
    return result.stdout.strip()


class TestCommitWorktreeChanges(unittest.TestCase):
    """Tests for _commit_worktree_changes helper."""

    def test_commits_uncommitted_changes(self):
        """When a worktree lane has uncommitted changes, they get committed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            wt_path = Path(tmpdir) / "worktree"
            wt_path.mkdir()
            _init_git_repo(wt_path)
            old_head = _head_sha(wt_path)

            # Create an uncommitted file
            new_file = wt_path / "new_work.txt"
            new_file.write_text("some work done\n", encoding="utf-8")

            agent = {
                "name": "test-agent",
                "worktree": {"path": str(wt_path), "branch": "feat/test"},
                "cwd": str(wt_path),
            }
            workflow_run_codex._commit_worktree_changes(agent)

            new_head = _head_sha(wt_path)
            self.assertNotEqual(old_head, new_head)

            log_result = subprocess.run(
                ["git", "-C", str(wt_path), "log", "--oneline", "-1"],
                text=True, capture_output=True, check=True,
            )
            self.assertIn("wip: test-agent implementation", log_result.stdout)
            self.assertIn("worktree changes committed", agent.get("summary", ""))

    def test_noop_when_clean(self):
        """When the worktree is clean, no commit is made and HEAD is unchanged."""
        with tempfile.TemporaryDirectory() as tmpdir:
            wt_path = Path(tmpdir) / "worktree"
            wt_path.mkdir()
            _init_git_repo(wt_path)
            old_head = _head_sha(wt_path)

            agent = {
                "name": "test-agent",
                "worktree": {"path": str(wt_path), "branch": "feat/test"},
                "cwd": str(wt_path),
            }
            workflow_run_codex._commit_worktree_changes(agent)

            new_head = _head_sha(wt_path)
            self.assertEqual(old_head, new_head)
            self.assertNotIn("worktree changes committed", agent.get("summary", ""))

    def test_noop_when_no_worktree_lane(self):
        """When the agent has no worktree key, no git calls are made at all."""
        agent = {"name": "plain-agent", "cwd": "/some/run/cwd"}

        with mock.patch("subprocess.run") as mock_run:
            workflow_run_codex._commit_worktree_changes(agent)
            mock_run.assert_not_called()

    def test_noop_when_worktree_path_missing(self):
        """When worktree.path is set but doesn't exist on disk, it's a no-op."""
        agent = {
            "name": "test-agent",
            "worktree": {"path": "/nonexistent/path/to/worktree"},
            "cwd": "/some/cwd",
        }

        with mock.patch("subprocess.run") as mock_run:
            workflow_run_codex._commit_worktree_changes(agent)
            mock_run.assert_not_called()


class TestAttachRunScoping(unittest.TestCase):
    """Tests for --attach-run skipping already-launched agents."""

    def test_attach_run_skips_started_agents(self):
        """Only agents with started_at=None should be enqueued on --attach-run."""
        processed_agents: list[str] = []

        async def fake_run_worker(run_id, agent, args, provider, semaphore, limiter):
            processed_agents.append(agent["agent_id"])
            # Mark as started so the test can observe it
            agent["started_at"] = "2026-01-01T00:00:00Z"

        completed_agent = {
            "agent_id": "worker-01-done",
            "name": "done-job",
            "role": "worker",
            "status": "completed",
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:01:00Z",
            "cwd": "/tmp",
            "worktree": {},
            "depends_on": "",
        }
        pending_agent = {
            "agent_id": "worker-02-new",
            "name": "new-job",
            "role": "worker",
            "status": "pending",
            "started_at": None,
            "cwd": "/tmp",
            "worktree": {},
            "depends_on": "",
        }

        run = {
            "run_id": "test-run-001",
            "status": "running",
            "agents": [completed_agent, pending_agent],
            "paths": {
                "artifacts_dir": "/tmp/artifacts",
                "logs_dir": "/tmp/logs",
            },
        }

        args = argparse.Namespace(
            max_agents=1,
            startup_delay=0.0,
            max_round=1,
            max_job=None,
            attach_run="test-run-001",
            dry_run=False,
            mock=False,
            quota_retries=0,
            failure_retries=0,
            quota_fail_fast=False,
            timeout_secs=None,
        )

        # Minimal mock provider
        provider = mock.MagicMock()
        provider.name = "test"
        provider.agent_type = "test-worker"

        with mock.patch.object(workflow_run_codex, "run_worker", fake_run_worker), \
             mock.patch.object(workflow_run_codex, "update_agent"), \
             mock.patch.object(workflow_run_codex, "_sweep_stale_workers", return_value=[]), \
             mock.patch.object(workflow_run_codex, "_record_unmet_dependencies"), \
             mock.patch.object(workflow_run_codex, "workflow_state") as mock_ws:
            # Make load_run return our run dict
            mock_ws.load_run.return_value = run
            mock_ws.mutate_run.side_effect = lambda run_id, fn: (None, fn(run) if fn else None, None)
            mock_ws.now.return_value = "2026-01-01T00:00:00Z"

            asyncio.run(workflow_run_codex.run_all(run, args, provider))

        # Only the pending agent (started_at=None) should have been processed
        self.assertIn("worker-02-new", processed_agents)
        self.assertNotIn("worker-01-done", processed_agents)

    def test_enqueue_all_when_no_attach_run(self):
        """Without --attach-run, all agents are enqueued regardless of started_at."""
        processed_agents: list[str] = []

        async def fake_run_worker(run_id, agent, args, provider, semaphore, limiter):
            processed_agents.append(agent["agent_id"])

        agent_a = {
            "agent_id": "worker-01",
            "name": "job-a",
            "role": "worker",
            "status": "completed",
            "started_at": "2026-01-01T00:00:00Z",
            "cwd": "/tmp",
            "worktree": {},
            "depends_on": "",
        }
        agent_b = {
            "agent_id": "worker-02",
            "name": "job-b",
            "role": "worker",
            "status": "pending",
            "started_at": None,
            "cwd": "/tmp",
            "worktree": {},
            "depends_on": "",
        }

        run = {
            "run_id": "test-run-002",
            "status": "running",
            "agents": [agent_a, agent_b],
            "paths": {
                "artifacts_dir": "/tmp/artifacts",
                "logs_dir": "/tmp/logs",
            },
        }

        args = argparse.Namespace(
            max_agents=1,
            startup_delay=0.0,
            max_round=1,
            max_job=None,
            attach_run=None,
            dry_run=False,
            mock=False,
            quota_retries=0,
            failure_retries=0,
            quota_fail_fast=False,
            timeout_secs=None,
        )

        provider = mock.MagicMock()
        provider.name = "test"
        provider.agent_type = "test-worker"

        with mock.patch.object(workflow_run_codex, "run_worker", fake_run_worker), \
             mock.patch.object(workflow_run_codex, "update_agent"), \
             mock.patch.object(workflow_run_codex, "_sweep_stale_workers", return_value=[]), \
             mock.patch.object(workflow_run_codex, "_record_unmet_dependencies"), \
             mock.patch.object(workflow_run_codex, "workflow_state") as mock_ws:
            mock_ws.load_run.return_value = run
            mock_ws.mutate_run.side_effect = lambda run_id, fn: (None, fn(run) if fn else None, None)
            mock_ws.now.return_value = "2026-01-01T00:00:00Z"

            asyncio.run(workflow_run_codex.run_all(run, args, provider))

        # Both agents should have been processed
        self.assertIn("worker-01", processed_agents)
        self.assertIn("worker-02", processed_agents)


if __name__ == "__main__":
    unittest.main()

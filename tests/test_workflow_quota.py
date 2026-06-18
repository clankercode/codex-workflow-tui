#!/usr/bin/env python3
"""Tests for quota fail-fast and default retry behaviour in workflow_run_codex."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("WORKFLOW_DETACH", "0")
os.environ.setdefault("WORKFLOW_NO_RESOLVE", "1")

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import workflow_run_codex  # noqa: E402
import workflow_state  # noqa: E402


# ---------------------------------------------------------------------------
# Pure-unit tests for helper functions
# ---------------------------------------------------------------------------


class TestQuotaLimitDetected(unittest.TestCase):
    """Verify the regex-based quota/rate-limit detection."""

    def test_detects_429(self) -> None:
        self.assertTrue(workflow_run_codex.quota_limit_detected("Error 429: Too Many Requests"))

    def test_detects_quota_word(self) -> None:
        self.assertTrue(workflow_run_codex.quota_limit_detected("You have exceeded your quota"))

    def test_detects_rate_limit(self) -> None:
        self.assertTrue(workflow_run_codex.quota_limit_detected("rate limit exceeded"))

    def test_detects_resource_exhausted(self) -> None:
        self.assertTrue(workflow_run_codex.quota_limit_detected("RESOURCE_EXHAUSTED"))

    def test_detects_limit_for_this_period(self) -> None:
        self.assertTrue(workflow_run_codex.quota_limit_detected("You reached the limit for this period"))

    def test_ignores_normal_output(self) -> None:
        self.assertFalse(workflow_run_codex.quota_limit_detected("Everything is fine"))

    def test_ignores_empty_strings(self) -> None:
        self.assertFalse(workflow_run_codex.quota_limit_detected("", ""))

    def test_checks_multiple_texts(self) -> None:
        self.assertTrue(workflow_run_codex.quota_limit_detected("ok", "quota error here"))

    def test_case_insensitive(self) -> None:
        self.assertTrue(workflow_run_codex.quota_limit_detected("QUOTA LIMIT HIT"))


class TestQuotaFailFastHelper(unittest.TestCase):
    """Verify the quota_fail_fast() decision helper."""

    def test_cli_flag_enables(self) -> None:
        args = argparse.Namespace(quota_fail_fast=True)
        self.assertTrue(workflow_run_codex.quota_fail_fast(args))

    def test_cli_flag_default_false(self) -> None:
        args = argparse.Namespace(quota_fail_fast=False)
        self.assertFalse(workflow_run_codex.quota_fail_fast(args))

    def test_env_var_enables(self) -> None:
        args = argparse.Namespace(quota_fail_fast=False)
        with mock.patch.dict(os.environ, {"WORKFLOW_QUOTA_FAIL_FAST": "1"}):
            self.assertTrue(workflow_run_codex.quota_fail_fast(args))

    def test_env_var_not_1_ignored(self) -> None:
        args = argparse.Namespace(quota_fail_fast=False)
        with mock.patch.dict(os.environ, {"WORKFLOW_QUOTA_FAIL_FAST": "0"}):
            self.assertFalse(workflow_run_codex.quota_fail_fast(args))

    def test_env_var_missing_uses_default(self) -> None:
        args = argparse.Namespace(quota_fail_fast=False)
        env = os.environ.copy()
        env.pop("WORKFLOW_QUOTA_FAIL_FAST", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(workflow_run_codex.quota_fail_fast(args))

    def test_missing_attr_defaults_false(self) -> None:
        args = argparse.Namespace()
        self.assertFalse(workflow_run_codex.quota_fail_fast(args))


# ---------------------------------------------------------------------------
# Integration tests exercising run_worker with mocked processes
# ---------------------------------------------------------------------------


def _make_args(**overrides: object) -> argparse.Namespace:
    """Build a minimal argparse.Namespace for run_worker."""
    defaults: dict[str, object] = {
        "cwd": "/tmp",
        "mock": False,
        "dry_run": False,
        "runner": "codex-direct",
        "max_agents": 1,
        "startup_delay": 0.0,
        "quota_retries": 2,
        "quota_fail_fast": False,
        "quota_retry_buffer_secs": 5.0,
        "failure_retries": 0,
        "timeout_secs": None,
        "model": None,
        "approval": "never",
        "sandbox": "read-only",
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class _FakeProcess:
    """Minimal fake asyncio subprocess for testing run_worker."""

    def __init__(self, stdout_bytes: bytes = b"", stderr_bytes: bytes = b"", exit_code: int = 0) -> None:
        self.stdout = _FakeStream(stdout_bytes)
        self.stderr = _FakeStream(stderr_bytes)
        self.stdin: _FakeStream | None = None
        self.pid = 999999
        self.returncode: int | None = None
        self._exit_code = exit_code

    async def wait(self) -> int:
        self.returncode = self._exit_code
        return self._exit_code


class _FakeStream:
    """Minimal fake asyncio stream."""

    def __init__(self, data: bytes = b"") -> None:
        self._data = data
        self._pos = 0

    async def read(self, n: int) -> bytes:
        chunk = self._data[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk

    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


def _setup_run(tmp_path: Path, env: dict[str, str]) -> tuple[str, str]:
    """Create a workflow run and agent, returning (run_id, agent_id).

    Assumes ``WORKFLOW_STATE_DIR`` is already set in ``os.environ``.
    """
    stdout_buf = io.StringIO()
    with contextlib.redirect_stdout(stdout_buf):
        workflow_state.cmd_init(
            argparse.Namespace(
                title="Quota Test",
                prompt="test quota",
                prompt_file=None,
                cwd=str(tmp_path),
                mode="codex-direct",
                tag=[],
                thread_id=None,
                coordinator_tool="codex-direct",
            )
        )
    created = json.loads(stdout_buf.getvalue())
    run_id = created["run_id"]

    with open(os.devnull, "w", encoding="utf-8") as sink, contextlib.redirect_stdout(sink):
        workflow_state.cmd_add_phase(
            argparse.Namespace(
                run=run_id,
                name="Test Phase",
                goal="test",
                phase_id=workflow_run_codex.PHASE_ID,
                status="running",
            )
        )

    agent_id = "codex-direct-01-test-job"
    run = workflow_state.load_run(run_id)
    artifacts = Path(run["paths"]["artifacts_dir"])
    logs = Path(run["paths"]["logs_dir"])
    output_path = artifacts / f"{agent_id}.final.md"
    prompt_path = artifacts / f"{agent_id}.prompt.md"
    prompt_path.write_text("test quota prompt\n", encoding="utf-8")
    output_path.write_text("", encoding="utf-8")

    with open(os.devnull, "w", encoding="utf-8") as sink, contextlib.redirect_stdout(sink):
        workflow_state.cmd_add_agent(
            argparse.Namespace(
                run=run_id,
                phase=workflow_run_codex.PHASE_ID,
                name="test-job",
                role="test-job",
                agent_type="codex-exec",
                agent_id=agent_id,
                status="pending",
                prompt=None,
                prompt_file=str(prompt_path),
                cwd=str(Path(run["paths"]["run_dir"])),
                model="",
                thread_id=None,
                process_id=None,
                write_scope=[],
                jsonl_path=str(logs / f"{agent_id}.jsonl"),
                log_path=str(logs / f"{agent_id}.stderr.log"),
                output_path=str(output_path),
            )
        )
    return run_id, agent_id


class TestRunWorkerQuotaFailFast(unittest.TestCase):
    """Test run_worker with quota_fail_fast enabled."""

    def test_quota_fail_fast_marks_agent_failed_immediately(self) -> None:
        """When fail-fast is on and quota error is detected, agent is failed without retry."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = str(tmp_path / "state")
            quota_stderr = b"Error 429: Too Many Requests\nRate limit exceeded\n"
            fake_proc = _FakeProcess(stderr_bytes=quota_stderr, exit_code=1)

            with mock.patch.dict(os.environ, {
                "WORKFLOW_STATE_DIR": state_dir,
                "WORKFLOW_QUOTA_RETRY_POLL_SECS": "0.01",
            }):
                run_id, agent_id = _setup_run(tmp_path, {})

                args = _make_args(cwd=str(tmp_path), quota_fail_fast=True, quota_retries=2)
                provider = workflow_run_codex.CodexDirectProvider()
                semaphore = asyncio.Semaphore(1)
                limiter = workflow_run_codex.StartupRateLimiter(0.0)

                async def exercise() -> None:
                    with mock.patch.object(limiter, "create_process", return_value=fake_proc):
                        run = workflow_state.load_run(run_id)
                        agent = workflow_state.find_item(run["agents"], "agent_id", agent_id)
                        await workflow_run_codex.run_worker(run_id, agent, args, provider, semaphore, limiter)

                asyncio.run(exercise())

                run = workflow_state.load_run(run_id)
                agent = workflow_state.find_item(run["agents"], "agent_id", agent_id)
                self.assertEqual(agent["status"], "failed")
                self.assertIn("fail-fast", agent["summary"])
                self.assertEqual(agent.get("quota_retry_count", 0), 0)

    def test_agent_execution_args_quota_fail_fast_overrides_run_defaults(self) -> None:
        """Per-job quota settings should be honored after execution_args rebuild."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = str(tmp_path / "state")
            quota_stderr = b"Error 429: Too Many Requests\n"
            fake_proc = _FakeProcess(stderr_bytes=quota_stderr, exit_code=1)

            with mock.patch.dict(os.environ, {
                "WORKFLOW_STATE_DIR": state_dir,
                "WORKFLOW_QUOTA_RETRY_POLL_SECS": "0.01",
                "WORKFLOW_QUOTA_RETRY_SLEEP_OVERRIDE_SECS": "0",
            }):
                run_id, agent_id = _setup_run(tmp_path, {})
                workflow_run_codex.update_agent(
                    run_id,
                    agent_id,
                    execution_args={
                        "quota_fail_fast": True,
                        "quota_retries": 0,
                        "quota_retry_buffer_secs": 0.0,
                        "failure_retries": 0,
                    },
                )

                args = _make_args(cwd=str(tmp_path), quota_fail_fast=False, quota_retries=2)
                provider = workflow_run_codex.CodexDirectProvider()
                semaphore = asyncio.Semaphore(1)
                limiter = workflow_run_codex.StartupRateLimiter(0.0)

                async def exercise() -> None:
                    with mock.patch.object(limiter, "create_process", return_value=fake_proc) as create_process:
                        run = workflow_state.load_run(run_id)
                        agent = workflow_state.find_item(run["agents"], "agent_id", agent_id)
                        await workflow_run_codex.run_worker(run_id, agent, args, provider, semaphore, limiter)
                        self.assertEqual(create_process.call_count, 1)

                asyncio.run(exercise())

                run = workflow_state.load_run(run_id)
                agent = workflow_state.find_item(run["agents"], "agent_id", agent_id)
                self.assertEqual(agent["status"], "failed")
                self.assertIn("fail-fast", agent["summary"])
                self.assertEqual(agent.get("quota_retry_count", 0), 0)


class TestRunWorkerQuotaRetryDefault(unittest.TestCase):
    """Test run_worker with default retry behaviour (quota_fail_fast disabled)."""

    def test_default_retry_retries_on_quota_error(self) -> None:
        """When fail-fast is off, quota errors trigger retry then eventually fail."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = str(tmp_path / "state")
            quota_stderr = b"Error 429: Too Many Requests\n"
            nonlocal_call_count = {"count": 0}

            async def fake_create_process(command, cwd=None):
                nonlocal_call_count["count"] += 1
                return _FakeProcess(stderr_bytes=quota_stderr, exit_code=1)

            with mock.patch.dict(os.environ, {
                "WORKFLOW_STATE_DIR": state_dir,
                "WORKFLOW_QUOTA_RETRY_POLL_SECS": "0.01",
                "WORKFLOW_QUOTA_RETRY_SLEEP_OVERRIDE_SECS": "0",
            }):
                run_id, agent_id = _setup_run(tmp_path, {})

                args = _make_args(cwd=str(tmp_path), quota_fail_fast=False, quota_retries=1)
                provider = workflow_run_codex.CodexDirectProvider()
                semaphore = asyncio.Semaphore(1)
                limiter = workflow_run_codex.StartupRateLimiter(0.0)

                async def exercise() -> None:
                    with mock.patch.object(limiter, "create_process", side_effect=fake_create_process):
                        run = workflow_state.load_run(run_id)
                        agent = workflow_state.find_item(run["agents"], "agent_id", agent_id)
                        await workflow_run_codex.run_worker(run_id, agent, args, provider, semaphore, limiter)

                asyncio.run(exercise())

                run = workflow_state.load_run(run_id)
                agent = workflow_state.find_item(run["agents"], "agent_id", agent_id)
                self.assertEqual(agent["status"], "failed")
                self.assertNotIn("fail-fast", agent.get("summary", ""))
                self.assertEqual(agent.get("quota_retry_count", 0), 1)
                self.assertEqual(nonlocal_call_count["count"], 2)


if __name__ == "__main__":
    unittest.main()

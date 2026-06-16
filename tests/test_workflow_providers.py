#!/usr/bin/env python3
"""Tests for workflow_run_providers — focused on PiDirectProvider."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import workflow_run_providers  # noqa: E402


def _make_args(**overrides: object) -> argparse.Namespace:
    """Return a minimal argparse.Namespace with pi-direct defaults."""
    defaults: dict[str, object] = {
        "runner": "pi-direct",
        "model": "",
        "cwd": "/tmp/work",
        "approval": "auto-edit",
        "sandbox": "danger-full-access",
        "cli_agent": "",
        "ccc_output_mode": "stream-json",
        "ccc_runner": "",
        "ccc_control": [],
        "permission_mode": "",
        "timeout_secs": 0,
        "kimi_max_steps_per_turn": 0,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _make_agent(**overrides: object) -> dict[str, object]:
    """Return a minimal agent dict with sensible defaults."""
    defaults: dict[str, object] = {
        "name": "test-agent",
        "prompt": "do the thing",
        "output_path": "/tmp/out.final.md",
        "jsonl_path": "/tmp/out.jsonl",
        "log_path": "/tmp/out.stderr.log",
    }
    defaults.update(overrides)
    return defaults  # type: ignore[return-value]


class TestPiDirectBuildCommand(unittest.TestCase):
    """build_command produces the expected argv for pi."""

    def test_basic_command_structure(self) -> None:
        provider = workflow_run_providers.PiDirectProvider()
        agent = _make_agent(prompt="fix the bug")
        args = _make_args()
        cmd = provider.build_command(agent, args)
        self.assertEqual(cmd[:5], ["pi", "-p", "--mode", "json", "--approve"])
        self.assertEqual(cmd[-1], "fix the bug")
        self.assertNotIn("--model", cmd)

    def test_model_flag_passed_through(self) -> None:
        provider = workflow_run_providers.PiDirectProvider()
        agent = _make_agent()
        args = _make_args(model="pi-large/pi-pro")
        cmd = provider.build_command(agent, args)
        idx = cmd.index("--model")
        self.assertEqual(cmd[idx + 1], "pi-large/pi-pro")
        self.assertEqual(cmd[-1], agent["prompt"])

    def test_prompt_is_last_argument(self) -> None:
        provider = workflow_run_providers.PiDirectProvider()
        agent = _make_agent(prompt="summarise all the things")
        args = _make_args(model="m1")
        cmd = provider.build_command(agent, args)
        self.assertEqual(cmd[-1], "summarise all the things")


class TestPiDirectExtractResult(unittest.TestCase):
    """extract_result handles JSON, plain text, and output-file fallback."""

    def _provider(self) -> workflow_run_providers.PiDirectProvider:
        return workflow_run_providers.PiDirectProvider()

    def test_parses_json_stdout_response_field(self) -> None:
        provider = self._provider()
        agent = _make_agent()
        stdout = json.dumps({"response": "all tests pass", "session_id": "ses_abc"})
        with tempfile.TemporaryDirectory() as tmp:
            agent["output_path"] = str(Path(tmp) / "out.md")
            result = provider.extract_result(agent, 0, stdout_text=stdout)
        self.assertEqual(result.result, "all tests pass")
        self.assertEqual(result.thread_id, "ses_abc")

    def test_parses_json_stdout_text_field(self) -> None:
        provider = self._provider()
        agent = _make_agent()
        stdout = json.dumps({"text": "from text field"})
        with tempfile.TemporaryDirectory() as tmp:
            agent["output_path"] = str(Path(tmp) / "out.md")
            result = provider.extract_result(agent, 0, stdout_text=stdout)
        self.assertEqual(result.result, "from text field")

    def test_parses_json_stdout_content_field(self) -> None:
        provider = self._provider()
        agent = _make_agent()
        stdout = json.dumps({"content": "from content field", "sessionId": "ses_xyz"})
        with tempfile.TemporaryDirectory() as tmp:
            agent["output_path"] = str(Path(tmp) / "out.md")
            result = provider.extract_result(agent, 0, stdout_text=stdout)
        self.assertEqual(result.result, "from content field")
        self.assertEqual(result.thread_id, "ses_xyz")

    def test_parses_json_stdout_string_value(self) -> None:
        provider = self._provider()
        agent = _make_agent()
        stdout = json.dumps("just a string reply")
        with tempfile.TemporaryDirectory() as tmp:
            agent["output_path"] = str(Path(tmp) / "out.md")
            result = provider.extract_result(agent, 0, stdout_text=stdout)
        self.assertEqual(result.result, "just a string reply")

    def test_falls_back_to_plain_text_stdout(self) -> None:
        provider = self._provider()
        agent = _make_agent()
        stdout = "not json at all, just plain text output"
        with tempfile.TemporaryDirectory() as tmp:
            agent["output_path"] = str(Path(tmp) / "out.md")
            result = provider.extract_result(agent, 0, stdout_text=stdout)
        self.assertEqual(result.result, "not json at all, just plain text output")

    def test_falls_back_to_output_file(self) -> None:
        provider = self._provider()
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "out.md"
            out_path.write_text("content from file\n", encoding="utf-8")
            agent = _make_agent(output_path=str(out_path))
            result = provider.extract_result(agent, 1, stdout_text="")
        self.assertEqual(result.result, "content from file\n")

    def test_empty_stdout_and_missing_file_yields_empty_result(self) -> None:
        provider = self._provider()
        agent = _make_agent(output_path="/tmp/does-not-exist-probe-xyz.md")
        result = provider.extract_result(agent, 2, stdout_text="")
        self.assertEqual(result.result, "")
        self.assertIn("exited 2", result.summary)

    def test_summary_uses_first_line(self) -> None:
        provider = self._provider()
        agent = _make_agent()
        stdout = json.dumps({"response": "first line\nsecond line\nthird"})
        with tempfile.TemporaryDirectory() as tmp:
            agent["output_path"] = str(Path(tmp) / "out.md")
            result = provider.extract_result(agent, 0, stdout_text=stdout)
        self.assertEqual(result.summary, "first line")

    def test_result_written_to_output_path(self) -> None:
        provider = self._provider()
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "out.md"
            agent = _make_agent(output_path=str(out_path))
            stdout = json.dumps({"response": "persist me"})
            provider.extract_result(agent, 0, stdout_text=stdout)
            self.assertEqual(out_path.read_text(encoding="utf-8"), "persist me")


class TestPiDirectProviderMetadata(unittest.TestCase):
    """Provider exposes expected name and agent_type."""

    def test_name_and_type(self) -> None:
        provider = workflow_run_providers.PiDirectProvider()
        self.assertEqual(provider.name, "pi-direct")
        self.assertEqual(provider.agent_type, "pi")


class TestBuildProviderFactory(unittest.TestCase):
    """build_provider returns PiDirectProvider for runner='pi-direct'."""

    def test_factory_returns_pi_direct(self) -> None:
        args = _make_args(runner="pi-direct")
        provider = workflow_run_providers.build_provider(args)
        self.assertIsInstance(provider, workflow_run_providers.PiDirectProvider)
        self.assertEqual(provider.name, "pi-direct")


if __name__ == "__main__":
    unittest.main()

"""Integration tests for ``wf merge-lanes`` automatic conflict resolution.

These tests are intentionally kept in a separate file from ``test_workflow.py``
(which a parallel job owns) to keep merge-back conflict-free.

Scope
-----
The existing ``test_merge_lanes_auto_resolve_dispatches_resolver_agent`` test
exercises the resolver with ``--leave-conflicts`` (conflict markers are left in
the tree when the resolver runs). The tests here cover the **default** path that
real users hit when they run ``wf merge-lanes`` with no special flags:

* ``_handle_merge_failure`` *aborts* the failed merge first, so the resolver
  agent runs against a clean tree and must re-create the conflict, resolve it,
  and let ``_attempt_post_resolve_merge`` re-merge to mark the lane merged.
* The resolver is a fake ``ccc`` binary on ``PATH`` (no real model calls) driven
  by the ``CCC_FAKE_MODE`` env var so one binary serves the success and failure
  scenarios.

Both happy path (resolver succeeds -> lane merged) and the failure edge case
(resolver exits non-zero -> merge aborted, lane stays conflicted) are covered,
plus a guard that ``WORKFLOW_NO_RESOLVE=1`` skips dispatch entirely.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

# The merger prompt is built by ``build_merger_prompt``; this header line is how
# the fake resolver detects which lane branch to re-merge (the default merge-lanes
# path aborts the merge before dispatching the resolver, so the tree is clean).
_BRANCH_MARKER = "- Branch: `"


# The fake ``ccc`` resolver binary. Mode is selected at runtime via
# ``CCC_FAKE_MODE`` so a single installed binary serves every scenario:
#   * "resolve" (default): re-run the lane merge, combine both sides of each
#     conflict marker, commit, exit 0.
#   * "fail": record the invocation then exit non-zero.
#   * "noop": record the invocation, exit 0, touch nothing (tree stays clean).
_RESOLVER_SCRIPT = r'''#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from pathlib import Path

MODE = os.environ.get("CCC_FAKE_MODE", "resolve")
args_path = os.environ.get("CCC_ARGS_PATH")
if args_path:
    Path(args_path).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")


def combine_markers(text):
    """Collapse git conflict markers, preserving BOTH sides in order.

    Handles both the default 2-way style and the diff3 style (which inserts a
    `|||||||` base section between ours and theirs); the base section is dropped.
    """
    out = []
    state = "normal"
    ours = []
    theirs = []
    for line in text.splitlines():
        if line.startswith("<<<<<<<"):
            state = "ours"
            continue
        if line.startswith("|||||||"):
            state = "base"
            continue
        if line.startswith("======="):
            state = "theirs"
            continue
        if line.startswith(">>>>>>>"):
            out.extend(ours)
            out.extend(theirs)
            ours = []
            theirs = []
            state = "normal"
            continue
        if state == "ours":
            ours.append(line)
        elif state == "theirs":
            theirs.append(line)
        elif state == "normal":
            out.append(line)
        # state == "base": discard the common-ancestor lines.
    out.extend(ours)
    out.extend(theirs)
    return "\n".join(out) + "\n"


def parse_branch(prompt):
    """Pull the lane branch out of the merger prompt's context header."""
    idx = prompt.find("- Branch: `")
    if idx < 0:
        return ""
    start = idx + len("- Branch: `")
    end = prompt.find("`", start)
    return prompt[start:end] if end >= 0 else ""


if MODE == "fail":
    sys.stderr.write("fake ccc resolver: simulated failure\n")
    sys.exit(1)

if MODE == "noop":
    sys.exit(0)

# MODE == "resolve": actually resolve the conflicted lane.
# The default merge-lanes path aborts the failed merge before dispatching us,
# so the tree is clean here -- re-create the conflict by re-merging the lane
# branch parsed from the prompt, then combine both sides and commit.
prompt = sys.argv[-1] if sys.argv else ""
branch = parse_branch(prompt) or os.environ.get("CCC_FAKE_BRANCH", "")
if not branch:
    sys.stderr.write("fake ccc resolver: could not determine branch\n")
    sys.exit(2)

merge = subprocess.run(["git", "merge", "--no-edit", branch], capture_output=True, text=True)
if merge.returncode != 0:
    status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
    for line in status.stdout.strip().splitlines():
        prefix = line[:2]
        path = line[3:]
        if prefix in ("UU", "AA", "DD", "AU", "UA", "DU", "UD") and path:
            target = Path(path)
            target.write_text(combine_markers(target.read_text(encoding="utf-8")), encoding="utf-8")
            subprocess.run(["git", "add", path], check=True)
    commit = subprocess.run(["git", "commit", "--no-edit"], capture_output=True, text=True)
    if commit.returncode != 0:
        sys.stderr.write("fake ccc resolver: commit failed: " + commit.stderr + "\n")
        sys.exit(commit.returncode)
sys.exit(0)
'''


class MergeResolveIntegrationTests(unittest.TestCase):
    """End-to-end ``wf merge-lanes`` auto-resolve behavior via a fake resolver."""

    # ------------------------------------------------------------------
    # helpers (kept local so this file does not import test_workflow.py,
    # which sets WORKFLOW_NO_RESOLVE=1 at import time)
    # ------------------------------------------------------------------
    def run_wf(self, *args: str, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        command = [str(SCRIPTS / "wf"), *args]
        return subprocess.run(command, check=check, text=True, capture_output=True, env=env)

    def git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        command = [
            "git", "-C", str(cwd),
            "-c", "user.name=Workflow Test",
            "-c", "user.email=workflow-test@example.invalid",
            *args,
        ]
        return subprocess.run(command, check=True, text=True, capture_output=True)

    def _init_repo_with_conflicting_lanes(self, tmp_path: Path) -> Path:
        """Create a repo on ``main`` plus two lane branches that both edit shared.txt.

        ``main`` stays at ``base``; lane-1 sets the file to ``lane-1`` and lane-2
        sets it to ``lane-2`` (both from the same base). merge-lanes will
        fast-forward lane-1 cleanly, then conflict on lane-2 -- exercising the
        auto-resolve path.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, text=True, capture_output=True)
        # Hermetic identity so the resolver's own commits succeed without relying
        # on the developer's global git config.
        self.git(repo, "config", "user.name", "Workflow Test")
        self.git(repo, "config", "user.email", "workflow-test@example.invalid")
        (repo / "shared.txt").write_text("base\n", encoding="utf-8")
        self.git(repo, "add", "shared.txt")
        self.git(repo, "commit", "-m", "base")
        base = self.git(repo, "rev-parse", "HEAD").stdout.strip()

        self.git(repo, "checkout", "-b", "workflow/lane-1", base)
        (repo / "shared.txt").write_text("lane-1\n", encoding="utf-8")
        self.git(repo, "commit", "-am", "lane-1 change")

        self.git(repo, "checkout", "-b", "workflow/lane-2", base)
        (repo / "shared.txt").write_text("lane-2\n", encoding="utf-8")
        self.git(repo, "commit", "-am", "lane-2 change")

        self.git(repo, "checkout", "main")
        return repo

    def _make_run_with_lanes(self, repo: Path, tmp_path: Path, env: dict[str, str]) -> tuple[str, Path]:
        """init a run, add a completed phase, and seed two completed lane agents."""
        created = self.run_wf("init", "--title", "Merge Resolve", "--prompt", "resolve", "--cwd", str(repo), env=env)
        created_data = json.loads(created.stdout)
        run_id = created_data["run_id"]
        run_path = Path(created_data["path"])
        self.run_wf(
            "add-phase", run_id, "--phase-id", "phase-impl", "--name", "Implementation",
            "--status", "completed", env=env,
        )
        for agent_id, name in [("agent-lane-1", "lane-1"), ("agent-lane-2", "lane-2")]:
            self.run_wf(
                "add-agent", run_id, "--phase", "phase-impl",
                "--agent-id", agent_id, "--name", name,
                "--agent-type", "codex-exec", "--status", "completed",
                "--cwd", str(repo), env=env,
            )
        run = json.loads(run_path.read_text(encoding="utf-8"))
        run["agents"][0]["worktree"] = {"branch": "workflow/lane-1", "path": str(tmp_path / "lane1"), "merge_target": "main"}
        run["agents"][1]["worktree"] = {"branch": "workflow/lane-2", "path": str(tmp_path / "lane2"), "merge_target": "main"}
        run_path.write_text(json.dumps(run, indent=2), encoding="utf-8")
        return run_id, run_path

    def _install_resolver_ccc(self, fake_bin: Path) -> Path:
        """Install the mode-driven fake ``ccc`` binary and return its path."""
        fake_bin.mkdir(parents=True, exist_ok=True)
        fake_ccc = fake_bin / "ccc"
        fake_ccc.write_text(_RESOLVER_SCRIPT, encoding="utf-8")
        fake_ccc.chmod(0o755)
        return fake_ccc

    def _base_env(self, tmp_path: Path, fake_bin: Path, ccc_args_path: Path) -> dict[str, str]:
        """Build a subprocess env with the fake resolver on PATH and resolve enabled."""
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}:{env['PATH']}"
        env["WORKFLOW_STATE_DIR"] = str(tmp_path / "state")
        env["CCC_ARGS_PATH"] = str(ccc_args_path)
        # Critical: run the REAL auto-resolve path (do not skip dispatch).
        env.pop("WORKFLOW_NO_RESOLVE", None)
        return env

    @staticmethod
    def _agent(run: dict[str, Any], agent_id: str) -> dict[str, Any]:
        return next(a for a in run["agents"] if a["agent_id"] == agent_id)

    # ------------------------------------------------------------------
    # tests
    # ------------------------------------------------------------------
    def test_auto_resolve_succeeds_without_no_resolve_flag(self) -> None:
        """Default merge-lanes dispatches the resolver, which fully resolves and marks the lane merged."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = self._init_repo_with_conflicting_lanes(tmp_path)
            fake_bin = tmp_path / "bin"
            self._install_resolver_ccc(fake_bin)
            ccc_args_path = tmp_path / "ccc-args.json"
            env = self._base_env(tmp_path, fake_bin, ccc_args_path)
            env["CCC_FAKE_MODE"] = "resolve"
            run_id, run_path = self._make_run_with_lanes(repo, tmp_path, env)

            result = self.run_wf("merge-lanes", run_id, env=env)

            # merge-lanes exits 0 once the conflict is auto-resolved.
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["merged"], ["agent-lane-1", "agent-lane-2"])
            self.assertEqual(payload["conflicts"], [])

            run_after = json.loads(run_path.read_text(encoding="utf-8"))

            # Resolver combined both sides of the conflict.
            self.assertEqual((repo / "shared.txt").read_text(encoding="utf-8"), "lane-1\nlane-2\n")
            # Target checkout is back on main with a clean tree.
            self.assertEqual(self.git(repo, "branch", "--show-current").stdout.strip(), "main")
            self.assertEqual(self.git(repo, "status", "--porcelain").stdout.strip(), "")

            # lane-1 merged cleanly (fast-forward); lane-2 merged via auto-resolve.
            lane1 = self._agent(run_after, "agent-lane-1")
            lane2 = self._agent(run_after, "agent-lane-2")
            self.assertEqual(lane1["worktree"]["merge_status"], "merged")
            self.assertEqual(lane2["worktree"]["merge_status"], "merged")
            self.assertTrue(lane2["worktree"]["merged_at"])
            self.assertTrue(lane2["worktree"]["merge_commit"])

            # A conflict was recorded, then resolved.
            self.assertTrue(any(
                e.get("operation") == "merge-conflicted" and e.get("agent_id") == "agent-lane-2"
                for e in run_after["events"]
            ))
            self.assertTrue(any(
                e.get("operation") == "merge-resolved" and e.get("agent_id") == "agent-lane-2"
                for e in run_after["events"]
            ))
            self.assertTrue(any(
                c.get("kind") == "merge" and c.get("status") == "passed"
                and "merge lane agent-lane-2" in c.get("name", "")
                for c in run_after["checks"]
            ))

            # The resolver was dispatched with the expected ccc invocation + prompt.
            self.assertTrue(ccc_args_path.exists(), "resolver was never dispatched")
            ccc_args = json.loads(ccc_args_path.read_text(encoding="utf-8"))
            self.assertIn("--yolo", ccc_args)
            self.assertTrue(ccc_args, "ccc must receive arguments")
            prompt = ccc_args[-1]
            self.assertIn("Merge Conflict Resolution", prompt)
            self.assertIn(f"{_BRANCH_MARKER}workflow/lane-2`", prompt)

    def test_auto_resolve_failure_aborts_and_keeps_lane_conflicted(self) -> None:
        """When the resolver exits non-zero, the merge is aborted and the lane stays conflicted."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = self._init_repo_with_conflicting_lanes(tmp_path)
            fake_bin = tmp_path / "bin"
            self._install_resolver_ccc(fake_bin)
            ccc_args_path = tmp_path / "ccc-args.json"
            env = self._base_env(tmp_path, fake_bin, ccc_args_path)
            env["CCC_FAKE_MODE"] = "fail"
            run_id, run_path = self._make_run_with_lanes(repo, tmp_path, env)

            result = self.run_wf("merge-lanes", run_id, env=env, check=False)

            # Resolver failed -> merge-lanes reports the conflict and exits non-zero.
            self.assertNotEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["merged"], ["agent-lane-1"])
            conflict_agent_ids = [c.get("agent_id") for c in payload["conflicts"]]
            self.assertIn("agent-lane-2", conflict_agent_ids)

            run_after = json.loads(run_path.read_text(encoding="utf-8"))

            # The conflicted merge was aborted: target is clean, back on main, with
            # lane-1's already-merged content (lane-2's change is gone).
            self.assertEqual(self.git(repo, "status", "--porcelain").stdout.strip(), "")
            self.assertEqual(self.git(repo, "branch", "--show-current").stdout.strip(), "main")
            self.assertEqual((repo / "shared.txt").read_text(encoding="utf-8"), "lane-1\n")

            lane1 = self._agent(run_after, "agent-lane-1")
            lane2 = self._agent(run_after, "agent-lane-2")
            self.assertEqual(lane1["worktree"]["merge_status"], "merged")
            self.assertEqual(lane2["worktree"]["merge_status"], "conflicted")
            self.assertTrue(lane2["worktree"]["merge_check_id"])
            self.assertNotIn("merged_at", lane2["worktree"])

            # A failed merge check and conflicted event were recorded; no resolution.
            self.assertTrue(any(
                c.get("kind") == "merge" and c.get("status") == "failed"
                and "merge lane agent-lane-2" in c.get("name", "")
                for c in run_after["checks"]
            ))
            self.assertTrue(any(
                e.get("operation") == "merge-conflicted" and e.get("agent_id") == "agent-lane-2"
                for e in run_after["events"]
            ))
            self.assertFalse(any(
                e.get("operation") == "merge-resolved" for e in run_after["events"]
            ))

            # The resolver WAS dispatched (then failed) -- proving the failure path.
            self.assertTrue(ccc_args_path.exists(), "resolver was never dispatched")
            ccc_args = json.loads(ccc_args_path.read_text(encoding="utf-8"))
            self.assertIn("--yolo", ccc_args)

    def test_workflow_no_resolve_env_skips_resolver_dispatch(self) -> None:
        """WORKFLOW_NO_RESOLVE=1 must skip resolver dispatch entirely; the lane stays conflicted."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = self._init_repo_with_conflicting_lanes(tmp_path)
            fake_bin = tmp_path / "bin"
            self._install_resolver_ccc(fake_bin)
            ccc_args_path = tmp_path / "ccc-args.json"
            env = self._base_env(tmp_path, fake_bin, ccc_args_path)
            # Explicitly opt out of auto-resolve via the documented env var.
            env["WORKFLOW_NO_RESOLVE"] = "1"
            run_id, run_path = self._make_run_with_lanes(repo, tmp_path, env)

            result = self.run_wf("merge-lanes", run_id, env=env, check=False)

            self.assertNotEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["merged"], ["agent-lane-1"])
            self.assertIn("agent-lane-2", [c.get("agent_id") for c in payload["conflicts"]])

            # Resolver was never invoked.
            self.assertFalse(ccc_args_path.exists(), "resolver should not be dispatched under WORKFLOW_NO_RESOLVE=1")

            run_after = json.loads(run_path.read_text(encoding="utf-8"))
            lane2 = self._agent(run_after, "agent-lane-2")
            self.assertEqual(lane2["worktree"]["merge_status"], "conflicted")
            self.assertFalse(any(e.get("operation") == "merge-resolved" for e in run_after["events"]))


if __name__ == "__main__":
    unittest.main()

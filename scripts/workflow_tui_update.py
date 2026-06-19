"""Git update checking, skill pulling, workflow control, and attention toasts for the TUI."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import workflow_state

UPDATE_CHECK_TIMEOUT = 2.0
UPDATE_PULL_TIMEOUT = 30.0
UPDATE_CHECK_INTERVAL = 15 * 60.0


@dataclass(frozen=True)
class GitCommandResult:
    """Bounded git subprocess result for update checks and actions."""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def summary(self) -> str:
        raw = "\n".join(part for part in (self.stderr, self.stdout) if part).strip()
        if len(raw) <= 400:
            return raw
        return "\u2026" + raw[-399:]


@dataclass(frozen=True)
class UpdateStatus:
    """Current git-update status for the workflow skill checkout."""

    state: str
    message: str
    local_head: str = ""
    remote_head: str = ""
    upstream: str = ""


@dataclass(frozen=True)
class UpdateActionResult:
    """Result of an attempted git pull update."""

    success: bool
    message: str
    status: UpdateStatus


@dataclass(frozen=True)
class WorkflowControlResult:
    """Result of a pause, resume, or stop action from the live TUI."""

    action: str
    success: bool
    message: str


def skill_repo_root() -> Path:
    """Return the workflow skill checkout that contains this TUI script."""
    return Path(__file__).resolve().parents[1]


def text_from_process(value: Any) -> str:
    """Decode subprocess output from either text or timeout paths."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def run_git_command(repo_root: Path, args: list[str], timeout: float) -> GitCommandResult:
    """Run a bounded git command in the skill checkout."""
    command = ["git", "-C", str(repo_root), *args]
    try:
        result = subprocess.run(command, check=False, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return GitCommandResult(tuple(args), 124, text_from_process(exc.stdout).strip(), text_from_process(exc.stderr).strip(), True)
    except OSError as exc:
        return GitCommandResult(tuple(args), 127, "", str(exc))
    return GitCommandResult(tuple(args), result.returncode, result.stdout.strip(), result.stderr.strip())


def unavailable_update(message: str) -> UpdateStatus:
    """Return a status for checkouts that cannot be checked safely."""
    return UpdateStatus("unavailable", message)


def parse_ls_remote_head(output: str) -> str:
    """Extract the first object id from git ls-remote output."""
    for line in output.splitlines():
        parts = line.split()
        if parts:
            return parts[0]
    return ""


def short_head(value: str) -> str:
    """Return a compact commit id for update notifications."""
    return value[:12] if value else "unknown"


def check_skill_update(repo_root: Path | None = None, timeout: float = UPDATE_CHECK_TIMEOUT) -> UpdateStatus:
    """Check whether the workflow skill checkout differs from its upstream head."""
    root = (repo_root or skill_repo_root()).expanduser().resolve()
    inside = run_git_command(root, ["rev-parse", "--is-inside-work-tree"], timeout)
    if not inside.ok or inside.stdout != "true":
        return unavailable_update("Workflow skill directory is not a git checkout.")

    branch = run_git_command(root, ["branch", "--show-current"], timeout)
    if not branch.ok or not branch.stdout:
        return unavailable_update("Workflow skill checkout is detached; no upstream branch can be checked.")

    remote = run_git_command(root, ["config", f"branch.{branch.stdout}.remote"], timeout)
    merge = run_git_command(root, ["config", f"branch.{branch.stdout}.merge"], timeout)
    if not remote.ok or not remote.stdout or not merge.ok or not merge.stdout:
        return unavailable_update("Workflow skill checkout has no upstream branch configured.")

    local = run_git_command(root, ["rev-parse", "HEAD"], timeout)
    if not local.ok or not local.stdout:
        return unavailable_update("Could not read the local workflow skill HEAD.")

    upstream = f"{remote.stdout}/{merge.stdout.removeprefix('refs/heads/')}"
    remote_head = run_git_command(root, ["ls-remote", "--heads", remote.stdout, merge.stdout], timeout)
    if remote_head.timed_out:
        return UpdateStatus("unavailable", f"Timed out checking {upstream}.", local_head=local.stdout, upstream=upstream)
    if not remote_head.ok:
        detail = remote_head.summary()
        suffix = f": {detail}" if detail else "."
        return UpdateStatus("unavailable", f"Could not check {upstream}{suffix}", local_head=local.stdout, upstream=upstream)

    remote_sha = parse_ls_remote_head(remote_head.stdout)
    if not remote_sha:
        return UpdateStatus("unavailable", f"Could not resolve upstream branch {upstream}.", local_head=local.stdout, upstream=upstream)
    if remote_sha == local.stdout:
        return UpdateStatus("current", f"Workflow skill is current at {short_head(local.stdout)}.", local_head=local.stdout, remote_head=remote_sha, upstream=upstream)
    message = f"Workflow skill update available: {short_head(local.stdout)} -> {short_head(remote_sha)} from {upstream}."
    return UpdateStatus("available", message, local_head=local.stdout, remote_head=remote_sha, upstream=upstream)


def update_skill_from_git(
    repo_root: Path | None = None,
    timeout: float = UPDATE_PULL_TIMEOUT,
    check_timeout: float = UPDATE_CHECK_TIMEOUT,
) -> UpdateActionResult:
    """Run git pull --ff-only in the workflow skill checkout, then re-check status."""
    root = (repo_root or skill_repo_root()).expanduser().resolve()
    pull = run_git_command(root, ["pull", "--ff-only"], timeout)
    status = check_skill_update(root, timeout=check_timeout)
    if not pull.ok:
        detail = pull.summary()
        message = "git pull --ff-only timed out." if pull.timed_out else "git pull --ff-only failed."
        if detail:
            message = f"{message} {detail}"
        return UpdateActionResult(False, message, status)
    if status.state == "current":
        return UpdateActionResult(True, f"Workflow skill updated to {short_head(status.local_head)}.", status)
    return UpdateActionResult(True, f"git pull --ff-only completed. {status.message}", status)


def workflow_control_action(run_id: str, action: str, reason: str = "TUI command palette") -> WorkflowControlResult:
    """Apply a workflow control action through the durable state layer."""
    commands = {
        "pause": workflow_state.cmd_pause,
        "resume": workflow_state.cmd_resume,
        "stop": workflow_state.cmd_stop,
    }
    if action not in commands:
        return WorkflowControlResult(action, False, f"Unknown workflow action: {action}")
    if not run_id:
        return WorkflowControlResult(action, False, "No workflow run is selected.")
    args = argparse.Namespace(run=run_id, reason=reason, terminate=True)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            commands[action](args)
    except (OSError, SystemExit, KeyError, json.JSONDecodeError) as exc:
        return WorkflowControlResult(action, False, f"{action.title()} failed: {exc}")
    verb = {"pause": "paused", "resume": "resumed", "stop": "stopped"}[action]
    return WorkflowControlResult(action, True, f"Workflow {run_id} {verb}.")


# ---------------------------------------------------------------------------
# Attention toast notifications
# ---------------------------------------------------------------------------
#
# The attention tab lists health items.  When a brand-new item
# appears we surface a compact toast at the top of the TUI for a few seconds
# and flag the row as unread in the sidebar.  State lives here so the live TUI
# (one long-running process, many refreshes) can remember what it has already
# announced.  The clock is sourced from ``workflow_tui_render.snapshot_reference_time``
# (env-driven via WORKFLOW_TUI_SNAPSHOT_NOW) so tests can drive it deterministically.

ATTENTION_SEEN_AGE_SECONDS = 15.0  # item counts as "seen"/read once this old
ATTENTION_TOAST_TTL_SECONDS = 7.5  # an active toast stays visible this long
ATTENTION_TOAST_HEIGHT = 3  # border + single content line + border

# item_key -> epoch seconds the TUI first observed the item.
_ATTENTION_FIRST_SEEN: dict[str, float] = {}
# Active toast payload: {"items": [...], "shown_at": epoch} or None.
_ACTIVE_ATTENTION_TOAST: dict[str, Any] | None = None


def _toast_reference_epoch() -> float:
    """Return the reference clock as epoch seconds (env-driven for tests)."""
    from workflow_tui_render import snapshot_reference_time  # lazy: avoid import cycle

    reference = snapshot_reference_time()
    return datetime.now().timestamp() if reference is None else reference.timestamp()


def _attention_item_epoch(item: dict[str, Any]) -> float | None:
    """Return the epoch an attention item arose, or None when its ts is unknown."""
    from workflow_tui_render import parse_local_datetime  # lazy: avoid import cycle

    parsed = parse_local_datetime(item.get("ts"))
    return None if parsed is None else parsed.timestamp()


def _attention_item_key(item: dict[str, Any], index: int) -> str:
    """Stable key for an attention item (mirrors item_key for the attention tab)."""
    return str(item.get("attention_id") or f"item-{index}")


def reset_attention_toast_state() -> None:
    """Clear toast tracking (fresh TUI sessions and tests)."""
    global _ACTIVE_ATTENTION_TOAST
    _ATTENTION_FIRST_SEEN.clear()
    _ACTIVE_ATTENTION_TOAST = None


def attention_unread_keys(items: list[dict[str, Any]]) -> set[str]:
    """Return keys for items still fresh enough to count as unread."""
    if not items:
        return set()
    now_epoch = _toast_reference_epoch()
    unread: set[str] = set()
    for index, item in enumerate(items):
        item_epoch = _attention_item_epoch(item)
        if item_epoch is not None and now_epoch - item_epoch < ATTENTION_SEEN_AGE_SECONDS:
            unread.add(_attention_item_key(item, index))
    return unread


def refresh_attention_toasts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Record first-seen timestamps and return brand-new items to toast.

    An item toasts when it is fresh (younger than ``ATTENTION_SEEN_AGE_SECONDS``)
    AND has not been observed in a previous refresh.  The new items also become
    the active toast, which ``active_attention_toast_items`` keeps visible for
    ``ATTENTION_TOAST_TTL_SECONDS``.
    """
    global _ACTIVE_ATTENTION_TOAST
    now_epoch = _toast_reference_epoch()
    current_keys = {_attention_item_key(item, index) for index, item in enumerate(items)}
    for stale in [key for key in _ATTENTION_FIRST_SEEN if key not in current_keys]:
        _ATTENTION_FIRST_SEEN.pop(stale, None)

    new_items: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        key = _attention_item_key(item, index)
        if key in _ATTENTION_FIRST_SEEN:
            continue  # observed in a prior refresh -> no re-toast
        item_epoch = _attention_item_epoch(item)
        age = None if item_epoch is None else now_epoch - item_epoch
        _ATTENTION_FIRST_SEEN[key] = item_epoch if item_epoch is not None else now_epoch
        if age is None or age < ATTENTION_SEEN_AGE_SECONDS:
            new_items.append(item)
    if new_items:
        _ACTIVE_ATTENTION_TOAST = {"items": new_items, "shown_at": now_epoch}
    return new_items


def active_attention_toast_items() -> list[dict[str, Any]] | None:
    """Return items for the currently visible toast, or None once it expires."""
    global _ACTIVE_ATTENTION_TOAST
    if not _ACTIVE_ATTENTION_TOAST:
        return None
    now_epoch = _toast_reference_epoch()
    if now_epoch - _ACTIVE_ATTENTION_TOAST["shown_at"] >= ATTENTION_TOAST_TTL_SECONDS:
        _ACTIVE_ATTENTION_TOAST = None
        return None
    return _ACTIVE_ATTENTION_TOAST.get("items")


def build_attention_toast_panel(width: int = 100) -> Any:
    """Return a compact single-line Rich panel for the active toast, or None."""
    from rich import box
    from rich.panel import Panel
    from rich.text import Text

    items = active_attention_toast_items()
    if not items:
        return None
    labels = [str(item.get("title") or item.get("kind") or "attention") for item in items]
    body = Text("  ".join(f"\u25cf {label}" for label in labels), no_wrap=True, overflow="ellipsis")
    count = "" if len(items) == 1 else f" ({len(items)})"
    return Panel(
        body,
        title=f"New attention{count}",
        title_align="left",
        border_style="dim yellow",
        box=box.ROUNDED,
        width=width,
        expand=True,
        padding=(0, 1),
    )

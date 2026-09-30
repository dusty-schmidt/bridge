"""Per-session git branch + auto-commit, isolated to a branch Bridge owns.

``bridge launch`` (top-level sessions only, never a child) checks out
``bridge/<agent_id>`` off whatever branch was current, and commits any
uncommitted changes to it when the session ends. Bridge never merges,
pushes, or touches the branch you were already on — only its own branch.
Failures here are swallowed: a git hiccup must not block a launch.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=30)


def is_repo(path: Path) -> bool:
    try:
        r = _run(["git", "rev-parse", "--is-inside-work-tree"], path)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and r.stdout.strip() == "true"


def current_branch(path: Path) -> str | None:
    try:
        r = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], path)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def create_session_branch(path: Path, branch: str) -> bool:
    """Create and check out ``branch`` from HEAD. Returns whether it switched."""
    try:
        r = _run(["git", "checkout", "-b", branch], path)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def has_changes(path: Path) -> bool:
    try:
        r = _run(["git", "status", "--porcelain"], path)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


def commit_all(path: Path, message: str) -> bool:
    """Stage and commit everything on the current branch. Returns whether it committed."""
    try:
        _run(["git", "add", "-A"], path)
        r = _run(["git", "commit", "-m", message], path)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0

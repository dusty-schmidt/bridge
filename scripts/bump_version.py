#!/usr/bin/env python3
"""Bump the version in pyproject.toml, promote CHANGELOG's [Unreleased], tag.

Usage: scripts/bump_version.py [major|minor|patch]  (default: patch)
"""
from __future__ import annotations

import datetime
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
CHANGELOG = ROOT / "CHANGELOG.md"


def bump(version: str, part: str) -> str:
    major, minor, patch = (int(x) for x in version.split("."))
    if part == "major":
        major, minor, patch = major + 1, 0, 0
    elif part == "minor":
        minor, patch = minor + 1, 0
    elif part == "patch":
        patch += 1
    else:
        raise SystemExit(f"unknown part: {part}")
    return f"{major}.{minor}.{patch}"


def main() -> None:
    part = sys.argv[1] if len(sys.argv) > 1 else "patch"
    if part not in ("major", "minor", "patch"):
        raise SystemExit(f"usage: {sys.argv[0]} [major|minor|patch]")

    text = PYPROJECT.read_text()
    match = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
    if not match:
        raise SystemExit("could not find version in pyproject.toml")
    old = match.group(1)
    new = bump(old, part)
    PYPROJECT.write_text(
        text[: match.start(1)] + new + text[match.end(1) :]
    )

    log = CHANGELOG.read_text()
    if "## [Unreleased]" not in log:
        raise SystemExit("CHANGELOG.md has no [Unreleased] section to promote")
    today = datetime.date.today().isoformat()
    log = log.replace(
        "## [Unreleased]",
        f"## [Unreleased]\n\n## [{new}] - {today}",
        1,
    )
    CHANGELOG.write_text(log)

    subprocess.run(["git", "add", str(PYPROJECT), str(CHANGELOG)], cwd=ROOT, check=True)
    subprocess.run(
        ["git", "commit", "-m", f"Release {new}"], cwd=ROOT, check=True
    )
    subprocess.run(["git", "tag", f"v{new}"], cwd=ROOT, check=True)
    print(f"{old} -> {new} (tagged v{new})")


if __name__ == "__main__":
    main()

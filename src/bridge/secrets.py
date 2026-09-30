"""Auto-loaded secrets: one mode-600 ``KEY=VALUE`` file, no manual ``export``.

Bridge reads ``secrets_file()`` (``$BRIDGE_STATE_DIR/secrets.env``, ``~/.bridge/secrets.env``
by default) once at CLI startup and sets each key in the process environment,
without overwriting a variable the caller already exported. Values referenced
from a profile as ``${ENV_VAR}`` (see :mod:`bridge.profile`) resolve from here
with nothing else to type or source by hand.
"""

from __future__ import annotations

from pathlib import Path

from . import paths


def parse(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines. Blank lines and ``#`` comments are skipped.

    Values are used verbatim (no quoting rules, no interpolation) — this is a
    flat secret store, not a shell.
    """
    out: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        out[key.strip()] = value.strip()
    return out


def load(env: dict[str, str], path: Path | None = None) -> list[str]:
    """Load secrets from ``path`` into ``env`` (typically ``os.environ``).

    Existing keys in ``env`` win over the file, so an explicit ``export``
    still overrides it. Returns the list of keys actually set. Missing file
    is not an error — first run has nothing to load yet.
    """
    p = path or paths.secrets_file()
    if not p.is_file():
        return []
    parsed = parse(p.read_text(encoding="utf-8"))
    applied = []
    for key, value in parsed.items():
        if key not in env:
            env[key] = value
            applied.append(key)
    return applied


def ensure_file() -> Path:
    """Create an empty, mode-600 secrets file if none exists yet."""
    p = paths.secrets_file()
    if not p.is_file():
        paths.ensure_state()
        p.write_text(
            "# bridge secrets: KEY=VALUE, one per line. Loaded automatically at CLI startup.\n",
            encoding="utf-8",
        )
    p.chmod(0o600)
    return p

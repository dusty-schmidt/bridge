"""Workspace profiles: discovery, inheritance, merge rules, effective config.

A workspace is a directory tree. Any directory may declare itself a **root**
with ``root: true`` in ``.bridge/profile.yaml``; the inheritance chain runs from
the nearest declared root down to the directory a session is launched in.
Each directory on the way may contribute its own ``profile.yaml``.

Merge rules (child wins, applied root -> launch dir):

* mappings (``dict``) merge key-by-key, recursively;
* scalars replace parent values; a child value of YAML ``null`` deletes that key;
* **lists are replaced wholesale** — the child's list is the effective list,
  never concatenated;

Secrets are never stored here: profiles reference them as ``${ENV_VAR}`` and
the variable is resolved (and only then) at use time. ``bridge config show``
prints the effective configuration with secret-shaped values redacted.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import paths
from .util import redact, redact_deep, short_hash

MAX_WALK = 64

DEFAULTS: dict[str, Any] = {
    "instructions": {"files": [], "text": ""},
    "mcp": {"servers": {}},
    "memory": {"backend": "sqlite", "scope": "root_tree", "top_k": 5, "mcp": True},
    "docs": {"routes": {}},
    "git": {"auto_branch": True},
    "nats": {
        "url": "nats://100.115.32.6:4222",
        "stream": "BRIDGE",
        "prefix": "bridge",
    },
    "agent": {"client": "claude", "model": "sonnet", "app": "claude-code", "role": "worker"},
}


class ProfileError(RuntimeError):
    pass


# --- discovery --------------------------------------------------------------


def profile_path(directory: Path) -> Path:
    return directory / paths.PROFILE_DIR / paths.PROFILE_FILE


def read_profile(directory: Path) -> dict[str, Any] | None:
    p = profile_path(directory)
    if not p.is_file():
        return None
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ProfileError(f"{p}: invalid YAML: {e}") from e
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ProfileError(f"{p}: top level must be a mapping")
    return data


@dataclass
class Chain:
    """The resolved inheritance chain for a directory."""

    start_dir: Path
    dirs: list[Path] = field(default_factory=list)  # root first, start last
    profiles: list[dict[str, Any]] = field(default_factory=list)
    declared_root: Path | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def root_dir(self) -> Path | None:
        return self.dirs[0] if self.dirs else None

    @property
    def tree_id(self) -> str:
        return short_hash(str(self.root_dir or self.start_dir))


def chain_for(start: str | Path) -> Chain:
    """Walk up from ``start`` collecting profiles until a declared root.

    A directory with a profile but no ``root: true`` anywhere in its ancestry
    is a configuration error, not a fallback: inheriting an undeclared root's
    profile silently would let a workspace pick up the wrong tree's config
    with no error. With no profiles at all the chain is empty and the
    built-in defaults apply — that is not an error, there is simply nothing
    to inherit.
    """
    start_dir = Path(start).expanduser().resolve()
    if not start_dir.is_dir():
        raise ProfileError(f"not a directory: {start_dir}")

    found: list[Path] = []
    declared: Path | None = None
    cur = start_dir
    for _ in range(MAX_WALK):
        data = read_profile(cur)
        if data is not None:
            found.append(cur)
            if data.get("root") is True:
                declared = cur
                break
        if cur.parent == cur:
            break
        cur = cur.parent

    if found and declared is None:
        raise ProfileError(
            f"{found[0]} declares a profile but no ancestor declares `root: true`; "
            "add `root: true` to the profile that should anchor this tree"
        )

    found.reverse()  # root first
    chain = Chain(start_dir=start_dir, dirs=found, declared_root=declared)
    if not found:
        chain.notes.append("no .bridge/profile.yaml found; built-in defaults apply")
    chain.profiles = [read_profile(d) or {} for d in chain.dirs]
    return chain


# --- merging ----------------------------------------------------------------


def merge_values(base: Any, override: Any) -> Any:
    """Child wins: dicts merge recursively, lists and scalars are replaced."""
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for k, v in override.items():
            if v is None:
                out.pop(k, None)
            elif k in out:
                out[k] = merge_values(out[k], v)
            else:
                out[k] = v
        return out
    return override


def _apply_overrides(profile: dict[str, Any]) -> dict[str, Any]:
    """Preserve null values so :func:`merge_values` can delete inherited keys."""
    out: dict[str, Any] = {}
    for k, v in profile.items():
        if k == "~":
            continue
        if isinstance(v, dict):
            out[k] = _apply_overrides(v)
        else:
            out[k] = v
    return out


# --- effective configuration -----------------------------------------------


@dataclass
class EffectiveProfile:
    start_dir: Path
    chain: Chain
    data: dict[str, Any]
    version: str

    @property
    def root_dir(self) -> Path | None:
        return self.chain.root_dir

    @property
    def tree_id(self) -> str:
        return self.chain.tree_id

    @property
    def workspace_id(self) -> str:
        return short_hash(str(self.start_dir))

    @property
    def instructions(self) -> dict[str, Any]:
        return self.data.get("instructions", {})

    @property
    def nats(self) -> dict[str, Any]:
        n = dict(DEFAULTS["nats"], **(self.data.get("nats") or {}))
        url = os.environ.get(paths.ENV_NATS_URL) or n.get("url") or ""
        n["url"] = os.path.expandvars(str(url))
        return n

    @property
    def memory(self) -> dict[str, Any]:
        return dict(DEFAULTS["memory"], **(self.data.get("memory") or {}))

    @property
    def mcp_servers(self) -> dict[str, Any]:
        return dict((self.data.get("mcp") or {}).get("servers") or {})

    @property
    def docs(self) -> dict[str, Any]:
        return dict(DEFAULTS["docs"], **(self.data.get("docs") or {}))

    @property
    def git(self) -> dict[str, Any]:
        return dict(DEFAULTS["git"], **(self.data.get("git") or {}))

    @property
    def agent(self) -> dict[str, Any]:
        return dict(DEFAULTS["agent"], **(self.data.get("agent") or {}))

    def resolved_instruction_text(self) -> str:
        """Instructions as text: effective file list + effective inline text."""
        chunks: list[str] = []
        files = self.instructions.get("files") or []
        origin = self.chain.start_dir
        for directory, config in zip(self.chain.dirs, self.chain.profiles):
            if (config.get("instructions") or {}).get("files") is not None:
                origin = directory
        for rel in files:
            p = Path(rel)
            cand = p if p.is_absolute() else origin / p
            if cand.is_file():
                chunks.append(f"<!-- from {cand} -->\n{cand.read_text(encoding='utf-8', errors='replace').strip()}")
        inline = (self.instructions.get("text") or "").strip()
        if inline:
            chunks.append(inline)
        return "\n\n".join(chunks)


def effective(start: str | Path) -> EffectiveProfile:
    chain = chain_for(start)
    data: dict[str, Any] = {}
    for prof in chain.profiles:
        data = merge_values(data, _apply_overrides(prof))
    # `root: true` is structural, not part of the merged settings.
    data.pop("root", None)
    version = short_hash(json.dumps(data, sort_keys=True, default=str))
    return EffectiveProfile(start_dir=chain.start_dir, chain=chain, data=data, version=version)


def redacted_view(eff: EffectiveProfile) -> dict[str, Any]:
    """Effective configuration for display: secrets masked, provenance kept."""
    raw = eff.data
    shown_nats = merge_values(DEFAULTS["nats"], raw.get("nats") or {})
    if os.environ.get(paths.ENV_NATS_URL):
        shown_nats["url"] = "${BRIDGE_NATS_URL}"
    settings = merge_values(DEFAULTS, raw)
    settings.pop("root", None)
    for section, defaults in DEFAULTS.items():
        override = raw.get(section)
        settings[section] = merge_values(defaults, override if isinstance(override, dict) else {})
    settings["nats"] = shown_nats
    return {
        "start_dir": str(eff.start_dir),
        "chain": [str(d) for d in eff.chain.dirs],
        "declared_root": str(eff.chain.declared_root) if eff.chain.declared_root else None,
        "root": str(eff.root_dir) if eff.root_dir else None,
        "workspace_id": eff.workspace_id,
        "tree_id": eff.tree_id,
        "profile_version": eff.version,
        "instructions": redact(_text_preview(eff)),
        "settings": redact_deep(settings),
        "notes": eff.chain.notes,
    }


def _text_preview(eff: EffectiveProfile) -> str:
    text = eff.resolved_instruction_text()
    return text if len(text) <= 400 else text[:400] + f"… [{len(text)} chars]"

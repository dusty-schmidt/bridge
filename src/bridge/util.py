"""Small shared helpers: time, ids, hashing, secret redaction."""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from datetime import datetime, timezone

# --- time -------------------------------------------------------------------


def utcnow() -> str:
    """Current UTC time as an ISO-8601 string with a Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# --- ids --------------------------------------------------------------------


def new_agent_id() -> str:
    return "ag-" + secrets.token_hex(4)


def new_session_id() -> str:
    return str(uuid.uuid4())


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def short_hash(*parts: str, n: int = 12) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:n]


# --- redaction --------------------------------------------------------------
# The bus has no auth (see NETWORK.md): nothing secret may leave the process.
# These patterns are a backstop for *structured* fields we forward, not a
# licence to put secrets into events in the first place.

_SECRET_KV = re.compile(
    r"(?i)\b([a-z0-9_.-]*(?:token|secret|password|passwd|api[_-]?key|apikey|"
    r"credential|authorization|auth)[a-z0-9_.-]*)(\s*[:=]\s*)(\S+)"
)
_BEARER = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/=-]{8,})")
_URL_AUTH = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)([^/@:\s]+):([^/@\s]+)@")
_LONG_TOKEN = re.compile(r"\b([A-Za-z][A-Za-z0-9]*\d[A-Za-z0-9]*[-_][A-Za-z0-9_-]{16,})\b")
_ENV_REF = re.compile(r"\$\{[A-Z_][A-Z0-9_]*\}")  # placeholders are not secrets

MASK = "***"


def redact(text: str) -> str:
    """Mask key-shaped values, bearer headers and long credential-looking tokens.

    Environment-variable references (`${MY_TOKEN}`) are left alone: they are
    references, not values.
    """
    if not isinstance(text, str) or not text:
        return text
    out = _SECRET_KV.sub(lambda m: m.group(1) + m.group(2) + MASK, text)
    out = _BEARER.sub(lambda m: m.group(1) + MASK, out)
    out = _URL_AUTH.sub(lambda m: m.group(1) + MASK + ":" + MASK + "@", out)
    # Protect env placeholders before the generic token sweep.
    placeholders: list[str] = []

    def _stash(m: re.Match) -> str:
        placeholders.append(m.group(0))
        return f"\x00{len(placeholders) - 1}\x00"

    out = _ENV_REF.sub(_stash, out)
    out = _LONG_TOKEN.sub(MASK, out)
    for i, ph in enumerate(placeholders):
        out = out.replace(f"\x00{i}\x00", ph)
    return out


def redact_deep(value, _depth: int = 0):
    """Apply :func:`redact` to every string in a nested structure (depth-limited)."""
    if _depth > 8:
        return value
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        secret_key = re.compile(r"(?i)(token|secret|password|passwd|api[_-]?key|credential|authorization|auth)")
        return {
            k: MASK if secret_key.search(str(k)) and not _ENV_REF.fullmatch(str(v))
            else redact_deep(v, _depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_deep(v, _depth + 1) for v in value]
    return value

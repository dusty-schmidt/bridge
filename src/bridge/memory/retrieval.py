"""Straightforward text retrieval (term-overlap ranking).

Deliberately dumb and dependency-free: it is the *first* implementation behind
the retriever interface, so it can be swapped for embeddings/FTS later without
touching the backend or the MCP tool signatures.
"""

from __future__ import annotations

import math
import re
from typing import Any, Callable, Protocol

_WORD = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return _WORD.findall((text or "").lower())


class Retriever(Protocol):
    def rank(self, rows: list[dict[str, Any]], query: str, limit: int) -> list[tuple[dict[str, Any], float]]:
        ...


class TermOverlapRetriever:
    """Score = weighted term overlap; ties broken by recency (rows are newest first)."""

    def __init__(self, stop: set[str] | None = None) -> None:
        self.stop = stop or {"a", "an", "the", "and", "or", "of", "to", "in", "is", "it", "for", "on", "with"}

    def rank(
        self, rows: list[dict[str, Any]], query: str, limit: int
    ) -> list[tuple[dict[str, Any], float]]:
        terms = [t for t in tokenize(query) if t not in self.stop]
        if not terms:
            return [(r, 0.0) for r in rows[:limit]]
        # document frequency over the candidate set (tiny idf, kills common words)
        df: dict[str, int] = {}
        tokenized = [tokenize(r.get("content") or "") for r in rows]
        for toks in tokenized:
            for t in set(toks):
                df[t] = df.get(t, 0) + 1
        n = max(len(rows), 1)
        scored: list[tuple[dict[str, Any], float]] = []
        for row, toks in zip(rows, tokenized):
            if not toks:
                continue
            score = 0.0
            counts = {t: toks.count(t) for t in set(toks)}
            for t in terms:
                if t in counts:
                    idf = math.log((n + 1) / (df.get(t, 1) + 1)) + 1.0
                    score += (1 + math.log(counts[t])) * idf
            if score > 0:
                scored.append((row, round(score, 4)))
        scored.sort(key=lambda p: -p[1])
        return scored[:limit]


def default_retriever() -> Retriever:
    return TermOverlapRetriever()

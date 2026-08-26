"""Local fallback token estimation, used only when a Cortex COMPLETE response
shape does not expose usage metadata. Never silently written to the ledger
as a real count - callers must set `estimated=True` alongside it.

A whitespace-ish heuristic (~4 chars/token) is used rather than pulling in a
real tokenizer dependency; it only needs to be in the right ballpark for a
cost estimate flagged as estimated, not exact.
"""
from __future__ import annotations


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    # ~4 characters per token is the standard cheap heuristic for English
    # prose; never used when the API supplies a real count.
    return max(1, len(text) // 4)


def estimate_messages_tokens(contents: list[str]) -> int:
    return sum(estimate_tokens(c) for c in contents)

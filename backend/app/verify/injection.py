"""Screen summary claims for known instruction and payload patterns.

A matching claim causes the pipeline to abstain. This is a lexical screen.
See docs/corpus-controls.md for guarantees and evaluation limits.
"""
from __future__ import annotations

import re

from backend.app.verify.grounding import split_claims

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(rx, re.IGNORECASE))
    for name, rx in (
        ("override", r"\b(ignore|disregard|forget|override)\b[^.]{0,40}\b(previous|prior|above|earlier|all|system)\b[^.]{0,20}\b(instructions?|prompts?|rules?|messages?)\b"),
        ("role_tag", r"^\W*(system|assistant|developer)\s*:\s"),
        ("persona", r"\byou are now\b|\bact as (an?|the) \w+|\bmaintenance mode\b|\bjailbreak\b"),
        ("prompt_exfil", r"\b(reveal|print|show|output|repeat)\b[^.]{0,30}\b(system prompt|hidden prompt|your instructions|api[ _-]?key|password|credentials?|secret)\b"),
        ("host_path", r"/etc/(passwd|shadow)|~/\.ssh|\.env\b|169\.254\.169\.254"),
        ("shell", r"\b(curl|wget)\s+https?://|\|\s*(sh|bash)\b|\brm\s+-rf\b|`[^`]*\b(sh|bash|curl|wget)\b[^`]*`"),
        ("sql", r"\b(drop|truncate|alter)\s+table\b|\bdelete\s+from\b|;\s*--"),
        ("link_directive", r"\b(click|visit|go to|navigate to|open)\b[^.]{0,20}https?://"),
    )
)


def instruction_markers(text: str) -> list[str]:
    """Names of the patterns `text` matches; empty means it reads as content."""
    return [name for name, rx in _PATTERNS if rx.search(text)]


def screen_summary(markdown: str) -> list[tuple[str, list[str]]]:
    """(claim, matched pattern names) for every claim that reads as an instruction."""
    flagged = []
    for claim in split_claims(markdown):
        markers = instruction_markers(claim)
        if markers:
            flagged.append((claim, markers))
    return flagged

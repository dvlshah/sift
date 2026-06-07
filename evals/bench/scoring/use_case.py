"""Use-case-specific pattern scoring.

The premise: a "good" tax-docs extraction must preserve dollar amounts and
FY references; a "good" RFC extraction must preserve `Section 4.2` and BCP-14
keywords like MUST/SHOULD; a "good" coding-docs extraction must preserve
``` fences. Generic structural metrics don't capture these.

Each fixture's :attr:`use_case_patterns` is a tuple of regex strings the
extracted markdown should retain at high coverage. We compare per-pattern
counts in HTML vs markdown and report per-pattern preservation ratios.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence


def _strip_tags(html: str) -> str:
    """Cheap text-only view of HTML — drops tags but keeps element content
    so patterns like ``$3,500`` match whether they're inside `<p>` or `<td>`.
    Not a real HTML→text converter, just enough to keep regex pattern
    counts comparable to markdown."""
    return re.sub(r"<[^>]+>", " ", html)


@dataclass(frozen=True)
class PatternPreservation:
    pattern: str
    html_count: int
    md_count:   int
    ratio:      float


def score_use_case_patterns(
    html: str,
    md: str,
    patterns: Sequence[str],
) -> list[PatternPreservation]:
    """For each pattern, count occurrences in the HTML's text content and in
    the extracted markdown, return a list of per-pattern preservation ratios.
    """
    text = _strip_tags(html)
    out: list[PatternPreservation] = []
    for p in patterns:
        rx = re.compile(p, re.MULTILINE)
        h = len(rx.findall(text))
        m = len(rx.findall(md))
        ratio = 1.0 if h == 0 else min(1.0, m / h)
        out.append(PatternPreservation(pattern=p, html_count=h,
                                       md_count=m, ratio=ratio))
    return out


def aggregate_use_case_score(rows: list[PatternPreservation]) -> float:
    """Mean preservation across patterns that have non-zero HTML count.
    Patterns absent from HTML (count=0) are vacuously preserved at 1.0
    and don't contribute to the mean."""
    active = [r.ratio for r in rows if r.html_count > 0]
    return sum(active) / len(active) if active else 0.0

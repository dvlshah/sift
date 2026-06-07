"""Structural element scoring — count headings / tables / lists / code blocks
/ links in HTML and the resulting markdown, then compute preservation ratios.

Borrowed pattern from MainWebBench: web extraction benchmarks must
**explicitly** measure structured-element preservation, not just text fidelity.
A 95% text-match score is meaningless if every code block was stripped to
inline text. We count five structural element types in both representations
and report (md_count / html_count) as the preservation ratio per type.

All counts are intentionally simple regex-based heuristics. They're not
perfect HTML/MD parsers — they're stable, fast, dependency-free comparators
that move with the corpus and produce numbers a human can sanity-check by
opening a single file.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping


# ---- HTML element counts ---------------------------------------------------

_HTML_PATTERNS: dict[str, re.Pattern[str]] = {
    "heading":   re.compile(r"<h[1-6]\b", re.IGNORECASE),
    "table":     re.compile(r"<table\b", re.IGNORECASE),
    "list":      re.compile(r"<[uo]l\b", re.IGNORECASE),  # ul or ol
    # Code blocks: <pre>, <pre><code>, or fenced ```. We count <pre> in HTML
    # since trafilatura emits both <pre> and <pre><code> shapes.
    "code_block": re.compile(r"<pre\b", re.IGNORECASE),
    "link":      re.compile(r"<a\b[^>]*\bhref\s*=", re.IGNORECASE),
}


def count_html_elements(html: str) -> dict[str, int]:
    """Count structural elements in raw HTML. Heuristic-grade — counts opening
    tags only, so nested elements are counted at their natural level."""
    return {kind: len(p.findall(html)) for kind, p in _HTML_PATTERNS.items()}


# ---- Markdown element counts -----------------------------------------------

# Heading: leading hashes
_MD_HEADING = re.compile(r"^#{1,6}\s+\S", re.MULTILINE)

# Table: any line that starts with `|` and contains another `|` — markdown
# table rows. We count the row, not the table; for our purposes a non-zero
# count for tables means table content survived.
_MD_TABLE = re.compile(r"^\|[^\n]+\|", re.MULTILINE)

# List: leading `-`, `*`, `+`, or `1.` etc. at start of line (after spaces)
_MD_LIST = re.compile(r"^\s*(?:[-*+]\s|\d+\.\s)\S", re.MULTILINE)

# Code block: triple-backtick fences. We count opening fences (every other
# fence is closing).
_MD_FENCE = re.compile(r"^```", re.MULTILINE)

# Link: standard [text](url) — `url` may contain almost anything except a
# bare `)` until the closing paren.
_MD_LINK = re.compile(r"\[[^\]]+\]\([^)]+\)")


def count_md_elements(md: str) -> dict[str, int]:
    """Count structural elements in markdown. For tables, we count row-lines
    (so a 5-row table contributes 5); for code blocks, opening fences only.
    Headings and lists are counted at their natural line-level."""
    fences = len(_MD_FENCE.findall(md))
    return {
        "heading":     len(_MD_HEADING.findall(md)),
        "table":       len(_MD_TABLE.findall(md)),
        "list":        len(_MD_LIST.findall(md)),
        # Code blocks come in pairs of opening + closing fences. Round down so
        # a stray unclosed fence at EOF doesn't double-count.
        "code_block":  fences // 2,
        "link":        len(_MD_LINK.findall(md)),
    }


# ---- Preservation ratios ---------------------------------------------------

@dataclass(frozen=True)
class PreservationScore:
    html_counts: Mapping[str, int]
    md_counts:   Mapping[str, int]
    ratios:      Mapping[str, float]  # md / html per element type
    summary:     dict                 # for JSON dump

    @property
    def mean_ratio(self) -> float:
        """Mean preservation across element types that exist in HTML."""
        active = [r for kind, r in self.ratios.items()
                  if self.html_counts.get(kind, 0) > 0]
        return sum(active) / len(active) if active else 0.0


def preservation_score(html: str, md: str) -> PreservationScore:
    """Compute (md count / html count) per element type. Ratios are capped at
    1.0 — markdown shouldn't produce *more* of an element than HTML had, but
    counting heuristics can occasionally overshoot (e.g. extract phase
    re-emits a TOC of links), so we cap to avoid spurious >100% scores.

    Element types with zero HTML count contribute 1.0 to the mean (vacuously
    preserved — nothing to lose). They're tracked separately in `summary` so
    a reader can see "table_count_html=0" and not assume the site has tables.
    """
    h = count_html_elements(html)
    m = count_md_elements(md)
    ratios: dict[str, float] = {}
    for kind in _HTML_PATTERNS:
        hc, mc = h[kind], m[kind]
        if hc == 0:
            ratios[kind] = 1.0           # vacuously preserved
        else:
            ratios[kind] = min(1.0, mc / hc)
    summary = {
        "html_counts": dict(h),
        "md_counts":   dict(m),
        "ratios":      ratios,
    }
    return PreservationScore(html_counts=h, md_counts=m,
                             ratios=ratios, summary=summary)

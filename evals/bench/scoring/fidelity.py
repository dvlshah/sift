"""Fidelity scoring — reference-based metrics for "how close is candidate
output to a reference text" (edit distance + n-gram overlap).

Used by the comparative-baseline path (sift vs raw curl+trafilatura vs
Firecrawl markdown) and by per-stage tests where we have a fixed expected
output (e.g. extract on a synthetic HTML fixture).

Self-contained — no extra dependencies. Edit distance is the standard
Levenshtein O(n·m) DP; n-gram overlap is the simplest unigram-Jaccard, which
maps to BLEU-1 with brevity penalty disabled. Good enough as a comparator
where the reference is a real markdown file we trust.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# Word tokenization — markdown content is heavily punctuated; we strip ALL
# punctuation and lowercase so "Section 8" and "section 8." match.
_TOKEN_RE = re.compile(r"\w+")


def _tokens(s: str) -> list[str]:
    return _TOKEN_RE.findall(s.lower())


def edit_distance(a: str, b: str, *, max_chars: int = 50_000) -> int:
    """Levenshtein distance, O(n·m) time, O(min(n,m)) space.

    Capped at ``max_chars`` per side to keep worst-case bounded — comparing
    two 100KB markdown files would otherwise be ~10GB of DP cells. For sift's
    use the cap is fine: extracted pages are ~5KB mean, ~20KB worst-case.
    """
    a, b = a[:max_chars], b[:max_chars]
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            ins = curr[j - 1] + 1
            dele = prev[j] + 1
            sub = prev[j - 1] + (0 if ca == cb else 1)
            curr[j] = min(ins, dele, sub)
        prev = curr
    return prev[-1]


def normalized_edit_distance(a: str, b: str) -> float:
    """Edit distance / max(len(a), len(b)) — 0.0 = identical, 1.0 = total
    rewrite. Inverse of fidelity, so subtract from 1.0 to compare to other
    "higher-is-better" scores."""
    if not a and not b:
        return 0.0
    return edit_distance(a, b) / max(len(a[:50_000]), len(b[:50_000]))


def unigram_jaccard(a: str, b: str) -> float:
    """Token overlap |A ∩ B| / |A ∪ B|. Insensitive to order and length.
    Useful for "did the same words appear" even when extractors differ on
    sentence boundaries or spacing."""
    ta, tb = set(_tokens(a)), set(_tokens(b))
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def ngram_overlap(a: str, b: str, *, n: int = 2) -> float:
    """Token-level n-gram overlap (BLEU-1 / BLEU-2 / ... style, no brevity
    penalty). Default n=2 is bigram. More order-sensitive than unigram
    Jaccard; catches "section eight" vs "eight section" as a real difference.
    """
    def _ngrams(tokens: list[str], n: int) -> list[tuple[str, ...]]:
        return [tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)]

    ga = _ngrams(_tokens(a), n)
    gb = _ngrams(_tokens(b), n)
    if not ga and not gb:
        return 1.0
    if not ga or not gb:
        return 0.0
    # Multi-set intersection size / max(|ga|, |gb|)
    from collections import Counter
    ca, cb = Counter(ga), Counter(gb)
    inter = sum((ca & cb).values())
    return inter / max(len(ga), len(gb))


@dataclass(frozen=True)
class FidelityScore:
    edit_distance:  int
    norm_edit:      float
    unigram_jacc:   float
    bigram_overlap: float

    @property
    def composite(self) -> float:
        """Single 0-1 score for ranking purposes. 50% bigram + 30% unigram
        + 20% (1 - normalized edit distance). Weighted toward bigram since it
        catches both order and presence — but no single metric tells the
        whole story, hence the per-component fields above for drill-down."""
        return (0.5 * self.bigram_overlap
                + 0.3 * self.unigram_jacc
                + 0.2 * (1.0 - self.norm_edit))


def fidelity_score(candidate: str, reference: str) -> FidelityScore:
    return FidelityScore(
        edit_distance=edit_distance(candidate, reference),
        norm_edit=normalized_edit_distance(candidate, reference),
        unigram_jacc=unigram_jaccard(candidate, reference),
        bigram_overlap=ngram_overlap(candidate, reference, n=2),
    )

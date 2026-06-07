"""Extraction strategy seam — the pipeline structure behind ``extract_one``.

Stage 1 of the extraction generalization (see the design discussion in
PR history). This module defines the *types* and *registries* for the
two composition modes the extract phase uses; the concrete primary
strategies are wired in ``extract.py`` where their functions live.

Two composition modes
======================

* **Primary strategies COMPETE** — markdown-passthrough, PDF, and HTML
  (trafilatura). Exactly one runs, chosen first-applicable. This is the
  ``body_kind`` dispatch that used to be an if/elif/else in
  ``extract_one``.

* **Enricher strategies COMPOSE** — code-block recovery and Next.js RSC
  recovery run *after* the HTML primary, each merging additional content
  the primary's main-content scorer dropped. They're HTML-specific (PDF
  and markdown bodies have nothing to enrich) so they're scoped to the
  HTML primary rather than the global pipeline.

Why a seam instead of inline calls
==================================

The pre-refactor code bolted each new recovery pass directly into
``extract_markdown`` as a sequential merge. That worked but didn't
scale: every new rendering shape meant another hard-coded merge, no
per-strategy provenance, and no place to hang the quality scoring +
acquisition-feedback the later stages need. Making the pipeline a
registry turns "add a recovery pass" into "append to a tuple" and gives
Stage 2 a clean spot to attach quality signals.

Determinism contract (unchanged)
================================

Every strategy here is a **pure function of the raw blob** — no network,
no clock, no global state. That's what lets ``content_hash =
sha256(normalize(extract(raw)))`` stay reproducible and lets the
determinism eval re-extract offline. Acquisition concerns (browser
render, ``_next/data`` re-fetch) deliberately do NOT live here — they
belong in the fetch phase, which produces the raw blob this pipeline
parses.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Protocol, Union

from .extract_code import merge_missing_code_blocks
from .extract_next_state import merge_next_state


@dataclass(frozen=True)
class ExtractInput:
    """Everything a strategy needs to decide applicability + run.

    Pure data — no handles to the network or DB. ``body_kind`` is the
    site profile's classification (``"markdown"`` / ``"pdf"`` / ``"html"``
    / ``None`` to defer to the body sniff), passed in so strategy
    applicability stays a pure predicate over this struct.
    """
    raw: bytes
    url: str
    content_type: Optional[str]
    body_kind: Optional[str]


class _ExtractFn(Protocol):
    """The shape every primary extractor already has: ``(raw, url) ->
    (markdown_or_None, title_or_None)``."""
    def __call__(self, body: bytes, url: str) -> tuple[Optional[str], Optional[str]]: ...


@dataclass(frozen=True)
class PrimaryStrategy:
    """One competing primary extractor.

    ``applies`` is a pure predicate over ``ExtractInput``; the first
    strategy in the registry whose predicate is True wins. ``version``
    is the ``EXTRACTOR_VERSION_*`` recorded on the manifest row (so a
    bump to one path doesn't invalidate the others). ``kind`` is the
    short label used in ExtractResult reasons (``new-content-<kind>`` /
    ``extract-failed-<kind>``).
    """
    name: str
    kind: str                 # 'md' | 'pdf' | 'html'
    version: str
    applies: Callable[[ExtractInput], bool]
    extract: _ExtractFn


# ---- Enricher registry (HTML primary only) --------------------------------

# Signature shared by every enricher: ``(markdown, raw) -> (markdown,
# n_appended)``. Pure; appends recovered content the trafilatura pass
# missed. Order matters only for output stability, not correctness —
# kept fixed so the HTML extractor version's output is byte-stable.
_EnricherFn = Callable[[str, Union[bytes, str]], tuple[str, int]]


@dataclass(frozen=True)
class Enricher:
    name: str
    merge: _EnricherFn


# The active HTML enrichers, in run order. Adding a recovery pass is a
# one-line append here (plus an EXTRACTOR_VERSION_HTML bump in extract.py
# when the new pass changes output). This tuple is the extensible seam
# that replaces the inline merges that used to live in extract_markdown.
HTML_ENRICHERS: tuple[Enricher, ...] = (
    Enricher("code-blocks", merge_missing_code_blocks),
    Enricher("next-rsc", merge_next_state),
)


def run_html_enrichers(md: str, raw: Union[bytes, str]) -> tuple[str, int]:
    """Run every HTML enricher in order, threading the markdown through.

    Returns ``(enriched_markdown, total_appended)``. ``total_appended``
    is the sum of all enrichers' append counts — the HTML primary uses
    it to distinguish "trafilatura found nothing AND nothing was
    recovered" (a real extraction failure) from "trafilatura found
    nothing but an enricher rescued the page."
    """
    total = 0
    for enricher in HTML_ENRICHERS:
        md, n = enricher.merge(md, raw)
        total += n
    return md, total


def select_primary(
    inp: ExtractInput, strategies: tuple[PrimaryStrategy, ...],
) -> PrimaryStrategy:
    """First-applicable wins. The final strategy in ``strategies`` is the
    terminal fallback and is expected to always apply (the HTML path),
    so this never returns None — but we defensively fall back to the
    last entry if no predicate matched."""
    for s in strategies:
        if s.applies(inp):
            return s
    return strategies[-1]

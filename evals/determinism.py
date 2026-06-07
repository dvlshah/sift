"""Determinism eval — does re-extracting from cached raw produce the same content_hash?

The whole pipeline rests on this invariant. If `trafilatura -> normalize -> sha256`
isn't byte-stable on the same input, every gate and provenance claim is shaky.

We sample N FRESH pages, fetch the raw blob from the content-addressed store,
re-run the extract path, and compare the recomputed content_hash to the manifest's
stored content_hash. Any mismatch is a P0 bug.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from sift.extract import reextract_and_hash
from sift.fetch import read_raw_blob
from sift.index_profile import apply_index_profile

from .sampler import SampleSpec, stratified


@dataclass
class DeterminismMismatch:
    url: str
    tier: str
    stored_content_hash: str
    recomputed_content_hash: str
    extractor_version_stored: str
    extractor_kind: str   # "html" or "pdf"


@dataclass
class DeterminismMetrics:
    run_id: str
    sample_size: int = 0
    matches: int = 0
    mismatches: int = 0
    skipped_no_raw: int = 0
    skipped_extract_failed: int = 0
    # A page whose stored extractor_version differs from the current code's:
    # a differing hash is EXPECTED version skew (the corpus predates an
    # extractor bump and wants `sift re-extract`), NOT a determinism failure.
    # Kept separate so a stale corpus never masquerades as a broken invariant
    # — only ``mismatches`` (same version, different hash) is the P0 signal.
    skipped_version_skew: int = 0
    mismatch_examples: list[DeterminismMismatch] = field(default_factory=list)
    by_tier_matches: dict[str, int] = field(default_factory=dict)
    by_tier_mismatches: dict[str, int] = field(default_factory=dict)


def _recompute_content_hash(html: bytes, url: str) -> tuple[Optional[str], str, str]:
    """Re-run the canonical extract path on raw bytes. Returns
    (content_hash, kind, extractor_version), or (None, kind, version) if
    extraction failed.

    Routes through ``reextract_and_hash`` — the SAME dispatch
    ``extract_one`` uses (markdown-passthrough / PDF / HTML primaries +
    enrichers) — rather than re-implementing a subset. The hand-rolled
    version this replaced skipped the markdown-passthrough primary,
    which produced spurious mismatches on Stripe-style .md pages.
    Requires the index's profile to be active (see ``run`` below). The
    returned extractor_version is the CURRENT code's, so the caller can
    distinguish version skew from genuine non-determinism."""
    res = reextract_and_hash(html, url)
    return (res.content_hash if res.ok else None), res.kind, res.extractor_version


def run(
    root: Path,
    run_id: str,
    *,
    conn: sqlite3.Connection,
    sample: int = 50,
) -> DeterminismMetrics:
    """Re-extract `sample` random FRESH pages and verify their content_hash matches."""
    # Activate the index's OWN profile before any re-hash — the content_hash
    # is profile-dependent (normalize_for_hash strips the profile's dynamic
    # patterns + body_kind drives the strategy). Without this, a non-ATO
    # index is re-hashed under the package-default ATO profile and every
    # page falsely mismatches.
    apply_index_profile(root)
    # Proportional sample across tiers.
    from .sampler import sample_by_count
    rows = sample_by_count(conn, sample, label="determinism", fresh_only=True)

    metrics = DeterminismMetrics(run_id=run_id, sample_size=len(rows))
    for row in rows:
        if not row.raw_hash or not row.content_hash:
            metrics.skipped_no_raw += 1
            continue
        try:
            html = read_raw_blob(root, row.raw_hash)
        except (FileNotFoundError, OSError):
            metrics.skipped_no_raw += 1
            continue
        recomputed, kind, current_version = _recompute_content_hash(html, row.url)
        if recomputed is None:
            metrics.skipped_extract_failed += 1
            continue
        if recomputed == row.content_hash:
            metrics.matches += 1
            metrics.by_tier_matches[row.tier] = (
                metrics.by_tier_matches.get(row.tier, 0) + 1
            )
        elif row.extractor_version and row.extractor_version != current_version:
            # Version skew, not non-determinism: this page was extracted by a
            # DIFFERENT extractor version than the current code, so a differing
            # hash is the expected consequence of an extractor bump. Counting it
            # as a mismatch would make a corpus that simply needs `sift
            # re-extract` look like the hash invariant is broken.
            metrics.skipped_version_skew += 1
        else:
            # Same extractor version, different hash — the real P0 signal:
            # the extractor is non-deterministic on identical input.
            metrics.mismatches += 1
            metrics.by_tier_mismatches[row.tier] = (
                metrics.by_tier_mismatches.get(row.tier, 0) + 1
            )
            if len(metrics.mismatch_examples) < 10:
                metrics.mismatch_examples.append(DeterminismMismatch(
                    url=row.url,
                    tier=row.tier,
                    stored_content_hash=row.content_hash,
                    recomputed_content_hash=recomputed,
                    extractor_version_stored=row.extractor_version or "",
                    extractor_kind=kind,
                ))
    return metrics


def to_dict(m: DeterminismMetrics) -> dict:
    return asdict(m)

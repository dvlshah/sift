"""Deterministic stratified sampling over the manifest.

We sample by tier (LIVING / CURRENT_FORMS / NEWS / FROZEN) so that quality
evals don't accidentally over-represent the biggest tier. Same `seed` + same
manifest = same sample, which matters for cross-run comparison.

Returns ManifestRow objects to keep callers decoupled from the SQL layer.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import Iterable

from sift.manifest import ManifestRow, iter_all


@dataclass(frozen=True)
class SampleSpec:
    """Per-tier counts for a stratified sample."""
    living: int = 0
    current_forms: int = 0
    frozen: int = 0
    news: int = 0

    def total(self) -> int:
        return self.living + self.current_forms + self.frozen + self.news

    def for_tier(self, tier: str) -> int:
        return {
            "LIVING":         self.living,
            "CURRENT_FORMS":  self.current_forms,
            "FROZEN":         self.frozen,
            "NEWS":           self.news,
        }.get(tier, 0)


def _seed_rng(label: str, seed: str = "sift-evals-v1") -> random.Random:
    """Deterministic per-call RNG so eval samples are reproducible."""
    h = hashlib.sha256(f"{seed}:{label}".encode()).digest()
    return random.Random(int.from_bytes(h[:8], "big"))


def stratified(
    conn,
    spec: SampleSpec,
    *,
    label: str,
    fresh_only: bool = True,
) -> list[ManifestRow]:
    """Return up to spec.for_tier(t) rows per tier, sampled deterministically.

    If a tier has fewer eligible rows than requested, returns all of them
    (no error, no padding). Use `fresh_only=False` to include UNSEEN/FAILED
    rows (e.g. when sampling for the determinism eval which needs raw_hash).
    """
    rng = _seed_rng(label)
    by_tier: dict[str, list[ManifestRow]] = {}
    for row in iter_all(conn):
        if fresh_only and row.state != "FRESH":
            continue
        by_tier.setdefault(row.tier, []).append(row)

    out: list[ManifestRow] = []
    for tier, pool in by_tier.items():
        n = spec.for_tier(tier)
        if n <= 0:
            continue
        if len(pool) <= n:
            out.extend(pool)
        else:
            out.extend(rng.sample(pool, n))
    return out


def sample_by_count(
    conn,
    total: int,
    *,
    label: str,
    fresh_only: bool = True,
) -> list[ManifestRow]:
    """Proportional sample of `total` rows across tiers, weighted by tier size.

    Cheaper interface for evals that just need 'N representative pages'."""
    by_tier: dict[str, int] = {}
    for row in iter_all(conn):
        if fresh_only and row.state != "FRESH":
            continue
        by_tier[row.tier] = by_tier.get(row.tier, 0) + 1
    total_rows = sum(by_tier.values())
    if total_rows == 0:
        return []
    # Proportional allocation. Per-tier rounding can sum to more than `total`
    # (e.g. 1.8 + 0.6 + 0.6 = 3.0 → 2 + 1 + 1 = 4); cap the result at `total`
    # using a deterministic per-URL hash so the sample stays reproducible.
    spec = SampleSpec(
        living=round(total * by_tier.get("LIVING", 0) / total_rows),
        current_forms=round(total * by_tier.get("CURRENT_FORMS", 0) / total_rows),
        frozen=round(total * by_tier.get("FROZEN", 0) / total_rows),
        news=round(total * by_tier.get("NEWS", 0) / total_rows),
    )
    rows = stratified(conn, spec, label=label, fresh_only=fresh_only)
    if len(rows) <= total:
        return rows
    # Deterministic clip: hash by url so the same overflow always drops the
    # same URLs (reproducibility across runs).
    rows.sort(key=lambda r: hashlib.sha256(f"{label}:{r.url}".encode()).hexdigest())
    return rows[:total]

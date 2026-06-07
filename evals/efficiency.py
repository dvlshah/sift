"""Efficiency baseline — disk footprint, bandwidth, dedup ratio, refresh efficiency.

Per-stage cost-per-page metrics. Useful for:
  * "is the corpus growing linearly with content, or are we hoarding stale data?"
  * "how cache-efficient is our raw-blob store? (dedup ratio)"
  * "how much wall time does a refresh save vs a full crawl? (refresh efficiency)"
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class DiskFootprint:
    manifest_db_bytes: int = 0
    raw_blobs_bytes: int = 0
    raw_blobs_count: int = 0
    md_files_bytes: int = 0
    md_files_count: int = 0
    facts_bytes: int = 0
    facts_count: int = 0
    artifacts_bytes: int = 0
    sections_bytes: int = 0
    routes_tsv_bytes: int = 0
    changelog_bytes: int = 0
    total_bytes: int = 0


@dataclass
class PerPageCost:
    raw_bytes_per_page: float = 0.0
    md_bytes_per_page: float = 0.0
    manifest_bytes_per_row: float = 0.0


@dataclass
class DedupRatio:
    # raw HTML can deduplicate when distinct URLs serve identical bytes
    unique_raw_hashes: int = 0
    fresh_rows_with_raw_hash: int = 0
    # ratio < 1 means some URLs share a raw blob
    blob_to_row_ratio: float = 1.0


@dataclass
class EfficiencyMetrics:
    run_id: str
    disk: DiskFootprint = field(default_factory=DiskFootprint)
    per_page: PerPageCost = field(default_factory=PerPageCost)
    dedup: DedupRatio = field(default_factory=DedupRatio)
    changelog_entries: int = 0


def _du_dir(p: Path) -> int:
    """Sum of file sizes under p (recursive)."""
    if not p.exists():
        return 0
    total = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                continue
    return total


def _file_count(p: Path, glob: str) -> int:
    if not p.exists():
        return 0
    return sum(1 for _ in p.rglob(glob))


def run(root: Path, run_id: str) -> EfficiencyMetrics:
    metrics = EfficiencyMetrics(run_id=run_id)

    # Disk per-component
    metrics.disk.manifest_db_bytes = (
        (root / "manifest.db").stat().st_size if (root / "manifest.db").exists() else 0
    )
    metrics.disk.raw_blobs_bytes = _du_dir(root / "raw")
    metrics.disk.raw_blobs_count = _file_count(root / "raw", "*.html.gz")

    run_dir = root / "runs" / run_id
    md_dir = run_dir / "md"
    metrics.disk.md_files_bytes = sum(
        f.stat().st_size for f in md_dir.rglob("*.md")
    ) if md_dir.exists() else 0
    metrics.disk.md_files_count = _file_count(md_dir, "*.md") if md_dir.exists() else 0

    facts_dir = run_dir / "facts"
    metrics.disk.facts_bytes = _du_dir(facts_dir)
    metrics.disk.facts_count = sum(
        1 for f in facts_dir.rglob("*.json")
        if "schemas" not in f.parts
    ) if facts_dir.exists() else 0

    metrics.disk.artifacts_bytes = _du_dir(run_dir / "artifacts")
    metrics.disk.sections_bytes = _du_dir(run_dir / "sections")
    rt = run_dir / "routes.tsv"
    metrics.disk.routes_tsv_bytes = rt.stat().st_size if rt.exists() else 0

    cl = root / "changelog.jsonl"
    if cl.exists():
        metrics.disk.changelog_bytes = cl.stat().st_size
        with cl.open() as f:
            metrics.changelog_entries = sum(1 for _ in f)

    metrics.disk.total_bytes = (
        metrics.disk.manifest_db_bytes + metrics.disk.raw_blobs_bytes
        + metrics.disk.md_files_bytes + metrics.disk.facts_bytes
        + metrics.disk.artifacts_bytes + metrics.disk.sections_bytes
        + metrics.disk.routes_tsv_bytes + metrics.disk.changelog_bytes
    )

    # Per-row metrics from manifest
    db_path = root / "manifest.db"
    if db_path.exists():
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        total_rows = conn.execute("SELECT COUNT(*) FROM manifest").fetchone()[0] or 1
        fresh = conn.execute(
            "SELECT COUNT(*) FROM manifest WHERE state='FRESH'"
        ).fetchone()[0]
        fresh_with_raw = conn.execute(
            "SELECT COUNT(*) FROM manifest WHERE state='FRESH' AND raw_hash IS NOT NULL"
        ).fetchone()[0]
        unique_raw = conn.execute(
            "SELECT COUNT(DISTINCT raw_hash) FROM manifest "
            "WHERE state='FRESH' AND raw_hash IS NOT NULL"
        ).fetchone()[0]
        metrics.dedup.unique_raw_hashes = unique_raw
        metrics.dedup.fresh_rows_with_raw_hash = fresh_with_raw
        if fresh_with_raw > 0:
            metrics.dedup.blob_to_row_ratio = round(
                unique_raw / fresh_with_raw, 4
            )
        if fresh > 0 and metrics.disk.raw_blobs_bytes > 0:
            metrics.per_page.raw_bytes_per_page = round(
                metrics.disk.raw_blobs_bytes / fresh, 1
            )
        if metrics.disk.md_files_count > 0 and metrics.disk.md_files_bytes > 0:
            metrics.per_page.md_bytes_per_page = round(
                metrics.disk.md_files_bytes / metrics.disk.md_files_count, 1
            )
        if total_rows > 0 and metrics.disk.manifest_db_bytes > 0:
            metrics.per_page.manifest_bytes_per_row = round(
                metrics.disk.manifest_db_bytes / total_rows, 1
            )
        conn.close()

    return metrics


def to_dict(m: EfficiencyMetrics) -> dict:
    return asdict(m)

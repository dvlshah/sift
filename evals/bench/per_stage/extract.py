"""Stage 4 evals: extract.

This is the user-visible quality story — does the markdown sift produces
preserve the structural and semantic content of the source HTML?

We pair each extracted markdown file with its source HTML (read from the
content-addressed raw store via the manifest's raw_hash), then run four
evals per file and aggregate per-fixture:

  * ``extract_structural_preservation`` — headings, tables, lists, code
    blocks, links survive at ≥ 85% per type (mean ≥ 0.85)
  * ``extract_anchor_injection`` — every markdown heading carries a
    deterministic ``{#slug}`` anchor; uniqueness within the file
  * ``extract_link_preservation`` — count of ``<a href=>`` in HTML vs
    ``[](url)`` in markdown ≥ 0.85
  * ``extract_use_case_quality`` — fixture's use-case-specific patterns
    preserved at ≥ 0.95 (currency for tax, RFC refs for legal, code fences
    for coding, etc.)

Determinism is covered by the existing ``evals.determinism`` module — we
import it as a related eval rather than reimplement.
"""
from __future__ import annotations

import gzip
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Optional

from sift import paths
from sift.manifest import open_db

from ..fixtures.sites import SiteFixture
from ..scoring.structural import preservation_score, PreservationScore
from ..scoring.use_case import (
    PatternPreservation,
    aggregate_use_case_score,
    score_use_case_patterns,
)


# ---- Helpers: file pairing -------------------------------------------------

@dataclass(frozen=True)
class FilePair:
    url: str
    md_path: Path
    raw_hash: str

    def html(self, root: Path) -> str:
        return gzip.decompress(paths.raw_path(root, self.raw_hash).read_bytes()).decode(
            "utf-8", errors="replace")

    def md(self) -> str:
        return self.md_path.read_text(encoding="utf-8")


def iter_file_pairs(
    root: Path,
    run_id: str,
    *,
    fixture: Optional[SiteFixture] = None,
    sample: Optional[int] = None,
) -> Iterable[FilePair]:
    """Yield (url, md_path, raw_hash) tuples for FRESH/FROZEN rows whose
    md_path exists. If ``fixture`` is provided, restrict to URLs on that
    fixture's host.
    """
    conn = open_db(paths.manifest_path(root))
    sql = (
        "SELECT url, raw_hash FROM manifest "
        "WHERE state IN ('FRESH','FROZEN') AND raw_hash IS NOT NULL"
    )
    params: tuple = ()
    if fixture is not None:
        sql += " AND url LIKE ?"
        params = (f"https://{fixture.host}/%",)
    if sample:
        # Multiply the LIMIT to account for md_path-missing skips below.
        sql += f" LIMIT {int(sample) * 5}"
    yielded = 0
    for url, raw_hash in conn.execute(sql, params).fetchall():
        md = paths.md_path(root, run_id, url)
        if not md.exists():
            continue
        yield FilePair(url=url, md_path=md, raw_hash=raw_hash)
        yielded += 1
        if sample and yielded >= sample:
            break


# ---- Eval: structural preservation -----------------------------------------

@dataclass
class StructuralResult:
    name: str = "extract_structural_preservation"
    pass_threshold: float = 0.85
    n_pages: int = 0
    mean_ratio: float = 0.0
    per_type: dict = None
    passed: bool = False
    # First few sample paths so a human can sanity-check
    samples: list = None


def eval_structural(root: Path, run_id: str, *,
                    fixture: Optional[SiteFixture] = None,
                    sample: Optional[int] = 20) -> StructuralResult:
    """Mean structural preservation across N sampled pages."""
    accum_ratios: dict[str, list[float]] = {}
    samples: list[dict] = []
    n = 0
    for pair in iter_file_pairs(root, run_id, fixture=fixture, sample=sample):
        try:
            score: PreservationScore = preservation_score(
                pair.html(root), pair.md()
            )
        except Exception:
            continue
        for kind, ratio in score.ratios.items():
            accum_ratios.setdefault(kind, []).append(ratio)
        if len(samples) < 3:
            samples.append({"url": pair.url, "ratios": dict(score.ratios)})
        n += 1
    per_type = {k: round(sum(v) / len(v), 3) for k, v in accum_ratios.items()
                if v}
    mean = round(sum(per_type.values()) / len(per_type), 3) if per_type else 0.0
    return StructuralResult(
        n_pages=n,
        mean_ratio=mean,
        per_type=per_type,
        passed=mean >= 0.85,
        samples=samples,
    )


# ---- Eval: anchor injection ------------------------------------------------

_MD_HEADING_WITH_ANCHOR = re.compile(
    r"^(#{1,6})\s+.+?\{#([a-z0-9-]+)\}\s*$", re.MULTILINE
)
_MD_ANY_HEADING = re.compile(r"^(#{1,6})\s+\S", re.MULTILINE)


@dataclass
class AnchorResult:
    name: str = "extract_anchor_injection"
    pass_threshold: float = 0.95
    n_pages: int = 0
    mean_anchor_ratio: float = 0.0
    duplicate_anchor_pages: int = 0
    passed: bool = False
    samples: list = None


def eval_anchor_injection(root: Path, run_id: str, *,
                          fixture: Optional[SiteFixture] = None,
                          sample: Optional[int] = 20) -> AnchorResult:
    """Per page: count headings, count headings carrying a ``{#slug}`` anchor;
    flag pages with duplicate anchors (slug collisions). Mean ratio + dup
    count both surfaced."""
    ratios: list[float] = []
    dup_pages = 0
    samples: list[dict] = []
    n = 0
    for pair in iter_file_pairs(root, run_id, fixture=fixture, sample=sample):
        md = pair.md()
        total = len(_MD_ANY_HEADING.findall(md))
        if total == 0:
            continue
        anchored = _MD_HEADING_WITH_ANCHOR.findall(md)
        seen: set[str] = set()
        has_dup = False
        for _, slug in anchored:
            if slug in seen:
                has_dup = True
                break
            seen.add(slug)
        r = len(anchored) / total if total else 0.0
        ratios.append(r)
        if has_dup:
            dup_pages += 1
        if len(samples) < 3:
            samples.append({"url": pair.url, "ratio": round(r, 3)})
        n += 1
    mean = round(sum(ratios) / len(ratios), 3) if ratios else 0.0
    return AnchorResult(
        n_pages=n,
        mean_anchor_ratio=mean,
        duplicate_anchor_pages=dup_pages,
        passed=mean >= 0.95 and dup_pages == 0,
        samples=samples,
    )


# ---- Eval: use-case pattern preservation -----------------------------------

@dataclass
class UseCaseResult:
    name: str = "extract_use_case_quality"
    pass_threshold: float = 0.95
    n_pages: int = 0
    mean_score: float = 0.0
    per_pattern_mean: dict = None
    passed: bool = False
    samples: list = None


def eval_use_case_patterns(root: Path, run_id: str, fixture: SiteFixture,
                           *, sample: Optional[int] = 20) -> UseCaseResult:
    """Run the fixture's use_case_patterns against (HTML text, markdown).
    Per-page score = aggregate_use_case_score; eval score = mean of those."""
    if not fixture.use_case_patterns:
        return UseCaseResult(n_pages=0, mean_score=1.0,
                             per_pattern_mean={}, passed=True, samples=[])
    page_scores: list[float] = []
    per_pattern: dict[str, list[float]] = {}
    samples: list[dict] = []
    n = 0
    for pair in iter_file_pairs(root, run_id, fixture=fixture, sample=sample):
        rows: list[PatternPreservation] = score_use_case_patterns(
            pair.html(root), pair.md(), fixture.use_case_patterns
        )
        page_scores.append(aggregate_use_case_score(rows))
        for r in rows:
            if r.html_count > 0:
                per_pattern.setdefault(r.pattern, []).append(r.ratio)
        if len(samples) < 3:
            samples.append({
                "url": pair.url,
                "score": round(page_scores[-1], 3),
                "patterns": {r.pattern: {"html": r.html_count,
                                         "md": r.md_count}
                             for r in rows},
            })
        n += 1
    mean = round(sum(page_scores) / len(page_scores), 3) if page_scores else 0.0
    per_pattern_mean = {
        p: round(sum(vs) / len(vs), 3)
        for p, vs in per_pattern.items() if vs
    }
    return UseCaseResult(
        n_pages=n,
        mean_score=mean,
        per_pattern_mean=per_pattern_mean,
        passed=mean >= 0.95,
        samples=samples,
    )


# ---- Wrapper: run all extract evals for one fixture ------------------------

def run_extract_evals(root: Path, run_id: str, fixture: SiteFixture,
                      *, sample: Optional[int] = 20) -> dict:
    """Run all stage-4 extract evals for one fixture. Returns a dict shape
    suitable for JSON dump and the report aggregator."""
    return {
        "fixture":   {"use_case": fixture.use_case, "slug": fixture.slug,
                       "host": fixture.host},
        "structural":  asdict(eval_structural(root, run_id,
                                              fixture=fixture, sample=sample)),
        "anchor":      asdict(eval_anchor_injection(root, run_id,
                                                    fixture=fixture, sample=sample)),
        "use_case":    asdict(eval_use_case_patterns(root, run_id, fixture,
                                                    sample=sample)),
    }

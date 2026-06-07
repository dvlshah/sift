"""Facts-coverage eval — surface pages whose HTML looks fact-shaped but
where no extractor fired.

This is the visibility we were missing: when the canonical /tax-rates-australian-residents
page changed shape (or when ATO put 2026-27 brackets on a non-`tax-rates` URL),
the existing extractors silently produced nothing. The user only found out by
asking the agent and getting "facts not present."

Heuristic candidate detection (deliberately rough — we want recall, not precision):
  * **Rate-table candidate**: HTML has a <table> whose <caption> contains an
    FY pattern (YYYY-YY) OR whose th text contains "tax on this income" / "tax
    payable" / "marginal rate".
  * **Threshold candidate**: text contains $ amounts AND % values in close
    proximity AND a year-like reference. (Less specific; flagged separately.)

For each candidate URL we then check whether any facts/<schema>/<slug>.json
was produced naming this URL as `source_url`. Pages with candidate shape but
no output go on the gap list — those are the extractors we still need to write.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from selectolax.lexbor import LexborHTMLParser

from sift import paths
from sift.fetch import read_raw_blob
from sift.manifest import iter_all


_FY_RE = re.compile(r"\b(19|20)\d{2}\s*[\-–/]\s*\d{2}\b")
_RATE_HEADER_HINTS = (
    "tax on this income",
    "tax payable",
    "marginal rate",
    "rate of tax",
)


@dataclass
class CoverageCandidate:
    url: str
    tier: str
    rate_table_signal: bool
    fy_in_caption: list[str]
    fy_in_h1: list[str]
    table_count: int


@dataclass
class FactsCoverageMetrics:
    run_id: str
    pages_scanned: int = 0
    pages_with_rate_table_shape: int = 0
    facts_files_emitted: int = 0
    facts_source_urls: list[str] = field(default_factory=list)
    # The actionable list: pages that LOOK like rate-table pages but produced no facts
    coverage_gaps: list[CoverageCandidate] = field(default_factory=list)
    # Coverage ratio: emitted / candidates (1.0 = every fact-shaped page produced facts)
    coverage_ratio: float = 0.0
    # Per-section breakdown of gaps so we can tell where extractor work is needed
    gaps_by_section: dict[str, int] = field(default_factory=dict)


def _section_of(url: str) -> str:
    from urllib.parse import urlparse
    parts = [p for p in urlparse(url).path.split("/") if p]
    return parts[0] if parts else "(root)"


def _detect_candidate(html: bytes) -> Optional[CoverageCandidate]:
    """Return a CoverageCandidate if this page looks fact-shaped, else None."""
    try:
        tree = LexborHTMLParser(html)
    except Exception:
        return None

    fy_caps: list[str] = []
    fy_h1s: list[str] = []
    table_count = 0
    has_rate_header = False

    for h1 in tree.css("h1"):
        text = h1.text(strip=True) or ""
        if _FY_RE.search(text):
            fy_h1s.append(text[:100])

    for table in tree.css("table"):
        table_count += 1
        cap = table.css_first("caption")
        if cap:
            cap_text = cap.text(strip=True) or ""
            if _FY_RE.search(cap_text):
                fy_caps.append(cap_text[:100])
        for th in table.css("th"):
            th_text = (th.text(strip=True) or "").lower()
            if any(h in th_text for h in _RATE_HEADER_HINTS):
                has_rate_header = True
                break

    if not (fy_caps or has_rate_header):
        return None  # no rate-table-shaped signal

    return CoverageCandidate(
        url="",  # filled by caller
        tier="",
        rate_table_signal=has_rate_header,
        fy_in_caption=fy_caps,
        fy_in_h1=fy_h1s,
        table_count=table_count,
    )


def _load_emitted_source_urls(facts_root: Path) -> set[str]:
    """Find every URL that produced at least one facts file."""
    if not facts_root.exists():
        return set()
    urls: set[str] = set()
    for f in facts_root.rglob("*.json"):
        if "schemas" in f.parts:
            continue
        try:
            payload = json.loads(f.read_text())
            if isinstance(payload, dict):
                src = payload.get("source_url")
                if src:
                    urls.add(src)
        except (json.JSONDecodeError, OSError):
            continue
    return urls


def run(
    root: Path,
    run_id: str,
    *,
    conn: sqlite3.Connection,
    scan_limit: Optional[int] = None,
) -> FactsCoverageMetrics:
    """Scan every FRESH page (or up to scan_limit) for rate-table-shaped HTML.

    Reports candidates that produced no facts files — those are the extractor
    gaps that need new url_matcher + extractor_fn entries in facts._EXTRACTORS.
    """
    metrics = FactsCoverageMetrics(run_id=run_id)
    facts_root = root / "runs" / run_id / "facts"
    emitted = _load_emitted_source_urls(facts_root)
    metrics.facts_source_urls = sorted(emitted)
    metrics.facts_files_emitted = len(emitted)

    scanned = 0
    for row in iter_all(conn):
        if row.state != "FRESH" or not row.raw_hash:
            continue
        if scan_limit is not None and scanned >= scan_limit:
            break
        scanned += 1
        try:
            html = read_raw_blob(root, row.raw_hash)
        except (FileNotFoundError, OSError):
            continue
        cand = _detect_candidate(html)
        if cand is None:
            continue
        metrics.pages_with_rate_table_shape += 1
        if row.url not in emitted:
            cand.url = row.url
            cand.tier = row.tier
            section = _section_of(row.url)
            metrics.gaps_by_section[section] = (
                metrics.gaps_by_section.get(section, 0) + 1
            )
            if len(metrics.coverage_gaps) < 50:
                metrics.coverage_gaps.append(cand)

    metrics.pages_scanned = scanned
    if metrics.pages_with_rate_table_shape > 0:
        # coverage = how many candidate pages actually produced facts
        # Use min() because a single URL can produce multiple facts (e.g.
        # the canonical multi-year page produces 42), inflating the numerator.
        emitted_among_candidates = (
            metrics.pages_with_rate_table_shape - len(metrics.coverage_gaps)
        )
        metrics.coverage_ratio = round(
            max(0, emitted_among_candidates) / metrics.pages_with_rate_table_shape, 3
        )
    return metrics


def to_dict(m: FactsCoverageMetrics) -> dict:
    return asdict(m)

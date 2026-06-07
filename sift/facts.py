"""Atomic structured facts as JSON. The high-stakes-numbers half of the corpus.

This module provides the **framework**:
  * `FactCandidate` dataclass + helpers
  * `extract_facts_for_url` — iterates the active profile's extractors
  * `build_all_facts` — orchestrates extraction across the corpus, writes
    JSON files + schemas to disk
  * Per-site utilities (parsing $ amounts, % rates, FY-from-body) reusable
    across extractors

The **schemas** and **extractor functions** live in the active SiteProfile:
  * `profile.facts_schemas` — `dict[$id, json_schema]`
  * `profile.facts_extractors` — `list[(url_matcher, extractor_fn)]`

The ATO reference extractors (rate tables) and the parsing helpers stay in
this file so multiple sites can reuse them (most tax authorities encode
brackets with $/% cells); a generic profile inherits an empty extractor list
and emits nothing.

`extract_individual_resident_brackets` is referenced by ATOProfile via
lazy import to avoid a circular dependency.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from selectolax.lexbor import LexborHTMLParser

from . import paths
from .classify import fy_years as fy_years_for
from .fetch import read_raw_blob
from .manifest import get_row
from .sites import current_profile


# Site-specific schemas come from the active profile. This module-level alias
# is read at import time; for runtime-changing profiles use `_schemas()`.
SCHEMAS: dict[str, dict] = current_profile().facts_schemas


def _schemas() -> dict[str, dict]:
    """Live-read the active profile's schemas. Use this in code that may
    execute after a profile swap (i.e. essentially everything below)."""
    return current_profile().facts_schemas

EXTRACTOR_VERSION = "facts-v3"

# Match "2025-26", "2025–26" (en-dash), "2025/26" — common ATO FY formats.
# Year start must look like a real FY (1990-2050 range).
_BODY_FY_RE = re.compile(r"\b((?:19|20)\d{2})[\-–/](\d{2})\b")


def fy_from_body(html: bytes, max_search_bytes: int = 200_000) -> Optional[str]:
    """Find a financial year in the page's <h1>, <title>, or first table-header.

    Used when the URL itself doesn't carry an FY (ATO's canonical rate pages
    drop the year from the URL slug). We only consider tags that authoritatively
    label the page; we do NOT scan body prose, because mentions like "for prior
    years see..." would mislead us.

    Returns None when no plausible FY found. Caller must skip rather than guess.
    """
    if not html:
        return None
    try:
        tree = LexborHTMLParser(html[:max_search_bytes])
    except Exception:
        return None

    # Priority order: most specific first.
    for selector in ("h1", "title", "table thead th", "table th", "h2"):
        for node in tree.css(selector):
            text = (node.text(strip=True) or "")
            m = _BODY_FY_RE.search(text)
            if m:
                start, end_2 = int(m.group(1)), int(m.group(2))
                if end_2 != (start + 1) % 100:
                    continue  # implausible — keep looking
                return f"{start}-{end_2:02d}"
    return None


# ---- Extractors -------------------------------------------------------------

@dataclass
class FactCandidate:
    """One row produced by an extractor: schema + payload + output filename slug."""
    schema: str
    slug: str          # filename component, e.g. "individual-resident-2025-26"
    payload: dict


# Currency: "$18,200" -> 18200
_DOLLAR_RE = re.compile(r"\$?\s*([\d,]+)")


def _dollars(s: str) -> Optional[int]:
    m = _DOLLAR_RE.search(s or "")
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


# Percent: "30%" or "30c per dollar" -> 0.30
_PERCENT_RE = re.compile(r"([\d.]+)\s*%")
_CENTS_RE = re.compile(r"([\d.]+)\s*c(?:ents?)?\s+(?:for each|per)\s+\$?1")


def _rate(s: str) -> Optional[float]:
    if not s:
        return None
    m = _PERCENT_RE.search(s)
    if m:
        try:
            return float(m.group(1)) / 100.0
        except ValueError:
            return None
    m = _CENTS_RE.search(s.lower())
    if m:
        try:
            return float(m.group(1)) / 100.0
        except ValueError:
            return None
    if s.strip().lower() in ("nil", "0", "$0", "0%", "no tax"):
        return 0.0
    return None


def _fy_from_caption(text: str) -> Optional[str]:
    """Extract an FY from a table caption like 'Resident tax rates 2025–26'."""
    if not text:
        return None
    m = _BODY_FY_RE.search(text)
    if not m:
        return None
    start, end_2 = int(m.group(1)), int(m.group(2))
    if end_2 != (start + 1) % 100:
        return None
    return f"{start}-{end_2:02d}"


def _parse_bracket_table(table) -> list[dict]:
    """Parse one HTML table into a brackets list. Returns [] if not a rate table."""
    brackets: list[dict] = []
    for tr in table.css("tr"):
        cells = [(td.text(strip=True) or "") for td in tr.css("td")]
        if len(cells) < 2:
            continue
        income_cell, tax_cell = cells[0], cells[1]
        il = income_cell.lower()
        band: Optional[tuple[int, Optional[int]]] = None
        m = re.search(r"\$?([\d,]+)\s*(?:[-–to]+)\s*\$?([\d,]+)", income_cell)
        if m:
            band = (int(m.group(1).replace(",", "")),
                    int(m.group(2).replace(",", "")))
        elif "over" in il or "and over" in il or "more than" in il:
            m = re.search(r"\$?([\d,]+)", income_cell)
            if m:
                band = (int(m.group(1).replace(",", "")), None)
        else:
            continue
        from_, to_ = band
        rate = _rate(tax_cell)
        base = _dollars(tax_cell) if "plus" in tax_cell.lower() else 0
        if rate is None:
            if "nil" in tax_cell.lower() or tax_cell.strip() in ("$0", "0"):
                rate = 0.0
                base = 0
            else:
                continue
        brackets.append({"from": from_, "to": to_, "rate": rate, "base": base})
    return brackets


def extract_individual_resident_brackets(
    *, url: str, html: bytes, fy: Optional[str], content_hash: str
) -> list[FactCandidate]:
    """Recognize ATO 'Resident tax rates' tables and produce a bracket list per table.

    A single page (e.g. /tax-rates-and-codes/tax-rates-australian-residents)
    holds many tables, one per FY. We emit one FactCandidate per table whose
    caption carries a recognizable FY. The `fy` argument is an override —
    if given, it's used only when the table caption is unreadable.

    Strict-by-design: tables we can't FY-tag are skipped silently rather than
    written under a guessed FY. Better to have zero facts than wrong facts.
    """
    try:
        tree = LexborHTMLParser(html)
    except Exception:
        return []

    out: list[FactCandidate] = []
    seen_slugs: set[str] = set()

    for table in tree.css("table"):
        headers = [(h.text(strip=True) or "").lower() for h in table.css("th")]
        joined = " | ".join(headers)
        if "taxable income" not in joined:
            continue
        if "tax on" not in joined and "tax payable" not in joined:
            continue

        # FY resolution per table: caption first (ATO's canonical multi-year page),
        # then URL/body FY as fallback for single-year pages.
        caption_node = table.css_first("caption")
        caption_fy = _fy_from_caption(caption_node.text(strip=True)) if caption_node else None
        this_fy = caption_fy or fy
        if this_fy is None:
            continue

        brackets = _parse_bracket_table(table)
        if len(brackets) < 2:
            continue

        slug = f"individual-resident-{this_fy}"
        if slug in seen_slugs:
            continue  # duplicate table for the same FY — keep the first
        seen_slugs.add(slug)

        start_y = int(this_fy.split("-")[0])
        out.append(FactCandidate(
            schema="ato-rate-table-v1",
            slug=slug,
            payload={
                "$schema": "ato-rate-table-v1",
                "source_url": url,
                "content_hash": f"sha256:{content_hash}",
                "fy": this_fy,
                "audience": "individual_resident",
                "brackets": brackets,
                "effective_from": f"{start_y}-07-01",
                "effective_to":   f"{start_y + 1}-06-30",
                "extractor_version": EXTRACTOR_VERSION,
            },
        ))
    return out


# Registry: (url-match predicate) -> extractor that returns list[FactCandidate]
def _is_individual_resident_rates(url: str) -> bool:
    """Match ATO's resident-rate pages. Permissive on purpose: the extractor
    is strict-by-design (it skips tables it can't FY-tag) so a too-wide URL
    match just means more empty calls, not wrong output."""
    p = urlparse(url).path.lower()
    if "tax-rates-and-codes" not in p:
        return False
    # Canonical + per-year + previous-years archive
    return any(s in p for s in (
        "tax-rates-australian-residents",
        "resident-tax-rates",
        "individual-income-tax-rates",
        "tax-tables-for-",
        "previous-years-tax-tables",
    ))


def _extractors() -> list[tuple[Callable[[str], bool], Callable]]:
    """Live-read the active profile's (url_matcher, extractor_fn) list.
    The framework calls this on every fact-extraction; no module-level
    constant so a profile swap takes effect immediately."""
    return current_profile().facts_extractors


def extract_facts_for_url(
    conn: sqlite3.Connection, root: Path, url: str
) -> list[FactCandidate]:
    """Run any matching extractors for `url` against its cached raw HTML.

    FY resolution: try the URL slug, then page body, then per-table caption
    inside the extractor. Each layer is a fallback for the next.
    Each extractor returns a list — a single page can produce multiple facts
    (e.g. the multi-year rate page yields one fact per FY).
    """
    row = get_row(conn, url)
    if row is None or not row.raw_hash or not row.content_hash:
        return []
    try:
        html = read_raw_blob(root, row.raw_hash)
    except (FileNotFoundError, OSError):
        return []
    fys = fy_years_for(url)
    fy_hint: Optional[str] = fys[0] if fys else fy_from_body(html)
    out: list[FactCandidate] = []
    for matches, extractor in _extractors():
        if not matches(url):
            continue
        # Extractors handle their own per-table FY resolution; the hint
        # is only used when no caption-level FY is present.
        out.extend(extractor(
            url=url, html=html, fy=fy_hint, content_hash=row.content_hash,
        ))
    return out


def build_all_facts(
    conn: sqlite3.Connection, root: Path, run_id: str
) -> dict[str, int]:
    """Run extractors over every FRESH/FROZEN URL; write facts/<schema>/<slug>.json.

    Also writes facts/schemas/<schema>.json once (so the agent can validate).
    Returns counts by schema.
    """
    fdir = paths.facts_dir(root, run_id)
    sdir = fdir / "schemas"
    sdir.mkdir(parents=True, exist_ok=True)
    for name, schema in _schemas().items():
        (sdir / f"{name}.json").write_text(
            json.dumps(schema, indent=2, sort_keys=True)
        )

    counts: dict[str, int] = {}
    from .manifest import iter_all
    for row in iter_all(conn):
        if row.state not in ("FRESH", "FROZEN"):
            continue
        for cand in extract_facts_for_url(conn, root, row.url):
            schema_dir = fdir / cand.schema
            schema_dir.mkdir(parents=True, exist_ok=True)
            (schema_dir / f"{cand.slug}.json").write_text(
                json.dumps(cand.payload, indent=2, sort_keys=True)
            )
            counts[cand.schema] = counts.get(cand.schema, 0) + 1
    return counts

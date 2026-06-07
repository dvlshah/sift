"""Structural quality eval — count headings, tables, links, paragraphs in
HTML vs markdown. Flags pages with abnormal ratios as candidates for the
LLM-judge eval to look at more carefully.

This is not a fidelity score — it's a quick anomaly detector. A page that
"lost half its links" might be fine (we strip nav). A page that "lost half
its tables" probably isn't, on a tax site.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from selectolax.lexbor import LexborHTMLParser

from sift import paths
from sift.fetch import read_raw_blob
from sift.manifest import get_row

from .sampler import sample_by_count


# Thresholds for flagging anomalies (lower = stricter). Reasonable defaults
# for ATO-shaped corpora.
HEADING_RATIO_FLOOR = 0.40    # md should keep >=40% of h2/h3 headings
TABLE_RATIO_FLOOR = 0.70      # tables matter on tax pages, be stricter
TEXT_RATIO_FLOOR = 0.10       # if md body is <10% of HTML visible text, suspicious
TEXT_RATIO_CEILING = 3.0      # if md body is 3x larger, we probably kept boilerplate


# Markdown counters
_MD_H2_RE = re.compile(r"^##\s", re.MULTILINE)
_MD_H3_RE = re.compile(r"^###\s", re.MULTILINE)
_MD_TABLE_ROW_RE = re.compile(r"^\|.+\|\s*$", re.MULTILINE)
_MD_LINK_RE = re.compile(r"\[[^\]]+\]\([^)]+\)")
_MD_LIST_RE = re.compile(r"^[\-*]\s", re.MULTILINE)
_MD_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)


@dataclass
class StructuralComparison:
    url: str
    tier: str
    # Raw counts
    html_h1: int = 0
    html_h2: int = 0
    html_h3: int = 0
    html_tables: int = 0
    html_links: int = 0
    html_list_items: int = 0
    html_text_chars: int = 0
    md_h2: int = 0
    md_h3: int = 0
    md_table_rows: int = 0
    md_links: int = 0
    md_list_items: int = 0
    md_text_chars: int = 0
    # Ratios (md / html, after capping)
    heading_ratio: float = 0.0
    table_ratio: float = 0.0
    link_ratio: float = 0.0
    text_ratio: float = 0.0
    flags: list[str] = field(default_factory=list)


@dataclass
class StructuralMetrics:
    run_id: str
    sample_size: int = 0
    pages_evaluated: int = 0
    pages_skipped: int = 0
    flagged_count: int = 0
    # Aggregate ratios (median across sample)
    median_heading_ratio: float = 0.0
    median_table_ratio: float = 0.0
    median_link_ratio: float = 0.0
    median_text_ratio: float = 0.0
    # Per-tier flag counts
    flags_by_tier: dict[str, int] = field(default_factory=dict)
    # Up to 20 example flagged pages with details
    flagged_examples: list[StructuralComparison] = field(default_factory=list)


def _count_html(html: bytes) -> tuple[int, int, int, int, int, int, int]:
    """Returns (h1, h2, h3, tables, links, list_items, visible_text_chars)."""
    try:
        tree = LexborHTMLParser(html)
    except Exception:
        return 0, 0, 0, 0, 0, 0, 0
    body = tree.body or tree
    # Remove script/style/nav/footer/header for a fair text-content comparison
    for node in body.css("script, style, nav, footer, header, aside"):
        node.decompose()
    h1 = len(body.css("h1"))
    h2 = len(body.css("h2"))
    h3 = len(body.css("h3"))
    tables = len(body.css("table"))
    links = len(body.css("a[href]"))
    list_items = len(body.css("li"))
    text_chars = len((body.text(strip=True) or ""))
    return h1, h2, h3, tables, links, list_items, text_chars


def _count_md(md_text: str) -> tuple[int, int, int, int, int, int]:
    """Returns (h2, h3, table_rows, links, list_items, body_text_chars).
    Strips the YAML frontmatter before counting."""
    body = _MD_FRONTMATTER_RE.sub("", md_text)
    return (
        len(_MD_H2_RE.findall(body)),
        len(_MD_H3_RE.findall(body)),
        len(_MD_TABLE_ROW_RE.findall(body)),
        len(_MD_LINK_RE.findall(body)),
        len(_MD_LIST_RE.findall(body)),
        len(body),
    )


def _ratio(num: int, den: int) -> float:
    if den <= 0:
        return 1.0 if num == 0 else float("inf")
    return round(num / den, 3)


def _compare_one(
    url: str, tier: str, html: bytes, md_text: str
) -> StructuralComparison:
    h1, h2, h3, tables, links, list_items, text_chars = _count_html(html)
    md_h2, md_h3, md_table_rows, md_links, md_list_items, md_chars = _count_md(md_text)

    heading_ratio = _ratio(md_h2 + md_h3, h2 + h3)
    # Tables: markdown table rows include header + separator, so > 2 rows = real table.
    # Compare "markdown tables present" vs "html tables present".
    md_table_count = max(0, (md_table_rows - tables) // 2 + tables) if md_table_rows else 0
    # Simpler: any md table rows means at least one table; pin md_table_count to >=1.
    md_table_count = 1 if md_table_rows > 2 else 0
    if tables == 0:
        table_ratio = 1.0  # nothing to preserve
    else:
        # Approximate: md should have at least some tables when html does.
        # Stricter signal: md_table_rows should be >= html-table count
        table_ratio = _ratio(md_table_rows, tables * 3)  # ~3 rows/table minimum

    link_ratio = _ratio(md_links, links)
    text_ratio = _ratio(md_chars, text_chars)

    flags: list[str] = []
    if (h2 + h3) >= 3 and heading_ratio < HEADING_RATIO_FLOOR:
        flags.append(f"low-headings({heading_ratio:.2f})")
    if tables >= 1 and table_ratio < TABLE_RATIO_FLOOR:
        flags.append(f"lost-tables({table_ratio:.2f})")
    if text_chars >= 500 and text_ratio < TEXT_RATIO_FLOOR:
        flags.append(f"thin-body({text_ratio:.2f})")
    if text_ratio > TEXT_RATIO_CEILING:
        flags.append(f"oversized-body({text_ratio:.2f})")

    return StructuralComparison(
        url=url, tier=tier,
        html_h1=h1, html_h2=h2, html_h3=h3,
        html_tables=tables, html_links=links,
        html_list_items=list_items, html_text_chars=text_chars,
        md_h2=md_h2, md_h3=md_h3,
        md_table_rows=md_table_rows, md_links=md_links,
        md_list_items=md_list_items, md_text_chars=md_chars,
        heading_ratio=heading_ratio,
        table_ratio=table_ratio,
        link_ratio=link_ratio,
        text_ratio=text_ratio,
        flags=flags,
    )


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    sorted_xs = sorted(x for x in xs if x != float("inf"))
    n = len(sorted_xs)
    if n == 0:
        return 0.0
    mid = n // 2
    return round(sorted_xs[mid] if n % 2 else (sorted_xs[mid - 1] + sorted_xs[mid]) / 2, 3)


def run(
    root: Path,
    run_id: str,
    *,
    conn: sqlite3.Connection,
    sample: int = 100,
) -> StructuralMetrics:
    rows = sample_by_count(conn, sample, label="structural", fresh_only=True)
    metrics = StructuralMetrics(run_id=run_id, sample_size=len(rows))

    heading_ratios: list[float] = []
    table_ratios: list[float] = []
    link_ratios: list[float] = []
    text_ratios: list[float] = []

    for row in rows:
        if not row.raw_hash:
            metrics.pages_skipped += 1
            continue
        try:
            html = read_raw_blob(root, row.raw_hash)
        except (FileNotFoundError, OSError):
            metrics.pages_skipped += 1
            continue
        md_path = paths.md_path(root, run_id, row.url)
        if not md_path.exists():
            metrics.pages_skipped += 1
            continue
        md_text = md_path.read_text(encoding="utf-8", errors="replace")

        cmp = _compare_one(row.url, row.tier, html, md_text)
        metrics.pages_evaluated += 1
        heading_ratios.append(cmp.heading_ratio)
        table_ratios.append(cmp.table_ratio)
        link_ratios.append(cmp.link_ratio)
        text_ratios.append(cmp.text_ratio)
        if cmp.flags:
            metrics.flagged_count += 1
            metrics.flags_by_tier[row.tier] = metrics.flags_by_tier.get(row.tier, 0) + 1
            if len(metrics.flagged_examples) < 20:
                metrics.flagged_examples.append(cmp)

    metrics.median_heading_ratio = _median(heading_ratios)
    metrics.median_table_ratio = _median(table_ratios)
    metrics.median_link_ratio = _median(link_ratios)
    metrics.median_text_ratio = _median(text_ratios)
    return metrics


def to_dict(m: StructuralMetrics) -> dict:
    return asdict(m)

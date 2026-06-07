"""ATO (Australian Taxation Office) site profile.

Concentrates every ATO-specific decision in one module so the rest of the
pipeline can stay site-agnostic. To target a different site, copy this file
to `sites/<name>.py`, replace the constants, override the methods, and
point `[site] profile` at the new class.

What's ATO-specific that lives here:

  * URL year regex (4-digit years embedded in slugs, often as FY ranges)
  * Audience map (their 9 top-level sections)
  * Tier rules (/media-centre/ → NEWS, /forms-and-instructions/ → CURRENT_FORMS,
    past-year embedded → FROZEN)
  * `parent_guide` extraction (multi-page guides under /forms-and-instructions/)
  * Default exclude patterns (their sitemap.xml, api endpoints, error stubs, SPA)
  * Dynamic-content patterns ("Last modified:", "QC ####" quick-codes,
    Commonwealth copyright year)
  * Section taxonomy + display labels (their 9 sections)
  * Facts schemas (rate tables, caps, deadlines, eligibility — all tax-themed)
  * Facts extractor (resident bracket parser for their rate-table HTML shape)

What stays generic (in classify/normalize/agent_surface/facts):

  * URL canonicalization (lowercase host, sort query, strip fragment)
  * Anchor injection (from heading text, deterministic slug)
  * Markdown frontmatter format
  * Hash normalization steps (NFC, line endings, blank-line collapse) —
    only the *patterns* are site-specific
  * Merkle root, chained changelog, integrity gates
  * Plan/fetch/extract/commit/publish phase orchestration
"""

from __future__ import annotations

import re
from typing import Callable, Optional
from urllib.parse import urlparse

from . import SiteProfile


# ---- ATO URL patterns ------------------------------------------------------

# Australian FY-style years (or any 4-digit year) embedded in URL path
# segments. ATO encodes years inside slugs: "foreign-income-2005",
# "capital-gains-tax-guide-2011", "fund-income-tax-return-2015-instructions",
# "2025-26-tax-rates". We accept the year preceded by `/` or `-` and
# followed by `/`, `-`, or end-of-path.
_YEAR_RE = re.compile(r"[/\-]((?:19|20)\d{2})(?=[/\-]|$)")


# Top-level URL segment -> audience label. Unknown segments → "general".
_AUDIENCE_MAP: dict[str, str] = {
    "individuals-and-families":     "individuals",
    "businesses-and-organisations": "businesses",
    "tax-and-super-professionals":  "professionals",
    "forms-and-instructions":       "forms",
    "media-centre":                 "news",
    "law":                          "legal",
    "calculators-and-tools":        "tools",
    "tax-rates-and-codes":          "rates",
    "about-ato":                    "ato",
}


# Default seed-time URL excludes. Each is a regex against URL path.
# /single-page-applications/ is intentionally NOT excluded — those URLs
# now route through the browser path via requires_browser() below.
DEFAULT_EXCLUDES: tuple[str, ...] = (
    r"^/sitemap(\.xml)?(/|$)",          # the sitemap itself
    r"^/api/",                           # JSON endpoints, not HTML
    r"^/print(/|$)",                     # print-friendly duplicates
    r"^/errors?(/|$)",                   # error pages
    r"^/page-unavailable(/|$)",          # template error pages
    r"^/whats-new(/|$)",                 # redirect stub
)


# Site-specific dynamic boilerplate that rotates without indicating real
# content change. These are stripped before hashing so a content_hash
# diff means actual content changed.
_DYNAMIC_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?im)^\s*Last\s+(?:modified|reviewed|updated)\s*:?.*$"),
    re.compile(r"(?im)^\s*Page\s+last\s+(?:modified|reviewed|updated)\s*:?.*$"),
    # ATO's QC quick-code that occasionally rotates across template revs
    re.compile(r"\bQC\s*\d{3,7}\b"),
    re.compile(r"(?i)\bSession\s*ID\s*:?\s*[A-Z0-9\-]+\b"),
    re.compile(r"(?i)\bPage\s*ID\s*:?\s*\d+\b"),
    # Strip the year from Commonwealth copyright line — content didn't change
    # just because we crossed a calendar boundary.
    re.compile(r"(©\s*Commonwealth\s+of\s+Australia)\s+\d{4}"),
)


# Top-level section taxonomy for INDEX.md. Ordered for display.
_SECTION_ORDER: list[tuple[str, str, str]] = [
    ("individuals-and-families",     "individuals",   "Individuals and families"),
    ("businesses-and-organisations", "businesses",    "Businesses and organisations"),
    ("tax-and-super-professionals",  "professionals", "Tax and super professionals"),
    ("forms-and-instructions",       "forms",         "Forms and instructions"),
    ("tax-rates-and-codes",          "rates",         "Tax rates and codes"),
    ("calculators-and-tools",        "tools",         "Calculators and tools"),
    ("law",                          "legal",         "Law and rulings"),
    ("media-centre",                 "news",          "Media centre"),
    ("about-ato",                    "ato",           "About the ATO"),
]


# ---- ATO Facts schemas + extractors ----------------------------------------

# Schemas tax-themed; structurally generic enough that an IRS / HMRC / etc.
# profile can reuse the rate-table shape with renamed audience enums.
_FACTS_SCHEMAS: dict[str, dict] = {
    "ato-rate-table-v1": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "ato-rate-table-v1",
        "type": "object",
        "required": ["$schema", "source_url", "content_hash", "fy",
                     "audience", "brackets", "effective_from", "effective_to"],
        "properties": {
            "$schema": {"const": "ato-rate-table-v1"},
            "source_url": {"type": "string", "format": "uri"},
            "content_hash": {"type": "string", "pattern": "^(sha256:[0-9a-f]{64}|manual:.*)$"},
            "fy": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}$"},
            "audience": {"type": "string",
                         "enum": ["individual_resident", "individual_foreign_resident",
                                  "individual_working_holiday_maker", "company"]},
            "brackets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["from", "rate"],
                    "properties": {
                        "from": {"type": "integer", "minimum": 0},
                        "to":   {"type": ["integer", "null"], "minimum": 0},
                        "rate": {"type": "number", "minimum": 0, "maximum": 1},
                        "base": {"type": "number", "minimum": 0},
                    },
                },
            },
            "effective_from": {"type": "string", "format": "date"},
            "effective_to":   {"type": "string", "format": "date"},
            "extractor_version": {"type": "string"},
            "notes": {"type": "string"},
        },
    },
    "ato-cap-threshold-v1": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "ato-cap-threshold-v1",
        "type": "object",
        "required": ["$schema", "source_url", "content_hash", "fy",
                     "name", "amount", "unit"],
        "properties": {
            "$schema": {"const": "ato-cap-threshold-v1"},
            "source_url": {"type": "string"},
            "content_hash": {"type": "string"},
            "fy": {"type": "string"},
            "name": {"type": "string"},
            "amount": {"type": "number"},
            "unit": {"type": "string",
                     "enum": ["AUD", "AUD_per_year", "percent", "count"]},
            "notes": {"type": "string"},
            "extractor_version": {"type": "string"},
        },
    },
    "ato-deadline-v1": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "ato-deadline-v1",
        "type": "object",
        "required": ["$schema", "source_url", "content_hash", "fy",
                     "name", "due_date", "applies_to"],
        "properties": {
            "$schema": {"const": "ato-deadline-v1"},
            "source_url": {"type": "string"},
            "content_hash": {"type": "string"},
            "fy": {"type": "string"},
            "name": {"type": "string"},
            "due_date": {"type": "string", "format": "date"},
            "applies_to": {"type": "string"},
            "notes": {"type": "string"},
            "extractor_version": {"type": "string"},
        },
    },
    "ato-eligibility-rule-v1": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "ato-eligibility-rule-v1",
        "type": "object",
        "required": ["$schema", "source_url", "content_hash",
                     "name", "criteria"],
        "properties": {
            "$schema": {"const": "ato-eligibility-rule-v1"},
            "source_url": {"type": "string"},
            "content_hash": {"type": "string"},
            "name": {"type": "string"},
            "fy": {"type": "string"},
            "criteria": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["condition"],
                    "properties": {
                        "condition": {"type": "string"},
                        "value": {},
                    },
                },
            },
            "extractor_version": {"type": "string"},
        },
    },
}


def _ato_extractors():
    """Lazy import to avoid a top-level cycle through facts.py."""
    from ..facts import (
        _is_individual_resident_rates,
        extract_individual_resident_brackets,
    )
    return [(_is_individual_resident_rates, extract_individual_resident_brackets)]


# ---- Profile ---------------------------------------------------------------


class ATOProfile(SiteProfile):
    name = "ato"
    primary_host = "www.ato.gov.au"

    # ---- Properties exposed to the pipeline --------------------------------

    @property
    def default_excludes(self) -> tuple[str, ...]:
        return DEFAULT_EXCLUDES

    @property
    def dynamic_patterns(self) -> tuple[re.Pattern[str], ...]:
        return _DYNAMIC_PATTERNS

    @property
    def section_order(self) -> list[tuple[str, str, str]]:
        return _SECTION_ORDER

    @property
    def facts_schemas(self) -> dict[str, dict]:
        return _FACTS_SCHEMAS

    @property
    def facts_extractors(self) -> list[tuple[Callable[[str], bool], Callable]]:
        return _ato_extractors()

    # ---- URL classification methods ----------------------------------------

    def classify_tier(self, url: str, current_year_start: int) -> str:
        path = urlparse(url).path
        year = self._extract_year(url)
        if year is not None and year < current_year_start:
            return "FROZEN"
        if path.startswith("/media-centre/"):
            return "NEWS"
        if path.startswith("/forms-and-instructions/"):
            return "CURRENT_FORMS"
        return "LIVING"

    def fy_years(self, url: str) -> list[str]:
        """ATO uses Australian FY 'YYYY-YY'. Single years in the URL are
        interpreted as the start year of an FY range."""
        path = urlparse(url).path
        found: list[str] = []
        seen: set[str] = set()
        for m in _YEAR_RE.finditer(path):
            start = int(m.group(1))
            tail = path[m.end():]
            tail_m = re.match(r"^-?(\d{2})(?=[/\-]|$)", tail)
            if tail_m:
                end_2 = int(tail_m.group(1))
                if end_2 == (start + 1) % 100:
                    fy = f"{start}-{end_2:02d}"
                else:
                    fy = f"{start}-{(start + 1) % 100:02d}"
            else:
                fy = f"{start}-{(start + 1) % 100:02d}"
            if fy not in seen:
                seen.add(fy)
                found.append(fy)
        return found

    def parent_guide(self, url: str) -> Optional[str]:
        path = urlparse(url).path
        if not path.startswith("/forms-and-instructions/"):
            return None
        parts = [seg for seg in path.split("/") if seg]
        return parts[1] if len(parts) >= 2 else None

    def audience(self, url: str) -> str:
        parts = [seg for seg in urlparse(url).path.split("/") if seg]
        if not parts:
            return "general"
        return _AUDIENCE_MAP.get(parts[0].lower(), "general")

    # ---- Browser-fetch routing --------------------------------------------

    def requires_browser(self, url: str) -> bool:
        """ATO's Legal Database (and other future SPAs) hydrate client-side.
        Routing them through the browser path is the only way trafilatura
        sees real content."""
        return urlparse(url).path.startswith("/single-page-applications/")

    def browser_config(self, url: str):
        """ATO's Legal DB SPA is Next.js with a calm post-load network
        (no third-party analytics drumming) — networkidle resolves cleanly
        in 5-8s and is the safest signal that Next.js hydration finished.
        The global default (domcontentloaded + 3s) returns before
        hydration completes, producing a shell HTML that breaks
        trafilatura extraction.

        Per-site browser_config() override is exactly the design's
        intended escape hatch for sites that genuinely need networkidle."""
        if not self.requires_browser(url):
            return None
        from ..browser import BrowserFetchConfig
        return BrowserFetchConfig(
            wait_until="networkidle",
            page_timeout_s=45.0,
            delay_before_return_html_s=0.0,  # networkidle implies done
        )

    # ---- Internal helpers --------------------------------------------------

    def _extract_year(self, url: str) -> Optional[int]:
        m = _YEAR_RE.search(urlparse(url).path)
        return int(m.group(1)) if m else None

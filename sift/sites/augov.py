"""Australian Government site profile (base class).

A reusable base for AU gov department / agency sites that share common
patterns observed across federal and state corpora:

  * **URL audience taxonomy** — most AU gov sites segregate content by
    audience using one of: /individuals, /business, /news (or /media),
    /about (or /governance), /consultation, /events, /resources,
    /forms (or /services), /legal (or /law / /legislation).

  * **Common exclude patterns** — Drupal user/admin paths, file system
    machinery (/system/files), search interfaces, sitemap themselves.

  * **Boilerplate normalization** — "Last reviewed/updated" timestamps,
    Commonwealth/State copyright footers with rotating years, ABN
    boilerplate strings.

Use directly for a generic AU gov site, or subclass to add site-specific
audience patterns, tier classification, and dynamic-pattern strips.
For sites with their own deep customization, see ATOProfile as the
reference subclass.

Empirical basis: derived from sitemap discovery + content-URL extraction
across 16 federal + state portals (see
``archive/au-gov-benchmark/sitemap_discovery.json``).
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from . import SiteProfile


# Default excludes — Drupal/CMS machinery + standard gov-site clutter.
DEFAULT_EXCLUDES: tuple[str, ...] = (
    r"^/sitemap(\.xml)?(/|$)",
    r"^/search(/|$)",
    r"^/api/",
    r"^/print(/|$)",
    r"^/_/",                          # Drupal internal
    r"^/admin(/|$)",
    r"^/login(/|$)",
    r"^/user(/|$)",                   # Drupal user pages
    r"^/system/files/",               # Drupal file system links
    r"^/cron(/|$)",
    r"^/feeds?(/|$)",
    r"^/rss(/|$)",
    r"^/node/\d+/edit",               # Drupal edit forms (if leaked)
)


# Strip rotating per-page boilerplate before hashing so a no-content-change
# rebuild doesn't churn content_hash.
DYNAMIC_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "Last reviewed: 15 May 2026" / "Page last updated 03/04/2026" / etc.
    re.compile(r"(?im)^\s*Last\s+(?:modified|reviewed|updated|published|amended)\s*:?.*$"),
    re.compile(r"(?im)^\s*Page\s+last\s+(?:modified|reviewed|updated)\s*:?.*$"),
    # Commonwealth / State copyright lines with year (year-only strip)
    re.compile(r"(©\s*Commonwealth\s+of\s+Australia)\s+\d{4}"),
    re.compile(r"(©\s*(?:Government|State)\s+of\s+(?:South\s+Australia|New\s+South\s+Wales|Victoria|Queensland|Western\s+Australia|Tasmania|Northern\s+Territory))\s+\d{4}"),
    # Drupal node IDs in footers ("Node ID: 12345")
    re.compile(r"(?i)\bNode\s*(?:ID|id)\s*:?\s*\d+\b"),
    # Build identifiers / session IDs that appear inline
    re.compile(r"(?i)\bBuild\s*(?:ID|version)\s*:?\s*[A-Za-z0-9_-]{6,}\b"),
)


# Audience patterns: substrings (matched in URL path) -> label.
# Order matters: more specific patterns first.
_AUDIENCE_PATTERNS: list[tuple[str, str]] = [
    ("/individuals-and-families", "individuals"),
    ("/individuals",              "individuals"),
    ("/people",                   "individuals"),
    ("/citizens",                 "individuals"),
    ("/families",                 "individuals"),

    ("/businesses-and-organisations", "businesses"),
    ("/business",                 "businesses"),
    ("/employers",                "businesses"),
    ("/companies",                "businesses"),
    ("/industry",                 "businesses"),

    ("/news",                     "news"),
    ("/media-centre",             "news"),
    ("/media-releases",           "news"),
    ("/media",                    "news"),
    ("/press-release",            "news"),
    ("/announcements",            "news"),

    ("/forms-and-instructions",   "forms"),
    ("/forms",                    "forms"),
    ("/services",                 "services"),

    ("/consultations",            "consultation"),
    ("/consultation",             "consultation"),

    ("/events",                   "events"),

    ("/resources",                "resources"),
    ("/publications",             "resources"),
    ("/reports",                  "resources"),

    ("/legislation",              "legal"),
    ("/legal-database",           "legal"),
    ("/law",                      "legal"),
    ("/legal",                    "legal"),

    ("/governance",               "about"),
    ("/about-us",                 "about"),
    ("/about-the-",               "about"),
    ("/about",                    "about"),

    ("/regulatory-resources",     "regulatory"),
    ("/regulation",               "regulatory"),
]


class AUGovProfile(SiteProfile):
    """Reusable base profile for Australian government sites.

    Operators can use this directly for a department/agency site that doesn't
    need site-specific tier or audience rules, or subclass to add overrides.
    HTTP-only by default (override ``requires_browser`` for SPA paths).

    Example::

        [site]
        profile = "sift.sites.augov:AUGovProfile"

        [seed]
        host_allow = ["www.example.gov.au"]
    """

    name = "au-gov"
    primary_host = ""  # operator sets via [seed].host_allow

    @property
    def default_excludes(self) -> tuple[str, ...]:
        return DEFAULT_EXCLUDES

    @property
    def dynamic_patterns(self) -> tuple[re.Pattern[str], ...]:
        return DYNAMIC_PATTERNS

    def audience(self, url: str) -> str:
        path = urlparse(url).path.lower()
        for fragment, label in _AUDIENCE_PATTERNS:
            if fragment in path:
                return label
        return "general"


class SAGovProfile(AUGovProfile):
    """South Australian state government sites.

    Extends AUGovProfile with audience labels driven by SA-specific hosts
    (RevenueSA, PlanSA, SAHealth, SAPOL) and path patterns where SA
    departments use distinct terminology.
    """

    name = "sa-gov"

    def audience(self, url: str) -> str:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        path = parsed.path.lower()
        # Host-driven audience (RevenueSA / PlanSA / SA Health / SAPOL)
        if "revenuesa" in host:
            return "tax"
        if "plansa" in host or "plan.sa.gov.au" in host:
            return "planning"
        if "sahealth" in host:
            return "health"
        if "police.sa.gov.au" in host or "sapol" in host:
            return "police"
        # Path-driven SA-specific patterns (when host is the master sa.gov.au)
        if "/revenue" in path:
            return "tax"
        if "/code-amendment" in path or "/planning-and-design-code" in path:
            return "planning"
        if "/development-applications" in path or path.startswith("/das"):
            return "planning"
        # Fall through to AUGovProfile patterns
        return super().audience(url)

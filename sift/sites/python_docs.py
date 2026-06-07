"""Python docs site profile (docs.python.org).

Reference profile for an official language documentation corpus.
Demonstrates:
  * Tier classification driven by version segments in the URL
    (3 = current = LIVING; 2 / pre-3 = FROZEN; future versions = LIVING)
  * Section-based audience taxonomy (library, tutorial, reference, …)
  * HTTP-path fetch — python.org docs are Sphinx-rendered static HTML

URL shape (canonical):
    https://docs.python.org/<version>/<section>/<page>.html
    e.g. /3/library/asyncio.html
         /3/tutorial/classes.html
         /3.12/whatsnew/3.12.html
         /2/library/string.html
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from . import SiteProfile


# Top-level docs section -> audience label.
# Anything under /<version>/<section>/...
_SECTION_AUDIENCE: dict[str, str] = {
    "library":     "stdlib",
    "reference":   "language-ref",
    "tutorial":    "tutorial",
    "howto":       "howto",
    "faq":         "faq",
    "whatsnew":    "release-notes",
    "extending":   "c-api",
    "c-api":       "c-api",
    "using":       "tooling",
    "distributing": "packaging",
    "installing":  "packaging",
    "glossary":    "glossary",
    "py-modindex": "index",
    "genindex":    "index",
}


_SECTION_ORDER: list[tuple[str, str, str]] = [
    ("tutorial",    "tutorial",     "Tutorial"),
    ("library",     "stdlib",       "Standard Library Reference"),
    ("reference",   "language-ref", "Language Reference"),
    ("howto",       "howto",        "HOWTOs"),
    ("whatsnew",    "release-notes", "What's New"),
    ("c-api",       "c-api",        "C API Reference"),
    ("extending",   "c-api",        "Extending Python"),
    ("using",       "tooling",      "Using Python"),
    ("distributing", "packaging",   "Distributing Modules"),
    ("installing",  "packaging",    "Installing Modules"),
    ("faq",         "faq",          "FAQs"),
]


DEFAULT_EXCLUDES: tuple[str, ...] = (
    r"^/sitemap(\.xml)?(/|$)",
    r"^/_sources/",        # Sphinx source dumps — not human-readable HTML
    r"^/_static/",          # CSS/JS/images
    r"^/_images/",
    r"^/objects\.inv$",     # Sphinx intersphinx inventory
    r"^/search\.html$",
    r"^/searchindex\.js$",
    r"^/genindex-all\.html$",  # 10MB+ alphabetical megapage
    r"^/dev(/|$)",          # in-development docs; bumps daily, separate corpus
)


# Sphinx ships build timestamps + git revision in the footer of every page.
# Strip before hashing so a no-content-change rebuild doesn't churn.
_DYNAMIC_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?im)^\s*Last\s+updated\s+on\s+.*$"),
    re.compile(r"(?i)Sphinx\s*\d+\.\d+\.\d+"),
    re.compile(r"(?i)build\s+from\s+commit\s+[a-f0-9]{6,}"),
)


# Match /<version>/... where version is "3", "3.12", "3.13", "2", etc.
_VERSION_RE = re.compile(r"^/(\d+(?:\.\d+)?)/")


class PythonDocsProfile(SiteProfile):
    """Python docs reference profile.

    Tier rules:
      * /2/... or /2.x/... → FROZEN (legacy; preserved for archival lookups)
      * /3/... → LIVING (current stable, the canonical reference)
      * /3.x/... where x ≤ current_year_start - 2008 → LIVING-ish; we keep
        these LIVING since python.org keeps the last few minor versions
        actively patched
      * anything else → LIVING (safe default)
    """

    name = "python-docs"
    primary_host = "docs.python.org"

    @property
    def default_excludes(self) -> tuple[str, ...]:
        return DEFAULT_EXCLUDES

    @property
    def dynamic_patterns(self) -> tuple[re.Pattern[str], ...]:
        return _DYNAMIC_PATTERNS

    @property
    def section_order(self) -> list[tuple[str, str, str]]:
        return _SECTION_ORDER

    def classify_tier(self, url: str, current_year_start: int) -> str:
        m = _VERSION_RE.match(urlparse(url).path)
        if not m:
            return "LIVING"
        version = m.group(1)
        major = version.split(".")[0]
        if major == "2":
            return "FROZEN"
        return "LIVING"

    def audience(self, url: str) -> str:
        """Map /<version>/<section>/... to a section label.
        URLs without a known section → 'reference'."""
        path = urlparse(url).path
        # Strip /<version>/ prefix; pull next segment
        m = re.match(r"^/\d+(?:\.\d+)?/([^/]+)", path)
        if not m:
            return "reference"
        return _SECTION_AUDIENCE.get(m.group(1).lower(), "reference")

    # HTTP-only. Sphinx output is static HTML, no JS needed.

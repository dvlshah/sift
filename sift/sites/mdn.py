"""MDN Web Docs site profile (developer.mozilla.org).

Reference profile for community technical reference. Demonstrates:
  * Locale-aware excludes (default to en-US; operators can override)
  * Deep audience taxonomy by /Web/<topic>/
  * HTTP-path fetch — MDN is server-rendered via Yari (Rust SSR)

URL shape (canonical):
    https://developer.mozilla.org/<locale>/docs/<area>/<topic>/<page>
    e.g. /en-US/docs/Web/JavaScript/Reference/Operators/Optional_chaining
         /en-US/docs/Web/CSS/grid-template-columns
         /en-US/docs/Web/API/Fetch_API

Out of scope for v1 of this profile:
  * Deprecation tier (MDN flags deprecated pages but exposes them under
    the same /Web/ tree — a per-page heuristic on the deprecation banner
    would be a separate initiative)
  * Browser-compat data extraction (the structured BCD JSON in
    /en-US/docs/Web/API/<thing>) — facts territory, deferred
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from . import SiteProfile


# /Web/<TOPIC>/... -> audience label.
# Casing matters: MDN URLs are TitleCase (e.g. /Web/JavaScript not /web/javascript).
_WEB_AUDIENCE_MAP: dict[str, str] = {
    "JavaScript":      "javascript",
    "CSS":             "css",
    "HTML":            "html",
    "API":             "web-api",
    "HTTP":            "http",
    "Accessibility":   "accessibility",
    "Performance":     "performance",
    "Privacy":         "privacy",
    "Security":        "security",
    "SVG":             "svg",
    "MathML":          "mathml",
    "WebAssembly":     "wasm",
    "Manifest":        "manifest",
    "Media":           "media",
    "XPath":           "xpath",
    "XSLT":            "xslt",
    "Guide":           "guide",
    "Tutorials":       "tutorials",
}


_SECTION_ORDER: list[tuple[str, str, str]] = [
    ("Web/JavaScript",   "javascript",    "JavaScript"),
    ("Web/CSS",          "css",           "CSS"),
    ("Web/HTML",         "html",          "HTML"),
    ("Web/API",          "web-api",       "Web APIs"),
    ("Web/HTTP",         "http",          "HTTP"),
    ("Web/Accessibility","accessibility", "Accessibility"),
    ("Web/Performance",  "performance",   "Performance"),
    ("Web/Security",     "security",      "Security"),
    ("Web/SVG",          "svg",           "SVG"),
    ("Web/WebAssembly",  "wasm",          "WebAssembly"),
]


DEFAULT_EXCLUDES: tuple[str, ...] = (
    # Non-en-US locales: keep the corpus monolingual by default. Operators
    # who want a specific locale set its host_allow + remove this exclude.
    r"^/(?!en-US/|sitemap|api/)",
    r"^/contribute(/|$)",
    r"^/plus(/|$)",
    r"^/observatory(/|$)",
    r"^/play(/|$)",
    r"^/blog(/|$)",
    r"^/curriculum(/|$)",
    r"^/community(/|$)",
)


# MDN's templated pages embed locale-rotated "Page last modified" strings
# and per-build revision IDs. Strip before hashing so docs-unchanged
# rebuilds don't churn content_hash.
_DYNAMIC_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?im)^\s*Page\s+last\s+modified\s*:?.*$"),
    re.compile(r"(?im)^\s*Last\s+modified\s*:?.*$"),
    re.compile(r"(?i)revision\s*:?\s*[a-f0-9]{6,}"),
)


class MDNProfile(SiteProfile):
    """MDN Web Docs reference profile.

    Defaults to en-US locale only. To index a different/additional locale,
    set ``[seed].extra_exclude_patterns`` to override the locale-gate and
    ``[seed].host_allow`` accordingly.
    """

    name = "mdn"
    primary_host = "developer.mozilla.org"

    @property
    def default_excludes(self) -> tuple[str, ...]:
        return DEFAULT_EXCLUDES

    @property
    def dynamic_patterns(self) -> tuple[re.Pattern[str], ...]:
        return _DYNAMIC_PATTERNS

    @property
    def section_order(self) -> list[tuple[str, str, str]]:
        return _SECTION_ORDER

    def audience(self, url: str) -> str:
        """Map /en-US/docs/Web/<TOPIC>/... to a topic label.
        Non-/Web/ paths or unmatched topics → 'reference'."""
        path = urlparse(url).path
        # Strip /<locale>/docs/ prefix; expect /en-US/docs/Web/<topic>/...
        m = re.match(r"^/[a-zA-Z-]+/docs/Web/([^/]+)", path)
        if not m:
            return "reference"
        return _WEB_AUDIENCE_MAP.get(m.group(1), "reference")

    # No tier classification — MDN doesn't expose a stable version axis
    # in URLs. Everything is LIVING (the base default).

    # HTTP-only. MDN's Yari serves complete HTML; no browser needed.

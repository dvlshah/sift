"""Stripe Docs site profile (docs.stripe.com).

Reference profile for a commercial SaaS API docs corpus. Demonstrates:
  * Audience labeling by top-level topic (api, payments, billing, connect, …)
  * Default excludes for marketing/blog content
  * HTTP-path fetch (no browser required) — Stripe ships server-rendered
    docs with substantial markup before hydration

Out of scope for v1 of this profile:
  * Tier classification beyond LIVING — Stripe doesn't version-tag docs URLs
    the way ATO/Python do, so a per-page heuristic isn't worth it yet
  * Facts extraction — Stripe's docs are prose-heavy with code blocks; the
    structured-facts opportunity is the API reference (rate limits, status
    codes) which is a separate initiative
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

from . import SiteProfile


# Top-level URL segment -> audience label. Unknown segments → "developers"
# (Stripe is dev-first; everything is documentation aimed at integrators).
_AUDIENCE_MAP: dict[str, str] = {
    "api":                    "api-reference",
    "payments":               "payments",
    "billing":                "billing",
    "connect":                "connect",
    "terminal":               "terminal",
    "treasury":               "treasury",
    "financial-connections":  "data",
    "issuing":                "issuing",
    "identity":               "identity",
    "atlas":                  "atlas",
    "tax":                    "tax",
    "radar":                  "radar",
    "sigma":                  "sigma",
    "climate":                "climate",
    "elements":               "elements",
    "checkout":               "checkout",
    "invoicing":              "invoicing",
    "webhooks":               "webhooks",
    "testing":                "testing",
    "cli":                    "cli",
    "sdks":                   "sdks",
}


# Section taxonomy for the agent-facing INDEX.md.
_SECTION_ORDER: list[tuple[str, str, str]] = [
    ("api",       "api-reference",  "API Reference"),
    ("payments",  "payments",       "Payments"),
    ("billing",   "billing",        "Billing & Subscriptions"),
    ("checkout",  "checkout",       "Checkout"),
    ("elements",  "elements",       "Stripe Elements"),
    ("connect",   "connect",        "Connect (Platforms)"),
    ("terminal",  "terminal",       "Terminal (In-Person)"),
    ("treasury",  "treasury",       "Treasury"),
    ("issuing",   "issuing",        "Issuing"),
    ("identity",  "identity",       "Identity Verification"),
    ("webhooks",  "webhooks",       "Webhooks"),
    ("testing",   "testing",        "Testing"),
    ("cli",       "cli",            "Stripe CLI"),
    ("sdks",      "sdks",           "SDKs & Libraries"),
]


# Path patterns the seed phase excludes by default.
DEFAULT_EXCLUDES: tuple[str, ...] = (
    r"^/blog(/|$)",
    r"^/jobs(/|$)",
    r"^/customers(/|$)",
    r"^/customer-references(/|$)",
    r"^/about(/|$)",
    r"^/legal(/|$)",
    r"^/privacy(/|$)",
    r"^/terms(/|$)",
    r"^/atlas/guides(/|$)",   # marketing-heavy, not API docs
    r"^/sitemap(\.xml)?(/|$)",
    r"^/search(/|$)",
)


# Stripe rotates per-build asset hashes and timestamp strings inline.
# Strip these so content_hash stays stable across no-content-change rebuilds.
_DYNAMIC_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)build\s+id\s*:?\s*[A-Za-z0-9_-]{6,}"),
    re.compile(r"(?im)^\s*Updated\s*:?\s*\d{4}-\d{2}-\d{2}.*$"),
)


class StripeDocsProfile(SiteProfile):
    """Stripe Docs reference profile.

    URL shape: ``https://docs.stripe.com/<topic>/<page>``
      e.g. ``/api/customers/object``, ``/payments/payment-intents``
    """

    name = "stripe-docs"
    primary_host = "docs.stripe.com"

    @property
    def default_excludes(self) -> tuple[str, ...]:
        return DEFAULT_EXCLUDES

    @property
    def dynamic_patterns(self) -> tuple[re.Pattern[str], ...]:
        return _DYNAMIC_PATTERNS

    @property
    def section_order(self) -> list[tuple[str, str, str]]:
        return _SECTION_ORDER

    def body_kind(self, url: str, *, content_type: Optional[str] = None) -> Optional[str]:
        # Stripe serves its `.md` docs variant as `text/plain`, so Content-Type
        # is an unreliable markdown signal here — the `.md` URL shape is. These
        # pages are already clean Markdown; trafilatura (the HTML path) mangles
        # them, so pass them through verbatim.
        if urlparse(url).path.lower().endswith(".md"):
            return "markdown"
        return super().body_kind(url, content_type=content_type)

    def audience(self, url: str) -> str:
        parts = [seg for seg in urlparse(url).path.split("/") if seg]
        if not parts:
            return "developers"
        return _AUDIENCE_MAP.get(parts[0].lower(), "developers")

    # Tier stays LIVING for everything — docs continuously update; no
    # FROZEN/CURRENT_FORMS distinction. classify_tier inherits the base
    # default (LIVING).

    # HTTP-only — Stripe docs are server-rendered. Browser path stays the
    # base-class default (returns False) so seed → plan → fetch uses httpx.

"""CVE.org site profile — vulnerability records via the CVE Services API.

The human page (``https://www.cve.org/CVERecord?id=CVE-…``) is a JavaScript
shell: the record is fetched client-side from the CVE Services API. sift routes
the page to that API (``https://cveawg.mitre.org/api/cve/CVE-…``) via the
``api_url`` acquisition transport — the canonical cve.org URL stays on the
manifest row and in citations, while the bytes come from the robots-allowed,
byte-deterministic JSON API. Extraction is the generic json-api strategy (the
CVE 5.1 record is already structured content); a per-profile field-map that
renders it as prose is a later lane.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import parse_qs, urlparse

from . import SiteProfile

# 'CVE-<4+ digit year>-<1+ digit sequence>', anchored + case-insensitive so a
# malformed or injected id can never build an API URL.
_CVE_ID = re.compile(r"^CVE-\d{4,}-\d+$", re.IGNORECASE)


class CVEProfile(SiteProfile):
    """Route cve.org record pages to the CVE Services API; defaults elsewhere."""

    name = "cve"
    primary_host = "www.cve.org"

    def api_url(self, url: str) -> Optional[str]:
        """``www.cve.org/CVERecord?id=CVE-Y-N`` -> the CVE Services API record URL.

        Returns ``None`` for every other cve.org URL (search, lists, a shell with
        no / more-than-one id) so only a resolvable single-record page routes to
        the API. The parsed id is upper-cased: canonicalization preserves case
        but the API serves the canonical 'CVE-' form.
        """
        parts = urlparse(url)
        if parts.netloc.lower() != "www.cve.org":
            return None
        if parts.path.rstrip("/").lower() != "/cverecord":
            return None
        ids = parse_qs(parts.query).get("id", [])
        if len(ids) != 1 or not _CVE_ID.match(ids[0]):
            return None
        return f"https://cveawg.mitre.org/api/cve/{ids[0].upper()}"

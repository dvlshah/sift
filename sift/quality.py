"""Content-quality heuristic for the tiered fetch transport.

The native HTTP fetcher's escalation was historically *status-only*: a 401/403
triggered the Firecrawl fallback, everything else was committed as-is. That
misses the dominant modern failure — a WAF or SPA that returns **HTTP 200 with
an empty shell or a JS-challenge interstitial**. Those pages have a success
status but ~no content, so they would be hashed and indexed as if real,
silently corrupting the corpus.

``looks_thin`` is the trigger that lets ``fetch_one`` route a 200-but-empty
response *up* the transport ladder (curl_cffi → Firecrawl/browser) instead of
committing junk. It is deliberately conservative: it only judges HTML, treats
substantial embedded data (Next.js ``__NEXT_DATA__``, JSON-LD) as real content,
and is a no-op when ``threshold <= 0`` so existing callers are byte-identical.
"""

from __future__ import annotations

import re

# Interstitial / challenge fingerprints. A 200 carrying any of these is a block
# page dressed as success — always thin regardless of length.
_CHALLENGE_MARKERS = (
    "just a moment",  # Cloudflare IUAM
    "attention required",  # Cloudflare block
    "cf-browser-verification",
    "checking your browser before",
    "_incapsula_resource",  # Imperva
    "pardon our interruption",  # Imperva/PerimeterX
    "please enable javascript",
    "enable javascript to run this app",  # bare CRA/Vite shell
    "access denied",  # Akamai reference page
    "request unsuccessful. incapsula",
    "px-captcha",  # PerimeterX/HUMAN
    "datadome",  # DataDome
    "verify you are human",
)

# Embedded-data markers. If present in volume, the content IS in the HTML
# (extractable downstream) even when visible text is sparse — do NOT escalate.
_DATA_MARKERS = (
    "__next_data__",
    "application/ld+json",
    "window.__nuxt__",
    "__apollo_state__",
    "self.__next_f",
)

_SCRIPT_STYLE_TAGS = re.compile(r"(?is)<script.*?</script>|<style.*?</style>|<[^>]+>")
_NEXT_DATA_BLOB = re.compile(r'(?is)id="__NEXT_DATA__"[^>]*>(.*?)</script>')


def visible_text_len(html: str) -> int:
    """Length of human-visible text after stripping scripts, styles, and tags."""
    stripped = _SCRIPT_STYLE_TAGS.sub(" ", html)
    return len(re.sub(r"\s+", " ", stripped).strip())


def looks_thin(body: bytes, content_type: str | None, threshold: int) -> bool:
    """True if ``body`` is an empty shell / challenge page that should escalate.

    Args:
        body: raw response bytes.
        content_type: response ``Content-Type`` (used to skip non-HTML).
        threshold: minimum visible-text chars; ``<= 0`` disables the check.

    Only HTML is judged — PDFs, JSON, XML, images are never "thin" (a valid PDF
    has no HTML text). Challenge fingerprints force thin=True; substantial
    embedded JSON forces thin=False.
    """
    if threshold <= 0:
        return False
    ct = (content_type or "").lower()
    if ct and "html" not in ct:
        return False  # non-HTML payloads are out of scope for the shell heuristic

    text = body.decode("utf-8", "ignore")
    low = text.lower()

    # Without a content-type hint, only judge things that actually look like HTML.
    if not ct and "<html" not in low and "<!doctype html" not in low:
        return False

    if any(m in low for m in _CHALLENGE_MARKERS):
        return True

    # Real embedded data present? Measure the Next.js payload specifically; for
    # other markers use a coarse size proxy. Either way, the content is there.
    m = _NEXT_DATA_BLOB.search(text)
    if m and len(m.group(1)) >= max(2048, threshold * 2):
        return False
    if any(d in low for d in _DATA_MARKERS) and len(text) >= threshold * 12:
        return False

    return visible_text_len(text) < threshold

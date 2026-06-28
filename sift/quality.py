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


# Markers that appear ONLY on a whole-page bot-challenge / block interstitial —
# never on a normal 200 page from the same vendor. We deliberately EXCLUDE vendor
# *protection* tags that ride on ordinary pages (measured live on real content):
# `/cdn-cgi/challenge-platform` (Cloudflare Bot-Fight / "JS Detections" injects it
# into every page's <head>) and a bare `datadome` substring (DataDome's
# client-side key is on every protected page). Those caused real false positives.
# All STRUCTURAL (script/class/iframe markup), never the visible block text — the
# structure-vs-content test below requires markers absent from the extracted prose,
# so a visible-text marker (e.g. "Request unsuccessful. Incapsula…") would be dead
# weight (always in the extracted text too). _incapsula_resource (the block iframe
# path) covers Incapsula structurally.
_CHALLENGE_PAGE_MARKERS = (
    "cf-browser-verification",  # Cloudflare IUAM page body class
    "cf-im-under-attack",       # Cloudflare IUAM page body class
    "window._cf_chl_opt",       # Cloudflare challenge options object
    "_incapsula_resource",      # Imperva/Incapsula block iframe path
    "px-captcha",               # PerimeterX / HUMAN captcha block element id
    "captcha-delivery",         # DataDome block iframe (geo.captcha-delivery.com)
)


def admit_content(
    raw: bytes, extracted_text: str | None, content_type: str | None
) -> tuple[bool, str]:
    """Trust-boundary gate at the extract step. Returns ``(admit, reason)``.

    Rejects a *non-empty* extraction whose RAW HTML carries a whole-page
    bot-challenge / block marker that is **not** present in the extracted text —
    i.e. the marker is page *structure* (a challenge script / block iframe), not
    something the page is *about*. ``looks_thin`` only *escalates* the fetch on
    these; when escalation is disabled or every transport tier is blocked, the
    interstitial reaches extract and would otherwise be hashed and signed as real
    content (the §6.2 trust-boundary hole).

    The structure-vs-content test makes the gate length-independent (a *verbose*
    interstitial is still caught) while never dropping a real article that merely
    *discusses* a bot-manager (the marker is then in the extracted prose, so the
    ``not in`` guard admits it). Combined with markers that never appear on a
    vendor's normal pages, the gate errs toward admitting — no false positives.

    Scope (a heuristic backstop, not comprehensive): Cloudflare IUAM/managed
    challenge, Incapsula, PerimeterX, and DataDome block pages. It does NOT cover
    Akamai / AWS WAF / Kasada / Turnstile, nor a challenge that renders its marker
    into visible text; per-site calibrated admission is future work. Empty
    extractions are already handled upstream (``reextract_and_hash`` ok=False).
    """
    if not extracted_text:
        return True, ""  # empty handled upstream; nothing to judge
    ct = (content_type or "").lower()
    if ct and "html" not in ct:
        return True, ""  # PDFs / JSON / images are never challenge pages
    low = raw.decode("utf-8", "ignore").lower()
    text_low = extracted_text.lower()
    for m in _CHALLENGE_PAGE_MARKERS:
        # In the raw HTML (page structure) but NOT in the extracted content:
        # a challenge script/iframe, not an article discussing the vendor.
        if m in low and m not in text_low:
            return False, "admission-challenge-page"
    return True, ""

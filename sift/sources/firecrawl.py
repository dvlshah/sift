"""Firecrawl integration for sift — two narrow integration points.

1. **Discovery via ``/v2/map``** (``walk_firecrawl_map`` + ``FirecrawlMapSource``).
   Used by ``sift seed --from-firecrawl-map`` to seed URLs on sites where
   ``sitemap.xml`` is missing (``docs.github.com``, ``www.w3.org``),
   sparse-by-design (``docs.python.org`` lists only version roots), or
   bot-blocked at the edge (Cloudflare 403 on ``help.shopify.com``).

2. **Fetch fallback via ``/v2/scrape``** (``firecrawl_scrape`` +
   ``FirecrawlScrapePool`` + ``FIRECRAWL_FETCHER_VERSION``).
   Used by ``sift fetch --firecrawl-fallback`` (or ``[crawl.firecrawl] enabled``)
   to escalate single URLs that get 401/403 from the native HTTP fetcher.
   Firecrawl renders the page through its own proxy stack and returns HTML;
   sift feeds that HTML through the normal extract pipeline so
   ``content_hash`` provenance stays under sift's control.

Both integration points are deliberately direct (no generic provider
abstraction), each owning its own payload shape, error mapping, and live-wire
shape quirks. The shape divergence between ``/v2/map`` (``{success, links}``,
top-level) and ``/v2/scrape`` (``{success, data: {html, metadata}}``,
``data``-wrapped) is real and documented inline in each function.

Scope:

* No SDK dependency — both endpoints are hit via ``httpx`` (already a sift
  dep). One fewer install for sift users.
* Auth from ``FIRECRAWL_API_KEY`` env (or explicit ``api_key=`` arg). We avoid
  config-file secrets deliberately.
* No conditional GET / ETag at the upstream level — Firecrawl doesn't proxy
  our ``If-None-Match`` and the API ETag is per-response not per-content.
  The scrape pool sets ``maxAge=0`` by default for freshness.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional, Sequence

import httpx

from . import SeedSource

if TYPE_CHECKING:
    from ..config import FirecrawlScrapeConfig
    from ..fetch import FetchInput, FetchResult

_DEFAULT_API_BASE = "https://api.firecrawl.dev"
_DEFAULT_LIMIT = 500
_DEFAULT_TIMEOUT = 60.0


def _api_base() -> str:
    """Resolve the Firecrawl API base URL.

    Defaults to the hosted cloud endpoint. Operators can flip to a self-hosted
    Firecrawl instance by exporting ``FIRECRAWL_API_BASE`` (e.g.
    ``http://localhost:3002``). Note: self-hosted Firecrawl lacks the
    Fire-engine bot-bypass stack — fine for ``/v2/map`` discovery and
    clean-site scraping, NOT a substitute for the cloud scrape API when the
    point is bypassing Cloudflare/Akamai.
    """
    return os.environ.get("FIRECRAWL_API_BASE", _DEFAULT_API_BASE).rstrip("/")

# Firecrawl's own probe URLs occasionally leak into /map output; strip them
# so they don't pollute the manifest.
_PROBE_MARKERS = ("fc_probe", "dryrun=", "pool_concurrency", "fc_fetch")

# Version stamp on FetchResults produced by FirecrawlScrapePool. Mirrors
# BROWSER_VERSION's role: bumping it invalidates Firecrawl-fetched rows on
# the next plan cycle. Reuses the ``browser_version`` column because the
# semantics are the same — a non-HTTP renderer; rename to ``fetcher_version``
# is a v2-schema follow-on.
FIRECRAWL_FETCHER_VERSION = "firecrawl-2026-05"


class FirecrawlError(RuntimeError):
    """Raised on Firecrawl ``/map`` or ``/scrape`` failures the operator should
    fix. Wrapped HTTP errors and response-shape mismatches surface here with a
    short actionable hint, so callers can warn cleanly rather than bubble a
    raw stack trace.
    """


class FirecrawlBudgetExhausted(FirecrawlError):
    """Raised when a fetch_all run has spent its ``max_credits_per_run`` budget.
    The caller (typically ``fetch_one``) preserves the native failure rather
    than attempting further escalations."""


def _auth_headers(key: Optional[str]) -> dict[str, str]:
    """Compose the request headers. ``Authorization`` is included only when a
    key was resolved — self-hosted Firecrawl instances often run without auth."""
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def _status_hint(code: int) -> str:
    """Map common Firecrawl HTTP statuses to actionable hints.

    Shared by ``walk_firecrawl_map`` and ``firecrawl_scrape``. Empirically
    discovered — the docs page is silent on error response shapes, so each
    code here was either observed live or inferred from API conventions.
    """
    if code == 401:
        return " — FIRECRAWL_API_KEY rejected; check the key is current"
    if code == 402:
        return " — Firecrawl quota exceeded; check your plan at firecrawl.dev"
    if code in (429, 503):
        return " — rate-limited or upstream busy; retry shortly"
    return ""


def _resolve_api_key(explicit: Optional[str]) -> Optional[str]:
    """Resolve the Firecrawl API key.

    Returns ``None`` (no auth needed) when the API base is non-cloud (i.e.
    operator pointed ``FIRECRAWL_API_BASE`` at a self-hosted instance), since
    self-hosted Firecrawl runs without auth by default. Raises
    ``FirecrawlError`` only when the cloud endpoint is targeted and no key is
    configured — that's a misconfiguration the operator must fix.
    """
    if explicit:
        return explicit
    env = os.environ.get("FIRECRAWL_API_KEY")
    if env:
        return env
    if _api_base() != _DEFAULT_API_BASE:
        return None
    raise FirecrawlError(
        "FIRECRAWL_API_KEY is not set. Get a key at https://firecrawl.dev "
        "and export it (e.g. `export FIRECRAWL_API_KEY=fc-...`), or point "
        "FIRECRAWL_API_BASE at your self-hosted instance."
    )


def _firecrawl_post(endpoint: str, payload: dict, *, key: Optional[str], timeout: float) -> dict:
    """POST to a Firecrawl v2 ``endpoint`` (bare name, e.g. ``"map"`` /
    ``"scrape"``) and return the parsed, success-checked JSON envelope. Raises
    :class:`FirecrawlError` on any HTTP / non-JSON / ``success=False`` failure,
    preserving the native error and the ``/{endpoint}`` wording callers' tests
    pin."""
    try:
        with httpx.Client(timeout=timeout) as c:
            resp = c.post(
                f"{_api_base()}/v2/{endpoint}",
                headers=_auth_headers(key),
                json=payload,
            )
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        raise FirecrawlError(
            f"Firecrawl /{endpoint} failed (HTTP {code}){_status_hint(code)}"
        ) from e
    except httpx.HTTPError as e:
        raise FirecrawlError(f"Firecrawl /{endpoint} failed: {e}") from e

    try:
        body = resp.json()
    except ValueError as e:
        raise FirecrawlError(f"Firecrawl /{endpoint} returned non-JSON: {e}") from e

    if not body.get("success"):
        raise FirecrawlError(
            f"Firecrawl /{endpoint} returned success=False: {body.get('error') or body}"
        )
    return body


def walk_firecrawl_map(
    url: str,
    *,
    api_key: Optional[str] = None,
    limit: int = _DEFAULT_LIMIT,
    search: Optional[str] = None,
    include_subdomains: bool = False,
    timeout: float = _DEFAULT_TIMEOUT,
) -> list[tuple[str, Optional[str]]]:
    """Call Firecrawl ``POST /v2/map`` and return discovered URLs.

    Shape matches ``walk_sitemap`` — list of ``(url, lastmod)`` tuples with
    ``lastmod=None`` (Firecrawl /map doesn't expose it). Probe artifacts that
    occasionally leak into ``/map`` results are filtered out.

    Raises ``FirecrawlError`` on auth / HTTP / response-shape failures, with
    a hint pointing at the most likely cause.
    """
    key = _resolve_api_key(api_key)
    payload: dict = {"url": url, "limit": int(limit)}
    if search:
        payload["search"] = search
    if include_subdomains:
        payload["includeSubdomains"] = True

    body = _firecrawl_post("map", payload, key=key, timeout=timeout)

    # /v2/map returns ``links`` at the top level (verified empirically against
    # the live API on 2026-05-31). Some older shapes and SDK wrappers (e.g.
    # the firecrawl CLI's JSON output) nest under ``data.links``. Accept
    # either so we stay resilient to upstream wrapper changes.
    links = body.get("links")
    if links is None:
        links = (body.get("data") or {}).get("links") or []
    out: list[tuple[str, Optional[str]]] = []
    for link in links:
        if not isinstance(link, dict):
            continue
        u = (link.get("url") or "").strip()
        if not u or any(m in u for m in _PROBE_MARKERS):
            continue
        out.append((u, None))
    return out


class FirecrawlMapSource(SeedSource):
    """SeedSource adapter over ``walk_firecrawl_map``."""

    name = "firecrawl-map"

    def __init__(
        self,
        url: str,
        *,
        api_key: Optional[str] = None,
        limit: int = _DEFAULT_LIMIT,
        search: Optional[str] = None,
        include_subdomains: bool = False,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.limit = limit
        self.search = search
        self.include_subdomains = include_subdomains

    def discover(self) -> Iterable[tuple[str, Optional[str]]]:
        yield from walk_firecrawl_map(
            self.url,
            api_key=self.api_key,
            limit=self.limit,
            search=self.search,
            include_subdomains=self.include_subdomains,
        )


# ---- /v2/scrape: fetch fallback for bot-blocked sites ----------------------


def firecrawl_scrape(
    url: str,
    *,
    api_key: Optional[str] = None,
    formats: Sequence[str] = ("html",),
    proxy: str = "auto",
    max_age_ms: int = 0,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict:
    """Call Firecrawl ``POST /v2/scrape`` and return the parsed JSON envelope.

    The response shape is ``{success: bool, data: {html, markdown, metadata,
    ...}}`` — note ``data``-wrapped, **unlike** ``/v2/map`` which returns
    ``links`` at the top level. The two endpoints disagree on shape within
    the same API; verified live against the production endpoint.

    Defaults aimed at the bot-block fallback use case:

    * ``formats=["html"]`` so sift's normalizer/extractor owns ``content_hash``
      determinism end-to-end. Firecrawl's own markdown is bypassed.
    * ``maxAge=0`` to always fetch fresh from the origin; Firecrawl's
      server-side cache otherwise serves up to its TTL.
    * ``proxy="auto"`` so Firecrawl picks the right strategy per-site
      (their ``"enhanced"`` proxy is what actually defeats Cloudflare-class
      detection; ``"auto"`` picks it when needed without flat-charging the
      enhanced rate everywhere).

    Raises ``FirecrawlError`` on auth/HTTP/JSON-shape failures with an
    actionable hint — caller maps that to "preserve native failure" semantics.
    """
    key = _resolve_api_key(api_key)
    payload: dict = {
        "url": url,
        "formats": list(formats),
        "maxAge": int(max_age_ms),
        "proxy": proxy,
    }

    return _firecrawl_post("scrape", payload, key=key, timeout=timeout)


class FirecrawlScrapePool:
    """Async pool for Firecrawl ``/v2/scrape`` calls within a single
    ``fetch_all`` run. Mirrors ``BrowserPool``'s shape: shared concurrency
    semaphore + shared rate limiter + shared budget counter, one instance per
    run, ``aclose`` for symmetry.

    The pool resolves ``FIRECRAWL_API_KEY`` at construction so missing keys
    fail loudly at startup — not silently mid-run when the first escalation
    fires.

    Caller pattern (in ``fetch_one`` retry loop):

    .. code-block:: python

        if firecrawl_pool is not None and result.status in pool.fallback_statuses:
            try:
                return await firecrawl_pool.fetch(inp, root)
            except FirecrawlError:
                pass   # native result stands; budget counter still advanced
                       # on any HTTP attempt Firecrawl charged us for.
    """

    def __init__(
        self,
        cfg: "FirecrawlScrapeConfig",
        *,
        api_key: Optional[str] = None,
    ) -> None:
        self.cfg = cfg
        # Resolve at construction — failing here surfaces the misconfiguration
        # at startup rather than at the first escalation, which may be hours
        # into a multi-thousand-URL run.
        self._api_key = _resolve_api_key(api_key)
        self._sem = asyncio.Semaphore(cfg.concurrency)
        # aiolimiter is already a dep via fetch.py — same rate-limiter shape
        # as the per-host HTTP limiter, just per Firecrawl account.
        from aiolimiter import AsyncLimiter
        self._limiter = AsyncLimiter(max_rate=cfg.rate_per_sec, time_period=1.0)
        # Atomic-reservation lock for ``_credits_used``. Without this, N
        # concurrent fetches all pass the budget check before any of them
        # increments the counter — observed in the bench as 95 credits spent
        # against a 30-credit budget on W3C, 157/30 on Shopify.
        self._budget_lock = asyncio.Lock()
        self._credits_used = 0
        self._calls_attempted = 0
        self._calls_succeeded = 0

    @property
    def credits_used(self) -> int:
        return self._credits_used

    @property
    def calls_attempted(self) -> int:
        return self._calls_attempted

    @property
    def calls_succeeded(self) -> int:
        return self._calls_succeeded

    @property
    def fallback_statuses(self) -> tuple[int, ...]:
        return self.cfg.fallback_statuses

    def budget_remaining(self) -> int:
        return max(0, self.cfg.max_credits_per_run - self._credits_used)

    def _per_call_reservation(self) -> int:
        """Pessimistic per-call credit reservation used by the atomic pre-flight
        check. ``"basic"`` proxy is documented at 1 credit; ``"stealth"`` and
        ``"auto"`` (which may escalate to stealth) bill up to 5. Reserving the
        upper bound up-front guarantees we never overshoot — actual cost is
        settled from ``metadata.creditsUsed`` after the call returns."""
        return 1 if self.cfg.proxy == "basic" else 5

    async def fetch(self, inp: "FetchInput", root: Path) -> "FetchResult":
        """Scrape ``inp.url`` via Firecrawl, write the raw blob, return a
        FetchResult mirroring the native success path. Raises
        ``FirecrawlBudgetExhausted`` when the credit budget is spent,
        ``FirecrawlError`` on any other failure — caller catches and
        preserves the original native failure result.
        """
        # Lazy import to break the circular dep with sift.fetch (which itself
        # imports FirecrawlScrapePool).
        from ..fetch import FetchResult, store_body
        from ..manifest import now_utc

        # Atomic check + reservation. We pre-charge ``reserved`` credits
        # (worst case per call) so concurrent fetchers can't all race past a
        # stale ``budget_remaining()`` reading. After the call we settle by
        # ``actual - reserved`` (typically a refund). If the call raises before
        # billing, we refund the full reservation in the finally block.
        reserved = self._per_call_reservation()
        async with self._budget_lock:
            if self._credits_used + reserved > self.cfg.max_credits_per_run:
                raise FirecrawlBudgetExhausted(
                    f"Firecrawl budget exhausted "
                    f"({self._credits_used}/{self.cfg.max_credits_per_run} credits used)"
                )
            self._credits_used += reserved
            self._calls_attempted += 1

        try:
            async with self._sem:
                async with self._limiter:
                    # Run the sync HTTP call in a worker thread — Firecrawl's
                    # server latency dominates (3-30s), so spending a thread on
                    # it is fine and keeps the rest of the event loop
                    # responsive.
                    body = await asyncio.to_thread(
                        firecrawl_scrape,
                        inp.url,
                        api_key=self._api_key,
                        formats=("html",),
                        proxy=self.cfg.proxy,
                        max_age_ms=self.cfg.max_cache_age_ms,
                        timeout=self.cfg.timeout_sec,
                    )
        except Exception:
            # No HTTP round-trip completed (DNS/timeout/auth) → refund the
            # reservation. This is safe because errors from
            # ``firecrawl_scrape`` mean Firecrawl never billed us.
            async with self._budget_lock:
                self._credits_used -= reserved
            raise

        # Settle the reservation to the actual cost reported by Firecrawl.
        # Even if downstream validation (statusCode / data.html) rejects the
        # response and we raise below, Firecrawl already billed us — the
        # settled count must reflect that so the next iteration's budget
        # check sees reality.
        data = body.get("data") or {}
        meta = data.get("metadata") or {}
        # Honor a legitimate creditsUsed:0 — a cache hit under
        # max_cache_age_ms>0 costs nothing, and the whole point of caching is
        # to not pay credits. The old ``or 1`` mis-charged those as 1, so the
        # budget under-reported and the run stopped short of its real cap.
        # Default to 1 only when the field is absent/null/non-numeric.
        cu = meta.get("creditsUsed")
        try:
            actual = int(cu) if cu is not None else 1
        except (TypeError, ValueError):
            actual = 1
        async with self._budget_lock:
            # Net across reserve(+reserved) then settle is +actual. The max(0,…)
            # floor is an explicit invariant — the credit counter (and thus
            # budget_remaining) can never read negative under any interleaving.
            self._credits_used = max(0, self._credits_used + (actual - reserved))

        # CRITICAL VALIDATION: Firecrawl can return success=True while the
        # origin returned a 403 challenge page. Without checking statusCode,
        # we would commit the Cloudflare "verify you are human" HTML as if it
        # were real content — silently corrupting the index. This check is
        # the load-bearing correctness gate of the whole fallback.
        origin_status = meta.get("statusCode")
        if origin_status not in (200, 301, 302):
            raise FirecrawlError(
                f"Firecrawl reported origin statusCode={origin_status} for "
                f"{inp.url} — treating as fetch failure"
            )

        # Defensive shape fallback for the same reason walk_firecrawl_map has
        # one: Firecrawl could (and historically has) wrap responses
        # inconsistently. Prefer the documented `data.html`; fall back to a
        # hypothetical top-level shape.
        html = data.get("html") or body.get("html")
        if not html:
            raise FirecrawlError(
                f"Firecrawl /scrape response missing data.html for {inp.url}"
            )

        # Write the raw blob exactly like the native HTTP path — the extract
        # phase reads identical input shape, and content_hash is computed by
        # sift's deterministic normalizer (not Firecrawl's markdown).
        body_bytes = html.encode("utf-8")
        raw_hash, n_bytes = store_body(root, body_bytes)

        self._calls_succeeded += 1
        return FetchResult(
            url=inp.url,
            decision=inp.decision,
            status=int(origin_status),
            etag=None,             # Firecrawl doesn't pass origin ETag through
            last_modified=None,    # same — see module docstring
            raw_hash=raw_hash,
            raw_bytes=n_bytes,
            fetched_at=now_utc(),
            error=None,
            browser_version=FIRECRAWL_FETCHER_VERSION,
            content_type=meta.get("contentType") or "text/html",
        )

    async def aclose(self) -> None:
        """No persistent state to release — present for symmetry with
        BrowserPool. The sync ``httpx.Client`` inside ``firecrawl_scrape`` is
        created and closed per call."""
        return

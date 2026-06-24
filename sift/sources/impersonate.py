"""Tier-2 escalation transport: a TLS-fingerprint-impersonating HTTP client.

Most "hardened" sites (Cloudflare/Akamai/Imperva bot managers) block sift's
native ``httpx`` fetcher on the **TLS + HTTP/2 fingerprint**, not on JavaScript.
A browser-like User-Agent doesn't help — the JA3/JA4 handshake still says
"Python". ``curl_cffi`` (libcurl + curl-impersonate) replays a real Chrome's
TLS/H2 fingerprint, so those edges serve the page. Empirically this clears the
sites that even browser-*headers* on httpx could not (Allianz, Vitality), and
the UA-only blocks (UnitedHealthcare) — all for **$0, no browser, self-hosted**.

It sits between native httpx (tier 1) and the paid Firecrawl / browser
fallback (tier 3). It raises :class:`EscalateError` whenever it can't produce
*good* content (block status, still-thin body, off-allowlist redirect), so
``fetch_one``'s ladder falls through to the next tier instead of committing
junk. Optional dep: ``pip install 'sift-engine[impersonate]'``.

Mirrors :class:`~sift.sources.firecrawl.FirecrawlScrapePool` (shared sem +
limiter + version stamp + ``fetch(inp, root)``) so the two are swappable rungs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlparse

if TYPE_CHECKING:
    from ..config import ImpersonateConfig
    from ..fetch import FetchInput, FetchResult

# Version stamp on FetchResults this pool produces. Reuses the ``browser_version``
# column (same semantics as FIRECRAWL_FETCHER_VERSION — a non-vanilla transport);
# bumping it invalidates impersonate-fetched rows on the next plan cycle.
CURL_CFFI_FETCHER_VERSION = "impersonate-2026-06"


class EscalateError(RuntimeError):
    """Raised when this tier can't return good content, so the ladder should try
    the next transport. Carries a short reason for telemetry/logs."""


class CurlCffiScrapePool:
    """Async pool for curl_cffi impersonation fetches within one ``fetch_all``
    run. Free (no credit budget, unlike Firecrawl); bounded only by a shared
    concurrency semaphore + rate limiter so we stay polite.

    ``fetch`` runs the synchronous curl_cffi call in a worker thread (libcurl is
    blocking), applies the same SSRF / size / content-quality gates as the
    native path, and on any failure raises :class:`EscalateError` so the caller
    falls through to the paid/browser tier.
    """

    def __init__(self, cfg: "ImpersonateConfig") -> None:
        self.cfg = cfg
        self._sem = asyncio.Semaphore(cfg.concurrency)
        from aiolimiter import AsyncLimiter

        self._limiter = AsyncLimiter(max_rate=cfg.rate_per_sec, time_period=1.0)
        self._calls_attempted = 0
        self._calls_succeeded = 0

    @property
    def calls_attempted(self) -> int:
        return self._calls_attempted

    @property
    def calls_succeeded(self) -> int:
        return self._calls_succeeded

    @property
    def escalate_statuses(self) -> tuple[int, ...]:
        return self.cfg.escalate_statuses

    def _get(self, url: str):
        """Synchronous impersonating GET. Returns a curl_cffi Response, or None
        on any transport-level failure (DNS, timeout, TLS, connection reset)."""
        try:
            from curl_cffi import requests as creq  # optional dep, imported lazily
        except ImportError as e:  # pragma: no cover - exercised via clear message
            raise EscalateError(
                "curl_cffi not installed — `pip install 'sift-engine[impersonate]'`"
            ) from e
        try:
            return creq.get(
                url,
                impersonate=self.cfg.impersonate,
                timeout=self.cfg.timeout_sec,
                allow_redirects=True,
            )
        except Exception:
            return None

    async def fetch(
        self,
        inp: "FetchInput",
        root: Path,
        *,
        allowed_hosts: Optional[frozenset[str]] = None,
    ) -> "FetchResult":
        """Impersonation-fetch ``inp.url``, write the raw blob, return a
        FetchResult mirroring the native success path. Raises
        :class:`EscalateError` on block status / thin body / off-allowlist
        redirect / transport failure so the ladder continues."""
        from ..fetch import FetchResult, MAX_BODY_BYTES, store_body
        from ..manifest import now_utc
        from ..quality import looks_thin

        self._calls_attempted += 1
        async with self._sem:
            async with self._limiter:
                resp = await asyncio.to_thread(self._get, inp.url)

        if resp is None:
            raise EscalateError("impersonate transport failure")
        status = getattr(resp, "status_code", 0)
        if status < 200 or status >= 300:
            raise EscalateError(f"impersonate http-{status}")

        # SSRF: curl_cffi followed redirects, so validate the FINAL host against
        # the allow-list before storing — same guard as the native 2xx path.
        if allowed_hosts is not None:
            final_host = (urlparse(str(resp.url)).netloc or "").lower()
            if final_host and final_host not in allowed_hosts:
                raise EscalateError(f"impersonate redirect-off-allowlist:{final_host}")

        body = (
            resp.content
            if isinstance(resp.content, bytes)
            else resp.content.encode("utf-8")
        )
        if len(body) > MAX_BODY_BYTES:
            raise EscalateError(f"impersonate body-too-large:{len(body)}")

        ct = resp.headers.get("content-type")
        if looks_thin(body, ct, self.cfg.thin_text_threshold):
            # Same shell/challenge the native path saw — let the next tier (a
            # real browser / Firecrawl) try to render it.
            raise EscalateError("impersonate still-thin")

        raw_hash, n_bytes = store_body(root, body)
        self._calls_succeeded += 1
        return FetchResult(
            url=inp.url,
            decision=inp.decision,
            status=int(status),
            etag=resp.headers.get("etag"),
            last_modified=resp.headers.get("last-modified"),
            raw_hash=raw_hash,
            raw_bytes=n_bytes,
            fetched_at=now_utc(),
            error=None,
            browser_version=CURL_CFFI_FETCHER_VERSION,
            content_type=ct,
        )

    async def aclose(self) -> None:
        """Symmetry with the other pools; curl_cffi sessions are per-call."""
        return

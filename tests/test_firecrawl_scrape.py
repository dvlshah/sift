"""Firecrawl /v2/scrape fallback fetcher — wire-level + integration coverage.

Uses httpx.MockTransport to intercept REST calls so tests run offline. Same
discipline as test_sources_firecrawl.py: fixtures derived from real curl traces
of /v2/scrape, not from SDK output, to avoid a repeat of the /v2/map shape bug.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from sift.config import FirecrawlScrapeConfig
from sift.fetch import FetchInput, FetchResult, USER_AGENT
from sift.sources import firecrawl as fc_mod
from sift.sources.firecrawl import (
    FIRECRAWL_FETCHER_VERSION,
    FirecrawlBudgetExhausted,
    FirecrawlError,
    FirecrawlScrapePool,
    firecrawl_scrape,
)


# ---- helpers ---------------------------------------------------------------

def _proxy_client(handler) -> type:
    class _ProxyClient(httpx.Client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)
    return _ProxyClient


def _scrape_response(*, html: str = "<html><body>ok</body></html>",
                     origin_status: int = 200, credits: int = 1,
                     content_type: str = "text/html",
                     wrap_in_data: bool = True) -> httpx.Response:
    """Build a /v2/scrape success envelope. Defaults to the real /v2/scrape
    shape (`data`-wrapped); pass ``wrap_in_data=False`` to exercise the
    defensive top-level fallback for resilience."""
    payload: dict = {
        "html": html,
        "metadata": {
            "statusCode": origin_status,
            "contentType": content_type,
            "creditsUsed": credits,
            "sourceURL": "https://x.test/page",
        },
    }
    body = {"success": True, "data": payload} if wrap_in_data else {"success": True, **payload}
    return httpx.Response(200, json=body)


# ---- firecrawl_scrape REST: payload + auth + response shape ----------------

class TestScrapeRest:
    def test_payload_has_required_defaults(self, monkeypatch):
        seen = {}

        def handler(req):
            seen["url"] = str(req.url)
            seen["headers"] = dict(req.headers)
            seen["body"] = json.loads(req.content)
            return _scrape_response()

        monkeypatch.setattr(httpx, "Client", _proxy_client(handler))
        firecrawl_scrape("https://x.test/page", api_key="fc-x")
        assert seen["url"].endswith("/v2/scrape")
        assert seen["body"]["url"] == "https://x.test/page"
        # Defaults aligned with the bot-block-fallback use case:
        assert seen["body"]["formats"] == ["html"]
        assert seen["body"]["maxAge"] == 0          # always fresh from origin
        assert seen["body"]["proxy"] == "auto"      # Firecrawl picks per-site
        assert seen["headers"]["authorization"] == "Bearer fc-x"

    def test_custom_options_forwarded(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(httpx, "Client", _proxy_client(
            lambda req: (seen.update(body=json.loads(req.content)), _scrape_response())[-1]
        ))
        firecrawl_scrape("https://x.test", api_key="fc-x",
                         formats=("html", "markdown"),
                         proxy="enhanced", max_age_ms=3_600_000)
        assert seen["body"]["formats"] == ["html", "markdown"]
        assert seen["body"]["proxy"] == "enhanced"
        assert seen["body"]["maxAge"] == 3_600_000

    def test_response_data_wrapped_shape(self, monkeypatch):
        # The real /v2/scrape shape: {success, data: {html, metadata, ...}}
        monkeypatch.setattr(httpx, "Client", _proxy_client(
            lambda req: _scrape_response(html="<html>real</html>", wrap_in_data=True)
        ))
        body = firecrawl_scrape("https://x.test", api_key="fc-x")
        assert body["data"]["html"] == "<html>real</html>"
        assert body["data"]["metadata"]["statusCode"] == 200

    def test_api_key_from_env_when_unset(self, monkeypatch):
        monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-env")
        seen = {}
        monkeypatch.setattr(httpx, "Client", _proxy_client(
            lambda req: (seen.update(headers=dict(req.headers)), _scrape_response())[-1]
        ))
        firecrawl_scrape("https://x.test")
        assert seen["headers"]["authorization"] == "Bearer fc-env"

    def test_missing_key_against_cloud_raises(self, monkeypatch):
        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
        monkeypatch.delenv("FIRECRAWL_API_BASE", raising=False)
        with pytest.raises(FirecrawlError, match="FIRECRAWL_API_KEY is not set"):
            firecrawl_scrape("https://x.test")

    def test_self_hosted_no_key_works(self, monkeypatch):
        # When FIRECRAWL_API_BASE is non-cloud, missing API key is OK
        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
        monkeypatch.setenv("FIRECRAWL_API_BASE", "http://localhost:3002")
        seen = {}
        monkeypatch.setattr(httpx, "Client", _proxy_client(
            lambda req: (seen.update(url=str(req.url), headers=dict(req.headers)),
                         _scrape_response())[-1]
        ))
        firecrawl_scrape("https://x.test")
        assert "localhost:3002" in seen["url"]
        assert "authorization" not in seen["headers"]  # no auth on self-hosted

    @pytest.mark.parametrize("code,marker", [
        (401, "FIRECRAWL_API_KEY rejected"),
        (402, "quota exceeded"),
        (429, "rate-limited"),
        (503, "rate-limited"),
        (500, "HTTP 500"),
    ])
    def test_http_status_mapping(self, monkeypatch, code, marker):
        monkeypatch.setattr(httpx, "Client", _proxy_client(
            lambda req: httpx.Response(code, content=b"")
        ))
        with pytest.raises(FirecrawlError, match=marker):
            firecrawl_scrape("https://x.test", api_key="fc-x")

    def test_success_false_raises(self, monkeypatch):
        monkeypatch.setattr(httpx, "Client", _proxy_client(
            lambda req: httpx.Response(200, json={"success": False, "error": "nope"})
        ))
        with pytest.raises(FirecrawlError, match="success=False"):
            firecrawl_scrape("https://x.test", api_key="fc-x")


# ---- FirecrawlScrapePool: budget, validation, FetchResult shape ------------

class TestScrapePool:
    def _cfg(self, **kwargs) -> FirecrawlScrapeConfig:
        defaults = dict(enabled=True, max_credits_per_run=10,
                        rate_per_sec=100.0, concurrency=4)
        defaults.update(kwargs)
        return FirecrawlScrapeConfig(**defaults)

    async def test_fetch_returns_FetchResult_with_firecrawl_version(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(httpx, "Client", _proxy_client(
            lambda req: _scrape_response(html="<html><body>real</body></html>")
        ))
        pool = FirecrawlScrapePool(self._cfg(), api_key="fc-x")
        inp = FetchInput(url="https://x.test/page", decision="FETCH",
                         etag=None, last_modified=None)
        result = await pool.fetch(inp, tmp_path)
        assert isinstance(result, FetchResult)
        assert result.status == 200
        assert result.browser_version == FIRECRAWL_FETCHER_VERSION
        assert result.content_type == "text/html"
        assert result.raw_hash and result.raw_bytes > 0
        # Body written to the raw store at the expected content-addressed path
        from sift import paths
        assert paths.raw_path(tmp_path, result.raw_hash).exists()

    async def test_validates_origin_statusCode(self, tmp_path, monkeypatch):
        # Firecrawl returns success=True but origin returned 403 (challenge page) —
        # this is the LOAD-BEARING correctness check. Must reject.
        monkeypatch.setattr(httpx, "Client", _proxy_client(
            lambda req: _scrape_response(html="<html>cf-challenge</html>", origin_status=403)
        ))
        pool = FirecrawlScrapePool(self._cfg(), api_key="fc-x")
        inp = FetchInput(url="https://x.test/page", decision="FETCH",
                         etag=None, last_modified=None)
        with pytest.raises(FirecrawlError, match="origin statusCode=403"):
            await pool.fetch(inp, tmp_path)
        # Credits charged anyway — Firecrawl billed us regardless
        assert pool.credits_used == 1

    async def test_credit_accumulation(self, tmp_path, monkeypatch):
        responses = iter([
            _scrape_response(credits=1),
            _scrape_response(credits=2),  # "enhanced" proxy costs more
            _scrape_response(credits=1),
        ])
        monkeypatch.setattr(httpx, "Client", _proxy_client(
            lambda req: next(responses)
        ))
        pool = FirecrawlScrapePool(self._cfg(), api_key="fc-x")
        for url in ("https://x.test/a", "https://x.test/b", "https://x.test/c"):
            await pool.fetch(FetchInput(url=url, decision="FETCH", etag=None,
                                        last_modified=None), tmp_path)
        assert pool.credits_used == 4
        assert pool.calls_succeeded == 3
        assert pool.calls_attempted == 3

    async def test_budget_exhausted_raises(self, tmp_path, monkeypatch):
        # "basic" proxy reserves 1 credit per call so this small-budget test
        # exercises the per-call settlement rather than the upper-bound
        # reservation. (Atomic-reservation concurrency is exercised by
        # ``test_concurrent_fetches_never_overshoot`` below.)
        monkeypatch.setattr(httpx, "Client", _proxy_client(
            lambda req: _scrape_response(credits=1)
        ))
        pool = FirecrawlScrapePool(
            self._cfg(max_credits_per_run=2, proxy="basic"), api_key="fc-x",
        )
        # First 2 fetches succeed
        for url in ("https://x.test/a", "https://x.test/b"):
            await pool.fetch(FetchInput(url=url, decision="FETCH", etag=None,
                                        last_modified=None), tmp_path)
        # 3rd raises FirecrawlBudgetExhausted
        with pytest.raises(FirecrawlBudgetExhausted, match="budget exhausted"):
            await pool.fetch(FetchInput(url="https://x.test/c", decision="FETCH",
                                        etag=None, last_modified=None), tmp_path)
        assert pool.budget_remaining() == 0

    async def test_concurrent_fetches_never_overshoot(self, tmp_path, monkeypatch):
        """Regression: pre-fix, N concurrent fetches all read ``credits_used``
        before any of them incremented, so the budget was exceeded by up to
        ``concurrency × credits-per-call``. The bench saw 95/30 on W3C and
        157/30 on Shopify. The atomic reservation must hold a strict cap."""
        import asyncio

        # Each call costs the max (5 credits) — the worst case. With budget=10
        # and concurrency=4, the pre-fix code would let all 4 calls through
        # (using 20). The post-fix code admits at most 2 (reserving 5 each
        # under the lock).
        monkeypatch.setattr(httpx, "Client", _proxy_client(
            lambda req: _scrape_response(credits=5)
        ))
        pool = FirecrawlScrapePool(
            self._cfg(max_credits_per_run=10, concurrency=4, proxy="auto"),
            api_key="fc-x",
        )

        async def attempt(i):
            try:
                await pool.fetch(
                    FetchInput(url=f"https://x.test/{i}", decision="FETCH",
                               etag=None, last_modified=None),
                    tmp_path,
                )
                return "ok"
            except FirecrawlBudgetExhausted:
                return "exhausted"

        # Launch 8 concurrent fetches against a 10-credit budget.
        results = await asyncio.gather(*(attempt(i) for i in range(8)))

        # The hard cap MUST hold: total credits used never exceeds the budget.
        assert pool.credits_used <= 10, (
            f"budget overshoot: used={pool.credits_used} > 10"
        )
        # Exactly 2 calls fit (each reserving 5) → 6 must report exhausted
        assert results.count("ok") == 2
        assert results.count("exhausted") == 6

    async def test_failed_call_refunds_reservation(self, tmp_path, monkeypatch):
        """A pre-billing failure (timeout / DNS / auth) must refund the
        reserved credits so the next call still has the same budget."""
        # First call: network error (httpx raises before any Firecrawl billing)
        # Second call: succeeds normally
        responses = iter([
            httpx.Response(503, json={"error": "upstream-busy"}),
            _scrape_response(credits=1),
        ])
        monkeypatch.setattr(httpx, "Client", _proxy_client(
            lambda req: next(responses)
        ))
        pool = FirecrawlScrapePool(
            self._cfg(max_credits_per_run=5, proxy="basic"), api_key="fc-x",
        )

        with pytest.raises(FirecrawlError):
            await pool.fetch(FetchInput(url="https://x.test/a", decision="FETCH",
                                        etag=None, last_modified=None), tmp_path)
        # Reservation refunded — counter at 0 even though one call attempted.
        assert pool.credits_used == 0

        await pool.fetch(FetchInput(url="https://x.test/b", decision="FETCH",
                                    etag=None, last_modified=None), tmp_path)
        assert pool.credits_used == 1

    async def test_missing_html_raises(self, tmp_path, monkeypatch):
        # success=True, statusCode=200, but no html field → FirecrawlError
        monkeypatch.setattr(httpx, "Client", _proxy_client(
            lambda req: httpx.Response(200, json={
                "success": True,
                "data": {"metadata": {"statusCode": 200, "creditsUsed": 1}},
            })
        ))
        pool = FirecrawlScrapePool(self._cfg(), api_key="fc-x")
        with pytest.raises(FirecrawlError, match="missing data.html"):
            await pool.fetch(FetchInput(url="https://x.test/page", decision="FETCH",
                                        etag=None, last_modified=None), tmp_path)

    async def test_aclose_is_noop_safe(self):
        pool = FirecrawlScrapePool(self._cfg(), api_key="fc-x")
        await pool.aclose()      # should not raise


# ---- fetch_one integration: try-then-fallback ------------------------------

class TestFetchOneFallback:
    """The end-to-end escalation: a native 403 should hand off to the pool,
    a native 200 should never consult the pool, and a Firecrawl error should
    preserve the native failure result."""

    async def test_native_403_escalates_to_firecrawl(self, tmp_path, monkeypatch):
        from sift.fetch import fetch_one
        from aiolimiter import AsyncLimiter
        import asyncio

        # Native httpx returns 403, MockTransport on the sync Client returns
        # a successful /v2/scrape envelope.
        async def native_handler(req):
            return httpx.Response(403, content=b"<html>blocked</html>")

        monkeypatch.setattr(httpx, "Client", _proxy_client(
            lambda req: _scrape_response(html="<html>via-firecrawl</html>")
        ))
        pool = FirecrawlScrapePool(
            FirecrawlScrapeConfig(enabled=True, max_credits_per_run=5,
                                  rate_per_sec=100.0, concurrency=4),
            api_key="fc-x",
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(native_handler)) as client:
            limiter = AsyncLimiter(max_rate=100.0)
            sem = asyncio.Semaphore(4)
            inp = FetchInput(url="https://blocked.test/page", decision="FETCH",
                             etag=None, last_modified=None)
            result = await fetch_one(client, inp, tmp_path, limiter, sem,
                                     retries=0, firecrawl_pool=pool)
        assert result.status == 200
        assert result.browser_version == FIRECRAWL_FETCHER_VERSION
        assert result.error is None
        assert pool.calls_succeeded == 1

    async def test_native_200_does_not_consult_pool(self, tmp_path, monkeypatch):
        from sift.fetch import fetch_one
        from aiolimiter import AsyncLimiter
        import asyncio

        # If the pool's httpx.Client is ever called, this raises. Confirms no escalation.
        def boom(req):
            raise AssertionError("pool should not be consulted on 200")
        monkeypatch.setattr(httpx, "Client", _proxy_client(boom))

        async def native_ok(req):
            return httpx.Response(200, content=b"<html>fine</html>",
                                  headers={"content-type": "text/html"})

        pool = FirecrawlScrapePool(
            FirecrawlScrapeConfig(enabled=True, max_credits_per_run=5,
                                  rate_per_sec=100.0, concurrency=4),
            api_key="fc-x",
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(native_ok)) as client:
            limiter = AsyncLimiter(max_rate=100.0)
            sem = asyncio.Semaphore(4)
            result = await fetch_one(
                client,
                FetchInput(url="https://ok.test/page", decision="FETCH",
                           etag=None, last_modified=None),
                tmp_path, limiter, sem, retries=0, firecrawl_pool=pool,
            )
        assert result.status == 200
        assert result.browser_version is None       # native, not firecrawl
        assert pool.calls_attempted == 0

    async def test_firecrawl_failure_preserves_native_403(self, tmp_path, monkeypatch):
        from sift.fetch import fetch_one
        from aiolimiter import AsyncLimiter
        import asyncio

        async def native_403(req):
            return httpx.Response(403, content=b"")
        # Pool's client returns success=False so pool.fetch raises FirecrawlError
        monkeypatch.setattr(httpx, "Client", _proxy_client(
            lambda req: httpx.Response(200, json={"success": False, "error": "boom"})
        ))
        pool = FirecrawlScrapePool(
            FirecrawlScrapeConfig(enabled=True, max_credits_per_run=5,
                                  rate_per_sec=100.0, concurrency=4),
            api_key="fc-x",
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(native_403)) as client:
            limiter = AsyncLimiter(max_rate=100.0)
            sem = asyncio.Semaphore(4)
            result = await fetch_one(
                client,
                FetchInput(url="https://blocked.test/page", decision="FETCH",
                           etag=None, last_modified=None),
                tmp_path, limiter, sem, retries=0, firecrawl_pool=pool,
            )
        # Native 403 stands; counter advanced for telemetry
        assert result.status == 403
        assert result.error == "http-403"
        assert pool.calls_attempted == 1
        assert pool.calls_succeeded == 0

    async def test_pool_absent_means_403_stays_403(self, tmp_path):
        from sift.fetch import fetch_one
        from aiolimiter import AsyncLimiter
        import asyncio

        async def native_403(req):
            return httpx.Response(403, content=b"")

        async with httpx.AsyncClient(transport=httpx.MockTransport(native_403)) as client:
            limiter = AsyncLimiter(max_rate=100.0)
            sem = asyncio.Semaphore(4)
            result = await fetch_one(
                client,
                FetchInput(url="https://x.test/page", decision="FETCH",
                           etag=None, last_modified=None),
                tmp_path, limiter, sem, retries=0, firecrawl_pool=None,
            )
        assert result.status == 403
        assert result.error == "http-403"

    async def test_404_not_in_fallback_set_skips_pool(self, tmp_path, monkeypatch):
        from sift.fetch import fetch_one
        from aiolimiter import AsyncLimiter
        import asyncio

        def boom(req):
            raise AssertionError("pool should not be consulted on 404")
        monkeypatch.setattr(httpx, "Client", _proxy_client(boom))

        async def native_404(req):
            return httpx.Response(404, content=b"")

        pool = FirecrawlScrapePool(
            FirecrawlScrapeConfig(enabled=True, fallback_statuses=(401, 403),
                                  max_credits_per_run=5, rate_per_sec=100.0,
                                  concurrency=4),
            api_key="fc-x",
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(native_404)) as client:
            limiter = AsyncLimiter(max_rate=100.0)
            sem = asyncio.Semaphore(4)
            result = await fetch_one(
                client,
                FetchInput(url="https://gone.test/page", decision="FETCH",
                           etag=None, last_modified=None),
                tmp_path, limiter, sem, retries=0, firecrawl_pool=pool,
            )
        assert result.status == 404
        assert pool.calls_attempted == 0

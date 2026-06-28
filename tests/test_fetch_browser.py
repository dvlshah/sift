"""Fetch-branch tests for the browser-fetch capability (design §9 layer 3).

Pins what the layer-1 contract tests can't:

  * fetch.py dispatch: `profile.requires_browser(url)` routes to the browser
    path; everything else takes the http path.
  * The browser path projects RenderedPage -> FetchResult correctly
    (raw blob written, etag/last-modified surfaced, browser_version tagged).
  * Commit persists `browser_version` on the manifest row.
  * Plan-phase §8.2 invalidation: a row whose stored `browser_version`
    differs from the current `BROWSER_VERSION` gets re-fetched.

These tests stub `sift.browser.render` so they don't need crawl4ai+Chromium
installed; the layer-5 `test_browser_real.py` (env-gated) covers the live
runtime separately.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

from sift.browser import BROWSER_VERSION, RenderedPage
from sift.config import IndexConfig
from sift.fetch import FetchInput, FetchResult, _fetch_browser, fetch_all
from sift.manifest import (
    apply_fetch_result,
    init_schema,
    now_utc,
    open_db,
    transaction,
    upsert_seed,
)
from sift.plan import plan
from sift.sites import SiteProfile


# ---------------------------------------------------------------------------
# Fakes — minimal substitutes that exercise the real branching logic.
# ---------------------------------------------------------------------------


class _SpaProfile(SiteProfile):
    """Routes /spa/* to browser, everything else to http."""

    def requires_browser(self, url: str) -> bool:
        return "/spa/" in url


class _FakePool:
    """Placeholder pool — render() is fully mocked so the pool body never runs."""

    async def acquire(self, url: str):  # pragma: no cover — unused under mock
        raise AssertionError("fake pool should never be acquired (render mocked)")

    async def aclose(self) -> None:
        return None


def _rendered(
    url: str,
    *,
    html: str = "<html><body>spa content</body></html>",
    status: int = 200,
    headers: Optional[dict] = None,
    error: Optional[str] = None,
) -> RenderedPage:
    return RenderedPage(
        html=html, final_url=url, status_code=status, elapsed_ms=42,
        headers=headers, error=error,
    )


# ---------------------------------------------------------------------------
# _fetch_browser: RenderedPage -> FetchResult projection
# ---------------------------------------------------------------------------


class TestFetchBrowserProjection:
    """`_fetch_browser` projects RenderedPage into FetchResult per §4.2."""

    @pytest.mark.asyncio
    async def test_success_writes_raw_blob_and_tags_version(self, tmp_path):
        inp = FetchInput(url="https://x/spa/page", decision="FETCH",
                         etag=None, last_modified=None)
        page = _rendered(inp.url, html="<html>HELLO SPA</html>",
                         headers={"etag": 'W/"abc"',
                                  "last-modified": "Wed, 01 Jan 2026 00:00:00 GMT"})

        with patch("sift.browser.render", return_value=page) as mock_render:
            result = await _fetch_browser(inp, tmp_path, _SpaProfile(), _FakePool())

        assert mock_render.await_count == 1
        assert result.error is None
        assert result.status == 200
        assert result.browser_version == BROWSER_VERSION
        assert result.etag == 'W/"abc"'
        assert result.last_modified == "Wed, 01 Jan 2026 00:00:00 GMT"
        assert result.raw_hash is not None
        assert result.raw_bytes == len(b"<html>HELLO SPA</html>")

        # Raw blob actually on disk at the content-addressed path.
        from sift import paths
        assert paths.raw_path(tmp_path, result.raw_hash).exists()

    @pytest.mark.asyncio
    async def test_render_error_returns_failed_result_with_version(self, tmp_path):
        inp = FetchInput(url="https://x/spa/broken", decision="FETCH",
                         etag=None, last_modified=None)
        from sift.browser import BrowserFetchError

        with patch("sift.browser.render", side_effect=BrowserFetchError("network kaboom")):
            result = await _fetch_browser(inp, tmp_path, _SpaProfile(), _FakePool())

        assert result.status == 0
        assert result.error is not None and "kaboom" in result.error
        assert result.browser_version == BROWSER_VERSION
        assert result.raw_hash is None

    @pytest.mark.asyncio
    async def test_no_headers_yields_none_etag(self, tmp_path):
        inp = FetchInput(url="https://x/spa/no-cache", decision="FETCH",
                         etag=None, last_modified=None)
        with patch("sift.browser.render", return_value=_rendered(inp.url, headers=None)):
            result = await _fetch_browser(inp, tmp_path, _SpaProfile(), _FakePool())
        assert result.etag is None
        assert result.last_modified is None
        assert result.browser_version == BROWSER_VERSION


class TestFetchBrowserSsrfGuard:
    """The browser path re-validates the FINAL rendered host against the
    allow-list before storing — mirroring the native fetch path — so an open
    redirect / JS navigation can't land an off-allowlist (e.g. cloud-metadata)
    DOM under the requested URL."""

    @pytest.mark.asyncio
    async def test_offlist_final_host_is_not_stored(self, tmp_path):
        inp = FetchInput(url="https://x/spa/page", decision="FETCH",
                         etag=None, last_modified=None)
        # render() navigates/redirects to the cloud-metadata host, off-allowlist
        page = _rendered("https://169.254.169.254/latest/meta-data/",
                         html="<html>SECRET CREDS</html>")
        with patch("sift.browser.render", return_value=page):
            result = await _fetch_browser(
                inp, tmp_path, _SpaProfile(), _FakePool(),
                allowed_hosts=frozenset({"x"}),
            )
        assert result.raw_hash is None  # body NOT stored
        assert result.error is not None
        assert "redirect-off-allowlist" in result.error
        assert "169.254.169.254" in result.error

    @pytest.mark.asyncio
    async def test_onlist_final_host_is_stored(self, tmp_path):
        inp = FetchInput(url="https://x/spa/page", decision="FETCH",
                         etag=None, last_modified=None)
        page = _rendered("https://x/spa/final", html="<html>OK</html>")
        with patch("sift.browser.render", return_value=page):
            result = await _fetch_browser(
                inp, tmp_path, _SpaProfile(), _FakePool(),
                allowed_hosts=frozenset({"x"}),
            )
        assert result.error is None
        assert result.raw_hash is not None

    @pytest.mark.asyncio
    async def test_no_allowlist_skips_check_backcompat(self, tmp_path):
        # allowed_hosts=None (default) preserves the prior behavior: no check.
        inp = FetchInput(url="https://x/spa/page", decision="FETCH",
                         etag=None, last_modified=None)
        page = _rendered("https://anywhere.example/x", html="<html>OK</html>")
        with patch("sift.browser.render", return_value=page):
            result = await _fetch_browser(inp, tmp_path, _SpaProfile(), _FakePool())
        assert result.error is None
        assert result.raw_hash is not None


# ---------------------------------------------------------------------------
# fetch_all dispatch: profile.requires_browser routes per URL.
# ---------------------------------------------------------------------------


class TestFetchAllDispatch:
    """`fetch_all` splits inputs by `profile.requires_browser` and runs both."""

    @pytest.mark.asyncio
    async def test_browser_required_without_pool_raises(self, tmp_path):
        log = tmp_path / "fetch.log"
        with pytest.raises(RuntimeError, match="browser rendering"):
            await fetch_all(
                [FetchInput(url="https://x/spa/a", decision="FETCH",
                            etag=None, last_modified=None)],
                tmp_path, log,
                profile=_SpaProfile(),
                browser_pool=None,
            )

    @pytest.mark.asyncio
    async def test_routes_browser_urls_through_pool(self, tmp_path):
        log = tmp_path / "fetch.log"
        spa_url = "https://x/spa/a"
        page = _rendered(spa_url, html="<html>SPA</html>")

        # http_inputs would normally hit the network; we don't include any so
        # no httpx call happens.
        with patch("sift.browser.render", return_value=page) as mock_render:
            count = await fetch_all(
                [FetchInput(url=spa_url, decision="FETCH", etag=None, last_modified=None)],
                tmp_path, log,
                profile=_SpaProfile(),
                browser_pool=_FakePool(),
            )

        assert count == 1
        assert mock_render.await_count == 1
        # Fetch log records the browser_version
        lines = log.read_text().strip().split("\n")
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["url"] == spa_url
        assert record["browser_version"] == BROWSER_VERSION

    @pytest.mark.asyncio
    async def test_http_only_inputs_dont_construct_browser_path(self, tmp_path):
        """Pre-browser back-compat: no profile + no pool → all URLs go through
        http (and if there are none, no crawl4ai import happens)."""
        log = tmp_path / "fetch.log"
        # Empty inputs; just verifying no crash + no crawl4ai import.
        count = await fetch_all([], tmp_path, log)
        assert count == 0
        import sys
        assert "crawl4ai" not in sys.modules


# ---------------------------------------------------------------------------
# Manifest persistence: commit propagates browser_version.
# ---------------------------------------------------------------------------


class TestBrowserVersionPersistence:
    """`apply_fetch_result(browser_version=...)` writes the column."""

    def test_apply_fetch_result_persists_browser_version(self, tmp_path):
        conn = open_db(tmp_path / "manifest.db")
        init_schema(conn)
        # Seed a URL so the UPDATE has a row to land on.
        with transaction(conn):
            upsert_seed(conn, url="https://x/spa/a", tier="LIVING",
                        parent_guide_=None, classifier_version="v1",
                        sitemap_lastmod=None, now=now_utc())
        with transaction(conn):
            apply_fetch_result(
                conn,
                url="https://x/spa/a",
                now=now_utc(),
                http_status=200,
                http_etag='W/"abc"',
                http_last_modified="Wed, 01 Jan 2026 00:00:00 GMT",
                raw_hash="rh",
                content_hash="ch",
                crawler_version="v1.0.0",
                extractor_version="ev",
                normalizer_version="nv",
                error=None,
                browser_version="crawl4ai-0.8.6",
            )
        row = conn.execute(
            "SELECT browser_version FROM manifest WHERE url = ?",
            ("https://x/spa/a",),
        ).fetchone()
        assert row["browser_version"] == "crawl4ai-0.8.6"

    def test_http_path_doesnt_overwrite_existing_browser_version(self, tmp_path):
        """COALESCE invariant: an http re-fetch with browser_version=None must
        not erase a previously-stored value (no operational reason for that
        flip; if it ever happens it should be explicit)."""
        conn = open_db(tmp_path / "manifest.db")
        init_schema(conn)
        with transaction(conn):
            upsert_seed(conn, url="https://x/spa/a", tier="LIVING",
                        parent_guide_=None, classifier_version="v1",
                        sitemap_lastmod=None, now=now_utc())
            apply_fetch_result(
                conn, url="https://x/spa/a", now=now_utc(),
                http_status=200, http_etag=None, http_last_modified=None,
                raw_hash="rh", content_hash="ch",
                crawler_version="v1.0.0", extractor_version="ev",
                normalizer_version="nv", error=None,
                browser_version="crawl4ai-0.8.6",
            )
        # Simulate a subsequent http-path call (no browser_version)
        with transaction(conn):
            apply_fetch_result(
                conn, url="https://x/spa/a", now=now_utc(),
                http_status=200, http_etag=None, http_last_modified=None,
                raw_hash="rh2", content_hash="ch2",
                crawler_version="v1.0.0", extractor_version="ev",
                normalizer_version="nv", error=None,
                browser_version=None,
            )
        row = conn.execute(
            "SELECT browser_version FROM manifest WHERE url = ?",
            ("https://x/spa/a",),
        ).fetchone()
        assert row["browser_version"] == "crawl4ai-0.8.6", (
            "browser_version=None must preserve, not overwrite (COALESCE)"
        )


# ---------------------------------------------------------------------------
# Plan-phase §8.2 invalidation rule.
# ---------------------------------------------------------------------------


class TestBrowserVersionInvalidation:
    """A row with stored browser_version != current BROWSER_VERSION gets
    re-fetched (FETCH_CONDITIONAL) on the next plan cycle."""

    def test_stale_browser_version_promotes_skip_to_conditional(self, tmp_path):
        conn = open_db(tmp_path / "manifest.db")
        init_schema(conn)
        url = "https://x/spa/page"
        # Seed and write a FRESH row whose browser_version differs from current.
        with transaction(conn):
            upsert_seed(conn, url=url, tier="LIVING", parent_guide_=None,
                        classifier_version="v1", sitemap_lastmod=None,
                        now=now_utc())
            apply_fetch_result(
                conn, url=url, now=now_utc(),
                http_status=200, http_etag=None, http_last_modified=None,
                raw_hash="rh", content_hash="ch",
                crawler_version="v1.0.0", extractor_version="ev",
                normalizer_version="nv", error=None,
                browser_version="crawl4ai-0.8.4",  # stale (current is 0.8.6)
            )
        cfg = IndexConfig()
        plan_path = tmp_path / "plan.jsonl"
        plan(
            conn, plan_path,
            now=datetime.now(timezone.utc),
            extractor_version="ev",
            normalizer_version="nv",
            profile=_SpaProfile(),
            cfg=cfg,
        )
        entries = [json.loads(line) for line in plan_path.read_text().strip().split("\n")]
        ours = [e for e in entries if e["url"] == url]
        assert len(ours) == 1
        assert ours[0]["decision"] == "FETCH_CONDITIONAL"
        assert "browser_version bump" in ours[0]["reason"]

    def test_matching_browser_version_keeps_skip(self, tmp_path):
        """Sanity: a current-version row should NOT be invalidated."""
        conn = open_db(tmp_path / "manifest.db")
        init_schema(conn)
        url = "https://x/spa/page"
        with transaction(conn):
            upsert_seed(conn, url=url, tier="LIVING", parent_guide_=None,
                        classifier_version="v1", sitemap_lastmod=None,
                        now=now_utc())
            apply_fetch_result(
                conn, url=url, now=now_utc(),
                http_status=200, http_etag=None, http_last_modified=None,
                raw_hash="rh", content_hash="ch",
                crawler_version="v1.0.0", extractor_version="ev",
                normalizer_version="nv", error=None,
                browser_version=BROWSER_VERSION,  # matches → no invalidation
            )
        cfg = IndexConfig()
        plan_path = tmp_path / "plan.jsonl"
        plan(
            conn, plan_path,
            now=datetime.now(timezone.utc),
            extractor_version="ev",
            normalizer_version="nv",
            profile=_SpaProfile(),
            cfg=cfg,
        )
        entries = [json.loads(line) for line in plan_path.read_text().strip().split("\n")]
        ours = [e for e in entries if e["url"] == url]
        assert len(ours) == 1
        # Just-fetched row: decide() returns SKIP (within interval). No bump.
        assert ours[0]["decision"] == "SKIP"

    def test_http_row_unaffected_by_browser_version_rule(self, tmp_path):
        """Rows with browser_version=NULL (http path) are not touched by §8.2."""
        conn = open_db(tmp_path / "manifest.db")
        init_schema(conn)
        url = "https://x/page"  # not SPA → http path
        with transaction(conn):
            upsert_seed(conn, url=url, tier="LIVING", parent_guide_=None,
                        classifier_version="v1", sitemap_lastmod=None,
                        now=now_utc())
            apply_fetch_result(
                conn, url=url, now=now_utc(),
                http_status=200, http_etag=None, http_last_modified=None,
                raw_hash="rh", content_hash="ch",
                crawler_version="v1.0.0", extractor_version="ev",
                normalizer_version="nv", error=None,
                # browser_version omitted → stored as NULL
            )
        cfg = IndexConfig()
        plan_path = tmp_path / "plan.jsonl"
        plan(
            conn, plan_path,
            now=datetime.now(timezone.utc),
            extractor_version="ev",
            normalizer_version="nv",
            profile=_SpaProfile(),
            cfg=cfg,
        )
        entries = [json.loads(line) for line in plan_path.read_text().strip().split("\n")]
        ours = [e for e in entries if e["url"] == url]
        assert len(ours) == 1
        assert ours[0]["decision"] == "SKIP"

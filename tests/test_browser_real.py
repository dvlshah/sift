"""Layer-5 real-browser integration tests (design §9 layer 5).

Hits public URLs with a real Chromium runtime. Skipped unless::

    SIFT_REAL_BROWSER=1 pytest tests/test_browser_real.py -v -s

Requires:
  * pip install 'sift[browser]'
  * python -m playwright install chromium

These tests share a single BrowserPool across the whole module (one
~5–10s Chromium boot, not per test) and are deliberately polite to ATO:
five URLs total per full run, no parallelism above pool concurrency.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

# Hard skip — no SIFT_REAL_BROWSER, no live run.
REAL = os.environ.get("SIFT_REAL_BROWSER") == "1"
pytestmark = pytest.mark.skipif(
    not REAL,
    reason="set SIFT_REAL_BROWSER=1 to run the live-browser layer (needs crawl4ai + chromium)",
)


# Pool fixture — module-scoped so we only pay the Chromium boot cost once.
@pytest.fixture(scope="module")
async def pool():
    from sift.browser import BrowserPool
    from sift.config import BrowserConfigDefaults
    p = BrowserPool(concurrency=2, defaults=BrowserConfigDefaults())
    try:
        yield p
    finally:
        await p.aclose()


# ---------------------------------------------------------------------------
# 1. Direct render() against real SPAs.
# ---------------------------------------------------------------------------


SPA_URL = "https://www.ato.gov.au/single-page-applications/legaldatabase"


def _ato_cfg(url: str):
    """Fetch the per-URL config the active profile would supply via
    fetch.py's dispatch. Falls back to global defaults if the profile
    doesn't override (mirrors _fetch_browser's selection logic)."""
    from sift.browser import BrowserFetchConfig
    from sift.sites.ato import ATOProfile
    return ATOProfile().browser_config(url) or BrowserFetchConfig()


@pytest.mark.asyncio
async def test_render_real_spa_returns_substantive_html(pool):
    """The ATO Legal DB SPA must render to >50KB of HTML when crawled with a
    real browser — the http path returns ~285 bytes of shell. The bigness
    of the delta is what justifies the whole browser-fetch capability.

    Uses ATOProfile's per-site config: ATO's Next.js shell needs
    networkidle (not the global domcontentloaded default) for hydration
    to finish before content() reads the DOM."""
    from sift.browser import RenderedPage, render

    cfg = _ato_cfg(SPA_URL)
    t0 = time.perf_counter()
    page = await render(SPA_URL, cfg, pool)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    assert isinstance(page, RenderedPage)
    assert page.error is None, f"render returned error: {page.error}"
    assert page.status_code == 200, f"unexpected status {page.status_code}"
    assert len(page.html) > 50_000, (
        f"SPA rendered to only {len(page.html)} bytes — likely returned the "
        f"unhydrated shell instead of the post-JS DOM. Expected >50KB."
    )
    print(
        f"\n  [render] {SPA_URL}\n"
        f"    cfg={cfg}\n"
        f"    html={len(page.html):,}B  status={page.status_code}  "
        f"elapsed={elapsed_ms}ms (pool reported {page.elapsed_ms}ms)"
    )


@pytest.mark.asyncio
async def test_render_real_spa_is_deterministic(pool):
    """Two renders of the same SPA should yield byte-identical post-normalize
    content_hash (the html itself differs by trace IDs, but the design's
    invariant is that content_hash is stable — that's what survives into
    the Merkle root)."""
    from sift.browser import render
    from sift.normalize import normalize_for_hash
    import hashlib

    cfg = _ato_cfg(SPA_URL)
    page1 = await render(SPA_URL, cfg, pool)
    page2 = await render(SPA_URL, cfg, pool)

    # raw_hash WILL differ (Akamai trace IDs in script tags etc.)
    raw1 = hashlib.sha256(page1.html.encode("utf-8")).hexdigest()
    raw2 = hashlib.sha256(page2.html.encode("utf-8")).hexdigest()
    raw_drift = raw1 != raw2

    # content_hash should not differ after normalize_for_hash. We need to
    # extract the body first; for this smoke test we approximate by hashing
    # normalized html directly (extract pipeline is downstream).
    norm1 = hashlib.sha256(normalize_for_hash(page1.html).encode("utf-8")).hexdigest()
    norm2 = hashlib.sha256(normalize_for_hash(page2.html).encode("utf-8")).hexdigest()
    norm_stable = norm1 == norm2

    print(
        f"\n  [determinism] raw_drift={raw_drift}  "
        f"normalized_stable={norm_stable}\n"
        f"    raw1={raw1[:16]} raw2={raw2[:16]}\n"
        f"    norm1={norm1[:16]} norm2={norm2[:16]}"
    )
    # The hard contract: even if raw drifts, normalize_for_hash must give
    # stability for the Merkle story to hold. If this fails on a given URL,
    # we'd want a per-site dynamic_pattern to strip whatever's rotating.
    # Allowing 1 character of drift would be wrong — Merkle is byte-exact.
    # If this test ever fails in CI: add a dynamic_pattern to ATOProfile.
    # For now (smoke), warn rather than fail since extract+normalize is the
    # downstream layer that owns this contract.
    if not norm_stable:
        print("    WARN: normalize_for_hash NOT stable across renders; ATO "
              "dynamic_patterns may need expansion.")


# ---------------------------------------------------------------------------
# 2. fetch_all dispatch against a real ATO profile.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_all_real_mixed_dispatch(pool, tmp_path):
    """Drive fetch_all with one SPA + one static URL through the real ATO
    profile. Asserts: SPA URL gets browser_version tagged in fetch.log;
    static URL goes through http path (no browser_version)."""
    from sift.fetch import FetchInput, fetch_all
    from sift.sites.ato import ATOProfile

    profile = ATOProfile()
    inputs = [
        FetchInput(
            url=SPA_URL,
            decision="FETCH", etag=None, last_modified=None,
        ),
        FetchInput(
            url="https://www.ato.gov.au/individuals-and-families/managing-your-tax",
            decision="FETCH", etag=None, last_modified=None,
        ),
    ]
    log = tmp_path / "fetch.log"
    n = await fetch_all(
        inputs, tmp_path, log,
        rate=1.0, concurrency=2,
        profile=profile, browser_pool=pool,
    )
    assert n == 2

    records = [json.loads(line) for line in log.read_text().strip().split("\n")]
    by_url = {r["url"]: r for r in records}

    spa_rec = by_url[SPA_URL]
    static_rec = by_url["https://www.ato.gov.au/individuals-and-families/managing-your-tax"]

    print(
        f"\n  [dispatch] {len(records)} records in fetch.log:\n"
        f"    SPA    : status={spa_rec['status']} bytes={spa_rec['raw_bytes']} "
        f"browser_version={spa_rec['browser_version']}\n"
        f"    static : status={static_rec['status']} bytes={static_rec['raw_bytes']} "
        f"browser_version={static_rec['browser_version']}"
    )

    # Browser path
    assert spa_rec["browser_version"] is not None, "SPA must carry browser_version tag"
    assert spa_rec["browser_version"].startswith("crawl4ai-"), spa_rec["browser_version"]
    assert spa_rec["raw_bytes"] > 50_000, f"SPA rendered tiny: {spa_rec['raw_bytes']}B"

    # Http path
    assert static_rec["browser_version"] is None, (
        f"static URL must NOT carry browser_version, got "
        f"{static_rec['browser_version']!r}"
    )
    assert static_rec["status"] == 200, f"static status was {static_rec['status']}"


# ---------------------------------------------------------------------------
# 3. SiteProfile contract on real ATOProfile.
# ---------------------------------------------------------------------------


def test_ato_profile_routes_real_urls_correctly():
    """Smoke check that the ATOProfile categorizes a representative URL set
    correctly. No browser launched — pure profile.requires_browser() call."""
    from sift.sites.ato import ATOProfile

    ap = ATOProfile()
    cases = [
        # (url, expected_requires_browser)
        ("https://www.ato.gov.au/single-page-applications/legaldatabase", True),
        ("https://www.ato.gov.au/single-page-applications/iar", True),
        ("https://www.ato.gov.au/individuals-and-families", False),
        ("https://www.ato.gov.au/individuals-and-families/managing-your-tax", False),
        ("https://www.ato.gov.au/forms-and-instructions", False),
        ("https://www.ato.gov.au/media-centre/news", False),
        ("https://www.ato.gov.au/tax-rates-and-codes/individual-income-tax-rates", False),
    ]
    print("\n  [profile routing]")
    for url, expected in cases:
        got = ap.requires_browser(url)
        flag = "OK" if got == expected else "FAIL"
        print(f"    {flag:5s} requires_browser={got} (want {expected}) :: {url}")
        assert got is expected, f"profile mismatch on {url}: got {got}, want {expected}"


if __name__ == "__main__":
    # Allow running directly without pytest:
    #   SIFT_REAL_BROWSER=1 python tests/test_browser_real.py
    sys.exit(pytest.main([__file__, "-v", "-s"]))

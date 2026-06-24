"""Tiered fetch transport: content-quality trigger + escalation ladder.

Covers ``sift.quality.looks_thin`` (the trigger) and ``sift.fetch.fetch_one``'s
ladder (httpx → curl_cffi → Firecrawl) with fake pools, so no network is hit.
The live integration against a real WAF'd site lives in the demo script, not the
hermetic suite.
"""

from __future__ import annotations

import asyncio

import httpx
import respx
from aiolimiter import AsyncLimiter
from httpx import Response

from sift.fetch import FetchInput, FetchResult, HostTierMemo, fetch_one, host_of
from sift.quality import looks_thin
from sift.sources.firecrawl import FIRECRAWL_FETCHER_VERSION
from sift.sources.impersonate import CURL_CFFI_FETCHER_VERSION, EscalateError

URL = "https://example.test/"
THIN = b"<html><body><div id='root'></div></body></html>"
CHALLENGE = b"<html><head><title>Just a moment...</title></head><body></body></html>"
REAL = (
    b"<html><body><article>"
    + (b"lorem ipsum dolor " * 60)
    + b"</article></body></html>"
)
NEXTJS = (
    b'<html><body><div id="__next"></div>'
    b'<script id="__NEXT_DATA__" type="application/json">'
    + (b'{"k":"v"}' * 600)
    + b"</script></body></html>"
)


# ---- looks_thin (the content-quality trigger) ------------------------------


def test_empty_spa_shell_is_thin():
    assert looks_thin(THIN, "text/html", 500) is True


def test_challenge_page_is_thin():
    assert looks_thin(CHALLENGE, "text/html", 500) is True


def test_real_content_is_not_thin():
    assert looks_thin(REAL, "text/html", 500) is False


def test_non_html_never_thin():
    # a short PDF/JSON body must not be judged a "shell"
    assert looks_thin(b"%PDF-1.4 ...", "application/pdf", 500) is False
    assert looks_thin(b'{"rate":0.5}', "application/json", 500) is False


def test_threshold_zero_disables():
    assert looks_thin(THIN, "text/html", 0) is False


def test_embedded_next_data_is_not_thin():
    # data is IN the HTML (extractable) even though visible text is sparse
    assert looks_thin(NEXTJS, "text/html", 500) is False


# ---- fake escalation tiers --------------------------------------------------


def _result(status: int, version: str | None) -> FetchResult:
    return FetchResult(
        url=URL,
        decision="FETCH",
        status=status,
        etag=None,
        last_modified=None,
        raw_hash="deadbeef",
        raw_bytes=10,
        fetched_at="t",
        error=None,
        browser_version=version,
        content_type="text/html",
    )


class FakeImpersonate:
    escalate_statuses = (403, 429, 503)

    def __init__(self, result=None, raise_exc=None):
        self.result, self.raise_exc, self.calls = result, raise_exc, 0

    async def fetch(self, inp, root, *, allowed_hosts=None):
        self.calls += 1
        if self.raise_exc:
            raise self.raise_exc
        return self.result


class FakeFirecrawl:
    fallback_statuses = (401, 403)

    def __init__(self, result=None, raise_exc=None, escalate_on_thin=False):
        self.result, self.raise_exc, self.calls = result, raise_exc, 0
        self.cfg = type("C", (), {"escalate_on_thin": escalate_on_thin})()

    def budget_remaining(self):
        return 10

    async def fetch(self, inp, root):
        self.calls += 1
        if self.raise_exc:
            raise self.raise_exc
        return self.result


async def _run_fetch(
    tmp_path,
    *,
    impersonate_pool=None,
    browser_pool=None,
    profile=None,
    firecrawl_pool=None,
    thin_text_threshold=500,
    memo=None,
):
    inp = FetchInput(url=URL, decision="FETCH", etag=None, last_modified=None)
    async with httpx.AsyncClient() as client:
        return await fetch_one(
            client,
            inp,
            tmp_path,
            AsyncLimiter(1000, 1),
            asyncio.Semaphore(8),
            retries=0,
            impersonate_pool=impersonate_pool,
            browser_pool=browser_pool,
            profile=profile,
            firecrawl_pool=firecrawl_pool,
            thin_text_threshold=thin_text_threshold,
            memo=memo,
        )


# ---- the ladder -------------------------------------------------------------


@respx.mock
async def test_block_status_escalates_to_impersonate(tmp_path):
    respx.get(URL).mock(return_value=Response(403, text="blocked"))
    imp = FakeImpersonate(result=_result(200, CURL_CFFI_FETCHER_VERSION))
    res = await _run_fetch(tmp_path, impersonate_pool=imp)
    assert imp.calls == 1
    assert res.status == 200
    assert res.browser_version == CURL_CFFI_FETCHER_VERSION


@respx.mock
async def test_thin_200_escalates_to_impersonate(tmp_path):
    respx.get(URL).mock(
        return_value=Response(
            200, content=THIN, headers={"content-type": "text/html; charset=utf-8"}
        )
    )
    imp = FakeImpersonate(result=_result(200, CURL_CFFI_FETCHER_VERSION))
    res = await _run_fetch(tmp_path, impersonate_pool=imp)
    assert imp.calls == 1
    assert res.browser_version == CURL_CFFI_FETCHER_VERSION


@respx.mock
async def test_ladder_falls_through_impersonate_to_firecrawl(tmp_path):
    respx.get(URL).mock(return_value=Response(403))
    imp = FakeImpersonate(raise_exc=EscalateError("still blocked"))
    fc = FakeFirecrawl(result=_result(200, FIRECRAWL_FETCHER_VERSION))
    res = await _run_fetch(tmp_path, impersonate_pool=imp, firecrawl_pool=fc)
    assert imp.calls == 1 and fc.calls == 1
    assert res.browser_version == FIRECRAWL_FETCHER_VERSION


@respx.mock
async def test_thin_does_not_burn_firecrawl_unless_opted_in(tmp_path):
    # firecrawl-only, escalate_on_thin=False → thin 200 is committed natively
    respx.get(URL).mock(
        return_value=Response(200, content=THIN, headers={"content-type": "text/html"})
    )
    fc = FakeFirecrawl(
        result=_result(200, FIRECRAWL_FETCHER_VERSION), escalate_on_thin=False
    )
    res = await _run_fetch(tmp_path, firecrawl_pool=fc)
    assert fc.calls == 0
    assert res.browser_version is None  # native body stored
    assert res.raw_hash is not None


@respx.mock
async def test_thin_escalates_to_firecrawl_when_opted_in(tmp_path):
    respx.get(URL).mock(
        return_value=Response(200, content=THIN, headers={"content-type": "text/html"})
    )
    fc = FakeFirecrawl(
        result=_result(200, FIRECRAWL_FETCHER_VERSION), escalate_on_thin=True
    )
    res = await _run_fetch(tmp_path, firecrawl_pool=fc)
    assert fc.calls == 1
    assert res.browser_version == FIRECRAWL_FETCHER_VERSION


@respx.mock
async def test_real_200_never_escalates(tmp_path):
    respx.get(URL).mock(
        return_value=Response(200, content=REAL, headers={"content-type": "text/html"})
    )
    imp = FakeImpersonate(result=_result(200, CURL_CFFI_FETCHER_VERSION))
    res = await _run_fetch(tmp_path, impersonate_pool=imp)
    assert imp.calls == 0
    assert res.browser_version is None
    assert res.raw_hash is not None


@respx.mock
async def test_no_pools_is_native_only_backcompat(tmp_path):
    # 403 with no tiers wired → unchanged native failure, nothing escalated
    respx.get(URL).mock(return_value=Response(403))
    res = await _run_fetch(tmp_path, thin_text_threshold=0)
    assert res.status == 403
    assert res.raw_hash is None
    assert res.error == "http-403"


# ---- tier-3a: self-hosted browser ------------------------------------------

import sift.fetch as fetchmod  # noqa: E402

BROWSER_VER = "browser-test"


def _stub_browser_success(monkeypatch):
    """Patch _fetch_browser to render real content into the blob store."""

    async def fake(inp, root, profile, pool):
        from sift.fetch import FetchResult, store_body

        raw_hash, n = store_body(
            root, b"<html><body>" + b"real " * 200 + b"</body></html>"
        )
        return FetchResult(
            url=inp.url,
            decision=inp.decision,
            status=200,
            etag=None,
            last_modified=None,
            raw_hash=raw_hash,
            raw_bytes=n,
            fetched_at="t",
            error=None,
            browser_version=BROWSER_VER,
            content_type="text/html",
        )

    monkeypatch.setattr(fetchmod, "_fetch_browser", fake)


def _stub_browser_failure(monkeypatch):
    async def fake(inp, root, profile, pool):
        from sift.fetch import FetchResult

        return FetchResult(
            url=inp.url,
            decision=inp.decision,
            status=0,
            etag=None,
            last_modified=None,
            raw_hash=None,
            raw_bytes=0,
            fetched_at="t",
            error="render-timeout",
            browser_version=BROWSER_VER,
            content_type=None,
        )

    monkeypatch.setattr(fetchmod, "_fetch_browser", fake)


@respx.mock
async def test_block_escalates_to_browser_tier(tmp_path, monkeypatch):
    # browser is the ONLY tier → a 403 escalates straight to it
    _stub_browser_success(monkeypatch)
    respx.get(URL).mock(return_value=Response(403))
    res = await _run_fetch(tmp_path, browser_pool=object(), profile=object())
    assert res.browser_version == BROWSER_VER


@respx.mock
async def test_thin_200_escalates_to_browser_free(tmp_path, monkeypatch):
    # thin reaches the FREE browser tier with no escalate_on_thin gate needed
    _stub_browser_success(monkeypatch)
    respx.get(URL).mock(
        return_value=Response(200, content=THIN, headers={"content-type": "text/html"})
    )
    res = await _run_fetch(tmp_path, browser_pool=object(), profile=object())
    assert res.browser_version == BROWSER_VER


@respx.mock
async def test_browser_failure_falls_through_to_firecrawl(tmp_path, monkeypatch):
    # order: impersonate raises → browser fails → Firecrawl serves it
    _stub_browser_failure(monkeypatch)
    respx.get(URL).mock(return_value=Response(403))
    imp = FakeImpersonate(raise_exc=EscalateError("blocked"))
    fc = FakeFirecrawl(result=_result(200, FIRECRAWL_FETCHER_VERSION))
    res = await _run_fetch(
        tmp_path,
        impersonate_pool=imp,
        browser_pool=object(),
        profile=object(),
        firecrawl_pool=fc,
    )
    assert imp.calls == 1 and fc.calls == 1
    assert res.browser_version == FIRECRAWL_FETCHER_VERSION


@respx.mock
async def test_browser_precedes_firecrawl(tmp_path, monkeypatch):
    # when the browser succeeds, the paid tier is never consulted
    _stub_browser_success(monkeypatch)
    respx.get(URL).mock(return_value=Response(403))
    fc = FakeFirecrawl(result=_result(200, FIRECRAWL_FETCHER_VERSION))
    res = await _run_fetch(
        tmp_path, browser_pool=object(), profile=object(), firecrawl_pool=fc
    )
    assert fc.calls == 0
    assert res.browser_version == BROWSER_VER


@respx.mock
async def test_browser_render_crash_falls_through(tmp_path, monkeypatch):
    # a browser EXCEPTION (not just a failure row) must not crash the fetch —
    # it declines and the ladder continues to Firecrawl
    async def boom(inp, root, profile, pool):
        from sift.browser import BrowserNotInstalledError

        raise BrowserNotInstalledError("no playwright")

    monkeypatch.setattr(fetchmod, "_fetch_browser", boom)
    respx.get(URL).mock(return_value=Response(403))
    fc = FakeFirecrawl(result=_result(200, FIRECRAWL_FETCHER_VERSION))
    res = await _run_fetch(
        tmp_path, browser_pool=object(), profile=object(), firecrawl_pool=fc
    )
    assert fc.calls == 1
    assert res.browser_version == FIRECRAWL_FETCHER_VERSION


@respx.mock
async def test_browser_crash_no_other_tier_keeps_native_failure(tmp_path, monkeypatch):
    # browser is the only escalation tier and it crashes → native failure stands,
    # the run does NOT crash
    async def boom(inp, root, profile, pool):
        raise RuntimeError("render boom")

    monkeypatch.setattr(fetchmod, "_fetch_browser", boom)
    respx.get(URL).mock(return_value=Response(403))
    res = await _run_fetch(tmp_path, browser_pool=object(), profile=object())
    assert res.status == 403
    assert res.raw_hash is None


# ---- adaptive per-host floor (HostTierMemo) --------------------------------


def test_memo_floors_after_threshold():
    m = HostTierMemo(threshold=3)
    h = "blocked.test"
    m.record_block(h)
    m.record_block(h)
    assert not m.should_skip_native(h)  # 2 < 3
    m.record_block(h)
    assert m.should_skip_native(h)  # 3rd latches the floor


def test_memo_ok_resets_transient_blocks():
    m = HostTierMemo(threshold=3)
    h = "flaky.test"
    m.record_block(h)
    m.record_block(h)
    m.record_ok(h)  # healthy again → tally cleared
    m.record_block(h)
    m.record_block(h)
    assert not m.should_skip_native(h)  # never reached 3 in a row


def test_memo_threshold_zero_never_floors():
    m = HostTierMemo(threshold=0)
    for _ in range(10):
        m.record_block("x.test")
    assert not m.should_skip_native("x.test")


def test_memo_floor_is_sticky():
    m = HostTierMemo(threshold=2)
    h = "hard.test"
    m.record_block(h)
    m.record_block(h)
    m.record_ok(h)  # ok after flooring does NOT un-floor
    assert m.should_skip_native(h)


@respx.mock
async def test_floored_host_skips_native_round_trip(tmp_path):
    # pre-floor the host → fetch_one must NOT touch the native client
    route = respx.get(URL).mock(return_value=Response(403))
    memo = HostTierMemo(threshold=3)
    for _ in range(3):
        memo.record_block(host_of(URL))
    imp = FakeImpersonate(result=_result(200, CURL_CFFI_FETCHER_VERSION))
    res = await _run_fetch(tmp_path, impersonate_pool=imp, memo=memo)
    assert route.call_count == 0  # native skipped entirely
    assert imp.calls == 1
    assert res.browser_version == CURL_CFFI_FETCHER_VERSION
    assert memo.skipped == 1  # telemetry counted the skipped native round-trip


@respx.mock
async def test_repeated_blocks_then_native_is_skipped(tmp_path):
    # end-to-end: 3 URLs block native, the 4th skips it (route count stops rising)
    route = respx.get(URL).mock(return_value=Response(403))
    memo = HostTierMemo(threshold=3)
    imp = FakeImpersonate(result=_result(200, CURL_CFFI_FETCHER_VERSION))
    for _ in range(3):
        await _run_fetch(tmp_path, impersonate_pool=imp, memo=memo)
    assert route.call_count == 3  # native tried for the first 3
    await _run_fetch(tmp_path, impersonate_pool=imp, memo=memo)
    assert route.call_count == 3  # 4th skipped native → curl_cffi direct
    assert imp.calls == 4
    # telemetry: exactly one native round-trip was skipped, on the floored host
    assert memo.skipped == 1
    assert memo.floored_hosts == frozenset({host_of(URL)})


def test_memo_skip_counter():
    m = HostTierMemo(threshold=1)
    assert m.skipped == 0
    m.record_skip()
    m.record_skip()
    assert m.skipped == 2

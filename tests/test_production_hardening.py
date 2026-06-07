"""Production-hardening pass: failure-path coverage + regression locks.

Companion to test_pre_freeze_hardening.py. Each class here either locks a
fix made during the pre-release failure-path audit, or covers a previously
untested success/failure path the audit surfaced. Grouped by pipeline stage.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from sift import paths
from sift.fetch import FetchInput, FetchResult, fetch_one, sha256_hex, write_raw_blob
from sift.manifest import (
    init_schema, now_utc, open_db, record_run_start, transaction,
)


# ---- async + sync httpx mock helpers --------------------------------------

def _async_client(handler):
    class _C(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **kw)
    return _C


def _sync_client(handler):
    class _C(httpx.Client):
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **kw)
    return _C


def _make_run(root: Path, run_id: str, *, status: str, link: bool) -> None:
    rd = paths.run_dir(root, run_id)
    (rd / "md").mkdir(parents=True)
    (rd / "snapshot.json").write_text(json.dumps({"run_id": run_id, "status": status}))
    if link:
        cur = root / "current"
        if cur.exists() or cur.is_symlink():
            cur.unlink()
        cur.symlink_to(rd)


# ===========================================================================
# Fix #1 — single canonical published-run resolver (provenance regression)
# ===========================================================================

class TestPublishedRunResolver:
    def test_degraded_only_resolves_none(self, tmp_path):
        # A run that degraded never flips `current`; it must not be served.
        _make_run(tmp_path, "2026-01-01T00-00-00Z", status="degraded", link=False)
        assert paths.published_run_dir(tmp_path) is None

    def test_published_symlink_resolves(self, tmp_path):
        _make_run(tmp_path, "2026-02-02T00-00-00Z", status="published", link=True)
        resolved = paths.published_run_dir(tmp_path)
        assert resolved is not None and resolved.name == "2026-02-02T00-00-00Z"

    def test_no_runs_resolves_none(self, tmp_path):
        assert paths.published_run_dir(tmp_path) is None

    def test_registry_describe_degraded_is_not_published(self, tmp_path):
        # THE regression: registry used to fall back to latest_run_dir and
        # advertise a gate-failed run as published, so list_indexes promised a
        # page count the read tools then refused. Now describe() agrees with
        # the content tools: degraded-only -> last_published None.
        from sift import registry
        idx = tmp_path / "idx"
        _make_run(idx, "2026-03-03T00-00-00Z", status="degraded", link=False)
        d = registry.describe(idx)
        assert d.last_published is None
        # ...but still discoverable (is_sift_root is deliberately broader than
        # "published", so the agent can see the index exists and that a run ran
        # — it just isn't advertised as a readable published snapshot).
        assert registry.is_sift_root(idx)

    def test_registry_describe_published_reports_run(self, tmp_path):
        from sift import registry
        idx = tmp_path / "idx"
        _make_run(idx, "2026-04-04T00-00-00Z", status="published", link=True)
        d = registry.describe(idx)
        assert d.last_published == "2026-04-04T00-00-00Z"

    def test_all_three_resolvers_agree_on_degraded(self, tmp_path):
        # registry, mcp_server, and paths must never disagree about published.
        from sift import mcp_server, registry
        idx = tmp_path / "idx"
        _make_run(idx, "2026-05-05T00-00-00Z", status="degraded", link=False)
        assert paths.published_run_dir(idx) is None
        assert registry._published_run_dir(idx) is None
        assert mcp_server._published_run_dir(idx) is None


# ===========================================================================
# Fix #2 — extract_one per-URL exception containment
# ===========================================================================

def _seed_blob(root: Path, url: str) -> FetchResult:
    body = b"<html><body><h1>Title</h1><p>" + b"Body text. " * 40 + b"</p></body></html>"
    rh = sha256_hex(body)
    write_raw_blob(root, rh, body)
    return FetchResult(
        url=url, decision="FETCH", status=200, etag=None, last_modified=None,
        raw_hash=rh, raw_bytes=len(body), fetched_at=now_utc(), error=None,
    )


class TestExtractOneContainment:
    def test_unexpected_raise_becomes_failed_row(self, tmp_path, monkeypatch):
        from sift import extract as extract_mod
        conn = open_db(paths.manifest_path(tmp_path)); init_schema(conn)
        fr = _seed_blob(tmp_path, "https://x.test/p")

        def boom(*a, **k):
            raise RuntimeError("synthetic extract crash")
        monkeypatch.setattr(extract_mod, "reextract_and_hash", boom)

        res = extract_mod.extract_one(
            fr, root=tmp_path, run_id="r", conn=conn, crawler_version="v1")
        assert res.ok is False
        assert res.reason.startswith("extract-error:")
        assert "RuntimeError" in res.reason

    def test_extract_all_continues_past_a_crash(self, tmp_path, monkeypatch):
        # One pathological blob must not abort the whole batch.
        from sift import extract as extract_mod
        conn = open_db(paths.manifest_path(tmp_path)); init_schema(conn)
        fetches = [_seed_blob(tmp_path, f"https://x.test/p{i}") for i in range(3)]

        monkeypatch.setattr(extract_mod, "reextract_and_hash",
                            lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
        el = paths.extract_log_path(tmp_path, "r")
        n = extract_mod.extract_all(
            fetches, root=tmp_path, run_id="r", conn=conn,
            crawler_version="v1", extract_log=el)
        # All three attempted (loop did not abort), all recorded ok=False.
        assert n == 3
        lines = [json.loads(x) for x in el.read_text().splitlines()]
        assert len(lines) == 3
        assert all(not l["ok"] for l in lines)


# ===========================================================================
# Fix #2b — is_pdf no longer over-matches HTML that mentions %PDF-
# ===========================================================================

class TestIsPdfSniff:
    def test_html_mentioning_pdf_magic_is_not_pdf(self):
        from sift.extract import is_pdf
        html = (b"<!DOCTYPE html><html><body>"
                b"<code>%PDF-1.7</code> a docs page about PDFs"
                b"</body></html>")
        assert not is_pdf(html)

    def test_real_pdf_at_offset_zero(self):
        from sift.extract import is_pdf
        assert is_pdf(b"%PDF-1.7\nbody")

    def test_pdf_with_leading_nul_or_whitespace(self):
        from sift.extract import is_pdf
        assert is_pdf(b"\x00\x00%PDF-1.7\nbody")   # BOM/junk tolerated
        assert is_pdf(b"   \n%PDF-1.5 body")        # leading whitespace tolerated

    def test_empty_is_not_pdf(self):
        from sift.extract import is_pdf
        assert not is_pdf(b"")


# ===========================================================================
# Fix #3 — terminal 'failed' status on a crash in ANY phase command
# ===========================================================================

class TestPhaseCommandCrashGuard:
    def _prepared_root(self, tmp_path):
        (tmp_path / "sift.toml").write_text(
            "[browser]\nenabled = false\n\n[seed]\nhost_allow = [\"x.test\"]\n"
        )
        conn = open_db(paths.manifest_path(tmp_path)); init_schema(conn)
        with transaction(conn):
            record_run_start(conn, "rid", now_utc())
        conn.close()

    def _status(self, tmp_path):
        conn = open_db(paths.manifest_path(tmp_path))
        row = conn.execute(
            "SELECT status, error FROM runs WHERE run_id='rid'").fetchone()
        conn.close()
        return row

    def test_publish_crash_records_failed(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        import sift.cli as climod
        self._prepared_root(tmp_path)
        monkeypatch.setattr(climod, "build_artifacts",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
        res = CliRunner().invoke(climod.main, [
            "publish", "--root", str(tmp_path),
            "--config", str(tmp_path / "sift.toml"), "--run-id", "rid",
        ])
        assert res.exit_code != 0
        status, error = self._status(tmp_path)
        assert status == "failed"
        assert "disk full" in (error or "")

    def test_commit_crash_records_failed(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        import sift.cli as climod
        self._prepared_root(tmp_path)
        monkeypatch.setattr(climod, "commit_phase",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("commit boom")))
        res = CliRunner().invoke(climod.main, [
            "commit", "--root", str(tmp_path),
            "--config", str(tmp_path / "sift.toml"), "--run-id", "rid",
        ])
        assert res.exit_code != 0
        status, _ = self._status(tmp_path)
        assert status == "failed"


# ===========================================================================
# Fix #4 — Firecrawl honors a legitimate creditsUsed:0 (cache hit)
# ===========================================================================

def _scrape_envelope(*, html="<html><body>ok</body></html>", origin_status=200,
                     credits=1):
    return httpx.Response(200, json={
        "success": True,
        "data": {"html": html, "metadata": {
            "statusCode": origin_status, "creditsUsed": credits,
            "sourceURL": "https://x.test/page"}},
    })


class TestFirecrawlBudgetAccounting:
    def _cfg(self, **kw):
        from sift.config import FirecrawlScrapeConfig
        d = dict(enabled=True, max_credits_per_run=10, rate_per_sec=100.0,
                 concurrency=4)
        d.update(kw)
        return FirecrawlScrapeConfig(**d)

    async def test_cache_hit_zero_credits_not_charged(self, tmp_path, monkeypatch):
        # creditsUsed:0 (a cache hit under max_cache_age_ms>0) must stay 0,
        # not be rounded up to 1 by the old `or 1`.
        from sift.sources.firecrawl import FirecrawlScrapePool
        monkeypatch.setattr(httpx, "Client",
                            _sync_client(lambda req: _scrape_envelope(credits=0)))
        pool = FirecrawlScrapePool(self._cfg(proxy="basic"), api_key="fc-x")
        await pool.fetch(FetchInput(url="https://x.test/page", decision="FETCH",
                                    etag=None, last_modified=None), tmp_path)
        assert pool.credits_used == 0
        assert pool.calls_succeeded == 1

    async def test_counter_never_negative(self, tmp_path, monkeypatch):
        # auto proxy reserves 5 but the call only costs 1 — the settle must
        # land on the real cost and never drive the counter below zero.
        from sift.sources.firecrawl import FirecrawlScrapePool
        monkeypatch.setattr(httpx, "Client",
                            _sync_client(lambda req: _scrape_envelope(credits=1)))
        pool = FirecrawlScrapePool(self._cfg(proxy="auto"), api_key="fc-x")
        await pool.fetch(FetchInput(url="https://x.test/page", decision="FETCH",
                                    etag=None, last_modified=None), tmp_path)
        assert pool.credits_used == 1


# ===========================================================================
# Fix #5 — sitemap walk is bounded (sitemap-bomb guard)
# ===========================================================================

class TestSitemapRecursionBound:
    def test_max_sitemaps_caps_fanout(self, monkeypatch):
        from sift.sources import sitemap as sm
        # An index whose children are themselves indexes — infinite-ish fanout.
        def handler(req: httpx.Request) -> httpx.Response:
            # every sitemap is an index pointing at two fresh children
            n = req.url.path
            body = (
                '<?xml version="1.0"?>'
                '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                f'<sitemap><loc>https://x.test{n}a.xml</loc></sitemap>'
                f'<sitemap><loc>https://x.test{n}b.xml</loc></sitemap>'
                '</sitemapindex>'
            )
            return httpx.Response(200, text=body)
        monkeypatch.setattr(httpx, "Client", _sync_client(handler))
        out = sm.walk_sitemap("https://x.test/s.xml", max_sitemaps=10)
        # Bounded: we fetched at most max_sitemaps documents, didn't hang.
        assert out == []   # all indexes, no actual <url> entries

    def test_max_urls_truncates(self, monkeypatch):
        from sift.sources import sitemap as sm
        def handler(req: httpx.Request) -> httpx.Response:
            urls = "".join(
                f"<url><loc>https://x.test/p/{i}</loc></url>" for i in range(50))
            body = ('<?xml version="1.0"?>'
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    f'{urls}</urlset>')
            return httpx.Response(200, text=body)
        monkeypatch.setattr(httpx, "Client", _sync_client(handler))
        out = sm.walk_sitemap("https://x.test/s.xml", max_urls=10)
        assert len(out) == 10


# ===========================================================================
# Fix #6 — fetch_all per-task containment
# ===========================================================================

class TestFetchTaskContainment:
    async def test_blob_write_oserror_becomes_failed_row(self, tmp_path, monkeypatch):
        # A disk error during the raw-blob write must not abort the whole run;
        # it becomes one status=0 row and siblings still complete.
        import sift.fetch as fetchmod

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>ok</html>")

        calls = {"n": 0}
        real_write = fetchmod.write_raw_blob

        def flaky_write(root, h, body):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk full")
            return real_write(root, h, body)
        monkeypatch.setattr(fetchmod, "write_raw_blob", flaky_write)
        monkeypatch.setattr(httpx, "AsyncClient", _async_client(handler))

        fl = tmp_path / "fetch.log"
        n = await fetchmod.fetch_all(
            [FetchInput(url=f"https://x.test/p{i}", decision="FETCH",
                        etag=None, last_modified=None) for i in range(2)],
            tmp_path, fl, rate=100.0, concurrency=2,
        )
        assert n == 2
        rows = [json.loads(x) for x in fl.read_text().splitlines()]
        # Exactly one crashed (status 0, task-crash) and one succeeded.
        crashed = [r for r in rows if r["status"] == 0]
        ok = [r for r in rows if r["status"] == 200]
        assert len(crashed) == 1 and len(ok) == 1
        assert crashed[0]["error"].startswith("task-crash:")


# ===========================================================================
# Fix #7 — fan-out does not poison a useful partial result with isError
# ===========================================================================

class TestFanoutIsError:
    def test_partial_success_is_not_error(self, tmp_path):
        from sift import mcp_server
        # alpha: published with a match. beta: no published run (degraded).
        alpha = tmp_path / "alpha"
        _make_run(alpha, "2026-01-01T00-00-00Z", status="published", link=True)
        (paths.run_dir(alpha, "2026-01-01T00-00-00Z") / "md" / "p.md").write_text(
            "---\nurl: https://a.test/p\n---\nneedle here\n")
        beta = tmp_path / "beta"
        _make_run(beta, "2026-01-02T00-00-00Z", status="degraded", link=False)

        server = mcp_server.build_server(tmp_path)
        res = _call(server, "grep_corpus", {"pattern": "needle"})
        # alpha's match present, and the aggregate is NOT flagged an error
        # just because beta has no published snapshot.
        assert not res.isError
        assert "needle" in res.content[0].text

    def test_all_failed_is_error(self, tmp_path):
        from sift import mcp_server
        # Two indexes, both unpublished -> the whole fan-out genuinely fails.
        for slug in ("a", "b"):
            _make_run(tmp_path / slug, "2026-01-01T00-00-00Z",
                      status="degraded", link=False)
        server = mcp_server.build_server(tmp_path)
        res = _call(server, "grep_corpus", {"pattern": "anything"})
        assert res.isError


def _call(server, name, args):
    """Invoke a tool through the MCP dispatcher and return its CallToolResult."""
    import mcp.types as mcp_types
    handler = server.request_handlers[mcp_types.CallToolRequest]
    req = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=args),
    )
    return asyncio.run(handler(req)).root


# ===========================================================================
# Untested failure path — fetch_one retry / transient / network machinery
# ===========================================================================

async def _instant_sleep(*_a, **_k):
    """Non-recursive no-op replacement for asyncio.sleep (RETRY_BACKOFF_BASE
    is 1.5s; we don't want real backoff delays in tests)."""
    return None


class TestFetchRetryMachinery:
    def _limiter(self):
        from aiolimiter import AsyncLimiter
        return AsyncLimiter(1000, 1)

    async def test_transient_status_is_retried_then_succeeds(self, tmp_path, monkeypatch):
        # 503 on the first attempt, 200 on the retry. Backoff slept-through.
        monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
        seq = iter([503, 200])

        def handler(req):
            return httpx.Response(next(seq), text="<html>ok</html>")
        async with _async_client(handler)() as client:
            res = await fetch_one(
                client, FetchInput(url="https://x.test/p", decision="FETCH",
                                   etag=None, last_modified=None),
                tmp_path, self._limiter(), asyncio.Semaphore(2), retries=2)
        assert res.status == 200
        assert res.raw_hash is not None

    async def test_retry_exhaustion_returns_last_transient(self, tmp_path, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

        def handler(req):
            return httpx.Response(503, text="busy")
        async with _async_client(handler)() as client:
            res = await fetch_one(
                client, FetchInput(url="https://x.test/p", decision="FETCH",
                                   etag=None, last_modified=None),
                tmp_path, self._limiter(), asyncio.Semaphore(2), retries=1)
        assert res.status == 503
        assert res.raw_hash is None
        assert res.error and "503" in res.error

    async def test_network_error_returns_status_zero(self, tmp_path):
        def handler(req):
            raise httpx.ConnectError("name resolution failed")
        async with _async_client(handler)() as client:
            res = await fetch_one(
                client, FetchInput(url="https://x.test/p", decision="FETCH",
                                   etag=None, last_modified=None),
                tmp_path, self._limiter(), asyncio.Semaphore(2), retries=0)
        assert res.status == 0
        assert res.raw_hash is None
        assert res.error

    async def test_304_not_modified_stores_nothing(self, tmp_path):
        def handler(req):
            return httpx.Response(304)
        async with _async_client(handler)() as client:
            res = await fetch_one(
                client, FetchInput(url="https://x.test/p", decision="FETCH_CONDITIONAL",
                                   etag='"abc"', last_modified=None),
                tmp_path, self._limiter(), asyncio.Semaphore(2), retries=0)
        assert res.status == 304
        assert res.raw_hash is None


# ===========================================================================
# Fix #8 — browser availability probe is deferred to the fetch phase
# ===========================================================================

class TestBrowserProbeDeferred:
    def test_config_load_does_not_probe_browser(self, tmp_path, monkeypatch):
        # BUG-1: a box that lost Playwright must still be able to load config
        # and index its HTTP-only majority. Config load must NOT call
        # check_browser_available, even with [browser].enabled=true. We patch
        # the probe to raise (simulating a missing Playwright): if a future
        # change re-adds the eager probe, this test catches it.
        import sift.browser as browsermod
        import sift.cli as climod
        cfg_path = tmp_path / "sift.toml"
        cfg_path.write_text("[browser]\nenabled = true\n")

        def boom():
            raise browsermod.BrowserNotInstalledError("no playwright")
        monkeypatch.setattr(browsermod, "check_browser_available", boom)

        cfg = climod._load_cli_config(cfg_path)   # must not raise
        assert cfg.browser.enabled is True


# ===========================================================================
# `sift run` end-to-end (previously zero CLI coverage of the happy path)
# ===========================================================================

class TestRunPipelineEndToEnd:
    def test_full_run_reaches_terminal_status(self, tmp_path, monkeypatch):
        # Drives plan->fetch->extract->commit->purge->publish through the CLI
        # with an offline fetch. The core guarantee: a completed pipeline
        # records a TERMINAL run status (succeeded/degraded) and never leaves
        # the row stuck at 'running'. Also exercises the run() body that was
        # re-wrapped under the _fail_run_on_crash guard.
        import sift.cli as climod
        from click.testing import CliRunner
        from sift.classify import CLASSIFIER_VERSION
        from sift.manifest import upsert_seed

        (tmp_path / "sift.toml").write_text(
            '[site]\nprofile = "sift.sites.generic:GenericProfile"\n\n'
            '[browser]\nenabled = false\n\n'
            '[seed]\nhost_allow = ["x.test"]\n'
        )
        conn = open_db(paths.manifest_path(tmp_path)); init_schema(conn)
        with transaction(conn):
            upsert_seed(conn, "https://x.test/page", "LIVING", None,
                        CLASSIFIER_VERSION, None, now_utc())
        conn.close()

        async def fake_fetch_all(inputs, root, fl, **kw):
            fl.parent.mkdir(parents=True, exist_ok=True)
            n = 0
            with fl.open("a") as f:
                for inp in inputs:
                    body = (b"<html><body><h1>Page</h1><p>"
                            + b"Real content here. " * 40 + b"</p></body></html>")
                    rh = sha256_hex(body)
                    write_raw_blob(root, rh, body)
                    f.write(FetchResult(
                        url=inp.url, decision=inp.decision, status=200,
                        etag=None, last_modified=None, raw_hash=rh,
                        raw_bytes=len(body), fetched_at=now_utc(), error=None,
                        content_type="text/html").to_json_line())
                    n += 1
            return n
        monkeypatch.setattr(climod, "fetch_all", fake_fetch_all)

        res = CliRunner().invoke(climod.main, [
            "run", "--root", str(tmp_path),
            "--config", str(tmp_path / "sift.toml"),
        ])
        # Exit 0 (published) or 2 (degraded) — both terminal. NOT 1 (crash).
        assert res.exit_code in (0, 2), res.output

        conn = open_db(paths.manifest_path(tmp_path))
        row = conn.execute(
            "SELECT run_id, status FROM runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row is not None
        run_id, status = row[0], row[1]
        assert status in ("succeeded", "degraded"), status
        # A snapshot is written regardless of gate outcome.
        assert paths.snapshot_path(tmp_path, run_id).exists()
        # And the fetch actually ran offline (no network).
        assert paths.fetch_log_path(tmp_path, run_id).exists()


# ===========================================================================
# MCP onboarding surface — server `instructions` + tool annotations
# ===========================================================================

class TestServerInstructions:
    def test_single_index_instructions_front_load_snapshot_status(self, tmp_path):
        from sift import mcp_server
        _make_run(tmp_path, "2026-01-01T00-00-00Z", status="published", link=True)
        ins = mcp_server.build_server(tmp_path).create_initialization_options().instructions
        assert ins
        # The call-first contract must be in the first 512 chars (Codex priority).
        assert "snapshot_status" in ins[:512]
        # Single mode: no multi-index or write-tool guidance.
        assert "list_indexes" not in ins
        assert "index_url" not in ins

    def test_multi_index_instructions_name_list_indexes(self, tmp_path):
        from sift import mcp_server
        for slug in ("alpha", "beta"):
            _make_run(tmp_path / slug, "2026-01-01T00-00-00Z",
                      status="published", link=True)
        ins = mcp_server.build_server(tmp_path).create_initialization_options().instructions
        assert "list_indexes" in ins and "index=<slug>" in ins

    def test_enable_index_instructions_mention_write_loop(self, tmp_path):
        from sift import mcp_server
        _make_run(tmp_path, "2026-01-01T00-00-00Z", status="published", link=True)
        ins = mcp_server.build_server(
            tmp_path, enable_index=True).create_initialization_options().instructions
        assert "index_url" in ins and "index_status" in ins


class TestToolAnnotations:
    def test_read_tools_readonly_write_tool_openworld(self):
        from sift import mcp_server
        tools = {t.name: t for t in
                 mcp_server._tool_descriptors(include_index=True, multi=False)}
        for name in ("grep_corpus", "read_md", "read_facts", "glob_corpus",
                     "list_dir", "query_manifest", "snapshot_status", "index_status"):
            a = tools[name].annotations
            assert a is not None and a.readOnlyHint is True, name
        # The one write tool: not read-only, flagged open-world (fetches the web).
        iu = tools["index_url"].annotations
        assert iu is not None
        assert iu.readOnlyHint is False and iu.openWorldHint is True


# ===========================================================================
# Determinism eval — version skew must NOT be reported as non-determinism
# ===========================================================================

class TestDeterminismVersionSkew:
    def test_skew_separated_from_real_mismatch(self, tmp_path):
        # The eval must distinguish three cases so a corpus that merely needs
        # `sift re-extract` (version skew) never looks like a broken hash
        # invariant (the P0 non-determinism signal).
        from sift.extract import (
            EXTRACTOR_VERSION_HTML, reextract_and_hash,
        )
        from sift.index_profile import apply_index_profile
        from sift.manifest import apply_fetch_result, upsert_seed
        from evals.determinism import run

        apply_index_profile(tmp_path)   # ATO default — same as the eval uses
        conn = open_db(paths.manifest_path(tmp_path)); init_schema(conn)
        now = now_utc()
        html = (b"<html><body><h1>Heading</h1><p>"
                + b"This is a real paragraph of article content. " * 30
                + b"</p></body></html>")
        rh = sha256_hex(html)
        write_raw_blob(tmp_path, rh, html)

        match_url = "https://www.ato.gov.au/a-match"
        real = reextract_and_hash(html, match_url).content_hash
        assert real is not None

        WRONG = "deadbeef" * 8
        # (url, stored_content_hash, stored_extractor_version)
        cases = [
            (match_url, real, EXTRACTOR_VERSION_HTML),                  # match
            ("https://www.ato.gov.au/b-skew", WRONG, "trafilatura-2.0.0-cfg3"),  # skew
            ("https://www.ato.gov.au/c-bug", WRONG, EXTRACTOR_VERSION_HTML),     # P0
        ]
        for url, ch, ev in cases:
            with transaction(conn):
                upsert_seed(conn, url, "LIVING", None, "cv", None, now)
                apply_fetch_result(
                    conn, url=url, now=now, http_status=200,
                    http_etag=None, http_last_modified=None, raw_hash=rh,
                    content_hash=ch, crawler_version="cv",
                    extractor_version=ev, normalizer_version="v1", error=None)

        m = run(tmp_path, "t", conn=conn, sample=100)
        assert m.matches == 1                  # correct hash + current version
        assert m.skipped_version_skew == 1     # wrong hash but STALE version
        assert m.mismatches == 1               # wrong hash + CURRENT version = P0


# ===========================================================================
# Provenance-critical — publish() gate failure must NOT flip current
# ===========================================================================

class TestPublishGateFailNoFlip:
    def test_degraded_run_does_not_flip_current(self, tmp_path):
        from sift.manifest import apply_fetch_result, upsert_seed
        from sift.publish import publish as publish_phase
        conn = open_db(paths.manifest_path(tmp_path)); init_schema(conn)
        (paths.run_dir(tmp_path, "rid") / "md").mkdir(parents=True)
        now = now_utc()
        with transaction(conn):
            upsert_seed(conn, "https://www.ato.gov.au/a", "LIVING", None, "v1", None, now)
            apply_fetch_result(
                conn, url="https://www.ato.gov.au/a", now=now, http_status=200,
                http_etag=None, http_last_modified=None,
                raw_hash="r" + "0" * 63, content_hash="c" + "0" * 63,
                crawler_version="v1", extractor_version="ext",
                normalizer_version="v1", error=None)
        # Force the coverage gate to fail by claiming far more expected URLs
        # than exist; several gates will fail -> passed=False.
        ok, gates, snap = publish_phase(
            conn, tmp_path, "rid", started_at=now, expected_urls=10_000)
        assert ok is False
        # The provenance invariant: a degraded run NEVER flips current.
        assert not (tmp_path / "current").exists()
        # And the snapshot self-reports degraded.
        assert json.loads(snap.read_text())["status"] == "degraded"

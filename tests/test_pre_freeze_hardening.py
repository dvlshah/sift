"""Tests for the pre-freeze hardening pass.

Covers the three P0s and the P1/P2 fixes:
  P0-1  SSRF: redirect to an off-allowlist host is not stored
  P0-1  body cap: oversized body is refused
  P0-2  resolve fallback only treats genuinely-published runs as published
  P0-3  reextract_and_hash is the single canonical hash; profile activation
  P1-4  a crashed `sift run` records a terminal 'failed' status
  P2    query_manifest closes its connection
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from sift import mcp_server, paths
from sift.fetch import FetchInput, MAX_BODY_BYTES, fetch_one


# ---- P0-1: SSRF redirect re-validation ------------------------------------

def _mock_client(handler):
    class _C(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **kw)
    return _C


class TestRedirectAllowlist:
    async def test_redirect_to_off_allowlist_host_is_not_stored(self, tmp_path):
        # The allow-listed origin 302s to an internal host; httpx follows it.
        # The final response must NOT be stored under the original URL.
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.host == "allowed.test":
                return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})
            return httpx.Response(200, text="SECRET-INSTANCE-METADATA")

        from aiolimiter import AsyncLimiter
        async with _mock_client(handler)() as client:
            res = await fetch_one(
                client,
                FetchInput(url="https://allowed.test/page", decision="FETCH",
                           etag=None, last_modified=None),
                tmp_path, AsyncLimiter(100, 1), asyncio.Semaphore(2),
                retries=0,
                allowed_hosts=frozenset({"allowed.test"}),
            )
        assert res.raw_hash is None
        assert res.error and res.error.startswith("redirect-off-allowlist:")
        assert "169.254.169.254" in res.error

    async def test_same_host_redirect_is_stored(self, tmp_path):
        # http->https on the same allowed host is fine — must store.
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.scheme == "http":
                return httpx.Response(301, headers={"location": "https://allowed.test/page"})
            return httpx.Response(200, text="<html>real content</html>")

        from aiolimiter import AsyncLimiter
        async with _mock_client(handler)() as client:
            res = await fetch_one(
                client,
                FetchInput(url="http://allowed.test/page", decision="FETCH",
                           etag=None, last_modified=None),
                tmp_path, AsyncLimiter(100, 1), asyncio.Semaphore(2),
                retries=0,
                allowed_hosts=frozenset({"allowed.test"}),
            )
        assert res.raw_hash is not None
        assert res.error is None

    async def test_none_allowed_hosts_skips_check(self, tmp_path):
        # Back-compat: allowed_hosts=None disables the guard.
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>content</html>")
        from aiolimiter import AsyncLimiter
        async with _mock_client(handler)() as client:
            res = await fetch_one(
                client,
                FetchInput(url="https://anything.test/x", decision="FETCH",
                           etag=None, last_modified=None),
                tmp_path, AsyncLimiter(100, 1), asyncio.Semaphore(2),
                retries=0, allowed_hosts=None,
            )
        assert res.raw_hash is not None


class TestBodyCap:
    async def test_oversized_body_refused(self, tmp_path, monkeypatch):
        # Shrink the cap so we don't have to generate 25MB.
        monkeypatch.setattr(mcp_server, "MAX_BODY_BYTES", 10, raising=False)
        import sift.fetch as fetchmod
        monkeypatch.setattr(fetchmod, "MAX_BODY_BYTES", 10)
        big = "x" * 5000
        def handler(req): return httpx.Response(200, text=big)
        from aiolimiter import AsyncLimiter
        async with _mock_client(handler)() as client:
            res = await fetch_one(
                client,
                FetchInput(url="https://allowed.test/big", decision="FETCH",
                           etag=None, last_modified=None),
                tmp_path, AsyncLimiter(100, 1), asyncio.Semaphore(2),
                retries=0, allowed_hosts=frozenset({"allowed.test"}),
            )
        assert res.raw_hash is None
        assert res.error and res.error.startswith("body-too-large:")


# ---- P0-2: resolve fallback honors genuine publish ------------------------

def _make_run(root: Path, run_id: str, *, status: str, link: bool) -> None:
    """Create a runs/<run_id> dir with a snapshot.json of the given status,
    optionally flipping the `current` symlink to it."""
    rd = paths.run_dir(root, run_id)
    (rd / "md").mkdir(parents=True)
    (rd / "snapshot.json").write_text(json.dumps({"run_id": run_id, "status": status}))
    if link:
        cur = root / "current"
        if cur.exists() or cur.is_symlink():
            cur.unlink()
        cur.symlink_to(rd)


class TestResolveFallback:
    def test_degraded_only_run_is_not_published(self, tmp_path):
        # A single degraded run, no current symlink → must resolve unpublished.
        _make_run(tmp_path, "2026-01-01T00-00-00Z", status="degraded", link=False)
        resolved, is_pub = mcp_server._resolve_root(tmp_path)
        assert is_pub is False

    def test_published_run_via_symlink_is_published(self, tmp_path):
        _make_run(tmp_path, "2026-02-02T00-00-00Z", status="published", link=True)
        resolved, is_pub = mcp_server._resolve_root(tmp_path)
        assert is_pub is True
        assert resolved.name == "2026-02-02T00-00-00Z"

    def test_degraded_newer_than_published_serves_published(self, tmp_path):
        # current points at the older published run; a newer degraded run
        # exists. Must serve the PUBLISHED one, never the newer degraded.
        _make_run(tmp_path, "2026-03-01T00-00-00Z", status="published", link=True)
        _make_run(tmp_path, "2026-03-09T00-00-00Z", status="degraded", link=False)
        resolved, is_pub = mcp_server._resolve_root(tmp_path)
        assert is_pub is True
        assert resolved.name == "2026-03-01T00-00-00Z"

    def test_snapshot_status_reports_unpublished_degraded(self, tmp_path):
        _make_run(tmp_path, "2026-04-04T00-00-00Z", status="degraded", link=False)
        res = mcp_server.tool_snapshot_status(tmp_path)
        body = json.loads(res.content[0].text)
        assert body["published"] is False
        assert body["unpublished_latest_run"] == "2026-04-04T00-00-00Z"
        assert body["unpublished_latest_status"] == "degraded"


# ---- P0-3: canonical reextract_and_hash -----------------------------------

class TestReextractAndHash:
    def test_matches_inline_pipeline(self):
        from sift.extract import (
            reextract_and_hash, ExtractInput, PRIMARY_STRATEGIES,
            select_primary, inject_heading_anchors, hash_normalized_body,
        )
        from sift.sites import current_profile
        html = b"<html><body><h1>Title</h1><p>" + b"Body text. " * 30 + b"</p></body></html>"
        url = "https://x.test/page"
        # inline replication
        inp = ExtractInput(raw=html, url=url, content_type=None,
                           body_kind=current_profile().body_kind(url))
        primary = select_primary(inp, PRIMARY_STRATEGIES)
        md, _ = primary.extract(html, url)
        annotated, _ = inject_heading_anchors(md)
        expected = hash_normalized_body(annotated)
        res = reextract_and_hash(html, url)
        assert res.ok
        assert res.content_hash == expected

    def test_extract_failure_returns_not_ok(self):
        from sift.extract import reextract_and_hash
        res = reextract_and_hash(b"", "https://x.test/empty")
        assert res.ok is False
        assert res.content_hash is None


# ---- P0-3: profile activation per index -----------------------------------

class TestIndexProfile:
    def test_reads_site_profile_from_sift_toml(self, tmp_path):
        from sift.index_profile import index_profile_path
        (tmp_path / "sift.toml").write_text(
            '[site]\nprofile = "sift.sites.generic:GenericProfile"\n'
        )
        assert index_profile_path(tmp_path) == "sift.sites.generic:GenericProfile"

    def test_defaults_when_no_toml(self, tmp_path):
        from sift.index_profile import index_profile_path
        # No sift.toml → config default (ATO), matching build-time default.
        assert "ATOProfile" in index_profile_path(tmp_path)

    def test_apply_activates_profile(self, tmp_path):
        from sift.index_profile import apply_index_profile
        from sift import sites
        (tmp_path / "sift.toml").write_text(
            '[site]\nprofile = "sift.sites.generic:GenericProfile"\n'
        )
        apply_index_profile(tmp_path)
        assert type(sites.current_profile()).__name__ == "GenericProfile"
        # restore ATO default for other tests
        sites.set_profile(sites.load_profile("sift.sites.ato:ATOProfile"))


# ---- P1-4: crashed run records a terminal 'failed' status -----------------

class TestRunStatusOnCrash:
    def test_crash_records_failed_not_stuck_running(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        from sift.cli import main
        from sift.manifest import open_db, init_schema
        from sift import paths as sift_paths

        # Minimal index + config (browser off so _load_cli_config won't probe).
        (tmp_path / "sift.toml").write_text(
            "[browser]\nenabled = false\n\n[seed]\nhost_allow = [\"x.test\"]\n"
        )
        conn = open_db(sift_paths.manifest_path(tmp_path))
        init_schema(conn)
        conn.close()

        # Make the PLAN phase blow up so the pipeline crashes after
        # record_run_start but before the terminal record_run_end.
        import sift.cli as climod
        def boom(*a, **k):
            raise RuntimeError("synthetic plan crash")
        monkeypatch.setattr(climod, "plan_phase", boom)

        runner = CliRunner()
        result = runner.invoke(main, [
            "run", "--root", str(tmp_path), "--config", str(tmp_path / "sift.toml"),
        ])
        # Click surfaces the exception (exit code != 0).
        assert result.exit_code != 0

        # The runs row must be terminal 'failed', NOT stuck at 'running'.
        conn = open_db(sift_paths.manifest_path(tmp_path))
        row = conn.execute(
            "SELECT status, error FROM runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "failed", f"expected failed, got {row[0]}"
        assert "synthetic plan crash" in (row[1] or "")


# ---- P2: query_manifest closes its connection -----------------------------

class TestQueryManifestNoLeak:
    def test_connection_closed_after_query(self, tmp_path, monkeypatch):
        from sift.manifest import init_schema, open_db
        conn0 = open_db(paths.manifest_path(tmp_path))
        init_schema(conn0)
        conn0.close()

        opened = []
        real_connect = mcp_server.sqlite3.connect

        def tracking_connect(*a, **kw):
            c = real_connect(*a, **kw)
            opened.append(c)
            return c

        monkeypatch.setattr(mcp_server.sqlite3, "connect", tracking_connect)
        res = mcp_server.tool_query_manifest(
            tmp_path, "SELECT name FROM sqlite_master WHERE type='table'",
            index_root=tmp_path,
        )
        assert not res.isError
        # Every connection opened by the tool must now be closed.
        for c in opened:
            with pytest.raises(Exception):
                c.execute("SELECT 1")   # closed connection raises

"""UA-override threading + zero-seed / all-filtered warnings.

Covers:
  * `[crawl] user_agent` now flows from CrawlConfig into
      - `fetch_all`'s httpx.AsyncClient default headers, and
      - `walk_sitemap`'s sync httpx.Client, with a UA-rejected hint on 401/403.
  * `sift seed` warns on stderr when any active source yields zero URLs or
    when everything was filtered, so the operator gets a signal beyond the
    green-looking JSON summary.
"""
from __future__ import annotations

import httpx
import pytest
from click.testing import CliRunner

from sift import fetch as fetch_mod
from sift.cli import main as cli_main
from sift.fetch import FetchInput, USER_AGENT, fetch_all
from sift.sources import sitemap as sitemap_mod
from sift.sources.sitemap import walk_sitemap


# ---- helpers ---------------------------------------------------------------

def _proxy_async_client(seen: list, *, status: int = 200,
                       body: bytes = b"<html>ok</html>") -> type:
    """Subclass of httpx.AsyncClient that records request headers and serves
    fake responses via a MockTransport — keeps real defaults / redirect /
    header-merge behavior intact so we test the actual UA path."""
    class _ProxyAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            def handler(req):
                seen.append(dict(req.headers))
                return httpx.Response(status, content=body,
                                      headers={"content-type": "text/html"})
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)
    return _ProxyAsyncClient


def _proxy_sync_client(seen: list, *, status: int = 200,
                      body: bytes | None = None) -> type:
    if body is None:
        body = (b'<?xml version="1.0" encoding="UTF-8"?>'
                b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                b'<url><loc>https://example.test/a</loc></url>'
                b'<url><loc>https://example.test/b</loc></url>'
                b'</urlset>')

    class _ProxySyncClient(httpx.Client):
        def __init__(self, *args, **kwargs):
            def handler(req):
                seen.append(dict(req.headers))
                return httpx.Response(status, content=body,
                                      headers={"content-type": "application/xml"})
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)
    return _ProxySyncClient


# ---- fetch_all UA threading -------------------------------------------------

class TestFetchAllUserAgent:
    @pytest.mark.asyncio
    async def test_custom_ua_appears_in_outgoing_request(self, tmp_path, monkeypatch):
        seen: list = []
        monkeypatch.setattr(fetch_mod.httpx, "AsyncClient",
                            _proxy_async_client(seen))
        await fetch_all(
            [FetchInput(url="https://example.test/page", decision="FETCH",
                        etag=None, last_modified=None)],
            tmp_path, tmp_path / "fetch.log",
            user_agent="bench/0.1 (+contact)",
        )
        assert seen, "expected at least one captured request"
        assert seen[0].get("user-agent") == "bench/0.1 (+contact)"

    @pytest.mark.asyncio
    async def test_default_ua_when_unset(self, tmp_path, monkeypatch):
        seen: list = []
        monkeypatch.setattr(fetch_mod.httpx, "AsyncClient",
                            _proxy_async_client(seen))
        await fetch_all(
            [FetchInput(url="https://example.test/page", decision="FETCH",
                        etag=None, last_modified=None)],
            tmp_path, tmp_path / "fetch.log",
        )
        assert seen and seen[0].get("user-agent") == USER_AGENT


# ---- walk_sitemap UA threading + 401/403 hint ------------------------------

class TestSitemapWalkerUserAgent:
    def test_custom_ua_in_sitemap_request(self, monkeypatch):
        seen: list = []
        monkeypatch.setattr(httpx, "Client", _proxy_sync_client(seen))
        out = walk_sitemap("https://example.test/sitemap.xml",
                            user_agent="bench/0.1")
        assert seen and seen[0].get("user-agent") == "bench/0.1"
        # And the fake urlset really did parse:
        assert ("https://example.test/a", None) in out

    def test_default_ua_when_unset(self, monkeypatch):
        seen: list = []
        monkeypatch.setattr(httpx, "Client", _proxy_sync_client(seen))
        walk_sitemap("https://example.test/sitemap.xml")
        assert seen and seen[0].get("user-agent") == USER_AGENT

    def test_403_emits_ua_hint(self, monkeypatch, capsys):
        seen: list = []
        # 403 with no custom UA → walker should emit the UA-rejection hint
        monkeypatch.setattr(httpx, "Client",
                            _proxy_sync_client(seen, status=403, body=b""))
        out = walk_sitemap("https://example.test/sitemap.xml")
        assert out == []
        err = capsys.readouterr().err
        assert "sitemap fetch failed" in err
        assert "[crawl] user_agent" in err

    def test_403_with_custom_ua_does_not_emit_hint(self, monkeypatch, capsys):
        """If the operator already set a custom UA and still got 403, the hint
        is wrong/noisy — suppress it."""
        seen: list = []
        monkeypatch.setattr(httpx, "Client",
                            _proxy_sync_client(seen, status=403, body=b""))
        walk_sitemap("https://example.test/sitemap.xml",
                      user_agent="custom-already-set/1.0")
        err = capsys.readouterr().err
        assert "sitemap fetch failed" in err
        assert "[crawl] user_agent" not in err


# ---- seed-command zero-seed / all-filtered warnings ------------------------

class TestSeedZeroWarning:
    def _run_seed(self, monkeypatch, tmp_path, *, walk_returns: list,
                  host_allow=("example.test",)):
        """Drive `sift seed --from-sitemap` with a stubbed walk_sitemap.
        Returns CliRunner result so the caller can inspect exit_code / stderr.
        SitemapSource.discover() delegates to walk_sitemap, so patching at the
        source-of-truth covers the CLI path."""
        cfg_path = tmp_path / "sift.toml"
        cfg_path.write_text(
            '[site]\nprofile = "sift.sites.generic:GenericProfile"\n'
            '[browser]\nenabled = false\n'
            f'[seed]\nhost_allow = {list(host_allow)!r}\n'
        )
        monkeypatch.setattr(sitemap_mod, "walk_sitemap",
                            lambda url, **kw: walk_returns)
        runner = CliRunner()
        return runner.invoke(cli_main, [
            "init", "--root", str(tmp_path / "idx")
        ]), runner, cfg_path

    def test_zero_seeded_emits_warning(self, tmp_path, monkeypatch):
        init_res, runner, cfg = self._run_seed(monkeypatch, tmp_path, walk_returns=[])
        assert init_res.exit_code == 0
        res = runner.invoke(cli_main, [
            "seed", "--root", str(tmp_path / "idx"),
            "--config", str(cfg),
            "--from-sitemap", "https://example.test/sitemap.xml",
        ])
        assert res.exit_code == 0
        # New shape: warning names whichever discovery source(s) were active
        assert "[sitemap]" in res.stderr
        assert "yielded zero URLs" in res.stderr
        assert "[crawl] user_agent" in res.stderr  # hint at the most common cause

    def test_all_filtered_emits_warning(self, tmp_path, monkeypatch):
        # Walker returns URLs but they're on a host that isn't allow-listed
        walk = [("https://other.test/a", None), ("https://other.test/b", None)]
        init_res, runner, cfg = self._run_seed(
            monkeypatch, tmp_path, walk_returns=walk,
            host_allow=("example.test",),
        )
        assert init_res.exit_code == 0
        res = runner.invoke(cli_main, [
            "seed", "--root", str(tmp_path / "idx"),
            "--config", str(cfg),
            "--from-sitemap", "https://example.test/sitemap.xml",
        ])
        assert res.exit_code == 0, res.stderr
        assert "were filtered out" in res.stderr
        assert "host_filter=2" in res.stderr

    def test_normal_case_emits_no_warning(self, tmp_path, monkeypatch):
        walk = [("https://example.test/a", None), ("https://example.test/b", None)]
        init_res, runner, cfg = self._run_seed(
            monkeypatch, tmp_path, walk_returns=walk,
            host_allow=("example.test",),
        )
        assert init_res.exit_code == 0
        res = runner.invoke(cli_main, [
            "seed", "--root", str(tmp_path / "idx"),
            "--config", str(cfg),
            "--from-sitemap", "https://example.test/sitemap.xml",
        ])
        assert res.exit_code == 0, res.stderr
        # No "yielded zero" or "filtered out" warnings — happy path is quiet.
        assert "yielded zero" not in res.stderr
        assert "filtered out" not in res.stderr

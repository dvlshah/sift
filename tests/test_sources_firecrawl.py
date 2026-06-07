"""Tests for the Firecrawl /v2/map discovery source.

Uses httpx.MockTransport to intercept the actual REST call so the tests run
offline and assert on the wire-level shape Firecrawl receives — payload keys,
auth header, parameter forwarding — plus our error-mapping behavior on the
common HTTP statuses.
"""
from __future__ import annotations

import json

import httpx
import pytest
from click.testing import CliRunner

from sift.cli import main as cli_main
from sift.sources import firecrawl as fc_mod
from sift.sources.firecrawl import (
    FirecrawlError, FirecrawlMapSource, walk_firecrawl_map,
)


# ---- helpers ---------------------------------------------------------------

def _proxy_client(handler) -> type:
    """httpx.Client subclass that routes everything through `handler`
    (a callable taking httpx.Request and returning httpx.Response)."""
    class _ProxyClient(httpx.Client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)
    return _ProxyClient


def _ok_response(links, *, wrap_in_data: bool = False):
    """Build a /v2/map-shaped success response.

    Defaults to the real /v2 shape (``links`` at top level); pass
    ``wrap_in_data=True`` to exercise the legacy SDK-wrapped shape that some
    older callers and the firecrawl CLI's JSON output still emit. Both are
    accepted by walk_firecrawl_map.
    """
    link_objs = [{"url": u} for u in links]
    body: dict = {"success": True}
    if wrap_in_data:
        body["data"] = {"links": link_objs}
    else:
        body["links"] = link_objs
    return httpx.Response(200, json=body)


# ---- walk_firecrawl_map happy-path + payload assertions --------------------

class TestPayloadAndAuth:
    def test_payload_has_url_and_limit(self, monkeypatch):
        seen = {}

        def handler(req):
            assert req.url.path == "/v2/map"
            seen["headers"] = dict(req.headers)
            seen["body"] = json.loads(req.content)
            return _ok_response(["https://x.test/a", "https://x.test/b"])

        monkeypatch.setattr(httpx, "Client", _proxy_client(handler))
        out = walk_firecrawl_map("https://x.test", api_key="fc-test-key",
                                 limit=42)
        assert seen["body"]["url"] == "https://x.test"
        assert seen["body"]["limit"] == 42
        assert "search" not in seen["body"]
        assert "includeSubdomains" not in seen["body"]
        assert seen["headers"]["authorization"] == "Bearer fc-test-key"
        assert out == [("https://x.test/a", None), ("https://x.test/b", None)]

    def test_optional_params_forwarded(self, monkeypatch):
        seen = {}

        def handler(req):
            seen["body"] = json.loads(req.content)
            return _ok_response([])

        monkeypatch.setattr(httpx, "Client", _proxy_client(handler))
        walk_firecrawl_map("https://x.test", api_key="fc-x",
                           limit=10, search="auth",
                           include_subdomains=True)
        assert seen["body"]["search"] == "auth"
        assert seen["body"]["includeSubdomains"] is True

    def test_api_key_from_env_when_unset(self, monkeypatch):
        monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-from-env")
        seen = {}

        def handler(req):
            seen["headers"] = dict(req.headers)
            return _ok_response([])

        monkeypatch.setattr(httpx, "Client", _proxy_client(handler))
        walk_firecrawl_map("https://x.test")
        assert seen["headers"]["authorization"] == "Bearer fc-from-env"

    def test_missing_key_raises_actionable_error(self, monkeypatch):
        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
        with pytest.raises(FirecrawlError, match="FIRECRAWL_API_KEY is not set"):
            walk_firecrawl_map("https://x.test")


# ---- response-shape filtering ---------------------------------------------

class TestResponseShape:
    """Verify walk_firecrawl_map handles both /v2 response shapes."""

    def test_v2_top_level_links(self, monkeypatch):
        # Real /v2/map shape: {success, links: [...]}.
        def handler(req):
            return _ok_response(["https://x.test/a", "https://x.test/b"],
                                wrap_in_data=False)

        monkeypatch.setattr(httpx, "Client", _proxy_client(handler))
        out = walk_firecrawl_map("https://x.test", api_key="fc-x")
        assert out == [("https://x.test/a", None), ("https://x.test/b", None)]

    def test_legacy_data_wrapped_links(self, monkeypatch):
        # Legacy / CLI-wrapped shape: {success, data: {links: [...]}}.
        def handler(req):
            return _ok_response(["https://x.test/a"], wrap_in_data=True)

        monkeypatch.setattr(httpx, "Client", _proxy_client(handler))
        out = walk_firecrawl_map("https://x.test", api_key="fc-x")
        assert out == [("https://x.test/a", None)]


class TestResponseFiltering:
    def test_probe_artifacts_stripped(self, monkeypatch):
        def handler(req):
            return _ok_response([
                "https://x.test/real-page",
                "https://x.test/?fc_probe=123",
                "https://x.test/another",
                "https://x.test/?dryrun=1",
                "https://x.test/?pool_concurrency=4",
            ])

        monkeypatch.setattr(httpx, "Client", _proxy_client(handler))
        out = walk_firecrawl_map("https://x.test", api_key="fc-x")
        urls = [u for u, _ in out]
        assert urls == ["https://x.test/real-page", "https://x.test/another"]

    def test_empty_links_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(httpx, "Client",
                            _proxy_client(lambda r: _ok_response([])))
        assert walk_firecrawl_map("https://x.test", api_key="fc-x") == []

    def test_success_false_raises(self, monkeypatch):
        def handler(req):
            return httpx.Response(200, json={"success": False, "error": "nope"})

        monkeypatch.setattr(httpx, "Client", _proxy_client(handler))
        with pytest.raises(FirecrawlError, match="success=False"):
            walk_firecrawl_map("https://x.test", api_key="fc-x")


# ---- HTTP-status error mapping ---------------------------------------------

class TestErrorMapping:
    @pytest.mark.parametrize("code,marker", [
        (401, "FIRECRAWL_API_KEY rejected"),
        (402, "quota exceeded"),
        (429, "rate-limited"),
        (503, "rate-limited"),
        (500, "HTTP 500"),
    ])
    def test_each_status_gets_actionable_hint(self, monkeypatch, code, marker):
        def handler(req):
            return httpx.Response(code, content=b"")

        monkeypatch.setattr(httpx, "Client", _proxy_client(handler))
        with pytest.raises(FirecrawlError, match=marker):
            walk_firecrawl_map("https://x.test", api_key="fc-x")


# ---- FirecrawlMapSource adapter --------------------------------------------

class TestFirecrawlMapSource:
    def test_source_yields_walk_output(self, monkeypatch):
        def handler(req):
            return _ok_response(["https://x.test/a", "https://x.test/b"])

        monkeypatch.setattr(httpx, "Client", _proxy_client(handler))
        src = FirecrawlMapSource("https://x.test", api_key="fc-x")
        assert list(src.discover()) == [
            ("https://x.test/a", None),
            ("https://x.test/b", None),
        ]
        assert src.name == "firecrawl-map"


# ---- seed-command integration ---------------------------------------------

class TestSeedCommandIntegration:
    def test_from_firecrawl_map_flag_works(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "sift.toml"
        cfg_path.write_text(
            '[site]\nprofile = "sift.sites.generic:GenericProfile"\n'
            '[browser]\nenabled = false\n'
            '[seed]\nhost_allow = ["x.test"]\n'
        )
        # Stub the walker at its source so the CLI path is exercised end-to-end.
        monkeypatch.setattr(fc_mod, "walk_firecrawl_map",
                            lambda url, **kw: [("https://x.test/a", None),
                                               ("https://x.test/b", None)])
        runner = CliRunner()
        init = runner.invoke(cli_main, ["init", "--root", str(tmp_path / "idx")])
        assert init.exit_code == 0
        res = runner.invoke(cli_main, [
            "seed", "--root", str(tmp_path / "idx"),
            "--config", str(cfg_path),
            "--from-firecrawl-map", "https://x.test",
        ])
        assert res.exit_code == 0, res.stderr
        payload = json.loads(res.output)
        assert payload["inserted"] == 2
        # No zero/filtered warning on the happy path.
        assert "yielded zero" not in res.stderr
        assert "filtered out" not in res.stderr

    def test_firecrawl_error_surfaces_as_warn_not_crash(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "sift.toml"
        cfg_path.write_text(
            '[site]\nprofile = "sift.sites.generic:GenericProfile"\n'
            '[browser]\nenabled = false\n'
            '[seed]\nhost_allow = ["x.test"]\n'
        )

        def boom(url, **kw):
            raise FirecrawlError("auth rejected")

        monkeypatch.setattr(fc_mod, "walk_firecrawl_map", boom)
        runner = CliRunner()
        runner.invoke(cli_main, ["init", "--root", str(tmp_path / "idx")])
        res = runner.invoke(cli_main, [
            "seed", "--root", str(tmp_path / "idx"),
            "--config", str(cfg_path),
            "--from-firecrawl-map", "https://x.test",
        ])
        # Should not raise / crash the CLI; should surface as a warn line
        # and still emit the zero-URL summary message.
        assert res.exit_code == 0, res.output
        assert "firecrawl-map discovery failed" in res.stderr
        assert "auth rejected" in res.stderr

    def test_combining_sources_aggregates(self, tmp_path, monkeypatch):
        """JSON seed + Firecrawl seed in one invocation should be additive."""
        cfg_path = tmp_path / "sift.toml"
        cfg_path.write_text(
            '[site]\nprofile = "sift.sites.generic:GenericProfile"\n'
            '[browser]\nenabled = false\n'
            '[seed]\nhost_allow = ["x.test"]\n'
        )
        json_path = tmp_path / "seed.json"
        json_path.write_text(json.dumps({"links": [{"url": "https://x.test/from-json"}]}))
        monkeypatch.setattr(fc_mod, "walk_firecrawl_map",
                            lambda url, **kw: [("https://x.test/from-firecrawl", None)])
        runner = CliRunner()
        runner.invoke(cli_main, ["init", "--root", str(tmp_path / "idx")])
        res = runner.invoke(cli_main, [
            "seed", "--root", str(tmp_path / "idx"),
            "--config", str(cfg_path),
            "--from-json", str(json_path),
            "--from-firecrawl-map", "https://x.test",
        ])
        assert res.exit_code == 0, res.stderr
        payload = json.loads(res.output)
        assert payload["inserted"] == 2  # both sources flowed through

    def test_no_source_provided_errors(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "sift.toml"
        cfg_path.write_text(
            '[site]\nprofile = "sift.sites.generic:GenericProfile"\n'
            '[browser]\nenabled = false\n'
            '[seed]\nhost_allow = ["x.test"]\n'
        )
        runner = CliRunner()
        runner.invoke(cli_main, ["init", "--root", str(tmp_path / "idx")])
        res = runner.invoke(cli_main, [
            "seed", "--root", str(tmp_path / "idx"),
            "--config", str(cfg_path),
        ])
        assert res.exit_code != 0
        assert "--from-firecrawl-map" in res.output  # error mentions the new option

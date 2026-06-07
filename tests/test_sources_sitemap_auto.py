"""Auto-discovery + lenient-parsing tests for the sitemap source.

Covers the v1.1 sitemap-logic revision:
  * ``discover_sitemaps`` against robots.txt + well-known paths
  * gzip decompression
  * plain-text fallback parser
  * lenient XML (continues past one bad sitemap)
  * ``AutoSitemapSource`` end-to-end
"""
from __future__ import annotations

import gzip

import httpx
import pytest

from sift.sources import sitemap as sitemap_mod
from sift.sources.sitemap import (
    AutoSitemapSource,
    _decompress_if_gzip,
    _parse_sitemap_body,
    discover_sitemaps,
    walk_sitemap,
)


def _proxy_client(handler) -> type:
    class _ProxyClient(httpx.Client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)
    return _ProxyClient


_XML_URLSET = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    b'<url><loc>https://x.test/a</loc><lastmod>2026-05-01</lastmod></url>'
    b'<url><loc>https://x.test/b</loc></url>'
    b'</urlset>'
)
_XML_INDEX = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    b'<sitemap><loc>https://x.test/sitemap-en.xml</loc></sitemap>'
    b'<sitemap><loc>https://x.test/sitemap-fr.xml</loc></sitemap>'
    b'</sitemapindex>'
)


# ---- _parse_sitemap_body --------------------------------------------------

class TestParseSitemapBody:
    def test_urlset_returns_urls(self):
        urls, children = _parse_sitemap_body(_XML_URLSET, "https://x.test/sitemap.xml")
        assert urls == [("https://x.test/a", "2026-05-01"),
                        ("https://x.test/b", None)]
        assert children == []

    def test_index_returns_children(self):
        urls, children = _parse_sitemap_body(_XML_INDEX, "https://x.test/sitemap.xml")
        assert urls == []
        assert children == ["https://x.test/sitemap-en.xml",
                            "https://x.test/sitemap-fr.xml"]

    def test_plain_text_fallback(self):
        body = (b"https://x.test/page-1\nhttps://x.test/page-2\n"
                b"# comment line ignored\nhttps://x.test/page-3\n"
                b"not-a-url-skipped")
        urls, children = _parse_sitemap_body(body, "https://x.test/urls.txt")
        assert children == []
        assert [u for u, _ in urls] == [
            "https://x.test/page-1",
            "https://x.test/page-2",
            "https://x.test/page-3",
        ]

    def test_malformed_xml_falls_back_to_plain_text(self):
        # XML doesn't parse, but the document has well-formed URL lines.
        # Plain-text fallback rescues them — same shape we'd see if a CMS
        # silently switched from XML to a text export.
        body = (b"<urlset>broken header\n"
                b"https://x.test/page-a\n"
                b"https://x.test/page-b\n")
        urls, _ = _parse_sitemap_body(body, "https://x.test/sm.xml")
        assert [u for u, _ in urls] == [
            "https://x.test/page-a",
            "https://x.test/page-b",
        ]


# ---- _decompress_if_gzip --------------------------------------------------

class TestGzipDecompression:
    def test_gz_url_triggers_decompress(self):
        compressed = gzip.compress(_XML_URLSET)
        out = _decompress_if_gzip(compressed, "https://x.test/sitemap.xml.gz", None)
        assert out == _XML_URLSET

    def test_content_encoding_header_triggers_decompress(self):
        compressed = gzip.compress(_XML_URLSET)
        out = _decompress_if_gzip(compressed, "https://x.test/sitemap.xml",
                                   "gzip")
        assert out == _XML_URLSET

    def test_non_gzip_passes_through(self):
        out = _decompress_if_gzip(_XML_URLSET, "https://x.test/sitemap.xml", None)
        assert out == _XML_URLSET

    def test_claimed_gzip_but_corrupt_falls_through(self):
        # Server lied about gzip; we shouldn't raise.
        out = _decompress_if_gzip(b"not gzip bytes", "https://x.test/sm.gz",
                                   None)
        assert out == b"not gzip bytes"


# ---- discover_sitemaps ----------------------------------------------------

class TestDiscoverSitemaps:
    def test_reads_sitemaps_from_robots(self, monkeypatch):
        def handler(req):
            if req.url.path == "/robots.txt":
                return httpx.Response(200, text=(
                    "User-agent: *\n"
                    "Disallow: /admin/\n"
                    "Sitemap: https://x.test/sitemap-a.xml\n"
                    "Sitemap: https://x.test/sitemap-b.xml\n"
                ))
            return httpx.Response(404)
        monkeypatch.setattr(httpx, "Client", _proxy_client(handler))
        out = discover_sitemaps("https://x.test")
        assert out == ["https://x.test/sitemap-a.xml",
                       "https://x.test/sitemap-b.xml"]

    def test_resolves_relative_sitemap_paths_in_robots(self, monkeypatch):
        def handler(req):
            if req.url.path == "/robots.txt":
                return httpx.Response(200, text="Sitemap: /sitemap-en.xml\n")
            return httpx.Response(404)
        monkeypatch.setattr(httpx, "Client", _proxy_client(handler))
        out = discover_sitemaps("https://x.test")
        assert out == ["https://x.test/sitemap-en.xml"]

    def test_falls_back_to_well_known_paths(self, monkeypatch):
        def handler(req):
            p = req.url.path
            if p == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nDisallow:\n")
            if p in ("/sitemap.xml", "/wp-sitemap.xml"):
                return httpx.Response(200, content=_XML_URLSET)
            return httpx.Response(404)
        monkeypatch.setattr(httpx, "Client", _proxy_client(handler))
        out = discover_sitemaps("https://x.test")
        # Returns the well-known paths that responded 200, in probe order
        assert "https://x.test/sitemap.xml" in out
        assert "https://x.test/wp-sitemap.xml" in out

    def test_accepts_bare_domain(self, monkeypatch):
        def handler(req):
            if req.url.path == "/robots.txt":
                return httpx.Response(200, text="Sitemap: https://x.test/sm.xml\n")
            return httpx.Response(404)
        monkeypatch.setattr(httpx, "Client", _proxy_client(handler))
        # No https:// prefix — should be normalized
        out = discover_sitemaps("x.test")
        assert out == ["https://x.test/sm.xml"]

    def test_no_sitemap_anywhere_returns_empty(self, monkeypatch):
        monkeypatch.setattr(httpx, "Client",
                            _proxy_client(lambda req: httpx.Response(404)))
        out = discover_sitemaps("https://x.test")
        assert out == []

    def test_head_5xx_falls_back_to_get(self, monkeypatch):
        """Regression: canada.ca returns 503 on HEAD /sitemap.xml (Akamai
        WAF) but 200 on GET. The earlier ``status_code == 405`` fallback
        missed this; the broadened ``!= 200`` fallback should catch it."""
        def handler(req):
            if req.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nDisallow:\n")
            if req.url.path == "/sitemap.xml":
                return (httpx.Response(503) if req.method == "HEAD"
                        else httpx.Response(200, content=_XML_URLSET))
            return httpx.Response(404)
        monkeypatch.setattr(httpx, "Client", _proxy_client(handler))
        out = discover_sitemaps("https://x.test")
        assert out == ["https://x.test/sitemap.xml"]

    def test_head_403_falls_back_to_get(self, monkeypatch):
        # Same shape as 503 — some WAFs return 403 to method probes too.
        def handler(req):
            if req.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nDisallow:\n")
            if req.url.path == "/sitemap.xml":
                return (httpx.Response(403) if req.method == "HEAD"
                        else httpx.Response(200, content=_XML_URLSET))
            return httpx.Response(404)
        monkeypatch.setattr(httpx, "Client", _proxy_client(handler))
        out = discover_sitemaps("https://x.test")
        assert out == ["https://x.test/sitemap.xml"]


# ---- AutoSitemapSource end-to-end -----------------------------------------

class TestAutoSitemapSource:
    def test_discovers_and_walks(self, monkeypatch):
        def handler(req):
            p = req.url.path
            if p == "/robots.txt":
                return httpx.Response(200, text=(
                    "Sitemap: https://x.test/sitemap.xml\n"
                ))
            if p == "/sitemap.xml":
                return httpx.Response(200, content=_XML_URLSET)
            return httpx.Response(404)
        monkeypatch.setattr(httpx, "Client", _proxy_client(handler))
        src = AutoSitemapSource("https://x.test")
        out = list(src.discover())
        assert ("https://x.test/a", "2026-05-01") in out
        assert ("https://x.test/b", None) in out

    def test_dedupes_across_multiple_sitemaps(self, monkeypatch):
        # Two sitemaps in robots.txt, both referencing the same URL
        def handler(req):
            p = req.url.path
            if p == "/robots.txt":
                return httpx.Response(200, text=(
                    "Sitemap: https://x.test/sm-1.xml\n"
                    "Sitemap: https://x.test/sm-2.xml\n"
                ))
            if p in ("/sm-1.xml", "/sm-2.xml"):
                return httpx.Response(200, content=_XML_URLSET)
            return httpx.Response(404)
        monkeypatch.setattr(httpx, "Client", _proxy_client(handler))
        src = AutoSitemapSource("https://x.test")
        out = list(src.discover())
        # Each URL appears once despite being in both sitemaps
        urls = [u for u, _ in out]
        assert len(urls) == len(set(urls))

    def test_warns_when_nothing_discovered(self, monkeypatch, capsys):
        monkeypatch.setattr(httpx, "Client",
                            _proxy_client(lambda req: httpx.Response(404)))
        src = AutoSitemapSource("https://x.test")
        out = list(src.discover())
        assert out == []
        assert "no sitemaps discovered" in capsys.readouterr().err

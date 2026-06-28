"""Recursive link frontier — discover in-scope pages beyond the sitemap.

The manifest-as-frontier model (engine-hardening §4.2): each ``sift seed
--from-frontier`` pass reads the HTML pages fetched so far, extracts their
in-scope ``<a href>`` links, and yields the ones not already in the manifest as
new UNSEEN rows for the next run to fetch. Iterating
``seed -> run -> seed --from-frontier -> run`` crawls a site that has no (or an
incomplete) ``sitemap.xml`` — the dominant gap today, since the only alternative
is the Firecrawl ``/map`` cap of 500 or a hand-rolled URL list.

Idempotent — the seed pipeline canonicalizes, host-filters, exclude-filters,
robots-checks and upsert-dedups every yielded URL, so re-running is safe — and
bounded by ``max_urls``. A per-root harvest-state file records which raw blobs a
pass already drained, so the next pass only reads pages it hasn't seen and the
crawl advances one hop at a time instead of re-scanning the whole corpus.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

from . import SeedSource


def extract_links(html: bytes, base_url: str, allowed_hosts: frozenset[str]) -> set[str]:
    """In-scope, canonicalized ``<a href>`` links from an HTML page.

    Returns an empty set for non-HTML bodies (a PDF/JSON blob yields no links).
    Hosts are filtered to ``allowed_hosts`` so the frontier stays in scope; the
    seed pipeline applies excludes/robots/canonicalization again downstream.
    """
    head = html[:64].lstrip(b"\xef\xbb\xbf\x00 \t\r\n\f\v").lower()  # tolerate a BOM
    if not (head.startswith(b"<") or b"<html" in html[:4096].lower()):
        return set()
    from selectolax.parser import HTMLParser

    from ..classify import canonicalize_url
    try:
        tree = HTMLParser(html.decode("utf-8", "replace"))
    except Exception:
        return set()
    out: set[str] = set()
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href[0] in "#?" or href.lower().startswith(
                ("mailto:", "javascript:", "tel:", "data:")):
            continue
        try:
            parsed = urlparse(urljoin(base_url, href))
        except Exception:
            continue
        if parsed.scheme in ("http", "https") and parsed.netloc.lower() in allowed_hosts:
            out.add(canonicalize_url(parsed.geturl()))
    return out


class LinkFrontierSource(SeedSource):
    """Drain in-scope outbound links from the already-fetched HTML pages."""

    name = "frontier"

    def __init__(self, root: Path, *, allowed_hosts: Iterable[str], max_urls: int = 1000):
        self.root = Path(root)
        self.allowed = frozenset(h.lower() for h in allowed_hosts)
        self.max_urls = max_urls

    def _state_path(self) -> Path:
        return self.root / "frontier_harvested.txt"

    def discover(self) -> Iterable[tuple[str, Optional[str]]]:
        from .. import paths
        from ..fetch import read_raw_blob
        from ..manifest import iter_all, open_db

        if not self.allowed or self.max_urls <= 0:
            return

        conn = open_db(paths.manifest_path(self.root))
        try:
            existing: set[str] = set()
            fetched: list[tuple[str, str]] = []
            for row in iter_all(conn):  # deterministic: ORDER BY url
                existing.add(row.url)
                if row.raw_hash:
                    fetched.append((row.url, row.raw_hash))
        finally:
            conn.close()

        sp = self._state_path()
        harvested = set(sp.read_text().split()) if sp.exists() else set()

        seen = set(existing)
        emitted: list[str] = []
        newly: list[str] = []
        for url, raw_hash in fetched:
            if len(emitted) >= self.max_urls:
                break
            if raw_hash in harvested:
                continue
            try:
                blob = read_raw_blob(self.root, raw_hash)
            except Exception:
                newly.append(raw_hash)  # unreadable -> don't retry it forever
                continue
            truncated = False
            for link in sorted(extract_links(blob, url, self.allowed)):
                if link not in seen:
                    seen.add(link)
                    emitted.append(link)
                    if len(emitted) >= self.max_urls:
                        truncated = True
                        break
            if not truncated:
                # Only mark a blob drained when ALL its new links were emitted.
                # If we hit max_urls mid-blob, leave it un-harvested so the next
                # pass recovers the remainder — the emitted links are manifest
                # rows by then, so they're filtered out and only the rest yield.
                newly.append(raw_hash)

        if newly:
            with sp.open("a") as f:
                f.write("\n".join(newly) + "\n")
        for u in emitted:
            yield (u, None)

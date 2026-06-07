"""``sitemap.xml`` / sitemap-index discovery sources — single-URL and
auto-discovery flavors.

Two ``SeedSource`` implementations:

* ``SitemapSource(url)`` — walks one operator-supplied sitemap URL. The
  original v1.0 source, kept for back-compat and for cases where the
  operator knows exactly which sitemap to read.
* ``AutoSitemapSource(base_url)`` — given just a base URL (a domain), it
  discovers every reachable sitemap by:
    1. Parsing ``/robots.txt`` for every ``Sitemap:`` directive
    2. Probing well-known paths (``/sitemap.xml``, ``/sitemap_index.xml``,
       ``/wp-sitemap.xml``, etc.) when robots.txt yields nothing
    3. Walking each discovered sitemap and deduplicating URLs across them.

  Addresses the "sitemap logic needs revision" gap the bench surfaced on
  sites whose sitemaps live at non-standard paths.

The underlying walker (``walk_sitemap``) is also more lenient than the v1.0
version:

* Detects + decompresses gzipped sitemap responses (``.xml.gz`` URLs or
  ``Content-Encoding: gzip`` headers), since real sitemaps over a few MB
  are usually gzipped at the edge.
* Falls back to a plain-text URL-per-line parser when the response isn't
  valid XML — some CMS exports look like that.
* Lenient XML parse: ``ET.ParseError`` no longer drops the whole file; we
  emit a warn and keep walking the rest of the discovered sitemaps.

Both ``walk_sitemap`` and ``discover_sitemaps`` follow redirects so
``/sitemap.xml`` → ``/sitemap-index.xml`` redirects work without operator
intervention.
"""

from __future__ import annotations

import gzip
import xml.etree.ElementTree as ET
from typing import Iterable, Optional
from urllib.parse import urlparse

import click
import httpx

from ..fetch import USER_AGENT
from . import SeedSource

_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

# Probed in order when robots.txt yields no Sitemap: directives. Ordered by
# frequency-in-practice — /sitemap.xml first, WordPress and gzipped variants
# after the bare-XML standards.
_WELLKNOWN_SITEMAP_PATHS: tuple[str, ...] = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/sitemap/sitemap-index.xml",
    "/wp-sitemap.xml",
    "/sitemaps.xml",
    "/sitemap.xml.gz",
)


def _origin(base_url: str) -> str:
    """Return scheme + netloc for a URL. ``base_url`` may be a full URL or
    just a domain; we treat schemeless input as https://."""
    if "://" not in base_url:
        base_url = "https://" + base_url
    p = urlparse(base_url)
    return f"{p.scheme}://{p.netloc}"


def _decompress_if_gzip(content: bytes, url: str,
                        content_encoding: Optional[str]) -> bytes:
    """Inflate gzipped sitemap content based on URL extension or
    Content-Encoding header. No-op for non-gzipped data."""
    if url.endswith(".gz") or (content_encoding or "").lower() == "gzip":
        try:
            return gzip.decompress(content)
        except (OSError, gzip.BadGzipFile):
            # Server claimed gzip but body isn't — fall through with raw bytes.
            pass
    return content


def _parse_sitemap_body(content: bytes, url: str
                        ) -> tuple[list[tuple[str, Optional[str]]], list[str]]:
    """Parse sitemap bytes into ``(urls, child_sitemaps)``.

    Strategy:

    1. Standard XML ``<urlset>`` → return urls.
    2. Standard XML ``<sitemapindex>`` → return child sitemap URLs.
    3. On ``ET.ParseError`` or empty results, fall back to plain-text
       (one URL per line, http(s):// prefix).

    Returns two lists so the caller can dispatch: children get pushed onto
    the work stack; urls go into the output buffer.
    """
    urls: list[tuple[str, Optional[str]]] = []
    children: list[str] = []
    try:
        root = ET.fromstring(content)
        tag = root.tag.split("}")[-1]
        if tag == "sitemapindex":
            for sm in root.findall("sm:sitemap", _SITEMAP_NS):
                loc = sm.findtext("sm:loc", default="",
                                  namespaces=_SITEMAP_NS).strip()
                if loc:
                    children.append(loc)
            return urls, children
        if tag == "urlset":
            for u in root.findall("sm:url", _SITEMAP_NS):
                loc = u.findtext("sm:loc", default="",
                                 namespaces=_SITEMAP_NS).strip()
                lm = u.findtext("sm:lastmod", default="",
                                namespaces=_SITEMAP_NS).strip() or None
                if loc:
                    urls.append((loc, lm))
            return urls, children
        # Unknown root tag — fall through to plain-text below
    except ET.ParseError:
        pass

    # Plain-text fallback: one URL per line, http(s):// prefix only.
    try:
        text = content.decode("utf-8", errors="replace")
    except Exception:
        return urls, children
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("http://", "https://")):
            urls.append((line, None))
    return urls, children


def discover_sitemaps(
    base_url: str,
    *,
    user_agent: Optional[str] = None,
    timeout: float = 15.0,
) -> list[str]:
    """Discover sitemap URLs for a domain.

    Two-step strategy:

    1. Fetch ``<origin>/robots.txt``. Collect every ``Sitemap: URL``
       directive (RFC-compliant location).
    2. If robots.txt yielded nothing, probe well-known sitemap paths
       (``/sitemap.xml``, ``/sitemap_index.xml``, ``/wp-sitemap.xml``, …)
       in order; return all that respond 2xx.

    Returns a deduplicated, order-preserving list of sitemap URLs the
    caller should walk.
    """
    ua = user_agent or USER_AGENT
    origin = _origin(base_url)
    discovered: list[str] = []
    seen: set[str] = set()

    def _add(u: str) -> None:
        if u and u not in seen:
            seen.add(u)
            discovered.append(u)

    with httpx.Client(
        timeout=timeout,
        headers={"User-Agent": ua},
        follow_redirects=True,
    ) as c:
        # Step 1: robots.txt
        try:
            r = c.get(f"{origin}/robots.txt")
            if r.status_code == 200:
                for line in r.text.splitlines():
                    line = line.strip()
                    if line.lower().startswith("sitemap:"):
                        sm_url = line.split(":", 1)[1].strip()
                        if sm_url.startswith("/"):
                            sm_url = origin + sm_url
                        _add(sm_url)
        except httpx.HTTPError as e:
            click.echo(f"warn: robots.txt fetch failed for {origin}: {e}",
                       err=True)

        if discovered:
            return discovered

        # Step 2: probe well-known paths. HEAD first (cheap), GET on any
        # non-2xx response. Observed live: canada.ca returns ``503 Service
        # Unavailable`` on HEAD for ``/sitemap.xml`` but ``200 OK`` on GET
        # (Akamai WAF rejects HEAD before reaching the origin). A literal
        # ``405`` check missed those; the broader fallback also covers 403
        # / 503 / similar method-discrimination WAF behavior.
        for path in _WELLKNOWN_SITEMAP_PATHS:
            url = origin + path
            try:
                r = c.head(url)
                if r.status_code != 200:
                    r = c.get(url)
                if r.status_code == 200:
                    _add(url)
            except httpx.HTTPError:
                continue
    return discovered


#: Politeness/safety ceilings on a single sitemap walk. The ``seen`` set
#: stops *cycles*, but a malicious or misconfigured site can serve a
#: sitemap-index whose children fan out into a deep/wide tree of *distinct*
#: sitemaps (a "sitemap bomb") — unbounded fetches + unbounded memory in
#: ``out``. These caps bound both. Defaults are far above any real corpus
#: (ATO is ~dozens of sitemaps / ~thousands of URLs); they only fire on
#: pathological input. Callers/tests can override per call.
MAX_SITEMAPS_WALKED = 2000
MAX_SITEMAP_URLS = 500_000


def walk_sitemap(
    url: str,
    *,
    user_agent: Optional[str] = None,
    max_sitemaps: int = MAX_SITEMAPS_WALKED,
    max_urls: int = MAX_SITEMAP_URLS,
) -> list[tuple[str, Optional[str]]]:
    """Best-effort walker. Follows ``<sitemapindex>`` entries recursively,
    decompresses gzipped responses, and falls back to plain-text parsing on
    bodies that aren't valid XML.

    A custom ``user_agent`` is strongly recommended for sites behind
    Cloudflare / Akamai — those reject the default ``httpx`` UA at the edge
    with a 401/403 before the body ever reaches us.

    Bounded by ``max_sitemaps`` (distinct sitemap documents fetched) and
    ``max_urls`` (URLs collected); on hitting either we warn and stop with
    whatever was gathered so far, so a sitemap bomb can't hang the crawl or
    exhaust memory.
    """
    ua = user_agent or USER_AGENT
    out: list[tuple[str, Optional[str]]] = []
    seen: set[str] = set()
    stack = [url]
    with httpx.Client(
        timeout=30.0,
        headers={"User-Agent": ua},
        follow_redirects=True,
    ) as c:
        while stack:
            if len(seen) >= max_sitemaps:
                click.echo(
                    f"warn: sitemap walk hit max_sitemaps={max_sitemaps} for "
                    f"{url}; stopping ({len(out)} URLs collected). This is a "
                    "very large index or a sitemap bomb.",
                    err=True)
                break
            sm_url = stack.pop()
            if sm_url in seen:
                continue
            seen.add(sm_url)
            try:
                r = c.get(sm_url)
                r.raise_for_status()
            except httpx.HTTPError as e:
                status = getattr(getattr(e, "response", None),
                                 "status_code", None)
                hint = ""
                if status in (401, 403) and user_agent is None:
                    hint = (" — server rejected the default UA; try setting "
                            "[crawl] user_agent in your config")
                click.echo(f"warn: sitemap fetch failed {sm_url}: {e}{hint}",
                           err=True)
                continue

            content = _decompress_if_gzip(
                r.content, sm_url, r.headers.get("content-encoding"))
            urls, children = _parse_sitemap_body(content, sm_url)
            if not urls and not children:
                click.echo(f"warn: sitemap parse yielded nothing for {sm_url}",
                           err=True)
                continue
            out.extend(urls)
            if len(out) >= max_urls:
                del out[max_urls:]
                click.echo(
                    f"warn: sitemap walk hit max_urls={max_urls} for {url}; "
                    "truncating. Raise the cap if this corpus is genuinely "
                    "larger.",
                    err=True)
                break
            stack.extend(children)
    return out


class SitemapSource(SeedSource):
    """SeedSource adapter over ``walk_sitemap`` — single URL in."""

    name = "sitemap"

    def __init__(self, url: str, *, user_agent: Optional[str] = None) -> None:
        self.url = url
        self.user_agent = user_agent

    def discover(self) -> Iterable[tuple[str, Optional[str]]]:
        yield from walk_sitemap(self.url, user_agent=self.user_agent)


class AutoSitemapSource(SeedSource):
    """SeedSource that auto-discovers all sitemaps for a domain via
    ``discover_sitemaps`` and walks each one.

    Yields each URL at most once across all discovered sitemaps. Operators
    can pass either a full URL or a bare domain; ``_origin()`` normalizes.
    """

    name = "auto-sitemap"

    def __init__(self, base_url: str,
                 *, user_agent: Optional[str] = None) -> None:
        self.base_url = base_url
        self.user_agent = user_agent

    def discover(self) -> Iterable[tuple[str, Optional[str]]]:
        sitemaps = discover_sitemaps(
            self.base_url, user_agent=self.user_agent)
        if not sitemaps:
            click.echo(
                f"warn: no sitemaps discovered for {self.base_url} "
                "(robots.txt has no Sitemap: directives + no well-known path "
                "responded 200). Consider --from-firecrawl-map as fallback.",
                err=True,
            )
            return
        click.echo(
            f"auto-sitemap: discovered {len(sitemaps)} sitemap(s) "
            f"for {self.base_url}",
            err=True,
        )
        seen_urls: set[str] = set()
        for sm_url in sitemaps:
            for url, lastmod in walk_sitemap(
                sm_url, user_agent=self.user_agent
            ):
                if url not in seen_urls:
                    seen_urls.add(url)
                    yield (url, lastmod)

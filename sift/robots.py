"""robots.txt Disallow enforcement.

We respect the operator's crawl directives: a URL whose path is Disallowed for
our user-agent is dropped at SEED time — it never enters the manifest and is
never fetched. Enforcing at seed (next to host_allow + excludes) means a broad
``Disallow`` correctly overrides a sitemap that happens to list the path.

Scope: enforcement is net-new-seed only. Manifest rows seeded *before* this
check existed — or before a site's robots.txt changed — are not re-evaluated at
fetch time; a fresh ``seed`` re-applies the current rules.

Parser: we use ``protego`` (RFC 9309: wildcards ``*``/``$``, longest-match
Allow/Disallow precedence), NOT stdlib ``urllib.robotparser``. The stdlib parser
runs each rule path through ``urlparse``/``urlunparse``, which (a) strips the
empty query in ``Disallow: /?`` down to a bare ``Disallow: /`` — over-blocking
the ENTIRE origin (verified live against google.com/robots.txt) — and (b)
URL-encodes ``*``/``$`` so wildcard rules silently stop matching (under-blocking).
Both are common on real sites, so the stdlib parser is unsafe for honest
enforcement.

Response handling (RFC 9309 §2.3.1):
  * 200             -> parse + apply the rules (BOM-tolerant).
  * 404 / other 4xx -> no robots.txt -> allow all.
  * 5xx / 429       -> server signals unavailable/overloaded -> COMPLETE
                       disallow (back off rather than crawl through the signal).
  * network error   -> couldn't reach robots.txt at all -> allow (don't drop a
                       whole host because one robots.txt fetch had a transient
                       blip while the site itself is up).

Set ``[crawl] respect_robots = false`` only for sources you have written
permission to index. robots.txt is fetched at most once per origin.
"""
from __future__ import annotations

from typing import Iterable
from urllib.parse import urlsplit

import httpx
from protego import Protego

from .fetch import USER_AGENT

# Sentinel rulesets for the non-200 cases (cheap to parse).
_ALLOW_ALL = ""
_DISALLOW_ALL = "User-agent: *\nDisallow: /"


class RobotsGate:
    """Per-origin robots.txt cache + ``can_fetch`` check.

    Synchronous: used in the seed phase, which is already synchronous and
    network-bound (it fetches sitemaps). robots.txt is fetched at most once per
    origin and cached for the life of the gate.
    """

    def __init__(self, user_agent: str | None = None, *, timeout: float = 10.0):
        self.user_agent = user_agent or USER_AGENT
        self.timeout = timeout
        # origin -> parsed ruleset. Cached for the life of the gate.
        self._cache: dict[str, Protego] = {}

    def _build(self, origin: str) -> Protego:
        try:
            r = httpx.get(
                f"{origin}/robots.txt",
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": self.user_agent},
            )
        except (httpx.HTTPError, OSError):
            return Protego.parse(_ALLOW_ALL)  # unreachable -> don't drop the host
        if r.status_code == 200:
            # Decode from bytes with utf-8-sig so a leading BOM doesn't hide the
            # first "User-agent:" line (some IIS origins serve BOM-prefixed
            # robots.txt). An empty body parses to no rules -> allow all.
            return Protego.parse(r.content.decode("utf-8-sig", "replace"))
        if r.status_code == 429 or r.status_code >= 500:
            # Server explicitly signals unavailable/overloaded. RFC 9309 treats
            # this as a (transient) COMPLETE disallow — back off rather than
            # crawl through the signal.
            return Protego.parse(_DISALLOW_ALL)
        return Protego.parse(_ALLOW_ALL)  # 4xx (incl. 404) -> no robots -> allow

    def _parser_for(self, origin: str) -> Protego:
        rp = self._cache.get(origin)
        if rp is None:
            rp = self._build(origin)
            self._cache[origin] = rp
        return rp

    def prewarm(self, urls: Iterable[str]) -> None:
        """Populate the per-origin cache up front — e.g. before opening a write
        transaction — so the in-loop ``allowed()`` checks do no network I/O."""
        seen: set[str] = set()
        for url in urls:
            parts = urlsplit(url)
            if parts.scheme and parts.netloc:
                origin = f"{parts.scheme}://{parts.netloc}"
                if origin not in seen:
                    seen.add(origin)
                    self._parser_for(origin)

    def allowed(self, url: str) -> bool:
        """True if our user-agent may fetch ``url`` under the host's robots.txt."""
        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            return True  # no resolvable origin -> not ours to block here
        origin = f"{parts.scheme}://{parts.netloc}"
        return self._parser_for(origin).can_fetch(url, self.user_agent)

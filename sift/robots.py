"""robots.txt Disallow enforcement.

We respect the operator's crawl directives: a URL whose path is Disallowed for
our user-agent is dropped at SEED time — it never enters the manifest and is
never fetched. Enforcing at seed (next to host_allow + excludes) means a broad
``Disallow`` correctly overrides a sitemap that happens to list the path, and a
disallowed URL never reaches the fetch ladder.

A missing or unreachable robots.txt allows everything (standard semantics) — we
don't block a crawl on a flaky/absent robots.txt. Set ``[crawl] respect_robots
= false`` only for sources you have written permission to index.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .fetch import USER_AGENT


class RobotsGate:
    """Per-host robots.txt cache + ``can_fetch`` check.

    Synchronous: used in the seed phase, which is already synchronous and
    network-bound (it fetches sitemaps). robots.txt is fetched at most once per
    origin and cached for the life of the gate.
    """

    def __init__(self, user_agent: Optional[str] = None, *, timeout: float = 10.0):
        self.user_agent = user_agent or USER_AGENT
        self.timeout = timeout
        # origin -> parser (rules) | None (no rules: allow all)
        self._cache: dict[str, Optional[RobotFileParser]] = {}

    def _parser_for(self, origin: str) -> Optional[RobotFileParser]:
        if origin in self._cache:
            return self._cache[origin]
        rp: Optional[RobotFileParser] = None
        try:
            r = httpx.get(
                f"{origin}/robots.txt",
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": self.user_agent},
            )
            if r.status_code == 200 and r.text.strip():
                rp = RobotFileParser()
                rp.parse(r.text.splitlines())
            # 4xx / empty -> None (allow all). 5xx is also treated as allow:
            # we won't halt a crawl on a transient robots.txt server error.
        except (httpx.HTTPError, OSError):
            rp = None
        self._cache[origin] = rp
        return rp

    def allowed(self, url: str) -> bool:
        """True if our user-agent may fetch ``url`` under the host's robots.txt."""
        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            return True  # no resolvable origin -> not ours to block here
        origin = f"{parts.scheme}://{parts.netloc}"
        rp = self._parser_for(origin)
        if rp is None:
            return True
        return rp.can_fetch(self.user_agent, url)

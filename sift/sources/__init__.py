"""Seed-discovery sources for sift.

A ``SeedSource`` is a thin, swappable provider for one URL-discovery strategy.
The seed command builds a list of them from CLI flags / config and iterates —
the rest of the pipeline (canonicalize, host_allow, exclude patterns, upsert)
is identical regardless of source.

Currently shipping:

* ``JsonFileSource``                       — local discovery dump (`--from-json`)
* ``sift.sources.sitemap.SitemapSource``    — sitemap.xml + sitemap-index walker
                                              (`--from-sitemap`)
* ``sift.sources.firecrawl.FirecrawlMapSource`` — blended discovery via
                                              Firecrawl ``/v2/map``
                                              (`--from-firecrawl-map`)

Adding a new source = subclass ``SeedSource``, implement ``discover``, plug it
into the seed command. No changes to the downstream pipeline. The same shape
will support a future browser-rendered link-extraction source when we add the
Playwright/Firecrawl browser fallback for hard-to-crawl sites.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable, Optional


class SeedSource(ABC):
    """One URL-discovery strategy.

    Subclasses encapsulate everything provider-specific (auth, recursion,
    keyword filtering, retries). They yield uniform ``(url, lastmod)`` tuples
    so the seed pipeline can drain them without caring which source produced
    which row. ``lastmod`` is an ISO-8601 string when the source exposes one,
    else ``None`` (the planner falls back to ETag / If-Modified-Since at fetch
    time, which is correct for discovery-only sources like Firecrawl ``/map``).
    """

    #: Short identifier surfaced in warnings + the seed summary.
    name: str = "source"

    @abstractmethod
    def discover(self) -> Iterable[tuple[str, Optional[str]]]:
        """Yield ``(url, lastmod_or_None)`` tuples for the seed pipeline.

        Implementations should be best-effort: emit ``warn:`` lines on stderr
        for partial failures (e.g. one sub-sitemap of many 403'd) but only
        raise when the entire source has nothing to contribute.
        """
        raise NotImplementedError


class JsonFileSource(SeedSource):
    """Seed from a discovery dump on disk.

    Accepts both shapes:

      * ``{"links": [{"url": "...", "lastmod": "...?"}, ...]}`` — the canonical
        sift discovery format
      * A bare ``["url1", "url2", ...]`` array — the minimal hand-rolled form

    Used by ``sift seed --from-json``.
    """

    name = "json"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def discover(self) -> Iterable[tuple[str, Optional[str]]]:
        data = json.loads(self.path.read_text())
        links = data.get("links", data if isinstance(data, list) else [])
        for item in links:
            if isinstance(item, dict):
                url = item.get("url")
                lm = item.get("lastmod") or item.get("last_modified")
            else:
                url, lm = item, None
            if url:
                yield (str(url), lm)


__all__ = ["SeedSource", "JsonFileSource"]

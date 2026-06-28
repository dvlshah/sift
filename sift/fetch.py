"""Phase 2: async HTTP fetch with per-host rate limit, conditional GETs, retries.

Reads plan.jsonl (or a filtered subset), writes fetch.log + raw blobs.
Resumable: skipping URLs already present in fetch.log.

Each fetch.log entry is one JSON object per line:
    {"url": ..., "status": 200, "etag": "W/\"abc\"", "last_modified": "...",
     "raw_hash": "sha256...", "raw_bytes": 12345, "fetched_at": "...",
     "decision": "FETCH_CONDITIONAL", "error": null, "content_type": "text/html"}

For 304 entries we omit raw_hash/raw_bytes.
"""

from __future__ import annotations

import asyncio
import gzip
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional
from urllib.parse import urlparse

import httpx
from aiolimiter import AsyncLimiter

from . import paths
from ._io import sha256_hex
from .manifest import now_utc
from .quality import looks_thin

if TYPE_CHECKING:
    # Forward refs only — pulling these eagerly would defeat lazy crawl4ai.
    from .browser import BrowserPool
    from .sites import SiteProfile
    from .sources.firecrawl import FirecrawlScrapePool
    from .sources.impersonate import CurlCffiScrapePool

USER_AGENT = (
    "sift/0.1.0 (+https://github.com/dvlshah/sift; respectful crawler, 2 req/sec)"
)

# Be polite. ATO is a critical public service.
DEFAULT_RATE = 2.0  # requests per second per host
DEFAULT_CONCURRENCY = 6  # in-flight requests cap
DEFAULT_TIMEOUT = 30.0  # seconds per request
DEFAULT_RETRIES = 3  # transient retries per URL within a single fetch run
RETRY_BACKOFF_BASE = 1.5  # seconds; multiplied by 2^attempt
MAX_RETRY_BACKOFF = 30.0
# Hard ceiling on a stored response body. A page over this is refused
# (not stored, not extracted) — bounds the multiplicative downstream cost
# (blob write + extraction) of a pathological/hostile response. NOTE: the
# single oversized response is still read into memory by httpx before this
# check; a true pre-read streaming cap is a documented follow-up. 25 MB
# clears every real docs page (the largest CSS-spec markdown is ~320 KB)
# with wide margin.
MAX_BODY_BYTES = 25 * 1024 * 1024


@dataclass
class FetchInput:
    """One row of plan.jsonl that the fetcher cares about."""

    url: str
    decision: str  # FETCH or FETCH_CONDITIONAL
    etag: Optional[str]
    last_modified: Optional[str]


@dataclass
class FetchResult:
    url: str
    decision: str
    status: int
    etag: Optional[str]
    last_modified: Optional[str]
    raw_hash: Optional[str]
    raw_bytes: int
    fetched_at: str
    error: Optional[str]
    # Non-NULL iff this row was fetched via the browser path. Read by plan.py
    # for §8.2 cache invalidation and by status.py for the cached-headers ratio.
    # Default keeps old fetch.log lines parseable (FetchResult(**json.loads(...))).
    browser_version: Optional[str] = None
    # Raw response Content-Type. Consumed by extract's body_kind() routing
    # (e.g. text/markdown → pass-through). Defaults None for back-compat with
    # older fetch.log lines AND the re-extract path, which synthesizes
    # FetchResults from the manifest (which stores no content-type) — so
    # content-type is an opportunistic hint; URL-based profile rules are durable.
    content_type: Optional[str] = None

    def to_json_line(self) -> str:
        return json.dumps(self.__dict__, separators=(",", ":")) + "\n"


def write_raw_blob(root: Path, raw_hash: str, data: bytes) -> Path:
    """Content-addressed raw HTML store. Gzip-compressed on disk; idempotent."""
    p = paths.raw_path(root, raw_hash)
    if p.exists():
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write via tmp + rename.
    tmp = p.with_suffix(p.suffix + ".tmp")
    with gzip.open(tmp, "wb", compresslevel=6) as f:
        f.write(data)
    tmp.rename(p)
    return p


def read_raw_blob(root: Path, raw_hash: str) -> bytes:
    """Round-trip read for the extract phase."""
    with gzip.open(paths.raw_path(root, raw_hash), "rb") as f:
        return f.read()


def store_body(root: Path, body: bytes) -> tuple[str, int]:
    """Hash + persist a response body to the content-addressed raw store,
    returning ``(raw_hash, n_bytes)``. The single body-commit seam shared by
    the HTTP, browser, and Firecrawl transports so they store identically."""
    raw_hash = sha256_hex(body)
    write_raw_blob(root, raw_hash, body)
    return raw_hash, len(body)


def _no_body_result(
    inp: "FetchInput",
    status: int,
    error: Optional[str],
    fetched_at: str,
    *,
    etag: Optional[str] = None,
    last_modified: Optional[str] = None,
    browser_version: Optional[str] = None,
) -> "FetchResult":
    """A FetchResult that stored no body (raw_hash=None, raw_bytes=0) — the
    shared shape for every 304 / 4xx / 5xx / network / guard outcome. Only
    status, the cache headers, the error, and browser_version vary."""
    return FetchResult(
        url=inp.url,
        decision=inp.decision,
        status=status,
        etag=etag,
        last_modified=last_modified,
        raw_hash=None,
        raw_bytes=0,
        fetched_at=fetched_at,
        error=error,
        browser_version=browser_version,
    )


def load_completed_urls(fetch_log: Path) -> set[str]:
    """For resumability: URLs already written to fetch.log."""
    if not fetch_log.exists():
        return set()
    done: set[str] = set()
    with fetch_log.open() as f:
        for line in f:
            try:
                obj = json.loads(line)
                done.add(obj["url"])
            except (json.JSONDecodeError, KeyError):
                continue
    return done


def host_of(url: str) -> str:
    return urlparse(url).netloc.lower()


#: Sentinel status from _one_request: the adaptive memo skipped the native
#: request for a host known to block it. The caller routes straight to the ladder.
HOST_FLOORED = -1


async def _one_request(
    client: httpx.AsyncClient,
    inp: FetchInput,
    limiter: AsyncLimiter,
    semaphore: asyncio.Semaphore,
    *,
    memo: Optional["HostTierMemo"] = None,
    host: Optional[str] = None,
    free_tier: bool = False,
) -> tuple[int, httpx.Response | None, Optional[str]]:
    """Send one request honoring rate limit + concurrency. Caller handles retries.

    Returns (status_int, response_or_None, error_string_or_None). A response of
    None means hard failure (no HTTP exchange completed).

    The adaptive host floor is checked HERE — *after* acquiring the concurrency
    semaphore, the natural serialization point. That's what makes it work under
    concurrency: URLs queued behind the semaphore re-check when they get a slot,
    so they see a floor that earlier completions set, and skip the request. A
    top-of-fetch check would be evaluated by every coroutine before any block was
    recorded (they all start together), and never fire. Returns
    ``(HOST_FLOORED, None, None)`` to signal the skip without making the request.
    """
    # User-Agent is set as a default header on the AsyncClient in fetch_all,
    # so every request from this client (including redirects) gets it without
    # each call site having to remember to pass it.
    headers: dict[str, str] = {"Accept": "text/html,*/*;q=0.5"}
    if inp.decision == "FETCH_CONDITIONAL":
        if inp.etag:
            headers["If-None-Match"] = inp.etag
        if inp.last_modified:
            headers["If-Modified-Since"] = inp.last_modified

    async with semaphore:
        if (
            memo is not None
            and free_tier
            and host is not None
            and memo.should_skip_native(host)
        ):
            memo.record_skip()
            return HOST_FLOORED, None, None  # skip the doomed request entirely
        async with limiter:
            try:
                resp = await client.get(inp.url, headers=headers, follow_redirects=True)
                return resp.status_code, resp, None
            except httpx.HTTPError as e:
                return 0, None, f"{type(e).__name__}: {e}"


# Statuses that trigger escalation to the self-hosted browser tier when no
# impersonation pool contributes its own set. Bot-block / rate-limit signatures.
BROWSER_ESCALATE_STATUSES = (403, 429, 503)

# Consecutive native blocks for a host before its remaining URLs skip the native
# fetcher entirely (see HostTierMemo). 0 disables the adaptive floor.
DEFAULT_HOST_BLOCK_FLOOR = 3


class HostTierMemo:
    """Per-run adaptive routing for large crawls of hardened hosts.

    Once a host has blocked the native fetcher ``threshold`` times, its remaining
    URLs **skip the native round-trip** — and crucially its 429/503 retry-backoff
    (~10s/URL) — starting straight at the escalation ladder. On a thousand-URL
    crawl of a bot-managed host that's the difference between minutes and hours.

    It is a pure *speed* heuristic and deliberately safe:
      * It only ever RAISES a host's floor (monotonic) and never changes WHICH
        content is accepted — every quality/SSRF/verify gate still runs.
      * Consulted only when a FREE tier (curl_cffi/browser) exists, so a host is
        never skipped onto a paid tier that would bill per URL.
      * Lock-free on purpose: concurrent updates race benignly (at worst a few
        extra native attempts before the floor latches), and skipping a lock
        keeps the hot path fast. A wrong guess only costs a redundant curl_cffi
        fetch — correctness is unaffected.
    """

    def __init__(self, threshold: int = DEFAULT_HOST_BLOCK_FLOOR) -> None:
        self._threshold = threshold
        self._blocks: dict[str, int] = {}
        self._floored: set[str] = set()
        self._skipped = 0  # native round-trips skipped (surfaced in the run summary)

    def should_skip_native(self, host: str) -> bool:
        return host in self._floored

    def record_skip(self) -> None:
        """Count one native request skipped because its host was floored. Pure
        telemetry — surfaced in the run summary so an operator can see the floor's
        effect (and spot a host that was floored but shouldn't have been)."""
        self._skipped += 1

    def record_block(self, host: str) -> None:
        if self._threshold <= 0 or host in self._floored:
            return
        n = self._blocks.get(host, 0) + 1
        self._blocks[host] = n
        if n >= self._threshold:
            self._floored.add(host)

    def record_ok(self, host: str) -> None:
        # Native served this host fine → clear any transient block tally so a
        # one-off hiccup never floors a healthy host.
        if host not in self._floored:
            self._blocks.pop(host, None)

    @property
    def floored_hosts(self) -> frozenset[str]:
        return frozenset(self._floored)

    @property
    def skipped(self) -> int:
        """Number of native round-trips skipped because their host was floored."""
        return self._skipped


def _escalate_status_set(
    impersonate_pool, browser_pool, firecrawl_pool
) -> frozenset[int]:
    """Union of the wired tiers' trigger statuses. Empty when no escalation tier
    is configured, so the native-only path is byte-identical to before."""
    s: set[int] = set()
    if impersonate_pool is not None:
        s.update(impersonate_pool.escalate_statuses)
    if browser_pool is not None:
        s.update(BROWSER_ESCALATE_STATUSES)
    if firecrawl_pool is not None:
        s.update(firecrawl_pool.fallback_statuses)
    return frozenset(s)


async def _escalate_browser_tier(
    inp: "FetchInput",
    root: Path,
    profile: "SiteProfile",
    pool: "BrowserPool",
    thin_text_threshold: int,
    allowed_hosts: Optional[frozenset[str]] = None,
) -> "FetchResult":
    """The self-hosted browser as an escalation rung (not just profile-routing).

    Renders ``inp.url`` and returns its FetchResult, but RAISES ``EscalateError``
    on a render failure or still-thin output so the ladder falls through to the
    paid tier — unlike ``_fetch_browser`` (the direct-dispatch path), which
    returns the failure row as-is.
    """
    from .sources.impersonate import EscalateError

    try:
        res = await _fetch_browser(inp, root, profile, pool, allowed_hosts=allowed_hosts)
    except Exception as e:
        # A browser problem (missing Playwright, launch crash, OOM) must never
        # take down a fetch — decline so the ladder continues and any native
        # body still stands as the last resort.
        raise EscalateError(f"browser {type(e).__name__}") from e
    if res.error is not None or res.raw_hash is None:
        raise EscalateError(f"browser {res.error or 'no-body'}")
    if thin_text_threshold > 0 and looks_thin(
        read_raw_blob(root, res.raw_hash), res.content_type, thin_text_threshold
    ):
        raise EscalateError("browser still-thin")
    return res


async def _escalate(
    inp: "FetchInput",
    root: Path,
    reason: str,
    *,
    impersonate_pool: Optional["CurlCffiScrapePool"] = None,
    browser_pool: Optional["BrowserPool"] = None,
    profile: Optional["SiteProfile"] = None,
    firecrawl_pool: Optional["FirecrawlScrapePool"] = None,
    allowed_hosts: Optional[frozenset[str]] = None,
    thin_text_threshold: int = 0,
) -> Optional["FetchResult"]:
    """Walk the escalation ladder for a URL the native fetch couldn't serve well.

    * Tier 2 — curl_cffi impersonation (free, self-hosted): defeats TLS-fingerprint
      and UA blocks without a browser; tried first so it absorbs most escalations
      at zero cost.
    * Tier 3a — self-hosted browser (free): renders JS shells / challenges that
      curl_cffi can't (it executes no JS). Thin content is fine to send here —
      rendering is exactly how an empty SPA shell becomes real content, and it
      costs nothing but local compute.
    * Tier 3b — Firecrawl (paid, browser+proxy): optional last resort for the
      hardest anti-bot. A ``thin-content`` reason only reaches it when
      ``escalate_on_thin`` is set, so empty shells never silently burn credits.

    Returns the first FetchResult that passes a tier's quality gate, or ``None``
    when every available tier declines (caller keeps the native failure). Tier
    errors are swallowed per-tier — escalation never crashes the run.
    """
    from .sources.impersonate import EscalateError

    if impersonate_pool is not None:
        try:
            return await impersonate_pool.fetch(inp, root, allowed_hosts=allowed_hosts)
        except EscalateError:
            pass  # still blocked/thin at tier 2 → fall through

    if browser_pool is not None and profile is not None:
        try:
            return await _escalate_browser_tier(
                inp, root, profile, browser_pool, thin_text_threshold,
                allowed_hosts=allowed_hosts,
            )
        except EscalateError:
            pass  # render failed/thin → fall through to the paid tier

    if firecrawl_pool is not None and firecrawl_pool.budget_remaining() > 0:
        if reason == "thin-content" and not getattr(
            firecrawl_pool.cfg, "escalate_on_thin", False
        ):
            return None
        from .sources.firecrawl import FirecrawlError

        try:
            return await firecrawl_pool.fetch(inp, root)
        except FirecrawlError:
            pass  # native failure stands; pool counters advanced for telemetry
    return None


async def fetch_one(
    client: httpx.AsyncClient,
    inp: FetchInput,
    root: Path,
    limiter: AsyncLimiter,
    semaphore: asyncio.Semaphore,
    *,
    retries: int = DEFAULT_RETRIES,
    firecrawl_pool: Optional["FirecrawlScrapePool"] = None,
    impersonate_pool: Optional["CurlCffiScrapePool"] = None,
    browser_pool: Optional["BrowserPool"] = None,
    profile: Optional["SiteProfile"] = None,
    thin_text_threshold: int = 0,
    memo: Optional["HostTierMemo"] = None,
    allowed_hosts: Optional[frozenset[str]] = None,
) -> FetchResult:
    """Fetch with bounded retry on transient errors. Idempotent at the URL level.

    When ``firecrawl_pool`` is provided and the native response status is in
    the pool's configured fallback set (default 401/403), the URL is
    re-fetched through Firecrawl's ``/v2/scrape``. Firecrawl errors preserve
    the original native failure — the escalation never crashes the run.

    ``allowed_hosts`` (lowercased hostnames) is the SSRF guard: redirects are
    followed, so the body is stored only if the FINAL post-redirect host is
    on this set. Without it, an open redirect on an allow-listed origin could
    pull an internal/metadata endpoint's response into the index under the
    original URL. ``None`` disables the check (back-compat; the CLI always
    passes the run's seed.host_allow)."""
    host = host_of(inp.url)
    free_tier = impersonate_pool is not None or browser_pool is not None
    escalate_kwargs = dict(
        impersonate_pool=impersonate_pool,
        browser_pool=browser_pool,
        profile=profile,
        firecrawl_pool=firecrawl_pool,
        allowed_hosts=allowed_hosts,
        thin_text_threshold=thin_text_threshold,
    )

    last_error: Optional[str] = None
    last_status = 0

    for attempt in range(retries + 1):
        status, resp, err = await _one_request(
            client, inp, limiter, semaphore, memo=memo, host=host, free_tier=free_tier
        )
        last_status = status
        last_error = err
        if status == HOST_FLOORED:
            break  # adaptive skip — no request was made, no retries; escalate below
        transient = err is not None or status in (408, 429, 500, 502, 503, 504)
        if not transient:
            break
        if attempt < retries:
            backoff = min(MAX_RETRY_BACKOFF, RETRY_BACKOFF_BASE * (2**attempt))
            # Respect Retry-After if present
            if resp is not None and "retry-after" in resp.headers:
                try:
                    backoff = max(backoff, float(resp.headers["retry-after"]))
                except ValueError:
                    pass
            await asyncio.sleep(backoff)

    now = now_utc()
    escalate_statuses = _escalate_status_set(
        impersonate_pool, browser_pool, firecrawl_pool
    )

    if last_status == HOST_FLOORED:
        # Adaptive floor skipped the native request — go straight to the ladder.
        # No record_block (host is already floored); no native body to fall back to.
        esc = await _escalate(inp, root, "host-floored", **escalate_kwargs)
        if esc is not None:
            return esc
        return _no_body_result(inp, 0, f"host-floored-unserved:{host}", now)

    if resp is None:
        # No HTTP exchange completed after retries — frequently a TLS-fingerprint
        # reset on a hardened edge. Let the ladder try the impersonation tier
        # (a curl_cffi handshake often succeeds where httpx's was reset).
        if memo is not None and free_tier:
            memo.record_block(host)
        esc = await _escalate(
            inp, root, last_error or "network-failure", **escalate_kwargs
        )
        if esc is not None:
            return esc
        return _no_body_result(inp, last_status, last_error or "network-failure", now)

    etag = resp.headers.get("etag")
    last_mod = resp.headers.get("last-modified")

    if resp.status_code == 304:
        return _no_body_result(inp, 304, None, now, etag=etag, last_modified=last_mod)

    if resp.status_code in (404, 410):
        return _no_body_result(
            inp, resp.status_code, None, now, etag=etag, last_modified=last_mod
        )

    if resp.status_code >= 400:
        # Bot-block / rate-limit signatures escalate up the ladder (curl_cffi
        # first, then Firecrawl). ``escalate_statuses`` is the union of the wired
        # tiers' triggers; with no pools it's empty and this is a no-op, so the
        # native-only path stays byte-identical.
        if resp.status_code in escalate_statuses:
            if memo is not None and free_tier:
                memo.record_block(host)
            esc = await _escalate(
                inp, root, f"http-{resp.status_code}", **escalate_kwargs
            )
            if esc is not None:
                return esc
        return _no_body_result(
            inp,
            resp.status_code,
            f"http-{resp.status_code}",
            now,
            etag=etag,
            last_modified=last_mod,
        )

    # 2xx — native served this host (clear any transient block tally so a
    # one-off hiccup never floors a healthy host).
    if memo is not None and free_tier:
        memo.record_ok(host)

    # 2xx with body.
    # SSRF guard: redirects were followed, so re-validate the FINAL host
    # against the allow-list before storing. An allow-listed origin with an
    # open redirect (e.g. /out?url=) could otherwise land us on an internal
    # or cloud-metadata endpoint, whose body we'd store under inp.url.
    if allowed_hosts is not None:
        final_host = (resp.url.host or "").lower()
        if final_host and final_host not in allowed_hosts:
            return _no_body_result(
                inp,
                resp.status_code,
                f"redirect-off-allowlist:{final_host}",
                now,
                etag=etag,
                last_modified=last_mod,
            )
    body = resp.content
    if len(body) > MAX_BODY_BYTES:
        return _no_body_result(
            inp,
            resp.status_code,
            f"body-too-large:{len(body)}>{MAX_BODY_BYTES}",
            now,
            etag=etag,
            last_modified=last_mod,
        )

    # Content-quality escalation: a 200 carrying an empty SPA shell or a JS
    # challenge interstitial is a SILENT failure the status-only path missed —
    # it has a success code but ~no content. Route it up the ladder before
    # committing. If no tier improves it, the real 200 still stands (best
    # available). No-op unless a tier is wired AND thin_text_threshold > 0.
    ct = resp.headers.get("content-type")
    any_tier = (
        impersonate_pool is not None
        or browser_pool is not None
        or firecrawl_pool is not None
    )
    if any_tier and looks_thin(body, ct, thin_text_threshold):
        esc = await _escalate(inp, root, "thin-content", **escalate_kwargs)
        if esc is not None:
            return esc

    raw_hash, n_bytes = store_body(root, body)
    return FetchResult(
        url=inp.url,
        decision=inp.decision,
        status=resp.status_code,
        etag=etag,
        last_modified=last_mod,
        raw_hash=raw_hash,
        raw_bytes=n_bytes,
        fetched_at=now,
        error=None,
        content_type=ct,
    )


async def _fetch_browser(
    inp: FetchInput,
    root: Path,
    profile: "SiteProfile",
    pool: "BrowserPool",
    allowed_hosts: Optional[frozenset[str]] = None,
) -> FetchResult:
    """Render `inp.url` via the browser stack and project to FetchResult.

    Maps RenderedPage -> FetchResult:
      * page.html.encode("utf-8") hashed + stored in the same content-addressed
        raw blob path the http path uses (storage is unified per design §4.3).
      * page.headers["etag"|"last-modified"] become the FetchResult cache keys
        (Response hook + capture-then-filter already projected them per §12.1).
      * BROWSER_VERSION tags the row so plan.py can invalidate it on a bump (§8.2)
        and status.py can count browser-tracked URLs in the cached-headers ratio.

    Crawl4ai-side failures arrive as BrowserFetchError; we surface them with a
    status of 0 (matching the http path's network-failure convention).
    """
    # Lazy imports keep the http-only path crawl4ai-free.
    from .browser import BROWSER_VERSION, BrowserFetchConfig, BrowserFetchError, render

    cfg = profile.browser_config(inp.url) or BrowserFetchConfig()
    now = now_utc()

    try:
        page = await render(inp.url, cfg, pool)
    except BrowserFetchError as e:
        return _no_body_result(inp, 0, str(e), now, browser_version=BROWSER_VERSION)

    if page.error is not None:
        return _no_body_result(
            inp,
            page.status_code,
            page.error,
            now,
            browser_version=BROWSER_VERSION,
        )

    # SSRF guard (mirrors the native path in fetch_one): the browser follows
    # redirects and in-page navigations, so re-validate the FINAL rendered URL's
    # host against the allow-list before storing. An allow-listed origin with an
    # open redirect / JS navigation could otherwise land us on an internal or
    # cloud-metadata endpoint whose DOM we'd store under inp.url.
    if allowed_hosts is not None:
        # Fail CLOSED on an empty/opaque final_url (about:blank / data: from a
        # failed navigation). Unlike the native path — whose completed response
        # always carries a host — the browser can legitimately report a hostless
        # final_url, and a no-navigation render already falls back to the on-list
        # inp.url (browser.py), so refusing the opaque case loses no real content.
        final_host = (urlparse(page.final_url).hostname or "").lower()
        if final_host not in allowed_hosts:
            return _no_body_result(
                inp,
                page.status_code,
                f"redirect-off-allowlist:{final_host}",
                now,
                browser_version=BROWSER_VERSION,
            )

    body = page.html.encode("utf-8")
    raw_hash, n_bytes = store_body(root, body)
    headers = page.headers or {}
    return FetchResult(
        url=inp.url,
        decision=inp.decision,
        status=page.status_code,
        etag=headers.get("etag"),
        last_modified=headers.get("last-modified"),
        raw_hash=raw_hash,
        raw_bytes=n_bytes,
        fetched_at=now,
        error=None,
        browser_version=BROWSER_VERSION,
        content_type=headers.get("content-type"),
    )


async def _guarded_fetch(inp: "FetchInput", coro) -> "FetchResult":
    """Per-URL containment for fetch_all's gather loop.

    ``fetch_one`` already returns FetchResults for HTTP/network errors, but
    an unexpected raise — most plausibly an OSError from the raw-blob write
    in the browser path, or any future bug — would otherwise propagate out
    of ``as_completed`` and abort the WHOLE run (leaving sibling tasks
    pending). Wrapping each task turns such a raise into a single status=0
    failed row, so one pathological URL can't take the batch down. Mirrors
    the extract phase's per-URL isolation.
    """
    try:
        return await coro
    except Exception as e:
        return _no_body_result(
            inp,
            0,
            f"task-crash:{type(e).__name__}: {e}"[:300],
            now_utc(),
        )


async def fetch_all(
    inputs: Iterable[FetchInput],
    root: Path,
    fetch_log: Path,
    *,
    rate: float = DEFAULT_RATE,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float = DEFAULT_TIMEOUT,
    on_result: Optional[callable] = None,
    profile: Optional["SiteProfile"] = None,
    browser_pool: Optional["BrowserPool"] = None,
    user_agent: Optional[str] = None,
    firecrawl_pool: Optional["FirecrawlScrapePool"] = None,
    impersonate_pool: Optional["CurlCffiScrapePool"] = None,
    thin_text_threshold: int = 0,
    memo: Optional["HostTierMemo"] = None,
    allowed_hosts: Optional[frozenset[str]] = None,
) -> int:
    """Fetch all inputs honoring rate limit. Appends each result to fetch_log
    immediately so a crash mid-run resumes cleanly. Returns count fetched.

    Dispatch: a URL routes to the browser path iff ``profile.requires_browser(url)``
    is True AND ``browser_pool`` is provided. Browser-required URLs without a
    pool raise (caller should have either provided one or short-circuited them
    in plan via ``Decision.SKIPPED_BROWSER_DISABLED``). ``profile`` defaults to
    the active site profile for back-compat with callers that haven't been
    updated; URLs are then routed via http only (the pre-browser behavior)."""
    fetch_log.parent.mkdir(parents=True, exist_ok=True)

    done = load_completed_urls(fetch_log)
    pending = [inp for inp in inputs if inp.url not in done]
    if not pending:
        return 0

    # One adaptive per-host memo for the whole run, only when a FREE escalation
    # tier exists (never skip native onto a paid-only tier). Shared across all
    # fetch_one tasks so a host blocked early speeds up its later URLs. The CLI
    # passes its own (built with the configured threshold) so it can read the
    # floor's stats for the run summary; direct callers get a default-threshold one.
    if memo is None and (impersonate_pool is not None or browser_pool is not None):
        memo = HostTierMemo()

    # Split by transport. Browser-required URLs sidestep the per-host rate
    # limiter — they're already much slower (5-10s each) so the cost driver
    # is the BrowserPool semaphore, not requests/sec.
    if profile is None:
        # No profile passed → assume http for everything (pre-browser back-compat).
        http_inputs = list(pending)
        browser_inputs: list[FetchInput] = []
    else:
        browser_inputs = [inp for inp in pending if profile.requires_browser(inp.url)]
        http_inputs = [inp for inp in pending if not profile.requires_browser(inp.url)]

    if browser_inputs and browser_pool is None:
        urls = ", ".join(inp.url for inp in browser_inputs[:3])
        raise RuntimeError(
            f"{len(browser_inputs)} URL(s) require browser rendering "
            f"(e.g. {urls}) but no BrowserPool was provided. Either enable "
            "[browser] and pass a pool, or rely on plan.py to short-circuit "
            "these to Decision.SKIPPED_BROWSER_DISABLED before fetch."
        )

    # Group http inputs by host so each host gets its own rate limiter
    # (and politeness budget).
    by_host: dict[str, list[FetchInput]] = {}
    for inp in http_inputs:
        by_host.setdefault(host_of(inp.url), []).append(inp)

    limiters = {h: AsyncLimiter(max_rate=rate, time_period=1.0) for h in by_host}
    semaphore = asyncio.Semaphore(concurrency)
    count = 0

    limits = httpx.Limits(
        max_connections=concurrency * 2, max_keepalive_connections=concurrency
    )
    timeout_cfg = httpx.Timeout(timeout, connect=10.0)

    async with httpx.AsyncClient(
        timeout=timeout_cfg,
        limits=limits,
        http2=False,
        headers={"User-Agent": user_agent or USER_AGENT},
    ) as client:
        with fetch_log.open("a") as log_f:
            tasks = []
            for host, items in by_host.items():
                limiter = limiters[host]
                for inp in items:
                    tasks.append(
                        _guarded_fetch(
                            inp,
                            fetch_one(
                                client,
                                inp,
                                root,
                                limiter,
                                semaphore,
                                firecrawl_pool=firecrawl_pool,
                                impersonate_pool=impersonate_pool,
                                browser_pool=browser_pool,
                                profile=profile,
                                thin_text_threshold=thin_text_threshold,
                                memo=memo,
                                allowed_hosts=allowed_hosts,
                            ),
                        )
                    )
            for inp in browser_inputs:
                assert profile is not None and browser_pool is not None
                tasks.append(
                    _guarded_fetch(
                        inp,
                        _fetch_browser(
                            inp, root, profile, browser_pool,
                            allowed_hosts=allowed_hosts,
                        ),
                    )
                )
            for coro in asyncio.as_completed(tasks):
                result = await coro
                log_f.write(result.to_json_line())
                log_f.flush()
                count += 1
                if on_result is not None:
                    on_result(result)
    return count

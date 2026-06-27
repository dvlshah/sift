"""Single coupling point for browser-based rendering (Playwright).

This is the *only* module in sift that imports Playwright. Every other
module talks to sift-owned dataclasses (``BrowserFetchConfig``,
``RenderedPage``) and the ``render()`` coroutine. The design's §10 swap
contract: swap renderers by rewriting this file alone.

History: v0.2.0 originally shipped crawl4ai under the hood. Hangs on
analytics-heavy SPAs (verified with domain.com.au) drove the swap to bare
Playwright. The crawl4ai impl is preserved in ``archive/crawl4ai/`` with
the evidence trail.

See ``docs/design/browser-fetch.md`` for the full design.
"""

# DO NOT import playwright at module level.
# Pulls Playwright + Chromium import chain into every sift CLI startup,
# including http-only users. Lazy imports live inside function bodies.
#
# Also: do NOT add `from __future__ import annotations`. The contract tests
# use `signature(f).parameters[i].annotation is SomeType` (identity, not
# string equality), which only works when annotations resolve at definition.

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, AsyncContextManager, Literal, Optional

if TYPE_CHECKING:
    # String-form forward refs ("BrowserConfigDefaults") resolve via this.
    # Never imported at runtime.
    from .config import BrowserConfigDefaults  # noqa: F401


BROWSER_VERSION = "playwright-1.60"
"""Version pin for the browser-rendering stack. A bump invalidates any
manifest row whose ``browser_version`` differs. Playwright bundles its own
Chromium binary keyed to the Playwright release; pinning Playwright alone
uses its release pin as the proxy for the rendering binary."""


PERSISTED_HEADER_KEYS: frozenset[str] = frozenset({
    "etag", "last-modified", "cache-control",
})
"""Response headers we persist on the manifest row for cache validation.
Everything else (content-length, server, set-cookie, ...) describes the SPA
shell, not the rendered content, and is dropped at the boundary."""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BrowserNotInstalledError(ImportError):
    """Raised when ``import playwright`` fails (the [browser] extra is missing).

    The message includes the install hint so operators see exactly what to run.
    """


class BrowserFetchError(Exception):
    """Wraps any Playwright-side failure during rendering.

    Callers see this type; Playwright's own exception types never escape this
    module. Lets us swap renderers later without breaking error handling.
    """


def _install_hint() -> str:
    return (
        "playwright is not installed. Install with:\n"
        "    pip install 'sift-engine[browser]' && python -m playwright install chromium"
    )


# ---------------------------------------------------------------------------
# Per-fetch config (immutable, hashable, sift-owned)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BrowserFetchConfig:
    """Per-fetch knobs. Inherits defaults from the ``[browser]`` config section.

    Init-scripts live on ``BrowserConfigDefaults`` (per-context) — Playwright's
    ``add_init_script`` is per-context, so they belong with the pool defaults,
    not the per-fetch knobs. See §5.1 / P0-1 in the design doc.
    """

    wait_until: Literal["domcontentloaded", "load", "networkidle"] = "domcontentloaded"
    page_timeout_s: float = 30.0
    wait_for: Optional[str] = None
    js_code_before_wait: Optional[str] = None
    delay_before_return_html_s: float = 3.0

    # Reserved knob — surfaced in config + contract-tested, but not yet
    # consumed by render() (unlike remove_consent_popups below).
    flatten_shadow_dom: bool = False
    remove_consent_popups: bool = False


# ---------------------------------------------------------------------------
# Render output (immutable, sift-owned)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RenderedPage:
    """Sift-owned projection of Playwright's result.

    ``headers`` invariants (pinned by contract tests):
      * Keys are always lowercased (P1-2).
      * Keys are whitelisted to :data:`PERSISTED_HEADER_KEYS` (P1-4).
      * ``None`` when no navigation response was captured — caller treats
        that as "no conditional-fetch headers known."

    Deliberately omits Playwright's full API surface; if a future feature
    needs network-event capture or screenshots, they get their own
    function rather than expanding this dataclass.
    """

    html: str
    final_url: str
    status_code: int
    elapsed_ms: int
    headers: Optional[dict[str, str]] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Header projection helpers (pure, testable in isolation)
# ---------------------------------------------------------------------------


def _project_headers(raw: dict[str, str]) -> dict[str, str]:
    """Lowercase keys, then filter to :data:`PERSISTED_HEADER_KEYS`.

    The only path that writes into ``RenderedPage.headers``. The two
    invariants (lowercased, whitelisted) are enforced here once instead of
    every callsite.
    """
    out: dict[str, str] = {}
    for k, v in raw.items():
        lk = k.lower()
        if lk in PERSISTED_HEADER_KEYS:
            out[lk] = v
    return out


def _capture_navigation_headers(
    events: list[dict],
) -> Optional[dict[str, str]]:
    """Walk navigation Response events; return headers from the last 200/304.

    A SPA navigation through Akamai can produce 3–6 hops (301/302 → 200).
    Capture-then-filter handles error termination cleanly: keep only
    ``status in (200, 304)``, take the last, project via :func:`_project_headers`.
    Empty list → ``None``.

    See §12.1 / P1-1 / F12 in the design doc.
    """
    successes = [
        e for e in events
        if e.get("status") in (200, 304)
    ]
    if not successes:
        return None
    last = successes[-1]
    return _project_headers(last.get("headers") or {})


# ---------------------------------------------------------------------------
# Eager install check (called at CLI startup if [browser].enabled=true)
# ---------------------------------------------------------------------------


def check_browser_available() -> None:
    """Raise :class:`BrowserNotInstalledError` if Playwright is not importable.

    Used at CLI startup so a daily cron fails fast with a clear hint rather
    than 3 hours in when the first SPA URL hits a missing dep. Idempotent.
    """
    try:
        import playwright  # noqa: F401 — import is the check
    except ImportError as e:
        raise BrowserNotInstalledError(_install_hint()) from e
    if playwright is None:
        # monkeypatched-to-None idiom (used in tests) also fails the check
        raise BrowserNotInstalledError(_install_hint())


# ---------------------------------------------------------------------------
# BrowserPool — owns the shared Playwright Browser + concurrency semaphore
# ---------------------------------------------------------------------------


class BrowserPool:
    """Owns the shared Playwright :class:`Browser` and gates concurrent renders.

    Construct once per sift process; pass the same instance to every
    :func:`render` call; call :meth:`aclose` at shutdown. The Browser is
    lazy-init on first :meth:`acquire` (~1–2s startup paid once, not per
    fetch) and reused across acquires; each render opens a fresh context
    (~50ms) for isolation, then closes it.

    Concurrency is process-wide (not per-host) — RAM (~150–300 MB per page)
    is the binding constraint, and per-host caps don't help with the
    process budget. The ``url`` arg to :meth:`acquire` is reserved for a
    future per-host implementation; today it's unused.
    """

    def __init__(self, concurrency: int, defaults: "BrowserConfigDefaults") -> None:
        if concurrency < 1:
            raise ValueError(f"concurrency must be >= 1, got {concurrency}")
        self._semaphore = asyncio.Semaphore(concurrency)
        self._defaults = defaults
        self._playwright: Optional[object] = None
        self._browser: Optional[object] = None
        self._init_lock = asyncio.Lock()
        self._closed = False

    @property
    def defaults(self) -> "BrowserConfigDefaults":
        """Public accessor for the config defaults (read by render() for
        per-context user_agent / init_scripts wiring)."""
        return self._defaults

    async def _ensure_browser(self) -> object:
        """Lazy-create the shared Playwright Browser on first acquire."""
        if self._browser is not None:
            return self._browser
        async with self._init_lock:
            if self._browser is not None:
                return self._browser
            try:
                from playwright.async_api import async_playwright
            except ImportError as e:
                raise BrowserNotInstalledError(_install_hint()) from e
            pw = await async_playwright().start()
            try:
                browser = await pw.chromium.launch(headless=True)
            except Exception as e:
                # Couldn't even launch — preserve cleanup of the playwright
                # process before raising.
                try:
                    await pw.stop()
                except Exception:
                    pass
                raise BrowserFetchError(
                    f"failed to launch Chromium: {type(e).__name__}: {e}"
                ) from e
            self._playwright = pw
            self._browser = browser
            return browser

    async def acquire(self, url: str) -> AsyncContextManager:
        """Acquire a render slot. Blocks until a slot is free, then yields
        the shared underlying Browser.

        Usage::

            async with await pool.acquire(url) as browser:
                ctx = await browser.new_context(...)
                page = await ctx.new_page()
                ...

        ``url`` is unused today (reserved for future per-host policy); pass
        the target URL for forward-compat.
        """
        if self._closed:
            raise RuntimeError("BrowserPool is closed")
        return self._slot()

    @asynccontextmanager
    async def _slot(self):
        async with self._semaphore:
            browser = await self._ensure_browser()
            yield browser

    async def aclose(self) -> None:
        """Close the underlying Browser + Playwright. Idempotent."""
        if self._closed:
            return
        self._closed = True
        browser = self._browser
        pw = self._playwright
        self._browser = None
        self._playwright = None
        try:
            if browser is not None:
                await browser.close()
        except Exception:
            pass  # best-effort; pool is going away regardless
        try:
            if pw is not None:
                await pw.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# render() — the single entrypoint
# ---------------------------------------------------------------------------


async def render(
    url: str,
    config: BrowserFetchConfig,
    pool: BrowserPool,
) -> RenderedPage:
    """Render ``url`` with a headless browser and return a :class:`RenderedPage`.

    Acquires a slot from ``pool`` (semaphore-gated). The slot yields the
    shared :class:`Browser`; a fresh ``BrowserContext`` is opened for this
    fetch (~50ms), the page is rendered, then the context is closed.

    Raises:
        BrowserNotInstalledError: if the ``[browser]`` extra is missing.
        BrowserFetchError: on any render failure. Playwright's own exception
            types never escape this module.
    """
    try:
        # Imported here only to make TimeoutError/Error coercion explicit;
        # the actual page work happens via the browser from the pool.
        from playwright.async_api import Error as PWError  # noqa: F401
    except ImportError as e:
        raise BrowserNotInstalledError(_install_hint()) from e

    nav_events: list[dict] = []

    def _on_response(response) -> None:
        try:
            req = response.request
            if not req.is_navigation_request():
                return
            nav_events.append({
                "status": response.status,
                "headers": dict(response.headers),
            })
        except Exception:
            # Hook must never raise; degraded => no headers captured.
            pass

    t0 = time.perf_counter()
    try:
        async with await pool.acquire(url) as browser:
            ctx_kwargs: dict[str, object] = {}
            ua = getattr(pool.defaults, "user_agent", "") or ""
            if ua:
                ctx_kwargs["user_agent"] = ua
            init_scripts = tuple(getattr(pool.defaults, "init_scripts", ()))

            ctx = await browser.new_context(**ctx_kwargs)
            try:
                for src in init_scripts:
                    await ctx.add_init_script(src)
                page = await ctx.new_page()
                page.on("response", _on_response)

                resp = await page.goto(
                    url,
                    wait_until=config.wait_until,
                    timeout=int(config.page_timeout_s * 1000),
                )

                if config.wait_for:
                    expr = config.wait_for
                    if expr.startswith("js:"):
                        await page.wait_for_function(expr[3:])
                    else:
                        await page.wait_for_selector(expr)

                if config.js_code_before_wait:
                    await page.evaluate(config.js_code_before_wait)

                if config.delay_before_return_html_s > 0:
                    await asyncio.sleep(config.delay_before_return_html_s)

                if config.remove_consent_popups:
                    # Best-effort consent dismissal — click common buttons.
                    # Placeholder for v0.2.0; per-site profiles can supply
                    # better via js_code_before_wait until we generalize.
                    for sel in (
                        'button:has-text("Accept all")',
                        'button:has-text("Accept All")',
                        'button:has-text("Accept")',
                        '[aria-label="Accept cookies"]',
                    ):
                        try:
                            btn = await page.query_selector(sel)
                            if btn:
                                await btn.click(timeout=1000)
                                break
                        except Exception:
                            pass

                html = await page.content()
                final_url = page.url
                status_code = resp.status if resp else 0
            finally:
                await ctx.close()
    except BrowserNotInstalledError:
        raise
    except Exception as e:
        raise BrowserFetchError(
            f"render failed for {url}: {type(e).__name__}: {e}"
        ) from e

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    headers = _capture_navigation_headers(nav_events)

    return RenderedPage(
        html=html or "",
        final_url=final_url or url,
        status_code=status_code,
        elapsed_ms=elapsed_ms,
        headers=headers,
    )

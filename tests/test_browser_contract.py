"""Failing-by-design contract tests for the browser-fetch capability.

These tests pin the public surface described in
``docs/design/browser-fetch.md``. The browser-only cases are marked
``xfail(strict=False)`` because they require playwright + an installed
chromium (``pip install sift-engine[browser] && python -m playwright install
chromium``). With ``strict=False`` they are skipped-as-xfail without
chromium and reported as ``XPASS`` (non-failing) when chromium is present,
so an installed-chromium environment does not fail the suite.

Run only these:

    pytest tests/test_browser_contract.py -v
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import fields, is_dataclass
from inspect import iscoroutinefunction, signature
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# 1. sift.browser module surface
# ---------------------------------------------------------------------------


class TestBrowserModuleSurface:
    """``sift.browser`` is the single point of playwright coupling."""

    def test_module_importable(self):
        """The module exists and imports cleanly even without playwright installed."""
        import sift.browser  # noqa: F401

    def test_public_surface(self):
        """Every name documented in the design doc is exported."""
        import sift.browser as b

        expected = {
            "BrowserFetchConfig",
            "RenderedPage",
            "BrowserNotInstalledError",
            "BrowserFetchError",
            "render",
        }
        missing = expected - set(dir(b))
        assert not missing, f"sift.browser is missing: {sorted(missing)}"

    def test_render_is_async(self):
        from sift.browser import render

        assert iscoroutinefunction(render), "render() must be an async function"

    def test_render_signature(self):
        """render(url: str, config: BrowserFetchConfig, pool: BrowserPool) -> RenderedPage.

        Three args — `pool` was added in P0-2 (shared AsyncWebCrawler lifecycle).
        """
        from sift.browser import BrowserFetchConfig, BrowserPool, RenderedPage, render

        sig = signature(render)
        params = list(sig.parameters.values())
        assert len(params) == 3, f"render() should take 3 args, got {len(params)}"
        assert params[0].name == "url"
        assert params[1].name == "config"
        assert params[2].name == "pool"
        assert params[1].annotation is BrowserFetchConfig
        assert params[2].annotation is BrowserPool
        assert sig.return_annotation is RenderedPage


# ---------------------------------------------------------------------------
# 2. BrowserFetchConfig defaults
# ---------------------------------------------------------------------------


class TestBrowserFetchConfigDefaults:
    """Every default value documented in the design must hold."""

    def test_is_frozen_dataclass(self):
        from sift.browser import BrowserFetchConfig

        assert is_dataclass(BrowserFetchConfig)
        # frozen() prevents accidental mutation by callers
        cfg = BrowserFetchConfig()
        with pytest.raises((AttributeError, Exception)):
            cfg.wait_until = "load"  # type: ignore[misc]

    def test_default_wait_until(self):
        """domcontentloaded chosen over networkidle: analytics-heavy SPAs (Domain,
        most ad-supported sites) never reach networkidle; domcontentloaded plus a
        delay below works on 80%+ of sites. Sites that genuinely benefit from
        networkidle (calm-network corpora like ATO) opt in via SiteProfile."""
        from sift.browser import BrowserFetchConfig

        assert BrowserFetchConfig().wait_until == "domcontentloaded"

    def test_default_page_timeout(self):
        """30s default — long enough for hydration, short enough that an
        anti-bot hang surfaces as a failure within the same crawl cycle."""
        from sift.browser import BrowserFetchConfig

        assert BrowserFetchConfig().page_timeout_s == 30.0

    def test_default_wait_for_is_none(self):
        from sift.browser import BrowserFetchConfig

        assert BrowserFetchConfig().wait_for is None

    def test_default_js_before_wait_is_none(self):
        from sift.browser import BrowserFetchConfig

        assert BrowserFetchConfig().js_code_before_wait is None

    def test_default_delay_before_return_html(self):
        """3s default settle — pairs with wait_until='domcontentloaded' to let
        SPAs finish their post-load hydration before content() reads the DOM.
        Zero delay only works for fully-SSR'd pages; 3s is the safer floor."""
        from sift.browser import BrowserFetchConfig

        assert BrowserFetchConfig().delay_before_return_html_s == 3.0

    def test_consent_and_shadow_dom_off_by_default(self):
        from sift.browser import BrowserFetchConfig

        cfg = BrowserFetchConfig()
        assert cfg.flatten_shadow_dom is False
        assert cfg.remove_consent_popups is False

    def test_init_scripts_default_on_TOML_defaults(self):
        """P0-1 resolution: init_scripts is per-context (Playwright's
        add_init_script binds at AsyncWebCrawler construction), so it
        belongs on the TOML-config dataclass, not the per-fetch one."""
        from sift.config import BrowserConfigDefaults

        cfg = BrowserConfigDefaults()
        assert cfg.init_scripts == ()
        assert isinstance(cfg.init_scripts, tuple)  # must be hashable

    def test_BrowserFetchConfig_has_no_extra_init_scripts(self):
        """P0-1: per-fetch dataclass must not expose extra_init_scripts —
        it would force a fresh crawler per call. Asserting via field name
        scan rather than attribute access since the field's absence is
        the contract."""
        from dataclasses import fields

        from sift.browser import BrowserFetchConfig

        names = {f.name for f in fields(BrowserFetchConfig)}
        assert "extra_init_scripts" not in names, (
            "extra_init_scripts moved to BrowserConfigDefaults (P0-1). "
            "BrowserFetchConfig must stay per-fetch."
        )

    def test_hashable(self):
        """Frozen + immutable fields => dataclass is hashable, usable in caches."""
        from sift.browser import BrowserFetchConfig

        hash(BrowserFetchConfig())  # must not raise


# ---------------------------------------------------------------------------
# 3. RenderedPage contract
# ---------------------------------------------------------------------------


class TestRenderedPageContract:
    """``RenderedPage`` is the only sift-owned shape playwright's result projects into."""

    def test_is_frozen_dataclass(self):
        from sift.browser import RenderedPage

        assert is_dataclass(RenderedPage)

    def test_required_fields(self):
        from sift.browser import RenderedPage

        names = {f.name for f in fields(RenderedPage)}
        required = {"html", "final_url", "status_code", "elapsed_ms"}
        missing = required - names
        assert not missing, f"RenderedPage missing required fields: {sorted(missing)}"

    def test_optional_fields(self):
        from sift.browser import RenderedPage

        names = {f.name for f in fields(RenderedPage)}
        assert "headers" in names  # optional dict[str, str] | None
        assert "error" in names  # optional str | None

    def test_does_not_expose_playwright_specific_fields(self):
        """RenderedPage MUST NOT leak playwright's surface. If callers need
        network_requests / markdown / screenshot, those go in their own
        functions, not on this dataclass."""
        from sift.browser import RenderedPage

        names = {f.name for f in fields(RenderedPage)}
        forbidden = {"network_requests", "markdown", "screenshot", "console_messages"}
        leaked = names & forbidden
        assert not leaked, f"RenderedPage leaks playwright-specific fields: {sorted(leaked)}"


# ---------------------------------------------------------------------------
# 4. Lazy playwright import — coupling boundary
# ---------------------------------------------------------------------------


class TestLazyPlaywrightImport:
    """Importing ``sift.browser`` must NOT pull in playwright. The import
    happens inside ``render()`` so http-only users never pay for it."""

    def test_no_eager_playwright_import(self, monkeypatch):
        """F14: use monkeypatch to snapshot/restore sys.modules so this test
        doesn't poison downstream tests that need playwright available."""
        # Snapshot playwright modules + sift.browser, then drop them so the
        # re-import is fresh. monkeypatch restores them on teardown.
        for mod in list(sys.modules):
            if mod == "playwright" or mod.startswith("playwright."):
                monkeypatch.delitem(sys.modules, mod, raising=False)
        monkeypatch.delitem(sys.modules, "sift.browser", raising=False)

        importlib.import_module("sift.browser")

        playwright_loaded = any(
            m == "playwright" or m.startswith("playwright.")
            for m in sys.modules
        )
        assert not playwright_loaded, "sift.browser must lazy-import playwright"


# ---------------------------------------------------------------------------
# 5. Error classes
# ---------------------------------------------------------------------------


class TestBrowserErrorClasses:

    def test_BrowserNotInstalledError_inherits_ImportError(self):
        from sift.browser import BrowserNotInstalledError

        assert issubclass(BrowserNotInstalledError, ImportError)

    def test_BrowserFetchError_inherits_Exception(self):
        from sift.browser import BrowserFetchError

        assert issubclass(BrowserFetchError, Exception)
        # Specifically not a subclass of playwright exceptions
        assert "playwright" not in str(BrowserFetchError.__mro__)


# ---------------------------------------------------------------------------
# 6. SiteProfile additions
# ---------------------------------------------------------------------------


class TestSiteProfileBrowserMethods:
    """Two new methods on the base ``SiteProfile``, both default-safe."""

    def test_requires_browser_exists(self):
        from sift.sites import SiteProfile

        assert hasattr(SiteProfile, "requires_browser")

    def test_requires_browser_defaults_false(self):
        from sift.sites import SiteProfile

        sp = SiteProfile()
        assert sp.requires_browser("https://example.com/foo") is False

    def test_browser_config_exists(self):
        from sift.sites import SiteProfile

        assert hasattr(SiteProfile, "browser_config")

    def test_browser_config_defaults_none(self):
        from sift.sites import SiteProfile

        sp = SiteProfile()
        assert sp.browser_config("https://example.com/foo") is None

    def test_generic_profile_keeps_defaults(self):
        """Adding the methods to the base must NOT change GenericProfile behavior."""
        from sift.sites.generic import GenericProfile

        gp = GenericProfile()
        assert gp.requires_browser("https://example.com/") is False
        assert gp.browser_config("https://example.com/") is None


class TestATOProfileBrowserOverrides:
    """ATOProfile opts /single-page-applications/ URLs into browser rendering
    and drops them from default_excludes."""

    def test_legaldatabase_requires_browser(self):
        from sift.sites.ato import ATOProfile

        ap = ATOProfile()
        url = "https://www.ato.gov.au/single-page-applications/legaldatabase"
        assert ap.requires_browser(url) is True

    def test_iar_requires_browser(self):
        from sift.sites.ato import ATOProfile

        ap = ATOProfile()
        url = "https://www.ato.gov.au/single-page-applications/iar"
        assert ap.requires_browser(url) is True

    def test_static_path_does_not_require_browser(self):
        """Non-SPA URLs continue to use the http path."""
        from sift.sites.ato import ATOProfile

        ap = ATOProfile()
        url = "https://www.ato.gov.au/individuals/income-deductions-offsets-and-records"
        assert ap.requires_browser(url) is False

    def test_spa_pattern_dropped_from_excludes(self):
        from sift.sites.ato import ATOProfile

        ap = ATOProfile()
        assert all(
            "single-page-applications" not in pattern
            for pattern in ap.default_excludes
        ), "ATOProfile must drop /single-page-applications/ from default_excludes"

    def test_browser_config_uses_networkidle_for_spa(self):
        """ATO's Legal DB Next.js SPA needs networkidle (its post-load network
        is calm enough to reach idle reliably; the global domcontentloaded
        default returns before Next.js hydration completes). This is the
        canonical per-site override — proves the SiteProfile escape hatch
        composes with the swapped Playwright renderer.

        Consent / shadow-DOM stay off — defaults already off, ATO doesn't
        need them."""
        from sift.sites.ato import ATOProfile

        ap = ATOProfile()
        cfg = ap.browser_config(
            "https://www.ato.gov.au/single-page-applications/legaldatabase"
        )
        assert cfg is not None, "ATO SPA URLs must return an explicit config"
        assert cfg.wait_until == "networkidle", (
            f"ATO SPA needs networkidle for hydration; got {cfg.wait_until!r}"
        )
        assert cfg.page_timeout_s >= 30.0
        assert cfg.flatten_shadow_dom is False
        assert cfg.remove_consent_popups is False

    def test_browser_config_returns_none_for_non_spa(self):
        """Static URLs don't need browser at all; profile shouldn't pretend
        to configure one for them. Lets fetch.py's dispatch route them to
        http cleanly."""
        from sift.sites.ato import ATOProfile

        ap = ATOProfile()
        cfg = ap.browser_config(
            "https://www.ato.gov.au/individuals-and-families/managing-your-tax"
        )
        assert cfg is None, (
            f"non-SPA URLs should return None (http path); got {cfg!r}"
        )


# ---------------------------------------------------------------------------
# 7. [browser] config section
# ---------------------------------------------------------------------------


class TestConfigBrowserSection:
    """``IndexConfig`` parses a new ``[browser]`` TOML section."""

    def test_BrowserConfigDefaults_importable(self):
        from sift.config import BrowserConfigDefaults  # noqa: F401

    def test_indexconfig_has_browser_attr(self):
        from sift.config import IndexConfig

        cfg = IndexConfig()
        assert hasattr(cfg, "browser")

    def test_browser_defaults(self):
        from sift.config import IndexConfig

        b = IndexConfig().browser
        assert b.enabled is True
        assert b.concurrency == 2
        assert b.page_timeout_s == 60
        assert b.wait_until == "networkidle"
        assert b.flatten_shadow_dom is False
        assert b.remove_consent_popups is False
        assert b.user_agent == ""

    def test_browser_section_parsed_from_toml(self, tmp_path: Path, monkeypatch):
        """F7: also asserts init_scripts round-trips, since the P0-1 relocation
        moved it from BrowserFetchConfig to here — the TOML path is the only
        way operators can set it."""
        from sift.config import load_config

        monkeypatch.chdir(tmp_path)
        toml = tmp_path / "sift.toml"
        toml.write_text(
            "[browser]\n"
            "enabled = false\n"
            "concurrency = 4\n"
            "page_timeout_s = 90\n"
            'wait_until = "load"\n'
            "flatten_shadow_dom = true\n"
            "remove_consent_popups = true\n"
            'user_agent = "MyBot/1.0"\n'
            'init_scripts = ["window.foo = 1;", "window.bar = 2;"]\n'
        )
        cfg = load_config(toml)
        assert cfg.browser.enabled is False
        assert cfg.browser.concurrency == 4
        assert cfg.browser.page_timeout_s == 90
        assert cfg.browser.wait_until == "load"
        assert cfg.browser.flatten_shadow_dom is True
        assert cfg.browser.remove_consent_popups is True
        assert cfg.browser.user_agent == "MyBot/1.0"
        # F7: init_scripts must round-trip from TOML list to a tuple
        # (tuple for dataclass hashability, per P0-1 resolution)
        assert cfg.browser.init_scripts == ("window.foo = 1;", "window.bar = 2;")
        assert isinstance(cfg.browser.init_scripts, tuple)


# ---------------------------------------------------------------------------
# 8. BROWSER_VERSION constant
# ---------------------------------------------------------------------------


class TestBrowserVersionConstant:

    def test_browser_version_importable(self):
        from sift.browser import BROWSER_VERSION  # noqa: F401

    def test_browser_version_shape(self):
        """P1-3 resolution: pin playwright alone, let it be the proxy for the
        bundled Chromium binary. Chromium drift is surfaced separately in
        sift status (test_status_reports_browser_runtime_chromium)."""
        from sift.browser import BROWSER_VERSION

        assert isinstance(BROWSER_VERSION, str)
        assert "playwright" in BROWSER_VERSION.lower()
        # Must NOT pin Chromium directly — that's the whole point of P1-3
        assert "chromium" not in BROWSER_VERSION.lower(), (
            "P1-3: Chromium pin is fragile (playwright install chromium can change "
            "binary without bumping the constant). Pin playwright version only."
        )


# ---------------------------------------------------------------------------
# 9. Optional dep declaration
# ---------------------------------------------------------------------------


class TestOptionalBrowserExtra:
    """``sift-engine[browser]`` is the only opt-in path to install playwright."""

    def test_pyproject_declares_browser_extra(self):
        try:
            import tomllib  # py311+
        except ImportError:
            import tomli as tomllib  # type: ignore[no-redef]

        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text())

        extras = (
            data.get("project", {})
            .get("optional-dependencies", {})
        )
        assert "browser" in extras, (
            "Expected `[project.optional-dependencies].browser` "
            "to list playwright. Got: " + ", ".join(extras.keys())
        )
        joined = " ".join(extras["browser"]).lower()
        assert "playwright" in joined, "browser extra must declare playwright"


# ---------------------------------------------------------------------------
# 10. Resolved-decision contracts (§12 of the design doc)
# ---------------------------------------------------------------------------


class TestResponseHookHeaderCapture:
    """§12.1 — RenderedPage.headers is populated from the Playwright Response
    hook when available, None when capture fails (graceful degradation)."""

    def test_headers_is_optional_dict(self):
        """The annotation must be `dict[str, str] | None`, not just dict."""
        from typing import get_type_hints

        from sift.browser import RenderedPage

        hints = get_type_hints(RenderedPage)
        headers_type = hints["headers"]
        # Accept either Optional[dict[str, str]] or dict[str, str] | None
        type_str = str(headers_type).lower()
        assert "none" in type_str or "optional" in type_str, (
            f"headers must be optional; got {headers_type!r}"
        )

    def test_rendered_page_accepts_etag_and_last_modified(self):
        """The two header keys sift's plan phase consumes must round-trip."""
        from sift.browser import RenderedPage

        page = RenderedPage(
            html="<html></html>",
            final_url="https://example.com",
            status_code=200,
            elapsed_ms=1234,
            headers={"etag": 'W/"abc"', "last-modified": "Wed, 21 Oct 2020 07:28:00 GMT"},
        )
        assert page.headers is not None
        assert page.headers.get("etag") == 'W/"abc"'
        assert page.headers.get("last-modified") == "Wed, 21 Oct 2020 07:28:00 GMT"

    def test_rendered_page_accepts_none_headers(self):
        """Graceful degradation: hook failure -> headers=None, no error."""
        from sift.browser import RenderedPage

        page = RenderedPage(
            html="<html></html>",
            final_url="https://example.com",
            status_code=200,
            elapsed_ms=1234,
            headers=None,
        )
        assert page.headers is None

    def test_headers_keys_are_lowercased(self):
        """Header dict keys must be lowercased — pin via _project_headers
        helper which is the only writer of RenderedPage.headers."""
        from sift.browser import _project_headers

        out = _project_headers({
            "ETag": 'W/"abc"',
            "Last-Modified": "Wed, 21 Oct 2020 07:28:00 GMT",
            "Cache-Control": "max-age=3600",
        })
        # Every key in the result must be lowercase
        for k in out:
            assert k == k.lower(), f"header key not lowercased: {k!r}"
        assert out["etag"] == 'W/"abc"'
        assert out["last-modified"] == "Wed, 21 Oct 2020 07:28:00 GMT"
        assert out["cache-control"] == "max-age=3600"

    def test_persisted_header_keys_constant(self):
        """The whitelist is a frozenset, importable, contains exactly three
        cache-validation keys."""
        from sift.browser import PERSISTED_HEADER_KEYS

        assert isinstance(PERSISTED_HEADER_KEYS, frozenset)
        assert PERSISTED_HEADER_KEYS == frozenset({"etag", "last-modified", "cache-control"})

    def test_headers_whitelist_drops_non_cache_keys(self):
        """_project_headers drops everything not in PERSISTED_HEADER_KEYS.
        Specifically: content-length / content-encoding / server / set-cookie
        describe the SPA shell, not the rendered content."""
        from sift.browser import _project_headers

        out = _project_headers({
            "etag": 'W/"abc"',
            "content-length": "12345",
            "content-encoding": "gzip",
            "server": "Akamai",
            "set-cookie": "ak_bmsc=...",
            "last-modified": "Wed, 21 Oct 2020 07:28:00 GMT",
        })
        assert set(out.keys()) == {"etag", "last-modified"}, (
            f"only whitelisted keys should survive; got {sorted(out)}"
        )


class TestBrowserPoolInterface:
    """§12.2 — concurrency lives behind a `BrowserPool` abstraction so
    per-host can be added later without callsite changes."""

    def test_BrowserPool_importable(self):
        from sift.browser import BrowserPool  # noqa: F401

    def test_BrowserPool_constructor_takes_concurrency_and_defaults(self):
        """F13: `BrowserPool(concurrency, defaults)` per the §12.2 spec, not
        `BrowserPool(concurrency)`. The previous test signature would have
        TypeError'd on first impl, perpetually xfail — never catching the
        contract violation."""
        from sift.browser import BrowserPool
        from sift.config import BrowserConfigDefaults

        pool = BrowserPool(concurrency=2, defaults=BrowserConfigDefaults())
        assert pool is not None

    def test_BrowserPool_acquire_signature(self):
        """acquire(url: str) -> AsyncContextManager — `url` reserved for future
        per-host variant; today it's unused."""
        from inspect import signature

        from sift.browser import BrowserPool

        sig = signature(BrowserPool.acquire)
        params = list(sig.parameters.values())
        # self, url
        assert len(params) == 2
        assert params[1].name == "url"
        assert params[1].annotation is str

    @pytest.mark.xfail(
        strict=False,
        reason="needs playwright + chromium installed (pip install sift-engine[browser] "
        "&& python -m playwright install chromium)",
    )
    @pytest.mark.asyncio
    async def test_BrowserPool_acquire_limits_concurrency(self):
        """Two concurrent acquires on a concurrency=1 pool: second blocks until
        first exits."""
        import asyncio

        from sift.browser import BrowserPool
        from sift.config import BrowserConfigDefaults

        pool = BrowserPool(concurrency=1, defaults=BrowserConfigDefaults())
        order: list[str] = []

        async def worker(name: str, hold_s: float):
            async with await pool.acquire("https://example.com"):
                order.append(f"{name}:enter")
                await asyncio.sleep(hold_s)
                order.append(f"{name}:exit")

        try:
            await asyncio.gather(worker("A", 0.05), worker("B", 0.01))
        finally:
            await pool.aclose()
        # A enters and exits before B enters (concurrency=1)
        assert order == ["A:enter", "A:exit", "B:enter", "B:exit"]

    @pytest.mark.xfail(
        strict=False,
        reason="needs playwright + chromium installed (pip install sift-engine[browser] "
        "&& python -m playwright install chromium); see P0-2 shared-crawler contract",
    )
    @pytest.mark.asyncio
    async def test_BrowserPool_yields_shared_crawler(self):
        """P0-2 resolution: two acquires must return the SAME underlying
        AsyncWebCrawler instance. Without this pin, an implementation could
        legitimately spin up a fresh crawler per acquire (5-10s startup ×
        every render) and still pass test_BrowserPool_acquire_limits_concurrency."""
        from sift.browser import BrowserPool
        from sift.config import BrowserConfigDefaults

        pool = BrowserPool(concurrency=2, defaults=BrowserConfigDefaults())
        try:
            async with await pool.acquire("https://example.com/a") as crawler1:
                pass
            async with await pool.acquire("https://example.com/b") as crawler2:
                pass
            assert crawler1 is crawler2, (
                "BrowserPool must reuse the same AsyncWebCrawler across acquires "
                "(P0-2: crawler-process startup is the expensive part, ~5-10s)"
            )
        finally:
            await pool.aclose()

    @pytest.mark.asyncio
    async def test_BrowserPool_aclose_is_idempotent(self):
        """Double-close must not raise — common in cleanup paths."""
        from sift.browser import BrowserPool
        from sift.config import BrowserConfigDefaults

        pool = BrowserPool(concurrency=1, defaults=BrowserConfigDefaults())
        await pool.aclose()
        await pool.aclose()  # second call must be a no-op


class TestBrowserDisabledSkipState:
    """§12.3 (P0-3 resolution) — when [browser].enabled=false, browser-required
    URLs are short-circuited by plan.py to Decision.SKIPPED_BROWSER_DISABLED.
    Three vocabularies kept distinct: Decision enum, manifest state literal,
    terminal-state set helper. NO exported `manifest.SKIPPED_BROWSER_DISABLED`
    constant (matches existing manifest-state convention)."""

    def test_Decision_enum_has_skipped_browser_disabled(self):
        """The new value joins existing FETCH/FETCH_CONDITIONAL/SKIP/TOMBSTONE_PURGE
        on the same enum. Tests don't import a separate constant — they read
        the enum like every other Decision-checking code does today."""
        from sift.decide import Decision

        assert hasattr(Decision, "SKIPPED_BROWSER_DISABLED")
        assert Decision.SKIPPED_BROWSER_DISABLED.value == "SKIPPED_BROWSER_DISABLED"

    def test_route_to_browser_disabled_helper_exists(self):
        """Named helper in plan.py with a tight signature, testable in isolation.
        Not buried inline so future route_to_* siblings have a clean home."""
        from inspect import signature

        from sift.plan import route_to_browser_disabled

        sig = signature(route_to_browser_disabled)
        params = list(sig.parameters)
        # url, profile, cfg
        assert params == ["url", "profile", "cfg"], (
            f"expected (url, profile, cfg), got {params}"
        )

    def test_route_to_browser_disabled_true_case(self):
        from sift.config import IndexConfig
        from sift.plan import route_to_browser_disabled
        from sift.sites import SiteProfile

        class _ForceBrowser(SiteProfile):
            def requires_browser(self, url: str) -> bool:
                return True

        cfg = IndexConfig()
        cfg.browser.enabled = False  # type: ignore[attr-defined]
        assert route_to_browser_disabled("https://x/spa", _ForceBrowser(), cfg) is True

    def test_route_to_browser_disabled_false_cases(self):
        """Three negative cases: enabled=True+requires=True, enabled=False+requires=False,
        enabled=True+requires=False. All should return False (don't short-circuit)."""
        from sift.config import IndexConfig
        from sift.plan import route_to_browser_disabled
        from sift.sites import SiteProfile

        class _ForceBrowser(SiteProfile):
            def requires_browser(self, url: str) -> bool:
                return True

        cfg_on = IndexConfig()
        cfg_on.browser.enabled = True  # type: ignore[attr-defined]
        cfg_off = IndexConfig()
        cfg_off.browser.enabled = False  # type: ignore[attr-defined]

        assert route_to_browser_disabled("https://x/spa", _ForceBrowser(), cfg_on) is False
        assert route_to_browser_disabled("https://x/spa", SiteProfile(), cfg_off) is False
        assert route_to_browser_disabled("https://x/spa", SiteProfile(), cfg_on) is False

    def test_is_terminal_state_helper_exists(self):
        """Extracted from inlined sum in gate_coverage. Allows the coverage
        gate logic to be checked against any state without duplicating the set."""
        from sift.publish import _is_terminal_state, _TERMINAL_STATES

        assert isinstance(_TERMINAL_STATES, (set, frozenset))
        assert _is_terminal_state("FRESH") is True
        assert _is_terminal_state("GONE") is True
        assert _is_terminal_state("FROZEN") is True
        assert _is_terminal_state("SKIPPED_BROWSER_DISABLED") is True
        # Negative cases — non-terminal states
        assert _is_terminal_state("UNSEEN") is False
        assert _is_terminal_state("FAILED") is False


class TestEagerBrowserImportCheck:
    """§12.3 — at CLI startup with [browser].enabled=true, sift must
    attempt `import playwright` and raise loudly if missing."""

    def test_eager_check_function_exists(self):
        """A discrete function exists for the import-check so it can be tested
        in isolation and reused by other entrypoints (sift-mcp, etc.)."""
        from sift.browser import check_browser_available  # noqa: F401

    def test_eager_check_raises_when_dep_missing(self, monkeypatch):
        """If playwright isn't importable, check raises BrowserNotInstalledError
        with an install hint in the message."""
        import sys

        from sift.browser import BrowserNotInstalledError, check_browser_available

        # Force playwright to appear missing
        monkeypatch.setitem(sys.modules, "playwright", None)

        with pytest.raises(BrowserNotInstalledError) as exc:
            check_browser_available()
        msg = str(exc.value)
        assert "pip install" in msg
        assert "sift-engine[browser]" in msg or "'sift-engine[browser]'" in msg

    def test_eager_check_silent_when_dep_present(self):
        """Smoke test: when playwright is installed, check returns None (no raise).
        This test assumes playwright is in the test env via [dev,evals] extras
        OR mocks the import."""
        import sys
        from types import ModuleType

        from sift.browser import check_browser_available

        # Provide a stub playwright if not actually installed
        if "playwright" not in sys.modules:
            sys.modules["playwright"] = ModuleType("playwright")
        try:
            assert check_browser_available() is None
        finally:
            # Don't pollute downstream tests
            mod = sys.modules.get("playwright")
            if mod is not None and not hasattr(mod, "AsyncWebCrawler"):
                sys.modules.pop("playwright", None)


class TestSchemaMigrationV1toV2:
    """§8.1 (P0-4 resolution) — bumping schema must work on existing v1 DBs.
    Without this, the first `sift plan` against a v0.1.0 manifest crashes
    with `OperationalError: no such column: browser_version`."""

    def test_schema_version_bumped_to_2(self):
        from sift.manifest import SCHEMA_VERSION

        assert SCHEMA_VERSION == 2

    def test_migrate_helper_exists(self):
        from inspect import signature

        from sift.manifest import _migrate

        sig = signature(_migrate)
        # conn, from_v, to_v
        params = list(sig.parameters)
        assert params == ["conn", "from_v", "to_v"], (
            f"expected (conn, from_v, to_v); got {params}"
        )

    def test_migrations_registry_has_v1_to_v2(self):
        from sift.manifest import _MIGRATIONS

        assert 1 in _MIGRATIONS, "must have a migration from v1 to v2"
        sql = _MIGRATIONS[1].upper()
        assert "ALTER TABLE" in sql
        assert "BROWSER_VERSION" in sql

    def test_init_schema_migrates_existing_v1_db(self, tmp_path):
        """End-to-end: synthesize a v1 DB (real CREATE TABLE shape + schema_version=1),
        call init_schema, assert the new column appears AND schema_version flips to 2.

        F4: uses the real v1 schema (every column + index v0.1.0 actually created),
        not a minimal mock — otherwise CREATE INDEX statements in init_schema
        fail on missing columns and we'd be testing fiction, not the migration.
        """
        import sqlite3

        from sift.manifest import init_schema

        db_path = tmp_path / "v1.db"
        conn = sqlite3.connect(db_path)

        # Synthesize a v1-shaped DB matching what sift v0.1.0 actually wrote.
        # No browser_version column (that's the v2 addition).
        conn.executescript("""
            CREATE TABLE meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE manifest (
                url                  TEXT PRIMARY KEY,
                tier                 TEXT NOT NULL,
                parent_guide         TEXT,
                state                TEXT NOT NULL DEFAULT 'UNSEEN',
                sitemap_lastmod_seen TEXT,
                first_seen_at        TEXT NOT NULL,
                last_fetched_at      TEXT,
                last_attempted_at    TEXT,
                http_status          INTEGER,
                http_etag            TEXT,
                http_last_modified   TEXT,
                raw_hash             TEXT,
                content_hash         TEXT,
                last_changed_at      TEXT,
                unchanged_streak     INTEGER NOT NULL DEFAULT 0,
                crawler_version      TEXT,
                extractor_version    TEXT,
                normalizer_version   TEXT,
                classifier_version   TEXT,
                fail_count           INTEGER NOT NULL DEFAULT 0,
                last_error           TEXT
            );
            CREATE INDEX idx_manifest_state        ON manifest(state);
            CREATE INDEX idx_manifest_tier         ON manifest(tier);
            CREATE INDEX idx_manifest_parent_guide ON manifest(parent_guide);
            CREATE TABLE runs (
                run_id       TEXT PRIMARY KEY,
                started_at   TEXT NOT NULL,
                completed_at TEXT,
                phase        TEXT,
                status       TEXT,
                counts_json  TEXT,
                error        TEXT
            );
            INSERT INTO meta(key, value) VALUES ('schema_version', '1');
        """)
        conn.commit()

        # Run init_schema on the existing v1 DB → must migrate
        init_schema(conn)

        # 1. The new column appears
        cols = {row[1] for row in conn.execute("PRAGMA table_info(manifest)").fetchall()}
        assert "browser_version" in cols, (
            "init_schema must ALTER TABLE to add browser_version on v1->v2 upgrade"
        )

        # 2. meta.schema_version updates to 2
        v = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        assert v == "2", f"schema_version should be '2' post-migration, got {v!r}"

        conn.close()

    def test_init_schema_on_fresh_db_lands_at_v2(self, tmp_path):
        """Fresh-DB path: CREATE TABLE includes browser_version, schema_version
        written as 2. No migration runs because there's nothing to migrate from."""
        import sqlite3

        from sift.manifest import init_schema

        db_path = tmp_path / "fresh.db"
        conn = sqlite3.connect(db_path)
        init_schema(conn)

        cols = {row[1] for row in conn.execute("PRAGMA table_info(manifest)").fetchall()}
        assert "browser_version" in cols, "fresh DB must include browser_version"

        v = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        assert v == "2"

        conn.close()


class TestStatusBrowserMetrics:
    """§12.4 — `sift status` exposes two new observability metrics."""

    def test_status_reports_browser_disabled_skip_count(self, tmp_path):
        """When manifest contains SKIPPED_BROWSER_DISABLED rows, status
        surfaces a count under a named field."""
        from sift import status

        # The exact API will be settled during implementation. This pins the
        # contract: there's a field named `skipped_browser_disabled` in
        # whatever status returns.
        summary = status.compute_status_summary(tmp_path)
        assert "skipped_browser_disabled" in summary

    def test_status_reports_cached_headers_ratio(self, tmp_path):
        """When manifest contains browser-fetched rows, status surfaces a
        fraction: how many of them have non-null cached headers
        (i.e. participate in conditional-fetch)."""
        from sift import status

        summary = status.compute_status_summary(tmp_path)
        assert "browser_urls_with_cached_headers" in summary

    def test_status_reports_browser_runtime_chromium(self, tmp_path):
        """P1-3 resolution: sift status exposes the actual Chromium version
        playwright resolved at startup, separately from the BROWSER_VERSION
        pin. Operator can detect Chromium drift even though it doesn't
        invalidate cached blobs."""
        from sift import status

        summary = status.compute_status_summary(tmp_path)
        assert "versions" in summary
        assert "browser_runtime_chromium" in summary["versions"], (
            "Expected versions.browser_runtime_chromium key; "
            f"got versions keys: {sorted(summary.get('versions', {}))}"
        )


class TestResponseHookRedirectChain:
    """P1-1 — `is_navigation_request()` fires on every hop of a redirect
    chain; the hook must use last-wins semantics so the captured ETag
    reflects the *final* 200 response, not the first 301/302."""

    @pytest.mark.asyncio
    async def test_response_hook_last_navigation_wins(self):
        """F12-refined: spec is capture-then-filter, not pure last-wins.
        Walk nav events, keep only status in (200, 304), return the last
        one. Happy-path case: redirect chain ending in 200 returns the
        200's headers."""
        from sift.browser import _capture_navigation_headers

        events = [
            {"status": 301, "headers": {"etag": 'W/"redirect1"', "location": "/middle"}},
            {"status": 302, "headers": {"etag": 'W/"redirect2"', "location": "/final"}},
            {"status": 200, "headers": {"etag": 'W/"final"', "last-modified": "FINAL", "cache-control": "max-age=3600"}},
        ]
        result = _capture_navigation_headers(events)
        assert result is not None
        assert result.get("etag") == 'W/"final"'
        assert result.get("last-modified") == "FINAL"
        assert result.get("cache-control") == "max-age=3600"
        # Redirect etags must not leak through
        assert 'W/"redirect1"' not in result.values()
        assert 'W/"redirect2"' not in result.values()

    @pytest.mark.asyncio
    async def test_response_hook_returns_none_on_5xx_terminal(self):
        """F12 spec: when navigation terminates without a 200/304, return None
        rather than persist the 5xx's headers. Failed-fetch cache state is
        no state, not partial state."""
        from sift.browser import _capture_navigation_headers

        # Redirect chain that ends in a server error — no 200/304 in the chain
        events_5xx = [
            {"status": 301, "headers": {"etag": 'W/"redirect1"', "location": "/middle"}},
            {"status": 302, "headers": {"etag": 'W/"redirect2"', "location": "/final"}},
            {"status": 503, "headers": {"server": "Akamai", "retry-after": "5"}},
        ]
        assert _capture_navigation_headers(events_5xx) is None

        # Single 5xx, no redirects
        events_500 = [
            {"status": 500, "headers": {"server": "Akamai"}},
        ]
        assert _capture_navigation_headers(events_500) is None

        # Empty event list — no navigation occurred
        assert _capture_navigation_headers([]) is None

    @pytest.mark.asyncio
    async def test_response_hook_handles_304_conditional(self):
        """F12 spec: 304 Not Modified counts as a terminal success status
        alongside 200. On conditional re-fetch, the 304's etag/last-modified
        update the stored values (server confirmed they're still valid)."""
        from sift.browser import _capture_navigation_headers

        events = [
            {"status": 200, "headers": {"etag": 'W/"old"', "last-modified": "OLD"}},
            {"status": 304, "headers": {"etag": 'W/"still-valid"', "last-modified": "CONFIRMED"}},
        ]
        result = _capture_navigation_headers(events)
        assert result is not None
        assert result.get("etag") == 'W/"still-valid"'
        assert result.get("last-modified") == "CONFIRMED"

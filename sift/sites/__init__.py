"""Site profiles — pluggable per-site customizations.

The core pipeline (manifest, fetch, extract, commit, publish, MCP server) is
**site-agnostic**. Everything that needs site-specific knowledge lives in a
`SiteProfile`:

  * URL classification (tier mapping, audience map, year extraction, parent_guide)
  * Normalize patterns (per-site dynamic boilerplate the hash should ignore)
  * Section taxonomy for the agent-facing INDEX.md
  * Facts schemas + extractor functions

The active profile is a module-level singleton, set once at CLI startup from
`config.site.profile` (a `"module:Class"` import path). The pipeline calls
`current_profile().classify_tier(url, year)` etc. — never any hardcoded site
logic.

**Adding a new site** (e.g. irs.gov):
  1. Create `sift/sites/irs.py` with `class IRSProfile(SiteProfile)`.
  2. Override the methods/properties that differ from defaults.
  3. Set `[site] profile = "sift.sites.irs:IRSProfile"` in config.
  4. Reseed and run. No other code changes.

**Defaults** are deliberately minimal (everything LIVING, no facts, no
boilerplate stripping). The `ATOProfile` is the reference implementation
of every override.
"""

from __future__ import annotations

import importlib
import re
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    # Forward-only reference. Importing sift.browser eagerly would defeat
    # its lazy-crawl4ai contract (TestLazyCrawl4AIImport) on any site import.
    from ..browser import BrowserFetchConfig


_MARKDOWN_CONTENT_TYPES = frozenset({"text/markdown", "text/x-markdown"})


def _is_markdown_content_type(content_type: Optional[str]) -> bool:
    """True for an explicit Markdown Content-Type (charset/params stripped).

    Deliberately strict: ``text/plain`` is NOT treated as markdown (too
    ambiguous), so sites that mislabel markdown as text/plain (e.g. Stripe)
    must be recognized by a profile URL rule instead — see ``body_kind``."""
    if not content_type:
        return False
    return content_type.split(";", 1)[0].strip().lower() in _MARKDOWN_CONTENT_TYPES


class SiteProfile:
    """Per-site customization surface. Override what your site needs;
    the rest defaults to safe generic behavior.

    Static class attributes (`name`, `primary_host`) are subclass-overridable.
    Methods (`classify_tier`, `audience`, etc.) take URLs and return decisions.
    Properties (`default_excludes`, `dynamic_patterns`, ...) return data the
    pipeline consumes during seed, normalize, and publish.
    """

    # ---- Identity ----------------------------------------------------------
    name: str = "generic"
    primary_host: str = ""

    # ---- URL classification (overridable methods) --------------------------

    def classify_tier(self, url: str, current_year_start: int) -> str:
        """Map URL to one of LIVING / NEWS / CURRENT_FORMS / FROZEN.

        Default: every URL is LIVING. Override for sites with annual-cycle
        content, historical archives, news feeds, etc.

        `current_year_start` is passed from config so the profile can decide
        whether a year-embedded URL is past, current, or future.
        """
        return "LIVING"

    def fy_years(self, url: str) -> list[str]:
        """All financial/calendar years extracted from the URL, formatted
        per the site's convention (e.g. 'YYYY-YY' for Australian FY,
        'YYYY' for calendar). Default: none."""
        return []

    def parent_guide(self, url: str) -> Optional[str]:
        """For multi-page guide structures, return the guide slug; else None.
        Used by agent_surface to group sub-pages under their parent guide."""
        return None

    def audience(self, url: str) -> str:
        """Coarse audience label from URL path. Default: 'general'."""
        return "general"

    # ---- Browser-fetch routing (overridable) -------------------------------

    def requires_browser(self, url: str) -> bool:
        """Whether this URL needs browser rendering. Default: never.

        Profiles override for SPAs or other JS-rendered URL patterns.
        See ``docs/design/browser-fetch.md`` §6.
        """
        return False

    def browser_config(self, url: str) -> "Optional[BrowserFetchConfig]":
        """Per-URL browser knobs. Default: ``None`` (use ``[browser]`` defaults).

        Return a :class:`~sift.browser.BrowserFetchConfig` to override specific
        knobs (consent-banner removal, longer timeout, JS kick, etc.) for
        URLs matched by this profile. Different URL patterns can return
        different configs.
        """
        return None

    # ---- Extraction routing (overridable) ----------------------------------

    def body_kind(self, url: str, *, content_type: Optional[str] = None) -> Optional[str]:
        """Classify the fetched body for the extract phase, or ``None`` to let
        the core dispatcher sniff it (PDF by magic bytes / URL, JSON by
        content-type, else HTML).

        Return ``"markdown"``, ``"pdf"``, ``"json"``, or ``"html"`` to force an
        extractor (``"json"`` routes to the API-as-content lane).
        The main use is **markdown pass-through**: endpoints that already serve
        Markdown (``.md`` docs variants, ``llms.txt``) get mangled by the HTML
        extractor (trafilatura), so they should be stored as-is.

        Base behavior: an explicit ``text/markdown`` Content-Type is taken as
        markdown; everything else defers (``None``). Profiles override to also
        recognize their own markdown endpoints by URL shape — required when the
        server labels markdown as ``text/plain`` (e.g. Stripe), where
        Content-Type alone would miss it.

        ``content_type`` is the raw response header and may be ``None`` on the
        re-extract path (it isn't persisted in the manifest), so URL-based rules
        are the durable signal; treat ``content_type`` as an opportunistic hint.
        """
        if _is_markdown_content_type(content_type):
            return "markdown"
        return None

    # ---- Data properties (overridable) -------------------------------------

    @property
    def default_excludes(self) -> tuple[str, ...]:
        """URL-path regex patterns the seed phase skips by default.
        Examples: '^/sitemap', '^/api/', '^/print/'."""
        return ()

    @property
    def dynamic_patterns(self) -> tuple[re.Pattern[str], ...]:
        """Compiled regex patterns the normalizer strips before hashing.
        Site-specific rotating boilerplate (timestamps, session ids, etc.)
        that would otherwise cause false-positive content changes."""
        return ()

    @property
    def section_order(self) -> list[tuple[str, str, str]]:
        """Top-level URL sections, in display order, for INDEX.md.
        Each entry: (path_segment, audience_label, human_heading).
        Sections not listed here still get an INDEX.md but appear
        below the curated sections."""
        return []

    # ---- Facts framework (overridable) -------------------------------------

    @property
    def facts_schemas(self) -> dict[str, dict]:
        """JSON schemas for facts files, keyed by their `$id` field.
        These are written to `runs/<run>/facts/schemas/` and validated
        against during publish (gate G6)."""
        return {}

    @property
    def facts_extractors(self) -> list[tuple[Callable[[str], bool], Callable]]:
        """List of (url_matcher, extractor_fn) tuples.

        url_matcher: (str) -> bool — does this extractor apply to this URL?
        extractor_fn: keyword args (url, html, fy, content_hash) ->
                     list[FactCandidate]

        Empty by default — the framework just won't emit any facts."""
        return []


# ---- Singleton ----------------------------------------------------------

_CURRENT_PROFILE: SiteProfile = SiteProfile()


def current_profile() -> SiteProfile:
    """Read the active site profile. Used everywhere site-specific decisions
    are made in the pipeline (classify, normalize, agent_surface, facts)."""
    return _CURRENT_PROFILE


def set_profile(profile: SiteProfile) -> None:
    """Replace the active profile. Called once at CLI startup after config
    load; tests can call this to swap in a synthetic profile."""
    global _CURRENT_PROFILE
    _CURRENT_PROFILE = profile


def load_profile(import_path: str) -> SiteProfile:
    """Import + instantiate a SiteProfile by 'module:Class' path.

    Example:
        load_profile('sift.sites.ato:ATOProfile')
        load_profile('mycorp.sites.irs:IRSProfile')

    Raises ValueError on malformed paths or ImportError if the module
    doesn't exist (so misconfiguration fails loudly at startup, not
    silently mid-pipeline).
    """
    if ":" not in import_path:
        raise ValueError(
            f"profile import path must be 'module:Class', got: {import_path!r}"
        )
    module_name, class_name = import_path.split(":", 1)
    module = importlib.import_module(module_name)
    try:
        klass = getattr(module, class_name)
    except AttributeError as e:
        raise ImportError(
            f"profile class {class_name!r} not found in {module_name!r}"
        ) from e
    instance = klass()
    if not isinstance(instance, SiteProfile):
        raise TypeError(
            f"{import_path} did not produce a SiteProfile instance "
            f"(got {type(instance).__name__})"
        )
    return instance

"""GenericBrowserProfile — every URL routes through the browser path.

Use this profile when you don't know in advance which URLs in your target
corpus are JavaScript-rendered SPAs vs static HTML. It pays the
browser-render cost (typically 5–30× slower than http, ~150–300 MB RAM
per concurrent render) for the safety of always rendering with a real
JS-capable browser.

When this is the right choice:
  * You're indexing a new corpus and haven't yet learned its URL shapes
  * Most of the corpus is JS-rendered (e.g. modern doc sites, app dashboards)
  * Browser cost is acceptable in your operational budget

When this is the wrong choice:
  * Most of the corpus is static HTML (use GenericProfile, leave browser off)
  * You have mixed content and know which URL patterns need browser
    (author a SiteProfile subclass with explicit ``requires_browser``
    rules — see ``ato.py`` for the reference pattern)

Everything else is inherited from the base SiteProfile defaults:
  * Tier classification → LIVING for every URL
  * Audience → ``"general"``
  * No facts, no boilerplate stripping, no section taxonomy, no excludes

Operators can compose this with the ``[browser]`` config section to tune
concurrency, timeouts, init-scripts, etc. without writing any Python.
"""

from . import SiteProfile


class GenericBrowserProfile(SiteProfile):
    """SiteProfile that routes every URL through the browser path.

    Inherits every other behavior from the base ``SiteProfile`` (which
    matches ``GenericProfile`` — no facts, no excludes, LIVING tier for
    everything). The only override is :meth:`requires_browser` returning
    ``True`` unconditionally.
    """

    name = "generic-browser"

    def requires_browser(self, url: str) -> bool:
        """Always route through the browser path.

        ``[browser].enabled`` in config still gates whether rendering
        actually happens — if disabled, plan.py short-circuits these
        URLs to :class:`~sift.decide.Decision.SKIPPED_BROWSER_DISABLED`
        (see ``route_to_browser_disabled``)."""
        return True

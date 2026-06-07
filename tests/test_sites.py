"""SiteProfile abstraction: loading, swapping, and pluggability."""

import pytest

from sift import classify
from sift.sites import (
    SiteProfile,
    current_profile,
    load_profile,
    set_profile,
)
from sift.sites.ato import ATOProfile
from sift.sites.generic import GenericProfile


@pytest.fixture(autouse=True)
def restore_profile():
    """Save/restore the profile so test ordering doesn't matter."""
    saved = current_profile()
    yield
    set_profile(saved)


class TestProfileLoader:
    def test_loads_by_import_path(self):
        p = load_profile("sift.sites.ato:ATOProfile")
        assert isinstance(p, ATOProfile)
        assert p.name == "ato"
        assert p.primary_host == "www.ato.gov.au"

    def test_loads_generic_profile(self):
        p = load_profile("sift.sites.generic:GenericProfile")
        assert isinstance(p, GenericProfile)
        assert p.name == "generic"

    def test_malformed_path_raises(self):
        with pytest.raises(ValueError, match="module:Class"):
            load_profile("no-colon-here")

    def test_unknown_module_raises(self):
        with pytest.raises(ImportError):
            load_profile("sift.sites.does_not_exist:Profile")

    def test_unknown_class_raises(self):
        with pytest.raises(ImportError, match="not found"):
            load_profile("sift.sites.ato:NonexistentProfile")

    def test_non_profile_class_rejected(self):
        # str isn't a SiteProfile subclass
        with pytest.raises(TypeError, match="SiteProfile"):
            load_profile("builtins:str")


class TestGenericProfileDefaults:
    """A bare GenericProfile yields the safe-but-empty defaults."""

    def setup_method(self):
        set_profile(GenericProfile())

    def test_everything_is_living(self):
        assert classify.classify_tier("https://x/whatever") == classify.Tier.LIVING
        # No year extraction, no FROZEN
        assert classify.classify_tier("https://x/2005/old-page") == classify.Tier.LIVING

    def test_no_facts(self):
        assert current_profile().facts_schemas == {}
        assert current_profile().facts_extractors == []

    def test_no_excludes(self):
        assert current_profile().default_excludes == ()

    def test_no_dynamic_patterns(self):
        assert current_profile().dynamic_patterns == ()


class TestGenericBrowserProfile:
    """GenericBrowserProfile routes every URL through the browser path.
    Everything else inherits from the base SiteProfile defaults (matches
    GenericProfile — no facts, no excludes, LIVING for all)."""

    def test_routes_everything_to_browser(self):
        from sift.sites.generic_browser import GenericBrowserProfile

        gbp = GenericBrowserProfile()
        assert gbp.requires_browser("https://example.com/") is True
        assert gbp.requires_browser("https://example.com/api/v1/widgets") is True
        assert gbp.requires_browser("https://docs.example.com/foo/bar") is True

    def test_browser_config_returns_none(self):
        """None tells fetch.py to use the [browser] section defaults."""
        from sift.sites.generic_browser import GenericBrowserProfile

        assert GenericBrowserProfile().browser_config("https://x/y") is None

    def test_inherits_generic_safe_defaults(self):
        """Same shape as GenericProfile for everything except routing."""
        from sift.sites.generic_browser import GenericBrowserProfile

        gbp = GenericBrowserProfile()
        assert gbp.default_excludes == ()
        assert gbp.dynamic_patterns == ()
        assert gbp.facts_schemas == {}
        assert gbp.facts_extractors == []
        assert gbp.audience("https://x/y") == "general"
        assert gbp.parent_guide("https://x/y") is None

    def test_loadable_via_load_profile(self):
        from sift.sites import load_profile
        from sift.sites.generic_browser import GenericBrowserProfile

        p = load_profile("sift.sites.generic_browser:GenericBrowserProfile")
        assert isinstance(p, GenericBrowserProfile)


class TestAUGovProfile:
    """Generic AU gov profile — audience by URL section, default excludes
    for Drupal/CMS machinery, dynamic-pattern strips for boilerplate."""

    def setup_method(self):
        from sift.sites.augov import AUGovProfile
        set_profile(AUGovProfile())

    def test_loadable(self):
        from sift.sites.augov import AUGovProfile
        p = load_profile("sift.sites.augov:AUGovProfile")
        assert isinstance(p, AUGovProfile)
        assert p.name == "au-gov"

    def test_audience_individuals(self):
        for url in (
            "https://www.example.gov.au/individuals-and-families/topic",
            "https://www.example.gov.au/individuals/welfare",
            "https://www.example.gov.au/people/help",
        ):
            assert classify.audience(url) == "individuals", url

    def test_audience_businesses(self):
        for url in (
            "https://www.example.gov.au/businesses-and-organisations/foo",
            "https://www.example.gov.au/business/start",
            "https://www.example.gov.au/employers/super",
            "https://www.example.gov.au/industry/codes",
        ):
            assert classify.audience(url) == "businesses", url

    def test_audience_news(self):
        for url in (
            "https://www.example.gov.au/media-centre/release",
            "https://www.example.gov.au/news/today",
            "https://www.example.gov.au/announcements/2026",
        ):
            assert classify.audience(url) == "news", url

    def test_audience_about(self):
        assert classify.audience("https://www.example.gov.au/about-us/structure") == "about"
        assert classify.audience("https://www.example.gov.au/about") == "about"
        assert classify.audience("https://www.example.gov.au/governance/board") == "about"

    def test_audience_consultation(self):
        assert classify.audience("https://treasury.gov.au/consultation/c2026-01") == "consultation"
        assert classify.audience("https://treasury.gov.au/consultations/foo") == "consultation"

    def test_audience_resources(self):
        assert classify.audience("https://asic.gov.au/resources/regulatory-guides") == "resources"
        assert classify.audience("https://www.apra.gov.au/publications/quarterly") == "resources"

    def test_audience_legal(self):
        assert classify.audience("https://www.ato.gov.au/law/income-tax") == "legal"
        assert classify.audience("https://example.gov.au/legislation/act") == "legal"

    def test_audience_general_fallback(self):
        assert classify.audience("https://www.example.gov.au/random/page") == "general"
        assert classify.audience("https://www.example.gov.au/") == "general"

    def test_default_excludes_cover_cms_machinery(self):
        from sift.classify import compile_excludes, is_excluded
        excludes = compile_excludes(current_profile().default_excludes)
        # Drupal/CMS paths excluded
        for excluded in (
            "https://www.example.gov.au/sitemap.xml",
            "https://www.example.gov.au/search?q=tax",
            "https://www.example.gov.au/api/v1/items",
            "https://www.example.gov.au/admin/dashboard",
            "https://www.example.gov.au/user/login",
            "https://www.example.gov.au/system/files/file.pdf",
        ):
            assert is_excluded(excluded, excludes), excluded
        # Real content NOT excluded
        for kept in (
            "https://www.example.gov.au/individuals/something",
            "https://www.example.gov.au/business/start",
            "https://www.example.gov.au/news/release",
        ):
            assert not is_excluded(kept, excludes), kept

    def test_dynamic_patterns_strip_boilerplate(self):
        patterns = current_profile().dynamic_patterns
        text = (
            "Some content.\n"
            "Last reviewed: 15 May 2026\n"
            "More content.\n"
            "© Commonwealth of Australia 2026\n"
            "Page last updated 03/04/2026\n"
        )
        for p in patterns:
            text = p.sub("", text)
        # The rotating bits should be gone
        assert "15 May 2026" not in text
        assert "2026" not in text or "© Commonwealth" in text  # only year stripped
        assert "Last reviewed" not in text or "Last reviewed:" not in text
        # Real content preserved
        assert "Some content" in text
        assert "More content" in text

    def test_http_path_default(self):
        """AUGov is HTTP-only by default — operators override for SPAs."""
        assert current_profile().requires_browser("https://www.example.gov.au/x") is False


class TestSAGovProfile:
    """South Australian state government sites — extends AUGovProfile."""

    def setup_method(self):
        from sift.sites.augov import SAGovProfile
        set_profile(SAGovProfile())

    def test_loadable(self):
        from sift.sites.augov import SAGovProfile
        p = load_profile("sift.sites.augov:SAGovProfile")
        assert isinstance(p, SAGovProfile)
        assert p.name == "sa-gov"

    def test_sa_specific_audience_by_host(self):
        """Host-driven labels for SA-specific agencies."""
        assert classify.audience("https://www.revenuesa.sa.gov.au/services") == "tax"
        assert classify.audience("https://www.sahealth.sa.gov.au/wps/wcm/connect/x") == "health"
        assert classify.audience("https://www.police.sa.gov.au/about-us") == "police"
        assert classify.audience("https://plan.sa.gov.au/code-amendments/foo") == "planning"
        assert classify.audience("https://plan.sa.gov.au/development-applications/x") == "planning"

    def test_sa_path_driven_audience_on_master_portal(self):
        """When host is sa.gov.au, path-based patterns kick in."""
        assert classify.audience("https://www.sa.gov.au/revenue/payroll-tax") == "tax"
        assert classify.audience("https://www.sa.gov.au/code-amendments/proposed") == "planning"

    def test_inherits_augov_audience(self):
        assert classify.audience("https://www.sa.gov.au/individuals/topic") == "individuals"
        assert classify.audience("https://www.sa.gov.au/about-us/structure") == "about"

    def test_inherits_augov_excludes(self):
        from sift.classify import compile_excludes, is_excluded
        excludes = compile_excludes(current_profile().default_excludes)
        assert is_excluded("https://www.sa.gov.au/sitemap.xml", excludes)
        assert is_excluded("https://www.sa.gov.au/admin/dashboard", excludes)
        assert not is_excluded("https://www.sa.gov.au/individuals/x", excludes)


class TestATOProfileBehavior:
    """The ATOProfile reproduces every ATO-specific behavior."""

    def setup_method(self):
        set_profile(ATOProfile())

    def test_audience_map_intact(self):
        assert classify.audience("https://www.ato.gov.au/individuals-and-families/x") == "individuals"
        assert classify.audience("https://www.ato.gov.au/businesses-and-organisations/y") == "businesses"
        assert classify.audience("https://www.ato.gov.au/forms-and-instructions/z") == "forms"
        assert classify.audience("https://www.ato.gov.au/unknown-section/x") == "general"

    def test_frozen_for_past_year(self):
        # past FY (year < CURRENT_FY_START_YEAR=2025) → FROZEN
        u = "https://www.ato.gov.au/forms-and-instructions/foreign-income-2005/p1"
        assert classify.classify_tier(u) == classify.Tier.FROZEN

    def test_news_tier(self):
        u = "https://www.ato.gov.au/media-centre/some-press-release"
        assert classify.classify_tier(u) == classify.Tier.NEWS

    def test_current_forms_tier(self):
        u = "https://www.ato.gov.au/forms-and-instructions/individual-tax-return-2025-instructions"
        assert classify.classify_tier(u) == classify.Tier.CURRENT_FORMS

    def test_parent_guide_for_forms_pages(self):
        u = "https://www.ato.gov.au/forms-and-instructions/individual-tax-return-2025-instructions/p1"
        assert classify.parent_guide(u) == "individual-tax-return-2025-instructions"

    def test_parent_guide_none_for_non_forms(self):
        u = "https://www.ato.gov.au/individuals-and-families/your-tax-return"
        assert classify.parent_guide(u) is None

    def test_fy_year_extraction(self):
        u = "https://www.ato.gov.au/forms-and-instructions/foreign-income-2005/p1"
        assert classify.fy_years(u) == ["2005-06"]

    def test_excludes_present(self):
        excludes = current_profile().default_excludes
        # Smoke check — the ATO excludes contain the canonical patterns
        assert any("/sitemap" in p for p in excludes)
        assert any("/api/" in p for p in excludes)
        # /single-page-applications/ is intentionally NOT excluded — routed
        # through the browser path via profile.requires_browser() instead.
        assert not any("single-page-applications" in p for p in excludes)

    def test_spa_requires_browser(self):
        """ATO Legal DB SPA opts into browser rendering rather than being skipped."""
        sp = current_profile()
        assert sp.requires_browser(
            "https://www.ato.gov.au/single-page-applications/legaldatabase"
        ) is True
        assert sp.requires_browser(
            "https://www.ato.gov.au/individuals-and-families/your-tax-return"
        ) is False

    def test_dynamic_patterns_present(self):
        patterns = current_profile().dynamic_patterns
        # ATO has 6 dynamic patterns (Last modified, QC code, Commonwealth ©, etc.)
        assert len(patterns) >= 5

    def test_facts_schemas_present(self):
        schemas = current_profile().facts_schemas
        assert "ato-rate-table-v1" in schemas
        assert schemas["ato-rate-table-v1"]["$id"] == "ato-rate-table-v1"

    def test_facts_extractors_wired(self):
        ext = current_profile().facts_extractors
        assert len(ext) >= 1
        matcher, fn = ext[0]
        # Spot-check: the matcher recognizes the canonical resident-rates URL
        assert matcher("https://www.ato.gov.au/tax-rates-and-codes/tax-rates-australian-residents")

    def test_section_order_has_nine_curated_sections(self):
        section_order = current_profile().section_order
        sections = {seg for (seg, _, _) in section_order}
        assert "individuals-and-families" in sections
        assert "forms-and-instructions" in sections
        assert len(section_order) == 9


class TestProfileSwap:
    """Profile changes take effect immediately for delegated functions."""

    def test_classify_tier_follows_profile(self):
        set_profile(GenericProfile())
        u = "https://x/forms-and-instructions/2005/old"
        # Generic → LIVING for everything
        assert classify.classify_tier(u) == classify.Tier.LIVING

        set_profile(ATOProfile())
        # ATO → FROZEN for past-year forms
        u_ato = "https://www.ato.gov.au/forms-and-instructions/foreign-income-2005/p1"
        assert classify.classify_tier(u_ato) == classify.Tier.FROZEN


class CustomTestProfile(SiteProfile):
    """A synthetic profile used to verify the abstraction is pluggable."""
    name = "custom-test"

    def classify_tier(self, url, current_year_start):
        # Toy rule: URLs containing /foo/ are NEWS
        if "/foo/" in url:
            return "NEWS"
        return "LIVING"

    def audience(self, url):
        return "custom-audience"


class TestCustomProfile:
    def test_custom_profile_works(self):
        set_profile(CustomTestProfile())
        assert classify.classify_tier("https://x/foo/bar") == classify.Tier.NEWS
        assert classify.classify_tier("https://x/baz/quux") == classify.Tier.LIVING
        assert classify.audience("https://x/anything") == "custom-audience"


# ===========================================================================
# Popular-site reference profiles: Stripe Docs, MDN, Python Docs
# ===========================================================================


class TestStripeDocsProfile:
    """Reference profile for docs.stripe.com — commercial SaaS API docs."""

    def setup_method(self):
        from sift.sites.stripe import StripeDocsProfile
        set_profile(StripeDocsProfile())

    def test_loadable(self):
        from sift.sites.stripe import StripeDocsProfile
        p = load_profile("sift.sites.stripe:StripeDocsProfile")
        assert isinstance(p, StripeDocsProfile)
        assert p.name == "stripe-docs"
        assert p.primary_host == "docs.stripe.com"

    def test_audience_by_top_level_topic(self):
        assert classify.audience("https://docs.stripe.com/api/customers/object") == "api-reference"
        assert classify.audience("https://docs.stripe.com/payments/payment-intents") == "payments"
        assert classify.audience("https://docs.stripe.com/billing/subscriptions") == "billing"
        assert classify.audience("https://docs.stripe.com/connect/accounts") == "connect"
        assert classify.audience("https://docs.stripe.com/checkout/quickstart") == "checkout"
        assert classify.audience("https://docs.stripe.com/sdks/python") == "sdks"
        # Unknown topic → dev fallback
        assert classify.audience("https://docs.stripe.com/some-new-product/x") == "developers"
        # Empty path → fallback
        assert classify.audience("https://docs.stripe.com/") == "developers"

    def test_excludes_skip_marketing_paths(self):
        from sift.classify import compile_excludes, is_excluded
        excludes = compile_excludes(current_profile().default_excludes)
        # Marketing / non-docs paths excluded
        assert is_excluded("https://docs.stripe.com/blog/posts/a", excludes)
        assert is_excluded("https://docs.stripe.com/jobs/engineering", excludes)
        assert is_excluded("https://docs.stripe.com/customers/case-studies", excludes)
        assert is_excluded("https://docs.stripe.com/legal/terms", excludes)
        # Real docs URLs NOT excluded
        assert not is_excluded("https://docs.stripe.com/api/customers", excludes)
        assert not is_excluded("https://docs.stripe.com/payments/cards", excludes)

    def test_default_tier_is_living(self):
        # Stripe doesn't version-tag URLs; everything LIVING
        assert classify.classify_tier("https://docs.stripe.com/api/customers") == classify.Tier.LIVING
        assert classify.classify_tier("https://docs.stripe.com/payments") == classify.Tier.LIVING

    def test_http_path_not_browser(self):
        """Stripe ships SSR docs; no browser required."""
        assert current_profile().requires_browser("https://docs.stripe.com/api/customers") is False

    def test_section_order_present(self):
        sections = current_profile().section_order
        assert ("api", "api-reference", "API Reference") in sections
        assert len(sections) >= 10  # broad coverage


class TestMDNProfile:
    """Reference profile for developer.mozilla.org — community web reference."""

    def setup_method(self):
        from sift.sites.mdn import MDNProfile
        set_profile(MDNProfile())

    def test_loadable(self):
        from sift.sites.mdn import MDNProfile
        p = load_profile("sift.sites.mdn:MDNProfile")
        assert isinstance(p, MDNProfile)
        assert p.name == "mdn"
        assert p.primary_host == "developer.mozilla.org"

    def test_audience_by_web_topic(self):
        base = "https://developer.mozilla.org/en-US/docs/Web"
        assert classify.audience(f"{base}/JavaScript/Reference/Operators") == "javascript"
        assert classify.audience(f"{base}/CSS/grid-template-columns") == "css"
        assert classify.audience(f"{base}/HTML/Element/div") == "html"
        assert classify.audience(f"{base}/API/Fetch_API") == "web-api"
        assert classify.audience(f"{base}/HTTP/Headers") == "http"
        assert classify.audience(f"{base}/Accessibility/ARIA") == "accessibility"
        assert classify.audience(f"{base}/WebAssembly/Concepts") == "wasm"
        # Non-/Web/ → reference fallback
        assert classify.audience("https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons") == "reference"

    def test_excludes_non_english_locales(self):
        from sift.classify import compile_excludes, is_excluded
        excludes = compile_excludes(current_profile().default_excludes)
        # Other locales excluded by default
        assert is_excluded("https://developer.mozilla.org/fr/docs/Web/JavaScript", excludes)
        assert is_excluded("https://developer.mozilla.org/ja/docs/Web/CSS", excludes)
        assert is_excluded("https://developer.mozilla.org/es/docs/Web/HTML", excludes)
        # en-US NOT excluded
        assert not is_excluded("https://developer.mozilla.org/en-US/docs/Web/JavaScript", excludes)

    def test_excludes_marketing_paths(self):
        from sift.classify import compile_excludes, is_excluded
        excludes = compile_excludes(current_profile().default_excludes)
        assert is_excluded("https://developer.mozilla.org/plus/subscriptions", excludes)
        assert is_excluded("https://developer.mozilla.org/play/scratchpad", excludes)
        assert is_excluded("https://developer.mozilla.org/observatory/dashboard", excludes)

    def test_http_path_not_browser(self):
        """MDN's Yari serves complete HTML; no browser required."""
        sp = current_profile()
        assert sp.requires_browser(
            "https://developer.mozilla.org/en-US/docs/Web/JavaScript"
        ) is False

    def test_default_tier_is_living(self):
        # MDN doesn't have a stable version axis; everything LIVING
        u = "https://developer.mozilla.org/en-US/docs/Web/CSS/grid"
        assert classify.classify_tier(u) == classify.Tier.LIVING


class TestPythonDocsProfile:
    """Reference profile for docs.python.org — official language reference."""

    def setup_method(self):
        from sift.sites.python_docs import PythonDocsProfile
        set_profile(PythonDocsProfile())

    def test_loadable(self):
        from sift.sites.python_docs import PythonDocsProfile
        p = load_profile("sift.sites.python_docs:PythonDocsProfile")
        assert isinstance(p, PythonDocsProfile)
        assert p.name == "python-docs"
        assert p.primary_host == "docs.python.org"

    def test_tier_classification_version_aware(self):
        """Python 2.x → FROZEN (legacy); Python 3.x → LIVING."""
        # Python 3 current
        assert classify.classify_tier("https://docs.python.org/3/library/asyncio.html") == classify.Tier.LIVING
        assert classify.classify_tier("https://docs.python.org/3.13/whatsnew/3.13.html") == classify.Tier.LIVING
        assert classify.classify_tier("https://docs.python.org/3.12/library/json.html") == classify.Tier.LIVING
        # Python 2 legacy
        assert classify.classify_tier("https://docs.python.org/2/library/string.html") == classify.Tier.FROZEN
        assert classify.classify_tier("https://docs.python.org/2.7/tutorial/intro.html") == classify.Tier.FROZEN
        # Unversioned (e.g. root) — LIVING fallback
        assert classify.classify_tier("https://docs.python.org/") == classify.Tier.LIVING

    def test_audience_by_section(self):
        assert classify.audience("https://docs.python.org/3/library/asyncio.html") == "stdlib"
        assert classify.audience("https://docs.python.org/3/reference/datamodel.html") == "language-ref"
        assert classify.audience("https://docs.python.org/3/tutorial/classes.html") == "tutorial"
        assert classify.audience("https://docs.python.org/3/howto/logging.html") == "howto"
        assert classify.audience("https://docs.python.org/3/whatsnew/3.13.html") == "release-notes"
        assert classify.audience("https://docs.python.org/3/c-api/object.html") == "c-api"
        assert classify.audience("https://docs.python.org/3/faq/programming.html") == "faq"
        # Unknown section → reference
        assert classify.audience("https://docs.python.org/3/some-new-section/x") == "reference"

    def test_excludes_sphinx_machinery(self):
        from sift.classify import compile_excludes, is_excluded
        excludes = compile_excludes(current_profile().default_excludes)
        # Sphinx internals excluded
        assert is_excluded("https://docs.python.org/_sources/library/json.rst.txt", excludes)
        assert is_excluded("https://docs.python.org/_static/pygments.css", excludes)
        assert is_excluded("https://docs.python.org/objects.inv", excludes)
        assert is_excluded("https://docs.python.org/genindex-all.html", excludes)
        # Real docs NOT excluded
        assert not is_excluded("https://docs.python.org/3/library/json.html", excludes)
        assert not is_excluded("https://docs.python.org/3/tutorial/classes.html", excludes)

    def test_dev_branch_excluded(self):
        """/dev/ is bleeding-edge daily-changing; excluded by default."""
        from sift.classify import compile_excludes, is_excluded
        excludes = compile_excludes(current_profile().default_excludes)
        assert is_excluded("https://docs.python.org/dev/library/foo.html", excludes)

    def test_http_path_not_browser(self):
        """python.org is Sphinx static HTML; no browser required."""
        sp = current_profile()
        assert sp.requires_browser("https://docs.python.org/3/library/asyncio.html") is False

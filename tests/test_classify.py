"""Test the pure URL classifier against representative + adversarial cases."""

import json
from collections import Counter
from pathlib import Path

import pytest

from sift.classify import (
    Tier,
    canonicalize_url,
    classify_tier,
    is_malformed_url,
    parent_guide,
    safe_path_segments,
)


class TestCanonicalize:
    def test_strips_fragment(self):
        assert canonicalize_url("https://x.com/a#b") == "https://x.com/a"

    def test_lowers_host(self):
        assert canonicalize_url("https://WWW.X.COM/A") == "https://www.x.com/A"

    def test_strips_trailing_slash(self):
        assert canonicalize_url("https://x.com/a/") == "https://x.com/a"

    def test_keeps_root_slash(self):
        assert canonicalize_url("https://x.com/") == "https://x.com/"

    def test_sorts_query_params(self):
        assert (canonicalize_url("https://x.com/a?b=2&a=1") ==
                "https://x.com/a?a=1&b=2")

    def test_drops_tracking_params(self):
        u = "https://x.com/a?utm_source=g&utm_medium=cpc&gclid=xxx&keep=1"
        assert canonicalize_url(u) == "https://x.com/a?keep=1"

    def test_idempotent(self):
        u = "https://X.com/a/?utm_source=x&b=2&a=1#frag"
        once = canonicalize_url(u)
        twice = canonicalize_url(once)
        assert once == twice


class TestClassifyTier:
    @pytest.mark.parametrize("url,tier", [
        # FROZEN: past-FY year in URL
        ("https://www.ato.gov.au/forms-and-instructions/foreign-income-2005/p1", Tier.FROZEN),
        ("https://www.ato.gov.au/forms-and-instructions/capital-gains-tax-guide-2011", Tier.FROZEN),
        ("https://www.ato.gov.au/tax-rates-and-codes/2023-24-tax-rates", Tier.FROZEN),
        # CURRENT_FORMS: forms-and-instructions, current or future year
        ("https://www.ato.gov.au/forms-and-instructions/individual-tax-return-2025-instructions", Tier.CURRENT_FORMS),
        ("https://www.ato.gov.au/forms-and-instructions/individual-tax-return-2026-instructions", Tier.CURRENT_FORMS),
        ("https://www.ato.gov.au/forms-and-instructions/some-evergreen-form", Tier.CURRENT_FORMS),
        # NEWS
        ("https://www.ato.gov.au/media-centre/some-press-release", Tier.NEWS),
        # LIVING: everything else
        ("https://www.ato.gov.au/individuals-and-families/your-tax-return", Tier.LIVING),
        ("https://www.ato.gov.au/businesses-and-organisations/gst", Tier.LIVING),
        ("https://www.ato.gov.au/", Tier.LIVING),
    ])
    def test_tier(self, url, tier):
        assert classify_tier(url) == tier


class TestParentGuide:
    def test_forms_page(self):
        u = "https://www.ato.gov.au/forms-and-instructions/foreign-income-2005/p1"
        assert parent_guide(u) == "foreign-income-2005"

    def test_forms_root(self):
        u = "https://www.ato.gov.au/forms-and-instructions/foreign-income-2005"
        assert parent_guide(u) == "foreign-income-2005"

    def test_non_forms(self):
        u = "https://www.ato.gov.au/individuals-and-families/your-tax-return"
        assert parent_guide(u) is None

    def test_forms_root_index(self):
        assert parent_guide("https://www.ato.gov.au/forms-and-instructions/") is None


class TestSafePathSegments:
    def test_basic(self):
        assert safe_path_segments("https://x.com/a/b/c") == ["a", "b", "c"]

    def test_replaces_unsafe(self):
        # `+`, `%` get replaced
        out = safe_path_segments("https://x.com/a+b/c%20d")
        assert out == ["a_b", "c_20d"]

    def test_caps_segment_length(self):
        # POSIX PATH_MAX is the operational limit — each segment must stay
        # well under it. A 5000-char segment should be truncated to 200.
        long = "x" * 5000
        out = safe_path_segments(f"https://x.com/{long}")
        assert len(out) == 1
        assert len(out[0]) == 200


class TestIsMalformedUrl:
    def test_normal_url_is_fine(self):
        assert is_malformed_url("https://x.com/a/b/c") is False

    def test_long_url_above_2048_is_malformed(self):
        assert is_malformed_url("https://x.com/" + "a" * 3000) is True

    def test_concatenation_artifact_https(self):
        # Firecrawl /map sometimes joins multiple URLs with `%20`; this is the
        # shape that blew up Cornell LII (2918-char URL with embedded
        # `%20https:` markers).
        joined = ("https://www.law.cornell.edu/cfr/text/26/1.6011-1"
                  "%20https://www.law.cornell.edu/cfr/text/26/1.6012-2")
        assert is_malformed_url(joined) is True

    def test_concatenation_artifact_http(self):
        joined = "http://x/a%20http://x/b"
        assert is_malformed_url(joined) is True

    def test_url_at_boundary_is_fine(self):
        # Exactly 2048 chars — still fine. Anything *over* trips it.
        assert is_malformed_url("https://x.com/" + "a" * (2048 - len("https://x.com/"))) is False


# Sanity-check the real corpus matches our expected tier distribution.
class TestRealCorpus:
    @pytest.fixture
    def urls(self):
        p = Path(__file__).parent.parent / "www.ato.gov.au_.2026-05-24T02_52_49.199Z.json"
        if not p.exists():
            pytest.skip("URL dump not available")
        data = json.loads(p.read_text())
        return [link["url"] for link in data["links"]]

    def test_can_classify_all(self, urls):
        for u in urls:
            t = classify_tier(u)
            assert t in Tier

    def test_distribution_makes_sense(self, urls):
        counts = Counter(classify_tier(u).value for u in urls)
        # Forms-and-instructions dominates the corpus (~91%); most are historical -> FROZEN.
        assert counts.get("FROZEN", 0) > 1500
        # NEWS is the smallest tier
        assert counts.get("NEWS", 0) < 500
        # LIVING is the smallest *meaningful* tier
        assert counts.get("LIVING", 0) > 50

    def test_no_duplicates_after_canonicalize(self, urls):
        # Spec: canonicalization should be idempotent and produce a 1:1 mapping.
        canon = {canonicalize_url(u) for u in urls}
        # The dump itself should be roughly de-duped; we just check no canonical collisions.
        assert len(canon) >= len(urls) * 0.99

"""Markdown pass-through: the third extract path alongside HTML and PDF.

Some docs hosts serve a clean Markdown variant directly (Stripe's
``docs.stripe.com/<page>.md``, ``llms.txt``). Running the HTML extractor
(trafilatura) over Markdown mangles it, so a profile can declare such bodies
as ``"markdown"`` via ``SiteProfile.body_kind`` and the extract phase stores
them verbatim.

Detection lives in the profile layer (site-agnostic core), keyed on URL shape
and/or Content-Type — see ``SiteProfile.body_kind``.
"""

import pytest

from sift import CRAWLER_VERSION, paths
from sift.extract import (
    EXTRACTOR_VERSION_HTML,
    EXTRACTOR_VERSION_MD,
    EXTRACTOR_VERSION_PDF,
    extract_one,
    extract_passthrough_md,
)
from sift.fetch import FetchResult, sha256_hex, write_raw_blob
from sift.manifest import init_schema, now_utc, open_db
from sift.sites import SiteProfile, current_profile, set_profile
from sift.sites.stripe import StripeDocsProfile


@pytest.fixture(autouse=True)
def restore_profile():
    """Save/restore the active profile so set_profile() can't leak across tests."""
    saved = current_profile()
    yield
    set_profile(saved)


# ---- body_kind: base profile (Content-Type only) ---------------------------

class TestBodyKindBase:
    def setup_method(self):
        self.p = SiteProfile()

    @pytest.mark.parametrize("ctype,expected", [
        ("text/markdown", "markdown"),
        ("text/markdown; charset=utf-8", "markdown"),
        ("text/x-markdown", "markdown"),
        ("TEXT/MARKDOWN", "markdown"),          # case-insensitive
        ("text/plain", None),                    # ambiguous → NOT markdown
        ("text/plain; charset=utf-8", None),
        ("text/html; charset=utf-8", None),
        ("application/pdf", None),
        (None, None),
        ("", None),
    ])
    def test_content_type_routing(self, ctype, expected):
        assert self.p.body_kind("https://x.com/page", content_type=ctype) == expected

    def test_url_shape_is_ignored_by_base(self):
        # The base profile does NOT treat a `.md` URL as markdown — that's a
        # per-site opt-in. Only Content-Type drives the base decision.
        assert self.p.body_kind("https://x.com/page.md", content_type="text/plain") is None


# ---- body_kind: Stripe profile (URL shape, since Stripe mislabels as plain) -

class TestBodyKindStripe:
    def setup_method(self):
        self.p = StripeDocsProfile()

    def test_md_url_with_text_plain_is_markdown(self):
        # The real Stripe case: `.md` endpoint served as text/plain.
        assert self.p.body_kind(
            "https://docs.stripe.com/testing.md",
            content_type="text/plain; charset=utf-8",
        ) == "markdown"

    def test_md_url_without_content_type_is_markdown(self):
        # Durable across the re-extract path (no Content-Type persisted).
        assert self.p.body_kind("https://docs.stripe.com/api/errors.md") == "markdown"

    def test_md_url_with_query_string(self):
        assert self.p.body_kind(
            "https://docs.stripe.com/testing.md?locale=en"
        ) == "markdown"

    def test_html_url_defers_to_core(self):
        # No `.md`, no markdown Content-Type → None → core sniff → HTML path.
        assert self.p.body_kind(
            "https://docs.stripe.com/payments",
            content_type="text/html; charset=utf-8",
        ) is None

    def test_inherits_content_type_rule_from_base(self):
        # A non-`.md` URL that DOES carry text/markdown still classifies via super().
        assert self.p.body_kind(
            "https://docs.stripe.com/some/guide",
            content_type="text/markdown",
        ) == "markdown"


# ---- extract_passthrough_md -------------------------------------------------

class TestExtractPassthroughMd:
    def test_passes_markdown_through_verbatim(self):
        src = b"# Testing\n\nUse card 4242 4242 4242 4242 to simulate a charge.\n"
        md, title = extract_passthrough_md(src, "https://x/testing.md")
        assert "4242 4242 4242 4242" in md
        assert title == "Testing"

    def test_strips_outer_whitespace(self):
        md, _ = extract_passthrough_md(b"\n\n  # Heading\n\nbody\n\n", "https://x/a.md")
        assert md.startswith("# Heading")
        assert md.endswith("body")

    def test_title_from_first_h1_only(self):
        src = b"# First\n\ntext\n\n# Second\n"
        _, title = extract_passthrough_md(src, "https://x/a.md")
        assert title == "First"

    def test_no_heading_gives_null_title(self):
        md, title = extract_passthrough_md(b"just a paragraph, no heading\n", "https://x/a.md")
        assert md == "just a paragraph, no heading"
        assert title is None

    def test_empty_body_returns_none(self):
        assert extract_passthrough_md(b"", "https://x/a.md") == (None, None)
        assert extract_passthrough_md(b"   \n\t\n", "https://x/a.md") == (None, None)

    def test_invalid_utf8_does_not_crash(self):
        # Decoded with errors="replace" — deterministic, never raises.
        md, _ = extract_passthrough_md(b"# T\n\n\xff\xfe bad bytes", "https://x/a.md")
        assert md is not None
        assert md.startswith("# T")


# ---- end-to-end dispatch through extract_one --------------------------------

class TestMarkdownDispatch:
    def test_md_url_routes_to_passthrough(self, tmp_path):
        set_profile(StripeDocsProfile())
        root = tmp_path
        conn = open_db(paths.manifest_path(root))
        init_schema(conn)

        url = "https://docs.stripe.com/testing.md"
        # Distinctive markdown that the HTML extractor would NOT reproduce: raw
        # markdown link syntax survives pass-through but trafilatura would
        # rewrite or drop it.
        src = (
            b"# Testing\n\n"
            b"Simulate payments with [test cards](https://docs.stripe.com/testing.md#cards) "
            b"like 4242 4242 4242 4242.\n"
        )
        raw_hash = sha256_hex(src)
        write_raw_blob(root, raw_hash, src)

        fr = FetchResult(
            url=url, decision="FETCH", status=200, etag=None, last_modified=None,
            raw_hash=raw_hash, raw_bytes=len(src), fetched_at=now_utc(), error=None,
            content_type="text/plain; charset=utf-8",
        )
        res = extract_one(fr, root=root, run_id="t-md", conn=conn,
                          crawler_version=CRAWLER_VERSION)

        assert res.ok
        assert res.reason == "new-content-md"
        assert res.extractor_version == EXTRACTOR_VERSION_MD
        assert res.content_hash is not None

        body = paths.md_path(root, "t-md", url).read_text()
        assert "4242 4242 4242 4242" in body
        assert "[test cards](https://docs.stripe.com/testing.md#cards)" in body
        assert "# Testing {#testing}" in body  # heading anchors still injected

    def test_md_routing_independent_of_text_plain_content_type(self, tmp_path):
        """Even though Content-Type is text/plain (not text/markdown), the
        Stripe profile's URL rule routes it to markdown — the whole point."""
        set_profile(StripeDocsProfile())
        root = tmp_path
        conn = open_db(paths.manifest_path(root))
        init_schema(conn)

        url = "https://docs.stripe.com/refunds.md"
        src = b"# Refunds\n\nRefund a charge in part or in full.\n"
        raw_hash = sha256_hex(src)
        write_raw_blob(root, raw_hash, src)
        fr = FetchResult(
            url=url, decision="FETCH", status=200, etag=None, last_modified=None,
            raw_hash=raw_hash, raw_bytes=len(src), fetched_at=now_utc(), error=None,
            content_type="text/plain",
        )
        res = extract_one(fr, root=root, run_id="t-md-2", conn=conn,
                          crawler_version=CRAWLER_VERSION)
        assert res.ok and res.extractor_version == EXTRACTOR_VERSION_MD


# ---- version identity -------------------------------------------------------

class TestExtractorVersionMd:
    def test_md_version_distinct_from_html_and_pdf(self):
        assert EXTRACTOR_VERSION_MD != EXTRACTOR_VERSION_HTML
        assert EXTRACTOR_VERSION_MD != EXTRACTOR_VERSION_PDF
        assert "passthrough" in EXTRACTOR_VERSION_MD

"""Tests for the extraction strategy seam (Stage 1 of the extraction
generalization).

These cover the pipeline structure directly — primary selection order,
the applicability predicates, and the enricher registry — rather than
only exercising it indirectly through ``extract_one``. The behavioral
equivalence to the pre-refactor inline pipeline is proven separately;
here we lock the seam's contract so Stage 2/3 can build on it safely.
"""
from __future__ import annotations

import pytest

from sift.extract import (
    EXTRACTOR_VERSION_HTML,
    EXTRACTOR_VERSION_JSON,
    EXTRACTOR_VERSION_MD,
    EXTRACTOR_VERSION_PDF,
    PRIMARY_STRATEGIES,
    _html_applies,
    _md_applies,
    _pdf_applies,
)
from sift.extract_strategy import (
    Enricher,
    ExtractInput,
    HTML_ENRICHERS,
    PrimaryStrategy,
    run_html_enrichers,
    select_primary,
)


def _inp(*, raw: bytes = b"<html></html>", url: str = "https://x.test/p",
         content_type=None, body_kind=None) -> ExtractInput:
    return ExtractInput(raw=raw, url=url, content_type=content_type,
                        body_kind=body_kind)


# ---- Primary registry shape ----------------------------------------------

class TestRegistryShape:
    def test_primaries_in_priority_order(self):
        names = [s.name for s in PRIMARY_STRATEGIES]
        assert names == ["markdown-passthrough", "pdf", "json-api", "html-trafilatura"]

    def test_versions_wired_to_constants(self):
        by_name = {s.name: s for s in PRIMARY_STRATEGIES}
        assert by_name["markdown-passthrough"].version == EXTRACTOR_VERSION_MD
        assert by_name["pdf"].version == EXTRACTOR_VERSION_PDF
        assert by_name["json-api"].version == EXTRACTOR_VERSION_JSON
        assert by_name["html-trafilatura"].version == EXTRACTOR_VERSION_HTML

    def test_kinds_match_reason_labels(self):
        by_name = {s.name: s for s in PRIMARY_STRATEGIES}
        assert by_name["markdown-passthrough"].kind == "md"
        assert by_name["pdf"].kind == "pdf"
        assert by_name["json-api"].kind == "json"
        assert by_name["html-trafilatura"].kind == "html"

    def test_html_is_terminal_fallback(self):
        # The last primary must be the always-applicable HTML one so
        # select_primary never falls through to nothing.
        assert PRIMARY_STRATEGIES[-1].name == "html-trafilatura"
        assert PRIMARY_STRATEGIES[-1].applies(_inp(body_kind="anything")) is True


# ---- Applicability predicates --------------------------------------------

class TestPredicates:
    def test_md_applies_only_on_markdown_kind(self):
        assert _md_applies(_inp(body_kind="markdown")) is True
        assert _md_applies(_inp(body_kind="html")) is False
        assert _md_applies(_inp(body_kind=None)) is False

    def test_pdf_applies_on_explicit_kind(self):
        assert _pdf_applies(_inp(body_kind="pdf")) is True

    def test_pdf_applies_on_magic_bytes_when_deferred(self):
        assert _pdf_applies(_inp(raw=b"%PDF-1.7 ...", body_kind=None)) is True

    def test_pdf_applies_on_pdf_url_when_deferred(self):
        assert _pdf_applies(_inp(url="https://x.test/doc.pdf", body_kind=None)) is True

    def test_pdf_does_not_apply_when_profile_says_markdown(self):
        # A profile classification wins over the sniff — markdown kind
        # means markdown even if the bytes look PDF-ish.
        assert _pdf_applies(_inp(raw=b"%PDF-", body_kind="markdown")) is False

    def test_html_always_applies(self):
        assert _html_applies(_inp()) is True


# ---- select_primary dispatch ---------------------------------------------

class TestSelectPrimary:
    def test_markdown_kind_selects_passthrough(self):
        s = select_primary(_inp(body_kind="markdown"), PRIMARY_STRATEGIES)
        assert s.name == "markdown-passthrough"

    def test_pdf_kind_selects_pdf(self):
        s = select_primary(_inp(body_kind="pdf"), PRIMARY_STRATEGIES)
        assert s.name == "pdf"

    def test_deferred_html_selects_html(self):
        s = select_primary(_inp(body_kind=None, raw=b"<html><body>hi</body></html>"),
                           PRIMARY_STRATEGIES)
        assert s.name == "html-trafilatura"

    def test_pdf_sniff_beats_html_fallback(self):
        s = select_primary(_inp(raw=b"%PDF-1.4", body_kind=None), PRIMARY_STRATEGIES)
        assert s.name == "pdf"

    def test_first_applicable_wins_on_ordering(self):
        # Construct a registry where two predicates match; the first
        # must win.
        a = PrimaryStrategy("a", "a", "va", lambda i: True,
                            lambda raw, url: ("A", None))
        b = PrimaryStrategy("b", "b", "vb", lambda i: True,
                            lambda raw, url: ("B", None))
        assert select_primary(_inp(), (a, b)).name == "a"

    def test_falls_back_to_last_if_none_match(self):
        a = PrimaryStrategy("a", "a", "va", lambda i: False,
                            lambda raw, url: (None, None))
        b = PrimaryStrategy("b", "b", "vb", lambda i: False,
                            lambda raw, url: (None, None))
        # Neither matches — defensive fallback returns the last entry.
        assert select_primary(_inp(), (a, b)).name == "b"


# ---- Enricher registry ----------------------------------------------------

class TestEnricherRegistry:
    def test_two_enrichers_in_order(self):
        names = [e.name for e in HTML_ENRICHERS]
        assert names == ["code-blocks", "next-rsc"]

    def test_run_html_enrichers_threads_and_sums(self):
        # Synthetic enrichers that each append a marker + report a count;
        # confirm they run in order and totals sum.
        calls = []

        def e1(md, raw):
            calls.append("e1")
            return md + "\n<e1>", 2

        def e2(md, raw):
            calls.append("e2")
            # e2 sees e1's output — proves threading
            assert md.endswith("<e1>")
            return md + "\n<e2>", 3

        import sift.extract_strategy as mod
        original = mod.HTML_ENRICHERS
        mod.HTML_ENRICHERS = (Enricher("e1", e1), Enricher("e2", e2))
        try:
            out, total = run_html_enrichers("base", b"<html></html>")
        finally:
            mod.HTML_ENRICHERS = original
        assert calls == ["e1", "e2"]
        assert total == 5
        assert out == "base\n<e1>\n<e2>"

    def test_run_html_enrichers_real_noop_on_plain_html(self):
        # Real registry: plain HTML with no code/RSC → nothing appended.
        out, total = run_html_enrichers("# Doc\n\nProse.\n",
                                        b"<html><body><p>Prose.</p></body></html>")
        assert total == 0


# ---- ExtractInput is pure data -------------------------------------------

class TestExtractInput:
    def test_frozen(self):
        inp = _inp()
        with pytest.raises(Exception):
            inp.url = "mutated"  # type: ignore[misc]

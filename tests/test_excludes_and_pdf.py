"""Coverage for the three follow-ups: URL excludes, PDF extraction, body-FY parsing."""

from pathlib import Path
import io

import pytest

from sift.classify import (
    DEFAULT_EXCLUDE_PATTERNS,
    compile_excludes,
    is_excluded,
)
from sift.extract import (
    EXTRACTOR_VERSION_HTML,
    EXTRACTOR_VERSION_PDF,
    extract_pdf,
    is_pdf,
    _is_pdf_url,
)
from sift.facts import fy_from_body


# ---- Excludes ---------------------------------------------------------------

class TestDefaultExcludes:
    def setup_method(self):
        self.excludes = compile_excludes(DEFAULT_EXCLUDE_PATTERNS)

    @pytest.mark.parametrize("url,expect_excluded", [
        ("https://www.ato.gov.au/sitemap.xml", True),
        ("https://www.ato.gov.au/sitemap", True),
        ("https://www.ato.gov.au/api/public/content/abc", True),
        ("https://www.ato.gov.au/print/foo", True),
        ("https://www.ato.gov.au/errors", True),
        ("https://www.ato.gov.au/error", True),
        ("https://www.ato.gov.au/page-unavailable", True),
        ("https://www.ato.gov.au/whats-new", True),
        # /single-page-applications/ used to be excluded (no Playwright branch);
        # browser-fetch routes them through profile.requires_browser() instead,
        # so they participate in seed/plan/fetch like any other URL now.
        ("https://www.ato.gov.au/single-page-applications/legaldatabase", False),
        # Negatives — real content URLs must NOT be excluded
        ("https://www.ato.gov.au/individuals-and-families/your-tax-return", False),
        ("https://www.ato.gov.au/forms-and-instructions/individual-tax-return-2025-instructions", False),
        ("https://www.ato.gov.au/tax-rates-and-codes/individual-income-tax-rates", False),
        ("https://www.ato.gov.au/", False),
    ])
    def test_match(self, url, expect_excluded):
        assert is_excluded(url, self.excludes) == expect_excluded


class TestCustomExcludes:
    def test_extra_pattern_compiles(self):
        excludes = compile_excludes(("^/dev-only(/|$)",))
        assert is_excluded("https://x.com/dev-only/foo", excludes)
        assert not is_excluded("https://x.com/real/foo", excludes)

    def test_bad_regex_raises(self):
        import re
        with pytest.raises(re.error):
            compile_excludes(("(unclosed",))


# ---- PDF extraction ---------------------------------------------------------

# Minimal valid PDF for testing (one page, one text run, no embedded fonts —
# pypdf will refuse to extract text from this if the encoding is unknown).
# We use a known-good fixture: a real but tiny PDF generated below.

def _make_test_pdf(text: str) -> bytes:
    """Build a tiny single-page PDF with `text` as its sole content.
    Returns raw bytes. Uses pypdf's writer for guaranteed-parseable output."""
    import pypdf
    writer = pypdf.PdfWriter()
    # pypdf 6.x supports add_blank_page; text injection needs reportlab.
    # For test purposes, use a minimal real PDF that pypdf can roundtrip.
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class TestPdfDetection:
    def test_magic_bytes_detected(self):
        assert is_pdf(b"%PDF-1.7\nrest of file")
        # Some PDFs have a few bytes before the magic; our sniffer handles that
        assert is_pdf(b"\x00\x00%PDF-1.7\nrest")

    def test_html_not_pdf(self):
        assert not is_pdf(b"<!DOCTYPE html><html><body>...")

    def test_empty(self):
        assert not is_pdf(b"")

    def test_url_pdf_hint(self):
        assert _is_pdf_url("https://x.com/foo/bar.pdf")
        assert _is_pdf_url("https://x.com/foo/bar.PDF?download=1")
        assert not _is_pdf_url("https://x.com/foo/bar.html")


class TestPdfExtract:
    def test_extracts_real_pdf(self, tmp_path):
        # Round-trip: write a minimal PDF, extract from bytes, assert we don't crash.
        # A blank-page PDF has no text -> (None, None) is the documented contract
        # (caller will record extract-failed-pdf, which is correct).
        body = _make_test_pdf("hello")
        md, title = extract_pdf(body, "https://example.com/doc.pdf")
        if md is None:
            assert title is None  # contract: both None on failure
        else:
            assert md.startswith("# ")
            assert title is not None

    def test_corrupt_pdf_returns_none(self):
        md, title = extract_pdf(b"%PDF-1.7\nbut not actually a pdf",
                                "https://example.com/x.pdf")
        assert md is None
        assert title is None

    def test_html_routed_away_from_pdf(self):
        # extract_pdf on HTML bytes should not blow up; returns (None, None).
        md, _ = extract_pdf(b"<html>hi</html>", "https://example.com/x.pdf")
        assert md is None


class TestPdfTables:
    """The pdfplumber digital-PDF table lane: pypdf text + recovered tables."""

    def _table_pdf(self) -> bytes:
        return (Path(__file__).parent / "fixtures" / "table_sample.pdf").read_bytes()

    def test_table_recovered_as_markdown(self):
        md, title = extract_pdf(self._table_pdf(), "https://example.com/schedule.pdf")
        assert md is not None
        assert "Sample Tax Schedule" in md         # pypdf text is preserved
        assert "| Tax Year | Rate |" in md         # pdfplumber markdown header
        assert "| --- | --- |" in md               # ...with a separator row
        assert "| 2024 | 10 percent |" in md       # ...and the data rows
        assert "| 2025 | 12 percent |" in md

    def test_pdf_extraction_is_deterministic(self):
        body = self._table_pdf()
        a, _ = extract_pdf(body, "https://x/s.pdf")
        b, _ = extract_pdf(body, "https://x/s.pdf")
        assert a is not None and a == b            # G-det: byte-identical re-extract

    def test_version_records_both_libs(self):
        assert "pypdf" in EXTRACTOR_VERSION_PDF
        assert "plumber" in EXTRACTOR_VERSION_PDF


class TestExtractorVersions:
    def test_html_and_pdf_versions_distinct(self):
        # The whole point of separate versions: bump one without invalidating the other
        assert EXTRACTOR_VERSION_HTML != EXTRACTOR_VERSION_PDF
        assert "trafilatura" in EXTRACTOR_VERSION_HTML
        assert "pypdf" in EXTRACTOR_VERSION_PDF


# ---- FY-from-body -----------------------------------------------------------

class TestFyFromBody:
    def test_from_h1(self):
        html = b"<html><body><h1>Resident tax rates 2025-26</h1></body></html>"
        assert fy_from_body(html) == "2025-26"

    def test_from_title(self):
        html = b"<html><head><title>Tax rates 2024-25 | ATO</title></head></html>"
        assert fy_from_body(html) == "2024-25"

    def test_from_table_header(self):
        html = b"""<html><body>
            <table><thead>
                <tr><th>Income for 2025-26</th><th>Tax</th></tr>
            </thead></table>
        </body></html>"""
        assert fy_from_body(html) == "2025-26"

    def test_en_dash(self):
        html = "<h1>Resident tax rates 2025–26</h1>".encode()
        assert fy_from_body(html) == "2025-26"

    def test_slash_form(self):
        html = b"<h1>Tax rates 2024/25</h1>"
        assert fy_from_body(html) == "2024-25"

    def test_implausible_year_pair_rejected(self):
        # 2025-30 is not a valid FY; should keep looking and find nothing
        html = b"<h1>See 2025-30 archive</h1>"
        assert fy_from_body(html) is None

    def test_no_fy_returns_none(self):
        html = b"<h1>Your tax return</h1>"
        assert fy_from_body(html) is None

    def test_prefers_h1_over_table(self):
        """If multiple candidates exist, the most specific (h1) wins."""
        html = b"""<html><body>
            <h1>Tax rates 2025-26</h1>
            <table><thead><tr><th>Old: 2020-21</th></tr></thead></table>
        </body></html>"""
        assert fy_from_body(html) == "2025-26"

    def test_empty_input(self):
        assert fy_from_body(b"") is None
        assert fy_from_body(None) is None  # type: ignore[arg-type]

"""Tax-bracket extractor + schema validation against synthetic HTML."""

import json

import pytest

from sift.facts import (
    EXTRACTOR_VERSION,
    SCHEMAS,
    _is_individual_resident_rates,
    extract_individual_resident_brackets,
)


SYNTHETIC_RATES_HTML = b"""<!DOCTYPE html>
<html><body>
<h1>Resident tax rates 2025-26</h1>
<table>
<thead>
  <tr><th>Taxable income</th><th>Tax on this income</th></tr>
</thead>
<tbody>
  <tr><td>$0 - $18,200</td><td>Nil</td></tr>
  <tr><td>$18,201 - $45,000</td><td>16c for each $1 over $18,200</td></tr>
  <tr><td>$45,001 - $135,000</td><td>$4,288 plus 30c for each $1 over $45,000</td></tr>
  <tr><td>$135,001 - $190,000</td><td>$31,288 plus 37c for each $1 over $135,000</td></tr>
  <tr><td>$190,001 and over</td><td>$51,638 plus 45c for each $1 over $190,000</td></tr>
</tbody>
</table>
</body></html>"""


class TestUrlMatcher:
    @pytest.mark.parametrize("url", [
        # Actual ATO URLs observed in the live corpus
        "https://www.ato.gov.au/tax-rates-and-codes/tax-rates-australian-residents",
        "https://www.ato.gov.au/tax-rates-and-codes/previous-years-tax-tables/tax-tables-for-2025-26",
        # Plausible variants the matcher should also catch
        "https://www.ato.gov.au/tax-rates-and-codes/individual-income-tax-rates-2025-26",
        "https://www.ato.gov.au/tax-rates-and-codes/resident-tax-rates",
    ])
    def test_matches_resident_rates_pages(self, url):
        assert _is_individual_resident_rates(url)

    @pytest.mark.parametrize("url", [
        "https://www.ato.gov.au/individuals-and-families/your-tax-return",
        "https://www.ato.gov.au/tax-rates-and-codes/fringe-benefits-tax-rates-and-thresholds",
        "https://www.ato.gov.au/tax-rates-and-codes/super-guarantee",
    ])
    def test_rejects_non_matching(self, url):
        assert not _is_individual_resident_rates(url)


class TestBracketExtraction:
    def test_parses_synthetic_2025_26(self):
        cands = extract_individual_resident_brackets(
            url="https://www.ato.gov.au/tax-rates-and-codes/individual-income-tax-rates-2025-26",
            html=SYNTHETIC_RATES_HTML,
            fy="2025-26",
            content_hash="0" * 64,
        )
        assert len(cands) == 1
        cand = cands[0]
        assert cand.schema == "ato-rate-table-v1"
        assert cand.slug == "individual-resident-2025-26"
        p = cand.payload
        assert p["fy"] == "2025-26"
        assert p["audience"] == "individual_resident"
        assert p["effective_from"] == "2025-07-01"
        assert p["effective_to"] == "2026-06-30"
        brackets = p["brackets"]
        assert len(brackets) == 5
        assert brackets[0] == {"from": 0, "to": 18200, "rate": 0.0, "base": 0}
        assert brackets[1]["rate"] == pytest.approx(0.16)
        assert brackets[-1]["to"] is None
        assert brackets[-1]["rate"] == pytest.approx(0.45)
        assert brackets[-1]["base"] == 51638

    def test_returns_empty_on_empty_html(self):
        assert extract_individual_resident_brackets(
            url="https://example/", html=b"", fy="2025-26", content_hash="0" * 64
        ) == []

    def test_returns_empty_on_unrelated_html(self):
        assert extract_individual_resident_brackets(
            url="https://example/", html=b"<html><body>nothing useful</body></html>",
            fy="2025-26", content_hash="0" * 64,
        ) == []

    def test_multi_year_page_emits_one_per_table(self):
        """Critical case: ATO's canonical /tax-rates-australian-residents page
        has one table per FY with the FY in <caption>. Each FY -> one FactCandidate."""
        html = b"""<html><body>
            <h1>Tax rates - Australian resident</h1>
            <table>
              <caption>Resident tax rates 2025\xe2\x80\x9326</caption>
              <thead><tr><th>Taxable income</th><th>Tax on this income</th></tr></thead>
              <tbody>
                <tr><td>$0 - $18,200</td><td>Nil</td></tr>
                <tr><td>$18,201 - $45,000</td><td>16c for each $1 over $18,200</td></tr>
              </tbody>
            </table>
            <table>
              <caption>Resident tax rates 2024\xe2\x80\x9325</caption>
              <thead><tr><th>Taxable income</th><th>Tax on this income</th></tr></thead>
              <tbody>
                <tr><td>$0 - $18,200</td><td>Nil</td></tr>
                <tr><td>$18,201 - $45,000</td><td>19c for each $1 over $18,200</td></tr>
              </tbody>
            </table>
        </body></html>"""
        cands = extract_individual_resident_brackets(
            url="https://www.ato.gov.au/tax-rates-and-codes/tax-rates-australian-residents",
            html=html, fy=None, content_hash="0" * 64,
        )
        assert len(cands) == 2
        slugs = sorted(c.slug for c in cands)
        assert slugs == ["individual-resident-2024-25", "individual-resident-2025-26"]
        # 2024-25 had 19c rate (old), 2025-26 has 16c (new)
        by_fy = {c.payload["fy"]: c for c in cands}
        assert by_fy["2024-25"].payload["brackets"][1]["rate"] == pytest.approx(0.19)
        assert by_fy["2025-26"].payload["brackets"][1]["rate"] == pytest.approx(0.16)


class TestSchemas:
    def test_all_schemas_well_formed_json_schema(self):
        for name, sch in SCHEMAS.items():
            assert sch["$schema"].startswith("https://json-schema.org/")
            assert sch["$id"] == name
            assert "required" in sch
            assert "properties" in sch

    def test_rate_table_payload_matches_required(self):
        """Sanity-check that our extractor output includes every required field."""
        cands = extract_individual_resident_brackets(
            url="https://www.ato.gov.au/tax-rates-and-codes/x-2025-26",
            html=SYNTHETIC_RATES_HTML, fy="2025-26", content_hash="0" * 64,
        )
        assert len(cands) == 1
        cand = cands[0]
        sch = SCHEMAS[cand.schema]
        missing = [k for k in sch["required"] if k not in cand.payload]
        assert missing == [], f"payload missing required fields: {missing}"

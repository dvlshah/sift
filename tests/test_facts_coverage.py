"""Facts-coverage detector — find pages that look fact-shaped but produced no facts."""

import json
from pathlib import Path

import pytest

from sift import paths
from sift.manifest import (
    apply_fetch_result, init_schema, now_utc, open_db, transaction, upsert_seed,
)
from evals.facts_coverage import _detect_candidate, run as coverage_run


# Page with a rate-table-shaped table — should be detected as a candidate
RATE_TABLE_HTML = b"""<!DOCTYPE html>
<html><body>
<h1>Resident tax rates 2026-27</h1>
<table>
  <caption>Resident tax rates 2026-27</caption>
  <thead><tr><th>Taxable income</th><th>Tax on this income</th></tr></thead>
  <tbody>
    <tr><td>$0 - $18,200</td><td>Nil</td></tr>
    <tr><td>$18,201 - $45,000</td><td>15c for each $1 over $18,200</td></tr>
  </tbody>
</table>
</body></html>"""

# Page that looks like a news/announcement — no table headers about tax,
# no caption with FY. Should NOT be a candidate.
NON_CANDIDATE_HTML = b"""<!DOCTYPE html>
<html><body>
<h1>What's new at the ATO</h1>
<p>News items below.</p>
<ul><li>Item one</li><li>Item two</li></ul>
</body></html>"""


class TestDetector:
    def test_detects_table_with_rate_header(self):
        cand = _detect_candidate(RATE_TABLE_HTML)
        assert cand is not None
        assert cand.rate_table_signal is True
        assert any("2026-27" in s for s in cand.fy_in_caption)
        assert cand.table_count == 1

    def test_skips_non_table_page(self):
        cand = _detect_candidate(NON_CANDIDATE_HTML)
        assert cand is None

    def test_skips_empty(self):
        assert _detect_candidate(b"") is None
        assert _detect_candidate(b"<html></html>") is None


@pytest.fixture
def index_with_candidate(tmp_path):
    """Seed an index with one rate-table-shaped FRESH page + a non-candidate page.
    Neither has a facts file yet — coverage should report 1 gap."""
    root = tmp_path
    run_id = "test-cov-run"
    conn = open_db(paths.manifest_path(root))
    init_schema(conn)

    # Seed candidate page with raw blob
    candidate_url = "https://www.ato.gov.au/about-ato/new-legislation/in-detail/individuals/personal-income-tax-new-tax-cuts"
    noncand_url = "https://www.ato.gov.au/individuals-and-families/your-tax-return"
    from sift.fetch import sha256_hex, write_raw_blob
    h1 = sha256_hex(RATE_TABLE_HTML)
    h2 = sha256_hex(NON_CANDIDATE_HTML)
    write_raw_blob(root, h1, RATE_TABLE_HTML)
    write_raw_blob(root, h2, NON_CANDIDATE_HTML)

    now = now_utc()
    for url, raw_hash in ((candidate_url, h1), (noncand_url, h2)):
        with transaction(conn):
            upsert_seed(conn, url, "LIVING", None, "v1", None, now)
            apply_fetch_result(
                conn, url=url, now=now,
                http_status=200, http_etag=None, http_last_modified=None,
                raw_hash=raw_hash, content_hash="ch_" + raw_hash[:16],
                crawler_version="v1", extractor_version="ext",
                normalizer_version="v1", error=None,
            )

    # facts/ dir empty for now
    (paths.run_dir(root, run_id) / "facts").mkdir(parents=True)
    return root, run_id, conn, candidate_url


class TestCoverage:
    def test_reports_gap_when_no_facts_emitted(self, index_with_candidate):
        root, run_id, conn, candidate_url = index_with_candidate
        m = coverage_run(root, run_id, conn=conn)
        assert m.pages_scanned == 2
        assert m.pages_with_rate_table_shape == 1
        assert m.facts_files_emitted == 0
        assert len(m.coverage_gaps) == 1
        assert m.coverage_gaps[0].url == candidate_url
        assert m.coverage_ratio == 0.0
        assert m.gaps_by_section == {"about-ato": 1}

    def test_no_gap_when_facts_file_exists(self, index_with_candidate):
        root, run_id, conn, candidate_url = index_with_candidate
        # Write a facts file referencing the candidate URL
        f = paths.run_dir(root, run_id) / "facts" / "ato-rate-table-v1" / "x.json"
        f.parent.mkdir(parents=True)
        f.write_text(json.dumps({
            "$schema": "ato-rate-table-v1",
            "source_url": candidate_url,
            "fy": "2026-27",
        }))
        m = coverage_run(root, run_id, conn=conn)
        assert m.pages_with_rate_table_shape == 1
        assert m.facts_files_emitted == 1
        assert len(m.coverage_gaps) == 0
        assert m.coverage_ratio == 1.0

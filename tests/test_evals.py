"""Tests for the evals/ suite (excluding LLM judge which needs API access)."""

import json
from pathlib import Path

import pytest

from sift import paths
from sift.manifest import (
    apply_fetch_result, init_schema, now_utc, open_db, transaction, upsert_seed,
)


@pytest.fixture
def sized_index(tmp_path):
    """Build a tiny but realistic index with manifest rows + a few md files + snapshot.json."""
    root = tmp_path
    run_id = "test-run-20260101T120000Z"
    conn = open_db(paths.manifest_path(root))
    init_schema(conn)
    # Seed a handful across tiers
    seeds = [
        ("https://www.ato.gov.au/individuals-and-families/your-tax-return", "LIVING"),
        ("https://www.ato.gov.au/individuals-and-families/medicare-levy", "LIVING"),
        ("https://www.ato.gov.au/businesses-and-organisations/gst", "LIVING"),
        ("https://www.ato.gov.au/forms-and-instructions/foo-2025", "CURRENT_FORMS"),
        ("https://www.ato.gov.au/forms-and-instructions/old-2015", "FROZEN"),
    ]
    now = now_utc()
    for i, (url, tier) in enumerate(seeds):
        with transaction(conn):
            upsert_seed(conn, url, tier, None, "v1", None, now)
            apply_fetch_result(
                conn, url=url, now=now,
                http_status=200, http_etag=None, http_last_modified=None,
                raw_hash=f"raw{i}" + "0" * 60, content_hash=f"ch{i}" + "0" * 60,
                crawler_version="v1.0.0",
                extractor_version="trafilatura-2.0.0-cfg2",
                normalizer_version="v1", error=None,
            )
        # Write the md file the manifest claims
        md = paths.md_path(root, run_id, url)
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(
            f"---\nurl: {url}\ntier: {tier}\n"
            f"content_hash: sha256:ch{i}{'0'*60}\n---\n"
            f"# Test page {i}\n\n## Section A\n\nSome body text " * 20
        )
    # snapshot.json
    snap = paths.snapshot_path(root, run_id)
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(json.dumps({
        "run_id": run_id,
        "status": "published",
        "completed_at": "2026-01-01T12:00:00Z",
        "expected_urls": 5,
        "counts_by_state": {"FRESH": 5},
        "counts_by_tier": {"LIVING": 3, "CURRENT_FORMS": 1, "FROZEN": 1},
        "versions": {"crawler": "v1", "extractor": "ext", "normalizer": "v1", "classifier": "v1"},
        "gates": [{"name": "coverage", "passed": True, "detail": "ok"}],
    }))
    # current symlink (so CLI auto-resolves run_id)
    cur = root / "current"
    cur.symlink_to(paths.run_dir(root, run_id).resolve(), target_is_directory=True)

    # Write a synthetic run log so performance can find timings
    logs = root / "_logs"
    logs.mkdir(exist_ok=True)
    (logs / f"full-run-{run_id}.log").write_text(
        'plan: {"FETCH": 5}\n  [plan 0.01s]\n'
        'fetched: 5\n  [fetch 1.00s]\nextracted: 5\n  [extract 0.20s]\n'
        '{\n  "run_id": "' + run_id + '",\n'
        '  "published": true,\n'
        '  "timings_sec": {"plan": 0.01, "fetch": 1.0, "extract": 0.2, "commit": 0.01, "publish": 0.1},\n'
        '  "total_sec": 1.32,\n'
        '  "fetch_throughput_req_per_sec": 5.0\n'
        '}\n'
        '    1.5  real    0.5  user    0.1  sys\n'
        '  100000000  maximum resident set size\n'
    )
    return root, run_id


class TestSampler:
    def test_proportional_returns_at_most_total(self, sized_index):
        from evals.sampler import sample_by_count
        root, _ = sized_index
        conn = open_db(paths.manifest_path(root))
        rows = sample_by_count(conn, 3, label="test")
        assert len(rows) <= 3

    def test_deterministic_same_label_same_result(self, sized_index):
        from evals.sampler import sample_by_count
        root, _ = sized_index
        conn = open_db(paths.manifest_path(root))
        a = sample_by_count(conn, 3, label="x")
        b = sample_by_count(conn, 3, label="x")
        assert [r.url for r in a] == [r.url for r in b]


class TestEfficiency:
    def test_counts_disk(self, sized_index):
        from evals.efficiency import run as efficiency_run
        root, run_id = sized_index
        m = efficiency_run(root, run_id)
        assert m.disk.manifest_db_bytes > 0
        assert m.disk.md_files_count == 5
        assert m.disk.md_files_bytes > 0
        assert m.disk.total_bytes >= m.disk.manifest_db_bytes + m.disk.md_files_bytes

    def test_per_page_costs(self, sized_index):
        from evals.efficiency import run as efficiency_run
        root, run_id = sized_index
        m = efficiency_run(root, run_id)
        assert m.per_page.md_bytes_per_page > 0
        assert m.per_page.manifest_bytes_per_row > 0


class TestPerformance:
    def test_parses_timings_from_log(self, sized_index):
        from evals.performance import run as performance_run
        root, run_id = sized_index
        m = performance_run(root, run_id)
        assert m.phase_timings.total_sec == 1.32
        assert m.phase_timings.fetch_sec == 1.0
        assert m.fetch_throughput_req_per_sec == 5.0
        assert m.extract_throughput_pages_per_sec > 0  # derived: 5 / 0.2
        assert m.counts_by_state.get("FRESH") == 5

    def test_parses_time_l_block(self, sized_index):
        from evals.performance import run as performance_run
        root, run_id = sized_index
        m = performance_run(root, run_id)
        assert m.resources.peak_rss_bytes == 100_000_000
        assert m.resources.wall_sec == 1.5


class TestFactsValidation:
    def test_no_facts_no_errors(self, sized_index):
        from evals.facts_validation import run as facts_run
        root, run_id = sized_index
        m = facts_run(root, run_id)
        assert m.facts_total == 0
        assert m.facts_invalid == 0

    def test_validates_real_facts(self, sized_index):
        from evals.facts_validation import run as facts_run
        root, run_id = sized_index
        # Add a schema and a valid + invalid fact
        facts = paths.run_dir(root, run_id) / "facts"
        (facts / "schemas").mkdir(parents=True)
        (facts / "schemas" / "test-schema-v1.json").write_text(json.dumps({
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "test-schema-v1",
            "type": "object",
            "required": ["$schema", "x"],
            "properties": {"$schema": {"const": "test-schema-v1"}, "x": {"type": "integer"}},
        }))
        (facts / "test-schema-v1").mkdir()
        (facts / "test-schema-v1" / "ok.json").write_text(
            json.dumps({"$schema": "test-schema-v1", "x": 42})
        )
        (facts / "test-schema-v1" / "bad.json").write_text(
            json.dumps({"$schema": "test-schema-v1", "x": "not-an-int"})
        )
        m = facts_run(root, run_id)
        assert m.facts_total == 2
        assert m.facts_valid == 1
        assert m.facts_invalid == 1
        assert m.schemas_found == 1


class TestStructuralBasics:
    def test_handles_empty_corpus(self, tmp_path):
        from evals.structural import run as structural_run
        # Brand-new empty index
        conn = open_db(paths.manifest_path(tmp_path))
        init_schema(conn)
        m = structural_run(tmp_path, "no-run", conn=conn, sample=5)
        assert m.pages_evaluated == 0
        assert m.flagged_count == 0

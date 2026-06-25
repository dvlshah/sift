"""Functional tests for MCP tool implementations (skipping the stdio layer)."""

import json
from pathlib import Path

import pytest

from sift import mcp_server, paths
from sift.manifest import (
    apply_fetch_result, init_schema, now_utc, open_db, transaction, upsert_seed,
)


@pytest.fixture
def index(tmp_path):
    """A minimal index with a current/ symlink, one md file, one facts file, and a manifest."""
    root = tmp_path
    run_id = "test-run"
    # manifest
    conn = open_db(paths.manifest_path(root))
    init_schema(conn)
    url = "https://www.ato.gov.au/individuals-and-families/your-tax-return"
    now = now_utc()
    with transaction(conn):
        upsert_seed(conn, url, "LIVING", None, "v1", None, now)
        apply_fetch_result(
            conn, url=url, now=now,
            http_status=200, http_etag=None, http_last_modified=None,
            raw_hash="r1", content_hash="c1",
            crawler_version="v1", extractor_version="ext",
            normalizer_version="v1", error=None,
        )
    # md file
    md = paths.md_path(root, run_id, url)
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("""---
url: https://www.ato.gov.au/individuals-and-families/your-tax-return
title: Your tax return
content_hash: sha256:c1
tier: LIVING
audience: individuals
fy_years: ["2025-26"]
anchors: ["lodging-through-mytax"]
---
# Your tax return

## Lodging through myTax {#lodging-through-mytax}

You can lodge your tax return online via myTax. Section 8-1 of the ITAA 1997
applies here. The cents per kilometre rate for 2025-26 is 88c.
""")
    # facts file
    facts_file = paths.facts_dir(root, run_id) / "ato-rate-table-v1" / "individual-resident-2025-26.json"
    facts_file.parent.mkdir(parents=True, exist_ok=True)
    facts_file.write_text(json.dumps({
        "$schema": "ato-rate-table-v1",
        "source_url": "https://www.ato.gov.au/tax-rates-and-codes/individual-2025-26",
        "content_hash": "sha256:abc",
        "fy": "2025-26",
        "audience": "individual_resident",
        "brackets": [{"from": 0, "to": 18200, "rate": 0.0, "base": 0}],
        "effective_from": "2025-07-01",
        "effective_to": "2026-06-30",
    }, indent=2))
    # INDEX.md
    (paths.run_dir(root, run_id) / "INDEX.md").write_text(
        "# ato.gov.au — agent index\n\n## Sections\n\n- Individuals → sections/individuals/INDEX.md\n"
    )
    # current symlink
    cur = paths.current_symlink(root)
    if cur.exists() or cur.is_symlink():
        cur.unlink()
    cur.symlink_to(paths.run_dir(root, run_id).resolve(), target_is_directory=True)
    return root


def _text(result):
    """Extract the text payload from a CallToolResult."""
    assert result.content
    return result.content[0].text


class TestPathSafety:
    def test_traversal_blocked(self, index):
        cur, _ = mcp_server._resolve_root(index)
        r = mcp_server.tool_read_md(cur, "../../../etc/passwd")
        assert r.isError
        assert "escapes" in _text(r)

    def test_missing_file_has_recovery_hint(self, index):
        cur, _ = mcp_server._resolve_root(index)
        r = mcp_server.tool_read_md(cur, "md/nonexistent.md")
        assert r.isError
        assert "glob_corpus" in _text(r) or "query_manifest" in _text(r)


class TestReadMd:
    def test_reads_full_file(self, index):
        cur, _ = mcp_server._resolve_root(index)
        r = mcp_server.tool_read_md(
            cur, "md/individuals-and-families/your-tax-return.md"
        )
        assert not r.isError
        body = _text(r)
        assert "Your tax return" in body
        assert "Lodging through myTax" in body
        assert "{#lodging-through-mytax}" in body

    def test_reads_index_md(self, index):
        cur, _ = mcp_server._resolve_root(index)
        r = mcp_server.tool_read_md(cur, "INDEX.md")
        assert not r.isError
        assert "agent index" in _text(r)

    def test_offset_and_limit(self, index):
        cur, _ = mcp_server._resolve_root(index)
        r = mcp_server.tool_read_md(
            cur, "md/individuals-and-families/your-tax-return.md",
            offset=100, limit=50,
        )
        assert not r.isError
        body = _text(r)
        # Body content within limit; truncation note is added outside the limit
        # so the agent can see it was truncated.
        assert "[truncated at 50 chars" in body


class TestGrepCorpus:
    def test_finds_identifier(self, index):
        cur, _ = mcp_server._resolve_root(index)
        r = mcp_server.tool_grep_corpus(cur, r"Section 8-1")
        assert not r.isError
        body = _text(r)
        assert "your-tax-return.md" in body
        assert "Section 8-1" in body

    def test_finds_anchor(self, index):
        cur, _ = mcp_server._resolve_root(index)
        r = mcp_server.tool_grep_corpus(cur, r"\{#lodging-through-mytax\}")
        assert not r.isError
        assert "your-tax-return.md" in _text(r)

    def test_files_only(self, index):
        cur, _ = mcp_server._resolve_root(index)
        r = mcp_server.tool_grep_corpus(cur, "tax return", files_only=True)
        body = _text(r)
        # File-level: should be one line per matching file
        assert "your-tax-return.md" in body
        assert ":" not in body.split("\n")[0]  # no line numbers

    def test_no_match_is_ok_not_error(self, index):
        cur, _ = mcp_server._resolve_root(index)
        r = mcp_server.tool_grep_corpus(cur, "xyzzy-nonexistent-pattern")
        assert not r.isError
        assert "No matches" in _text(r)

    def test_invalid_regex_returns_error(self, index):
        cur, _ = mcp_server._resolve_root(index)
        r = mcp_server.tool_grep_corpus(cur, "(unclosed")
        assert r.isError


class TestGlobCorpus:
    def test_finds_md_files(self, index):
        cur, _ = mcp_server._resolve_root(index)
        r = mcp_server.tool_glob_corpus(cur, "md/**/*.md")
        assert not r.isError
        assert "your-tax-return.md" in _text(r)

    def test_finds_facts_files(self, index):
        cur, _ = mcp_server._resolve_root(index)
        r = mcp_server.tool_glob_corpus(cur, "facts/**/*.json")
        assert not r.isError
        assert "individual-resident-2025-26.json" in _text(r)


class TestListDir:
    def test_root_listing(self, index):
        cur, _ = mcp_server._resolve_root(index)
        r = mcp_server.tool_list_dir(cur, ".")
        body = _text(r)
        assert "md" in body
        assert "facts" in body
        assert "INDEX.md" in body

    def test_subdir(self, index):
        cur, _ = mcp_server._resolve_root(index)
        r = mcp_server.tool_list_dir(cur, "facts")
        assert "ato-rate-table-v1" in _text(r)


class TestQueryManifest:
    def test_basic_select(self, index):
        cur, _ = mcp_server._resolve_root(index)
        r = mcp_server.tool_query_manifest(
            cur, "SELECT url, tier FROM manifest", index_root=index,
        )
        assert not r.isError
        rows = json.loads(_text(r))
        assert len(rows) == 1
        assert rows[0]["tier"] == "LIVING"

    def test_refuses_non_select(self, index):
        cur, _ = mcp_server._resolve_root(index)
        r = mcp_server.tool_query_manifest(
            cur, "DROP TABLE manifest", index_root=index,
        )
        assert r.isError
        assert "SELECT" in _text(r)

    def test_with_cte_allowed(self, index):
        cur, _ = mcp_server._resolve_root(index)
        r = mcp_server.tool_query_manifest(
            cur,
            "WITH t AS (SELECT * FROM manifest) SELECT count(*) AS n FROM t",
            index_root=index,
        )
        assert not r.isError


class TestReadFacts:
    def test_reads_rate_table(self, index):
        cur, _ = mcp_server._resolve_root(index)
        r = mcp_server.tool_read_facts(
            cur, "facts/ato-rate-table-v1/individual-resident-2025-26.json"
        )
        assert not r.isError
        body = _text(r)
        # Header has provenance
        assert "schema: ato-rate-table-v1" in body
        assert "source_url:" in body
        # Body is the full JSON
        assert '"brackets"' in body
        assert '"fy": "2025-26"' in body

    def test_missing_facts_file(self, index):
        cur, _ = mcp_server._resolve_root(index)
        r = mcp_server.tool_read_facts(cur, "facts/none.json")
        assert r.isError
        # Recovery hint should mention list_dir / glob_corpus / schemas
        body = _text(r)
        assert "list_dir" in body or "glob_corpus" in body


class TestServerWiring:
    def test_list_tools_returns_all_read_tools(self, index):
        descs = mcp_server._tool_descriptors()
        names = {t.name for t in descs}
        assert names == {
            "snapshot_status", "changed_since",
            "read_md", "grep_corpus", "glob_corpus",
            "list_dir", "query_manifest", "read_facts",
        }

    def test_all_tools_read_only(self, index):
        for t in mcp_server._tool_descriptors():
            assert t.annotations.readOnlyHint is True


class TestSnapshotStatus:
    def test_reports_published_when_current_exists(self, index):
        # `index` fixture creates a current/ symlink + snapshot.json absence
        r = mcp_server.tool_snapshot_status(index)
        assert not r.isError
        body = json.loads(_text(r))
        assert body["published"] is True
        assert body["run_id"] == "test-run"
        assert body["current_path"].endswith("test-run")
        # Artifact inventory has counts
        assert "artifact_inventory" in body
        assert body["artifact_inventory"]["md_files"] >= 1
        # Entry points listed
        assert any("INDEX.md" in e for e in body["entry_points"])

    def test_reports_unpublished_when_no_current(self, tmp_path):
        # tmp_path has no current/ symlink
        r = mcp_server.tool_snapshot_status(tmp_path)
        assert not r.isError  # status is informational, never errors
        body = json.loads(_text(r))
        assert body["published"] is False
        assert "reason" in body
        assert "next_step" in body
        assert "sift" in body["next_step"]

    def test_includes_snapshot_json_when_present(self, tmp_path):
        # Build a minimal current/ pointing at a run with snapshot.json
        run_dir = paths.run_dir(tmp_path, "test-run-2")
        run_dir.mkdir(parents=True)
        (run_dir / "snapshot.json").write_text(json.dumps({
            "run_id": "test-run-2",
            "status": "published",
            "completed_at": "2026-05-24T12:00:00Z",
            "counts_by_state": {"FRESH": 1000},
            "counts_by_tier": {"LIVING": 500, "FROZEN": 500},
            "versions": {"crawler": "v1", "extractor": "ext", "normalizer": "v1", "classifier": "v1"},
            "gates": [{"name": "coverage", "passed": True, "detail": "ok"}],
            "expected_urls": 1000,
        }))
        cur = paths.current_symlink(tmp_path)
        cur.symlink_to(run_dir.resolve(), target_is_directory=True)
        r = mcp_server.tool_snapshot_status(tmp_path)
        body = json.loads(_text(r))
        assert body["snapshot"]["status"] == "published"
        assert body["snapshot"]["counts_by_tier"]["LIVING"] == 500
        assert body["snapshot"]["gates"][0]["name"] == "coverage"


class TestPublishedGuard:
    def test_resolve_root_returns_false_when_no_current(self, tmp_path):
        cur, is_published = mcp_server._resolve_root(tmp_path)
        assert is_published is False

    def test_resolve_root_returns_true_when_current_exists(self, index):
        cur, is_published = mcp_server._resolve_root(index)
        assert is_published is True

    def test_require_published_blocks(self):
        guard = mcp_server._require_published(False)
        assert guard is not None
        assert guard.isError
        assert "snapshot_status" in _text(guard)
        assert "sift publish" in _text(guard)

    def test_require_published_passes(self):
        guard = mcp_server._require_published(True)
        assert guard is None

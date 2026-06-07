"""INDEX.md / routes.tsv / section indexes generated from a small synthetic manifest."""

from pathlib import Path

import pytest

from sift import agent_surface, paths
from sift.manifest import (
    apply_fetch_result,
    init_schema,
    now_utc,
    open_db,
    transaction,
    upsert_seed,
)


@pytest.fixture
def populated(tmp_path):
    root = tmp_path
    conn = open_db(paths.manifest_path(root))
    init_schema(conn)
    rows = [
        ("https://www.ato.gov.au/individuals-and-families/your-tax-return",
         "LIVING", None),
        ("https://www.ato.gov.au/individuals-and-families/medicare-levy",
         "LIVING", None),
        ("https://www.ato.gov.au/businesses-and-organisations/gst",
         "LIVING", None),
        ("https://www.ato.gov.au/forms-and-instructions/individual-tax-return-2025-instructions/p1",
         "CURRENT_FORMS", "individual-tax-return-2025-instructions"),
        ("https://www.ato.gov.au/forms-and-instructions/individual-tax-return-2025-instructions/p2",
         "CURRENT_FORMS", "individual-tax-return-2025-instructions"),
        ("https://www.ato.gov.au/forms-and-instructions/foreign-income-2005/p1",
         "FROZEN", "foreign-income-2005"),
        ("https://www.ato.gov.au/tax-rates-and-codes/2025-26-tax-rates",
         "LIVING", None),
        ("https://www.ato.gov.au/media-centre/news-item-1",
         "NEWS", None),
    ]
    now = now_utc()
    for url, tier, pg in rows:
        with transaction(conn):
            upsert_seed(conn, url, tier, pg, "v1", None, now)
            apply_fetch_result(
                conn, url=url, now=now,
                http_status=200, http_etag=None, http_last_modified=None,
                raw_hash=f"raw{hash(url) % 10000}",
                content_hash=f"ch{hash(url) % 10000}",
                crawler_version="v1.0.0", extractor_version="ext",
                normalizer_version="v1", error=None,
            )
    return root, conn, "smoke-run"


class TestRoutesTsv:
    def test_writes_header_and_rows(self, populated):
        root, conn, run_id = populated
        out = agent_surface.build_routes_tsv(conn, root, run_id)
        lines = out.read_text().splitlines()
        assert lines[0].startswith("url\t")
        assert len(lines) == 9  # header + 8 rows
        for line in lines[1:]:
            parts = line.split("\t")
            assert len(parts) == 7  # url, md_path, tier, content_hash, fetched_at, audience, fy_years
            assert parts[0].startswith("https://")

    def test_md_paths_are_relative(self, populated):
        root, conn, run_id = populated
        out = agent_surface.build_routes_tsv(conn, root, run_id)
        for line in out.read_text().splitlines()[1:]:
            md_path = line.split("\t")[1]
            assert not md_path.startswith("/")  # relative

    def test_fy_years_populated_for_form_pages(self, populated):
        root, conn, run_id = populated
        out = agent_surface.build_routes_tsv(conn, root, run_id)
        text = out.read_text()
        # The 2025 instructions and 2025-26 rates should have FY tags
        for line in text.splitlines()[1:]:
            url = line.split("\t")[0]
            fys = line.split("\t")[-1]
            if "2025-instructions" in url or "2025-26" in url:
                assert "2025-26" in fys
            if "foreign-income-2005" in url:
                assert "2005-06" in fys


class TestIndexMd:
    def test_lists_sections(self, populated):
        root, conn, run_id = populated
        out = agent_surface.build_index_md(conn, root, run_id)
        text = out.read_text()
        # Header derives from the active profile's primary_host, not a hardcoded
        # site name — so non-ATO profiles get a correct title too.
        from sift.sites import current_profile
        assert f"# {current_profile().primary_host} — agent index" in text
        assert "Individuals and families" in text
        assert "Businesses and organisations" in text
        assert "Forms and instructions" in text

    def test_header_follows_active_profile(self, populated, monkeypatch):
        # Regression: the INDEX.md title must derive from the active profile's
        # primary_host, not a hardcoded 'ato.gov.au' (which leaked onto every
        # non-ATO corpus, e.g. a Stripe index).
        root, conn, run_id = populated

        class _FakeProfile:
            primary_host = "docs.example.com"
            section_order: list = []

        monkeypatch.setattr(agent_surface, "current_profile", lambda: _FakeProfile())
        out = agent_surface.build_index_md(conn, root, run_id)
        text = out.read_text()
        assert "# docs.example.com — agent index" in text
        assert "ato.gov.au" not in text

    def test_includes_tooling_pointers(self, populated):
        root, conn, run_id = populated
        out = agent_surface.build_index_md(conn, root, run_id)
        text = out.read_text()
        assert "routes.tsv" in text
        assert "manifest.db" in text
        assert "changelog.jsonl" in text
        assert "facts/" in text

    def test_under_200_lines(self, populated):
        root, conn, run_id = populated
        out = agent_surface.build_index_md(conn, root, run_id)
        # Index must stay loadable in one chunk
        assert out.read_text().count("\n") < 200


class TestSectionIndex:
    def test_forms_groups_by_guide(self, populated):
        root, conn, run_id = populated
        out = agent_surface.build_section_index(
            conn, root, run_id, "forms-and-instructions"
        )
        text = out.read_text()
        # Should call out each parent_guide once with the page count
        assert "individual-tax-return-2025-instructions" in text
        assert "2 pages" in text
        assert "foreign-income-2005" in text

    def test_living_lists_urls(self, populated):
        root, conn, run_id = populated
        out = agent_surface.build_section_index(
            conn, root, run_id, "individuals-and-families"
        )
        text = out.read_text()
        assert "your-tax-return" in text
        assert "medicare-levy" in text


class TestBuildAll:
    def test_produces_all_artifacts(self, populated):
        root, conn, run_id = populated
        result = agent_surface.build_all(conn, root, run_id)
        assert "index_md" in result
        assert "routes_tsv" in result
        assert int(result["section_indexes"]) >= 4

    def test_deterministic(self, populated):
        """Same manifest state -> byte-identical artifacts on re-run."""
        root, conn, run_id = populated
        agent_surface.build_all(conn, root, run_id)
        idx_a = paths.index_md_path(root, run_id).read_text()
        tsv_a = paths.routes_tsv_path(root, run_id).read_text()
        agent_surface.build_all(conn, root, run_id)
        idx_b = paths.index_md_path(root, run_id).read_text()
        tsv_b = paths.routes_tsv_path(root, run_id).read_text()
        assert idx_a == idx_b
        assert tsv_a == tsv_b

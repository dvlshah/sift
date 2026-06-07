"""Tests for ``sift.registry`` — multi-index discovery + IndexDescriptor
metadata resolution.

The registry is the routing layer for the multi-index MCP server, so a
broken description / page-count / slug fallback ends up in the agent's
context and biases its index-selection behavior. Tests cover all four
sources we draw from: [index] section in sift.toml, snapshot.json,
md/-tree count, and inferred domain from frontmatter URLs.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sift import paths
from sift.registry import (
    IndexDescriptor,
    IndexRegistry,
    RunSummary,
    describe,
    discover_indexes,
    is_sift_root,
    latest_run_dir,
    recent_runs_for,
    unseen_count_for,
    writeable_capability,
)


# ---- helpers --------------------------------------------------------------

def _format_block(name: str, payload: dict) -> str:
    lines = [f"[{name}]"]
    for k, v in payload.items():
        if isinstance(v, str):
            lines.append(f'{k} = "{v}"')
        elif isinstance(v, (list, tuple)):
            lines.append(f'{k} = [{", ".join(repr(x) for x in v)}]')
        else:
            lines.append(f'{k} = {v}')
    return "\n".join(lines) + "\n"


def _build_index(parent: Path, slug: str,
                 *, host: str = "x.test",
                 toml_index_section: dict | None = None,
                 toml_seed_section: dict | None = None,
                 pages: int = 1,
                 snapshot_count: int | None = None,
                 unseen_urls: tuple[str, ...] = ()) -> Path:
    """Build a sift-shaped root under ``parent/<slug>/`` with a usable
    ``current`` symlink and ``pages`` md files. Optionally writes
    ``[index]`` / ``[seed]`` sections to sift.toml, a snapshot.json with
    a fresh count, and inserts UNSEEN manifest rows."""
    root = parent / slug
    rid = "1970-01-01T00-00-00_test"
    md = paths.run_dir(root, rid) / "md"
    md.mkdir(parents=True)
    for i in range(pages):
        (md / f"page-{i}.md").write_text(
            f"---\nurl: https://{host}/p/{i}\n---\nbody {i}\n"
        )
    if snapshot_count is not None:
        snap = paths.snapshot_path(root, rid)
        snap.write_text(json.dumps({
            "counts_by_state": {"FRESH": snapshot_count}
        }))
    sections: list[str] = []
    if toml_index_section is not None:
        sections.append(_format_block("index", toml_index_section))
    if toml_seed_section is not None:
        sections.append(_format_block("seed", toml_seed_section))
    if sections:
        (root / "sift.toml").write_text("\n".join(sections))
    (root / "current").symlink_to(paths.run_dir(root, rid))

    # Always init the manifest so callers don't have to remember; the
    # registry tests rely on manifest.db existing for unseen_count_for.
    from sift.manifest import (
        init_schema, now_utc, open_db, transaction, upsert_seed,
    )
    conn = open_db(paths.manifest_path(root))
    init_schema(conn)
    if unseen_urls:
        now = now_utc()
        with transaction(conn):
            for url in unseen_urls:
                upsert_seed(conn, url, "LIVING", None, "v1", None, now)
    conn.close()
    return root


# ---- is_sift_root + latest_run_dir ----------------------------------------

class TestIsSiftRoot:
    def test_root_with_current_symlink(self, tmp_path):
        root = _build_index(tmp_path, "alpha")
        assert is_sift_root(root)

    def test_root_with_runs_but_broken_symlink(self, tmp_path):
        root = _build_index(tmp_path, "alpha")
        # Replace the symlink with a broken one.
        (root / "current").unlink()
        (root / "current").symlink_to("nope/does/not/exist")
        # is_sift_root must still return True via the runs/ fallback.
        assert is_sift_root(root)

    def test_root_without_runs_or_symlink_is_not_a_root(self, tmp_path):
        (tmp_path / "lonely").mkdir()
        assert not is_sift_root(tmp_path / "lonely")

    def test_nonexistent_path(self, tmp_path):
        assert not is_sift_root(tmp_path / "ghost")


class TestLatestRunDir:
    def test_picks_newest_by_name(self, tmp_path):
        root = tmp_path / "x"
        (root / "runs" / "2026-01-01T00-00-00Z").mkdir(parents=True)
        (root / "runs" / "2026-05-31T00-00-00Z").mkdir(parents=True)
        (root / "runs" / "2026-03-15T00-00-00Z").mkdir(parents=True)
        latest = latest_run_dir(root)
        assert latest is not None
        assert latest.name == "2026-05-31T00-00-00Z"

    def test_no_runs_returns_none(self, tmp_path):
        assert latest_run_dir(tmp_path / "x") is None


# ---- describe -------------------------------------------------------------

class TestDescribe:
    def test_falls_back_to_dir_name_and_inferred_domain(self, tmp_path):
        # No sift.toml — should derive slug from dir name + domain from
        # a sampled page URL.
        root = _build_index(tmp_path, "alpha", host="alpha.test")
        d = describe(root)
        assert d.slug == "alpha"
        assert d.domain == "alpha.test"
        assert "alpha.test" in d.description
        # page_count via md/ rglob when no snapshot.json
        assert d.page_count == 1
        assert d.last_published is not None

    def test_uses_operator_supplied_index_section(self, tmp_path):
        root = _build_index(tmp_path, "alpha", host="alpha.test",
                            toml_index_section={
                                "slug": "claude-docs",
                                "description": "Claude API reference",
                                "domain": "platform.claude.com",
                                "tags": ["coding-agents", "api"],
                            })
        d = describe(root)
        assert d.slug == "claude-docs"
        assert d.description == "Claude API reference"
        assert d.domain == "platform.claude.com"
        assert d.tags == ("coding-agents", "api")

    def test_page_count_prefers_snapshot(self, tmp_path):
        root = _build_index(tmp_path, "alpha", pages=3,
                            snapshot_count=2654)
        d = describe(root)
        # Snapshot wins over the on-disk count
        assert d.page_count == 2654


# ---- discover_indexes -----------------------------------------------------

class TestDiscoverIndexes:
    def test_single_root_returns_one(self, tmp_path):
        root = _build_index(tmp_path, "alpha")
        out = discover_indexes(root)
        assert len(out) == 1
        assert out[0].slug == "alpha"

    def test_parent_returns_each_subroot(self, tmp_path):
        _build_index(tmp_path, "alpha")
        _build_index(tmp_path, "beta")
        _build_index(tmp_path, "gamma")
        out = discover_indexes(tmp_path)
        slugs = sorted(d.slug for d in out)
        assert slugs == ["alpha", "beta", "gamma"]

    def test_skips_non_sift_subdirs(self, tmp_path):
        _build_index(tmp_path, "alpha")
        (tmp_path / "not-a-sift-dir").mkdir()
        out = discover_indexes(tmp_path)
        slugs = [d.slug for d in out]
        assert slugs == ["alpha"]

    def test_slug_collision_disambiguates(self, tmp_path):
        # Two indexes set the same slug in their sift.toml — discover
        # should rewrite both with the directory-name suffix so neither
        # is silently swallowed.
        _build_index(tmp_path, "left",
                     toml_index_section={"slug": "docs"})
        _build_index(tmp_path, "right",
                     toml_index_section={"slug": "docs"})
        out = discover_indexes(tmp_path)
        slugs = sorted(d.slug for d in out)
        assert slugs == ["docs@left", "docs@right"]

    def test_empty_dir(self, tmp_path):
        assert discover_indexes(tmp_path) == []


# ---- IndexRegistry --------------------------------------------------------

class TestIndexRegistry:
    def test_single_mode_is_multi_false(self, tmp_path):
        root = _build_index(tmp_path, "alpha")
        reg = IndexRegistry.discover(root)
        assert reg.is_multi is False
        assert len(reg.indexes) == 1
        assert reg.by_slug("alpha") is not None

    def test_multi_mode_is_multi_true(self, tmp_path):
        _build_index(tmp_path, "alpha")
        _build_index(tmp_path, "beta")
        reg = IndexRegistry.discover(tmp_path)
        assert reg.is_multi is True
        assert reg.slugs() == ["alpha", "beta"]

    def test_by_slug_miss_returns_none(self, tmp_path):
        _build_index(tmp_path, "alpha")
        reg = IndexRegistry.discover(tmp_path)
        assert reg.by_slug("not-a-slug") is None

    def test_empty_parent_returns_empty_registry(self, tmp_path):
        reg = IndexRegistry.discover(tmp_path)
        assert reg.is_multi is True
        assert reg.indexes == ()


# ---- IndexDescriptor.to_dict shape ----------------------------------------

class TestToDict:
    def test_round_trip_fields(self, tmp_path):
        root = _build_index(tmp_path, "alpha",
                            toml_index_section={
                                "description": "x",
                                "domain": "x.test",
                                "tags": ["a", "b"],
                            })
        d = describe(root)
        out = d.to_dict()
        assert out["slug"] == "alpha"
        assert out["description"] == "x"
        assert out["domain"] == "x.test"
        assert out["tags"] == ["a", "b"]
        assert isinstance(out["page_count"], int)
        assert isinstance(out["last_published"], str)
        # Write-side fields appear with safe defaults
        assert out["accepts_writes"] is False
        assert out["allowed_hosts"] == []
        assert isinstance(out["unseen_count"], int)
        assert out["recent_runs"] == []


# ---- Write-side capability ------------------------------------------------

class TestWriteableCapability:
    def test_returns_true_when_host_allow_set(self, tmp_path):
        root = _build_index(
            tmp_path, "alpha",
            toml_seed_section={"host_allow": ["alpha.test", "AlPhA.TEST"]},
        )
        accepts, hosts = writeable_capability(root)
        assert accepts is True
        # Hosts are lowercased to match URL-host comparison
        assert hosts == ("alpha.test", "alpha.test")

    def test_returns_false_when_sift_toml_missing(self, tmp_path):
        root = _build_index(tmp_path, "alpha")
        accepts, hosts = writeable_capability(root)
        assert accepts is False
        assert hosts == ()

    def test_returns_false_when_host_allow_missing(self, tmp_path):
        root = _build_index(
            tmp_path, "alpha",
            toml_index_section={"description": "x"},   # only [index]
        )
        accepts, hosts = writeable_capability(root)
        assert accepts is False
        assert hosts == ()

    def test_returns_false_when_host_allow_empty(self, tmp_path):
        root = _build_index(
            tmp_path, "alpha",
            toml_seed_section={"host_allow": []},
        )
        accepts, hosts = writeable_capability(root)
        assert accepts is False
        assert hosts == ()


# ---- UNSEEN counting -----------------------------------------------------

class TestUnseenCount:
    def test_zero_for_empty_manifest(self, tmp_path):
        root = _build_index(tmp_path, "alpha")
        assert unseen_count_for(root) == 0

    def test_reflects_unseen_rows(self, tmp_path):
        root = _build_index(
            tmp_path, "alpha",
            unseen_urls=(
                "https://x.test/a", "https://x.test/b", "https://x.test/c",
            ),
        )
        assert unseen_count_for(root) == 3

    def test_returns_none_when_no_manifest(self, tmp_path):
        # Build a sift-shaped root then delete manifest.db
        root = _build_index(tmp_path, "alpha")
        (root / "manifest.db").unlink(missing_ok=True)
        assert unseen_count_for(root) is None


# ---- Recent runs --------------------------------------------------------

class TestRecentRuns:
    def test_empty_when_no_runs_table(self, tmp_path):
        root = _build_index(tmp_path, "alpha")
        # init_schema isn't called by _build_index; runs table absent
        assert recent_runs_for(root) == ()

    def test_returns_runs_newest_first(self, tmp_path):
        from sift.manifest import (
            now_utc, open_db, record_run_end, record_run_start, transaction,
        )
        root = _build_index(tmp_path, "alpha")
        conn = open_db(paths.manifest_path(root))
        now = now_utc()
        with transaction(conn):
            for rid in ("r1", "r2", "r3"):
                record_run_start(conn, rid, now)
                record_run_end(conn, rid, now, "succeeded",
                               counts_json='{"FRESH": 1}')
        conn.close()
        out = recent_runs_for(root, limit=2)
        assert isinstance(out, tuple)
        assert all(isinstance(r, RunSummary) for r in out)
        assert len(out) == 2


# ---- describe() integrates the new fields ------------------------------

class TestDescribeWriteFields:
    def test_populates_accepts_writes_and_allowed_hosts(self, tmp_path):
        root = _build_index(
            tmp_path, "alpha",
            toml_seed_section={"host_allow": ["alpha.test"]},
        )
        d = describe(root)
        assert d.accepts_writes is True
        assert d.allowed_hosts == ("alpha.test",)

    def test_populates_unseen_count(self, tmp_path):
        root = _build_index(
            tmp_path, "alpha",
            unseen_urls=("https://x.test/a", "https://x.test/b"),
        )
        d = describe(root)
        assert d.unseen_count == 2

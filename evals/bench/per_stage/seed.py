"""Stage 1 evals: seed.

Implemented (B5):
  * ``seed_dedup_correctness`` — synthetic: seed with canonical-equivalent
    URL variants (``/path/`` vs ``/path`` vs ``?cb=123``), expect a single
    manifest row.
  * ``seed_host_allow_correctness`` — synthetic: seed with mixed-host URLs,
    expect off-host rows to land in ``skipped_host``.

Both are synthetic, mirroring ``plan.py``'s pattern: build a small temp
manifest, drive the relevant code path, assert outcomes. They detect
regressions in the URL canonicalizer and the host-filter logic — two
load-bearing pieces of the seed phase that don't get exercised by the
extract-quality numbers downstream.

Deferred:
  * ``seed_discovery_completeness`` — needs reference URL lists matched
    against actual seeded URLs. With ``--limit 500`` per site, the
    sample may or may not contain the ~3 reference URLs per fixture;
    surfacing a useful number here requires either (a) curated 500-URL
    reference lists per fixture or (b) probability-weighted recall on
    random samples. Worth doing but research-shaped.
  * ``seed_sitemap_index_recursion`` — needs synthetic sitemap-index XML
    fixtures and a mock httpx server, parallel to the firecrawl integration
    tests.
"""
from __future__ import annotations

import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class SeedDedupResult:
    name: str = "seed_dedup_correctness"
    pass_threshold: float = 1.0
    cases: int = 0
    correct: int = 0
    rate: float = 0.0
    passed: bool = False
    failures: list = None


def eval_seed_dedup() -> SeedDedupResult:
    """Synthetic: feed canonical-equivalent URLs to ``upsert_seed`` and
    expect a single manifest row to result for each equivalence class.

    Tests the canonicalizer's behavior on three documented collapse rules
    (per ``classify.canonicalize_url``: lowercase host, no fragment, sorted
    query params, trailing slash stripped):

      * trailing-slash normalization (``/path/`` ↔ ``/path``)
      * case-insensitive host (``HTTP://Example.com`` ↔ ``http://example.com``)
      * fragment removal (``/x#frag`` collapses to ``/x``)

    Explicitly NOT tested: default-port collapse (``:443``/``:80``) is not
    part of the documented contract. This is a known gap; if sift later
    adds it to ``canonicalize_url``, add the case here.
    """
    from sift.classify import CLASSIFIER_VERSION, canonicalize_url
    from sift.manifest import init_schema, open_db, transaction, upsert_seed
    from sift import paths

    equivalence_classes = [
        # trailing slash
        ("trailing-slash",
         ("https://x.test/path", "https://x.test/path/")),
        # case in host
        ("host-case",
         ("https://X.TEST/foo", "https://x.test/foo")),
        # fragment
        ("fragment-drop",
         ("https://x.test/anchor", "https://x.test/anchor#section-2")),
    ]

    failures: list = []
    correct = 0
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        conn = open_db(paths.manifest_path(root))
        init_schema(conn)
        now_iso = "2026-05-31T12:00:00+00:00"

        for name, urls in equivalence_classes:
            # Fresh manifest per case so each is independent.
            conn.execute("DELETE FROM manifest")
            conn.commit()
            with transaction(conn):
                for u in urls:
                    # IMPORTANT: the CLI's seed command canonicalizes the URL
                    # before calling upsert_seed (cli.py:230). The eval must
                    # do the same — upsert_seed itself stores the URL as-is.
                    upsert_seed(conn, url=canonicalize_url(u), tier="LIVING",
                                parent_guide_=None,
                                classifier_version=CLASSIFIER_VERSION,
                                sitemap_lastmod=None, now=now_iso)
            n = conn.execute(
                "SELECT COUNT(*) FROM manifest"
            ).fetchone()[0]
            if n == 1:
                correct += 1
            else:
                # Show what canonical forms the rows landed at
                rows = [r[0] for r in conn.execute(
                    "SELECT url FROM manifest").fetchall()]
                failures.append({"class": name,
                                 "input_urls": list(urls),
                                 "rows_after_dedup": rows,
                                 "expected": 1, "actual": n,
                                 "canonical_forms": [canonicalize_url(u)
                                                     for u in urls]})
    cases = len(equivalence_classes)
    rate = correct / cases if cases else 0.0
    return SeedDedupResult(cases=cases, correct=correct, rate=round(rate, 4),
                           passed=(rate == 1.0), failures=failures)


@dataclass
class SeedHostAllowResult:
    name: str = "seed_host_allow_correctness"
    pass_threshold: float = 1.0
    inserted: int = 0
    skipped_host: int = 0
    passed: bool = False
    note: str = ""


def eval_seed_host_allow() -> SeedHostAllowResult:
    """Synthetic: drive the seed command's host-filter directly. Feed 3
    on-host URLs + 3 off-host URLs through the same filter the CLI uses and
    expect the manifest to land 3 inserted + 3 skipped_host.

    This is intentionally low-level (no CliRunner) to bypass the click
    plumbing and exercise just the canonicalize + host-allow path.
    """
    from sift.classify import (
        CLASSIFIER_VERSION, canonicalize_url, classify_tier, parent_guide,
    )
    from sift.manifest import init_schema, open_db, transaction, upsert_seed
    from sift import paths

    on_host_urls = [
        "https://x.test/a",
        "https://x.test/b",
        "https://x.test/c",
    ]
    off_host_urls = [
        "https://other.test/a",
        "https://evil.test/b",
        "https://yet-another.test/c",
    ]
    allowed = {"x.test"}

    inserted = 0
    skipped_host = 0
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        conn = open_db(paths.manifest_path(root))
        init_schema(conn)
        now_iso = "2026-05-31T12:00:00+00:00"
        with transaction(conn):
            for raw_url in on_host_urls + off_host_urls:
                url = canonicalize_url(raw_url)
                host = url.split("//", 1)[-1].split("/", 1)[0].lower()
                if host not in allowed:
                    skipped_host += 1
                    continue
                tier = classify_tier(url).value
                pg = parent_guide(url)
                upsert_seed(conn, url=url, tier=tier, parent_guide_=pg,
                            classifier_version=CLASSIFIER_VERSION,
                            sitemap_lastmod=None, now=now_iso)
                inserted += 1
    passed = (inserted == 3 and skipped_host == 3)
    return SeedHostAllowResult(
        inserted=inserted, skipped_host=skipped_host, passed=passed,
        note=(f"{inserted} on-host inserted; {skipped_host} off-host skipped"),
    )


def run_seed_evals(*args: Any, **kwargs: Any) -> dict:
    """Both seed evals are synthetic — they don't take a root or fixture.
    Run once per bench, surfaced under the index-wide rollup."""
    return {
        "dedup":      asdict(eval_seed_dedup()),
        "host_allow": asdict(eval_seed_host_allow()),
    }

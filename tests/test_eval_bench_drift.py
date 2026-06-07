"""Tests for ``evals.bench.drift`` — B6 drift detection.

Covers the four severity bands (status, eval_flip, rate, count) plus
membership changes, the regression vs improvement classification, and
the markdown renderer's correctness on representative inputs.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.bench.drift import (
    Delta,
    SEVERITY_ORDER,
    compute_drift,
    load_suite,
    render_markdown,
)


# ---- fixture builders -----------------------------------------------------

def _result(
    slug: str,
    *,
    use_case: str = "x",
    host: str = "example.test",
    status: str = "published",
    seeded: int = 100,
    fetched: int = 95,
    md_count: int = 95,
    md_mean_bytes: int = 5000,
    firecrawl_credits: int = 0,
    stages: dict | None = None,
) -> dict:
    return {
        "use_case": use_case, "slug": slug, "host": host,
        "discovery": "sitemap", "status": status,
        "seeded": seeded, "fetched": fetched, "md_count": md_count,
        "md_mean_bytes": md_mean_bytes,
        "firecrawl_credits": firecrawl_credits,
        "wall_seconds": 1.0, "notes": [], "error": None,
        "bench": {"stages": stages or {}},
    }


def _eval(rate: float, passed: bool, name: str = "x") -> dict:
    return {"name": name, "rate": rate, "passed": passed}


def _suite(*results: dict) -> dict:
    return {
        "config": {"limit": 100},
        "total_wall_seconds": 1.0,
        "fixtures_attempted": len(results),
        "results": list(results),
    }


# ---- status changes --------------------------------------------------------

class TestStatusDelta:
    def test_published_to_failed_is_regression(self):
        b = _suite(_result("a", status="published"))
        c = _suite(_result("a", status="failed"))
        d = compute_drift(b, c)
        assert d.regressions == 1 and d.improvements == 0
        status_deltas = [x for x in d.deltas if x.severity == "status"]
        assert len(status_deltas) == 1
        assert status_deltas[0].is_regression is True
        assert "published" in status_deltas[0].note
        assert "failed" in status_deltas[0].note

    def test_failed_to_published_is_improvement(self):
        b = _suite(_result("a", status="failed"))
        c = _suite(_result("a", status="published"))
        d = compute_drift(b, c)
        assert d.improvements == 1 and d.regressions == 0
        assert d.deltas[0].is_regression is False

    def test_same_status_yields_no_status_delta(self):
        b = _suite(_result("a", status="published"))
        c = _suite(_result("a", status="published"))
        d = compute_drift(b, c)
        # No status delta surfaces (other count/eval matches may, but here both sides are identical).
        assert not any(x.kind == "status" for x in d.deltas)


# ---- eval pass/fail flips --------------------------------------------------

class TestEvalFlip:
    def test_pass_to_fail_is_regression(self):
        b = _suite(_result("a", stages={
            "3_fetch": {"success": _eval(0.99, True)}
        }))
        c = _suite(_result("a", stages={
            "3_fetch": {"success": _eval(0.99, False)}
        }))
        d = compute_drift(b, c)
        flips = [x for x in d.deltas if x.severity == "eval_flip"]
        assert len(flips) == 1
        assert flips[0].is_regression is True
        assert flips[0].kind == "3_fetch.success"

    def test_fail_to_pass_is_improvement(self):
        b = _suite(_result("a", stages={
            "3_fetch": {"success": _eval(0.50, False)}
        }))
        c = _suite(_result("a", stages={
            # Big rate jump too — but we're scoping this test to the flip
            # specifically; rate delta is its own bucket.
            "3_fetch": {"success": _eval(0.99, True)}
        }))
        d = compute_drift(b, c)
        flips = [x for x in d.deltas if x.severity == "eval_flip"]
        assert len(flips) == 1
        assert flips[0].is_regression is False


# ---- rate deltas -----------------------------------------------------------

class TestRateDelta:
    def test_below_threshold_ignored(self):
        b = _suite(_result("a", stages={
            "3_fetch": {"success": _eval(0.90, True)}
        }))
        c = _suite(_result("a", stages={
            "3_fetch": {"success": _eval(0.92, True)}  # +0.02, below 0.05 eps
        }))
        d = compute_drift(b, c)
        assert not any(x.severity == "rate" for x in d.deltas)

    def test_above_threshold_drop_is_regression(self):
        b = _suite(_result("a", stages={
            "3_fetch": {"success": _eval(0.95, True)}
        }))
        c = _suite(_result("a", stages={
            "3_fetch": {"success": _eval(0.80, True)}
        }))
        d = compute_drift(b, c)
        rates = [x for x in d.deltas if x.severity == "rate"]
        assert len(rates) == 1
        assert rates[0].is_regression is True

    def test_above_threshold_gain_is_improvement(self):
        b = _suite(_result("a", stages={
            "3_fetch": {"success": _eval(0.50, False)}
        }))
        c = _suite(_result("a", stages={
            "3_fetch": {"success": _eval(0.70, False)}
        }))
        d = compute_drift(b, c)
        rates = [x for x in d.deltas if x.severity == "rate"]
        assert len(rates) == 1
        assert rates[0].is_regression is False

    def test_custom_eps_changes_sensitivity(self):
        b = _suite(_result("a", stages={
            "3_fetch": {"success": _eval(0.90, True)}
        }))
        c = _suite(_result("a", stages={
            "3_fetch": {"success": _eval(0.88, True)}  # -0.02
        }))
        d = compute_drift(b, c, rate_eps=0.01)        # tighter eps
        assert any(x.severity == "rate" for x in d.deltas)


# ---- count deltas ----------------------------------------------------------

class TestCountDelta:
    def test_small_change_ignored(self):
        b = _suite(_result("a", seeded=100, fetched=95, md_count=95))
        c = _suite(_result("a", seeded=105, fetched=100, md_count=100))
        d = compute_drift(b, c)
        assert not any(x.severity == "count" for x in d.deltas)

    def test_large_drop_is_regression(self):
        b = _suite(_result("a", seeded=100, fetched=95, md_count=95))
        c = _suite(_result("a", seeded=50, fetched=45, md_count=45))
        d = compute_drift(b, c)
        counts = [x for x in d.deltas if x.severity == "count"]
        assert {x.kind for x in counts} == {"seeded", "fetched", "md_count"}
        assert all(x.is_regression for x in counts)

    def test_growth_is_improvement(self):
        b = _suite(_result("a", seeded=100, fetched=95, md_count=95))
        c = _suite(_result("a", seeded=200, fetched=190, md_count=190))
        d = compute_drift(b, c)
        counts = [x for x in d.deltas if x.severity == "count"]
        # All three count fields cross the +20% threshold.
        assert all(not x.is_regression for x in counts)

    def test_zero_both_sides_skipped(self):
        b = _suite(_result("a", seeded=0, fetched=0, md_count=0))
        c = _suite(_result("a", seeded=0, fetched=0, md_count=0))
        d = compute_drift(b, c)
        assert not any(x.severity == "count" for x in d.deltas)


# ---- membership changes ----------------------------------------------------

class TestMembership:
    def test_removed_fixture(self):
        b = _suite(_result("a"), _result("b"))
        c = _suite(_result("a"))
        d = compute_drift(b, c)
        rem = [x for x in d.deltas if x.severity == "membership"]
        assert len(rem) == 1
        assert rem[0].kind == "removed"
        assert rem[0].slug == "b"

    def test_added_fixture(self):
        b = _suite(_result("a"))
        c = _suite(_result("a"), _result("b"))
        d = compute_drift(b, c)
        add = [x for x in d.deltas if x.severity == "membership"]
        assert len(add) == 1
        assert add[0].kind == "added"

    def test_membership_excluded_from_regression_count(self):
        b = _suite(_result("a"))
        c = _suite(_result("b"))  # entirely different slug
        d = compute_drift(b, c)
        assert d.regressions == 0
        assert d.improvements == 0
        assert d.membership_changes == 2


# ---- file I/O + integration ------------------------------------------------

class TestLoadSuite:
    def test_round_trip(self, tmp_path):
        suite = _suite(_result("a"))
        p = tmp_path / "suite.json"
        p.write_text(json.dumps(suite))
        loaded = load_suite(p)
        assert loaded["results"][0]["slug"] == "a"

    def test_missing_results_raises(self, tmp_path):
        p = tmp_path / "broken.json"
        p.write_text(json.dumps({"config": {}}))
        with pytest.raises(ValueError, match="missing 'results'"):
            load_suite(p)


# ---- renderer --------------------------------------------------------------

class TestRender:
    def test_empty_drift_says_so(self):
        b = _suite(_result("a"))
        c = _suite(_result("a"))
        md = render_markdown(compute_drift(b, c))
        assert "No drift detected" in md

    def test_renders_each_severity_section(self):
        b = _suite(_result("a", status="published", seeded=100, stages={
            "3_fetch": {"success": _eval(0.95, True)}
        }))
        c = _suite(
            _result("a", status="failed", seeded=10, stages={
                "3_fetch": {"success": _eval(0.50, False)}
            }),
            _result("z"),  # added
        )
        d = compute_drift(b, c)
        md = render_markdown(d)
        # All four real-change sections + membership
        for heading in ("Status changes", "Eval pass/fail flips",
                        "Rate deltas", "Count deltas",
                        "Fixture set membership"):
            assert heading in md
        assert "REGRESSION" in md  # tag for the published→failed change


# ---- sanity check: severity ordering is stable -----------------------------

def test_severity_order_constants():
    # Order matters — drift output sorts by index in this tuple.
    assert SEVERITY_ORDER[0] == "status"
    assert SEVERITY_ORDER[-1] == "membership"

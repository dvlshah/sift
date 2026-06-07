"""B3 evals: plan decision correctness + fetch conditional-GET efficiency."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.bench.fixtures.sites import POSITIVE_FIXTURES, USE_CASES
from evals.bench.per_stage.plan import eval_plan_decision_correctness
from evals.bench.per_stage.fetch import eval_conditional_get


# ---- expanded-fixtures invariants ------------------------------------------

class TestExpandedFixtures:
    def test_24_fixtures(self):
        assert len(POSITIVE_FIXTURES) == 24

    def test_four_per_use_case(self):
        from collections import Counter
        per = Counter(f.use_case for f in POSITIVE_FIXTURES)
        for uc in USE_CASES:
            assert per[uc] == 4, f"{uc} has {per[uc]} fixtures, expected 4"

    def test_no_duplicate_slugs(self):
        slugs = [f.slug for f in POSITIVE_FIXTURES]
        assert len(slugs) == len(set(slugs))

    def test_no_duplicate_hosts(self):
        hosts = [f.host for f in POSITIVE_FIXTURES]
        assert len(hosts) == len(set(hosts))


# ---- plan_decision_correctness ---------------------------------------------

class TestPlanDecisionCorrectness:
    def test_all_six_synthetic_cases_pass(self):
        """The eval IS its own ground truth — it builds a manifest with known
        states and asserts the planner emits the expected decision for each.
        If this ever fails, either the planner regressed or our understanding
        of the planner is wrong; both are bugs worth catching."""
        r = eval_plan_decision_correctness()
        assert r.n_cases == 6
        assert r.rate == 1.0, (
            f"plan decision correctness failed: {r.failures}"
        )
        assert r.passed is True

    def test_result_shape_is_json_serializable(self):
        from dataclasses import asdict
        r = eval_plan_decision_correctness()
        json.dumps(asdict(r), default=str)


# ---- conditional_get_efficiency --------------------------------------------

class TestConditionalGetEval:
    def test_no_plan_returns_safe_zero(self, tmp_path):
        """No plan.jsonl yet → eval reports gracefully, doesn't crash."""
        r = eval_conditional_get(tmp_path, "nonexistent-run-id")
        assert r.efficiency == 0.0
        assert r.passed is False
        assert "missing" in r.note

    def test_counts_304s_for_conditional_decisions(self, tmp_path):
        """Build a tiny plan.jsonl + fetch.log fixture and verify the eval
        counts intersections correctly."""
        from sift import paths
        run_id = "test-run"
        plan_path = paths.plan_path(tmp_path, run_id)
        fetch_log = paths.fetch_log_path(tmp_path, run_id)
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        fetch_log.parent.mkdir(parents=True, exist_ok=True)

        # Plan: 3 FETCH_CONDITIONAL, 1 FETCH (excluded from denominator)
        plan_path.write_text(
            json.dumps({"url": "https://x.test/a", "decision": "FETCH_CONDITIONAL"}) + "\n"
            + json.dumps({"url": "https://x.test/b", "decision": "FETCH_CONDITIONAL"}) + "\n"
            + json.dumps({"url": "https://x.test/c", "decision": "FETCH_CONDITIONAL"}) + "\n"
            + json.dumps({"url": "https://x.test/d", "decision": "FETCH"}) + "\n"
        )
        # Fetch log: 2 of the 3 conditional URLs returned 304, the other 200
        fetch_log.write_text(
            json.dumps({"url": "https://x.test/a", "status": 304}) + "\n"
            + json.dumps({"url": "https://x.test/b", "status": 304}) + "\n"
            + json.dumps({"url": "https://x.test/c", "status": 200}) + "\n"
            + json.dumps({"url": "https://x.test/d", "status": 200}) + "\n"
        )
        r = eval_conditional_get(tmp_path, run_id)
        assert r.fetch_conditional_decisions == 3
        assert r.not_modified_responses == 2
        assert r.efficiency == pytest.approx(2 / 3, abs=0.001)
        # 67% < 80% threshold → not passed
        assert r.passed is False

    def test_passes_above_threshold(self, tmp_path):
        from sift import paths
        run_id = "test-run-ok"
        plan_path = paths.plan_path(tmp_path, run_id)
        fetch_log = paths.fetch_log_path(tmp_path, run_id)
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        fetch_log.parent.mkdir(parents=True, exist_ok=True)

        # 5/5 conditional decisions → 304: efficiency = 100%
        plan_lines = [
            json.dumps({"url": f"https://x.test/{i}",
                        "decision": "FETCH_CONDITIONAL"}) + "\n"
            for i in range(5)
        ]
        plan_path.write_text("".join(plan_lines))
        fetch_lines = [
            json.dumps({"url": f"https://x.test/{i}", "status": 304}) + "\n"
            for i in range(5)
        ]
        fetch_log.write_text("".join(fetch_lines))
        r = eval_conditional_get(tmp_path, run_id)
        assert r.efficiency == 1.0
        assert r.passed is True

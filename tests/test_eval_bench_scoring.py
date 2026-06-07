"""Smoke + correctness tests for the eval bench scoring helpers.

These have no network dep — they run in CI. We don't test the orchestrator
end-to-end (that needs a real index); the per-stage / runner integration is
covered by the live `sift-evals bench` run against test-index.
"""
from __future__ import annotations

import pytest

from evals.bench.scoring.structural import (
    count_html_elements, count_md_elements, preservation_score,
)
from evals.bench.scoring.fidelity import (
    edit_distance, normalized_edit_distance, unigram_jaccard,
    ngram_overlap, fidelity_score,
)
from evals.bench.scoring.use_case import (
    score_use_case_patterns, aggregate_use_case_score,
)
from evals.bench.fixtures.sites import POSITIVE_FIXTURES, by_slug, USE_CASES


# ---- structural ------------------------------------------------------------

class TestStructural:
    def test_html_element_counts(self):
        html = """
            <h1>Title</h1>
            <h2>Sub</h2>
            <p>para with <a href="x">link</a> and <a href="y">link2</a></p>
            <table><tr><td>x</td></tr></table>
            <ul><li>a</li><li>b</li></ul>
            <pre><code>print()</code></pre>
        """
        c = count_html_elements(html)
        assert c["heading"] == 2
        assert c["table"] == 1
        assert c["list"] == 1
        assert c["code_block"] == 1
        assert c["link"] == 2

    def test_md_element_counts(self):
        md = """# Title

## Sub

para with [link](https://x).

| a | b |
|---|---|
| 1 | 2 |

- one
- two

```python
print()
```
"""
        c = count_md_elements(md)
        assert c["heading"] == 2
        assert c["table"] >= 1   # at least one row
        assert c["list"] == 2
        assert c["code_block"] == 1
        assert c["link"] == 1

    def test_preservation_ratio(self):
        html = "<h1>A</h1><h2>B</h2><h3>C</h3><a href='x'>L</a>"
        md = "# A\n## B\n[L](x)"
        score = preservation_score(html, md)
        assert score.ratios["heading"] == pytest.approx(2 / 3)
        assert score.ratios["link"] == 1.0
        assert score.html_counts["table"] == 0
        assert score.ratios["table"] == 1.0   # vacuously preserved

    def test_mean_ratio_excludes_vacuous_types(self):
        # html has only links, md preserves all. Mean should be 1.0,
        # not (1.0 + 1.0 + 1.0 + 1.0 + 1.0)/5 — the vacuous ones don't
        # contribute to the *active* mean.
        html = "<a href='x'>L</a>"
        md = "[L](x)"
        score = preservation_score(html, md)
        assert score.mean_ratio == 1.0


# ---- fidelity --------------------------------------------------------------

class TestFidelity:
    def test_edit_distance_identical(self):
        assert edit_distance("abc", "abc") == 0

    def test_edit_distance_known(self):
        assert edit_distance("kitten", "sitting") == 3
        assert edit_distance("", "abc") == 3
        assert edit_distance("abc", "") == 3

    def test_norm_edit_in_unit_interval(self):
        assert 0 <= normalized_edit_distance("abc", "xyz") <= 1
        assert normalized_edit_distance("abc", "abc") == 0.0

    def test_unigram_jaccard(self):
        assert unigram_jaccard("the cat sat", "the cat sat") == 1.0
        assert unigram_jaccard("the cat", "the dog") == 1 / 3   # union={the,cat,dog}, intersect={the}
        assert unigram_jaccard("abc def", "") == 0.0

    def test_ngram_overlap_bigram(self):
        # ("the","cat") + ("cat","sat") = 2 bigrams in each; full overlap
        assert ngram_overlap("the cat sat", "the cat sat", n=2) == 1.0
        # No bigram overlap
        assert ngram_overlap("a b c", "d e f", n=2) == 0.0

    def test_composite_in_unit_interval(self):
        s = fidelity_score("hello world", "hello there")
        assert 0 <= s.composite <= 1


# ---- use_case patterns -----------------------------------------------------

class TestUseCase:
    def test_currency_pattern_preserved(self):
        html = "<p>Amount: $1,200 due by EOY</p>"
        md = "Amount: $1,200 due by EOY"
        rows = score_use_case_patterns(html, md, (r"\$[\d,]+",))
        assert len(rows) == 1
        assert rows[0].html_count == 1
        assert rows[0].md_count == 1
        assert rows[0].ratio == 1.0

    def test_pattern_with_zero_html_count_vacuous(self):
        html = "<p>no currency here</p>"
        md = "no currency here"
        rows = score_use_case_patterns(html, md, (r"\$[\d,]+",))
        assert rows[0].html_count == 0
        assert rows[0].ratio == 1.0
        assert aggregate_use_case_score(rows) == 0.0  # no active patterns

    def test_aggregate_mean_of_active(self):
        # Two patterns: one fully preserved, one partially. Mean of active = 0.75
        html = "<p>Section 8 says $100 must be paid</p>"
        md = "Section 8 says paid"  # currency stripped; section ref kept
        rows = score_use_case_patterns(
            html, md, (r"\$\d+", r"Section \d+")
        )
        assert rows[0].ratio == 0.0       # currency lost
        assert rows[1].ratio == 1.0       # section kept
        assert aggregate_use_case_score(rows) == 0.5


# ---- fixtures --------------------------------------------------------------

class TestFixtures:
    def test_24_positive_fixtures(self):
        # Expanded from 12 → 24 in the B3 PR to give the bench real-world
        # depth + variety across the 6 use cases.
        assert len(POSITIVE_FIXTURES) == 24

    def test_six_use_cases(self):
        assert len(USE_CASES) == 6

    def test_each_fixture_has_reference_urls(self):
        for f in POSITIVE_FIXTURES:
            assert f.reference_urls, f"{f.slug} has no reference URLs"
            assert len(f.reference_urls) >= 3, (
                f"{f.slug} should have ≥ 3 reference URLs for sampling"
            )

    def test_by_slug_round_trip(self):
        assert by_slug("ato") is not None
        assert by_slug("nonexistent-slug") is None

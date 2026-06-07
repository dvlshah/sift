"""LLM-as-judge for extraction fidelity.

For a stratified sample, ask Claude to score how faithfully the extracted
markdown represents the source HTML on six axes (1-5):
  * title accuracy
  * body coverage
  * heading preservation
  * table preservation
  * link preservation
  * overall faithfulness

Design notes:
  * Model: claude-opus-4-7 (per claude-api skill default)
  * Adaptive thinking + effort=medium — fidelity scoring isn't compute-heavy
  * Prompt caching on the rubric (stable across all 50 calls) — should give
    near-90% cache reads after the first request
  * Structured output via messages.parse() with Pydantic — guarantees the
    scores parse cleanly into our dataclass
  * Streaming to avoid SDK HTTP timeout on large HTML inputs
  * Per-call: input ~30K tokens (mostly HTML) + ~1K output → ~$0.16/call
    at Opus 4.7 prices; 50-call run ≈ $8 first time, ~$1 with cache reads
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from sift import paths
from sift.fetch import read_raw_blob

from .sampler import sample_by_count


JUDGE_MODEL = "claude-opus-4-7"
JUDGE_EFFORT = "medium"  # fidelity scoring doesn't need max effort
MAX_HTML_CHARS = 80_000  # ~20K tokens of HTML — keeps prompts cacheable + bounded
MAX_MD_CHARS = 30_000


# Frozen system prompt — cached via prompt-caching breakpoint.
JUDGE_RUBRIC = """You are an expert evaluator of HTML-to-markdown extraction quality.

You will be given:
  1. The source HTML body of a web page (boilerplate like nav/footer/scripts already stripped)
  2. The extracted markdown body, produced by trafilatura

Score the extraction on a 1-5 scale for each axis. 1 = unusable, 5 = faithful.
If a dimension does not apply (e.g. no tables in the source), set that field to null
and explain in the issues list.

Scoring rubric per axis:
  * **title_accuracy** — does the markdown's first heading (or a clear title) match the page's main topic?
  * **body_coverage** — what fraction of the source's substantive content is preserved? (5 = nearly all; 1 = mostly missing)
  * **heading_preservation** — are HTML headings represented at correct levels in markdown?
  * **table_preservation** — are HTML tables preserved as markdown tables with correct rows/columns? (null if no tables)
  * **link_preservation** — are the source's non-navigation links present in markdown? (null if very few links exist)
  * **overall_faithfulness** — your single-number judgment of whether this extraction would let a downstream agent answer questions about the page accurately

Return a single JSON object matching the response schema. The `issues` field is for
specific problems you observed — be concise and concrete, e.g.:
  * "Two h2 sections under '2. Lodging' are missing"
  * "Rate table for FY 2024-25 collapsed to plain text"
  * "Footer copyright leaked into body"

If extraction is excellent, return an empty issues list. Don't pad the list with positives.
"""


class FidelityScore(BaseModel):
    """Schema for the model's per-page judgment. messages.parse() validates this."""
    title_accuracy:        int = Field(..., ge=1, le=5)
    body_coverage:         int = Field(..., ge=1, le=5)
    heading_preservation:  int = Field(..., ge=1, le=5)
    table_preservation:    Optional[int] = Field(None, ge=1, le=5)
    link_preservation:     Optional[int] = Field(None, ge=1, le=5)
    overall_faithfulness:  int = Field(..., ge=1, le=5)
    issues:                list[str] = Field(default_factory=list)


@dataclass
class PageJudgment:
    url: str
    tier: str
    score: dict  # FidelityScore.model_dump()
    judge_input_tokens: int
    judge_cache_read_tokens: int
    judge_cache_write_tokens: int
    judge_output_tokens: int
    judge_latency_sec: float
    error: Optional[str] = None


@dataclass
class LLMJudgeMetrics:
    run_id: str
    judge_model: str = JUDGE_MODEL
    sample_size: int = 0
    pages_judged: int = 0
    pages_skipped: int = 0
    pages_errored: int = 0

    # Aggregate scores (mean across pages, 1-5)
    mean_title_accuracy: float = 0.0
    mean_body_coverage: float = 0.0
    mean_heading_preservation: float = 0.0
    mean_table_preservation: Optional[float] = None
    mean_link_preservation: Optional[float] = None
    mean_overall_faithfulness: float = 0.0

    # Per-tier overall scores
    overall_by_tier: dict[str, float] = field(default_factory=dict)

    # Distribution of overall scores (1-5 → count)
    overall_distribution: dict[int, int] = field(default_factory=dict)

    # Cost telemetry
    total_input_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_write_tokens: int = 0
    total_output_tokens: int = 0
    estimated_cost_usd: float = 0.0  # Opus 4.7: $5/$25 per Mt, cached reads ~10%
    wall_sec: float = 0.0

    # Per-page details (low-scoring pages get prioritized for the example list)
    low_scoring_examples: list[PageJudgment] = field(default_factory=list)


def _build_user_prompt(html_text: str, md_body: str, url: str) -> str:
    """The user-turn content. The HTML + MD vary per call so this part is NOT cached;
    the rubric in system is cached separately."""
    return (
        f"## Page URL\n{url}\n\n"
        f"## Source HTML body (boilerplate stripped, truncated to {MAX_HTML_CHARS} chars)\n\n"
        f"```html\n{html_text[:MAX_HTML_CHARS]}\n```\n\n"
        f"## Extracted markdown body (frontmatter stripped, truncated to {MAX_MD_CHARS} chars)\n\n"
        f"```markdown\n{md_body[:MAX_MD_CHARS]}\n```\n\n"
        "Score the extraction per the rubric and return the JSON response."
    )


def _strip_frontmatter(md_text: str) -> str:
    if md_text.startswith("---\n"):
        end = md_text.find("\n---\n", 4)
        if end != -1:
            return md_text[end + 5:]
    return md_text


def _strip_html_boilerplate(html: bytes) -> str:
    """Drop <script>/<style>/<nav>/etc. and return inner-body text+structure."""
    try:
        from selectolax.lexbor import LexborHTMLParser
        tree = LexborHTMLParser(html)
        body = tree.body or tree
        for node in body.css("script, style, nav, footer, header, aside, noscript"):
            node.decompose()
        return body.html or ""
    except Exception:
        try:
            return html.decode("utf-8", errors="replace")
        except Exception:
            return ""


def _cost(in_tok: int, cache_read: int, cache_write: int, out_tok: int) -> float:
    """Opus 4.7 pricing as of this skill version:
       $5/Mt input, $25/Mt output, cache reads ~10% input, cache writes ~125% input."""
    return round(
        (in_tok * 5e-6)
        + (cache_read * 0.5e-6)
        + (cache_write * 6.25e-6)
        + (out_tok * 25e-6),
        4,
    )


def run(
    root: Path,
    run_id: str,
    *,
    conn: sqlite3.Connection,
    sample: int = 30,
    api_key: Optional[str] = None,
) -> LLMJudgeMetrics:
    """Run the LLM-judge eval. Requires ANTHROPIC_API_KEY or explicit `api_key`."""
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError(
            "anthropic SDK not installed. Add to pyproject [project.optional-dependencies] "
            "as `evals = [\"anthropic>=0.40\", \"jsonschema>=4\"]` and reinstall."
        ) from e

    metrics = LLMJudgeMetrics(run_id=run_id)
    rows = sample_by_count(conn, sample, label="llm-judge", fresh_only=True)
    metrics.sample_size = len(rows)
    if not rows:
        return metrics

    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    scores_title: list[int] = []
    scores_body: list[int] = []
    scores_headings: list[int] = []
    scores_tables: list[int] = []
    scores_links: list[int] = []
    scores_overall: list[int] = []
    per_tier_overall: dict[str, list[int]] = {}
    judgments: list[PageJudgment] = []

    t_start = time.time()
    for i, row in enumerate(rows, 1):
        if not row.raw_hash:
            metrics.pages_skipped += 1
            continue
        try:
            html = read_raw_blob(root, row.raw_hash)
        except (FileNotFoundError, OSError):
            metrics.pages_skipped += 1
            continue
        md_path = paths.md_path(root, run_id, row.url)
        if not md_path.exists():
            metrics.pages_skipped += 1
            continue
        html_text = _strip_html_boilerplate(html)
        md_text = _strip_frontmatter(md_path.read_text(encoding="utf-8", errors="replace"))
        user_prompt = _build_user_prompt(html_text, md_text, row.url)

        t_call = time.time()
        try:
            # System block carries cache_control so the rubric stays cached
            # across all N calls in this eval run.
            response = client.messages.parse(
                model=JUDGE_MODEL,
                max_tokens=2048,
                thinking={"type": "adaptive"},
                output_config={"effort": JUDGE_EFFORT},
                system=[{
                    "type": "text",
                    "text": JUDGE_RUBRIC,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": user_prompt}],
                output_format=FidelityScore,
            )
            score: FidelityScore = response.parsed_output
            latency = time.time() - t_call
            usage = response.usage
            judgment = PageJudgment(
                url=row.url, tier=row.tier,
                score=score.model_dump(),
                judge_input_tokens=usage.input_tokens or 0,
                judge_cache_read_tokens=usage.cache_read_input_tokens or 0,
                judge_cache_write_tokens=usage.cache_creation_input_tokens or 0,
                judge_output_tokens=usage.output_tokens or 0,
                judge_latency_sec=round(latency, 2),
            )
            judgments.append(judgment)
            metrics.pages_judged += 1

            scores_title.append(score.title_accuracy)
            scores_body.append(score.body_coverage)
            scores_headings.append(score.heading_preservation)
            if score.table_preservation is not None:
                scores_tables.append(score.table_preservation)
            if score.link_preservation is not None:
                scores_links.append(score.link_preservation)
            scores_overall.append(score.overall_faithfulness)
            per_tier_overall.setdefault(row.tier, []).append(score.overall_faithfulness)

            metrics.total_input_tokens       += judgment.judge_input_tokens
            metrics.total_cache_read_tokens  += judgment.judge_cache_read_tokens
            metrics.total_cache_write_tokens += judgment.judge_cache_write_tokens
            metrics.total_output_tokens      += judgment.judge_output_tokens

            # Progress hint to stderr (callers can swallow if undesired)
            print(f"  [{i}/{len(rows)}] {row.url[:80]} -> {score.overall_faithfulness}/5",
                  flush=True)
        except Exception as e:
            metrics.pages_errored += 1
            judgments.append(PageJudgment(
                url=row.url, tier=row.tier, score={},
                judge_input_tokens=0, judge_cache_read_tokens=0,
                judge_cache_write_tokens=0, judge_output_tokens=0,
                judge_latency_sec=0.0,
                error=str(e),
            ))

    metrics.wall_sec = round(time.time() - t_start, 2)

    def _mean(xs: list[int]) -> float:
        return round(sum(xs) / len(xs), 2) if xs else 0.0

    metrics.mean_title_accuracy = _mean(scores_title)
    metrics.mean_body_coverage = _mean(scores_body)
    metrics.mean_heading_preservation = _mean(scores_headings)
    metrics.mean_table_preservation = _mean(scores_tables) if scores_tables else None
    metrics.mean_link_preservation = _mean(scores_links) if scores_links else None
    metrics.mean_overall_faithfulness = _mean(scores_overall)
    metrics.overall_by_tier = {
        t: round(sum(s) / len(s), 2) for t, s in per_tier_overall.items()
    }
    metrics.overall_distribution = {
        k: scores_overall.count(k) for k in (1, 2, 3, 4, 5)
    }

    metrics.estimated_cost_usd = _cost(
        metrics.total_input_tokens,
        metrics.total_cache_read_tokens,
        metrics.total_cache_write_tokens,
        metrics.total_output_tokens,
    )

    # Low-scoring examples for triage
    judgments.sort(
        key=lambda j: j.score.get("overall_faithfulness", 5) if j.error is None else -1
    )
    metrics.low_scoring_examples = judgments[:15]
    return metrics


def to_dict(m: LLMJudgeMetrics) -> dict:
    return asdict(m)

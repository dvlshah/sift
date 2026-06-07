"""LLM judge for agent-loop answers.

Scores each ``(question, condition)`` answer on three axes:

  * **correctness (1-5)** — does the answer match the gold reference?
    1 = wrong / hallucinated, 5 = matches the gold answer in substance.
  * **citation_present (bool)** — did the agent include URLs at all?
    Sniffed by URL regex on the answer body, NOT judged by the model —
    that keeps the rate cheap and deterministic.
  * **citation_faithful (bool)** — for each URL the agent cited, does the
    gold reference list it (or share a host)? Same deterministic check,
    not a judge call.

Design choices:

  * Use ``messages.parse()`` with a Pydantic schema so we get a structured
    int score back without re-prompting. The schema doubles as runtime
    validation when the model returns an out-of-range integer.
  * Cache the rubric on a separate ``system`` block. The rubric is stable
    across all judge calls in one bench run; cache reads should hit on
    every call after the first.
  * The judge sees BOTH the gold answer and the agent's answer, plus the
    question, plus the citations the agent emitted. It does not see the
    full retrieved corpus — that would let it overrule a "wrong but
    confidently-stated" answer based on its own retrieval rather than
    grading against the gold rubric.

Rubric below is intentionally short. Long rubrics drift the model toward
the rubric examples and away from the gold answer; we want the gold
answer to dominate.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Optional

from pydantic import BaseModel, Field


JUDGE_MODEL = "claude-opus-4-7"


JUDGE_RUBRIC = """You are evaluating a research agent's answer to a single
question. You have:

  * the question text
  * a hand-curated gold reference answer (1-3 sentences)
  * the agent's full answer text
  * the URLs the agent cited (may be empty)

Score on a 1-5 integer scale:

  5 — Agent's answer matches the gold answer in substance and key facts.
      Minor stylistic differences are fine.
  4 — Mostly correct but missing one secondary detail, OR adds a small
      irrelevant claim alongside the correct one.
  3 — Partially correct: the main fact is right but a significant detail
      is wrong or absent.
  2 — Largely wrong: the agent named the right topic but the specifics
      contradict the gold answer.
  1 — Wrong, fabricated, or "I don't know" with no useful content.

Additional fields:

  * ``key_fact_match`` — true if the single most specific fact in the
    gold answer (the number, name, header, or rate that anchors it)
    appears correctly in the agent's answer. Helps the report tell apart
    "right vibe, wrong number" from "right number, missing context."
  * ``hallucinated_specifics`` — true if the agent stated a specific
    value (number, date, name) that contradicts the gold answer.
  * ``brief_reason`` — one short sentence explaining the score. Avoid
    repeating the answer back; just say what's right or wrong.

Be strict but fair. The gold answer is authoritative — if the agent says
something the gold answer doesn't support, that's a deduction even if it
sounds plausible.
"""


class JudgeScore(BaseModel):
    """Pydantic schema for ``messages.parse`` structured output."""
    correctness:            int = Field(..., ge=1, le=5)
    key_fact_match:         bool
    hallucinated_specifics: bool
    brief_reason:           str = Field(..., max_length=400)


@dataclass
class Judgment:
    qid: str
    condition: str
    correctness: int
    key_fact_match: bool
    hallucinated_specifics: bool
    citation_present: bool
    citation_faithful: bool
    brief_reason: str
    judge_input_tokens: int = 0
    judge_output_tokens: int = 0
    judge_cache_read_tokens: int = 0
    judge_cache_write_tokens: int = 0
    judge_latency_sec: float = 0.0
    error: Optional[str] = None


def _citation_metrics(answer_urls: list[str],
                      gold_urls: tuple[str, ...]) -> tuple[bool, bool]:
    """Return ``(citation_present, citation_faithful)``.

    A citation is faithful when the agent cited a URL whose host appears in
    the gold set, OR whose full URL matches a gold URL. Host-level matching
    is more forgiving than path-equality (the agent may cite a sub-page
    that the gold answer lives within); without it we'd underreport
    faithful citations on sites like docs.python.org where many pages
    answer the same question.
    """
    if not answer_urls:
        return False, False
    gold_hosts = {u.split("//", 1)[-1].split("/", 1)[0].lower()
                  for u in gold_urls}
    gold_exact = set(gold_urls)
    for u in answer_urls:
        if u in gold_exact:
            return True, True
        host = u.split("//", 1)[-1].split("/", 1)[0].lower()
        if host in gold_hosts:
            return True, True
    return True, False


def _build_user_prompt(question: str, gold_answer: str,
                       gold_urls: tuple[str, ...],
                       agent_answer: str,
                       agent_urls: list[str]) -> str:
    gold_list = "\n".join(f"  - {u}" for u in gold_urls) or "  (none)"
    agent_list = "\n".join(f"  - {u}" for u in agent_urls) or "  (none)"
    return (
        f"QUESTION:\n{question}\n\n"
        f"GOLD ANSWER:\n{gold_answer}\n\n"
        f"GOLD REFERENCE URLS:\n{gold_list}\n\n"
        f"AGENT ANSWER:\n{agent_answer}\n\n"
        f"AGENT CITED URLS:\n{agent_list}\n\n"
        "Return your judgment in the structured schema."
    )


def judge_answer(
    *,
    qid: str,
    condition: str,
    question: str,
    gold_answer: str,
    gold_urls: tuple[str, ...],
    agent_answer: str,
    agent_urls: list[str],
    model: str = JUDGE_MODEL,
    api_key: Optional[str] = None,
    client=None,
) -> Judgment:
    """Score one agent answer. Network-free when ``client`` is injected."""
    t0 = time.time()
    citation_present, citation_faithful = _citation_metrics(
        agent_urls, gold_urls
    )
    judgment = Judgment(
        qid=qid, condition=condition,
        correctness=0,
        key_fact_match=False,
        hallucinated_specifics=False,
        citation_present=citation_present,
        citation_faithful=citation_faithful,
        brief_reason="",
    )

    if client is None:
        try:
            import anthropic
        except ImportError as e:
            judgment.error = f"anthropic SDK not installed: {e}"
            judgment.judge_latency_sec = round(time.time() - t0, 2)
            return judgment
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            judgment.error = "ANTHROPIC_API_KEY not set"
            judgment.judge_latency_sec = round(time.time() - t0, 2)
            return judgment
        client = anthropic.Anthropic(api_key=key)

    try:
        resp = client.messages.parse(
            model=model,
            max_tokens=1024,
            system=[{
                "type": "text",
                "text": JUDGE_RUBRIC,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{
                "role": "user",
                "content": _build_user_prompt(
                    question, gold_answer, gold_urls,
                    agent_answer, agent_urls,
                ),
            }],
            output_format=JudgeScore,
        )
        score: JudgeScore = resp.parsed_output
        usage = getattr(resp, "usage", None)
        if usage is not None:
            judgment.judge_input_tokens = getattr(usage, "input_tokens", 0) or 0
            judgment.judge_output_tokens = getattr(usage, "output_tokens", 0) or 0
            judgment.judge_cache_read_tokens = (
                getattr(usage, "cache_read_input_tokens", 0) or 0
            )
            judgment.judge_cache_write_tokens = (
                getattr(usage, "cache_creation_input_tokens", 0) or 0
            )
        judgment.correctness = score.correctness
        judgment.key_fact_match = score.key_fact_match
        judgment.hallucinated_specifics = score.hallucinated_specifics
        judgment.brief_reason = score.brief_reason
    except Exception as e:
        judgment.error = f"judge call failed: {e}"
    finally:
        judgment.judge_latency_sec = round(time.time() - t0, 2)
    return judgment

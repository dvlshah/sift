"""Claude tool-use loop — one ``run_agent`` call drives one
``(question, condition)`` cell of the bench grid.

Loop invariants:

  * **Bounded turns.** ``MAX_TURNS`` caps tool-use iterations; the loop
    returns whatever the model produced on the last turn so partial work
    is still scored, not silently dropped. We've seen models in this kind
    of harness fall into "grep → grep → grep" cycles when the corpus
    doesn't contain the answer; the cap is the deadbolt.
  * **System prompt is condition-aware** but content-stable across all
    questions in a condition, so prompt caching kicks in after the first
    call and per-question marginal cost drops sharply.
  * **Final answer extraction**: we don't trust the model to produce a
    machine-readable answer block — instead, the last assistant
    text-content concatenation IS the answer. Citations are sniffed
    separately by URL-regexing the same body.
  * **Token accounting is per-turn**: usage from every API call is summed
    so the report can show real total spend, including tool-use round-trips,
    not just the final-turn output.

The loop is sync (not async) on purpose — questions are run sequentially
per condition; we don't gain much from parallelism here and async-with-
tool-use complicates retry/observability without a payoff at N=20.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from .tools import ToolSpec


MAX_TURNS = 8
MAX_TOKENS = 4096


@dataclass
class TurnUsage:
    """Per-turn usage breakdown (mirrors Anthropic's UsageBlock fields)."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class AgentResult:
    """One end-to-end agent run on one question under one condition.

    The ``answer`` is the verbatim concatenation of the model's final-turn
    text content; ``cited_urls`` is parsed out by URL regex on that same
    text. Tool-use spans (every tool_use block the model emitted) are
    preserved so the judge can also score "did the agent actually look at
    the gold pages."
    """
    qid: str
    condition: str
    model: str
    answer: str
    cited_urls: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    turns: int = 0
    stop_reason: str = ""
    refused: bool = False
    wall_seconds: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_write_tokens: int = 0
    error: Optional[str] = None


# URL extractor used to populate ``cited_urls``. Permissive on the trailing
# punctuation a model is likely to attach (``.``, ``)``, ``,`` etc.).
_URL_RE = re.compile(r"https?://[^\s)>,]+", flags=re.IGNORECASE)

# Bare strings the model returns when it refuses or punts. Matching this
# lets the judge tell apart "wrong" from "didn't try". Hand-tuned from
# Anthropic's safety templates + observed refusal phrasings.
_REFUSAL_MARKERS = (
    "i don't have information",
    "i cannot answer",
    "i can't answer",
    "i'm unable to",
    "i am unable to",
    "i don't know",
    "i do not know",
    "no information available",
    "cannot determine",
)


def _build_system_prompt(condition: str) -> list[dict]:
    """One frozen system prompt per condition, with cache control on the
    text block so subsequent questions in the same run hit cache reads.

    The prompts are tuned to make the comparison fair:

      * closed-book — instruct the model to answer from its own knowledge
        and admit uncertainty rather than guess. (Without this, models
        will confidently make up specific numbers, which inflates the
        sift lift artificially.)
      * sift-grep   — instruct the model to use grep first, then read,
        then cite the URLs it relied on.
      * web-fetch   — same pattern as sift-grep but on the live web.

    Each prompt's "cite your sources" instruction is identical across the
    two retrieval conditions so we're not gaming citation rates by asking
    one condition for citations and not the other.
    """
    base = (
        "You are an expert research assistant answering reference questions "
        "about technical and policy documentation. Be precise, concise, and "
        "honest about uncertainty. If you don't know an answer or can't "
        "verify it, say so explicitly rather than guessing — guesses are "
        "worse than 'I don't know' for this evaluation.\n\n"
        "When you do answer, structure the response as:\n"
        "  1. A short direct answer (1-3 sentences)\n"
        "  2. (If a retrieval tool was available) a 'Sources:' line listing "
        "the URLs you relied on, one per line.\n"
    )
    extra = {
        "closed-book": (
            "You do NOT have access to any retrieval tools. Answer from your "
            "training knowledge only. If your training data is too stale or "
            "doesn't cover the question, state that clearly instead of "
            "inventing a specific value."
        ),
        "sift-grep": (
            "You have access to a sift-indexed snapshot of the relevant "
            "documentation. Use grep_index first to locate the right page, "
            "then read_page to read the full markdown. Cite the canonical "
            "URLs returned by read_page in your 'Sources:' line."
        ),
        "web-fetch": (
            "You have access to a fetch_url tool that performs a plain HTTP "
            "GET. Use it to retrieve the live page when you need specific, "
            "current information. Note: some sites return 403 or bot-block "
            "pages; if a fetch fails, do not make up the answer — say so."
        ),
    }[condition]
    text = base + "\n" + extra
    return [{"type": "text", "text": text,
             "cache_control": {"type": "ephemeral"}}]


def _spec_to_api(spec: ToolSpec) -> dict:
    return {"name": spec.name,
            "description": spec.description,
            "input_schema": spec.schema}


def _collect_text(content: list) -> str:
    """Concatenate all top-level text blocks in an assistant message."""
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(p for p in parts if p)


def _detect_refusal(text: str) -> bool:
    t = text.lower()
    return any(marker in t for marker in _REFUSAL_MARKERS)


def run_agent(
    *,
    question_text: str,
    condition: str,
    tools: list[ToolSpec],
    qid: str,
    model: str = "claude-opus-4-7",
    max_turns: int = MAX_TURNS,
    api_key: Optional[str] = None,
    client=None,
) -> AgentResult:
    """Run a single question through one condition. Returns an AgentResult
    that holds the model's answer, tool-use spans, token totals, and
    timing.

    ``client`` is optional so tests can inject a fake Anthropic SDK client
    without hitting the network; in production it's constructed from
    ``api_key`` (or the ``ANTHROPIC_API_KEY`` env var).
    """
    result = AgentResult(qid=qid, condition=condition, model=model, answer="")
    t0 = time.time()

    if client is None:
        try:
            import anthropic
        except ImportError as e:
            result.error = f"anthropic SDK not installed: {e}"
            result.wall_seconds = round(time.time() - t0, 2)
            return result
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            result.error = "ANTHROPIC_API_KEY not set"
            result.wall_seconds = round(time.time() - t0, 2)
            return result
        client = anthropic.Anthropic(api_key=key)

    system_blocks = _build_system_prompt(condition)
    tool_specs_api = [_spec_to_api(s) for s in tools]
    tool_fn_by_name = {s.name: s.fn for s in tools}

    # Conversation grows turn by turn — each turn appends an assistant
    # message and (if tool_use) a user message with tool_result blocks.
    messages: list[dict] = [{"role": "user", "content": question_text}]
    last_text = ""

    try:
        for turn in range(max_turns):
            result.turns = turn + 1
            try:
                resp = client.messages.create(
                    model=model,
                    max_tokens=MAX_TOKENS,
                    system=system_blocks,
                    tools=tool_specs_api or [],
                    messages=messages,
                )
            except Exception as e:
                # Most likely: 429 from a fresh API key, or a transient
                # network blip. Surface as an error result rather than
                # crash the whole suite.
                result.error = f"api error on turn {turn + 1}: {e}"
                break

            usage = getattr(resp, "usage", None)
            if usage is not None:
                result.total_input_tokens += getattr(usage, "input_tokens", 0) or 0
                result.total_output_tokens += getattr(usage, "output_tokens", 0) or 0
                result.total_cache_read_tokens += (
                    getattr(usage, "cache_read_input_tokens", 0) or 0
                )
                result.total_cache_write_tokens += (
                    getattr(usage, "cache_creation_input_tokens", 0) or 0
                )

            result.stop_reason = getattr(resp, "stop_reason", "") or ""
            content = list(resp.content or [])
            last_text = _collect_text(content)

            # Echo the assistant message into the conversation regardless
            # of whether we'll continue — the next turn needs it as context.
            messages.append({"role": "assistant", "content": content})

            # Done if the model either declared end_turn or didn't ask for
            # any tools this turn.
            tool_uses = [b for b in content if getattr(b, "type", None) == "tool_use"]
            if not tool_uses or result.stop_reason in ("end_turn", "stop_sequence"):
                break

            # Run each tool_use and collect tool_result blocks for the
            # next-turn user message.
            tool_result_blocks = []
            for tu in tool_uses:
                fn = tool_fn_by_name.get(tu.name)
                if fn is None:
                    tool_result = {"error": f"unknown tool {tu.name!r}"}
                else:
                    try:
                        tool_result = fn(dict(tu.input))
                    except Exception as e:
                        tool_result = {"error": f"tool {tu.name} crashed: {e}"}
                # Trace what the model called for the eval report.
                result.tool_calls.append({
                    "turn": turn + 1,
                    "name": tu.name,
                    "input": dict(tu.input),
                    "output_preview": _preview(tool_result),
                })
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": _stringify(tool_result),
                })
            messages.append({"role": "user", "content": tool_result_blocks})
        else:
            # max_turns exhausted without an end_turn — keep the last
            # answer text but flag the truncation in stop_reason.
            if not result.stop_reason:
                result.stop_reason = "max_turns_exhausted"

        result.answer = last_text or ""
        result.cited_urls = _URL_RE.findall(result.answer)
        result.refused = _detect_refusal(result.answer)
    finally:
        result.wall_seconds = round(time.time() - t0, 2)
    return result


def _stringify(payload: dict) -> str:
    import json as _json
    try:
        return _json.dumps(payload, default=str)[:6000]
    except (TypeError, ValueError):
        return str(payload)[:6000]


def _preview(payload: dict) -> str:
    """Compact preview of a tool result for the report — strip body content
    so the JSON dump stays readable."""
    out = {k: v for k, v in payload.items() if k != "content"}
    if "content" in payload:
        body = payload["content"]
        out["content_preview"] = (body[:200] + "…") if isinstance(body, str) else "(non-str)"
    import json as _json
    return _json.dumps(out, default=str)[:600]

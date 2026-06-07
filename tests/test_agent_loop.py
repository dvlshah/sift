"""Tests for the agent-loop bench scaffolding.

We do not exercise the Anthropic API in tests — instead, the agent loop
takes an injected ``client`` so we can drive it with a stub that returns
canned ``messages.create`` responses. Same for the judge. This keeps the
tests offline + deterministic.

Coverage:
  * Question dataclass + lookups
  * Tool registry per condition (toolspec shape + dispatching)
  * grep_index over a real on-disk markdown corpus
  * read_page front-matter + truncation
  * fetch_url with httpx.MockTransport
  * Agent loop: text-only response, single-tool-use cycle, max-turn cap
  * Judge: citation logic + structured parse on a stub
  * Runner: end-to-end resume behavior on the snapshot path
  * Report: shape + numbers sanity
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from evals.agent_loop.agent import (
    AgentResult,
    MAX_TURNS,
    _detect_refusal,
    run_agent,
)
from evals.agent_loop.judge import (
    JUDGE_RUBRIC,
    Judgment,
    _citation_metrics,
    judge_answer,
)
from evals.agent_loop.questions import (
    QUESTIONS,
    Question,
    by_qid,
    by_use_case,
    fresh_only,
)
from evals.agent_loop.report import write_report
from evals.agent_loop.runner import SuiteResult, run_suite
from evals.agent_loop.tools import (
    CONDITIONS,
    ToolSpec,
    make_fetch_tool,
    make_grep_tool,
    make_read_tool,
    tools_for,
)


# ---- Question set ----------------------------------------------------------

class TestQuestionSet:
    def test_has_at_least_15_questions(self):
        assert len(QUESTIONS) >= 15

    def test_qids_unique(self):
        qids = [q.qid for q in QUESTIONS]
        assert len(qids) == len(set(qids))

    def test_all_use_cases_covered(self):
        ucs = {q.use_case for q in QUESTIONS}
        # The bench-suite has 6 use cases; require >= 5 here so the report
        # always has a meaningful "by use case" breakdown.
        assert len(ucs) >= 5

    def test_every_question_has_gold_url_and_answer(self):
        for q in QUESTIONS:
            assert q.gold_urls, f"{q.qid} missing gold_urls"
            assert q.gold_answer, f"{q.qid} missing gold_answer"

    def test_gold_urls_are_http(self):
        for q in QUESTIONS:
            for u in q.gold_urls:
                assert u.startswith(("http://", "https://")), \
                    f"{q.qid}: non-HTTP URL {u!r}"

    def test_at_least_some_fresh_sensitive(self):
        fresh = fresh_only()
        # Fresh questions are the strongest sift signal — we need >= 3 of
        # them, otherwise the headline lift over closed-book is small.
        assert len(fresh) >= 3

    def test_by_qid_returns_none_on_miss(self):
        assert by_qid("not-a-real-qid") is None

    def test_by_use_case_filters(self):
        coding = by_use_case("coding-agents")
        assert all(q.use_case == "coding-agents" for q in coding)
        assert len(coding) >= 2


# ---- Tools: registry -------------------------------------------------------

class TestToolRegistry:
    def test_closed_book_has_no_tools(self):
        assert tools_for("closed-book") == []

    def test_sift_grep_requires_root(self):
        with pytest.raises(ValueError, match="requires `root`"):
            tools_for("sift-grep")

    def test_web_fetch_returns_single_tool(self):
        ts = tools_for("web-fetch")
        assert len(ts) == 1
        assert ts[0].name == "fetch_url"

    def test_unknown_condition_raises(self):
        with pytest.raises(ValueError, match="unknown condition"):
            tools_for("magic-condition")

    def test_conditions_constant_stable(self):
        assert CONDITIONS == ("closed-book", "sift-grep", "web-fetch")


# ---- Tools: sift-grep + read_page on a synthetic corpus --------------------

@pytest.fixture
def fake_sift_index(tmp_path: Path) -> Path:
    """Build a minimal sift-shaped index: root/<run_id>/md/...
    with two markdown files behind a ``current`` symlink."""
    from sift import paths

    run_id = "1970-01-01T00-00-00_test"
    md_dir = paths.run_dir(tmp_path, run_id) / "md"
    md_dir.mkdir(parents=True)

    # Two pages with frontmatter — same shape sift's extract writes.
    (md_dir / "alpha.md").write_text(
        "---\nurl: https://example.test/alpha\n---\n"
        "# Alpha\nThis page talks about asyncio.gather and TaskGroup.\n"
    )
    (md_dir / "beta.md").write_text(
        "---\nurl: https://example.test/beta\n---\n"
        "# Beta\nThis is the second page.\n"
    )
    # ``current`` symlink → run dir
    (tmp_path / "current").symlink_to(paths.run_dir(tmp_path, run_id))
    return tmp_path


class TestGrepTool:
    def test_grep_finds_pattern_across_files(self, fake_sift_index):
        tool = make_grep_tool(fake_sift_index)
        out = tool.fn({"pattern": "TaskGroup"})
        assert "matches" in out
        assert out["total_matches"] >= 1
        first = out["matches"][0]
        assert "TaskGroup" in first["snippet"]

    def test_grep_returns_empty_on_no_match(self, fake_sift_index):
        tool = make_grep_tool(fake_sift_index)
        out = tool.fn({"pattern": "xqzqxqzq-not-a-thing"})
        assert out["matches"] == []
        assert out["total_matches"] == 0

    def test_grep_missing_pattern_returns_error(self, fake_sift_index):
        tool = make_grep_tool(fake_sift_index)
        out = tool.fn({})
        assert "error" in out

    def test_grep_invalid_regex_returns_error(self, fake_sift_index):
        tool = make_grep_tool(fake_sift_index)
        out = tool.fn({"pattern": "[unclosed"})
        assert "error" in out

    def test_grep_caps_max_results(self, fake_sift_index):
        tool = make_grep_tool(fake_sift_index)
        out = tool.fn({"pattern": ".", "max_results": 2})
        assert len(out["matches"]) <= 2


class TestReadTool:
    def test_read_returns_full_content(self, fake_sift_index):
        tool = make_read_tool(fake_sift_index)
        out = tool.fn({"url": "https://example.test/alpha"})
        assert "Alpha" in out["content"]
        assert out["url"].endswith("/alpha")
        assert out["truncated"] is False

    def test_read_missing_url_returns_error(self, fake_sift_index):
        tool = make_read_tool(fake_sift_index)
        out = tool.fn({})
        assert "error" in out

    def test_read_404_returns_error(self, fake_sift_index):
        tool = make_read_tool(fake_sift_index)
        out = tool.fn({"url": "https://example.test/nope"})
        assert "error" in out
        assert "no page indexed" in out["error"]


# ---- Tools: web-fetch ------------------------------------------------------

class TestFetchTool:
    def test_fetch_returns_body_status(self, monkeypatch):
        def handler(req):
            assert req.url.path == "/page"
            return httpx.Response(200, text="hello",
                                  headers={"content-type": "text/plain"})

        class _Stub(httpx.Client):
            def __init__(self, *a, **kw):
                kw["transport"] = httpx.MockTransport(handler)
                super().__init__(*a, **kw)

        monkeypatch.setattr(httpx, "Client", _Stub)
        tool = make_fetch_tool()
        out = tool.fn({"url": "https://example.test/page"})
        assert out["status"] == 200
        assert "hello" in out["content"]

    def test_fetch_rejects_relative(self):
        tool = make_fetch_tool()
        out = tool.fn({"url": "/just/a/path"})
        assert "error" in out

    def test_fetch_handles_http_error(self, monkeypatch):
        def handler(req):
            raise httpx.ConnectError("connection refused")

        class _Stub(httpx.Client):
            def __init__(self, *a, **kw):
                kw["transport"] = httpx.MockTransport(handler)
                super().__init__(*a, **kw)

        monkeypatch.setattr(httpx, "Client", _Stub)
        tool = make_fetch_tool()
        out = tool.fn({"url": "https://example.test/page"})
        assert "error" in out


# ---- Agent loop with a stub client ----------------------------------------

class _StubBlock:
    """Mimic the Anthropic SDK's content blocks."""
    def __init__(self, type, **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


class _StubResp:
    def __init__(self, content, stop_reason="end_turn",
                 input_tokens=10, output_tokens=20,
                 cache_read=0, cache_write=0):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        )


class _StubAgentClient:
    """Replays a scripted sequence of ``messages.create`` returns."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        return self._responses.pop(0)


class TestAgentLoop:
    def test_text_only_response_returns_immediately(self):
        client = _StubAgentClient([
            _StubResp([_StubBlock("text", text="The answer is 42.")])
        ])
        result = run_agent(
            question_text="What's the answer?",
            condition="closed-book", tools=[],
            qid="t1", client=client,
        )
        assert result.answer == "The answer is 42."
        assert result.turns == 1
        assert result.stop_reason == "end_turn"
        assert result.total_input_tokens == 10
        assert result.total_output_tokens == 20
        # No tool calls
        assert result.tool_calls == []
        assert result.error is None

    def test_single_tool_use_cycle(self):
        # First turn: tool_use; second turn: final text.
        tool_use_block = _StubBlock(
            "tool_use", id="tu_1", name="echo",
            input={"value": "hi"},
        )
        client = _StubAgentClient([
            _StubResp([tool_use_block], stop_reason="tool_use"),
            _StubResp([_StubBlock("text", text="Got 'hi'. Sources:\nhttps://x.test/a")]),
        ])
        echo_tool = ToolSpec(
            name="echo", description="echo",
            schema={"type": "object"},
            fn=lambda args: {"echo": args.get("value")},
        )
        result = run_agent(
            question_text="echo hi", condition="sift-grep",
            tools=[echo_tool], qid="t2", client=client,
        )
        assert result.turns == 2
        assert result.answer.startswith("Got 'hi'")
        assert result.tool_calls and result.tool_calls[0]["name"] == "echo"
        assert result.cited_urls == ["https://x.test/a"]

    def test_max_turns_cap(self):
        # Every turn returns a tool_use; we should stop after MAX_TURNS.
        tool_use_block = _StubBlock(
            "tool_use", id="tu_x", name="echo",
            input={},
        )
        responses = [_StubResp([tool_use_block], stop_reason="tool_use")
                     for _ in range(MAX_TURNS + 2)]
        client = _StubAgentClient(responses)
        echo_tool = ToolSpec(
            name="echo", description="echo",
            schema={"type": "object"},
            fn=lambda args: {"echo": "ok"},
        )
        result = run_agent(
            question_text="loop forever", condition="sift-grep",
            tools=[echo_tool], qid="t3", client=client,
            max_turns=MAX_TURNS,
        )
        assert result.turns == MAX_TURNS
        assert result.stop_reason in ("tool_use", "max_turns_exhausted")

    def test_unknown_tool_returns_error_block(self):
        bogus_tool_use = _StubBlock(
            "tool_use", id="tu_b", name="not-registered",
            input={},
        )
        client = _StubAgentClient([
            _StubResp([bogus_tool_use], stop_reason="tool_use"),
            _StubResp([_StubBlock("text", text="Sorry, can't proceed.")]),
        ])
        result = run_agent(
            question_text="x", condition="sift-grep",
            tools=[], qid="t4", client=client,
        )
        # Tool call was recorded but the loop still finished cleanly
        assert any("not-registered" in tc["name"]
                   for tc in result.tool_calls)


class TestRefusalDetection:
    @pytest.mark.parametrize("text", [
        "I don't have information about that.",
        "I cannot answer this question.",
        "I'm unable to determine the value.",
        "I don't know the rate.",
    ])
    def test_refusal_markers(self, text):
        assert _detect_refusal(text)

    @pytest.mark.parametrize("text", [
        "The rate is 10%.",
        "Per RFC 9110, Retry-After accepts an HTTP-date or seconds.",
    ])
    def test_normal_answers_not_flagged(self, text):
        assert not _detect_refusal(text)


# ---- Judge: deterministic helpers + structured parse stub -----------------

class TestCitationMetrics:
    def test_no_urls(self):
        present, faithful = _citation_metrics(
            [], ("https://gold.test/a",)
        )
        assert present is False and faithful is False

    def test_host_match_is_faithful(self):
        present, faithful = _citation_metrics(
            ["https://gold.test/different-page"],
            ("https://gold.test/a",),
        )
        assert present is True and faithful is True

    def test_unrelated_host_is_unfaithful(self):
        present, faithful = _citation_metrics(
            ["https://random.test/anything"],
            ("https://gold.test/a",),
        )
        assert present is True and faithful is False

    def test_exact_url_match(self):
        present, faithful = _citation_metrics(
            ["https://gold.test/a"],
            ("https://gold.test/a",),
        )
        assert present is True and faithful is True


class _StubJudgeResp:
    def __init__(self, parsed):
        self.parsed_output = parsed
        self.usage = SimpleNamespace(
            input_tokens=100,
            output_tokens=20,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )


class _StubJudgeClient:
    def __init__(self, parsed):
        from evals.agent_loop.judge import JudgeScore
        self._resp = _StubJudgeResp(JudgeScore(
            correctness=parsed["correctness"],
            key_fact_match=parsed["key_fact_match"],
            hallucinated_specifics=parsed["hallucinated_specifics"],
            brief_reason=parsed["brief_reason"],
        ))
        self.messages = SimpleNamespace(parse=self._parse)
        self.last_call: dict = {}

    def _parse(self, **kw):
        self.last_call = kw
        return self._resp


class TestJudge:
    def test_passes_rubric_in_system_block(self):
        client = _StubJudgeClient({
            "correctness": 5, "key_fact_match": True,
            "hallucinated_specifics": False,
            "brief_reason": "matches gold.",
        })
        j = judge_answer(
            qid="t", condition="sift-grep",
            question="x", gold_answer="y", gold_urls=("https://g.test/x",),
            agent_answer="y", agent_urls=["https://g.test/x"],
            client=client,
        )
        assert client.last_call["system"][0]["text"] == JUDGE_RUBRIC
        assert j.correctness == 5
        assert j.citation_faithful is True
        assert j.error is None

    def test_records_token_usage(self):
        client = _StubJudgeClient({
            "correctness": 3, "key_fact_match": False,
            "hallucinated_specifics": False, "brief_reason": "partial",
        })
        j = judge_answer(
            qid="t", condition="closed-book",
            question="x", gold_answer="y", gold_urls=("https://g.test/x",),
            agent_answer="z", agent_urls=[],
            client=client,
        )
        assert j.judge_input_tokens == 100
        assert j.judge_output_tokens == 20


# ---- Runner end-to-end with stubs -----------------------------------------

def _make_stub_client(agent_text="ok", judge_score=4):
    """Build paired stub clients that finish each cell in one turn."""
    from evals.agent_loop.judge import JudgeScore

    agent_resp = _StubResp([_StubBlock("text", text=agent_text)])

    class _AClient:
        def __init__(self):
            self.messages = SimpleNamespace(create=lambda **kw: agent_resp)
    class _JClient:
        def __init__(self):
            self.messages = SimpleNamespace(parse=lambda **kw: _StubJudgeResp(
                JudgeScore(correctness=judge_score,
                           key_fact_match=True,
                           hallucinated_specifics=False,
                           brief_reason="ok")
            ))
    return _AClient(), _JClient()


class TestRunner:
    def test_run_two_questions_one_condition_writes_snapshot(
            self, tmp_path, fake_sift_index):
        a, j = _make_stub_client()
        out_dir = tmp_path / "out"
        questions = (
            Question(qid="q1", use_case="x", text="?",
                     gold_urls=("https://g.test/a",), gold_answer="ok"),
            Question(qid="q2", use_case="x", text="?",
                     gold_urls=("https://g.test/b",), gold_answer="ok"),
        )
        result = run_suite(
            sift_root=fake_sift_index,
            questions=questions, conditions=("closed-book",),
            output_dir=out_dir,
            agent_client=a, judge_client=j,
            progress=lambda s: None,
        )
        snap = json.loads((out_dir / "agent_bench.json").read_text())
        assert len(snap["results"]) == 2
        assert all(c.condition == "closed-book" for c in result.cells)

    def test_resume_skips_existing_cells(
            self, tmp_path, fake_sift_index):
        a, j = _make_stub_client()
        out_dir = tmp_path / "out"
        questions = (
            Question(qid="q1", use_case="x", text="?",
                     gold_urls=("https://g.test/a",), gold_answer="ok"),
        )
        run_suite(
            sift_root=fake_sift_index,
            questions=questions, conditions=("closed-book",),
            output_dir=out_dir,
            agent_client=a, judge_client=j,
            progress=lambda s: None,
        )

        # Second run with a client that would crash if called — proves
        # the resume path didn't reach the API.
        crash_client = SimpleNamespace(
            messages=SimpleNamespace(
                create=lambda **kw: (_ for _ in ()).throw(
                    RuntimeError("must not be called")
                ),
                parse=lambda **kw: (_ for _ in ()).throw(
                    RuntimeError("must not be called")
                ),
            )
        )
        result = run_suite(
            sift_root=fake_sift_index,
            questions=questions, conditions=("closed-book",),
            output_dir=out_dir,
            agent_client=crash_client, judge_client=crash_client,
            resume=True,
            progress=lambda s: None,
        )
        assert len(result.cells) == 1
        assert result.cells[0].correctness == 4


# ---- Report shape sanity --------------------------------------------------

class TestReport:
    def _suite(self) -> dict:
        return {
            "version": "v1",
            "config": {
                "agent_model": "claude-opus-4-7",
                "judge_model": "claude-opus-4-7",
                "sift_root": "/tmp/x", "sift_run_id": None,
                "conditions": ["closed-book", "sift-grep"],
                "n_questions": 2,
                "total_wall_seconds": 1.0,
            },
            "questions": [
                {"qid": "q1", "use_case": "tax-compliance",
                 "text": "?", "gold_urls": ["https://g.test/a"],
                 "gold_answer": "y", "fresh_sensitive": True, "notes": ""},
                {"qid": "q2", "use_case": "tax-compliance",
                 "text": "?", "gold_urls": ["https://g.test/b"],
                 "gold_answer": "y", "fresh_sensitive": False, "notes": ""},
            ],
            "results": [
                {"qid": "q1", "condition": "closed-book",
                 "agent": {"refused": False, "cited_urls": [],
                           "total_input_tokens": 100, "total_output_tokens": 50,
                           "total_cache_read_tokens": 0, "total_cache_write_tokens": 0},
                 "judge": {"correctness": 2, "citation_present": False,
                           "citation_faithful": False, "brief_reason": "wrong number",
                           "judge_input_tokens": 100, "judge_output_tokens": 20,
                           "judge_cache_read_tokens": 0, "judge_cache_write_tokens": 0}},
                {"qid": "q1", "condition": "sift-grep",
                 "agent": {"refused": False, "cited_urls": ["https://g.test/a"],
                           "total_input_tokens": 200, "total_output_tokens": 80,
                           "total_cache_read_tokens": 50, "total_cache_write_tokens": 100},
                 "judge": {"correctness": 5, "citation_present": True,
                           "citation_faithful": True, "brief_reason": "matches",
                           "judge_input_tokens": 120, "judge_output_tokens": 30,
                           "judge_cache_read_tokens": 100, "judge_cache_write_tokens": 0}},
                {"qid": "q2", "condition": "closed-book",
                 "agent": {"refused": False, "cited_urls": [],
                           "total_input_tokens": 50, "total_output_tokens": 30,
                           "total_cache_read_tokens": 0, "total_cache_write_tokens": 0},
                 "judge": {"correctness": 4, "citation_present": False,
                           "citation_faithful": False, "brief_reason": "ok",
                           "judge_input_tokens": 80, "judge_output_tokens": 15,
                           "judge_cache_read_tokens": 0, "judge_cache_write_tokens": 0}},
                {"qid": "q2", "condition": "sift-grep",
                 "agent": {"refused": False, "cited_urls": ["https://g.test/b"],
                           "total_input_tokens": 180, "total_output_tokens": 60,
                           "total_cache_read_tokens": 40, "total_cache_write_tokens": 0},
                 "judge": {"correctness": 4, "citation_present": True,
                           "citation_faithful": True, "brief_reason": "ok",
                           "judge_input_tokens": 90, "judge_output_tokens": 20,
                           "judge_cache_read_tokens": 100, "judge_cache_write_tokens": 0}},
            ],
            "totals": {
                "agent_tokens": {"input": 530, "output": 220,
                                 "cache_read": 90, "cache_write": 100},
                "judge_tokens": {"input": 390, "output": 85,
                                 "cache_read": 200, "cache_write": 0},
                "cells_total": 4,
            },
        }

    def test_report_contains_all_sections(self):
        md = write_report(self._suite())
        for section in ("Headline correctness", "Per-use-case",
                        "Per-question", "Citation behavior", "Cost"):
            assert section in md

    def test_lift_line_present(self):
        md = write_report(self._suite())
        # 4.5 (sift-grep mean) − 3.0 (closed-book mean) = +1.5
        assert "sift-grep lift over closed-book" in md
        assert "+1.5" in md

    def test_notable_failures_when_score_le_2(self):
        md = write_report(self._suite())
        assert "Notable failures" in md
        assert "wrong number" in md

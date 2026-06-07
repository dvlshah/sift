"""Run all (question × condition) cells, judge each, aggregate.

The orchestrator is deliberately linear (no asyncio): with ~20 questions and
~3 conditions, total agent + judge calls is ~120, each ~1-5 seconds. A
serial run lands in 5-15 minutes, well under the threshold where async
would buy us anything. Linear also keeps token-cost accounting honest —
prompt caching only kicks in within a sequential run.

Output shape (``run_suite`` return value) is a single JSON-serializable
dict the report module renders + the CLI dumps to disk:

  {
    "config": {...},                # model, conditions run, sift root, …
    "questions": [...],             # serialized Question dataclasses
    "results":   [...],             # one entry per (qid, condition) cell
    "totals":    {...},             # token + cost rollups
    "version":   AGENT_LOOP_VERSION,
  }

The bench can be re-run incrementally: if a JSON snapshot already exists
for a (qid, condition) cell, the runner can skip it. This matters because
the most common failure mode in mid-run is an API 429 — we don't want a
retry to redo the 90% that already worked.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from . import AGENT_LOOP_VERSION
from .agent import AgentResult, run_agent
from .judge import Judgment, judge_answer
from .questions import QUESTIONS, Question
from .tools import CONDITIONS, tools_for


@dataclass
class Cell:
    """One filled cell of the question × condition grid."""
    qid: str
    condition: str
    agent: dict           # AgentResult as dict
    judge: dict           # Judgment as dict

    @property
    def correctness(self) -> int:
        return int(self.judge.get("correctness") or 0)

    @property
    def refused(self) -> bool:
        return bool(self.agent.get("refused"))


@dataclass
class SuiteResult:
    config: dict
    questions: list[dict]
    cells: list[Cell] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "version": AGENT_LOOP_VERSION,
            "config": self.config,
            "questions": self.questions,
            "results": [
                {"qid": c.qid, "condition": c.condition,
                 "agent": c.agent, "judge": c.judge}
                for c in self.cells
            ],
            "totals": self._totals(),
        }

    def _totals(self) -> dict:
        agent_tokens = {
            "input": 0, "output": 0,
            "cache_read": 0, "cache_write": 0,
        }
        judge_tokens = {
            "input": 0, "output": 0,
            "cache_read": 0, "cache_write": 0,
        }
        for c in self.cells:
            a = c.agent or {}
            j = c.judge or {}
            agent_tokens["input"]       += a.get("total_input_tokens") or 0
            agent_tokens["output"]      += a.get("total_output_tokens") or 0
            agent_tokens["cache_read"]  += a.get("total_cache_read_tokens") or 0
            agent_tokens["cache_write"] += a.get("total_cache_write_tokens") or 0
            judge_tokens["input"]       += j.get("judge_input_tokens") or 0
            judge_tokens["output"]      += j.get("judge_output_tokens") or 0
            judge_tokens["cache_read"]  += j.get("judge_cache_read_tokens") or 0
            judge_tokens["cache_write"] += j.get("judge_cache_write_tokens") or 0
        return {
            "agent_tokens": agent_tokens,
            "judge_tokens": judge_tokens,
            "cells_total": len(self.cells),
        }


def _load_existing(snapshot_path: Path) -> dict:
    """Load a prior run's JSON snapshot, if any, keyed for incremental
    re-runs. Tolerant: a missing or unreadable snapshot just returns {}.
    """
    if not snapshot_path.exists():
        return {}
    try:
        return json.loads(snapshot_path.read_text())
    except (OSError, ValueError):
        return {}


def _existing_index(prior: dict) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for r in (prior.get("results") or []):
        key = (r.get("qid"), r.get("condition"))
        out[key] = r
    return out


def run_suite(
    *,
    sift_root: Path,
    sift_run_id: Optional[str] = None,
    questions: Iterable[Question] = QUESTIONS,
    conditions: Iterable[str] = CONDITIONS,
    agent_model: str = "claude-opus-4-7",
    judge_model: str = "claude-opus-4-7",
    api_key: Optional[str] = None,
    output_dir: Optional[Path] = None,
    resume: bool = True,
    progress=print,
    agent_client=None,
    judge_client=None,
) -> SuiteResult:
    """Run the full suite, write incremental snapshots, return the result.

    ``resume=True`` skips cells already present in the snapshot — that's the
    safe default; flip to False for a clean re-run.
    """
    q_list = list(questions)
    c_list = list(conditions)
    suite = SuiteResult(
        config={
            "sift_root": str(sift_root),
            "sift_run_id": sift_run_id,
            "agent_model": agent_model,
            "judge_model": judge_model,
            "conditions": c_list,
            "n_questions": len(q_list),
            "version": AGENT_LOOP_VERSION,
        },
        questions=[asdict(q) for q in q_list],
    )

    snapshot_path: Optional[Path] = None
    prior_index: dict = {}
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = output_dir / "agent_bench.json"
        if resume:
            prior_index = _existing_index(_load_existing(snapshot_path))

    overall_t0 = time.time()
    total_cells = len(q_list) * len(c_list)
    cell_n = 0
    for condition in c_list:
        # Build tools once per condition — the toolset doesn't depend on
        # the question, just on the condition.
        try:
            tools = tools_for(condition, root=sift_root, run_id=sift_run_id)
        except Exception as e:
            progress(f"[{condition}] tool init failed: {e}; skipping condition")
            continue

        for q in q_list:
            cell_n += 1
            key = (q.qid, condition)
            if key in prior_index:
                # Re-hydrate from the snapshot — keep the same shape so
                # totals + report don't have to special-case resumed cells.
                prev = prior_index[key]
                suite.cells.append(Cell(
                    qid=q.qid, condition=condition,
                    agent=prev.get("agent") or {},
                    judge=prev.get("judge") or {},
                ))
                progress(f"[{cell_n}/{total_cells}] {q.qid} / {condition} "
                         "(resumed from snapshot)")
                continue

            progress(f"[{cell_n}/{total_cells}] {q.qid} / {condition} …")
            agent_t0 = time.time()
            agent_res: AgentResult = run_agent(
                question_text=q.text, condition=condition, tools=tools,
                qid=q.qid, model=agent_model, api_key=api_key,
                client=agent_client,
            )
            agent_dt = round(time.time() - agent_t0, 2)
            judge_res: Judgment = judge_answer(
                qid=q.qid, condition=condition, question=q.text,
                gold_answer=q.gold_answer, gold_urls=q.gold_urls,
                agent_answer=agent_res.answer,
                agent_urls=list(agent_res.cited_urls),
                model=judge_model, api_key=api_key,
                client=judge_client,
            )
            cell = Cell(
                qid=q.qid, condition=condition,
                agent=asdict(agent_res), judge=asdict(judge_res),
            )
            suite.cells.append(cell)
            progress(
                f"   answer={agent_res.turns}t/{agent_dt}s "
                f"score={judge_res.correctness}/5 "
                f"refused={agent_res.refused} "
                f"err={(agent_res.error or judge_res.error) or '-'}"
            )

            # Write the snapshot after every cell so a mid-run abort never
            # loses more than one cell's worth of work.
            if snapshot_path is not None:
                snapshot_path.write_text(
                    json.dumps(suite.to_dict(), indent=2, default=str)
                )

    suite.config["total_wall_seconds"] = round(time.time() - overall_t0, 2)
    if snapshot_path is not None:
        snapshot_path.write_text(
            json.dumps(suite.to_dict(), indent=2, default=str)
        )
    return suite

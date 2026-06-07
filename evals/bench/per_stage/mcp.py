"""Stage 7 evals: MCP read.

Implemented: ``mcp_read_md_verify_round_trip`` — pick a sample of FRESH md
files, call read_md with verify=true, expect success. Tamper detection
exercised by the existing test_mcp_verify suite — we don't duplicate that
here.

Deferred to Phase B5: ``mcp_grep_precision_recall`` (needs an annotated
query set per use case) and the query_manifest safety eval (needs a fixture
of malicious SQL attempts).
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from sift import mcp_server, paths
from sift.manifest import open_db


@dataclass
class MCPVerifyResult:
    name: str = "mcp_read_md_verify_round_trip"
    pass_threshold: float = 1.0
    n_sampled: int = 0
    n_verified: int = 0
    rate: float = 0.0
    passed: bool = False
    failures: list = None


def eval_mcp_verify_round_trip(root: Path, run_id: str,
                                *, sample: int = 10) -> MCPVerifyResult:
    """Sample N FRESH md files, invoke read_md with verify=true, expect
    every result to be non-error. This validates the end-to-end content_hash
    chain: extract wrote a hash, frontmatter carries it, MCP read recomputes
    and matches."""
    conn = open_db(paths.manifest_path(root))
    rows = conn.execute(
        "SELECT url FROM manifest WHERE state IN ('FRESH','FROZEN') "
        "AND raw_hash IS NOT NULL LIMIT ?", (sample,),
    ).fetchall()

    # The MCP server's tool_read_md does ``(cur / rel).resolve()`` and then
    # checks the result is under ``cur``. If ``cur`` isn't itself resolved,
    # symlink-chasing produces a different absolute path and the safety
    # check fires. Always pass the resolved run dir.
    cur = paths.run_dir(root, run_id).resolve()
    n_ok = 0
    failures: list[dict] = []
    n_sampled = 0
    for (url,) in rows:
        md = paths.md_path(root, run_id, url).resolve()
        if not md.exists():
            continue
        n_sampled += 1
        try:
            rel = md.relative_to(cur)
        except ValueError:
            failures.append({"path": str(md),
                             "error": "md path not under run dir"})
            continue
        result = mcp_server.tool_read_md(cur, str(rel), verify=True)
        if not result.isError:
            n_ok += 1
        else:
            failures.append({"path": str(rel),
                             "error": result.content[0].text[:200]
                             if result.content else "?"})
    rate = (n_ok / n_sampled) if n_sampled else 0.0
    return MCPVerifyResult(
        n_sampled=n_sampled,
        n_verified=n_ok,
        rate=round(rate, 4),
        passed=(rate == 1.0 and n_sampled > 0),
        failures=failures[:5],
    )


def run_mcp_evals(root: Path, run_id: str, *, sample: int = 10) -> dict:
    return {"verify": asdict(eval_mcp_verify_round_trip(root, run_id,
                                                        sample=sample))}

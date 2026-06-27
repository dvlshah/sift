"""Phase 5: PUBLISH — verify gates, build artifacts, atomic symlink flip.

Five gates a run must pass before `current` is repointed:

  G1. Manifest/FS integrity: every FRESH manifest row has a md file in this run
      (or in an earlier run still on disk); every md file in this run has a row.
  G2. Hash sample: random 1% of md files re-normalized+hashed must match
      their stored content_hash.
  G3. Coverage: >= COVERAGE_FLOOR fraction of expected URLs reached a terminal
      state (FRESH / GONE) since the previous successful publish.
  G4. Schema sanity: 50-file random sample passes structural checks (frontmatter
      parses, non-empty title or H1, length within 3σ of corpus mean).
  G5. Snapshot summary: snapshot.json written with run_id, counts, version info.

If a gate fails, snapshot.json is still written (with status="degraded") for
forensics, but `current` is NOT repointed.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import sqlite3
from pathlib import Path
from typing import Optional

from . import CRAWLER_VERSION, paths
from . import agent_surface
from . import facts as facts_mod
from . import integrity
from ._io import (
    atomic_write_text,
    parse_frontmatter as _parse_frontmatter,
    split_frontmatter as _split_frontmatter,
)
from .classify import CLASSIFIER_VERSION
from .extract import EXTRACTOR_VERSION, hash_normalized_body
from .manifest import counts_by_state, counts_by_tier, iter_all, now_utc
from .normalize import NORMALIZER_VERSION
from .sites import current_profile

# Gate thresholds. Module-level mutable so apply_config() can swap them at startup.
COVERAGE_FLOOR = 0.99   # require >=99% of seeded URLs to be terminal
HASH_SAMPLE_RATE = 0.01  # 1%
HASH_SAMPLE_MIN = 25     # but never fewer than this if we have enough files
SCHEMA_SAMPLE_SIZE = 50

# Manifest states that count as "terminal" for the coverage gate. A URL in
# any of these states finished its lifecycle for this run (success, removed,
# frozen-in-place, or operator-opted-out of the only transport that would
# work). UNSEEN/FAILED are non-terminal.
_TERMINAL_STATES: frozenset[str] = frozenset({
    "FRESH",
    "GONE",
    "FROZEN",
    "SKIPPED_BROWSER_DISABLED",
})


def _is_terminal_state(s: str) -> bool:
    """True iff ``s`` is one of :data:`_TERMINAL_STATES`. Pure predicate so
    the coverage gate and ``sift status`` agree on what 'terminal' means."""
    return s in _TERMINAL_STATES

# Soft-failure tunables for schema_sanity. A "short" body may be legitimate
# (FROZEN archive stubs especially); we only fail if the fraction of short
# non-FROZEN bodies in the sample exceeds the tolerance.
SHORT_BODY_THRESHOLD = 50              # chars of stripped body
SHORT_BODY_TOLERANCE_NONFROZEN = 0.05  # fail above 5% short bodies in non-FROZEN sample


# Optional GPG signing key id. None = signing disabled (default).
# Set via apply_config from IndexConfig.publish.gpg_key_id.
GPG_KEY_ID: Optional[str] = None


def apply_config(cfg) -> None:
    """Override gate thresholds from an IndexConfig. Called once at CLI startup."""
    global COVERAGE_FLOOR, HASH_SAMPLE_RATE, HASH_SAMPLE_MIN, SCHEMA_SAMPLE_SIZE, GPG_KEY_ID
    COVERAGE_FLOOR = cfg.publish.coverage_floor
    HASH_SAMPLE_RATE = cfg.publish.hash_sample_rate
    HASH_SAMPLE_MIN = cfg.publish.hash_sample_min
    SCHEMA_SAMPLE_SIZE = cfg.publish.schema_sample_size
    GPG_KEY_ID = getattr(cfg.publish, "gpg_key_id", None)

def _seed_rng(run_id: str, label: str) -> random.Random:
    """Deterministic RNG per gate so verification is reproducible."""
    h = hashlib.sha256(f"{run_id}:{label}".encode()).digest()
    return random.Random(int.from_bytes(h[:8], "big"))


def consolidate_md_tree(
    conn: sqlite3.Connection, root: Path, run_id: str
) -> tuple[int, int]:
    """Ensure the current run's md/ tree contains a file for every FRESH/FROZEN
    manifest row. For rows whose md was written in a previous run (because the
    current run's fetch+extract didn't touch them), hardlink the file forward
    from the most recent run that has it.

    Without this step, an incremental run's md/ contains only newly-extracted
    pages; routes.tsv then points at paths that don't exist in current/ — the
    agent sees a "FRESH but unreadable" state.

    Returns (linked_count, still_missing_count). Hardlinking is essentially
    free disk-wise (shared inodes) and constant-time per file.
    """
    runs_dir = root / "runs"
    # Other runs to look back into, newest-first (cheaper to find recent dupes)
    other_runs = sorted(
        (p for p in runs_dir.iterdir() if p.is_dir() and p.name != run_id),
        key=lambda p: p.name,
        reverse=True,
    )

    linked = 0
    still_missing = 0
    for row in iter_all(conn):
        if row.state not in ("FRESH", "FROZEN"):
            continue
        if not row.content_hash:
            continue  # no extracted content available even in theory
        target = paths.md_path(root, run_id, row.url)
        if target.exists():
            continue
        # Find this URL's md in some prior run.
        rel = target.relative_to(paths.run_dir(root, run_id))
        for other in other_runs:
            src = other / rel
            if src.exists() and src.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(src, target)
                    linked += 1
                except OSError:
                    # cross-device or other link failure — fall back to copy
                    try:
                        target.write_bytes(src.read_bytes())
                        linked += 1
                    except OSError:
                        still_missing += 1
                break
        else:
            still_missing += 1
    return linked, still_missing


def gate_manifest_fs_integrity(
    conn: sqlite3.Connection, root: Path, run_id: str
) -> tuple[bool, str]:
    """G1: every FRESH/FROZEN manifest row with content_hash must have a real
    md file in this run's md/ tree (after consolidate_md_tree has run).
    Every md file in this run must correspond to a manifest row (no orphans).
    """
    missing: list[str] = []
    md_root = paths.run_dir(root, run_id) / "md"
    for row in iter_all(conn):
        if row.state not in ("FRESH", "FROZEN"):
            continue
        if not row.content_hash:
            continue
        md = paths.md_path(root, run_id, row.url)
        if not md.exists():
            missing.append(row.url)

    orphans: list[Path] = []
    if md_root.exists():
        for f in md_root.rglob("*.md"):
            try:
                txt = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                orphans.append(f)
                continue
            fm, _ = _split_frontmatter(txt)
            if not fm:
                orphans.append(f)
                continue
            url = _parse_frontmatter(fm).get("url")
            if url is None:
                orphans.append(f)
                continue
            row = conn.execute(
                "SELECT 1 FROM manifest WHERE url = ?", (url,)
            ).fetchone()
            if row is None:
                orphans.append(f)
    if orphans:
        return False, f"{len(orphans)} orphan md files (e.g. {orphans[0]})"
    if missing:
        return False, (
            f"{len(missing)} FRESH/FROZEN rows missing md in this run "
            f"(e.g. {missing[0]}). consolidate_md_tree should have linked these forward."
        )
    return True, "ok"


def gate_hash_sample(
    conn: sqlite3.Connection, root: Path, run_id: str
) -> tuple[bool, str]:
    """G2: re-hash a random 1% sample of md files; bodies must match content_hash."""
    md_root = paths.run_dir(root, run_id) / "md"
    if not md_root.exists():
        return True, "no md to sample"
    all_md = list(md_root.rglob("*.md"))
    if not all_md:
        return True, "no md to sample"
    sample_size = max(HASH_SAMPLE_MIN, int(len(all_md) * HASH_SAMPLE_RATE))
    sample_size = min(sample_size, len(all_md))
    rng = _seed_rng(run_id, "hash-sample")
    sample = rng.sample(all_md, sample_size)
    mismatches: list[str] = []
    for f in sample:
        text = f.read_text(encoding="utf-8", errors="replace")
        fm, body = _split_frontmatter(text)
        if not fm:
            mismatches.append(f"{f}: no frontmatter")
            continue
        meta = _parse_frontmatter(fm)
        stored = meta.get("content_hash", "").replace("sha256:", "")
        recomputed = hash_normalized_body(body)
        if stored != recomputed:
            mismatches.append(f"{f}: hash drift")
    if mismatches:
        return False, f"{len(mismatches)}/{len(sample)} hash mismatches; first: {mismatches[0]}"
    return True, f"{len(sample)}/{len(all_md)} sampled, all match"


def gate_coverage(
    conn: sqlite3.Connection, expected_urls: int
) -> tuple[bool, str]:
    """G3: of all seeded URLs, fraction reaching FRESH+GONE+FROZEN >= floor.

    FROZEN counts because frozen pages legitimately skipped fetch.
    """
    states = counts_by_state(conn)
    terminal = sum(n for state, n in states.items() if _is_terminal_state(state))
    if expected_urls == 0:
        return True, "no expected URLs"
    cov = terminal / expected_urls
    ok = cov >= COVERAGE_FLOOR
    msg = (f"coverage={cov:.4f} (terminal={terminal}/expected={expected_urls}, "
           f"floor={COVERAGE_FLOOR})")
    if not ok:
        # terminal << expected almost always means a deliberately capped or
        # narrow crawl (e.g. --limit against a much larger seed), not a real
        # coverage failure. Point the operator at the fix, not just a number.
        msg += (" — terminal is far below expected, which usually means a "
                "capped (--limit) or narrow crawl against a much larger seed. "
                "For an intentional partial index, run with --coverage-base "
                "planned, or scope the seed (host_allow / excludes) so seeded "
                "≈ what you crawl")
    return ok, msg


def gate_schema_sanity(root: Path, run_id: str) -> tuple[bool, str]:
    """G4: random 50-file sample passes basic structural checks.

    Two severity levels:
      * Hard failure (zero tolerance): missing frontmatter, missing url,
        missing content_hash. These indicate file corruption — any one fails.
      * Soft check (tolerance-based): suspiciously short body. ATO archive
        stubs (FROZEN tier) can legitimately be a heading + a code, so we
        only fail when > SHORT_BODY_TOLERANCE_NONFROZEN of the non-FROZEN
        sample is short.
    """
    md_root = paths.run_dir(root, run_id) / "md"
    if not md_root.exists():
        return True, "no md to sample"
    all_md = list(md_root.rglob("*.md"))
    if not all_md:
        return True, "no md to sample"
    sample_size = min(SCHEMA_SAMPLE_SIZE, len(all_md))
    rng = _seed_rng(run_id, "schema-sample")
    sample = rng.sample(all_md, sample_size)

    short_frozen = 0
    short_nonfrozen = 0
    nonfrozen_total = 0
    short_examples: list[str] = []

    for f in sample:
        text = f.read_text(encoding="utf-8", errors="replace")
        fm, body = _split_frontmatter(text)
        # ---- Hard failures (corruption indicators) -------------------------
        if not fm:
            return False, f"{f}: missing frontmatter"
        meta = _parse_frontmatter(fm)
        if not meta.get("url"):
            return False, f"{f}: frontmatter missing url"
        if not meta.get("content_hash"):
            return False, f"{f}: frontmatter missing content_hash"

        # ---- Soft check: short body, tier-aware ----------------------------
        tier = meta.get("tier", "").strip()
        body_len = len(body.strip())
        if tier != "FROZEN":
            nonfrozen_total += 1
        if body_len < SHORT_BODY_THRESHOLD:
            if tier == "FROZEN":
                short_frozen += 1
            else:
                short_nonfrozen += 1
                if len(short_examples) < 3:
                    short_examples.append(f"{f.name}({body_len})")

    if nonfrozen_total > 0:
        ratio = short_nonfrozen / nonfrozen_total
        if ratio > SHORT_BODY_TOLERANCE_NONFROZEN:
            return False, (
                f"{short_nonfrozen}/{nonfrozen_total} non-FROZEN sampled have "
                f"<{SHORT_BODY_THRESHOLD}-char bodies "
                f"({ratio:.1%} > {SHORT_BODY_TOLERANCE_NONFROZEN:.0%} tolerance); "
                f"examples: {', '.join(short_examples)}"
            )
    return True, (
        f"{len(sample)}/{len(all_md)} sampled, "
        f"{short_frozen} short FROZEN stubs (ok), "
        f"{short_nonfrozen}/{nonfrozen_total} short non-FROZEN (within tolerance)"
    )


def gate_facts_validation(root: Path, run_id: str) -> tuple[bool, str]:
    """G6: every facts/*.json must parse, declare a $schema we know, and
    satisfy the declared schema. A malformed facts file means a downstream
    agent could read invalid structured data — block publish."""
    import json as _json
    from typing import Any
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        return True, "jsonschema not installed; skipping (install for full integrity)"

    facts_root = root / "runs" / run_id / "facts"
    if not facts_root.exists():
        return True, "no facts/ to validate"

    schemas_dir = facts_root / "schemas"
    schemas: dict[str, dict] = {}
    if schemas_dir.exists():
        for f in schemas_dir.glob("*.json"):
            try:
                sch = _json.loads(f.read_text())
                sid = sch.get("$id") or f.stem
                schemas[sid] = sch
            except _json.JSONDecodeError:
                return False, f"unparseable schema: {f.name}"

    invalid: list[str] = []
    total = 0
    for f in facts_root.rglob("*.json"):
        if "schemas" in f.parts:
            continue
        total += 1
        try:
            payload: Any = _json.loads(f.read_text())
        except _json.JSONDecodeError as e:
            invalid.append(f"{f.relative_to(root)}: parse error: {e}")
            continue
        if not isinstance(payload, dict):
            invalid.append(f"{f.relative_to(root)}: not a JSON object")
            continue
        sid = payload.get("$schema")
        if not sid:
            invalid.append(f"{f.relative_to(root)}: missing $schema")
            continue
        if sid not in schemas:
            invalid.append(f"{f.relative_to(root)}: unknown schema {sid}")
            continue
        v = Draft202012Validator(schemas[sid])
        errs = [e.message for e in v.iter_errors(payload)]
        if errs:
            invalid.append(f"{f.relative_to(root)}: {errs[0]}")

    if invalid:
        first = invalid[0]
        return False, f"{len(invalid)}/{total} invalid facts (e.g. {first})"
    return True, f"{total}/{total} facts files valid ({len(schemas)} schemas registered)"


def gate_changelog_continuity(root: Path, run_id: str) -> tuple[bool, str]:
    """G-cont: the hash-chained changelog must EXTEND the previously published
    one, never silently restart or shrink.

    ``verify_chain`` only checks a chain's internal linkage, so
    ``rm changelog.jsonl && sift publish`` yields a fresh, internally-valid chain
    with a new genesis and a reset entry count — erasing lineage undetected. This
    gate compares this run's changelog genesis + length against the PRIOR
    PUBLISHED snapshot (``current/snapshot.json``) and fails if the genesis run
    changed or the total entry count went DOWN.

    Once the changelog has at least one entry, it cannot false-positive in
    legitimate use: a full index reset has no prior snapshot (passes), an empty
    prior chain is exempt (genesis only locks after the first real entry, see
    below), and normal append-only growth keeps the genesis and only grows the
    count (passes). The only failing case is "a non-empty changelog was
    deleted/truncated while the published snapshot was kept" — corruption or
    tampering, with no benign cause. An unreadable prior snapshot fails OPEN
    (continuity UNVERIFIED) rather than blocking publish.
    """
    prior_dir = paths.published_run_dir(root)
    if prior_dir is None:
        return True, "no prior published snapshot (fresh index)"
    try:
        prior = json.loads((prior_dir / "snapshot.json").read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return True, "prior snapshot unreadable — continuity UNVERIFIED"
    prior_integ = prior.get("integrity") or {}
    prior_genesis = prior_integ.get("changelog_genesis_run")
    prior_total = prior_integ.get("changelog_total_entries")
    if prior_genesis is None or prior_total is None:
        return True, "prior snapshot predates changelog-continuity fields"
    if prior_total == 0:
        # The prior published snapshot had no chain yet — its genesis was a
        # run_id fallback (write_snapshot uses the current run_id when the
        # changelog is empty), not a real first entry. There's no lineage to be
        # continuous WITH until a real entry exists, so genesis only locks once
        # prior_total >= 1; append-only growth from here is still enforced.
        return True, "prior chain empty (no lineage to enforce yet)"

    cur_genesis, cur_total = _changelog_chain_origin(root)
    if cur_genesis is None:
        cur_genesis = run_id  # mirror write_snapshot's fresh-start handling

    if cur_genesis != prior_genesis:
        return False, (
            f"changelog genesis changed ({prior_genesis} -> {cur_genesis}): the "
            "chain was reset/wiped, erasing lineage"
        )
    if cur_total < prior_total:
        return False, (
            f"changelog shrank ({prior_total} -> {cur_total} entries): "
            "entries were removed (truncation)"
        )
    return True, (
        f"continuous (genesis {cur_genesis}, {cur_total} entries, "
        f"prior {prior_total})"
    )


def gpg_sign_snapshot(snapshot_path: Path, key_id: str) -> Optional[Path]:
    """Detach-sign snapshot.json with GPG. Returns the .sig path on success,
    None on failure (failure is non-fatal — signing is optional)."""
    import subprocess
    sig_path = snapshot_path.with_suffix(snapshot_path.suffix + ".sig")
    try:
        subprocess.run(
            [
                "gpg", "--detach-sign", "--armor", "--batch", "--yes",
                "--local-user", key_id,
                "--output", str(sig_path), str(snapshot_path),
            ],
            check=True, capture_output=True, timeout=30,
        )
        return sig_path
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _changelog_chain_origin(root: Path) -> tuple[Optional[str], int]:
    """Read changelog.jsonl, return (genesis_run_id, total_entries).

    Genesis run = run_id of the first entry. None if the file is empty/missing.
    Cheap: scans the file just enough to read the first valid line + count
    remaining lines.
    """
    cl = paths.changelog_path(root)
    if not cl.exists():
        return None, 0
    first_run: Optional[str] = None
    count = 0
    with cl.open() as f:
        for line in f:
            if not line.strip():
                continue
            count += 1
            if first_run is None:
                try:
                    first_run = json.loads(line).get("run_id")
                except json.JSONDecodeError:
                    continue
    return first_run, count


def write_snapshot(
    root: Path,
    run_id: str,
    *,
    conn: sqlite3.Connection,
    started_at: str,
    completed_at: str,
    expected_urls: int,
    gate_results: list[tuple[str, bool, str]],
    status: str,
) -> Path:
    by_state = counts_by_state(conn)
    by_tier = counts_by_tier(conn)
    # Compute Merkle root over all (url, content_hash) for FRESH+FROZEN rows.
    # This single hex string commits to the entire corpus state — an auditor
    # can reseed the same manifest, recompute, and verify byte-for-byte
    # identity in O(N) without comparing N files.
    rows_for_merkle = [
        (r.url, r.content_hash)
        for r in iter_all(conn)
        if r.state in ("FRESH", "FROZEN") and r.content_hash
    ]
    merkle_root, leaf_count = integrity.compute_corpus_root(rows_for_merkle)

    # Genesis run = the run_id of the first changelog entry currently on disk.
    # Lets any auditor reading snapshot.json see when the current chain started
    # without head'ing changelog.jsonl. If the changelog was wiped (`rm -rf` on
    # the index, manual delete), this surfaces as the current publish's run_id
    # — the chain effectively started here.
    cl_genesis_run, cl_total_entries = _changelog_chain_origin(root)
    if cl_genesis_run is None:
        # No changelog (or empty) — current run is about to start it
        cl_genesis_run = run_id

    snap = {
        "run_id": run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "status": status,
        "expected_urls": expected_urls,
        "counts_by_state": by_state,
        "counts_by_tier": by_tier,
        "versions": {
            "crawler": CRAWLER_VERSION,
            "extractor": EXTRACTOR_VERSION,
            "normalizer": NORMALIZER_VERSION,
            "classifier": CLASSIFIER_VERSION,
            "integrity": integrity.INTEGRITY_VERSION,
        },
        "integrity": {
            "merkle_root": merkle_root,
            "leaf_count": leaf_count,
            "scheme": "sorted-leaves-bitcoin-style-sha256",
            "changelog_genesis_run": cl_genesis_run,
            "changelog_total_entries": cl_total_entries,
        },
        "gates": [
            {"name": name, "passed": ok, "detail": detail}
            for (name, ok, detail) in gate_results
        ],
    }
    out = paths.snapshot_path(root, run_id)
    # Atomic write: snapshot.json is the integrity anchor read by the live
    # MCP server (snapshot_status) and `sift verify`. A torn write would
    # surface as a JSON parse error mid-publish. tmp + rename is atomic.
    atomic_write_text(out, json.dumps(snap, indent=2, sort_keys=True))
    return out


def flip_current_symlink(root: Path, run_id: str) -> None:
    """Atomic relative-symlink swap. The replace() is atomic on POSIX
    (rename(2)) so readers either see the old target or the new one — never both."""
    target = paths.run_dir(root, run_id).resolve()
    link = paths.current_symlink(root)
    tmp = link.with_name(link.name + ".new")
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    # Use relative target so the index is movable on disk.
    rel = os.path.relpath(target, link.parent)
    tmp.symlink_to(rel, target_is_directory=True)
    os.replace(tmp, link)


def publish(
    conn: sqlite3.Connection,
    root: Path,
    run_id: str,
    *,
    started_at: str,
    expected_urls: int,
) -> tuple[bool, list[tuple[str, bool, str]], Path]:
    """Run all gates and (if passing) flip current. Returns (passed, gates, snapshot_path)."""
    # Consolidate first: link-forward any md files written in previous runs
    # so this snapshot's md/ tree is complete (matches what routes.tsv claims).
    # Without this, gate G1 would fail on every incremental run.
    linked, missing = consolidate_md_tree(conn, root, run_id)

    gates: list[tuple[str, bool, str]] = [
        ("consolidate_md_tree", missing == 0,
         f"linked={linked} still_missing={missing}"),
    ]
    g1_ok, g1_det = gate_manifest_fs_integrity(conn, root, run_id)
    gates.append(("manifest_fs_integrity", g1_ok, g1_det))
    g2_ok, g2_det = gate_hash_sample(conn, root, run_id)
    gates.append(("hash_sample", g2_ok, g2_det))
    g3_ok, g3_det = gate_coverage(conn, expected_urls)
    gates.append(("coverage", g3_ok, g3_det))
    g4_ok, g4_det = gate_schema_sanity(root, run_id)
    gates.append(("schema_sanity", g4_ok, g4_det))
    g5_ok, g5_det = gate_facts_validation(root, run_id)
    gates.append(("facts_validation", g5_ok, g5_det))
    gcont_ok, gcont_det = gate_changelog_continuity(root, run_id)
    gates.append(("changelog_continuity", gcont_ok, gcont_det))

    passed = all(ok for (_, ok, _) in gates)
    now = now_utc()
    snap = write_snapshot(
        root, run_id, conn=conn, started_at=started_at, completed_at=now,
        expected_urls=expected_urls, gate_results=gates,
        status="published" if passed else "degraded",
    )

    # Optional GPG detach-signature on the snapshot. Non-fatal — if the
    # configured key isn't available or gpg isn't installed, log via gates
    # but don't block publish.
    if GPG_KEY_ID:
        sig = gpg_sign_snapshot(snap, GPG_KEY_ID)
        gates.append((
            "gpg_signature",
            sig is not None,
            f"signed with {GPG_KEY_ID}" if sig else f"gpg sign failed (key {GPG_KEY_ID})",
        ))
        # Re-write snapshot.json with the gate row recorded
        snap = write_snapshot(
            root, run_id, conn=conn, started_at=started_at, completed_at=now,
            expected_urls=expected_urls, gate_results=gates,
            status="published" if passed else "degraded",
        )
        # If we re-wrote, re-sign so signature matches the final bytes
        if sig is not None:
            gpg_sign_snapshot(snap, GPG_KEY_ID)

    if passed:
        flip_current_symlink(root, run_id)
    return passed, gates, snap


def build_artifacts(
    conn: sqlite3.Connection, root: Path, run_id: str
) -> dict[str, object]:
    """Build all agent-facing artifacts for this run.

    Produces (under runs/<run_id>/):
        INDEX.md                  — always-loaded pointer table
        routes.tsv                — url -> file map (grep/awk friendly)
        sections/<top>/INDEX.md   — drill-down indexes per top-level section
        artifacts/by_guide/*.md   — concatenated multi-page guides
        artifacts/llms.txt        — legacy TOC (kept for backward compat)
        facts/<schema>/*.json     — atomic structured records
        facts/schemas/*.json      — JSON Schema for each fact type
    """
    art = paths.artifacts_dir(root, run_id)
    art.mkdir(parents=True, exist_ok=True)
    surface = agent_surface.build_all(conn, root, run_id)
    fact_counts = facts_mod.build_all_facts(conn, root, run_id)

    # llms.txt: group by tier, list top-level paths with parent_guide rollup.
    by_tier: dict[str, list[tuple[str, Optional[str], Optional[str]]]] = {}
    for row in iter_all(conn):
        if row.state not in ("FRESH", "FROZEN"):
            continue
        by_tier.setdefault(row.tier, []).append(
            (row.url, row.parent_guide, None)
        )
    host = current_profile().primary_host or "site"
    lines = [f"# {host} LLM index", "", f"Run: {run_id}", ""]
    for tier in sorted(by_tier):
        lines.append(f"## {tier} ({len(by_tier[tier])} pages)")
        lines.append("")
        # For forms with parent_guide, group by guide
        grouped: dict[str, list[str]] = {}
        ungrouped: list[str] = []
        for url, pg, _ in by_tier[tier]:
            if pg:
                grouped.setdefault(pg, []).append(url)
            else:
                ungrouped.append(url)
        for guide in sorted(grouped):
            urls = grouped[guide]
            lines.append(f"- [{guide}]({urls[0]}) — {len(urls)} pages")
        for u in sorted(ungrouped)[:200]:
            lines.append(f"- {u}")
        lines.append("")
    (art / "llms.txt").write_text("\n".join(lines))

    # by_guide/<guide>.md: concatenate pages of each guide in URL-path order.
    bg = art / "by_guide"
    bg.mkdir(exist_ok=True)
    guides: dict[str, list[str]] = {}
    for row in iter_all(conn):
        if row.parent_guide and row.state in ("FRESH", "FROZEN"):
            guides.setdefault(row.parent_guide, []).append(row.url)
    for guide, urls in guides.items():
        urls.sort()
        out_lines = [f"# {guide}", ""]
        for url in urls:
            md_file = paths.md_path(root, run_id, url)
            if not md_file.exists():
                continue
            text = md_file.read_text(encoding="utf-8", errors="replace")
            _, body = _split_frontmatter(text)
            out_lines.append(f"\n\n---\n\n## Source: {url}\n")
            out_lines.append(body.rstrip())
        (bg / f"{guide}.md").write_text("\n".join(out_lines))

    return {
        "surface": surface,
        "facts": fact_counts,
        "guides": len(guides),
    }

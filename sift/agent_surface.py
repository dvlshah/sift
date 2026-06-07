"""Generate the agent-facing navigation surface from the manifest.

Produces artifacts a grep-first agent harness can navigate without embeddings:

    INDEX.md         — always-loaded pointer table (~150 chars/line, <200 lines)
    routes.tsv       — url \t md_path \t tier \t content_hash \t fetched_at
    sections/<top>/INDEX.md  — drill-down indexes per top-level URL section

The principle: this file holds NO content, only pointers. Agents read this
to decide where to look; they never grep INDEX.md for facts.

Outputs are deterministic given (manifest state, run_id) so two consecutive
runs against the same state produce identical artifacts.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

from . import paths
from .classify import audience as audience_for, fy_years as fy_years_for
from .manifest import iter_all
from .sites import current_profile


def _section_order() -> list[tuple[str, str, str]]:
    """Per-site section taxonomy from the active profile, used to lay out
    the root INDEX.md. Sections not in this list still get an INDEX.md but
    appear below the curated ones, in alphabetical order."""
    return current_profile().section_order


# (No module-level SECTION_ORDER — callers go through _section_order() so the
# value tracks the active profile if it changes at runtime, e.g. in tests.)


def _md_relpath(root: Path, run_id: str, url: str) -> str:
    """Path of the md file relative to the run dir (so INDEX.md links work
    regardless of where the run dir is mounted)."""
    md = paths.md_path(root, run_id, url)
    run = paths.run_dir(root, run_id)
    try:
        return str(md.relative_to(run))
    except ValueError:
        return str(md)


def _top_segment(url: str) -> str:
    parts = [p for p in urlparse(url).path.split("/") if p]
    return parts[0].lower() if parts else ""


def build_routes_tsv(conn: sqlite3.Connection, root: Path, run_id: str) -> Path:
    """One row per FRESH/FROZEN URL. TSV for awk/grep ergonomics.

    Columns: url \t md_path \t tier \t content_hash \t fetched_at \t audience \t fy_years
    """
    out = paths.routes_tsv_path(root, run_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        f.write("url\tmd_path\ttier\tcontent_hash\tfetched_at\taudience\tfy_years\n")
        for row in iter_all(conn):
            if row.state not in ("FRESH", "FROZEN"):
                continue
            if not row.content_hash:
                continue  # FRESH but no md (e.g. extract failed)
            md_rel = _md_relpath(root, run_id, row.url)
            fys = ",".join(fy_years_for(row.url))
            aud = audience_for(row.url)
            f.write(
                f"{row.url}\t{md_rel}\t{row.tier}\t{row.content_hash}\t"
                f"{row.last_fetched_at or ''}\t{aud}\t{fys}\n"
            )
    return out


def build_index_md(conn: sqlite3.Connection, root: Path, run_id: str) -> Path:
    """Root INDEX.md: section pointers + structured-data pointers + tooling pointers.

    Stays under ~200 lines so an agent harness can always-load it. We do NOT
    list every page — that's what `sections/<x>/INDEX.md` is for, drilled to
    on demand.
    """
    by_section: dict[str, list] = defaultdict(list)
    parent_guide_pages: dict[str, list[str]] = defaultdict(list)
    for row in iter_all(conn):
        if row.state not in ("FRESH", "FROZEN"):
            continue
        seg = _top_segment(row.url)
        by_section[seg].append(row)
        if row.parent_guide:
            parent_guide_pages[row.parent_guide].append(row.url)

    host = current_profile().primary_host or "site"
    lines = [
        f"# {host} — agent index",
        "",
        f"Snapshot: `{run_id}`  •  Sections: {len(by_section)}",
        "",
        "## How to navigate",
        "",
        "- Sections below point to per-section indexes — drill in for full URL lists.",
        "- `routes.tsv` maps every URL to its markdown file (grep-friendly).",
        "- `facts/*.json` holds atomic structured records (rate tables, caps, deadlines).",
        "- `changelog.jsonl` (at index root) is **append-only across all publishes**",
        "  within this index root. Each entry's `prev_hash` chains to the previous entry's",
        "  `entry_hash`; `sift verify-changelog` walks the chain. Deleting the file",
        "  (or `rm -rf` on the index root) discards history — `snapshot.json.changelog_genesis_run`",
        "  records the run-id of the first entry in the current chain.",
        "- Every markdown file carries YAML frontmatter with `url`, `content_hash`,",
        "  `tier`, `audience`, `fy_years`, and `anchors` for self-verification.",
        "",
        "## Sections",
        "",
    ]
    for seg, audience, heading in _section_order():
        rows = by_section.get(seg, [])
        if not rows:
            continue
        n = len(rows)
        lines.append(
            f"- **{heading}** ({audience}, {n} pages) "
            f"→ `sections/{seg}/INDEX.md`"
        )
    # Any sections we didn't anticipate
    known = {seg for (seg, _, _) in _section_order()}
    extras = sorted(s for s in by_section if s and s not in known)
    for seg in extras:
        n = len(by_section[seg])
        lines.append(f"- {seg} ({n} pages) → `sections/{seg}/INDEX.md`")

    lines += [
        "",
        "## Structured data",
        "",
        "- `facts/` — atomic records as JSON (preferred for exact numeric lookups).",
        "  Each file has a `$schema` field and a `source_url` + `content_hash` for provenance.",
        "",
        "## Tooling pointers",
        "",
        "- `routes.tsv` — `url\\tmd_path\\ttier\\tcontent_hash\\tfetched_at\\taudience\\tfy_years`",
        "- `manifest.db` — SQLite (`SELECT * FROM manifest`) — full per-URL state",
        "- `snapshot.json` — gate results + version pins for this snapshot",
        "- `../changelog.jsonl` — `{ts,url,change_type,old_hash,new_hash,run_id,tier}` per change",
        "",
        "## Grep recipes",
        "",
        "```",
        "# Find a file by URL fragment",
        "grep -l 'url: https://.*tax-return' md/",
        "",
        "# Find pages for a financial year",
        "awk -F'\\t' '$7 ~ /2025-26/ {print $1}' routes.tsv",
        "",
        "# Jump to a heading by anchor",
        "grep -rn '{#cents-per-kilometre}' md/",
        "",
        "# Recent changes",
        "tail -n 100 ../changelog.jsonl | jq -r '[.ts,.change_type,.url]|@tsv'",
        "```",
        "",
    ]
    out = paths.index_md_path(root, run_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines))
    return out


def build_section_index(
    conn: sqlite3.Connection,
    root: Path,
    run_id: str,
    section: str,
) -> Path:
    """Per-section INDEX.md: pointer table, plain-text title, drill paths.

    Forms-and-instructions gets special treatment: grouped by `parent_guide`
    with the per-guide page count, so an agent can jump straight to the
    correct guide rather than scanning thousands of individual sub-pages.
    """
    rows = [
        r for r in iter_all(conn)
        if r.state in ("FRESH", "FROZEN") and _top_segment(r.url) == section
    ]
    rows.sort(key=lambda r: r.url)

    audience_label = next(
        (a for (s, a, _) in _section_order() if s == section), "general"
    )
    heading = next(
        (h for (s, _, h) in _section_order() if s == section), section
    )
    lines = [
        f"# {heading}",
        "",
        f"Audience: `{audience_label}`  •  Pages: {len(rows)}",
        "",
    ]

    if section == "forms-and-instructions":
        guides: dict[str, list] = defaultdict(list)
        standalone: list = []
        for r in rows:
            if r.parent_guide:
                guides[r.parent_guide].append(r)
            else:
                standalone.append(r)
        if guides:
            lines += ["## Multi-page guides", ""]
            for guide in sorted(guides):
                pages = guides[guide]
                fys = sorted({fy for r in pages for fy in fy_years_for(r.url)})
                fy_tag = f" [{', '.join(fys)}]" if fys else ""
                lines.append(
                    f"- **{guide}**{fy_tag} — {len(pages)} pages "
                    f"→ `../../artifacts/by_guide/{guide}.md`"
                )
            lines.append("")
        if standalone:
            lines += ["## Standalone forms", ""]
            for r in standalone:
                lines.append(
                    f"- `{r.tier}` {r.url} → `../../{_md_relpath(root, run_id, r.url)}`"
                )
            lines.append("")
    else:
        # Default: alphabetized URL list with tier and md link.
        for r in rows:
            fys = fy_years_for(r.url)
            fy_tag = f" [{', '.join(fys)}]" if fys else ""
            lines.append(
                f"- `{r.tier}`{fy_tag} {r.url} → `../../{_md_relpath(root, run_id, r.url)}`"
            )

    out = paths.section_index_path(root, run_id, section)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines))
    return out


def build_all_section_indexes(
    conn: sqlite3.Connection, root: Path, run_id: str
) -> list[Path]:
    sections_seen: set[str] = set()
    for row in iter_all(conn):
        if row.state not in ("FRESH", "FROZEN"):
            continue
        seg = _top_segment(row.url)
        if seg:
            sections_seen.add(seg)
    return [build_section_index(conn, root, run_id, s) for s in sorted(sections_seen)]


def build_all(conn: sqlite3.Connection, root: Path, run_id: str) -> dict[str, str]:
    """Build INDEX.md, routes.tsv, and all per-section indexes.

    Returns a dict mapping artifact -> path string.
    """
    out: dict[str, str] = {}
    out["index_md"] = str(build_index_md(conn, root, run_id))
    out["routes_tsv"] = str(build_routes_tsv(conn, root, run_id))
    section_paths = build_all_section_indexes(conn, root, run_id)
    out["section_indexes"] = str(len(section_paths))
    return out

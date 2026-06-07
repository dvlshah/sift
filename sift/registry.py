"""Index discovery for multi-index deployments.

A sift "index registry" is a parent directory containing one or more sift
indexes — each subdirectory is its own ``sift init``-shaped root with
``manifest.db`` + ``current/`` (or ``runs/<id>/``). The registry lets a
single MCP server expose many indexes; the agent calls ``list_indexes``
to see what's available and then routes its grep / read tools by slug.

This module is the single source of truth for "is this directory a sift
root?" and "what's the canonical slug + description?" — both
``sift mcp`` and the agent-loop bench import from here so the discovery
rules don't drift between consumers.

Single-index vs multi-index resolution:

  * ``discover_indexes(p)`` first checks whether ``p`` itself is a sift
    root. If so, returns ``[p]`` with the directory's name as the slug —
    backward-compat with the original "one MCP per index" pattern.
  * Otherwise, ``p`` is treated as a parent directory and every immediate
    subdirectory that's a sift root is returned.

Slug + description sources, in order:

  1. ``[index]`` section in the root's ``sift.toml`` (slug, description,
     domain, tags). Operators set these once when they create the index.
  2. Directory name as the slug fallback.
  3. The host extracted from a sample published URL as the description
     fallback. We don't fall back to "no description" silently — an
     unlabeled index is one the agent can't reason about.

Liveness:

  * ``last_published`` is the run-id stamped on the ``current`` symlink
    (or, on macOS where the bench's relative symlinks overshoot, the
    newest entry under ``runs/``). Sorted lexicographically, since run-
    ids are ISO timestamps.
  * ``page_count`` is taken from the latest run's snapshot.json if
    available; otherwise from a quick md/ file count, capped to keep
    discovery cheap.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import paths
from ._io import read_snapshot
from .manifest import open_manifest_ro


# ---- Sift-root detection --------------------------------------------------

def latest_run_dir(root: Path) -> Optional[Path]:
    """Newest ``runs/*`` subdirectory by name (ISO timestamps sort
    chronologically). Returns ``None`` when no runs/* exists. Used by
    callers that can't trust the ``current`` symlink — see ``is_sift_root``."""
    runs = root / "runs"
    if not runs.is_dir():
        return None
    candidates = [d for d in runs.iterdir() if d.is_dir()]
    if not candidates:
        return None
    return sorted(candidates, key=lambda d: d.name)[-1]


def is_sift_root(p: Path) -> bool:
    """A directory is a sift root if it has a usable published run.

    Two ways to satisfy this:
      1. The ``current`` symlink resolves to a real directory (the
         standard sift convention).
      2. There's at least one entry under ``runs/`` (fallback for cases
         where the symlink is broken — most commonly the bench writes
         relative symlinks that don't resolve through macOS's
         ``/tmp -> /private/tmp`` indirection).

    The intent is "an MCP server could publish read tools over this" —
    callers should not see indexes that have nothing to read.
    """
    if not p.is_dir():
        return False
    cur = p / "current"
    if cur.exists():
        return True
    return latest_run_dir(p) is not None


# ---- IndexDescriptor + discovery -----------------------------------------

@dataclass(frozen=True)
class RunSummary:
    """A compact view of one row in the manifest's ``runs`` table — what
    the agent needs at discovery time to see "is this index actively
    being indexed" vs "stale for months." Not the same shape as
    ``index_status``; that one has full counts + error detail."""
    run_id: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    status: Optional[str] = None       # succeeded | degraded | failed | running
    phase: Optional[str] = None        # current pipeline phase

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "phase": self.phase,
        }


@dataclass(frozen=True)
class IndexDescriptor:
    """One index in the registry, with the metadata the agent needs to
    pick it before grepping AND to know whether/where to write."""
    slug: str
    root: Path                      # absolute path to the sift root
    description: str                # what the index covers (operator-supplied)
    domain: Optional[str] = None    # primary host this index mirrors, when set
    tags: tuple[str, ...] = ()      # operator-supplied labels for filtering
    page_count: Optional[int] = None
    last_published: Optional[str] = None  # run-id (ISO timestamp) of the active publish
    # ---- Write-side surface ----
    # ``accepts_writes`` is the *capability* (the sift.toml has a usable
    # [seed].host_allow). It does NOT consider whether the MCP server was
    # started with --enable-index — the MCP layer overlays that. Keeping
    # capability and server-enablement separate lets the same descriptor
    # be used for non-server contexts (e.g. CLI tooling).
    accepts_writes: bool = False
    allowed_hosts: tuple[str, ...] = ()   # union of hosts this index will accept writes for
    # ---- Liveness signals for the agent at discovery time ----
    # ``unseen_count`` is rows in state='UNSEEN' — URLs the manifest
    # knows about but hasn't fetched. Nonzero = "if your question maps
    # to one of these, ask me to fetch them." The discovery-time
    # legibility prompt for the self-heal loop.
    unseen_count: Optional[int] = None
    recent_runs: tuple[RunSummary, ...] = ()

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "root": str(self.root),
            "description": self.description,
            "domain": self.domain,
            "tags": list(self.tags),
            "page_count": self.page_count,
            "last_published": self.last_published,
            "accepts_writes": self.accepts_writes,
            "allowed_hosts": list(self.allowed_hosts),
            "unseen_count": self.unseen_count,
            "recent_runs": [r.to_dict() for r in self.recent_runs],
        }


def _load_toml(path: Path) -> dict:
    """Parse a TOML file to a dict. Tolerant: missing file, missing tomllib,
    or a malformed file all return ``{}``. Registry discovery reads
    ``[index]`` / ``[seed]`` out of an index's sift.toml through this rather
    than the full config loader (which would pull dataclass validation we
    don't need here)."""
    if not path.exists():
        return {}
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore
        except ImportError:
            return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, ValueError):
        return {}


def _load_index_section(toml_path: Path) -> dict:
    """Return the ``[index]`` section from a sift.toml (``{}`` when absent)."""
    section = _load_toml(toml_path).get("index")
    return section if isinstance(section, dict) else {}


def _published_run_dir(root: Path) -> Optional[Path]:
    """Resolve the genuinely-published run for this index.

    Delegates to the single canonical resolver in ``paths`` so registry
    discovery and the MCP content tools can never disagree about what is
    published. In particular this does NOT fall back to ``runs/<latest>``:
    a run that failed its publish gates writes ``runs/<id>/`` but never
    flips ``current``, and serving it as published was a provenance hole
    (an index advertised as published whose every read then 404s).
    ``is_sift_root`` keeps the looser latest_run_dir check — discovery
    ("is there anything here") is deliberately broader than publication
    ("did a run pass all gates")."""
    return paths.published_run_dir(root)


def _infer_domain_from_pages(run_dir: Path) -> Optional[str]:
    """Best-effort: pick a host from one of the snapshot's pages. We use
    this only when no ``[index] domain`` is set in sift.toml."""
    md = run_dir / "md"
    if not md.is_dir():
        return None
    # Walk a couple of md files and look for the frontmatter ``url:`` line.
    import re as _re
    rx = _re.compile(r"^url:\s*(\S+)", _re.MULTILINE)
    for f in md.rglob("*.md"):
        try:
            head = f.read_text(encoding="utf-8", errors="replace")[:1024]
        except OSError:
            continue
        m = rx.search(head)
        if m:
            url = m.group(1)
            try:
                return url.split("//", 1)[-1].split("/", 1)[0].lower()
            except IndexError:
                continue
    return None


def writeable_capability(root: Path) -> tuple[bool, tuple[str, ...]]:
    """Read the index's sift.toml and return ``(accepts_writes, hosts)``.

    Accepts writes iff ``[seed].host_allow`` resolves to a non-empty
    tuple. The hosts are lowercased so the MCP server can compare
    against URL hosts without per-call normalization.

    Empty sift.toml, missing sift.toml, or empty host_allow all return
    ``(False, ())`` — the agent should see "writeable: false" in those
    cases so it doesn't try ``index_url`` on an index that has no
    allow-list to enforce."""
    data = _load_toml(root / "sift.toml")
    seed_section = data.get("seed") or {}
    raw_hosts = seed_section.get("host_allow") or ()
    if not isinstance(raw_hosts, (list, tuple)):
        return False, ()
    hosts = tuple(str(h).lower().strip() for h in raw_hosts if h)
    if not hosts:
        return False, ()
    return True, hosts


def unseen_count_for(root: Path) -> Optional[int]:
    """Count manifest rows in state='UNSEEN' — i.e. URLs the manifest
    knows about but hasn't fetched yet. Surface in ``list_indexes`` so
    the agent can see at a glance which indexes have ready-to-fill
    coverage gaps."""
    conn = open_manifest_ro(root / "manifest.db")
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM manifest WHERE state = ?", ("UNSEEN",)
        ).fetchone()
        return int(row[0]) if row is not None else 0
    except sqlite3.Error:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def recent_runs_for(root: Path, *, limit: int = 3) -> tuple[RunSummary, ...]:
    """Read the last ``limit`` rows from the manifest's ``runs`` table.

    Newest first. Returns ``()`` when the table is missing or empty —
    e.g. an index that's been initialised but never crawled. The agent
    uses this to see "was this corpus updated last week or last year."
    """
    conn = open_manifest_ro(root / "manifest.db")
    if conn is None:
        return ()
    try:
        rows = list(conn.execute(
            "SELECT run_id, started_at, completed_at, status, phase "
            "FROM runs ORDER BY started_at DESC LIMIT ?",
            (int(limit),),
        ))
    except sqlite3.Error:
        rows = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    out: list[RunSummary] = []
    for r in rows:
        d = dict(r)
        out.append(RunSummary(
            run_id=str(d.get("run_id")),
            started_at=d.get("started_at"),
            completed_at=d.get("completed_at"),
            status=d.get("status"),
            phase=d.get("phase"),
        ))
    return tuple(out)


def describe(root: Path) -> IndexDescriptor:
    """Build an IndexDescriptor for one sift root. Reads the ``[index]``
    section of its sift.toml; falls back to derived values where the
    operator hasn't set anything."""
    section = _load_index_section(root / "sift.toml")
    run_dir = _published_run_dir(root)
    snap = read_snapshot(run_dir) if run_dir is not None else {}

    domain = section.get("domain")
    if not domain and run_dir is not None:
        domain = _infer_domain_from_pages(run_dir)

    # Description fallback ladder: operator-supplied → domain → slug.
    slug = section.get("slug") or root.name
    description = section.get("description")
    if not description:
        description = (f"sift index covering {domain}" if domain
                       else f"sift index '{slug}'")

    # Page count: snapshot's count if available; otherwise a cheap rglob cap.
    page_count: Optional[int] = None
    counts = snap.get("counts_by_state") or snap.get("counts") or {}
    if isinstance(counts, dict):
        page_count = counts.get("FRESH") or counts.get("fresh")
    if page_count is None and run_dir is not None:
        md = run_dir / "md"
        if md.is_dir():
            n = 0
            for _ in md.rglob("*.md"):
                n += 1
                if n >= 50_000:           # cap so discovery stays fast
                    break
            page_count = n

    tags_raw = section.get("tags") or ()
    tags = tuple(str(t) for t in tags_raw) if isinstance(tags_raw, (list, tuple)) else ()

    accepts_writes, allowed_hosts = writeable_capability(root)
    unseen = unseen_count_for(root)
    recent = recent_runs_for(root)

    return IndexDescriptor(
        slug=str(slug),
        root=root.resolve(),
        description=str(description),
        domain=str(domain) if domain else None,
        tags=tags,
        page_count=page_count,
        last_published=run_dir.name if run_dir is not None else None,
        accepts_writes=accepts_writes,
        allowed_hosts=allowed_hosts,
        unseen_count=unseen,
        recent_runs=recent,
    )


def _with_slug(d: IndexDescriptor, new_slug: str) -> IndexDescriptor:
    """Return a copy of ``d`` with a different slug. Used by the
    collision-resolution path in ``discover_indexes`` so adding a field
    to ``IndexDescriptor`` doesn't break the rename."""
    return IndexDescriptor(
        slug=new_slug,
        root=d.root,
        description=d.description,
        domain=d.domain,
        tags=d.tags,
        page_count=d.page_count,
        last_published=d.last_published,
        accepts_writes=d.accepts_writes,
        allowed_hosts=d.allowed_hosts,
        unseen_count=d.unseen_count,
        recent_runs=d.recent_runs,
    )


def discover_indexes(path: Path) -> list[IndexDescriptor]:
    """Discover sift indexes under ``path``.

    Resolution order:
      1. If ``path`` itself is a sift root, return ``[describe(path)]`` —
         single-index mode, backward-compatible with one-MCP-per-index.
      2. Otherwise, ``path`` is a parent directory; every immediate
         subdirectory that's a sift root gets described and returned.

    Returned descriptors are sorted by slug for stable list_indexes
    output (slugs must be unique inside one registry; collisions are
    resolved by appending the directory name when an operator-set slug
    collides with a sibling). Returns ``[]`` when nothing under ``path``
    looks like a sift root.
    """
    path = Path(path).resolve()
    if is_sift_root(path):
        return [describe(path)]
    if not path.is_dir():
        return []
    out = [describe(p) for p in path.iterdir() if is_sift_root(p)]
    # Slug-collision resolution: when two operator-set slugs collide we
    # disambiguate by appending the directory name. Same when the slug
    # falls back to a non-unique name. Keep collision handling explicit
    # so an MCP routing call can never resolve to two roots.
    seen: dict[str, IndexDescriptor] = {}
    deduped: list[IndexDescriptor] = []
    for d in out:
        if d.slug not in seen:
            seen[d.slug] = d
            deduped.append(d)
            continue
        # Rewrite both sides with directory-name suffixes so the listing
        # makes clear which is which. Preserve every field — only the
        # slug changes.
        existing = seen[d.slug]
        deduped.remove(existing)
        a = _with_slug(existing, f"{existing.slug}@{existing.root.name}")
        b = _with_slug(d, f"{d.slug}@{d.root.name}")
        deduped.extend([a, b])
        seen[a.slug] = a
        seen[b.slug] = b
    deduped.sort(key=lambda x: x.slug)
    return deduped


@dataclass(frozen=True)
class IndexRegistry:
    """The full registry the MCP server queries at tool-call time.

    Designed to be cheap to rebuild (~5-10ms per sub-index on a warm
    filesystem); see ``RegistryCache`` for the agent-friendly
    rebuild-on-demand layer the MCP server uses so a freshly-built
    index is visible without restart."""
    indexes: tuple[IndexDescriptor, ...]
    is_multi: bool                  # True when discover_indexes resolved a parent dir
    parent_path: Path               # the original argument the operator passed

    def by_slug(self, slug: str) -> Optional[IndexDescriptor]:
        for d in self.indexes:
            if d.slug == slug:
                return d
        return None

    def slugs(self) -> list[str]:
        return [d.slug for d in self.indexes]

    @classmethod
    def discover(cls, path: Path) -> "IndexRegistry":
        path = Path(path).resolve()
        indexes = discover_indexes(path)
        # Multi-index iff the operator pointed at a parent dir, not at a
        # sift root itself. We test that by checking whether path itself
        # is a sift root — if it is, treat as single-index regardless of
        # how many sub-indexes happen to live under it.
        is_multi = not is_sift_root(path)
        return cls(
            indexes=tuple(indexes),
            is_multi=is_multi,
            parent_path=path,
        )


class RegistryCache:
    """TTL-bounded cache around ``IndexRegistry.discover`` for the MCP
    server's per-call refresh.

    Why a cache (instead of pure rebuild-on-every-call): full discovery
    runs sift.toml parse + manifest opens for every registered index,
    so for a ~25-index parent dir it's ~100-150ms wall. A typical agent
    session does one ``list_indexes`` followed by many scoped grep/read
    calls — caching for ~1 second means the agent pays the discovery
    cost once per burst but newly-built indexes still appear within a
    second of being built.

    Thread-safety: only the MCP server uses this, and tool dispatch is
    serialized through the asyncio loop, so no locking. If we later
    expose a multi-threaded HTTP surface, this needs a lock.
    """

    def __init__(self, path: Path, *, ttl_seconds: float = 1.0):
        self._path = Path(path).resolve()
        self._ttl = float(ttl_seconds)
        self._cached: Optional[IndexRegistry] = None
        self._expires_at: float = 0.0

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def get(self) -> IndexRegistry:
        import time as _time
        now = _time.monotonic()
        if self._cached is None or now >= self._expires_at:
            self._cached = IndexRegistry.discover(self._path)
            self._expires_at = now + self._ttl
        return self._cached

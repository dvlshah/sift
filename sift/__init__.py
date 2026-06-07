"""Deterministic website indexer for LLM-agent consumption (default profile: ATO).

Five-phase pipeline (each phase is resumable from its checkpoint file):

    seed   --> populates manifest with URL seeds (tier + parent_guide assigned)
    plan   --> emits plan.jsonl: per-URL decision (FETCH / FETCH_CONDITIONAL / SKIP)
    fetch  --> async HTTP with token-bucket + conditional GETs, raw HTML by sha256
    extract--> trafilatura -> normalized markdown, content_hash, meta.json
    commit --> single SQLite transaction applying the extract.log to the manifest
    publish--> 5 verification gates then atomic symlink swap to /index/current/

The pipeline is a pure function of (manifest state, URL seeds, sitemap lastmod, clock):
same inputs => same plan => same output snapshot. Each transformation is versioned;
bumping a version triggers re-derivation from cached raw, not refetch.

See README.md for the full design.
"""

__version__ = "0.1.0"
# Provenance stamp on manifest rows — a *behavioral* version (like
# EXTRACTOR_VERSION), bumped only when crawl/fetch behavior changes, not on
# every package release. this is the initial public release; crawl behavior is unchanged, so this stays.
CRAWLER_VERSION = "v1.0.0"


# Default site profile = ATO. Set at package import so any caller that
# imports the package (CLI, tests, eval suite) gets the ATO behavior
# without an explicit profile-load step. The CLI's _load_cli_config()
# overrides this from config.site.profile when invoked.
def _set_default_profile() -> None:
    from .sites import load_profile, set_profile
    try:
        set_profile(load_profile("sift.sites.ato:ATOProfile"))
    except Exception:
        # Don't fail package import if profile module is broken;
        # callers will see a clear error when they hit a profile method.
        pass


_set_default_profile()

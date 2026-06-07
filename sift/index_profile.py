"""Resolve + activate the site profile an index was built under.

The content_hash is a function of the active ``SiteProfile`` —
``normalize_for_hash`` strips the profile's ``dynamic_patterns`` and the
extract dispatch consults the profile's ``body_kind``. So ANY consumer
that re-extracts or re-normalizes from a stored blob (the determinism
eval, ``read_md --verify``) MUST do so under the SAME profile the index
was published with, or it computes a different hash and reports a false
mismatch.

Production write paths get this for free — the CLI calls
``set_profile`` at startup from the run's config. The offline re-hash
consumers did not, which made `read_md --verify` falsely fail on every
non-ATO index (the package default profile is ATO). This module is the
single place that maps an index root → its profile and activates it, so
all re-hash consumers route through identical logic.
"""
from __future__ import annotations

from pathlib import Path

from .config import load_config
from .sites import load_profile, set_profile


def index_profile_path(index_root: Path) -> str:
    """Return the ``module:Class`` profile path an index declares in its
    ``sift.toml`` ``[site].profile`` — or the config default when the
    index has no sift.toml. Resolving through ``load_config`` means the
    default exactly matches what the build used, so re-hash stays
    consistent for indexes built without an explicit ``[site]`` section.
    """
    toml = index_root / "sift.toml"
    cfg = load_config(toml if toml.exists() else None)
    return cfg.site.profile


def apply_index_profile(index_root: Path) -> str:
    """Activate the index's profile as the process-global active profile,
    so subsequent ``normalize_for_hash`` / extract calls reproduce the
    hash the index was built with. Returns the activated profile path.

    Note: this mutates the process-global profile (the same global the
    whole pipeline uses). Callers in the long-lived MCP server rely on
    serialized tool dispatch, so a per-call activation is safe; there's
    no concurrent profile race under the current single-loop dispatch.
    """
    profile_path = index_profile_path(index_root)
    set_profile(load_profile(profile_path))
    return profile_path

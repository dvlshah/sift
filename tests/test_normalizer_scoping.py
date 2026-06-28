"""normalizer_version() fingerprints the active profile's dynamic_patterns.

normalize_for_hash strips the active profile's dynamic_patterns, so the
content_hash depends on them. Without folding a fingerprint of those patterns
into the stored version, editing a profile's noise-stripping would leave every
row's normalizer_version reading "v2", and the decide/extract idempotency
short-circuit would skip re-extraction — silently keeping stale content_hashes.
"""
import re

import pytest

from sift.normalize import NORMALIZER_VERSION, normalizer_version
from sift.sites import SiteProfile, current_profile, load_profile, set_profile


class _NoPatterns(SiteProfile):
    pass


class _TwoPatterns(SiteProfile):
    @property
    def dynamic_patterns(self):
        return (re.compile(r"Last modified: .*"), re.compile(r"QC \d+"))


class _TwoPatternsReordered(SiteProfile):
    @property
    def dynamic_patterns(self):
        return (re.compile(r"QC \d+"), re.compile(r"Last modified: .*"))


class _DifferentPatterns(SiteProfile):
    @property
    def dynamic_patterns(self):
        return (re.compile(r"Last modified: .*"), re.compile(r"QC \d+ EXTRA"))


class _FlagDiffers(SiteProfile):
    @property
    def dynamic_patterns(self):
        return (re.compile(r"Last modified: .*"), re.compile(r"QC \d+", re.IGNORECASE))


@pytest.fixture(autouse=True)
def restore_profile():
    """Don't leak a swapped-in profile into other tests."""
    prev = current_profile()
    yield
    set_profile(prev)


def test_no_patterns_returns_bare_base():
    # Zero-pattern profiles keep the bare version so existing indexes don't churn.
    set_profile(_NoPatterns())
    assert normalizer_version() == NORMALIZER_VERSION


def test_patterns_extend_the_version():
    set_profile(_TwoPatterns())
    v = normalizer_version()
    assert v != NORMALIZER_VERSION
    assert v.startswith(NORMALIZER_VERSION + "+")


def test_changing_a_pattern_changes_the_version():
    # The core bug this PR fixes: a pattern edit must invalidate stored hashes.
    set_profile(_TwoPatterns())
    v1 = normalizer_version()
    set_profile(_DifferentPatterns())
    assert normalizer_version() != v1


def test_same_patterns_are_deterministic():
    set_profile(_TwoPatterns())
    v1 = normalizer_version()
    set_profile(_TwoPatterns())  # fresh instance, identical patterns
    assert normalizer_version() == v1


def test_pattern_order_is_significant():
    # Patterns apply in sequence (one's output can feed the next), so a reorder
    # may change output -> conservatively change the fingerprint.
    set_profile(_TwoPatterns())
    v1 = normalizer_version()
    set_profile(_TwoPatternsReordered())
    assert normalizer_version() != v1


def test_regex_flags_change_the_version():
    set_profile(_TwoPatterns())
    v1 = normalizer_version()
    set_profile(_FlagDiffers())
    assert normalizer_version() != v1


def test_real_ato_profile_is_fingerprinted():
    # The reference profile contributes real dynamic_patterns -> non-bare version.
    set_profile(load_profile("sift.sites.ato:ATOProfile"))
    assert len(current_profile().dynamic_patterns) > 0
    assert normalizer_version().startswith(NORMALIZER_VERSION + "+")

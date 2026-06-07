"""Shared pytest fixtures.

The key one here is process-global state isolation. sift keeps two pieces
of mutable module-level state that many modules read at call time:

  * the active site profile (``sift.sites.current_profile()``)
  * the current-FY start year (``sift.classify.current_fy_start_year()``)

A handful of tests legitimately mutate them (``set_profile`` /
``apply_index_profile`` / ``set_current_fy_start_year``). Before this
fixture existed, those mutations leaked across tests and made the suite
order-dependent — e.g. ``test_classify_picks_up_new_fy`` passed in the full
run but failed when a sibling test that left a non-ATO profile active ran
first. An order-dependent suite is a production-release liability: it can go
green locally and red in CI (or vice-versa) purely on collection order.

The autouse fixture snapshots both globals before each test and restores
them afterwards, so every test starts from a known baseline and none can
poison its neighbours.
"""
from __future__ import annotations

import pytest

from sift import classify as _classify
from sift import sites as _sites


@pytest.fixture(autouse=True)
def _restore_global_profile_state():
    profile = _sites.current_profile()
    fy_start = _classify.current_fy_start_year()
    try:
        yield
    finally:
        _sites.set_profile(profile)
        _classify.set_current_fy_start_year(fy_start)

"""Config loader: defaults, file-based overrides, partial tier overrides, errors."""

from datetime import timedelta

import pytest

from sift import classify as classify_mod, decide as decide_mod, publish as publish_mod
from sift.config import (
    DEFAULT_TIERS,
    IndexConfig,
    load_config,
)


@pytest.fixture(autouse=True)
def reset_module_state():
    """Reset module-level mutable state between tests so config tests don't
    bleed into one another or into other test files."""
    yield
    # Restore default tier intervals + FY year for downstream tests
    decide_mod.apply_config(IndexConfig())
    publish_mod.apply_config(IndexConfig())
    classify_mod.set_current_fy_start_year(2025)


class TestDefaultsWhenNoFile:
    def test_no_config_path_returns_defaults(self, tmp_path, monkeypatch):
        # Run from a clean cwd so the project's sift.toml isn't picked up
        monkeypatch.chdir(tmp_path)
        cfg = load_config(None)
        assert cfg.source_path is None
        assert cfg.current_fy_start_year == 2025
        assert cfg.crawl.rate_per_sec == 3.0
        assert cfg.crawl.concurrency == 8
        assert cfg.crawl.host_block_floor == 3
        assert cfg.publish.coverage_floor == 0.99
        assert cfg.seed.use_default_excludes is True
        assert cfg.tiers["LIVING"].floor_days == 7

    def test_missing_explicit_path_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nope.toml")


class TestFileOverrides:
    def test_full_file(self, tmp_path):
        p = tmp_path / "c.toml"
        p.write_text("""
[fy]
current_start_year = 2026

[crawl]
rate_per_sec = 5.0
concurrency = 16
timeout_sec = 60
retries = 5
user_agent = "test-agent/1.0"
host_block_floor = 7

[publish]
coverage_floor = 0.95
hash_sample_rate = 0.05
hash_sample_min = 10
schema_sample_size = 25

[seed]
host_allow = ["www.example.com", "x.com"]
use_default_excludes = false
extra_exclude_patterns = ["^/draft/"]

[tiers.LIVING]
floor_days = 3
ceiling_days = 30
tombstone_ttl_days = 60
max_failures = 5
""")
        cfg = load_config(p)
        assert cfg.source_path == str(p)
        assert cfg.current_fy_start_year == 2026
        assert cfg.crawl.rate_per_sec == 5.0
        assert cfg.crawl.user_agent == "test-agent/1.0"
        assert cfg.crawl.host_block_floor == 7
        assert cfg.publish.coverage_floor == 0.95
        assert cfg.seed.host_allow == ("www.example.com", "x.com")
        assert cfg.seed.use_default_excludes is False
        assert cfg.seed.extra_exclude_patterns == ("^/draft/",)
        assert cfg.tiers["LIVING"].floor_days == 3
        # Untouched tiers retain defaults
        assert cfg.tiers["NEWS"].floor_days == DEFAULT_TIERS["NEWS"].floor_days

    def test_partial_tier_override_keeps_other_fields(self, tmp_path):
        p = tmp_path / "c.toml"
        p.write_text("""
[tiers.NEWS]
floor_days = 2
""")
        cfg = load_config(p)
        # Overridden
        assert cfg.tiers["NEWS"].floor_days == 2
        # Inherited from default
        default = DEFAULT_TIERS["NEWS"]
        assert cfg.tiers["NEWS"].ceiling_days == default.ceiling_days
        assert cfg.tiers["NEWS"].tombstone_ttl_days == default.tombstone_ttl_days
        assert cfg.tiers["NEWS"].max_failures == default.max_failures


class TestValidation:
    def test_unknown_tier_raises(self, tmp_path):
        p = tmp_path / "c.toml"
        p.write_text('[tiers.GHOST]\nfloor_days = 1\n')
        with pytest.raises(ValueError, match="unknown tier"):
            load_config(p)

    def test_floor_greater_than_ceiling_raises(self, tmp_path):
        p = tmp_path / "c.toml"
        p.write_text("""
[tiers.LIVING]
floor_days = 100
ceiling_days = 10
""")
        with pytest.raises(ValueError, match="floor_days"):
            load_config(p)

    def test_zero_floor_raises(self, tmp_path):
        p = tmp_path / "c.toml"
        p.write_text("[tiers.LIVING]\nfloor_days = 0\n")
        with pytest.raises(ValueError):
            load_config(p)


class TestApplyConfig:
    def test_decide_picks_up_new_intervals(self, tmp_path):
        p = tmp_path / "c.toml"
        p.write_text("[tiers.LIVING]\nfloor_days = 3\nceiling_days = 30\n")
        cfg = load_config(p)
        decide_mod.apply_config(cfg)
        assert decide_mod.TIER_INTERVALS[decide_mod.Tier.LIVING] == (
            timedelta(days=3), timedelta(days=30)
        )

    def test_publish_picks_up_new_floor(self, tmp_path):
        p = tmp_path / "c.toml"
        p.write_text("[publish]\ncoverage_floor = 0.5\n")
        cfg = load_config(p)
        publish_mod.apply_config(cfg)
        assert publish_mod.COVERAGE_FLOOR == 0.5

    def test_classify_picks_up_new_fy(self, tmp_path):
        p = tmp_path / "c.toml"
        p.write_text("[fy]\ncurrent_start_year = 2030\n")
        cfg = load_config(p)
        classify_mod.set_current_fy_start_year(cfg.current_fy_start_year)
        assert classify_mod.current_fy_start_year() == 2030
        # And classification now treats anything < 2030 as FROZEN
        t = classify_mod.classify_tier(
            "https://www.ato.gov.au/forms-and-instructions/individual-tax-return-2028-instructions"
        )
        assert t == classify_mod.Tier.FROZEN


class TestSearchPaths:
    def test_local_overrides_main(self, tmp_path, monkeypatch):
        """sift.local.toml wins over sift.toml when both exist in cwd."""
        (tmp_path / "sift.toml").write_text("[crawl]\nrate_per_sec = 2.0\n")
        (tmp_path / "sift.local.toml").write_text("[crawl]\nrate_per_sec = 7.0\n")
        monkeypatch.chdir(tmp_path)
        cfg = load_config(None)
        assert cfg.crawl.rate_per_sec == 7.0
        assert cfg.source_path == "sift.local.toml"

    def test_only_main(self, tmp_path, monkeypatch):
        (tmp_path / "sift.toml").write_text("[crawl]\nrate_per_sec = 4.0\n")
        monkeypatch.chdir(tmp_path)
        cfg = load_config(None)
        assert cfg.crawl.rate_per_sec == 4.0
        assert cfg.source_path == "sift.toml"

    def test_no_files_returns_defaults(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cfg = load_config(None)
        assert cfg.source_path is None
        assert cfg.crawl.rate_per_sec == 3.0

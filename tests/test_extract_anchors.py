"""Heading anchor injection: deterministic slug + collision disambiguation."""

import pytest

from sift.extract import inject_heading_anchors, slugify


class TestSlugify:
    @pytest.mark.parametrize("text,want", [
        ("Hello World", "hello-world"),
        ("Hello, World!", "hello-world"),
        ("Tax rates 2025-26", "tax-rates-2025-26"),
        ("   leading and trailing   ", "leading-and-trailing"),
        ("UPPER case", "upper-case"),
        ("multiple   spaces", "multiple-spaces"),
        ("emoji 😀 stripped", "emoji-stripped"),
    ])
    def test_slugify(self, text, want):
        assert slugify(text) == want

    def test_empty_or_unsluggable(self):
        # Should still return a non-empty fallback.
        assert slugify("") == ""


class TestInjectAnchors:
    def test_adds_anchor_to_heading(self):
        md, anchors = inject_heading_anchors("## Foo bar\n\nbody")
        assert "## Foo bar {#foo-bar}" in md
        assert anchors == [(2, "foo-bar", "Foo bar")]

    def test_idempotent_for_existing_anchors(self):
        src = "## Foo bar {#existing-anchor}\nbody"
        md, anchors = inject_heading_anchors(src)
        # Should not double-annotate
        assert md.count("{#") == 1
        # We don't record existing anchors in the return list (they didn't change)
        assert anchors == []

    def test_collision_disambiguation(self):
        src = "## Foo\n\nbody1\n\n## Foo\n\nbody2\n\n## Foo\n\nbody3"
        md, anchors = inject_heading_anchors(src)
        slugs = [a[1] for a in anchors]
        assert slugs == ["foo", "foo-2", "foo-3"]
        # All anchors should appear in the output
        for s in slugs:
            assert f"{{#{s}}}" in md

    def test_multiple_heading_levels(self):
        src = "# H1\n## H2\n### H3\n#### H4"
        md, anchors = inject_heading_anchors(src)
        assert [a[0] for a in anchors] == [1, 2, 3, 4]
        assert [a[1] for a in anchors] == ["h1", "h2", "h3", "h4"]

    def test_non_heading_lines_untouched(self):
        src = "## Real heading\n\nBody text\nA line that mentions # in it\n\nMore body."
        md, _ = inject_heading_anchors(src)
        assert "Body text" in md
        # The "A line that mentions #" should not get an anchor
        assert "A line that mentions # in it {#" not in md

    def test_deterministic_across_runs(self):
        src = "## Section one\n## Section two\n## Section one"
        a, _ = inject_heading_anchors(src)
        b, _ = inject_heading_anchors(src)
        assert a == b

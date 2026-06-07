"""Hash-stability tests: same input -> same output across invocations."""

import hashlib

from sift.normalize import normalize_for_hash


class TestDeterminism:
    def test_empty(self):
        assert normalize_for_hash("") == ""
        assert normalize_for_hash(None) == ""

    def test_idempotent(self):
        text = "# H\n\nBody  \n\n\n\nMore"
        assert normalize_for_hash(text) == normalize_for_hash(normalize_for_hash(text))

    def test_unicode_nfc(self):
        # NFD (é decomposed) vs NFC (é composed) — should hash the same.
        nfd = "café"
        nfc = "café"
        assert normalize_for_hash(nfd) == normalize_for_hash(nfc)


class TestStripDynamic:
    def test_last_modified(self):
        text = "Body\nLast modified: 24 May 2026\nMore"
        assert "Last modified" not in normalize_for_hash(text)
        assert "Body" in normalize_for_hash(text)
        assert "More" in normalize_for_hash(text)

    def test_last_reviewed(self):
        text = "Body\nLast reviewed: 1 Jan 2025\nMore"
        assert "Last reviewed" not in normalize_for_hash(text)

    def test_qc_code(self):
        text = "Some text. QC 12345. More text."
        out = normalize_for_hash(text)
        assert "QC 12345" not in out
        assert "Some text" in out
        assert "More text" in out

    def test_copyright_year_stripped(self):
        a = normalize_for_hash("© Commonwealth of Australia 2025\nBody")
        b = normalize_for_hash("© Commonwealth of Australia 2026\nBody")
        assert a == b


class TestWhitespace:
    def test_crlf_to_lf(self):
        a = normalize_for_hash("a\r\nb\r\nc")
        b = normalize_for_hash("a\nb\nc")
        assert a == b

    def test_trailing_whitespace_per_line(self):
        a = normalize_for_hash("line1   \nline2\t\nline3")
        b = normalize_for_hash("line1\nline2\nline3")
        assert a == b

    def test_blank_line_collapse(self):
        a = normalize_for_hash("a\n\n\n\nb")
        b = normalize_for_hash("a\n\nb")
        assert a == b


class TestHashStability:
    def test_same_input_same_hash(self):
        text = "# Title\nBody text\nMore content"
        h1 = hashlib.sha256(normalize_for_hash(text).encode()).hexdigest()
        h2 = hashlib.sha256(normalize_for_hash(text).encode()).hexdigest()
        assert h1 == h2

    def test_only_timestamp_differs_no_hash_change(self):
        """The whole point of the normalizer: rotating timestamps shouldn't change content_hash."""
        t1 = "Body content here\nLast modified: 24 May 2026"
        t2 = "Body content here\nLast modified: 25 May 2026"
        h1 = hashlib.sha256(normalize_for_hash(t1).encode()).hexdigest()
        h2 = hashlib.sha256(normalize_for_hash(t2).encode()).hexdigest()
        assert h1 == h2

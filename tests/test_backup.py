"""SQLite manifest backup — online .backup() API + integrity verification."""

import sqlite3
from pathlib import Path

import pytest

from sift import paths
from sift.manifest import (
    apply_fetch_result, init_schema, now_utc, open_db, transaction, upsert_seed,
)


def _populate_manifest(root: Path) -> int:
    """Seed a manifest with a handful of rows. Returns the row count."""
    conn = open_db(paths.manifest_path(root))
    init_schema(conn)
    now = now_utc()
    for i in range(5):
        with transaction(conn):
            upsert_seed(conn, f"https://x/{i}", "LIVING", None, "v1", None, now)
            apply_fetch_result(
                conn, url=f"https://x/{i}", now=now,
                http_status=200, http_etag=None, http_last_modified=None,
                raw_hash=f"r{i:063d}_", content_hash=f"c{i:063d}_",
                crawler_version="v1", extractor_version="ext",
                normalizer_version="v1", error=None,
            )
    return 5


class TestSqliteBackup:
    """We can't easily invoke the click command here (it calls sys.exit on
    failures). Test the underlying SQLite backup API + the backup file's
    structural integrity, which is what the command wraps."""

    def test_online_backup_produces_readable_copy(self, tmp_path):
        root = tmp_path
        rows = _populate_manifest(root)

        # Now do the SQLite-online backup
        src_path = paths.manifest_path(root)
        dest_path = tmp_path / "backup.db"
        src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
        dst = sqlite3.connect(str(dest_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

        # Open the backup and verify schema + rowcount survived
        assert dest_path.exists()
        backup_conn = sqlite3.connect(f"file:{dest_path}?mode=ro", uri=True)
        backup_rows = backup_conn.execute("SELECT COUNT(*) FROM manifest").fetchone()[0]
        assert backup_rows == rows

        # PRAGMA integrity_check should report "ok"
        integ = backup_conn.execute("PRAGMA integrity_check").fetchone()[0]
        assert integ == "ok"
        backup_conn.close()

    def test_backup_works_with_concurrent_writes_open(self, tmp_path):
        """The whole point of the online backup API: source connection can be
        open for writes while we back up. This isn't truly concurrent in this
        test (single thread), but it proves we don't need to close the source."""
        root = tmp_path
        _populate_manifest(root)

        src_path = paths.manifest_path(root)
        # Keep the source conn open (simulating "the pipeline is running")
        live_conn = open_db(src_path)

        dest = tmp_path / "live-backup.db"
        ro_src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
        dst = sqlite3.connect(str(dest))
        ro_src.backup(dst)
        dst.close()
        ro_src.close()

        assert dest.exists()
        live_conn.close()


class TestBackupCommandSurface:
    """The click command structure; we don't invoke it (sys.exit on failures
    interferes with pytest) but we can verify it's registered."""

    def test_backup_commands_registered(self):
        from sift.cli import main
        names = list(main.commands.keys())
        assert "backup" in names
        assert "verify-backup" in names

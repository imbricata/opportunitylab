from __future__ import annotations

import gzip
import sqlite3
import time
from pathlib import Path

from .config import Config
from .db import prune_research_data, set_setting


def _remove_stale_backup_work(backup_dir: Path) -> tuple[int, int]:
    """Remove abandoned work files left by a killed maintenance process.

    The maintenance unit is serialized with flock, so no second backup can be
    using these files while this function runs.  Final ``.db.gz`` archives are
    deliberately excluded.
    """
    removed = removed_bytes = 0
    patterns = (
        ".factory-*.db.tmp*",
        ".factory-*.db.gz.tmp",
        ".factory-*.restore.db*",
    )
    for pattern in patterns:
        for path in backup_dir.glob(pattern):
            try:
                removed_bytes += path.stat().st_size
                path.unlink()
                removed += 1
            except FileNotFoundError:
                pass
    return removed, removed_bytes


def _daily_backup(db, path: Path, keep: int = 7) -> dict:
    backup_dir = path.parent / "backups" / "factory"
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stale_files, stale_bytes = _remove_stale_backup_work(backup_dir)
    stamp = time.strftime("%Y%m%d", time.gmtime())
    compressed = backup_dir / f"factory-{stamp}.db.gz"
    compressed_work = backup_dir / f".factory-{stamp}.db.gz.tmp"
    if compressed.exists():
        return {
            "state": "already_exists", "path": str(compressed),
            "stale_files_removed": stale_files,
            "stale_bytes_removed": stale_bytes,
        }
    temporary = backup_dir / f".factory-{stamp}.db.tmp"
    try:
        # VACUUM INTO creates a compact, transactionally consistent snapshot.
        # The old online-backup loop was starved by the continuously-writing
        # BTC worker and left a new partial file on every timeout.
        quoted = str(temporary).replace("'", "''")
        db.execute(f"VACUUM INTO '{quoted}'")
        target = sqlite3.connect(f"file:{temporary}?mode=ro", uri=True)
        try:
            integrity = str(target.execute("PRAGMA quick_check").fetchone()[0])
        finally:
            target.close()
        if integrity != "ok":
            raise RuntimeError(f"backup integrity check failed: {integrity}")
        with temporary.open("rb") as source, gzip.open(
            compressed_work, "wb", compresslevel=1
        ) as sink:
            while chunk := source.read(1024 * 1024):
                sink.write(chunk)
        # Reading through EOF verifies the gzip stream and CRC without writing
        # a second full-size restored copy to this small VM's disk.
        with gzip.open(compressed_work, "rb") as source:
            while source.read(1024 * 1024):
                pass
        compressed_work.chmod(0o600)
        compressed_work.replace(compressed)
    except Exception:
        compressed_work.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)
        for suffix in ("-journal", "-wal", "-shm"):
            Path(str(temporary) + suffix).unlink(missing_ok=True)
    backups = sorted(backup_dir.glob("factory-*.db.gz"), reverse=True)
    for old in backups[max(1, keep):]:
        old.unlink()
    return {
        "state": "created", "path": str(compressed), "integrity": "ok",
        "stale_files_removed": stale_files,
        "stale_bytes_removed": stale_bytes,
    }


def run_maintenance(db, cfg: Config, *, create_backup: bool = True) -> dict:
    now = time.time()
    prune_research_data(db)
    thirty_days = now - 30 * 86400
    ninety_days = now - 90 * 86400
    protected = "SELECT slug FROM fast_paper_trades UNION SELECT slug FROM fast_live_orders"
    deleted = {}
    before = db.total_changes
    db.execute(
        f"DELETE FROM fast_screen_decisions WHERE ts<? AND slug NOT IN ({protected})",
        (thirty_days,),
    )
    deleted["screens"] = db.total_changes - before
    before = db.total_changes
    db.execute(
        f"DELETE FROM fast_execution_attempts WHERE ts<? AND slug NOT IN ({protected})",
        (ninety_days,),
    )
    deleted["attempts"] = db.total_changes - before
    before = db.total_changes
    db.execute("DELETE FROM fast_shadow_decisions WHERE ts<?", (thirty_days,))
    deleted["shadow_decisions"] = db.total_changes - before
    before = db.total_changes
    db.execute(
        """UPDATE factory_sources SET raw_text='',status='archived'
        WHERE expires_ts<? AND raw_text!=''""",
        (now,),
    )
    deleted["source_payloads_archived"] = db.total_changes - before
    set_setting(db, "factory_last_maintenance", now)
    db.commit()
    db.execute("PRAGMA optimize")
    checkpoint = tuple(db.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())
    backup = _daily_backup(db, cfg.db_path) if create_backup else {"state": "disabled"}
    result = {"deleted": deleted, "checkpoint": checkpoint, "backup": backup}
    if backup.get("integrity") == "ok":
        set_setting(db, "factory_last_integrity_check", now)
    set_setting(db, "factory_last_maintenance_result", result)
    db.commit()
    return result

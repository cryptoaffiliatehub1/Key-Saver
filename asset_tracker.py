"""
Persistent cross-session asset tracker.

Records every Pexels / Pixabay video ID ever downloaded so the same
clip is never reused across different video builds on the same channel.

Uses a local SQLite database at data/asset_tracker.db.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

log = logging.getLogger("asset_tracker")

_DB_PATH = Path("data/asset_tracker.db")
_DB_PATH.parent.mkdir(exist_ok=True)
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    c.execute("""
        CREATE TABLE IF NOT EXISTS used_clips (
            source   TEXT NOT NULL,
            video_id TEXT NOT NULL,
            used_at  TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (source, video_id)
        )
    """)
    c.commit()
    return c


def is_used(source: str, video_id: str | int) -> bool:
    """Return True if this video_id was already used from the given source."""
    with _lock:
        c = _conn()
        try:
            row = c.execute(
                "SELECT 1 FROM used_clips WHERE source=? AND video_id=?",
                (source, str(video_id)),
            ).fetchone()
            return row is not None
        finally:
            c.close()


def mark_used(source: str, video_id: str | int) -> None:
    """Permanently record this video_id as used for the given source."""
    with _lock:
        c = _conn()
        try:
            c.execute(
                "INSERT OR IGNORE INTO used_clips (source, video_id) VALUES (?,?)",
                (source, str(video_id)),
            )
            c.commit()
        finally:
            c.close()


def total_used(source: str | None = None) -> int:
    """Count total persisted IDs (optionally per source)."""
    with _lock:
        c = _conn()
        try:
            if source:
                row = c.execute(
                    "SELECT COUNT(*) FROM used_clips WHERE source=?", (source,)
                ).fetchone()
            else:
                row = c.execute("SELECT COUNT(*) FROM used_clips").fetchone()
            return row[0] if row else 0
        finally:
            c.close()


def reset(source: str | None = None) -> None:
    """Admin reset — wipe the tracker for one source or all."""
    with _lock:
        c = _conn()
        try:
            if source:
                c.execute("DELETE FROM used_clips WHERE source=?", (source,))
            else:
                c.execute("DELETE FROM used_clips")
            c.commit()
            log.warning("asset_tracker reset (source=%s)", source or "ALL")
        finally:
            c.close()

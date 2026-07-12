"""Persistent "seen event ids" store, backed by SQLite.

An event is emitted to sinks only the first time its id is seen. Entries
older than PRUNE_AFTER_DAYS are pruned on each open() to keep the store
small.

v1 does not handle source updates to an already-seen event (e.g. USGS
revising a magnitude) — same id means "already seen", full stop. If update
handling is ever needed, it would go here: compare a stored payload hash
(not just the id) and re-emit + update the row when it changes.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

PRUNE_AFTER_DAYS = 30


class SeenStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS seen ("
            "id TEXT PRIMARY KEY, "
            "first_seen TEXT NOT NULL"
            ")"
        )
        self._conn.commit()
        self._prune()

    def _prune(self) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=PRUNE_AFTER_DAYS)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        self._conn.execute("DELETE FROM seen WHERE first_seen < ?", (cutoff,))
        self._conn.commit()

    def is_empty(self) -> bool:
        cur = self._conn.execute("SELECT 1 FROM seen LIMIT 1")
        return cur.fetchone() is None

    def filter_unseen(self, events: list[dict]) -> list[dict]:
        """Return only the events whose id has not been recorded before."""
        unseen = []
        for event in events:
            cur = self._conn.execute("SELECT 1 FROM seen WHERE id = ?", (event["id"],))
            if cur.fetchone() is None:
                unseen.append(event)
        return unseen

    def mark_seen(self, events: list[dict]) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._conn.executemany(
            "INSERT OR IGNORE INTO seen (id, first_seen) VALUES (?, ?)",
            [(event["id"], now) for event in events],
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SeenStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


@contextmanager
def open_store(db_path: str | Path):
    store = SeenStore(db_path)
    try:
        yield store
    finally:
        store.close()

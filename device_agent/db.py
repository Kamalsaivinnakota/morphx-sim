"""Local SQLite storage for the device agent; this database is the on-device source of truth."""

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    event_id        TEXT PRIMARY KEY,
    device_id       TEXT NOT NULL,
    sample_id       TEXT NOT NULL,
    sequence        INTEGER NOT NULL CHECK (sequence >= 1),
    measured_at     TEXT NOT NULL,
    measurements    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK (status IN ('PENDING', 'SYNCED', 'REJECTED')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    synced_at       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_records_device_sequence ON records (device_id, sequence);
CREATE INDEX IF NOT EXISTS idx_records_status_sequence ON records (status, sequence);
"""


def utc_now_iso() -> str:
    """Return the current UTC time as a timezone-aware ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: str) -> sqlite3.Connection:
    """Open a connection for the calling thread with WAL mode, full fsync and a 5 s busy timeout."""
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create the records table and its indexes if they do not exist yet."""
    conn.executescript(SCHEMA)


def next_sequence(conn: sqlite3.Connection, device_id: str) -> int:
    """Recover the next sequence number from disk: MAX(sequence) + 1, or 1 for an empty database."""
    row = conn.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM records WHERE device_id = ?",
        (device_id,),
    ).fetchone()
    return int(row[0])


def insert_record(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    """Persist one new record as PENDING in its own committed transaction."""
    with conn:
        conn.execute(
            "INSERT INTO records (event_id, device_id, sample_id, sequence, measured_at, measurements, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'PENDING')",
            (
                record["event_id"],
                record["device_id"],
                record["sample_id"],
                record["sequence"],
                record["measured_at"],
                json.dumps(record["measurements"]),
            ),
        )


def fetch_pending(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    """Return up to `limit` PENDING records, oldest sequence first, shaped as the server expects."""
    rows = conn.execute(
        "SELECT event_id, device_id, sample_id, sequence, measured_at, measurements FROM records "
        "WHERE status = 'PENDING' ORDER BY sequence LIMIT ?",
        (limit,),
    ).fetchall()
    return [{**dict(row), "measurements": json.loads(row["measurements"])} for row in rows]


def count_pending(conn: sqlite3.Connection) -> int:
    """Return how many records are still waiting to be synced."""
    return int(conn.execute("SELECT COUNT(*) FROM records WHERE status = 'PENDING'").fetchone()[0])


def record_attempt(
    conn: sqlite3.Connection,
    event_ids: Iterable[str],
    attempted_at: str,
    synced_ids: Iterable[str] = (),
    rejected_ids: Iterable[str] = (),
) -> None:
    """Atomically count one send attempt for every sent row and apply any server outcomes."""
    synced_at = utc_now_iso()
    with conn:
        conn.executemany(
            "UPDATE records SET attempts = attempts + 1, last_attempt_at = ? WHERE event_id = ?",
            [(attempted_at, event_id) for event_id in event_ids],
        )
        conn.executemany(
            "UPDATE records SET status = 'SYNCED', synced_at = ? WHERE event_id = ? AND status = 'PENDING'",
            [(synced_at, event_id) for event_id in synced_ids],
        )
        conn.executemany(
            "UPDATE records SET status = 'REJECTED' WHERE event_id = ? AND status = 'PENDING'",
            [(event_id,) for event_id in rejected_ids],
        )

"""SQLite storage for the central server."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from server.models import RecordIn

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    event_id     TEXT PRIMARY KEY,
    device_id    TEXT NOT NULL,
    sample_id    TEXT NOT NULL,
    sequence     INTEGER NOT NULL,
    measured_at  TEXT NOT NULL,
    received_at  TEXT NOT NULL,
    measurements TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_records_device_sequence ON records (device_id, sequence);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """Open a connection with WAL mode, full fsync and a 5 s busy timeout."""
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


@contextmanager
def open_db(db_path: str) -> Iterator[sqlite3.Connection]:
    """Yield a short-lived connection that is always closed afterwards."""
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    """Create the records table and index if they do not exist yet."""
    with open_db(db_path) as conn:
        conn.executescript(SCHEMA)


def insert_records(conn: sqlite3.Connection, records: list[RecordIn], received_at: str) -> list[bool]:
    """Insert all records in one transaction; True means newly stored, False means the event_id already existed."""
    stored = []
    with conn:
        for record in records:
            cursor = conn.execute(
                "INSERT INTO records "
                "(event_id, device_id, sample_id, sequence, measured_at, received_at, measurements) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (event_id) DO NOTHING",
                (
                    str(record.event_id),
                    record.device_id,
                    record.sample_id,
                    record.sequence,
                    record.measured_at.isoformat(),
                    received_at,
                    record.measurements.model_dump_json(),
                ),
            )
            stored.append(cursor.rowcount == 1)
    return stored


def list_records(conn: sqlite3.Connection, device_id: str, after_sequence: int, limit: int) -> list[dict[str, Any]]:
    """Return a device's records with sequence > after_sequence, in ascending sequence order."""
    rows = conn.execute(
        "SELECT event_id, device_id, sample_id, sequence, measured_at, received_at, measurements "
        "FROM records WHERE device_id = ? AND sequence > ? ORDER BY sequence, event_id LIMIT ?",
        (device_id, after_sequence, limit),
    ).fetchall()
    return [{**dict(row), "measurements": json.loads(row["measurements"])} for row in rows]

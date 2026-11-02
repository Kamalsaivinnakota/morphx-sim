"""Generator thread: creates synthetic blood-count records and saves them locally as PENDING."""

import logging
import random
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from device_agent import db
from device_agent.config import Config

log = logging.getLogger(__name__)


def build_record(device_id: str, sequence: int, rng: random.Random) -> dict[str, Any]:
    """Create one synthetic record with a fresh event_id and the device's own acquisition time."""
    measured_at = datetime.now(timezone.utc)
    return {
        "event_id": str(uuid.uuid4()),
        "device_id": device_id,
        "sample_id": f"SMP-{measured_at:%Y%m%d}-{sequence:06d}",
        "sequence": sequence,
        "measured_at": measured_at.isoformat(),
        "measurements": {
            "wbc": {"value": round(rng.uniform(4.0, 11.0), 1), "unit": "10^3/uL"},
            "rbc": {"value": round(rng.uniform(4.0, 6.0), 2), "unit": "10^6/uL"},
            "hb": {"value": round(rng.uniform(12.0, 17.0), 1), "unit": "g/dL"},
        },
    }


class Generator:
    """Produces one record per interval on a drift-resistant schedule; never touches the network."""

    def __init__(
        self,
        config: Config,
        start_sequence: int,
        stop: threading.Event,
        rng: random.Random | None = None,
    ) -> None:
        self._config = config
        self._sequence = start_sequence
        self._stop = stop
        self._rng = rng or random.Random()

    def run(self) -> None:
        """Thread body: wait for each tick, then generate and persist one record."""
        conn = db.connect(self._config.db_path)
        next_tick = time.monotonic() + self._config.interval
        try:
            while not self._stop.wait(max(0.0, next_tick - time.monotonic())):
                self._generate_one(conn)
                next_tick += self._config.interval
                now = time.monotonic()
                if next_tick <= now:
                    log.warning("generator fell behind schedule, skipping missed ticks")
                    next_tick = now + self._config.interval
        finally:
            conn.close()
        log.info("generator stopped at next sequence=%d", self._sequence)

    def _generate_one(self, conn: sqlite3.Connection) -> None:
        """Build and save one record; the sequence only advances once the insert is committed."""
        record = build_record(self._config.device_id, self._sequence, self._rng)
        try:
            db.insert_record(conn, record)
        except sqlite3.Error:
            log.exception("failed to save record seq=%d, retrying on next tick", self._sequence)
            return
        log.info(
            "generated seq=%d sample_id=%s event_id=%s",
            record["sequence"],
            record["sample_id"],
            record["event_id"],
        )
        self._sequence += 1

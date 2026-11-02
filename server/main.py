"""Central server: FastAPI app that validates device records and stores them idempotently in SQLite."""

import logging
import sys
import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone
from typing import Any

import uvicorn
from fastapi import FastAPI, Query

from server import db
from server.config import ConfigError, config_path_from_args, load_config
from server.models import BatchRequest, BatchResponse, RecordIn, RecordOut, RecordPage, RecordResult, validate_record

log = logging.getLogger("server")


class BatchStats:
    """Thread-safe in-memory counters of batch requests since process start."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests = 0
        self._records = 0
        self._sizes: deque[int] = deque(maxlen=1000)

    def add(self, size: int) -> None:
        """Count one batch request of `size` records."""
        with self._lock:
            self._requests += 1
            self._records += size
            self._sizes.append(size)

    def snapshot(self) -> dict[str, Any]:
        """Return a consistent copy of the counters."""
        with self._lock:
            return {
                "batch_requests": self._requests,
                "records_received": self._records,
                "recent_batch_sizes": list(self._sizes),
            }


def create_app(db_path: str) -> FastAPI:
    """Build the FastAPI app bound to one SQLite file, creating the schema if needed."""
    db.init_db(db_path)
    stats = BatchStats()
    app = FastAPI(title="MorphX central server")

    @app.post("/records/batch", response_model=BatchResponse)
    def post_batch(batch: BatchRequest) -> BatchResponse:
        """Validate each record on its own, store the valid ones in one transaction, report per-record outcomes."""
        stats.add(len(batch.records))
        outcomes = [validate_record(item) for item in batch.records]
        valid = [outcome for _, outcome in outcomes if isinstance(outcome, RecordIn)]
        received_at = datetime.now(timezone.utc).isoformat()
        with db.open_db(db_path) as conn:
            stored_flags = iter(db.insert_records(conn, valid, received_at))

        results = []
        for event_id, outcome in outcomes:
            if isinstance(outcome, str):
                log.warning("rejected record event_id=%s: %s", event_id, outcome)
                results.append(RecordResult(event_id=event_id, status="rejected", error=outcome))
            else:
                status = "stored" if next(stored_flags) else "duplicate"
                results.append(RecordResult(event_id=event_id, status=status))

        counts = Counter(result.status for result in results)
        log.info(
            "batch: size=%d stored=%d duplicate=%d rejected=%d",
            len(results),
            counts["stored"],
            counts["duplicate"],
            counts["rejected"],
        )
        return BatchResponse(results=results)

    @app.get("/devices/{device_id}/records", response_model=RecordPage)
    def get_device_records(
        device_id: str,
        after_sequence: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=1000),
    ) -> RecordPage:
        """Return a device's records ordered by sequence, starting after `after_sequence`."""
        with db.open_db(db_path) as conn:
            rows = db.list_records(conn, device_id, after_sequence, limit)
        return RecordPage(device_id=device_id, records=[RecordOut(**row) for row in rows])

    @app.get("/stats")
    def get_stats() -> dict[str, Any]:
        """Return in-memory batch counters; they reset when the server restarts."""
        return stats.snapshot()

    return app


def main(argv: list[str] | None = None) -> int:
    """Load the config file and serve the app with uvicorn until SIGINT/SIGTERM; return the exit code."""
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03dZ %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    config_path = config_path_from_args(argv)
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        log.error("invalid config: %s", exc)
        return 2
    log.info(
        "server starting: config=%s db=%s listening on http://%s:%d",
        config_path,
        config.db_path,
        config.host,
        config.port,
    )
    uvicorn.run(create_app(config.db_path), host=config.host, port=config.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())

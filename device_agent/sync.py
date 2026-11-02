"""Sync thread: pushes PENDING records to the server in batches with exponential backoff."""

import logging
import random
import sqlite3
import threading
from typing import Any

import requests

from device_agent import db
from device_agent.config import Config

log = logging.getLogger(__name__)

IDLE_POLL_SECONDS = 1.0


class SyncError(Exception):
    """A send attempt failed in a way that leaves the batch PENDING for a later retry."""


class SyncWorker:
    """Drains the local PENDING queue to the server; consecutive failures are tracked in memory only."""

    def __init__(
        self,
        config: Config,
        stop: threading.Event,
        session: requests.Session | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._config = config
        self._stop = stop
        self._session = session or requests.Session()
        self._rng = rng or random.Random()
        self._url = f"{config.server_url}/records/batch"
        self._failures = 0

    def run(self) -> None:
        """Thread body: repeat sync steps until stopped, sleeping as each step requests."""
        conn = db.connect(self._config.db_path)
        try:
            while not self._stop.is_set():
                try:
                    delay = self._step(conn)
                except sqlite3.Error:
                    log.exception("sync step failed on the local database")
                    delay = self._config.backoff_min
                if delay > 0:
                    self._stop.wait(delay)
        finally:
            conn.close()
            self._session.close()
        log.info("sync stopped")

    def _step(self, conn: sqlite3.Connection) -> float:
        """Send one batch and return how many seconds to wait before the next step."""
        batch = db.fetch_pending(conn, self._config.batch_size)
        if not batch:
            return IDLE_POLL_SECONDS

        event_ids = [record["event_id"] for record in batch]
        attempted_at = db.utc_now_iso()
        try:
            outcomes = self._classify(self._post(batch), set(event_ids))
        except SyncError as exc:
            db.record_attempt(conn, event_ids, attempted_at)
            self._failures += 1
            delay = self._backoff_delay()
            log.warning(
                "sync failed: %s, backoff %.1fs, pending=%d",
                exc,
                delay,
                db.count_pending(conn),
            )
            return delay

        db.record_attempt(
            conn,
            event_ids,
            attempted_at,
            synced_ids=outcomes["stored"] + outcomes["duplicate"],
            rejected_ids=outcomes["rejected"],
        )
        if self._failures:
            log.info("server reachable again after %d failed attempts, backoff reset", self._failures)
        self._failures = 0
        log.info(
            "sync ok: sent=%d stored=%d duplicate=%d rejected=%d pending=%d",
            len(batch),
            len(outcomes["stored"]),
            len(outcomes["duplicate"]),
            len(outcomes["rejected"]),
            db.count_pending(conn),
        )
        return 0.0

    def _post(self, batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """POST one batch and return the per-record results, raising SyncError on any retryable failure."""
        try:
            response = self._session.post(
                self._url,
                json={"records": batch},
                timeout=self._config.request_timeout,
            )
        except requests.Timeout:
            raise SyncError(f"timeout after {self._config.request_timeout:g}s") from None
        except requests.ConnectionError as exc:
            reason = "connection refused" if "refused" in str(exc).lower() else "connection error"
            raise SyncError(reason) from None
        except requests.RequestException as exc:
            raise SyncError(f"request error: {exc}") from None

        if response.status_code >= 500:
            raise SyncError(f"server error HTTP {response.status_code}")
        if response.status_code != 200:
            raise SyncError(f"unexpected HTTP {response.status_code}: {response.text[:200]}")
        try:
            body = response.json()
        except ValueError:
            raise SyncError("response is not valid JSON") from None

        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list) or not all(isinstance(item, dict) for item in results):
            raise SyncError("response has no valid 'results' list")
        return results

    def _classify(self, results: list[dict[str, Any]], sent: set[str]) -> dict[str, list[str]]:
        """Group sent event_ids by server status; unknown ids and statuses are ignored."""
        outcomes: dict[str, list[str]] = {"stored": [], "duplicate": [], "rejected": []}
        for result in results:
            event_id = result.get("event_id")
            status = result.get("status")
            if event_id not in sent or status not in outcomes:
                continue
            outcomes[status].append(event_id)
            if status == "rejected":
                log.error(
                    "record rejected by server, will not retry: event_id=%s error=%s",
                    event_id,
                    result.get("error"),
                )
        if not any(outcomes.values()):
            raise SyncError("server response resolved none of the sent records")
        return outcomes

    def _backoff_delay(self) -> float:
        """Exponential delay from backoff_min, doubling per consecutive failure, capped, with +/-20% jitter."""
        exponent = min(self._failures - 1, 32)
        base = min(self._config.backoff_max, self._config.backoff_min * 2**exponent)
        return base * self._rng.uniform(0.8, 1.2)

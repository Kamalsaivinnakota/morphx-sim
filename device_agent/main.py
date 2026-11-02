"""Device agent entry point: starts the generator and sync threads and stops them on SIGINT/SIGTERM."""

import logging
import signal
import sys
import threading
import time
from types import FrameType

from device_agent import db
from device_agent.config import Config, ConfigError, config_path_from_args, load_config
from device_agent.generator import Generator
from device_agent.sync import SyncWorker

log = logging.getLogger("device_agent")


def setup_logging() -> None:
    """Log to stderr with UTC ISO-8601 timestamps and the thread name."""
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03dZ %(levelname)s [%(threadName)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def prepare_database(config: Config) -> tuple[int, int]:
    """Create the schema if needed and return (next sequence, pending count) recovered from disk."""
    conn = db.connect(config.db_path)
    try:
        db.init_db(conn)
        return db.next_sequence(conn, config.device_id), db.count_pending(conn)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    """Run the agent until a stop signal arrives or a worker thread dies; return the exit code."""
    setup_logging()
    config_path = config_path_from_args(argv)
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        log.error("invalid config: %s", exc)
        return 2
    start_sequence, pending = prepare_database(config)
    log.info(
        "agent starting: config=%s device_id=%s db=%s server=%s interval=%gs batch_size=%d "
        "next_sequence=%d pending=%d",
        config_path,
        config.device_id,
        config.db_path,
        config.server_url,
        config.interval,
        config.batch_size,
        start_sequence,
        pending,
    )

    stop = threading.Event()

    def request_stop(signum: int, _frame: FrameType | None) -> None:
        log.info("received %s, stopping", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    threads = [
        threading.Thread(target=Generator(config, start_sequence, stop).run, name="generator", daemon=True),
        threading.Thread(target=SyncWorker(config, stop).run, name="sync", daemon=True),
    ]
    for thread in threads:
        thread.start()

    exit_code = 0
    while not stop.wait(0.5):
        if not all(thread.is_alive() for thread in threads):
            log.error("a worker thread died unexpectedly, shutting down")
            exit_code = 1
            stop.set()

    for thread in threads:
        thread.join(timeout=config.request_timeout + 1)
    log.info("agent stopped")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

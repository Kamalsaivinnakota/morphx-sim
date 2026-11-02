"""Shared test harness: real agent/server subprocesses, free ports, polling and DB/API inspection."""

import json
import os
import random
import socket
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from device_agent import db as agent_db
from device_agent.generator import build_record
from monitor import results_table, server_table, snapshot

DEVICE_ID = "MORPHX_TEST_001"
CALL_FAILED = pytest.StashKey[bool]()


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add --show-logs to print every subprocess log, not only those of failed tests."""
    parser.addoption("--show-logs", action="store_true", help="print agent and server logs after every test")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo) -> Any:
    """Remember whether a test body failed, so teardown knows whether to print the logs."""
    report = yield
    if report.when == "call":
        item.stash[CALL_FAILED] = report.failed
    return report


def free_port() -> int:
    """Ask the OS for a currently unused TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class Proc:
    """A running subprocess and the file that captures its output."""

    name: str
    popen: subprocess.Popen
    log_path: Path

    def log(self) -> str:
        """Return everything the process has logged so far."""
        return self.log_path.read_text(errors="replace")

    def is_running(self) -> bool:
        """True while the process has not exited."""
        return self.popen.poll() is None


class Sim:
    """One isolated simulation: temp DB files, a free port, and every subprocess started for a test."""

    def __init__(self, tmp_path: Path, name: str) -> None:
        self.tmp_path = tmp_path
        self.name = name
        self.device_id = DEVICE_ID
        self.port = free_port()
        self.server_url = f"http://127.0.0.1:{self.port}"
        self.server_db = tmp_path / "server.db"
        self.device_db = tmp_path / "device.db"
        self._procs: list[Proc] = []

    def wait_until(
        self,
        predicate: Callable[[], Any],
        timeout: float = 20.0,
        interval: float = 0.1,
        message: str = "condition",
    ) -> Any:
        """Poll `predicate` until it returns something truthy; transient I/O errors count as not yet."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                result = predicate()
            except (requests.RequestException, sqlite3.OperationalError):
                result = None
            if result:
                return result
            if time.monotonic() > deadline:
                raise AssertionError(f"timed out after {timeout:g}s waiting for {message}")
            time.sleep(interval)

    def _spawn(self, name: str, module_args: list[str]) -> Proc:
        """Start `python -m <module_args>` from the project root with output captured to a log file."""
        log_path = self.tmp_path / f"{len(self._procs) + 1:02d}-{name}.log"
        with open(log_path, "wb") as log_file:
            popen = subprocess.Popen(
                [sys.executable, "-m", *module_args],
                cwd=ROOT,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        proc = Proc(name, popen, log_path)
        self._procs.append(proc)
        return proc

    def _write_config(self, **device: Any) -> Path:
        """Write a config file for the next process: test-friendly device settings plus this test's server."""
        settings = {
            "device": {
                "device_id": DEVICE_ID,
                "interval": 0.3,
                "server_url": self.server_url,
                "batch_size": 50,
                "db_path": str(self.device_db),
                "request_timeout": 2,
                "backoff_min": 0.1,
                "backoff_max": 0.5,
                **device,
            },
            "server": {"host": "127.0.0.1", "port": self.port, "db_path": str(self.server_db)},
        }
        path = self.tmp_path / f"config-{len(self._procs) + 1:02d}.json"
        path.write_text(json.dumps(settings, indent=2))
        return path

    def start_server(self) -> Proc:
        """Start the server on this simulation's port and DB file, and wait until it answers."""
        proc = self._spawn("server", ["server.main", "--config", str(self._write_config())])

        def ready() -> bool:
            if not proc.is_running():
                raise AssertionError(f"server exited early:\n{proc.log()}")
            return requests.get(f"{self.server_url}/stats", timeout=1).status_code == 200

        self.wait_until(ready, message="server to accept requests")
        return proc

    def start_agent(self, interval: float = 0.3, batch_size: int = 50, device_id: str = DEVICE_ID) -> Proc:
        """Start the device agent with short intervals and a fast backoff suitable for tests."""
        config = self._write_config(interval=interval, batch_size=batch_size, device_id=device_id)
        return self._spawn("agent", ["device_agent.main", "--config", str(config)])

    def stop(self, proc: Proc) -> None:
        """Send SIGTERM and wait; escalate to SIGKILL if the process does not exit."""
        if proc.is_running():
            proc.popen.terminate()
            try:
                proc.popen.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.popen.kill()
                proc.popen.wait(timeout=5)

    def kill(self, proc: Proc) -> None:
        """Simulate a crash with SIGKILL: no signal handler and no clean shutdown run."""
        proc.popen.kill()
        proc.popen.wait(timeout=5)

    def stop_all(self, print_logs: bool) -> None:
        """Stop every process this test started; print their logs when asked to."""
        for proc in reversed(self._procs):
            self.stop(proc)
        if print_logs:
            for proc in self._procs:
                print(f"===== {proc.log_path.name} =====\n{proc.log()}")

    def _heading(self, step: str) -> str:
        """Banner naming the test, the step being shown and the time."""
        rule = "=" * 100
        return f"{rule}\n{self.name}   {datetime.now(timezone.utc):%H:%M:%S} UTC\nSTEP: {step}\n{rule}"

    def show(self, step: str, device_id: str = DEVICE_ID, rows: int = 8) -> None:
        """Print a monitor snapshot of this test's device DB and server (visible with -s or -rP)."""
        print("\n" + snapshot(self._heading(step), device_id, str(self.device_db), self.server_url, rows) + "\n")

    def show_results(self, step: str, results: list[dict[str, Any]]) -> None:
        """Print a batch response as a table of per-record outcomes."""
        print("\n" + results_table(self._heading(step), results) + "\n")

    def show_server(self, step: str, device_id: str = DEVICE_ID) -> None:
        """Print what the server returns for a device, oldest first, with gaps marked."""
        print("\n" + server_table(self._heading(step), self.server_url, device_id) + "\n")

    def show_text(self, step: str, lines: list[str]) -> None:
        """Print free-form lines under a step banner."""
        print("\n" + "\n".join([self._heading(step), "", *lines]) + "\n")

    def drain(self) -> None:
        """Run the agent with a very long interval so it only flushes the backlog, then stop it."""
        agent = self.start_agent(interval=3600)
        self.wait_until(lambda: self.device_status_counts()["PENDING"] == 0, message="device backlog to drain")
        self.stop(agent)

    def device_rows(self) -> list[dict[str, Any]]:
        """Read every row from the device DB, ordered by sequence."""
        if not self.device_db.exists():
            return []
        conn = sqlite3.connect(self.device_db, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in conn.execute("SELECT * FROM records ORDER BY sequence")]
        finally:
            conn.close()

    def device_status_counts(self) -> Counter[str]:
        """Count device rows per status; missing statuses count as 0."""
        return Counter(row["status"] for row in self.device_rows())

    def seed_device(self, count: int, device_id: str = DEVICE_ID) -> list[dict[str, Any]]:
        """Write `count` PENDING records straight into the device DB using the agent's own code."""
        conn = agent_db.connect(str(self.device_db))
        try:
            agent_db.init_db(conn)
            start = agent_db.next_sequence(conn, device_id)
            records = [build_record(device_id, seq, random.Random(seq)) for seq in range(start, start + count)]
            for record in records:
                agent_db.insert_record(conn, record)
        finally:
            conn.close()
        return records

    def make_record(self, sequence: int, device_id: str = DEVICE_ID) -> dict[str, Any]:
        """Build a valid record in memory without touching any database."""
        return build_record(device_id, sequence, random.Random(sequence))

    def post_batch(self, records: list[Any]) -> requests.Response:
        """POST a batch directly to the server, bypassing the agent."""
        return requests.post(f"{self.server_url}/records/batch", json={"records": records}, timeout=5)

    def server_records(self, device_id: str = DEVICE_ID) -> list[dict[str, Any]]:
        """Fetch all of a device's records from the server, following after_sequence pagination."""
        records: list[dict[str, Any]] = []
        after = 0
        while True:
            response = requests.get(
                f"{self.server_url}/devices/{device_id}/records",
                params={"after_sequence": after, "limit": 1000},
                timeout=5,
            )
            response.raise_for_status()
            page = response.json()["records"]
            records.extend(page)
            if len(page) < 1000:
                return records
            after = page[-1]["sequence"]

    def server_stats(self) -> dict[str, Any]:
        """Return the server's in-memory batch counters."""
        response = requests.get(f"{self.server_url}/stats", timeout=5)
        response.raise_for_status()
        return response.json()

    def assert_fully_synced(self, device_id: str = DEVICE_ID) -> list[dict[str, Any]]:
        """Assert no row is PENDING and the server holds each device record exactly once, gap-free and in order."""
        device = [row for row in self.device_rows() if row["device_id"] == device_id]
        server = self.server_records(device_id)
        assert device, "device has no records"
        assert self.device_status_counts()["PENDING"] == 0
        assert all(row["status"] == "SYNCED" and row["synced_at"] for row in device)
        server_ids = [record["event_id"] for record in server]
        assert len(server_ids) == len(set(server_ids)), "duplicate event_id on the server"
        assert {r["event_id"]: r["sequence"] for r in server} == {r["event_id"]: r["sequence"] for r in device}
        assert [record["sequence"] for record in server] == list(range(1, len(server) + 1))
        return server


@pytest.fixture
def sim(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Sim]:
    """Provide a fresh simulation and always terminate its subprocesses afterwards."""
    simulation = Sim(tmp_path, f"{request.node.path.stem} :: {request.node.name}")
    try:
        yield simulation
    finally:
        failed = request.node.stash.get(CALL_FAILED, True)
        simulation.stop_all(print_logs=failed or request.config.getoption("--show-logs"))

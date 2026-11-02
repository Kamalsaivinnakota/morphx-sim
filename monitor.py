"""Live terminal view of the device queue and the server, plus one-shot snapshots that the tests print."""

import argparse
import json
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from device_agent.config import DEFAULT_CONFIG_PATH, ConfigError, load_config

REFRESH_SECONDS = 1.0
HEADER_LINES = 11
COLOURS = {
    "PENDING": "33",
    "SYNCED": "32",
    "REJECTED": "31",
    "UP": "32",
    "DOWN": "31",
    "stored": "32",
    "duplicate": "36",
    "rejected": "31",
    "gap": "35",
}


def paint(text: str, key: str) -> str:
    """Wrap text in the ANSI colour for `key`, but only when writing to a terminal."""
    if not sys.stdout.isatty():
        return text
    return f"\033[{COLOURS[key]}m{text}\033[0m"


def clock(timestamp: str | None) -> str:
    """Show only HH:MM:SS of an ISO-8601 UTC timestamp, or '-' when there is none."""
    return timestamp[11:19] if timestamp else "-"


def gap_line(first: int, last: int) -> str:
    """A table row marking sequence numbers that are absent."""
    numbers = str(first) if first == last else f"{first}-{last}"
    return paint(f"{'':>6}  ... missing sequence {numbers} ...", "gap")


def read_device(db_path: str, limit: int) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """Return status counts and the newest `limit` rows; empty if the agent has not created the DB yet."""
    if not Path(db_path).exists():
        return {}, []
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        counts = {status: count for status, count in conn.execute("SELECT status, COUNT(*) FROM records GROUP BY status")}
        rows = [dict(row) for row in conn.execute("SELECT * FROM records ORDER BY sequence DESC LIMIT ?", (limit,))]
    finally:
        conn.close()
    return counts, rows


def read_server(
    server_url: str, device_id: str, rows: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """Return server stats (None if unreachable) and received_at by event_id for the given device rows."""
    try:
        stats = requests.get(f"{server_url}/stats", timeout=1).json()
        if not rows:
            return stats, {}
        page = requests.get(
            f"{server_url}/devices/{device_id}/records",
            params={
                "after_sequence": rows[-1]["sequence"] - 1,
                "limit": min(1000, rows[0]["sequence"] - rows[-1]["sequence"] + 1),
            },
            timeout=1,
        ).json()
    except (requests.RequestException, ValueError):
        return None, {}
    return stats, {record["event_id"]: record["received_at"] for record in page.get("records", [])}


def device_lines(rows: list[dict[str, Any]], received: dict[str, str]) -> list[str]:
    """Device rows newest first, with a marker wherever sequence numbers are missing."""
    lines = [
        f"{'SEQ':>6}  {'SAMPLE ID':<20} {'EVENT':<8} {'WBC':>7} {'RBC':>7} {'HB':>7}  "
        f"{'STATUS':<9} {'TRIES':>5}  {'MEASURED':<8}  RECEIVED",
        f"{'':>6}  {'':<20} {'':<8} {'10^3/uL':>7} {'10^6/uL':>7} {'g/dL':>7}",
    ]
    if not rows:
        lines.append("  no records yet, waiting for the agent")
    previous = None
    for row in rows:
        if previous is not None and previous - row["sequence"] > 1:
            lines.append(gap_line(row["sequence"] + 1, previous - 1))
        previous = row["sequence"]
        values = json.loads(row["measurements"])
        status = row["status"]
        lines.append(
            f"{row['sequence']:>6}  {row['sample_id']:<20} {row['event_id'][:8]:<8} "
            f"{values['wbc']['value']:>7.1f} {values['rbc']['value']:>7.2f} {values['hb']['value']:>7.1f}  "
            f"{paint(f'{status:<9}', status)} {row['attempts']:>5}  "
            f"{clock(row['measured_at']):<8}  {clock(received.get(row['event_id']))}"
        )
    return lines


def render(
    heading: str,
    device_id: str,
    db_path: str,
    server_url: str,
    counts: dict[str, int],
    rows: list[dict[str, Any]],
    stats: dict[str, Any] | None,
    received: dict[str, str],
) -> str:
    """Build one full screen: device summary, server summary and the device rows table."""
    next_sequence = rows[0]["sequence"] + 1 if rows else 1
    pending, synced, rejected = (counts.get(status, 0) for status in ("PENDING", "SYNCED", "REJECTED"))
    if stats:
        sizes = ", ".join(str(size) for size in stats["recent_batch_sizes"][-5:]) or "-"
        server_detail = (
            f"  batch requests {stats['batch_requests']}   records received {stats['records_received']}"
            f"   last batch sizes {sizes}"
        )
    else:
        server_detail = "  unreachable: the agent keeps saving new records as PENDING and retries with backoff"
    lines = [
        heading,
        "",
        f"DEVICE  {device_id}   {db_path}",
        f"  {paint('pending', 'PENDING')} {pending}   {paint('synced', 'SYNCED')} {synced}   "
        f"{paint('rejected', 'REJECTED')} {rejected}   total {pending + synced + rejected}   "
        f"next sequence {next_sequence}",
        "",
        f"SERVER  {server_url}   {paint('UP', 'UP') if stats else paint('DOWN', 'DOWN')}",
        server_detail,
        "",
        *device_lines(rows, received),
    ]
    return "\n".join(lines)


def snapshot(heading: str, device_id: str, db_path: str, server_url: str, limit: int) -> str:
    """Read the device DB and the server once and return one rendered screen."""
    try:
        counts, rows = read_device(db_path, limit)
    except sqlite3.Error:
        counts, rows = {}, []
    stats, received = read_server(server_url, device_id, rows)
    return render(heading, device_id, db_path, server_url, counts, rows, stats, received)


def results_table(heading: str, results: list[dict[str, Any]]) -> str:
    """Render a server batch response: one line per record with its status and any error."""
    lines = [heading, "", f"{'#':>3}  {'EVENT ID':<36}  {'STATUS':<9}  ERROR"]
    for index, result in enumerate(results, start=1):
        status = result["status"]
        lines.append(
            f"{index:>3}  {result['event_id'] or '-':<36}  {paint(f'{status:<9}', status)}  {result['error'] or ''}"
        )
    return "\n".join(lines)


def server_table(heading: str, server_url: str, device_id: str) -> str:
    """Render a device's records as the server returns them, oldest first, with missing sequences marked."""
    response = requests.get(f"{server_url}/devices/{device_id}/records", params={"limit": 1000}, timeout=5)
    records = response.json()["records"]
    lines = [
        heading,
        "",
        f"SERVER  {server_url}   device {device_id}   {len(records)} stored",
        "",
        f"{'SEQ':>6}  {'SAMPLE ID':<20}  {'EVENT ID':<36}  {'MEASURED':<8}  RECEIVED",
    ]
    previous = 0
    for record in records:
        if record["sequence"] - previous > 1:
            lines.append(gap_line(previous + 1, record["sequence"] - 1))
        previous = record["sequence"]
        lines.append(
            f"{record['sequence']:>6}  {record['sample_id']:<20}  {record['event_id']:<36}  "
            f"{clock(record['measured_at']):<8}  {clock(record['received_at'])}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Redraw the screen every second until Ctrl+C."""
    parser = argparse.ArgumentParser(prog="python monitor.py", description="Live view of the device queue and the server")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="JSON config file")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        sys.stderr.write(f"invalid config: {exc}\n")
        return 2

    sys.stdout.write("\033[?25l")
    try:
        while True:
            limit = max(1, shutil.get_terminal_size().lines - HEADER_LINES)
            heading = (
                f"MorphX live monitor   {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC   "
                f"refresh {REFRESH_SECONDS:g}s   Ctrl+C to quit"
            )
            screen = snapshot(heading, config.device_id, config.db_path, config.server_url, limit)
            sys.stdout.write("\033[H\033[J" + screen)
            sys.stdout.flush()
            time.sleep(REFRESH_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?25h\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Scenario 6: malformed records are rejected with clear errors; valid records in the same batch are stored."""

import json
import sqlite3
import time
from typing import Any

import requests

from conftest import Sim


def _invalid_cases(sim: Sim) -> list[tuple[Any, str]]:
    """Build (record, expected error fragment) pairs, one per kind of defect."""
    missing_field = sim.make_record(3)
    del missing_field["sample_id"]

    wrong_unit = sim.make_record(4)
    wrong_unit["measurements"]["hb"]["unit"] = "g/L"

    zero_sequence = sim.make_record(5)
    zero_sequence["sequence"] = 0

    text_sequence = sim.make_record(6)
    text_sequence["sequence"] = "6"

    bad_uuid = sim.make_record(7)
    bad_uuid["event_id"] = "not-a-uuid"

    bad_timestamp = sim.make_record(8)
    bad_timestamp["measured_at"] = "yesterday at noon"

    naive_timestamp = sim.make_record(9)
    naive_timestamp["measured_at"] = "2026-11-01T10:00:00"

    non_utc_timestamp = sim.make_record(10)
    non_utc_timestamp["measured_at"] = "2026-11-01T12:00:00+02:00"

    text_value = sim.make_record(11)
    text_value["measurements"]["wbc"]["value"] = "high"

    empty_device = sim.make_record(12)
    empty_device["device_id"] = ""

    return [
        (missing_field, "sample_id: Field required"),
        (wrong_unit, "measurements.hb.unit: Input should be 'g/dL'"),
        (zero_sequence, "sequence: Input should be greater than or equal to 1"),
        (text_sequence, "sequence: Input should be a valid integer"),
        (bad_uuid, "event_id: Input should be a valid UUID"),
        (bad_timestamp, "measured_at: Input should be a valid datetime"),
        (naive_timestamp, "measured_at: Input should have timezone info"),
        (non_utc_timestamp, "measured_at: Value error, must be UTC"),
        (text_value, "measurements.wbc.value: Input should be a valid number"),
        (empty_device, "device_id: String should have at least 1 character"),
        ("not an object", "record: Input should be a valid dictionary"),
    ]


def test_invalid_records_rejected_and_valid_ones_stored(sim: Sim) -> None:
    sim.start_server()
    first_valid = sim.make_record(1)
    last_valid = sim.make_record(2)
    invalid = _invalid_cases(sim)
    batch = [first_valid, *(record for record, _ in invalid), last_valid]

    response = sim.post_batch(batch)
    assert response.status_code == 200
    results = response.json()["results"]
    sim.show_results("one batch: 2 valid records around 11 broken ones", results)
    assert len(results) == len(batch)

    assert results[0] == {"event_id": first_valid["event_id"], "status": "stored", "error": None}
    assert results[-1] == {"event_id": last_valid["event_id"], "status": "stored", "error": None}
    for result, (record, expected_error) in zip(results[1:-1], invalid):
        expected_id = record.get("event_id") if isinstance(record, dict) else None
        assert result["event_id"] == expected_id
        assert result["status"] == "rejected"
        assert expected_error in result["error"]

    sim.show_server("only the 2 valid records were stored")
    stored_ids = [record["event_id"] for record in sim.server_records()]
    assert stored_ids == [first_valid["event_id"], last_valid["event_id"]]


def test_malformed_body_returns_422(sim: Sim) -> None:
    sim.start_server()
    url = f"{sim.server_url}/records/batch"
    cases = [
        ('{"items": []}', {"json": {"items": []}}),
        ('{"records": "not a list"}', {"json": {"records": "not a list"}}),
        ("{not json", {"data": "{not json", "headers": {"Content-Type": "application/json"}}),
    ]

    codes = [requests.post(url, timeout=5, **kwargs).status_code for _, kwargs in cases]
    sim.show_text(
        "malformed request bodies are refused as a whole",
        [f"HTTP {code}  <-  {body}" for (body, _), code in zip(cases, codes)],
    )
    assert codes == [422, 422, 422]


def test_rejected_record_does_not_block_the_queue(sim: Sim) -> None:
    sim.start_server()
    records = sim.seed_device(4)
    bad = records[0]
    bad["measurements"]["rbc"]["unit"] = "cells/L"
    conn = sqlite3.connect(sim.device_db)
    with conn:
        conn.execute(
            "UPDATE records SET measurements = ? WHERE event_id = ?",
            (json.dumps(bad["measurements"]), bad["event_id"]),
        )
    conn.close()

    agent = sim.start_agent(interval=3600)
    sim.wait_until(lambda: sim.device_status_counts()["PENDING"] == 0, message="the queue to be processed")
    time.sleep(1.5)
    sim.stop(agent)
    sim.show("sequence 1 had a wrong unit: REJECTED once and never retried, while 2-4 SYNCED")

    rows = sim.device_rows()
    assert {row["sequence"]: row["status"] for row in rows} == {
        1: "REJECTED",
        2: "SYNCED",
        3: "SYNCED",
        4: "SYNCED",
    }
    assert rows[0]["attempts"] == 1
    assert sim.server_stats()["batch_requests"] == 1
    assert "record rejected by server, will not retry" in agent.log()
    assert "measurements.rbc.unit" in agent.log()
    assert [record["sequence"] for record in sim.server_records()] == [2, 3, 4]

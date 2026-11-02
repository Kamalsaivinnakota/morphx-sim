"""Scenario 1: records are generated, synced and stored in order with no gaps."""

import json
from datetime import datetime, timedelta

from conftest import Sim

UNITS = {"wbc": "10^3/uL", "rbc": "10^6/uL", "hb": "g/dL"}
RANGES = {"wbc": (4.0, 11.0), "rbc": (4.0, 6.0), "hb": (12.0, 17.0)}


def test_records_are_generated_synced_and_stored_in_order(sim: Sim) -> None:
    sim.start_server()
    agent = sim.start_agent(interval=0.3)
    sim.wait_until(lambda: len(sim.server_records()) >= 5, message="5 records on the server")
    sim.show("agent and server running: each new record is saved, then synced (RECEIVED is filled in)")
    sim.stop(agent)
    sim.drain()

    records = sim.assert_fully_synced()
    sim.show(f"finished: all {len(records)} records SYNCED, sequences 1..{len(records)} with no gaps")
    sim.show_text(
        "one record exactly as the server returns it: every value carries its explicit unit",
        json.dumps(records[0], indent=2).splitlines(),
    )
    assert len(records) >= 5
    assert "sync ok" in agent.log()

    for record in records:
        measured_at = datetime.fromisoformat(record["measured_at"])
        received_at = datetime.fromisoformat(record["received_at"])
        assert measured_at.utcoffset() == timedelta(0)
        assert received_at.utcoffset() == timedelta(0)
        assert received_at > measured_at
        assert record["sample_id"] == f"SMP-{measured_at:%Y%m%d}-{record['sequence']:06d}"
        assert set(record["measurements"]) == set(UNITS)
        for name, (low, high) in RANGES.items():
            assert record["measurements"][name]["unit"] == UNITS[name]
            assert low <= record["measurements"][name]["value"] <= high

    for row in sim.device_rows():
        assert row["attempts"] >= 1
        assert datetime.fromisoformat(row["synced_at"]).utcoffset() == timedelta(0)

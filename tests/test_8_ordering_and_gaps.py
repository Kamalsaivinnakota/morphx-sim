"""Scenario 8: records come back ordered by sequence and a missing sequence is visible as a gap."""

import sqlite3

import requests

from conftest import Sim


def missing_sequences(sequences: list[int]) -> list[int]:
    """Return every sequence between 1 and the highest seen that is absent."""
    return sorted(set(range(1, max(sequences) + 1)) - set(sequences))


def test_out_of_order_arrival_is_returned_in_sequence_order(sim: Sim) -> None:
    sim.start_server()
    device_id = "MORPHX_ORDER_TEST"
    records = {seq: sim.make_record(seq, device_id=device_id) for seq in (1, 2, 4, 5)}
    sim.post_batch([records[5], records[2]])
    sim.post_batch([records[4], records[1]])
    sim.show_server("posted in the order 5, 2, 4, 1: returned as 1, 2, 4, 5 with 3 marked missing", device_id)

    stored = sim.server_records(device_id)
    sequences = [record["sequence"] for record in stored]
    assert sequences == [1, 2, 4, 5]
    assert missing_sequences(sequences) == [3]

    page = requests.get(
        f"{sim.server_url}/devices/{device_id}/records",
        params={"after_sequence": 2, "limit": 1},
        timeout=5,
    ).json()
    assert [record["sequence"] for record in page["records"]] == [4]


def test_missing_device_record_shows_as_gap_on_server(sim: Sim) -> None:
    agent = sim.start_agent(interval=0.2)
    sim.wait_until(lambda: len(sim.device_rows()) >= 6, message="a local backlog")
    sim.stop(agent)

    conn = sqlite3.connect(sim.device_db)
    with conn:
        conn.execute("DELETE FROM records WHERE sequence = 3")
    conn.close()
    expected = [row["sequence"] for row in sim.device_rows()]
    sim.show("sequence 3 was deleted on the device before it could be synced", rows=12)

    sim.start_server()
    sim.drain()
    sim.show_server("server view: sequence 3 is visibly missing")

    sequences = [record["sequence"] for record in sim.server_records()]
    assert sequences == expected
    assert sequences == sorted(sequences)
    assert missing_sequences(sequences) == [3]

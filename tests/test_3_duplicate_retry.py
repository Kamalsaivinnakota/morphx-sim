"""Scenario 3: re-sending the same event_id yields one server row and a 'duplicate' result."""

from conftest import Sim


def test_same_event_id_posted_twice_is_stored_once(sim: Sim) -> None:
    sim.start_server()
    record = sim.make_record(sequence=1)

    first = sim.post_batch([record]).json()["results"]
    sim.show_results("first POST of a record", first)
    original = sim.server_records()
    second = sim.post_batch([record]).json()["results"]
    sim.show_results("the same event_id POSTed again", second)
    sim.show_server("the server still holds exactly one row for it")

    assert first == [{"event_id": record["event_id"], "status": "stored", "error": None}]
    assert second == [{"event_id": record["event_id"], "status": "duplicate", "error": None}]
    assert sim.server_records() == original
    assert [stored["event_id"] for stored in original] == [record["event_id"]]


def test_same_event_id_twice_in_one_batch(sim: Sim) -> None:
    sim.start_server()
    record = sim.make_record(sequence=1)

    results = sim.post_batch([record, record]).json()["results"]
    sim.show_results("one batch that contains the same record twice", results)
    sim.show_server("the server holds exactly one row")

    assert [result["status"] for result in results] == ["stored", "duplicate"]
    assert len(sim.server_records()) == 1


def test_lost_response_is_retried_by_agent_and_reported_duplicate(sim: Sim) -> None:
    sim.start_server()
    [record] = sim.seed_device(1)
    assert sim.post_batch([record]).json()["results"][0]["status"] == "stored"
    original = sim.server_records()
    sim.show("lost response: the server stored the record (RECEIVED set) but the device row is still PENDING")

    agent = sim.start_agent(interval=3600)
    sim.wait_until(lambda: sim.device_status_counts()["SYNCED"] == 1, message="the retried record to be acknowledged")
    sim.stop(agent)
    sim.show("the agent retried with the same event_id: the server answered duplicate, the row is now SYNCED")

    [row] = sim.device_rows()
    assert row["event_id"] == record["event_id"]
    assert row["attempts"] == 1
    assert "duplicate=1" in agent.log()
    assert sim.server_records() == original

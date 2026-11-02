"""Scenario 2: acquisition continues while the server is down, and the backlog syncs once it is up."""

from conftest import Sim


def test_backlog_builds_while_server_down_and_syncs_after(sim: Sim) -> None:
    agent = sim.start_agent(interval=0.3)
    sim.wait_until(lambda: len(sim.device_rows()) >= 6, message="backlog while the server is down")
    sim.wait_until(
        lambda: any(row["attempts"] >= 2 for row in sim.device_rows()),
        message="failed send attempts to be persisted",
    )
    sim.show("server is DOWN: the agent keeps saving records as PENDING and retries with backoff (TRIES)")

    rows = sim.device_rows()
    assert all(row["status"] == "PENDING" for row in rows)
    assert all(row["synced_at"] is None for row in rows)
    assert any(row["last_attempt_at"] for row in rows)
    assert "sync failed: connection refused" in agent.log()
    backlog_ids = {row["event_id"] for row in rows}

    sim.start_server()
    sim.wait_until(
        lambda: backlog_ids <= {record["event_id"] for record in sim.server_records()},
        message="the offline backlog to reach the server",
    )
    sim.wait_until(
        lambda: len(sim.server_records()) >= len(backlog_ids) + 3,
        message="new records to keep arriving after recovery",
    )
    sim.show("server is UP again: the backlog was drained and new records keep syncing")
    assert "backoff reset" in agent.log()

    sim.stop(agent)
    sim.drain()
    records = sim.assert_fully_synced()
    sim.show(f"finished: {len(records)} records, each stored exactly once, none lost")
    assert backlog_ids <= {record["event_id"] for record in records}

"""Scenario 4: a killed and restarted agent continues its sequence and loses nothing."""

from conftest import Sim


def test_agent_crash_and_restart(sim: Sim) -> None:
    server = sim.start_server()
    agent = sim.start_agent(interval=0.3)
    sim.wait_until(lambda: len(sim.server_records()) >= 3, message="some records synced before the crash")
    sim.show("normal operation before the crash")
    sim.stop(server)
    sim.wait_until(lambda: sim.device_status_counts()["PENDING"] >= 3, message="a pending backlog before the crash")
    sim.kill(agent)
    sim.show("server stopped, then the agent was killed with SIGKILL: every saved row is still on disk", rows=12)

    rows_before = sim.device_rows()
    before = {row["sequence"]: row["event_id"] for row in rows_before}
    pending_before = {row["event_id"] for row in rows_before if row["status"] == "PENDING"}
    last_sequence = max(before)
    assert list(before) == list(range(1, last_sequence + 1))

    sim.start_server()
    restarted = sim.start_agent(interval=0.3)
    sim.wait_until(
        lambda: max(row["sequence"] for row in sim.device_rows()) >= last_sequence + 3,
        message="new records after the restart",
    )
    sim.wait_until(
        lambda: pending_before <= {record["event_id"] for record in sim.server_records()},
        message="the pre-crash backlog to sync",
    )
    sim.show(
        f"agent restarted: the sequence continued from {last_sequence + 1}, old rows kept their EVENT ids and synced",
        rows=16,
    )
    sim.stop(restarted)
    sim.drain()

    records = sim.assert_fully_synced()
    assert f"next_sequence={last_sequence + 1} pending={len(pending_before)}" in restarted.log()
    after = {row["sequence"]: row["event_id"] for row in sim.device_rows()}
    on_server = {record["sequence"]: record["event_id"] for record in records}
    for sequence, event_id in before.items():
        assert after[sequence] == event_id
        assert on_server[sequence] == event_id

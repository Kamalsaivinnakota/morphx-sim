"""Scenario 5: stored records survive a server restart and new records keep arriving."""

from conftest import Sim


def test_server_restart_keeps_data(sim: Sim) -> None:
    server = sim.start_server()
    agent = sim.start_agent(interval=0.3)
    sim.wait_until(lambda: len(sim.server_records()) >= 4, message="records before the server restart")
    before = sim.server_records()
    sim.show("server running: records are stored")
    sim.stop(server)
    sim.wait_until(lambda: "sync failed" in agent.log(), message="the agent to notice the outage")
    sim.show("server stopped: new records wait as PENDING on the device")

    sim.start_server()
    after_restart = sim.server_records()
    sim.show("server restarted: earlier records are still there with the same RECEIVED times", rows=12)
    assert after_restart[: len(before)] == before

    sim.wait_until(
        lambda: len(sim.server_records()) >= len(before) + 3,
        message="new records after the server restart",
    )
    sim.stop(agent)
    sim.drain()

    records = sim.assert_fully_synced()
    sim.show(f"finished: {len(records)} records, old and new, each stored once")
    assert records[: len(before)] == before

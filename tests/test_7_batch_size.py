"""Scenario 7: a backlog of 120 records with batch_size=50 drains in exactly 3 requests."""

from conftest import Sim


def test_backlog_of_120_drains_in_three_batches(sim: Sim) -> None:
    sim.seed_device(120)
    assert sim.device_status_counts()["PENDING"] == 120

    sim.start_server()
    assert sim.server_stats()["batch_requests"] == 0
    sim.show("backlog of 120 PENDING records; the server has received nothing yet")

    agent = sim.start_agent(interval=3600, batch_size=50)
    sim.wait_until(lambda: sim.device_status_counts()["SYNCED"] == 120, message="all 120 records to sync")
    stats = sim.server_stats()
    sim.stop(agent)
    sim.show("drained in 3 requests of 50, 50 and 20 (see 'last batch sizes')")

    assert stats["batch_requests"] == 3
    assert stats["recent_batch_sizes"] == [50, 50, 20]
    assert stats["records_received"] == 120
    assert agent.log().count("sync ok:") == 3
    assert len(sim.assert_fully_synced()) == 120

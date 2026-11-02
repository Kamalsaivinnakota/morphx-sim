# MorphX device simulator: offline-first sync

A small command-line simulation of the software on a MorphX blood analyser.

- The **device agent** generates synthetic blood-count records (WBC, RBC, haemoglobin), saves each one to a local SQLite database **first**, and pushes them to a central server over HTTP in the background.
- The **central server** validates each record, stores it idempotently, and serves records per device in acquisition order.

The core property is offline-first reliability: acquisition never stops during an outage, the backlog syncs by itself when the server returns, and no record is lost or duplicated across retries, crashes and restarts.

All data is synthetic. No real patient data is used.

## Architecture

```
  DEVICE PROCESS  (python -m device_agent.main)            SERVER PROCESS  (python -m server.main)
 +-----------------------------------------------+        +------------------------------------------+
 |                                               |        |  FastAPI + uvicorn                       |
 |  generator thread         sync thread         |  HTTP  |                                          |
 |  every <interval> s       loop:               |  POST  |  POST /records/batch                     |
 |    build record            SELECT PENDING     | -----> |    validate each record on its own       |
 |    INSERT status=PENDING   ORDER BY sequence  |        |    INSERT ... ON CONFLICT DO NOTHING     |
 |         |                  LIMIT batch_size   | <----- |    one transaction per batch             |
 |         v                  UPDATE SYNCED /    | results|    reply stored|duplicate|rejected      |
 |  +-----------------+       REJECTED/attempts  |        |                                          |
 |  |  device.db      | <-------------+          |        |  GET /devices/{id}/records               |
 |  |  SQLite, WAL    |   (only shared state)    |        |    ordered by sequence, with received_at |
 |  |  source of truth|                          |        |  GET /stats   (in-memory counters)       |
 |  +-----------------+                          |        |  +-----------------+                     |
 |                                               |        |  |  server.db      |                     |
 +-----------------------------------------------+        |  +-----------------+                     |
                                                          +------------------------------------------+
```

- The device pushes; the server never reads the device DB. Either side can be offline.
- Every record is committed to `device.db` before any network attempt is made.
- The two agent threads share nothing but the SQLite file. The generator never touches the network and never waits for the sync thread.

## Tech stack

| Choice | Why |
|---|---|
| Python 3.10+ | Matches the brief; `X \| None` type hints work natively. |
| Threads, not asyncio | One blocking HTTP call can only stall the sync thread. Plain `sqlite3` and `requests` stay simple and synchronous. |
| SQLite (stdlib `sqlite3`) on both sides | Zero setup, durable, transactional, one file per process. |
| WAL mode, `synchronous=FULL`, 5 s busy timeout | WAL lets the generator write while the sync thread reads. FULL fsyncs each commit so an acknowledged insert survives power loss. The timeout avoids "database is locked". |
| One SQLite connection per thread | `sqlite3` connections must not be shared across threads. The server opens a short-lived connection per request. |
| FastAPI + Pydantic | Declarative, strict validation with precise error locations (`measurements.hb.unit`). |
| `requests` | Simple timeouts and clear exception types for "server down". |

## Install

```bash
cd morphx-sim
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

From the project root, in two terminals:

```bash
python -m server.main
```

```bash
python -m device_agent.main
```

Query stored records:

```bash
curl "http://127.0.0.1:8000/devices/MORPHX_SIM_001/records"
```

To see offline behaviour, stop the server with Ctrl+C. The agent keeps generating and logs lines like `sync failed: connection refused, backoff 4.2s, pending=12`. Start the server again and the backlog drains immediately.

### Live monitor

Instead of reading scrolling logs, run the monitor in a third terminal:

```bash
python monitor.py
```

It redraws one screen in place every second:

```
MorphX live monitor   2026-11-01 15:54:21 UTC   refresh 1s   Ctrl+C to quit

DEVICE  MORPHX_SIM_001   /path/to/device.db
  pending 0   synced 17   rejected 0   total 17   next sequence 18

SERVER  http://127.0.0.1:8000   UP
  batch requests 2   records received 17   (since server start)

   SEQ  SAMPLE ID              WBC   RBC    HB  STATUS    TRIES  MEASURED  RECEIVED
    17  SMP-20261101-000017    7.9  5.25  15.3  SYNCED        1  15:54:21  15:54:21
    16  SMP-20261101-000016    4.1  4.04  16.8  SYNCED        1  15:54:20  15:54:21
```

- `STATUS`, `TRIES` (the `attempts` column) and `MEASURED` come from the device DB.
- `RECEIVED` comes from the server, so a time there proves the record is stored on the server.
- When the server is down, the header shows `DOWN` and the `pending` count and `TRIES` grow.

The monitor only reads. It never writes to either side, so it can be started and stopped at any time. It uses the same `config.json` (or `--config other.json`). To keep everything in one terminal, start the server and agent in the background with their logs going to files:

```bash
python -m server.main > server.log 2>&1 &
python -m device_agent.main > agent.log 2>&1 &
python monitor.py
```

## Configuration

All settings live in one file, [`config.json`](config.json), in the project root. There are no command-line settings and no environment variables. The agent reads only the `device` section; the server reads only the `server` section.

- Every key is required. A missing key, an unknown key or a wrong type stops the program with a clear `invalid config:` message and exit code 2.
- A relative `db_path` is resolved against the folder that holds the config file, not the current directory.
- To use a different file, for example for a second simulated device, pass `--config other.json`. This is the only command-line option, and the tests use it to give each process its own temporary config.

### `device` section

| Key | Value in config.json | Meaning |
|---|---|---|
| `device_id` | `MORPHX_SIM_001` | Stable device identity |
| `interval` | `5` | Seconds between acquisitions |
| `server_url` | `http://127.0.0.1:8000` | Central server base URL |
| `batch_size` | `50` | Max records per POST |
| `db_path` | `device.db` | Local SQLite file |
| `request_timeout` | `5` | HTTP timeout in seconds |
| `backoff_min` | `1` | First retry delay in seconds |
| `backoff_max` | `60` | Retry delay cap in seconds |

### `server` section

| Key | Value in config.json | Meaning |
|---|---|---|
| `host` | `127.0.0.1` | Interface to listen on |
| `port` | `8000` | TCP port; must match the port in `device.server_url` |
| `db_path` | `server.db` | Server SQLite file |

## Data flow and fields

1. **Generate.** On each tick the generator builds a record and INSERTs it with `status='PENDING'` in its own committed transaction.
2. **Select.** The sync thread selects up to `batch_size` PENDING rows, ordered by `sequence`.
3. **Send.** It POSTs them as `{"records": [...]}` to `/records/batch`.
4. **Store.** The server validates each record on its own, sets `received_at`, and inserts all valid records in one transaction.
5. **Apply results.** The device applies each per-record result in one transaction: `stored` and `duplicate` become `SYNCED`, `rejected` becomes `REJECTED`.

| Field | Set by | Purpose |
|---|---|---|
| `event_id` | device, once | UUID4 idempotency key. Never regenerated, reused on every retry and restart. Primary key on both sides. |
| `device_id` | device config | Which analyser produced the record. |
| `sample_id` | device | `SMP-YYYYMMDD-NNNNNN`. Groups the three values of one test. |
| `sequence` | device | Per-device counter, >= 1, recovered as `MAX(sequence)+1` at startup. Defines order and makes gaps detectable. |
| `measured_at` | device | Acquisition time, UTC ISO-8601 with offset. |
| `measurements` | device | `wbc` (10^3/uL), `rbc` (10^6/uL), `hb` (g/dL), each `{value, unit}`. |
| `received_at` | server only | Server UTC arrival time. Never accepted from the device; any such field is ignored. |
| `status`, `attempts`, `last_attempt_at`, `synced_at` | device only | Local sync bookkeeping. These are never sent. |

Synthetic ranges: WBC 4.0 to 11.0, RBC 4.0 to 6.0, Hb 12.0 to 17.0.

## Detecting server outages and backoff

There is no health-check endpoint in the sync path. The send attempt itself is the probe. A failure is any of: connection refused, DNS or connection error, timeout (`request_timeout`), HTTP 5xx, an unexpected non-200 status, or an unparseable response.

On failure:

- Rows stay PENDING. `attempts` is incremented and `last_attempt_at` is set in the device DB.
- The in-memory failure counter increases.
- The thread sleeps for `min(backoff_max, backoff_min * 2^(failures-1))`, multiplied by a random factor between 0.8 and 1.2.

With the defaults the schedule is **1, 2, 4, 8, 16, 32, 60, 60, ... seconds (each +/-20%)**. On the first successful response the counter resets to zero and the next batch is sent immediately, with no sleep, until the backlog is empty. When idle, the sync thread polls the DB about once a second.

A record the server rejects is marked `REJECTED`, logged with the server's error, and never retried, so one bad record cannot block the queue.

## Idempotency and the lost-response case

- `event_id` is created once, in the generator, and stored with the row. Retries read it back from disk, so it is identical across retries, crashes and restarts.
- The server inserts with `ON CONFLICT (event_id) DO NOTHING`. `rowcount == 1` means `stored`; `0` means `duplicate`. The first write wins, and the original `received_at` is kept.
- **Lost response:** the server commits a batch but the device times out before reading the reply. The rows are still PENDING on the device, so the next attempt resends the same `event_id`s. The server answers `duplicate`, and the device marks them SYNCED. Exactly one server row exists. `tests/test_3_duplicate_retry.py` reproduces this by storing the record on the server first, then starting the agent.
- **Crash safety:** each insert or update is a single SQLite transaction. A SIGKILL mid-write loses at most an uncommitted row that was never acknowledged anywhere.

## Tests

```bash
python -m pytest -v
```

Each test starts the real server and/or agent as separate subprocesses (`sys.executable -m ... --config <tmp file>`). Each process gets its own temporary config file pointing at temporary DB files and a free port, with short intervals (0.2 to 0.3 s) and a fast backoff (0.1 to 0.5 s). Tests poll with timeouts instead of sleeping, and teardown always terminates every subprocess. Subprocess logs are printed in the failure report.

| File | What it proves |
|---|---|
| `test_1_normal_flow.py` | Records are generated, synced, stored once, gap-free and in order. `received_at` is later than and separate from `measured_at`. Units and ranges are correct. |
| `test_2_server_down_then_up.py` | With no server, rows accumulate as PENDING and failed attempts are persisted. After start-up everything syncs with no loss or duplicates, and backoff resets. |
| `test_3_duplicate_retry.py` | The same `event_id` twice (across batches and within one batch) gives one row and `duplicate`. The lost-response retry by the real agent ends with one row and SYNCED. |
| `test_4_agent_restart.py` | After SIGKILL, the sequence continues from `MAX+1`, pending rows survive and sync, and `event_id`s are unchanged. |
| `test_5_server_restart_persistence.py` | Records stored before a server restart are returned unchanged afterwards, and new ones keep arriving. |
| `test_6_invalid_record.py` | Missing field, wrong unit, bad sequence, bad UUID, bad, naive or non-UTC timestamp, non-numeric value, empty device_id and non-object items are each rejected with a precise error, while valid records in the same batch are stored. A malformed body gets 422. A rejected record does not block the agent's queue and is not retried. |
| `test_7_batch_size.py` | A 120-record backlog with `batch_size=50` drains in exactly 3 requests of 50, 50 and 20, counted by the server's `/stats`. |
| `test_8_ordering_and_gaps.py` | Out-of-order arrival is returned sorted by `sequence`, `after_sequence`/`limit` paging works, and a record deleted on the device shows as a sequence gap on the server. |

### Running one scenario

```bash
python -m pytest tests/test_2_server_down_then_up.py -v
```

Run a single test function by name:

```bash
python -m pytest "tests/test_3_duplicate_retry.py::test_lost_response_is_retried_by_agent_and_reported_duplicate" -v
```

### Seeing what each test does

Every test prints the monitor's tables at its key steps: before the action, during it, and the final state. A plain `pytest` run hides them and only reports pass or fail. Add `-s` to see them live as the test runs:

```bash
python -m pytest tests/test_2_server_down_then_up.py -s
```

That shows three screens: server `DOWN` with records piling up as PENDING and TRIES rising, then server `UP` with the backlog drained, then every record SYNCED. Each screen has a banner naming the test and the step. Depending on the test, a screen can be:

- the device and server view, the same as `python monitor.py`;
- a table of per-record batch results, with `stored`, `duplicate` or `rejected` and the exact error;
- the server's stored records in order, with `... missing sequence 3 ...` wherever a number is absent.

### Testing Manually

Start the server, the agent and `python monitor.py` in three terminals, then follow these steps in order.

**1. Normal flow.** Watch for about 20 s.
A new row appears every 5 s, first as PENDING and then as SYNCED, with a RECEIVED time filled in.

**2. Turn the server off.** Press Ctrl+C in the server terminal.
The monitor shows `DOWN`. Rows keep arriving as PENDING, and TRIES grows more slowly over time because of the backoff. The agent prints `sync failed: connection refused, backoff ...`.

**3. Turn the server back on.** Run `python -m server.main`.
The monitor shows `UP`, and every PENDING row becomes SYNCED within a second or two. The agent prints `server reachable again ... backoff reset`.

**4. Batch-wise sync.** Records are sent in batches of up to 50. At the default 5 s interval, a short outage builds only a few records, so they all go in one request. To see several batches, the backlog has to be bigger than 50:
- either keep the server off for more than 4 minutes;
- or set `"interval": 0.1` in `config.json`, restart the agent, and stop the server for about 15 s.

After the server comes back, the monitor's server line shows `last batch sizes 50, 50, ...`, and the server prints one `batch: size=50` line per request. Set the interval back to `5` afterwards.

**5. Turn the device off and restart it.** Stop the agent with Ctrl+C (a clean stop), or simulate a crash:

```bash
pkill -9 -f device_agent.main
```

Then start it again with `python -m device_agent.main`. Its first log line says `next_sequence=N`, and new rows continue from N. If you stop the server before killing the agent, you also see that PENDING rows survive the restart, keep their original EVENT ids, and sync once the server is back.

**Check that nothing was lost or duplicated.** Run these after the steps above.

No duplicate sequences on the device (prints nothing):

```bash
sqlite3 device.db "SELECT sequence, COUNT(*) FROM records GROUP BY sequence HAVING COUNT(*) > 1;"
```

No gaps in the sequence (the two numbers are equal):

```bash
sqlite3 device.db "SELECT COUNT(*), MAX(sequence) FROM records;"
```

Device and server hold the same records (the two counts are equal):

```bash
sqlite3 device.db "SELECT COUNT(*) FROM records WHERE status = 'SYNCED';" && sqlite3 server.db "SELECT COUNT(*) FROM records;"
```

No duplicates on the server (prints nothing):

```bash
sqlite3 server.db "SELECT event_id, COUNT(*) FROM records GROUP BY event_id HAVING COUNT(*) > 1;"
```

## Trade-offs and limitations

- **First write wins on duplicates.** A resend with the same `event_id` but different content is reported as `duplicate` and the new content is silently dropped. No conflict detection is done.
- **Server `/stats` is in memory.** It resets on restart and exists for observability and tests.
- **Server `(device_id, sequence)` index is not unique**, as specified. If a device DB is wiped, its sequence restarts at 1 and collides in ordering. `after_sequence` paging assumes unique sequences per device.
- **One agent per device DB.** Two agents on the same file would fight over sequence numbers; the unique index stops them from corrupting data, but the second would keep failing inserts.
- **Missed ticks are skipped.** If the generator stalls, for example on a long DB lock, it skips missed ticks instead of bursting fake catch-up records.
- **`received_at` is per batch.** All records in one request share the same timestamp.
- **Device clock is trusted.** There is no clock-skew detection between `measured_at` and `received_at`.
- **The config loader is duplicated** in `device_agent/config.py` and `server/config.py`. This is deliberate: the two programs share no code, so either can be deployed alone.
- **No authentication or TLS.** Any client can post for any `device_id`.
- **No server-side batch size limit** and no request body size limit.
- **SYNCED rows are kept forever** on the device; the DB grows without bound.

## Unfinished work / future improvements

- Store the rejection reason and a `rejected_at` on the device row instead of only logging it.
- Retention: purge or archive SYNCED rows older than N days on the device.
- Detect conflicting duplicates by storing a content hash and returning `conflict` when it differs.
- Real fault-injection tests: a server that delays its response past `request_timeout` after committing (a true lost response), and a server that returns 5xx. Today these paths are covered by design and by the simulated lost-response test, not by injected network faults.
- Unit tests for the backoff calculation and for config-file validation (missing, unknown and mistyped keys).
- A gap-report endpoint per device, and Prometheus metrics instead of `/stats`.
- Schema migrations, packaging (`pyproject.toml` with console scripts), a Dockerfile and CI.


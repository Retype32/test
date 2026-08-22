# Load / crash testing

Two scripts that drive a *running* Nexus server over real HTTP to answer two
questions: how many concurrent users it can hold, and whether it survives a
large batch of transactions.

**Run these against a disposable environment, not a real deployment.**
They log in as the seeded demo accounts and create real transaction rows
(bag numbers prefixed `CU-`/`BATCH-` so they're easy to find and delete
afterwards). They do not modify or delete anything else.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Terminal 1 - use a throwaway set of DBs so this never touches real data
rm -f *.db  # only if these are already throwaway/dev databases
python run_backend.py
```

## Concurrent users

```bash
python -m loadtest.concurrent_users --base-url http://127.0.0.1:8000 \
    --levels 10,25,50,100,200,400 --actions-per-user 5
```

Ramps through each concurrency level, running a login + create-transaction +
read-it-back loop per simulated user. Stops and reports the breaking point
once the error rate exceeds 2% or p95 latency exceeds 3s at a level.

## Batch transactions

```bash
python -m loadtest.batch_transactions --base-url http://127.0.0.1:8000 \
    --total 3000 --concurrency 50
```

Fires `--total` transaction-create requests with `--concurrency` in flight
at once, reports throughput and error rate, and specifically calls out
SQLite `"database is locked"` failures if any occur.

## Interpreting results

- Both scripts only use the 4 seeded accounts (`admin`/`supervisor1`/
  `cashier1`/`cashier2`) round-robin — the point is to stress the *server's*
  handling of concurrent connections and concurrent writes, not to model a
  specific number of distinct staff. A real deployment with N distinct
  logged-in cashiers behaves the same way at the HTTP/DB layer.
- The default SQLite backend allows exactly one writer at a time. Expect
  `"database is locked"` errors to start appearing as write concurrency
  rises — that is the single biggest capacity ceiling in the current setup,
  not CPU or memory. See the repo-root `LOADTEST_RESULTS.md` (if present)
  for a worked example.
- Numbers depend heavily on the machine running the server (CPU, disk).
  Treat absolute throughput numbers as relative/directional on a laptop or
  small cloud instance, not as a guaranteed production ceiling.

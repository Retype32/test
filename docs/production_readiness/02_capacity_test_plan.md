# 02 — Capacity, Load, and Endurance Test Plan (Phase 1)

Date: 2026-08-22
Recorded by: Agent 2 (Load, Capacity, Endurance)
Status: **Design only.** No load-test scripts were written or run. No application
code, configuration, or dependencies were changed. This document is the plan for
a later implementation phase (Phase 3+).

---

## 1. Confirmation: there is no existing load-test suite

Independently confirmed (in addition to Agent 1's `00_baseline.md` §2 finding):

```
find . -iname "*locust*" -o -iname "*loadtest*" -o -iname "*load_test*" -o -iname "*k6*" -o -iname "*gatling*"
grep -ril "locust\|ASGITransport.*concurren\|asyncio.gather.*client\." tests/ 2>/dev/null
```
No hits beyond `httpx`/`ASGITransport` usage in `tests/conftest.py`, which is the
in-process functional test harness, not a load generator. `requirements.txt`
carries no load-testing package (`locust` not installed: `pip show locust` →
"Package(s) not found"). **There is nothing to review here — this document
designs a load-test suite from scratch.**

The existing pytest suite (127 passed, 1 skipped, 19.4s per Agent 1) is
functional-only and runs entirely over `httpx.AsyncClient(transport=ASGITransport(...))`
— in-process, no real socket, no TLS, no OS-level TCP/connection-pool behavior,
single shared event loop with the app itself. It cannot answer any question this
document asks. Every workload below **must** run against a real `uvicorn` process
reached over `http://127.0.0.1:<port>` (or a real NIC-bound address), started the
way `run_backend.py` starts it (minus `reload=True`, which forks a watcher process
that has no place in a load test).

---

## 2. What the app actually looks like from a load-testing angle

Read directly (not assumed) from source before designing anything below:

| Fact | Where | Why it matters here |
|---|---|---|
| One `create_async_engine` per database (1 core + 4 catalogs = 5 engines), no `pool_size`/`max_overflow` set | `backend/core/database.py:22-49` | SQLAlchemy defaults apply per engine (`QueuePool`, default `pool_size=5`, `max_overflow=10` for a real DBAPI driver). Under concurrency this is a real, untested ceiling — Workload A/B exist specifically to find it. |
| SQLite (`aiosqlite`) is the default DB for all 5 databases | `backend/core/config.py:13-17` | SQLite serializes writers; concurrent transaction-creates from multiple catalogs are independent (separate files/engines), but concurrent writes *within* one catalog (the realistic case — many cashiers on one shift, one catalog) will contend on that one file. This is a first-order variable for every workload: **results must be reported per catalog and must not be assumed to hold under PostgreSQL** (Agent 4's track). |
| `bcrypt.hashpw`/`bcrypt.checkpw` called directly inside an `async def`, no `asyncio.to_thread` | `backend/core/security.py:8-13`, called from `backend/services/auth_service.py:21,35` | CPU-bound, blocking, ~50-300ms per call depending on cost factor and hardware. Since `uvicorn` runs one asyncio event loop per worker process and `run_backend.py` starts a single worker, **every concurrent login serializes behind this call** — a login burst (e.g., start-of-shift) is a plausible real bottleneck, not a hypothetical one. Workload A must include a login-only sub-case at each concurrency tier. |
| Excel/CSV report build (`openpyxl`/`pandas`) runs synchronously inside the async route handler, no thread offload | `web/routes/reports_web.py:69-101`, `reports/report_engine.py:140-210` | Same class of problem as bcrypt: a multi-thousand-row report build blocks the single event loop for its full duration, stalling every other in-flight request (including `/health`) on that worker. This is the single most likely head-of-line-blocking hotspot in the app and is the primary target of Workload F's report scenario. |
| Hardware counter read *is* correctly offloaded | `web/routes/transaction_entry_web.py:441-452` (`asyncio.to_thread(_read_counter_sync)`) | Correct pattern, unlike the two rows above. `COUNTER_MODE` defaults to `"none"` (`hardware/config.py:7`) — no real device in this sandbox, so this endpoint isn't part of the load test's request mix by default (see §4.7). |
| `bag_number` has no unique/DB constraint; duplicate detection is a soft, scoped heuristic | `backend/models/transaction.py:29`, `backend/services/duplicate_detection_service.py:37-53` | Detection only compares a new transaction against **the same user's own transactions on the same business_date** (`transaction_repository.py:81-92`). Two different cashiers entering the same bag number is never flagged by the app. This is a correctness fact the reconciliation checks in §10 are built around, not a load-capacity fact — noted here because Workload F's duplicate scenario depends on it. |
| `EODClosure.close()` is an O(1) status flip — it does not sum, touch, or even count the day's transactions | `backend/repositories/eod_repository.py:19-42`, `backend/models/eod.py:15-34` (no monetary/total column at all) | **Correction to the task brief's framing:** EOD close is *not* itself a "large batch review/completion" operation — its cost is independent of transaction volume by construction. The real "large batch" cost in this app lives in the *read* paths that scan a full business_date (`GET /web/transactions?business_date=`, `GET /api/v1/stats/processors`, report download over a date range). Workload F is designed against those, with `close_day` itself included only as a correctness/race check (§7.6). |
| `GET /web/duplicates` does one `get_by_id` query per pending flag for both the transaction and its match (N+1 shaped) | `web/routes/duplicates_web.py:22-34` | Cheap at today's seed-data volume; worth timing once a catalog has hundreds of pending flags (Workload F). |
| No endpoint exposes `AuditRepository.list_recent` / `CoreAuditRepository.list_recent` | `backend/repositories/transaction_repository.py:184-188`, `backend/repositories/core_audit_repository.py:17-21` — grep across `web/routes/` and `backend/api/routes/` finds no caller | There is **no "audit search" screen or API** in this product today. The closest real analog exposed over HTTP for "search audit/transaction history" is the transaction list/filter screen (`GET /web/transactions`, `GET /api/v1/transactions/`), which is what §4.3/§7.7 test. The gap itself (audit log is written on every mutating action but is unqueryable) is noted here as an observation, not something this document can load-test. |
| Audit trail is split across two separate tables in two separate databases | `AuditLog` (per-catalog, `backend/models/transaction.py:93-103`) vs. `CoreAuditLog` (core DB, `backend/models/core_audit.py`) | `USER_LOGIN`/user-admin actions land in core; `TRANSACTION_CREATED`/`EOD_CLOSED`/etc. land in the catalog DB. Any audit-completeness reconciliation (§10) must query both. |
| App starts one `uvicorn` worker, `reload=True` in the shipped entrypoint | `run_backend.py:8-14` | `reload=True` must be turned off for load testing (it spawns a file-watcher subprocess and restarts on any write, which a report/backup run could trigger). Single-worker also means **all CPU-bound blocking calls above share one thread of control** — this is why bcrypt and report generation matter so much more here than they would behind multiple workers. |

---

## 3. Realistic user journeys (grounded in the actual code, not invented)

All journeys below are transcribed from the real route sequence in
`web/routes/*.py` and `backend/api/routes/*.py`, cross-checked against the
actual call sequences already exercised in `tests/test_web_transaction_entry.py`
and `tests/test_stats_api.py` (per the task brief's own instruction to read
those rather than guess payload shapes). Seeded accounts (`database/seed.py`,
also `tests/conftest.py:SEEDED_USERS`): `admin/admin` (administrator),
`supervisor1/super` (supervisor), `cashier1/cash1` + `cashier2/cash2` (cashier).
Only two cashier accounts are seeded by default — Phase 3 setup will need to
seed additional synthetic cashier accounts to drive realistic concurrency
without every virtual user sharing one login (see §11).

### J1 — Cashier wizard (the core, highest-volume journey)
Web portal, cookie session. Matches
`tests/test_web_transaction_entry.py::test_cashier_can_create_transaction_via_wizard`
step-for-step:

1. `GET /web/login`
2. `POST /web/login` (`username`, `password`) → 303
3. `POST /web/catalog/select` (`code`) → 303 → lands on `/web/transactions/new` for a cashier
4. `GET /web/transactions/new`
5. `GET /web/transactions/new/wizard/customer` (clears any stale draft)
6. `GET /web/transactions/new/wizard/customer-lookup?customer_id=C001` (AJAX/JSON, populates the location dropdown)
7. `POST /web/transactions/new/wizard/customer` (`customer_id`, `location_id`) → 303
8. `GET /web/transactions/new/wizard/bag`
9. `POST /web/transactions/new/wizard/bag` (`bag_number` — 12-16 digit scanned barcode, `amount` — declared total) → 303
10. `GET /web/transactions/new/wizard/wallet`
11. `POST /web/transactions/new/wizard/wallet` (`wallet_id`) → 303
12. `GET /web/transactions/new/wizard/cash`
13. `POST /web/transactions/new/wizard/cash` (8 denomination counts) → 200, shows BALANCED/SHORTAGE/OVERAGE
14. `POST /web/transactions/new/wizard/complete` → 200, commits the `Transaction` row (`TransactionService.create_transaction`, `backend/services/transaction_service.py:49-93`)

= **14 HTTP requests per completed transaction** (9 of them are the "read the
next form" GETs a real cashier's browser issues; a harness may legitimately
skip pure-render GETs it doesn't need to assert on, but the POST sequence
(6/7/9/11/13/14 = 6 stateful POSTs) is the minimum that must be replayed in
order — the server holds wizard state in the session, not in hidden form
fields, so steps cannot be reordered or parallelized within one virtual user).
"Complete Transaction" on the result screen can start an independent
next-wallet transaction on the same bag via
`GET/POST /web/transactions/new/wizard/wallet/next` — a real and common
shift pattern (multiple wallets per bag) worth including as a variant.

API-only equivalent (used for Workload B where wizard/HTML overhead should be
excluded so the measurement isolates business-logic + DB cost): `POST /api/v1/auth/login`
→ `POST /api/v1/transactions/` (`backend/api/routes/transactions.py:12-24`,
schema `TransactionCreate` in `backend/schemas/transaction.py:24-31`) with header
`X-Catalog: <code>`.

### J2 — Supervisor/admin: EOD close and reopen
1. Login (web or API) as `supervisor1`/`admin`, select catalog
2. `GET /web/eod` (lists last 60 days — `EODService.list_closures`, `web/routes/eod_web.py:14-32`)
3. `POST /web/eod/close` (`business_date`) → `EODService.close_day` (`backend/services/eod_service.py:23-32`) — API equivalent `POST /api/v1/eod/close`
4. `GET /web/eod` (verify)
5. `POST /web/eod/reopen` (`business_date`, `reason`) — admin only — API equivalent `POST /api/v1/eod/reopen`

### J3 — Supervisor: transaction review, search, and correction
1. `GET /web/transactions?date_from=&date_to=&customer_id=&location_id=` — the closest real analog to "search history" (see §2 audit-search gap)
2. `GET /web/transactions/{id}` (detail — loads corrections + original in separate queries, `web/routes/transactions_web.py:146-171`)
3. `GET /web/transactions/{id}/correct`
4. `POST /web/transactions/{id}/correct` (denominations + mandatory `reason`) → append-only correction (`TransactionService.correct_transaction`, `backend/services/transaction_service.py:197-257`)

### J4 — Reports export (closest analog to "receipt/label" generation)
1. `GET /web/reports`
2. `GET /web/reports/download?format=xlsx&date_from=&date_to=&customer_id=&location_id=` — builds an in-memory workbook synchronously (`reports/report_engine.py:140-203`), writes to a temp file, streams it back, deletes it via `BackgroundTask` — capped at 5000 rows per `_fetch_enriched_transactions` (`web/routes/reports_web.py:31-42`)
3. Same with `format=csv` (pandas `to_csv`, `report_engine.py:206-210`)

### J5 — Duplicate-flag review
1. `GET /web/duplicates` (pending flags, N+1-shaped per-flag lookups, `web/routes/duplicates_web.py:15-38`)
2. `POST /web/duplicates/{flag_id}/review` (`status`, `notes`)

### J6 — Notifications triage
1. `GET /web/notifications` (open, limit 200)
2. `POST /web/notifications/{id}/resolve`

### J7 — Stats / dashboard glance
1. `GET /web/dashboard`
2. `GET /web/stats?date_from=&date_to=` / API `GET /api/v1/stats/processors` and `.../processors/summary` (`backend/api/routes/stats.py`, shapes matching `tests/test_stats_api.py`)

### N/A — journeys the task brief anticipated that do not exist in this repo
Reprint flow, PDF/"3550" combined receipt generation, label generation,
generated-file history, CSV **import**. Confirmed absent by Agent 1's full-tree
search (`00_baseline.md` §2) and independently reconfirmed while reading
`web/routes/*.py` and `backend/api/routes/*.py` end-to-end for this document —
no route, template, or service touches any of these concepts. **No workload
below targets them; they are marked N/A wherever the brief's original
requirement list mentions them, not silently dropped.**

---

## 4. Capacity thresholds and the concurrency assumption

> **ASSUMPTION — needs business confirmation before any load test's results are
> used for a go/no-go decision.** Brink's Nexus is a cash-in-transit back-office
> tool, not a public or high-traffic consumer system. This plan assumes a
> **peak concurrency in the tens of simultaneous authenticated users, not
> thousands**: roughly 20-40 cashiers mid-wizard at once per catalog during a
> shift changeover, plus 5-10 supervisors/admins concurrently reviewing,
> closing EOD, or pulling reports — call it **~25-50 concurrent active sessions
> system-wide** as a realistic peak, possibly multiplied by however many
> physical sites share one deployment (unknown — to be confirmed by the
> business; this document does not know the real site count or shift overlap
> pattern). This is exactly why Workload A's top tier is 50 concurrent users
> and why Workload D's spike target (§7.4) is framed as "≥2× the *assumed*
> peak," not a claim that 100+ concurrent users is expected in production.

If the real number is materially different (e.g., a single Nexus deployment
serving dozens of sites at once), every threshold, workload tier, and
acceptance-criteria interpretation below needs re-scoping before the numbers
are treated as authoritative.

---

## 5. Workload profiles

All profiles run against a real spawned `uvicorn` process (`reload=False`,
production-shaped middleware stack as in `backend/main.py`), not ASGI-in-process.
Every profile below states target concurrency as **concurrent virtual users
(VUs)**, each VU executing one journey from §3 sequentially with realistic
inter-step think-time (~0.5-2s between wizard steps, matching how long it
actually takes a person to scan a bag or key in a count) unless the profile
says otherwise. Every numeric result reported from any profile **must be
captioned with the sandbox's hardware spec** (4 vCPU / 15 GiB RAM / Python
3.11.15, containerized, not representative of target production hardware —
Agent 1's `00_baseline.md` §4) so nobody downstream mistakes a sandbox number
for a production capacity guarantee.

### A — Baseline load
**Goal:** establish steady-state latency/throughput at low, realistic
concurrency, and find the knee of the curve before it matters.

- Concurrency tiers: **1, 5, 10, 25, 50** VUs, each running J1 (cashier
  wizard) as the primary mix, plus a dedicated **login-only** sub-run at each
  tier (isolates the bcrypt cost from §2 from everything else).
- Each tier run **5 times**, discard the first as warm-up (SQLite page cache,
  Python import/JIT-adjacent warm-up, connection-pool fill), report
  median + IQR across the remaining 4 — this is the "repeated enough to
  separate signal from noise" requirement; 5 reps is a floor, not a ceiling —
  widen it if IQR is large relative to the median.
- Duration per rep: fixed wall-clock window (e.g., 3 minutes) rather than
  fixed request count, so tiers are comparable.
- Run against **one catalog at a time first** (isolates SQLite single-writer
  contention from cross-catalog effects), then once more spread evenly across
  all 4 catalogs at the 50-VU tier only, to see whether the 4-engine/4-file
  split actually parallelizes as hoped or whether OS/process-level limits
  dominate.

### B — Transaction-creation ramp (volume, not concurrency, as the independent variable)
**Goal:** find how total attempted volume interacts with concurrency —
does the system degrade gracefully as attempts pile up, or fall over past a
cliff?

- Attempt counts: **500, 1000, 3000, 5000** transaction-create attempts
  (J1's API-only equivalent — `POST /api/v1/transactions/` — to isolate
  business-logic/DB cost from HTML rendering).
- Concurrency: **5, 10, 25, 50** VUs.
- Rigorous option: full 4×4 cross product (16 runs). Time-boxed pragmatic
  option: the matched diagonal (500@5, 1000@10, 3000@25, 5000@50) plus 2-3
  off-diagonal spot checks (e.g., 5000@5 to see whether low concurrency with
  huge volume behaves differently from high concurrency with the same
  volume) — whoever implements Phase 3 should pick based on available time,
  but the diagonal-only set is the **minimum** that still answers the
  question; a single point (e.g., only 5000@50) does not.
- Each attempt's outcome must be classified per §6 (business success / valid
  rejection / unexpected failure) — a "failed" attempt because a bag number
  collided with another VU's randomly generated one is a valid rejection
  category to design out of the generator, not a defect to report.
- Bag numbers, wallet IDs generated the same way the test suite does
  (`tests/test_web_transaction_entry.py:_random_bag_number`, 13-digit
  "310"-prefixed, and `WALLET-{uuid4 hex[:8]}`) so results reflect the real
  validation regex (`_validate_bag_number`, `web/routes/transaction_entry_web.py:72-76`).

### C — Realistic workflow chain
**Goal:** exercise the actual mix of what a shift looks like, not a single
journey in isolation, for a sustained period.

Weighted mix per VU-minute (proposed starting weights — tune against whatever
real usage data the business can share, another item for business
confirmation):

| Journey | Weight | Rationale |
|---|---|---|
| J1 — cashier wizard (create) | 65% | Highest-volume real activity |
| J3 — search/review/correct | 15% | Supervisors checking work throughout the shift |
| J2 — EOD close/reopen | 5% | Once or twice per business_date per catalog, not continuous |
| J4 — report export | 8% | Periodic, but the heaviest single request when it happens |
| J5 — duplicate review | 4% | Reactive, triggered by J1's own output |
| J6 — notifications | 3% | Reactive, same as above |

Explicitly walks the brief's named steps: **login → search/open EOD
equivalent (J2's `GET /web/eod`) → scan bag → create/update transaction (J1) →
review (J3) → complete where permitted (EOD close, J2, gated by role) →
generate the CSV/Excel report as the closest analog to "receipt/label" (J4) →
search audit history (closest real analog: J3's transaction search, per the
§2/§3 gap note — there is no separate audit-search screen) → reprint has no
analog in this codebase, omitted / N/A** as instructed.

Run at the assumed peak concurrency (§4, ~25-50 VUs) for a sustained window
(60-90 minutes) — long enough to see queueing effects that a 3-minute baseline
run would miss, short of full soak duration (Profile E).

### D — Spike test
**Goal:** confirm the system survives a sudden burst without corrupting data,
even if latency degrades during the spike itself.

- Baseline: hold at the assumed peak (~25-50 VUs, §4) for a stabilization
  period (5-10 min).
- Spike: within under a minute, jump to **≥2× the assumed peak** (≥100 VUs)
  running J1 predominantly (the highest-volume, most write-heavy journey —
  the scenario most likely to actually happen, e.g. every cashier station
  starting a shift within the same few minutes).
- Hold the spike for 5-10 minutes, then drop back to baseline concurrency.
- Specifically watch: does the DB connection pool (§2, unconfigured, SQLAlchemy
  default) recover once load drops, or do connections leak/stay checked out
  (the exact `SAWarning: garbage collector is trying to clean up
  non-checked-in connection` pattern Agent 1 already found in the pytest
  suite, `00_baseline.md` §3, is precisely the leak class a spike is designed
  to surface under real concurrency rather than in a single-threaded test
  run); does latency return to baseline after the spike ends, or does the
  system stay degraded (indicating a resource that isn't released cleanly).

### E — Soak / endurance test
**Goal:** catch slow leaks (memory, DB connections, temp files, open FDs,
SQLite WAL/journal growth) that only show up over sustained runtime, not in
any short run above.

- Initial target: **2-4 hours** at sustained moderate load (Profile C's mix,
  at a concurrency comfortably below peak — e.g. 10-15 VUs — since a soak
  test's point is duration, not stress).
- Stretch goal (explicitly optional, not required for Phase 1 sign-off):
  **overnight (8-12h)** variant, same mix, to catch anything with a longer
  time constant than 4 hours (log file growth, SQLite database file
  fragmentation, the nightly `AsyncIOScheduler` EOD auto-close job actually
  firing mid-run and interacting with concurrent traffic — `backend/main.py:45-49`,
  `backend/services/eod_scheduler.py`).
- Sample resource metrics (§6) at a fixed interval (e.g., every 60s) for the
  whole run so a slow trend is visible in the time series, not just start/end
  snapshots.
- Report generation (J4) should appear repeatedly across the soak window
  (temp file cleanup via `BackgroundTask(os.unlink, ...)`,
  `web/routes/reports_web.py:100` — confirm no temp-file accumulation if a
  download is ever interrupted mid-stream).

### F — Heavy operations (real features only)
**Goal:** find the cost of the app's genuinely expensive real operations,
using real seeded data volumes, not invented ones.

1. **EOD large-batch review** (corrected framing, see §2): seed one
   business_date in one catalog with a large transaction volume (e.g.
   5,000-20,000 rows across the seeded customers/locations) and measure:
   - `POST /api/v1/eod/close` itself (expected: fast, O(1) by construction —
     confirm this empirically rather than assuming the code comment is right)
   - `GET /web/transactions?business_date=...` and `GET /api/v1/transactions/?business_date=...` (the actual per-row cost — 3x `selectinload` per row, `backend/repositories/transaction_repository.py:126-172`)
   - `GET /api/v1/stats/processors?business_date=...` (aggregation cost, `backend/services/stats_service.py`)
   - Confirm `EODService.is_day_closed` correctly blocks new J1 completions
     racing against the close (`backend/services/transaction_service.py:50-52`)
     under real concurrency, not just in the single-threaded pytest case
     already covered by `tests/test_web_transaction_entry.py::test_transaction_creation_blocked_when_day_closed`.
2. **Excel/CSV report generation under concurrency**: multiple concurrent
   `GET /web/reports/download` requests at the 5000-row cap, both formats,
   while a light background trickle of J1/J7 traffic runs — specifically
   measure whether `/health` or other unrelated requests' latency spikes
   *during* a report build (direct test of the synchronous-openpyxl
   head-of-line-blocking risk flagged in §2).
3. **Audit/history search on a large seeded dataset**: seed one catalog with
   a large historical transaction volume (e.g. 50,000-200,000 rows spread
   over many business_dates) and measure `GET /web/transactions` /
   `GET /api/v1/transactions/` filtered by date range, customer, and location
   at that volume (the real analog for "audit/history search" per the §2/§3
   gap — there is no dedicated audit-log search endpoint to test instead).
4. **Hardware-report-parsing path**: `hardware/report_parser.py::parse_report`
   is pure-CPU regex/text parsing, already correctly run inside
   `asyncio.to_thread` when invoked (`web/routes/transaction_entry_web.py:441-452`),
   and `COUNTER_MODE` defaults to `"none"` in this sandbox (no real serial
   device — `hardware/config.py:7`). Since there's nothing to connect to,
   this is a **micro-benchmark, not an HTTP load scenario**: feed
   `parse_report()` a representative captured report text (reuse fixtures
   from `tests/test_c1_report_parser.py`) in a tight loop and under
   concurrent `asyncio.to_thread` calls, to confirm the per-parse CPU cost is
   negligible and that offloading it to a thread is in fact keeping it off
   the event loop under concurrent load. Lowest priority in Profile F since
   it's already off the hot path by design.

**Explicitly dropped as not applicable** (per the task's own scope note and
independently reconfirmed in §3): PDF/"3550" receipt generation, label
generation, CSV **import**, reprint flow, generated-file history. None of
these exist anywhere in this codebase — there is no code path to load-test,
and inventing one would test something Brink's Nexus doesn't do.

---

## 6. What every run must record

Every workload run (A-F) records the same metric set, so results are
comparable across profiles and over time:

**Request/response accounting**
- Attempted requests (total, and per endpoint/journey step)
- Responses grouped by HTTP status code (per endpoint)
- Outcomes classified into exactly three buckets, per request:
  - **Business success** — 2xx *and* independently confirmed to have produced
    the expected DB effect (e.g., a `POST /wizard/complete` that returned 200
    but whose `transaction_id` cannot be found in the DB afterward is **not**
    a business success, it's a missing record — see below)
  - **Valid rejection** — an expected 4xx from real business rules (400
    validation, 404 not found, 409 EOD-already-closed/day-closed conflict,
    401/403 auth) — these are not failures of the system, they're the system
    working correctly under adverse input, and must be reported separately
    from unexpected failures so the two are never conflated
  - **Unexpected failure** — any 5xx, connection reset/refused, client-side
    timeout, or a 2xx that fails the DB-effect check above

**Data-integrity accounting** (cross-checked against the DB after each run,
coordinating with Agent 4's checker — see §10)
- Committed DB records (count of rows actually inserted, per table, per run)
- Unique references (distinct `transaction_id` count — always unique by
  construction, UUID PK; distinct `bag_number` count, reported *alongside*
  total transaction count since `bag_number` is intentionally non-unique in
  this schema — a low distinct-count relative to total is not automatically a
  bug, see §10)
- Duplicate bags found (rows created in `duplicate_flags` during the run)
- Missing records (harness reported a business-success `transaction_id` that
  does not exist in the DB — should always be zero; nonzero is a critical
  finding)
- Missing audit events (a business-success transaction/EOD-close/reopen/
  correction/login with no corresponding `audit_log`/`core_audit_log` row —
  see §10 for the exact 1:1 mapping)
- Orphan records (`denominations` rows with no parent `transactions` row;
  `duplicate_flags` rows pointing at a nonexistent `transaction_id`)
- Reconciliation differences (`transactions.total_value` vs.
  `sum(denominations.value)` per transaction; see §10 for the full list)

**Throughput and latency**
- RPS (raw HTTP request rate)
- TPS (business-successful transaction-creates per second — distinct from RPS
  since one J1 completion is 6 stateful POSTs, only one of which is the
  actual commit)
- Latency p50/p90/p95/p99/max, both **overall** and **broken out per
  endpoint** (a single blended p95 across a 14-step wizard journey hides
  exactly the kind of single-slow-step problem, e.g. the report-download
  endpoint, that this plan exists to find)

**Resource accounting** (sampled at a fixed interval throughout the run, not
just at start/end — critical for Profile E)
- CPU and memory of the `uvicorn` server process
- Disk usage/growth (SQLite `.db`/`-wal`/`-shm` file sizes per catalog, temp
  report files, log files)
- DB connection-pool usage — since no `pool_size`/`max_overflow` is
  configured (§2), this needs either (a) a temporary, load-test-only debug
  endpoint that reports `engine.pool.checkedout()`/`.overflow()` per engine
  (never enabled outside a load-test run, never in production), or (b)
  inference from `TimeoutError`/pool-exhaustion exceptions surfacing as 5xx
  in the error log. Which approach to build is a Phase 3 implementation
  decision; this document flags that the pool is otherwise a black box to an
  external load generator and *some* instrumentation choice has to be made
  before Workload A/B numbers at the higher concurrency tiers can be trusted.

**Errors grouped by endpoint + root cause** — every unexpected-failure and
valid-rejection request bucketed by `(endpoint path, HTTP status, root-cause
category)`, where root-cause categories are named up front (validation error,
day-closed conflict, duplicate-bag soft-flag, auth failure, DB pool
exhaustion/timeout, connection refused, unhandled 5xx, other) so error
patterns are legible across hundreds or thousands of individual failures
instead of requiring someone to read every stack trace by hand.

---

## 7. Acceptance criteria

Stated exactly as given, with no substitutions made for sandbox convenience:

| Criterion | Threshold |
|---|---|
| Unexpected transaction loss | Zero |
| Duplicate financial effects | Zero (the app's own *soft* duplicate-flagging, §2/§10, is expected behavior, not a violation of this criterion — the violation would be e.g. a double-click or client retry producing two independently-committed rows for what was one physical bag count with no flag and no way to tell them apart) |
| Missing audit events | Zero |
| Orphan records | Zero |
| Unbalanced completed transactions | Zero (meaning: no transaction where `sum(denominations.value) != total_value` — an internal write-consistency check, distinct from `BalanceStatus.not_balanced`, which is a legitimate, expected business outcome when counted cash ≠ declared cash) |
| Unexpected 5xx at target load | Zero |
| p95 latency, normal API requests | < 1s |
| p99 latency, transaction commit (`POST .../wizard/complete` or `POST /api/v1/transactions/`) | < 2s |

**Explicit statement required by the brief, restated here so it isn't lost:**
if this sandbox's hardware (4 vCPU / 15 GiB RAM, containerized, per Agent 1's
`00_baseline.md` §4) cannot sustain proving these thresholds at the assumed
concurrency (§4), **that is a fact to report, not a threshold to quietly
lower.** A run that shows, say, p95 = 1.4s at 50 concurrent users on this
sandbox must be written up as exactly that — "threshold not met on sandbox
hardware at this concurrency" — with the sandbox caveat attached, and left for
a real go/no-go decision on production-equivalent hardware. Nothing in this
plan authorizes rewriting the acceptance numbers to make a sandbox run look
like a pass.

---

## 8. Tool choice

**Proposed: a small `asyncio` + `httpx` harness, written directly against this
repo, no new load-testing framework.**

Justification:
- `httpx==0.27.2` is already a pinned dependency (`requirements.txt`) and is
  exactly what `tests/conftest.py` already uses for every functional test —
  the load harness reuses the same request-building/assertion vocabulary the
  team already has, just pointed at a real socket (`AsyncClient(base_url=...)`
  with no `transport=ASGITransport(...)`) instead of in-process.
- `httpx.AsyncClient` natively supports both auth styles this app actually
  uses — cookie-jar sessions for the web wizard (J1-J7) and `Authorization:
  Bearer` + `X-Catalog` header for the JSON API — with no adapter layer.
- The wizard is genuinely stateful and strictly sequential per virtual user
  (session-held draft, §3) — a hand-rolled `async def run_cashier_vu()`
  coroutine expresses that directly; `asyncio.gather`/a semaphore-bounded
  worker pool gives concurrency control without needing a distributed
  master/worker architecture, which is overkill at the tens-to-low-hundreds
  VU scale this plan assumes (§4).
- Zero new heavyweight dependency to install, review, and maintain for a
  target this modest. `locust` is **not** currently installed or referenced
  anywhere in the repo.
- Resource sampling (§6) needs *something* to read CPU/mem of the server
  process — either the small `psutil` package (new, tiny, well-maintained,
  one `pip install` in the Phase 3 venv, never in the app's own
  `requirements.txt`) or a zero-dependency `/proc/<pid>/status` +
  `os.statvfs` reader on this Linux target. Either is a Phase 3 implementation
  detail, not a Phase 1 decision to lock in now.

**Escalation path (not recommended now, named for completeness):** if a later
phase needs a live web dashboard for non-engineers to watch a run, distributed
multi-machine load generation, or concurrency targets grow past the low
hundreds, **Locust** is the natural next tool (Python-native, same language as
the app, well-documented FastAPI usage patterns). None of those conditions
hold against the ~25-50 VU assumption in §4, so adding it now would be
over-engineering for this system's actual scale.

---

## 9. Machine-readable run-summary schema

One JSON file per run, plus one Markdown report per run (§9.2). Proposed
schema (field names and types — not yet implemented):

```json
{
  "run_id": "string (uuid or timestamp-based, unique per run)",
  "workload_profile": "A | B | C | D | E | F",
  "profile_variant": "string, e.g. 'A-50vu-rep3', 'B-3000attempts-25vu', 'F-eod-largebatch'",
  "started_at": "ISO 8601 timestamp, UTC",
  "finished_at": "ISO 8601 timestamp, UTC",
  "duration_seconds": "number",
  "environment": {
    "hardware_note": "string — MUST always state this is/is not the sandbox (4 vCPU/15GiB/containerized) vs. production-equivalent hardware",
    "cpu_count": "integer",
    "memory_total_mb": "integer",
    "python_version": "string",
    "app_commit": "string (git SHA)",
    "db_backend": "sqlite | postgresql",
    "uvicorn_workers": "integer"
  },
  "concurrency": {
    "target_vus": "integer",
    "catalogs_used": ["vms", "dayshift", "complete", "esnf"]
  },
  "requests": {
    "attempted_total": "integer",
    "by_status_code": {"200": "integer", "201": "integer", "400": "integer", "401": "integer", "403": "integer", "404": "integer", "409": "integer", "5xx": "integer", "...": "..."},
    "business_success_total": "integer",
    "valid_rejection_total": "integer",
    "unexpected_failure_total": "integer"
  },
  "throughput": {
    "rps_mean": "number",
    "tps_mean": "number"
  },
  "latency_ms": {
    "overall": {"p50": "number", "p90": "number", "p95": "number", "p99": "number", "max": "number"},
    "by_endpoint": {
      "<method> <path>": {"p50": "number", "p90": "number", "p95": "number", "p99": "number", "max": "number", "count": "integer"}
    }
  },
  "resources": {
    "sampled_every_seconds": "integer",
    "cpu_percent": {"mean": "number", "max": "number"},
    "memory_mb": {"mean": "number", "max": "number", "start": "number", "end": "number"},
    "disk": {
      "db_files_mb_start": {"core": "number", "vms": "number", "dayshift": "number", "complete": "number", "esnf": "number"},
      "db_files_mb_end": {"core": "number", "vms": "number", "dayshift": "number", "complete": "number", "esnf": "number"},
      "temp_report_files_leaked": "integer"
    },
    "db_pool": {
      "instrumentation_method": "debug_endpoint | error_inference | not_measured",
      "max_checked_out_observed": "integer | null",
      "pool_timeout_errors": "integer"
    }
  },
  "data_integrity": {
    "committed_records_by_table": {"transactions": "integer", "denominations": "integer", "duplicate_flags": "integer", "notifications": "integer", "audit_log": "integer", "core_audit_log": "integer", "eod_closures": "integer"},
    "unique_transaction_ids": "integer",
    "distinct_bag_numbers": "integer",
    "duplicate_bags_found": "integer",
    "missing_records": "integer",
    "missing_audit_events": "integer",
    "orphan_records": "integer",
    "reconciliation_differences": "integer"
  },
  "errors_by_endpoint_and_cause": [
    {"endpoint": "string", "status_code": "integer", "root_cause": "string (from the named category list in §6)", "count": "integer", "sample_detail": "string"}
  ],
  "acceptance_criteria": {
    "zero_unexpected_transaction_loss": "pass | fail",
    "zero_duplicate_financial_effects": "pass | fail",
    "zero_missing_audit_events": "pass | fail",
    "zero_orphan_records": "pass | fail",
    "zero_unbalanced_completed_transactions": "pass | fail",
    "zero_unexpected_5xx": "pass | fail",
    "p95_normal_api_under_1s": "pass | fail | not_applicable_hardware_limited",
    "p99_transaction_commit_under_2s": "pass | fail | not_applicable_hardware_limited"
  },
  "notes": "free-text — anomalies, deviations from the plan, hardware caveats restated"
}
```

### 9.1 Accompanying Markdown report per run

One `.md` per run (or per profile, with per-rep detail tables), containing:

1. **Header** — run ID, profile/variant, date, who ran it, link to the raw
   JSON summary and raw harness/server logs.
2. **Environment snapshot** — hardware, restated explicitly as sandbox vs.
   production-equivalent (never omit this line).
3. **Summary** — one paragraph, plain language: what was run, headline
   numbers (RPS/TPS, p95/p99, error rate), pass/fail against §7 at a glance.
4. **Request/response table** — status-code breakdown, business
   success/valid rejection/unexpected failure counts.
5. **Latency table** — per endpoint, p50/p90/p95/p99/max, request count.
6. **Resource usage** — CPU/mem/disk time series summary (chart or table),
   DB-pool observations.
7. **Data-integrity results** — the §6/§10 reconciliation output, explicit
   zero-or-not for each check.
8. **Errors by endpoint + root cause** — table, largest buckets first.
9. **Acceptance-criteria verdict table** — each of the 8 criteria in §7,
   actual value vs. threshold, pass/fail/not-applicable-hardware-limited.
10. **Anomalies / notable findings** — narrative: anything that looked wrong,
    any deviation from the plan (e.g., a rep discarded, a run cut short),
    explicitly including any hardware-limited miss per §7's requirement that
    this be reported as fact, not smoothed over.

---

## 10. Post-run database reconciliation checks (load-test side)

These are the checks *this* load-test plan needs to score its own acceptance
criteria (§7) and populate `data_integrity` (§9). **Agent 4 owns the
authoritative, general-purpose integrity-checker design** in
`docs/production_readiness/04_postgresql_and_reconciliation.md` — the list
below is the load-test-specific subset that must run after every workload rep,
and should call into (or at minimum stay consistent with) Agent 4's checker
rather than growing a second, divergent implementation.

Specific to this schema (not generic placeholders):

1. **`bag_number` duplicates** — `GROUP BY customer_id, business_date,
   bag_number HAVING COUNT(*) > 1` across `transactions`. Report this count
   *and* separately report how many of those groups have a corresponding
   `duplicate_flags` row — the app's own detection is scoped to
   **same-user, same-business_date** (`duplicate_detection_service.py:37-40`),
   so a same-bag-number collision across two *different* cashiers on the same
   day will show up in this raw count but will **not** have been flagged by
   the app. That gap is expected given the current design, not a load-test
   defect — but it must be reported as a fact for Agent 3/5 to weigh in on
   the risk-acceptance decision.
2. **`total_value` vs. `sum(denominations.value)`** — per `transaction_id`,
   `transactions.total_value` must equal the sum of its child `denominations.value`
   rows. Any mismatch is a write-consistency bug (partial write, race between
   the two flushes in `TransactionRepository.create`,
   `backend/repositories/transaction_repository.py:47-61`), not a business
   discrepancy (that's what `balance_status` already captures separately via
   `expected_total`).
3. **EOD-closure vs. transaction totals** — the schema has **no monetary
   total on `EODClosure` at all** (`backend/models/eod.py:15-34` — only
   `business_date`/`status`/who/when). There is nothing named "closure
   totals" to reconcile against a sum. The meaningful check here instead is a
   **concurrency-race check**: for every `business_date` closed during a run,
   confirm no `transactions` row for that same `(catalog, business_date)`
   has a `created_at` timestamp after the corresponding `eod_closures.closed_at`
   — i.e., that `EODService.is_day_closed`'s gate (`transaction_service.py:50-52`)
   actually held under real concurrent writes racing the close, not just in
   the single-threaded pytest case already covered by
   `test_transaction_creation_blocked_when_day_closed`.
4. **Audit-log completeness** — exact 1:1 expected mapping, split across the
   two audit tables (§2):
   - Catalog `audit_log`: 1× `TRANSACTION_CREATED` per committed transaction,
     1× `TRANSACTION_CORRECTED` per correction, 1× `EOD_CLOSED` per close, 1×
     `EOD_REOPENED` per reopen, 1× `TRANSACTION_DAY_TRANSFERRED` per transfer
     (`backend/services/*.py`, `AuditRepository.log` call sites)
   - Core `core_audit_log`: 1× `USER_LOGIN` per successful login, plus
     `USER_CREATED`/`USER_UPDATED`/`USER_ACTIVATED`/`USER_DEACTIVATED`/
     `USER_DELETED`/`DATABASE_BACKUP_CREATED` for any admin actions a
     workload exercises (`AuthService`, `web/routes/admin_web.py`)
   - Any business-success mutating action with no matching audit row is a
     `missing_audit_events` hit (§6/§9).
5. **Orphan records** — `denominations` rows with no matching
   `transactions.transaction_id` (should be structurally impossible given the
   `cascade="all, delete-orphan"` relationship, `transaction.py:74-76`, but
   verify empirically rather than trusting the ORM declaration under load);
   `duplicate_flags` rows whose `transaction_id` or
   `duplicate_of_transaction_id` no longer resolves to a row.
6. **Unbalanced completed transactions** (§7) — `balance_status ==
   BALANCED` rows where `total_value != sum(denominations.value)` (check #2
   above, filtered to the BALANCED subset specifically, since that's the
   literal wording of the acceptance criterion).

---

## 11. Test-data / environment setup notes (supporting §5, not a new section of the brief)

- Only 2 cashier accounts (`cashier1`, `cashier2`) and 2 non-cashier accounts
  (`admin`, `supervisor1`) are seeded by default (`database/seed.py:82-87`).
  Realistic concurrency (§4, 20-40 concurrent cashiers) requires seeding
  additional synthetic cashier/supervisor accounts before Workload A/B/C/D
  can be run — a Phase 3 setup step, not a Phase 1 design gap, but flagged
  here so it isn't missed when implementation starts. Login is not
  session-limited (no observed single-session-per-user enforcement in
  `AuthService`/`get_current_user`), so reusing few accounts across many VUs
  is *possible* but not representative of real per-cashier attribution in the
  audit trail — prefer seeding one account per intended VU.
- Workload F's large-batch scenarios (§7.6) need purpose-built seed data
  (5,000-200,000 transaction rows) generated directly against the catalog
  DB/ORM, not via HTTP (seeding 200,000 rows through the wizard's 14-request
  flow would itself take longer than the test) — a bulk-insert seeding script
  is a Phase 3 implementation task, separate from the load-generation harness
  itself.
- `COUNTER_MODE` must stay `"none"` (its default, `hardware/config.py:7`)
  unless a specific test explicitly wants the mock/simulate counter path —
  no real hardware exists in this sandbox, and the wizard's cash-count step
  works perfectly well with directly-posted denomination counts (exactly how
  `tests/test_web_transaction_entry.py` already does it), so J1 does not
  depend on the hardware endpoint at all.
- `SessionMiddleware`'s `secret_key` and JWT `secret_key` both currently
  default to the hardcoded value in `backend/core/config.py:19` (a Phase 0/3
  finding for Agent 3, not this document) — load-test runs should set a real
  `SECRET_KEY` via `.env`/environment variable regardless, both to test the
  intended production configuration path and to avoid the load-test's own
  traffic touching a value that should never be reused anywhere real.

---

## 12. Summary of open questions for the business / other agents

1. **Concurrency assumption (§4)** — is "tens of concurrent cashiers/
   supervisors per shift, one deployment" actually right, or does one Nexus
   deployment serve multiple sites/shifts concurrently at a materially higher
   number? This is the single input every other number in this document
   scales from.
2. **DB connection-pool instrumentation (§6)** — add a temporary debug
   endpoint for pool stats during load-test runs (needs an explicit "never
   ships to production" guard), or infer pool exhaustion only indirectly from
   error patterns? A Phase 3 decision this document can't make unilaterally.
3. **PostgreSQL vs. SQLite** — every number this plan produces on the default
   SQLite backend is backend-specific (single-writer file, no real network
   round-trip). Coordinate with Agent 4 on running at least Profile A and one
   Profile F scenario against PostgreSQL once that track stands up a server,
   so there's a same-workload comparison point.
4. **Weighted mix in Profile C (§5)** — the 65/15/5/8/4/3 split is a
   plausible starting guess grounded in what each journey is *for*, not
   measured real usage. If any real usage logs/analytics exist, they should
   replace this guess before Profile C's results are treated as
   representative.

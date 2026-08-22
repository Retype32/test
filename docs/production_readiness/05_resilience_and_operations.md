# 05 — Resilience, Recovery, and Operations (Phase 1)

Agent 5. Read-only/design phase: nothing below was executed as a live failure
injection. Every "confirmed" statement is grounded in a specific file/line
read during this review; every claim that would need a live run to verify is
labeled "needs live test" rather than asserted as fact.

Scope correction honored (per the task brief and `00_baseline.md` §2): there
is no PDF/receipt engine in this repo. The real analogs used throughout this
document are (a) the G+D BPS C1 hardware banknote-counter report parser
(`hardware/`), (b) the nightly EOD `apscheduler` job
(`backend/services/eod_scheduler.py`), (c) Excel/CSV report generation
(`reports/report_engine.py`, `web/routes/reports_web.py`), and (d) the SQLite
backup capability (`backend/services/backup_service.py`).

Transaction-boundary/idempotency-key detail is Agent 1's territory and is
only referenced here, not re-derived. Load-test/pool-saturation execution is
Agent 2's territory; this document designs the pool-exhaustion scenario but
does not run it.

---

## 1. Current state summary — logging, monitoring, health checks, correlation IDs

All four are effectively absent. Confirmed by full-repo search, not assumed:

- **Structured logging: none.** `grep -rl "logging\|logger"` across
  `backend/`, `web/`, `hardware/`, `reports/` returns exactly one file,
  `hardware/isa_log.py` — and that hit is the word "logging" in a docstring
  describing *ISA's own* device log file, not Python's `logging` module.
  There is no `import logging` anywhere in the application. All
  server-side diagnostic output is bare `print()` calls, and only in a few
  places: `hardware/mock.py`, `hardware/gd_c1_report.py`,
  `hardware/isa_log.py`, `hardware/__init__.py` (open/close of the shared
  counter). `backend/`, `web/`, and `reports/` have **zero** print/log
  statements of their own — a request that fails inside a route handler,
  service, or repository produces no application-level trace at all beyond
  whatever uvicorn's own ASGI error path emits to stderr for an unhandled
  exception. Nothing is written to a file; output only exists in whatever
  console window the process happens to be running in
  (`nexus_setup_manager.py`'s `action_start_nexus` launches Nexus in a new,
  unlogged console window on Windows — closing that window loses the output
  permanently).
- **No secret/PII-in-logs risk from the above, precisely because there is
  next to no logging** — but this cuts both ways: there is also nothing to
  audit for leakage, and nothing to grep when something goes wrong.
- **Health/readiness endpoints: one trivial one.** `backend/main.py:128-130`
  defines `GET /health` returning a hardcoded `{"status": "ok", "app": ...}`
  — it does not touch `core_engine`, any catalog engine, the scheduler, or
  the hardware counter. It will report "ok" even if every database is
  unreachable, the EOD scheduler has crashed, or the counter was never
  connected. There is no `/ready` distinct from `/health` (no way to signal
  "process is up but still draining/starting"), and no `/status` endpoint
  beyond the per-catalog business `GET /api/v1/eod/status` (business-day
  status, unrelated to system health).
- **Correlation / request IDs: none.** `grep -r "correlation\|request_id\|X-Request-ID\|Request-Id"` across the whole repo (`.py` and `.html`) returns zero
  hits. No middleware generates or propagates one; nothing is available to
  tie a client-reported failure to a specific server-side request.
- **Metrics/alerting integrations: none.** `grep` for `smtp`, `sendmail`,
  `webhook`, `slack`, `pagerduty`, `opsgenie`, `sentry`, `datadog`,
  `prometheus`, `statsd` across the whole tree returns zero hits. The
  in-app `Notification` model (`backend/models/notification.py`) is a
  **business-domain** table (duplicate transactions, transferred
  transactions, reopened EOD days) surfaced only in the web UI's
  notification list — it has no operational/system-health event types and
  nothing pages a human. There is no channel today by which any failure in
  this document could reach a human without someone actively looking at a
  console window or the database.
- **Retry logic: none.** `grep -r "retry\|tenacity\|backoff"` across
  `backend/`, `web/`, `hardware/`, `reports/` returns zero hits (other than
  the tools/free_port.py CLI naming, unrelated). Nothing in the request path
  retries anything automatically.

## 2. Current startup/shutdown behavior

`backend/main.py:39-63` (`lifespan`), read in full:

1. `await init_databases()` (`backend/core/database.py:92-105`) — for the
   core DB and each of the 4 catalog DBs: `metadata.create_all` (creates any
   missing tables) then stamps the DB at the Alembic `head` revision via
   `alembic.command.stamp` run in a thread. **This raises on failure** (e.g.
   an unreachable Postgres host, once the app is pointed at Postgres per the
   documented production target) and, per the ASGI lifespan protocol, an
   exception during lifespan startup makes uvicorn fail to start the server
   at all — no request is ever served, the process exits. This is standard
   uvicorn/Starlette behavior, not something this repo overrides; the exact
   console output would need a live test to capture, but the "process never
   starts" consequence is a direct reading of the lifespan contract combined
   with `init_databases()` having no try/except of its own.
2. `await _auto_seed()` (`backend/main.py:27-36`) — opens a **manual**
   `CoreSessionLocal()` session (not through `get_core_db`), checks for any
   existing user, and calls `database/seed.py`'s `seed()` if none exist.
   `seed()` itself (`database/seed.py:107-133` and further) also uses manual
   sessions per catalog with an explicit `await session.commit()` at the end
   of each function but **no explicit `except`/`rollback`** — if seeding
   raises partway through, `async with sessionmaker() as session:` still
   calls `session.close()` on the way out, which discards any uncommitted
   work (SQLAlchemy's standard implicit-rollback-on-close), so this is not
   an unsafe pattern, just an undocumented one that bypasses the app's own
   `get_core_db`/`get_catalog_db` convention. Any exception here also fails
   startup exactly like (1).
3. `await asyncio.to_thread(open_shared_counter)` — connects the shared
   hardware counter. Unlike (1) and (2), **failure here does not fail
   startup**: `open_shared_counter()` (`hardware/__init__.py:36-50`) catches
   any connection exception, prints a message, and leaves `_shared = None`,
   letting the app continue with manual cash-count entry. This is a real,
   confirmed asymmetry — a missing/misconfigured hardware counter is
   tolerated at boot, a missing/misconfigured database is not.
4. The APScheduler `AsyncIOScheduler` is created and started with one job,
   `auto_close_all_catalogs`, on a `CronTrigger(hour=0, minute=0)`
   (`backend/main.py:45-49`) — see §Scenario list item on the EOD scheduler
   below.
5. `yield` — app serves requests.
6. On shutdown: `scheduler.shutdown(wait=False)` — **does not wait** for a
   job that happens to be running to finish; `close_shared_counter()`;
   `await core_engine.dispose()` and `await _engine.dispose()` for every
   catalog engine (`backend/main.py:53-62`), added specifically, per the
   inline comment, to avoid SQLAlchemy's GC-warning leak pattern — confirmed
   this actually matters: **`00_baseline.md`'s Phase 0 test run already
   observed `SAWarning: garbage collector is trying to clean up
   non-checked-in connection` across five test files**, i.e. a live
   connection/session leak already exists somewhere in the app's own code
   paths (not in this dispose-on-shutdown code, which is correct — the leak
   is in a request-time session-acquisition path Agent 1 needs to
   root-cause). No shutdown step logs or prints anything — shutdown is
   completely silent even in the one place `print()` is used elsewhere in
   this file's neighborhood.

**Launch mode** (`run_backend.py:5-14`): `uvicorn.run("backend.main:app",
host="127.0.0.1", port=8000, reload=True, log_level="info")`. Three points
worth flagging together: (a) **`reload=True` is hardcoded** — this is a
uvicorn development convenience (file-watcher + auto-restart subprocess) and
is explicitly not recommended for production; there is no separate
production entry point in the repo (no gunicorn/uvicorn-workers config, no
`Procfile`, no systemd unit, no Dockerfile found). (b) **No worker count is
configured** — uvicorn defaults to a single process, single event loop; there
is no multi-process/multi-worker supervision anywhere. (c) **Bound to
`127.0.0.1`** — fine for a reverse-proxy-fronted deployment, but nothing in
the repo shows what fronts it in production. `nexus_setup_manager.py`'s
`action_start_nexus` (`nexus_setup_manager.py:2021-2043`) simply
`subprocess.Popen`s this same command in a new console window with no
supervision, no restart-on-crash, and no health check after launch beyond an
optional "open this URL in a browser" prompt.

## 3. Session/transaction-boundary sweep (grounds the "audit independent of business write" questions below)

Every session-acquisition pattern found by grepping for `CoreSessionLocal(`,
`get_catalog_sessionmaker(`, and `sessionmaker()` across `backend/` and
`database/`:

| Call site | Pattern | Commit/rollback safety |
|---|---|---|
| `backend/core/database.py:56-77` (`get_core_db`/`get_catalog_db`) | FastAPI dependency: `try: yield; commit() / except: rollback(); raise / finally: close()` | The one safe, reusable pattern; every `api/routes/*.py` and `web/routes/*.py` handler that touches the DB goes through this via `CoreDB`/`CatalogDB`/`WebCoreDB`/`WebCatalogDB` (`backend/api/deps.py`, `web/deps.py`) — confirmed no route file acquires a session any other way. |
| `backend/main.py:33` (`_auto_seed`) | Manual `async with CoreSessionLocal()`, no explicit commit inside `_auto_seed` itself (delegates to `seed()`) | Startup-only, not a request path; see §2 above. |
| `backend/services/eod_scheduler.py:21-33` | Manual `async with sessionmaker() as session`, explicit try/except with its own commit/rollback | Correctly duplicates the safe pattern by hand for the one code path that must run outside a request (the cron job) — see the EOD scenario below for what happens when it fires during a DB outage. |
| `database/seed.py:108,138` | Manual `async with sessionmaker() as session`, single `commit()` at the end, no explicit rollback branch | Safe by virtue of `AsyncSession.close()` discarding uncommitted work; startup-only. |

**Conclusion used throughout this document:** every request-time write path
found (`TransactionService`, `EODService`, `NotificationService`,
`DuplicateDetectionService`, all constructed with one shared `db` session
per request) puts its audit-log call (`AuditRepository.log`,
`backend/repositories/transaction_repository.py:179-182`) on the *same*
session as the business write it documents, and that repository method only
`add()`s + `flush()`s — it never commits independently. The commit is a
single `await session.commit()` in `get_core_db`/`get_catalog_db` after the
whole request handler returns. So for every one of those flows, "the audit
write fails independently of the business write" **cannot happen** as
commonly imagined (separate audit service/queue) — confirmed by code, not
assumed. The one **confirmed exception** is `web/routes/admin_web.py:112-146`
(`create_backup`): it calls `backup_service.create_backup()` (a filesystem
operation, no DB session involved) and only afterward writes an audit row
via `CoreAuditRepository` on a *different* session (`core_db`) — filesystem
success and DB-audit success are two independent resources with no
two-phase coordination between them. See Scenario 10 for the concrete
consequence.

---

## Scenario 1 — Process terminates during processing (kill -9 mid-request)

- **Expected user-visible result:** the client's TCP connection is reset;
  no HTTP response is ever produced. This is OS/process-level behavior, not
  something the app code intercepts — confirmed by the absence of any signal
  handler, supervisor, or process-level safety net anywhere in the repo
  (`run_backend.py` is a bare `uvicorn.run`; `nexus_setup_manager.py`'s
  `action_start_nexus`, `nexus_setup_manager.py:2021-2043`, just
  `subprocess.Popen`s it with no monitoring). **Confirmed by reading the
  launch code; the exact client-side symptom would need a live test.**
- **Expected database state:** SQLite's own file-level atomicity (not app
  code) guarantees no partially-written row survives an ungraceful kill —
  either the in-flight transaction was fully committed to disk before the
  kill, or it wasn't and nothing changed. The app-level `finally: close()`
  in `get_core_db`/`get_catalog_db` (`backend/core/database.py:64-65,76-77`)
  never gets to run on `SIGKILL`, but that's fine here precisely because the
  underlying engine guarantees atomicity independent of that cleanup code.
- **Expected audit state:** atomic with the business write, per §3 above —
  either both the business row and its audit row exist, or neither does.
- **Retry safety:** NOT confirmed safe for any state-changing endpoint.
  There is no idempotency-key mechanism anywhere in the schema (confirmed in
  `00_baseline.md` §2: "No idempotency-key support anywhere in the tree").
  `create_transaction`'s only downstream safety net is the soft,
  after-the-fact duplicate-detection heuristic
  (`backend/services/duplicate_detection_service.py:37-71`), which flags a
  suspected duplicate but does **not** block the second insert. Reference
  Agent 1 for the authoritative idempotency analysis.
- **Required alert (proposed, none exists today):** process-liveness
  monitoring — a supervisor (systemd, a Windows service wrapper, or even a
  simple watchdog script) with an automatic restart policy, plus an external
  prober hitting `GET /health` on an interval and paging after N consecutive
  failures. Today there is no supervisor of any kind and `/health` doesn't
  check anything meaningful anyway (§1).
- **Required support information (proposed):** a correlation/request ID
  (doesn't exist, §1) attached to every request and echoed in whatever
  minimal server log exists, so a support engineer can answer "was this
  specific request ever received/committed" instead of guessing from
  timestamps.
- **Recovery procedure (grounded in what exists today):** manually restart
  the process (`python run_backend.py`, or re-run
  `nexus_setup_manager.py`'s "Start Nexus" action) — there is no automated
  restart. On restart, `init_databases()` re-runs `metadata.create_all` +
  Alembic stamp, which is safe/idempotent against an already-initialized
  SQLite file. If data integrity is in doubt, the only available fallback is
  a manual restore from `backups/` (Scenario 12) — no automated
  integrity-check tool exists.
- **Pass/fail evidence needed (Phase 5):** a test that sends a
  state-changing request, kills the server process partway through
  (mid-handler, verified via a deliberate delay/hook), and asserts (a) the
  target row either fully exists with its audit entry or doesn't exist at
  all — never partially, (b) the next process start succeeds cleanly, (c)
  the client observes a connection error, not a hang.

## Scenario 2 — App restarts with requests in progress (planned restart/redeploy)

- **Expected user-visible result:** governed by uvicorn's own graceful
  shutdown (drains in-flight connections up to its default timeout before
  invoking the ASGI lifespan `shutdown` phase) — the app adds nothing of its
  own on top of that. **Confirmed by reading `backend/main.py:51-62`: the
  shutdown block only shuts the scheduler (`wait=False` — explicitly does
  NOT wait for a running job), disconnects the hardware counter, and
  disposes the DB engines — there is no explicit "wait for in-flight HTTP
  requests" logic beyond whatever uvicorn does by default.** The
  `reload=True` flag in `run_backend.py:12` additionally means a routine
  file-change in dev mode triggers this same restart path unexpectedly —
  inappropriate for a production binary but, as written, indistinguishable
  from one in this codebase (no separate prod entry point exists).
- **Expected database state:** identical reasoning to Scenario 1 for any
  request that gets cut off by the shutdown deadline; requests that
  complete within uvicorn's drain window commit normally through
  `get_core_db`/`get_catalog_db`.
- **Expected audit state:** same as Scenario 1 — atomic with the business
  write for every path identified in §3.
- **Retry safety:** same as Scenario 1 — reference Agent 1.
- **Required alert (proposed):** deploy-time readiness gating — since there
  is no `/ready` distinct from `/health` (§1), a load balancer or deploy
  tool has no way to know "this instance is draining, stop sending it new
  work" versus "this instance is healthy." Propose adding a `/ready` that
  goes false as soon as shutdown begins.
- **Required support information (proposed):** a log line (there is none
  today, §2) marking "shutdown initiated" / "shutdown complete" with a
  timestamp, so a deploy can be correlated with any request failures
  reported immediately after.
- **Recovery procedure:** none needed for a clean graceful restart; if the
  process hangs past its expected shutdown window, this degrades to
  Scenario 1 (manual kill + restart).
- **Pass/fail evidence needed (Phase 5):** fire N concurrent state-changing
  requests, send `SIGTERM` partway through, and confirm every one of the N
  requests either received a normal response with a committed row, or a
  clean error — none silently dropped or left half-committed.

## Scenario 3 — DB interrupted during a request (connection dropped / file locked)

- **Expected user-visible result:** **confirmed by code, not just
  inference** — no route handler in `backend/api/routes/*.py` or
  `web/routes/*.py` catches generic DB exceptions (the only broad
  `except Exception` in either directory is the hardware-counter read at
  `web/routes/transaction_entry_web.py:448-451`, unrelated to the DB). An
  unhandled `SQLAlchemyError`/`OperationalError` therefore falls through to
  Starlette's default `ServerErrorMiddleware`, since `backend/main.py`'s
  only custom `@app.exception_handler` (`main.py:111-125`) is registered
  for `StarletteHTTPException` only (401/403/404 get an HTML page for
  `/web/*`; everything else is untouched). **There is no `errors/500.html`
  template** — `web/templates/errors/` contains only `403.html` and
  `404.html` (confirmed by directory listing). The result is Starlette's
  bare `"Internal Server Error"` plain-text 500 for both API and web
  requests, with no correlation ID, no user-facing guidance, and (per §1)
  no server-side log record beyond whatever bare traceback uvicorn happens
  to print to stderr.
- **Expected database state:** the `except Exception: await
  session.rollback(); raise` branch in `get_core_db`/`get_catalog_db`
  (`backend/core/database.py:61-63, 73-75`) does fire and attempts a
  rollback on the same (possibly now-broken) connection. For SQLite this
  reliably discards the local transaction; whether the connection is
  returned to the pool in a reusable state after a mid-transaction failure
  is **not verifiable by static reading — needs a live test.** No partial
  commit is expected given the dependency wraps the entire session
  lifetime of the request in this try/except/finally.
- **Expected audit state:** same session, same rollback — atomic, per §3.
- **Retry safety:** depends on the specific endpoint; reference Agent 1's
  idempotency findings. GET-only endpoints are naturally retry-safe;
  `create_transaction` and friends are not proven so.
- **Required alert (proposed):** a 5xx-rate / exception-rate alert — there
  is no metrics collection of any kind today (§1), so this would require
  adding one from scratch.
- **Required support information (proposed):** the exception should be
  logged server-side with a correlation ID and enough context (route,
  catalog, user) to reproduce; today it is not logged by the application at
  all, only whatever uvicorn's own unhandled-exception path prints.
- **Recovery procedure:** SQLite's `"database is locked"` condition is
  typically write-write contention; **no `busy_timeout` is set anywhere** —
  `backend/core/database.py:10-11`'s `_connect_args` only sets
  `check_same_thread: False`, nothing else — so a lock contention error
  surfaces immediately as an `OperationalError` rather than waiting and
  retrying at the driver level. No app-level recovery exists beyond the
  client resubmitting once the lock clears; there is no documented
  operational runbook for this in `SETUP.md`/`README.md` (grepped, no
  hits).
- **Pass/fail evidence needed (Phase 5):** simulate a DB failure mid-`create_transaction` (e.g. revoke file permissions on the SQLite file, or sever a Postgres connection once that backend is exercised) and confirm (a) the client gets a clean, well-formed 5xx (not a hang), (b) no partial `Transaction`/`Denomination`/`AuditLog` rows exist afterward, (c) the app remains usable for the very next request (pool/connection recovers without a restart).

## Scenario 4 — Response lost after DB commit (client times out after server-side success)

- **Expected user-visible result:** the client observes a timeout or
  connection error despite the server having fully succeeded — the classic
  "false negative." Nothing in the code distinguishes this from a genuine
  failure on the client side; the multi-step wizard additionally stores its
  in-progress draft server-side in the session cookie
  (`web/routes/transaction_entry_web.py`'s `DRAFT_KEY`/`_draft`/`_save_draft`,
  lines 21-42) with no "was this step already submitted" guard.
- **Expected database state:** fully committed — this is a real, correct
  write; the danger is entirely on the client/operator's next action, not
  the server's prior one.
- **Expected audit state:** committed together with the business row, per
  §3 — the audit trail is accurate and complete for the write that actually
  happened.
- **Retry safety:** **NOT safe** for `create_transaction` specifically —
  since there is no idempotency key (confirmed, §Scenario 1) and the
  duplicate-detection heuristic only flags after the second row is already
  inserted (`duplicate_detection_service.py:37-71`), an operator who
  "just resubmits" because their screen looked like it failed creates a
  second, real `Transaction` row. Reference Agent 1 for the authoritative
  statement on which specific endpoints this applies to.
- **Required alert (proposed):** none specific to this scenario beyond the
  general 5xx/timeout monitoring proposed in Scenario 3 — the useful signal
  here is client-side (a timeout the app never sees), so an LB/reverse-proxy
  layer would need to be the source, and none is visible in this repo.
- **Required support information (proposed):** a correlation ID would let
  support match "the operator says it failed" against "the audit log shows
  it committed at that timestamp." Today the only queryable trail is
  `AuditRepository.list_recent()` (`backend/repositories/transaction_repository.py:184-188`,
  capped at `limit=100`, no admin UI page renders it — confirmed by grep,
  no `admin_web.py` route surfaces audit entries) — support would have to
  query the database directly.
- **Recovery procedure:** no automated reconciliation exists. A supervisor
  would need to notice the resulting `pending_review` duplicate flag in the
  notifications list and resolve it via `DuplicateDetectionService.review_flag`
  (dismiss the duplicate, keep the original) — this only works because the
  operator happened to retry in a way the heuristic can match; a differently
  shaped retry (e.g. a slightly different bag number typo) would not be
  caught at all.
- **Pass/fail evidence needed (Phase 5):** submit `create_transaction`,
  drop the connection after the server-side commit but before the response
  is written (e.g. via a test hook), then confirm exactly one `Transaction`
  row exists; separately, confirm that a genuine resubmission with identical
  fields is caught by the duplicate-detection notification.

## Scenario 5 — DB connection pool exhausted

- **Expected user-visible result:** `backend/core/database.py` (read in
  full) passes only `echo` and `connect_args` to every `create_async_engine`
  call (core at lines 22-26, each catalog at lines 39-41) — **no
  `pool_size`, `max_overflow`, `pool_timeout`, or `pool_pre_ping` is set
  anywhere.** This is already flagged as a gap in `00_baseline.md` §4
  ("Connection-pool settings: None explicit ... SQLAlchemy defaults
  apply"). There are 5 independent engines (core + 4 catalogs), so
  exhaustion is per-database, not global — a flood of catalog-VMS
  transaction writes cannot starve the core-DB login path, for instance.
  Once a pool is exhausted, the next request needing that engine's session
  blocks waiting for a free connection up to the pool's timeout, then
  raises — uncaught (no route catches pool-timeout errors specifically),
  falling into the same generic-500 path as Scenario 3. **Exact default
  pool numbers for this SQLAlchemy/aiosqlite combination were not verified
  by execution — needs a live test**, but the absence of any explicit
  tuning is confirmed by reading every `create_async_engine` call site.
- **Expected database state:** unaffected — a request that never acquires a
  session never touches the database.
- **Expected audit state:** n/a, same reason.
- **Retry safety:** retrying while the pool is still saturated just fails
  again (no corruption risk, no effectiveness either) until either load
  drops or whatever is leaking connections is fixed.
- **Required alert (proposed):** a pool-saturation metric (checked-out
  connections vs. pool size, per engine) — none exists; there is no metrics
  layer at all (§1).
- **Required support information (proposed):** logging pool-checkout
  timeouts with a correlation ID and the acquiring route; today this would
  present as an unlabeled traceback to stderr, indistinguishable from any
  other DB error without a live investigation.
- **Recovery procedure:** restart the process (resets all 5 pools) — the
  only lever available today. **Concrete evidence a leak already exists**:
  `00_baseline.md`'s Phase 0 test run recorded `SAWarning: garbage
  collector is trying to clean up non-checked-in connection` across
  `test_duplicates_and_notifications_api.py`, `test_eod_and_transfer_api.py`,
  `test_new_features.py`, `test_transactions_api.py`, and
  `test_web_admin_and_transfer.py` — i.e. some code path already acquires a
  session/connection that isn't released through the normal
  context-manager/dependency path. Root-causing which path is Agent 1's
  scope; it is directly relevant evidence here because it is exactly the
  kind of leak that turns into real pool exhaustion under sustained load
  (Agent 2's scope to load-test).
- **Pass/fail evidence needed (Phase 5):** (load-test, Agent 2) confirm the
  app either queues gracefully or fails cleanly under connection pressure
  rather than hanging indefinitely; (unit-level, in scope here) a test
  sweeping every session-acquisition path found in §3 and asserting each one
  closes its session even when the wrapped code raises, to close the leak
  the Phase 0 SAWarning already flagged.

## Scenario 6 — Report/hardware-parse generation fails

Two real analogs exist and both are covered.

### 6a. Hardware banknote-counter report fails to parse

- **Expected user-visible result:** **this is the one scenario in the whole
  app with an actual designed-for graceful path, confirmed by code.**
  `GDC1ReportCounter.wait_for_count_result` → `self._parse(text)` →
  `parse_report` raises `ReportParseError` on a report that doesn't match
  the configured profile; with `COUNTER_AUTODETECT` (default `true`,
  `hardware/config.py:23`) it tries every other installed profile and only
  accepts one whose own totals agree, otherwise re-raises with the raw
  report text attached (`hardware/gd_c1_report.py:172-206`). This propagates
  through `wait_for_shared_count()` to `_read_counter_sync`, which **is**
  wrapped in `try/except Exception` (`web/routes/transaction_entry_web.py:447-451`)
  and returned as JSON `{"error": str(exc)}` with **HTTP 200** (not an error
  status). `wizard_cash.html`'s JS (`web/templates/wizard_cash.html:361-367`)
  checks for the `error` key, and — unless the message matches
  `/no report received/i` (a plain timeout, treated as "keep listening",
  lines 362-364) — shows `"Machine unavailable — enter counts manually. " + error`
  and lets the operator finish the transaction by typing counts in by hand.
  Confirmed working fallback, not hypothetical.
- **Expected database state / audit state:** n/a — a parse failure happens
  before any `Transaction` row is created; nothing is written.
- **Retry safety:** safe. The driver is read-only by design — confirmed by
  `tests/test_c1_driver.py::test_driver_never_writes_to_the_port` — so
  re-listening for another report burst has no side effects.
- **Required alert (proposed):** none exists; propose counting parse
  failures per site/machine so a persistently misconfigured profile (wrong
  `COUNTER_PROFILE`) is visible to ops instead of only ever seen by whichever
  operator is at the wizard that day. Currently the only trace is a
  `print()` with the raw report embedded (`gd_c1_report.py:146,152,202`),
  console-only.
- **Required support information (proposed):** the already-parsed
  `report_number`/`machine_serial_number` (`gd_c1_report.py:155-157`,
  currently `print()`-only) should be persisted or surfaced somewhere
  queryable so support can correlate a specific physical report with a
  specific complaint.
- **Recovery procedure:** none needed — manual entry is the built-in
  recovery path and requires no code fix or restart.
- **Pass/fail evidence needed (Phase 5):** unit coverage of malformed input
  already exists and is strong (`tests/test_c1_driver.py`'s
  `test_unparseable_data_raises_and_includes_the_raw_text`,
  `test_strict_mode_rejects_a_report_whose_totals_disagree`,
  `test_noise_below_minimum_length_is_not_treated_as_a_report`, among
  others). What's missing is an end-to-end test through
  `POST /web/transactions/new/count` and the browser-side JS confirming the
  manual-entry fallback actually activates, not just that the driver raises
  the right exception.

### 6b. Excel/CSV report generation fails

- **Expected user-visible result:** **confirmed by reading both files in
  full — no exception handling anywhere in this path.**
  `reports/report_engine.py`'s `build_transactions_excel`/`build_transactions_csv`
  (`wb.save(output_path)` at line 202, `df.to_csv(output_path)` at line 209)
  have no try/except, and neither does the caller,
  `web/routes/reports_web.py:88-93` (`download_report`). Any failure —
  permission error, disk full, an openpyxl/pandas internal error on
  unexpected data — propagates uncaught into the same generic-500 path as
  Scenario 3 (no `errors/500.html`, no correlation ID, no app-level log).
- **Expected database state:** unaffected — `download_report` only reads
  (`_fetch_enriched_transactions`, lines 31-66); no write path exists in
  this endpoint at all.
- **Expected audit state:** **none is written for report downloads** —
  confirmed no `audit_repo.log` call anywhere in `reports_web.py`. Exporting
  transaction/customer/cash data to a file is not audited, which is a gap
  independent of the failure-handling question.
- **Retry safety:** always safe — the whole operation is a pure read plus
  file generation with no side effects on success or failure.
- **Required alert (proposed):** report-generation error rate; none exists.
- **Required support information (proposed):** which catalog/date-range/user
  triggered a failing report — nothing is logged today.
- **Recovery procedure:** retry once the underlying cause (disk space,
  permissions) is fixed. **Confirmed side effect worth flagging alongside
  Scenario 7/8:** `tempfile.NamedTemporaryFile(delete=False, ...)` is
  created and closed (`reports_web.py:86-87`) *before* `build_transactions_excel`/
  `build_transactions_csv` run; the `FileResponse`+`BackgroundTask(os.unlink, ...)`
  that cleans it up is only constructed *after* those calls succeed
  (`reports_web.py:96-101`). If generation raises, that code path is never
  reached — the empty/partial temp file is **never cleaned up**, a real
  temp-file leak on every report-generation failure.
- **Pass/fail evidence needed (Phase 5):** force `build_transactions_excel`
  to fail (e.g. point at an unwritable path) and confirm (a) a clean error
  response, not a hang, (b) no orphaned temp file survives the failure, (c)
  no other part of the app is affected (no DB state touched).

## Scenario 7 — Filesystem unavailable / read-only

- **Where it bites, confirmed by reading each write path:** (a) the 5
  SQLite files themselves; (b) `backups/` (`backup_service.py`'s
  `os.makedirs(BACKUP_ROOT)`, `sqlite3.connect(dst_path)` for each backup
  file); (c) report temp files (`reports_web.py`, Scenario 6b); (d) Alembic
  version/ini files read at startup (`init_databases()`).
- **Expected user-visible result:** SQLite writes to a read-only filesystem
  raise `sqlite3.OperationalError: attempt to write a readonly database`,
  surfaced through `aiosqlite` into the same uncaught-exception → 500 path
  as Scenario 3. For backups specifically: `create_backup()`'s per-database
  loop (`backup_service.py:56-63`) has **no try/except around the per-file
  backup call** — confirmed by reading the loop — so a filesystem problem
  on, say, the 3rd catalog's destination aborts the whole function with an
  unhandled exception, silently losing the backup for every catalog after
  it in iteration order, with no partial-success reporting. `admin_web.py`'s
  `create_backup` route (lines 124-146) has **no try/except around `await
  backup_service.create_backup()` either** — confirmed — so this surfaces to
  the admin as the same generic 500, not a clear "backup partially failed"
  message.
- **Expected database state:** as Scenario 3 — the rollback branch fires,
  no partial commit — but every subsequent write request fails identically
  until the filesystem issue clears, and nothing detects or reports this
  condition proactively (no periodic write-probe exists).
- **Expected audit state:** if the filesystem itself is read-only, the
  audit write can't land either — but per §3 this is because the *whole
  transaction* fails together, not because the audit write failed on its
  own. The one place this is genuinely decoupled (backup creation vs. its
  audit row) is covered in Scenario 10.
- **Retry safety:** not safe to blindly retry while the condition persists
  (identical failure each time); safe once the filesystem issue is
  resolved, subject to Agent 1's idempotency findings for the specific
  write being retried.
- **Required alert (proposed):** filesystem read-only/write-failure
  detection (e.g. a periodic write-probe against each `.db` file's
  directory and `backups/`); none exists today.
- **Required support information (proposed):** the raw `OSError`/
  `OperationalError` with the failing path and a correlation ID; today it's
  an unlabeled traceback to stderr only.
- **Recovery procedure:** fix the underlying mount/permissions, then verify
  with a manual write (e.g. re-trigger a backup from the admin page). No
  automated recovery or self-check exists.
- **Pass/fail evidence needed (Phase 5):** make the `backups/` directory (or
  one catalog's destination) read-only mid-run and confirm `create_backup()`
  reports which catalogs succeeded/failed individually instead of losing
  the remainder silently — this requires an actual code change to add
  per-file error handling, which is out of scope for Phase 1 but should be
  the acceptance criterion for whatever Phase 3/4 fix addresses this gap.

## Scenario 8 — Disk space low

- **Where it bites:** identical set of write paths as Scenario 7. SQLite
  raises `sqlite3.OperationalError: database or disk is full` on `ENOSPC`;
  openpyxl's `wb.save()`/pandas' `to_csv()` raise
  `OSError: [Errno 28] No space left on device`.
- **Expected user-visible result:** same uncaught-exception → 500 path —
  confirmed by the same absence of disk-specific handling already
  established for `report_engine.py`, `backup_service.py`, and the generic
  `except Exception` in `get_core_db`/`get_catalog_db`, which does not
  distinguish "disk full" from any other DB error.
- **Expected database state:** SQLite is designed to detect `ENOSPC`
  mid-write and roll back rather than leave a corrupt file, but **this is a
  guarantee of the SQLite engine, not app code, and was not exercised live
  in this review — needs a live test** to confirm rather than assume.
- **Expected audit state:** same-transaction, same fate as the business
  write it documents (§3).
- **Retry safety:** not safe until space is actually freed.
- **Required alert (proposed):** disk-usage threshold alerting (e.g.
  >85%/>95% used on the volume holding the 5 `.db` files and `backups/`) —
  none exists. Given this system runs entirely on local SQLite files with
  **no capacity ceiling, retention policy, or pruning logic enforced
  anywhere in code** (confirmed: no "prune"/"retention"/delete logic in
  `backup_service.py` — `list_backups()` only enumerates, nothing ever
  deletes an old backup), this is arguably the single most important
  missing alert for this system as built.
- **Required support information (proposed):** free-space at time of
  failure; nothing captures this today.
- **Recovery procedure:** free disk space — the only concrete lever that
  exists is manually deleting old directories under `backups/` (no tool
  does this automatically or even assists with it), then restart the
  process if it's left in a bad state.
- **Pass/fail evidence needed (Phase 5):** a container/VM-level test that
  drives free space to near-zero and confirms the app fails writes cleanly
  (clear error, no corrupted `.db` file — verified with SQLite's own
  integrity check) and recovers automatically once space is freed, without
  requiring a restart.

## Scenario 9 — External dependency (hardware counter) slow/unavailable

- **At startup:** `open_shared_counter()` (`hardware/__init__.py:36-50`)
  catches any connection failure, prints a message, and continues without a
  counter — **confirmed graceful, does not block app startup**, in direct
  contrast to a database failure at the same point in `lifespan` (§2). Real
  positive finding, worth preserving in any future refactor.
- **Mid-operation, slow:** `wait_for_shared_count()` blocks up to
  `timeout_seconds` (defaults to 120s, `hardware/base.py:24`) inside
  `asyncio.to_thread` (`transaction_entry_web.py:448-449`), so it doesn't
  block the event loop for *other* requests, but it does hold that one
  request/response cycle open for up to 120s per poll.
- **Expected user-visible result on timeout:** caught by the broad
  `except Exception` (`transaction_entry_web.py:450-451`), returned as
  `{"error": "No report received within 120s..."}` with **HTTP 200**.
  `wizard_cash.html`'s JS specifically pattern-matches
  `/no report received/i` and treats it as "keep listening silently"
  (`wizard_cash.html:362-364`) rather than surfacing anything — **meaning a
  genuinely absent/disconnected machine causes the wizard to poll forever
  with no visible error to the operator**, who has to notice nothing is
  happening and give up on their own. Only a `ConnectionError` (counter
  never connected at all, or the shared handle is `None`) produces the
  visible "Machine unavailable — enter counts manually" message
  (`hardware/__init__.py:63-69`, `wizard_cash.html:366`).
- **Serial port already claimed (e.g. by ISA):** `connect()` raises
  `ConnectionError` with a specific, well-written diagnostic pointing at ISA
  as the likely owner (`hardware/gd_c1_report.py:53-73`) — a good, tested
  error message (`tests/test_c1_driver.py::test_busy_port_error_names_isa_as_the_likely_owner`).
  `tools/free_port.py` exists specifically to resolve this contention
  (Windows-only, stop-the-holder with `--restore` to undo) but is an
  operator-run CLI tool, never invoked automatically by the app.
- **Expected database/audit state:** n/a — this is entirely pre-DB-write
  (count-entry stage, before `create_transaction` is ever called).
- **Retry safety:** safe — read-only driver, confirmed (Scenario 6a).
- **Required alert (proposed):** an unexpected mid-shift counter
  disconnection is currently invisible operationally — `open_shared_counter()`
  only runs once at startup; **there is no reconnect-on-failure logic
  anywhere for a counter that connected successfully and later drops**
  (confirmed by re-reading `hardware/__init__.py` in full — no retry/backoff
  path exists there). Propose alerting on a sustained run of
  `wait_for_shared_count` timeouts.
- **Required support information (proposed):** the report/machine serial
  number and COM port, plus the actual timeout duration hit, surfaced to
  support instead of only the server console.
- **Recovery procedure (grounded in the code's own comment):** restart
  Nexus to reconnect the counter — this is literally what
  `hardware/__init__.py:47-48` says: *"Continuing without it. Fix the port
  and restart Nexus to reconnect."* Any hardware disconnection mid-shift
  therefore requires a **full application restart** to restore automatic
  counting, which disrupts every other concurrently-open wizard session too
  — a real operational cost for what should be a localized, single-machine
  problem.
- **Pass/fail evidence needed (Phase 5):** start a wizard session, drop the
  simulated/mock counter mid-wait, and confirm (a) the operator sees a
  distinct, visible signal rather than silent indefinite polling, (b)
  manual entry stays available throughout, (c) an operational alert fires
  so a supervisor learns about it before an operator has to complain.

## Scenario 10 — Audit write fails

- **Confirmed by code (§3):** for every request-time write path found —
  `TransactionService`, `EODService`, `NotificationService`,
  `DuplicateDetectionService` — the audit write
  (`AuditRepository.log`, `transaction_repository.py:179-182`) shares the
  *same* `AsyncSession` as the business write it documents, and only
  `add()`s + `flush()`s; the actual commit is the single
  `await session.commit()` in `get_core_db`/`get_catalog_db` after the
  whole request handler returns. **"The audit write fails independently of
  the business write" therefore cannot happen for any of these flows** —
  they succeed or roll back as one atomic unit. I checked every
  state-changing method in these four services and each one that mutates
  data pairs it with an `audit_repo.log`/`notification_service.raise_event`
  call on the same session; I did not exhaustively re-verify every route in
  the repo beyond these four services — Agent 1 owns the authoritative,
  complete transaction-boundary audit.
- **The one confirmed real exception:** `web/routes/admin_web.py:124-146`
  (`create_backup`, the web route). It calls `await
  backup_service.create_backup()` — a **filesystem** operation, no DB
  session involved — and only *afterward* writes an audit row via
  `CoreAuditRepository` on `core_db`, a *separate* session/resource with no
  two-phase coordination between the two. Two concrete failure modes exist
  here, both confirmed by reading the route in full: (1) if
  `create_backup()` itself raises (e.g. the `IndexError` described in
  Scenario 12 against a non-SQLite URL, or a filesystem error per Scenario
  7), the route has no try/except around that call — it 500s, and **no
  audit row is even attempted**, so there is no record anywhere that a
  backup was tried and failed; (2) if `create_backup()` succeeds but the
  subsequent `core_db` commit fails (e.g. `core.db` locked/full), the backup
  files genuinely exist on disk but **no audit trail records who created
  them or when** — filesystem success and DB-audit success are not atomic
  with each other.
- **Expected user-visible result:** for the four shared-session services,
  identical to whatever DB/filesystem/disk scenario caused the shared
  transaction to fail (Scenarios 3/7/8) — not a distinct behavior. For
  `create_backup`, a bare 500 with no audit trail either way, per above.
- **Expected database state:** no partial state possible for the
  shared-session paths — either both rows exist or neither does. For
  `create_backup`, the backup **files** and the audit **row** can
  legitimately diverge (files present, no audit row).
- **Retry safety:** governed by the same idempotency question as the
  underlying business write (reference Agent 1); `create_backup` itself is
  safe to retry (Scenario 11).
- **Required alert (proposed):** an audit-volume sanity check (e.g.
  transaction-row count vs. `TRANSACTION_CREATED` audit-entry count drift)
  as a cheap independent signal, given there is no separate audit pipeline
  to alert on directly; also specifically for backups, alert if a
  `backups/<timestamp>/` directory exists with no matching
  `DATABASE_BACKUP_CREATED` audit row. Neither exists today.
- **Required support information (proposed):** `AuditRepository.list_recent()`
  (`transaction_repository.py:184-188`) is the only query surface for
  catalog-level audit data (default `limit=100`), and there is **no admin
  UI page rendering it at all** — confirmed by grep, no route in
  `admin_web.py` serves audit entries. Support has no built-in way to
  browse the audit trail today short of querying the database directly.
- **Recovery procedure:** n/a for the atomic shared-session paths (nothing
  to reconcile — it's one transaction). For the backup/audit divergence:
  manually cross-check `backups/` directory contents against the core audit
  log to spot an unrecorded backup, or an audit row whose backup directory
  no longer exists — no tooling assists with this today.
- **Pass/fail evidence needed (Phase 5):** (a) fault-inject a failure
  between a business write and its paired `audit_repo.log` call within one
  request and confirm neither row persists, proving the shared-transaction
  guarantee holds under fault injection and not just on the happy path; (b)
  specifically for `create_backup`, fault-inject a failure in the post-backup
  `core_db` commit and confirm the resulting backup-files-without-audit-row
  state is at least detectable (even if not yet fixed).

## Scenario 11 — A transient operation is retried

- Given zero retry/backoff logic anywhere in the codebase (confirmed, §1),
  "retried" here only ever means a human or client manually resubmitting —
  there is no automatic retry at the DB layer, the hardware layer, or the
  HTTP layer anywhere in this app.
- **Per-operation safety, cross-referencing what's confirmed throughout this
  document:**
  - **Hardware count read** (`POST /web/transactions/new/count`): safe —
    read-only, no side effects (Scenario 9).
  - **`create_transaction`**: **NOT safe** — no idempotency key; the
    duplicate-detection heuristic only flags after the fact, does not block
    (Scenarios 1, 4). Reference Agent 1 for the authoritative statement.
  - **`close_day`/`reopen_day`**: **confirmed safe by construction** — the
    one clearly idempotent write path found in this review.
    `EODService.close_day` (`eod_service.py:23-32`) raises
    `ValueError("...already closed")` on a second call for the same date,
    and the route (`backend/api/routes/eod.py:18-22`) maps that to a clean
    HTTP 409, not a duplicate closure or a 500.
  - **`create_backup`**: safe to retry — each call makes a new
    timestamped directory (`backup_service.py:50-51`); retries just produce
    redundant backups, never corrupt existing ones. The only cost is disk
    space, and there is no retention policy to bound it (Scenario 8).
  - **Report download**: safe to retry — pure read plus regeneration, no
    side effects either way (Scenario 6b).
- **Expected user-visible result / DB state / audit state:** entirely
  dependent on which operation, per the table above.
- **Required alert (proposed):** a "duplicate flag rate" or
  `pending_review` queue-depth signal, since that flag is presently the
  *only* system-generated evidence that a retry produced a real duplicate —
  nothing pages on it, it just sits in the business notifications list
  today.
- **Required support information (proposed):** correlation ID (proposed
  throughout this document) to let support tie "the operator says they
  clicked submit twice" to the two actual server-side requests.
- **Recovery procedure (grounded in what exists):** for transactions, a
  supervisor reviews the `pending_review` duplicate-flag queue
  (`DuplicateDetectionService.list_flags`/`review_flag`) and dismisses the
  extra one; for EOD, moot (409 prevents the double-close outright); for
  backups, manually delete the redundant directory under `backups/` — no
  delete/prune tool exists, this is a raw filesystem action.
- **Pass/fail evidence needed (Phase 5):** a test matrix, one case per
  state-changing endpoint, submitting the identical request twice in
  immediate succession and asserting the documented behavior above per
  operation (409 for EOD, a flagged duplicate pair for transactions,
  harmless redundancy for backups).

## Scenario 12 — A backup is restored to a clean environment

- **What exists, confirmed by reading `backup_service.py` in full:**
  `create_backup()` (lines 48-64) uses SQLite's own online backup API
  (`sqlite3.Connection.backup`, not a raw file copy — deliberately, per the
  module docstring, to be safe against the app's long-lived `aiosqlite`
  connections) to copy all 5 databases into a timestamped
  `backups/<YYYYMMDD_HHMMSS>/*.db` directory. `list_backups()` (lines
  67-83) enumerates existing backup directories with file counts and total
  size. Both are exposed through `web/routes/admin_web.py:112-146`, gated
  behind `WebAdminOnly` (confirmed by the route signature).
- **What does NOT exist, confirmed by grep across `backend/`, `tools/`, and
  `nexus_setup_manager.py`:** **any restore function, anywhere.** The
  "restore" hits that do exist are unrelated —
  `nexus_setup_manager.py:846-861` backs up/restores the Python **venv**
  and `requirements.txt` before modifying them (an installer safety net,
  not a database concern), and `tools/free_port.py --restore`
  (`tools/free_port.py:385-390`) restarts Windows **services** that the
  tool itself stopped to free a COM port — also unrelated to the databases.
  There is no `restore_backup()` function, no admin UI control, no CLI
  script, and **no documented procedure in `SETUP.md` or `README.md`**
  (grepped for "backup"/"restore", zero hits in either file).
- **Confirmed, separate, Critical-severity gap:** `backup_service.py` is
  **SQLite-only**. `_sqlite_path()` (lines 20-25) parses each database URL
  by splitting on the literal substring `"///"`, which only appears in
  `sqlite+aiosqlite:///<path>`-style URLs. `00_baseline.md` §4 confirms the
  documented production target is **PostgreSQL** via `asyncpg`
  (`postgresql+asyncpg://user:pass@host/db` — no `"///"` substring in the
  normal case). Against such a URL, `url.split("///", 1)[1]` raises
  `IndexError: list index out of range` — `create_backup()` **does not
  function at all** against the stated production database engine. This is
  a Critical gap: the one backup capability that exists stops working
  entirely on the system's own documented production target.
- **Inferred (not code-supported) manual restore procedure**, the only
  mechanically possible path given what `create_backup()` produces
  (identical-format SQLite files at the same paths the live app reads):
  stop the app, copy the desired `backups/<timestamp>/<name>.db` files over
  the live `core.db`/`catalog_*.db` files, restart so `init_databases()`
  re-stamps the Alembic heads against the restored files. **This is
  explicitly described as inferred/untested — nothing in the repo verifies
  it works.**
- **Cross-database consistency, confirmed by reading the loop:**
  `create_backup()`'s `for name, src_path in _sources().items()` loop
  (lines 56-63) backs up the 5 databases **one at a time**, sequentially,
  against a live application. SQLite's backup API guarantees each
  individual `.db` file is internally consistent, but the 5 files are not
  snapshotted together — a backup taken while the app is live can capture
  `core.db` and, say, `catalog_vms.db` at meaningfully different instants.
  A restore therefore only guarantees per-file consistency, not
  cross-database (e.g. `User` referenced by a transaction's `user_id`)
  consistency at a single point in time.
- **Expected database/audit state after a real restore:** whatever state
  each file was in at its own backup moment — no verification tooling
  exists to confirm the restored set is internally consistent as a whole;
  `list_backups()` reports only file count and total size, nothing about
  cross-file timestamp skew.
- **Retry safety / audit:** n/a — this is an offline operational procedure,
  not a request-scoped operation.
- **Required alert (proposed):** since backup creation is **100% manual/
  admin-triggered today** — confirmed no scheduled job runs it (unlike EOD
  close, there is no `scheduler.add_job(...create_backup...)` anywhere in
  `backend/main.py` or `eod_scheduler.py`) — the first, more urgent proposal
  is alerting on **backup staleness** (e.g. "no successful backup in the
  last N hours") rather than anything about restore itself, since restore
  quality is moot if backups aren't being taken regularly in the first
  place.
- **Required support information (proposed):** document the manual restore
  steps above, and add a "backups taken within N seconds of each other"
  consistency indicator to `list_backups()`'s output so an operator can see
  at a glance whether a given backup set is cross-consistent before relying
  on it.
- **Recovery procedure:** as described above (manual file copy + restart) —
  explicitly not a tested or code-supported procedure, only the sole
  mechanically-possible one given the backup format.
- **Pass/fail evidence needed (Phase 5):** an end-to-end test that (a)
  seeds data, (b) runs `create_backup()`, (c) in a clean environment,
  replaces the live `.db` files with the backup's files, (d) starts the app
  through the normal `lifespan` path, and confirms all seeded data is
  intact and Alembic considers the schema current. **This exact round-trip
  has never been run anywhere in this repo** — none of the 15 files under
  `tests/` exercises it.

---

## Overall gap ranking

**Critical**

1. **No backup/restore path works against the documented production
   database.** `backup_service.py`'s `_sqlite_path()` throws `IndexError`
   on any non-`sqlite+aiosqlite:///` URL — the only backup capability in
   the system is non-functional against PostgreSQL, the stated production
   target (Scenario 12).
2. **No restore procedure exists at all, for either database engine** —
   only backup creation and listing. A cash-processing platform with real
   money-movement records has no tested way to recover from data loss
   (Scenario 12).
3. **No idempotency mechanism for any state-changing write**, combined with
   zero automatic retry and a purely after-the-fact duplicate-detection
   heuristic that does not block — a lost-response or crash-and-retry
   scenario on `create_transaction` can silently double-book cash
   (Scenarios 1, 4, 11; reference Agent 1 for the authoritative statement).
4. **No alerting/paging channel exists at all** — zero email/SMS/webhook/
   metrics integration anywhere in the repo. Every failure scenario in this
   document, including data-loss-adjacent ones, is invisible to operations
   until a human notices manually (§1, all scenarios).

**High**

5. **No structured logging anywhere in `backend/`, `web/`, or `reports/`.**
   An unhandled exception in a request produces no application-level trace
   at all; diagnosis depends entirely on whatever bare traceback happens to
   scroll past in an unlogged console window (§1, §2, Scenarios 3, 6b, 7,
   8).
6. **No correlation/request ID mechanism** — impossible to tie a client
   complaint to a specific server-side request or database state without
   one, compounding every other gap in this document (§1, all scenarios).
7. **`/health` checks nothing** — it will report healthy while every
   database is unreachable, the EOD scheduler is dead, or the hardware
   counter was never connected. There is also no `/ready` distinct from
   `/health` for deploy-time draining (§1, Scenario 2).
8. **No connection-pool tuning, and Phase 0 already observed a live
   session/connection leak** (`SAWarning` across 5 test files) — concrete
   evidence, not speculation, that pool exhaustion is a real risk once
   under load (Scenario 5).
9. **`run_backend.py` hardcodes `reload=True`** with no separate production
   entry point, no process supervisor, and no restart-on-crash anywhere in
   the repo — a crashed process simply stays down until a human notices and
   restarts it manually (§2, Scenario 1).
10. **A dropped hardware counter connection requires a full application
    restart to recover**, disrupting every concurrently-open wizard session
    for what should be a single-machine problem, and there is no
    reconnect/backoff logic (Scenario 9).

**Medium**

11. **No generic exception handler and no `errors/500.html`** — every
    uncaught exception (DB, filesystem, report generation) surfaces as
    Starlette's bare plain-text "Internal Server Error," inconsistent with
    the app's otherwise-branded 403/404 pages (Scenarios 3, 6b, 7, 8).
12. **`backup_service.create_backup()` has no per-file error handling** —
    a filesystem problem partway through the 5-database loop silently loses
    the remaining backups with no partial-success reporting to the admin
    (Scenario 7).
13. **No retention/pruning for `backups/`**, on a system with no disk-space
    alerting either — backups can accumulate unbounded (Scenarios 8, 12).
14. **Report-generation failures leak a temp file** every time
    (`NamedTemporaryFile(delete=False, ...)` with cleanup wired only on the
    success path) — a slow, silent disk-space drain (Scenario 6b).
15. **The wizard's hardware-timeout UX silently retries forever** on a
    disconnected machine instead of surfacing an error, unlike a
    `ConnectionError`, which does show one — an inconsistency an operator
    has no way to distinguish from "still working" (Scenario 9).
16. **Backup creation and its audit record are not atomic** (different
    resources: filesystem vs. `core_db` session) — a backup can exist with
    no audit trail, or fail with none either (Scenario 10).
17. **No admin UI page renders the audit trail at all** —
    `AuditRepository.list_recent()` exists but nothing calls it from
    `admin_web.py`; support has no built-in way to browse audit history
    (Scenario 10).

**Low**

18. **No SQLite `busy_timeout` configured** — lock contention surfaces
    immediately as an error rather than a bounded wait/retry at the driver
    level (Scenario 3).
19. **APScheduler shutdown uses `wait=False`**, so a shutdown that lands
    exactly during the nightly EOD job does not wait for it to finish
    before disposing the DB engines out from under it (§2).
20. **No process/deploy-time logging at all** — even a shutdown or restart
    produces zero server-side trace, only silence (§2).

Every finding above is traceable to a specific file/line read during this
review (cited inline in the scenario sections); items marked "needs live
test" in the scenario text are flagged as such and are not counted as
independently confirmed facts.

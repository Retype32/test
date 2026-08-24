# Brink's Nexus — Crash Test & Security Evaluation

Run 2026-08-22 against the app as it stands on `master`, on a 4-core/15GB
container, single `uvicorn` worker, default SQLite configuration (as shipped).
Tooling lives in [`loadtest/`](loadtest/); re-run instructions are in
[`loadtest/README.md`](loadtest/README.md).

## TL;DR

The app is **fine for its actual current scale** (a handful of cashiers per
shift) but has **one specific, well-understood ceiling**: the default SQLite
setup allows exactly one writer at a time, and errors out under write
concurrency instead of queueing gracefully. That shows up in both crash
tests below as the same root cause. Nothing else came close to a limit —
CPU, memory, and connection handling were never the bottleneck.

Before a live/production push, the SQLite ceiling plus a handful of concrete
auth/secrets gaps (list in Part 3) are the things worth closing first.

---

## Part 1 — Concurrent users

**Method:** ramp 10 → 800 simulated concurrent users, each logging in once
then repeating (create transaction → read it back) 5 times. Stop when error
rate > 2% or p95 latency > 3s at a level. (`loadtest/concurrent_users.py`)

| Concurrency | Requests | Success rate | p50 / p95 latency |
|---|---|---|---|
| 10  | 100 | 100% | 25–30ms / ~870ms |
| 25  | 250 | 99.6% | 30ms / ~1.8–2.6s |
| 50  | ~300–430 | 89–95% | ~40ms / ~2.4–2.6s |

**Result: the ramp broke at 50 concurrent users**, with 5–10% of requests
failing — including some **login** requests, not just transaction writes.
Root-caused (see below): `sqlite3.OperationalError: database is locked`.

This is not a "the server fell over" failure — CPU stayed low, the process
never crashed, and 10–25 concurrent users ran cleanly all day. It's a
specific contention point: every login also writes an audit-log row, so even
read-heavy traffic queues behind the same single SQLite writer as
transaction entry once enough requests overlap.

## Part 2 — Batch of transactions (2–3k)

**Method:** fire a fixed total of transaction-create requests at a fixed
concurrency. (`loadtest/batch_transactions.py`)

| Run | Total | Concurrency | Success | Wall time | Throughput |
|---|---|---|---|---|---|
| A | 3000 | 10 | **98.6%** (2,959/3,000) | 189s | ~16 req/s (started at ~25 req/s, degraded as the table grew) |
| B | 3000 | 50 | **52.9%** (1,586/3,000) | 206s | ~15 req/s (flat — more concurrency did *not* mean more throughput) |

**Result: the program holds up fine across 3,000 transactions submitted at
low-to-moderate concurrency (~99% success), but throughput has a hard
ceiling around 15–25 transactions/second regardless of how much concurrency
you throw at it** — and pushing concurrency past that ceiling doesn't queue
extra requests, it fails **almost half of them**.

### Root cause (confirmed, not guessed)

Reproduced directly against the app in-process to get the real traceback
(the client only ever sees a generic "Internal Server Error" — the server
doesn't leak DB errors, which is correct behavior, but it means you can't
diagnose this from the HTTP response alone):

```
sqlite3.OperationalError: database is locked
```

raised from `aiosqlite` under concurrent writers. SQLite allows exactly one
writer to hold the database at a time; a second writer either waits
(`busy_timeout`) or fails immediately (`OperationalError`). This app's
engine setup (`backend/core/database.py`) sets no `busy_timeout`/`PRAGMA
journal_mode=WAL`, so under contention it fails fast instead of queueing.
Every one of the 5 SQLite files (core + 4 catalogs) has this same ceiling
independently.

This explains both crash tests with one mechanism: concurrent users failing
at 50 (Part 1) and the batch degrading as concurrency rises (Part 2) are the
same lock contention, just triggered from different directions.

---

## Part 3 — Security evaluation

Manual review of the full codebase (auth, sessions, access control, input
handling, secrets) with the goal of a pre-production checklist. Full
endpoint-by-endpoint role-guard audit was done across every API and web
route — **access control itself is solid**: every single API and web-portal
endpoint requires authentication, and role checks (cashier / supervisor /
administrator) are applied consistently and correctly at both layers. SQL
injection risk is low — the codebase uses the SQLAlchemy ORM/query builder
everywhere with no raw string-built SQL found. No path traversal, no
`eval`/`pickle`/`yaml.load`, no disabled Jinja2 autoescaping found anywhere
in the app.

Gaps found, ranked by what actually matters before going live:

| # | Finding | Where | Why it matters |
|---|---|---|---|
| 1 | **No brute-force protection on login** — unlimited attempts, no lockout, no delay, no failed-attempt logging | `backend/services/auth_service.py::login` | Once reachable from more than a trusted LAN, `admin`/`admin`-style credentials (see #2) are crackable by simple scripting. Also nothing records that an attack was attempted. |
| 2 | **Default seeded credentials ship without a forced change** — `admin/admin`, `supervisor1/super`, `cashier1/cash1`, `cashier2/cash2` | `database/seed.py`, documented in `SETUP.md` | Documented as "change before production," but nothing in the app enforces it — no first-login forced reset, no startup warning if defaults are still active. Easy to forget. |
| 3 | **Fallback `SECRET_KEY` baked into source** if the env var is ever unset: `"change-this-in-production-a-very-long-secret-key-for-jwt"` | `backend/core/config.py:19` | This key signs both JWTs (API) and session cookies (web portal). If a deployment ever runs without `SECRET_KEY` set — a missed `.env`, a container rebuilt from a stale image — it silently starts signing tokens with a secret that's sitting in the public/private repo history, which means anyone with source access can forge a valid admin token or session cookie. No startup check refuses to boot on the default value. |
| 4 | **Minimum password length is 4 characters, no complexity rule** | `backend/schemas/user.py:10,20` | Weak for a system that handles cash-transaction records at a financial services company. |
| 5 | **No CSRF token on web-portal state-changing forms** (relies solely on `SameSite=Lax` cookies) | `backend/main.py` (`SessionMiddleware`), all `web/routes/*_web.py` POST forms | `SameSite=Lax` blocks *most* practical CSRF in current browsers, but it's the only layer — no defense-in-depth token. Worth adding if the portal is ever reachable outside a fully trusted LAN. |
| 6 | **JWTs have no revocation path** — 8-hour expiry, no blocklist/short-lived-token-plus-refresh pattern | `backend/core/security.py`, `backend/api/deps.py` | A stolen bearer token is valid for up to 8 hours even after a "logout" (there's no server-side logout for the JSON API — deactivating the *user* does immediately cut them off, which is good, but revoking a single leaked token isn't possible). Reasonable tradeoff for an internal tool; worth knowing before exposing the API more broadly. |
| 7 | **CORS origins are hardcoded to `localhost`/`127.0.0.1`** | `backend/main.py:73` | Not a vulnerability today, but it means the current config will need a real update (never `allow_origins=["*"]` combined with credentials) before the app is served from a real domain — flagging so it isn't missed or "fixed" insecurely under deploy pressure. |

None of these are exploitable *today* against the app's actual current
deployment model (a single trusted LAN, a handful of named staff) — they're
listed because the user asked specifically about readiness for a **live**
push, where the trust boundary changes.

---

## Recommended next steps, in priority order

**1. Fix the SQLite write-contention ceiling (blocks anything beyond current small-scale use).** Pick one:
   - Quick, low-risk: enable WAL mode and a `busy_timeout` PRAGMA on every engine (`backend/core/database.py`) — lets concurrent writers queue for a few seconds instead of failing instantly. This alone would likely have gotten Run B above close to 100% success, just slower.
   - Real fix for actual scale: migrate to PostgreSQL (already supported — `asyncpg` is in `requirements.txt`, `DATABASE_URL_*` per catalog is already the only thing to change). Postgres has proper MVCC and doesn't share SQLite's single-writer limit; this removes the ceiling rather than just raising it.
   - Either way, re-run `loadtest/batch_transactions.py --total 3000 --concurrency 50` afterward and confirm the failure rate drops to ~0%.

**2. Close the security gaps in Part 3 before any deployment outside a fully trusted LAN**, roughly in this order:
   - Refuse to start (or at minimum log a loud warning) if `SECRET_KEY` is still the source-code default (#3) — cheapest fix, biggest single risk if missed.
   - Add login rate limiting / lockout after N failed attempts, and log failed attempts (#1).
   - Force a password change on first login for the seeded accounts, or at minimum add a startup/admin-dashboard warning if a default password hash is still in place (#2).
   - Raise the minimum password length and consider a complexity rule (#4).
   - Decide on CSRF tokens for the web portal if it will ever be reachable beyond the current LAN (#5).

**3. Re-run both load tests after each infrastructure change** (WAL/Postgres, and again once real production hardware/network is chosen) — the numbers here are from a single 4-core container and are directional, not a guarantee for the actual deployment box. The two scripts in `loadtest/` are reusable for that.

**4. Decide on realistic target numbers before optimizing further.** This is a cash-in-transit back-office tool for named staff, not a public consumer app — "how many concurrent users" is probably a question of tens, not thousands. Confirming the real expected peak (e.g. "20 cashiers across 3 shifts, occasional month-end burst") would tell you whether step 1's quick WAL fix is enough or whether the Postgres migration is worth doing now versus later.

**5. Add basic operational monitoring before go-live**: uvicorn/DB error-rate alerting and a simple health/uptime check external to the box, so a recurrence of the "database is locked" failure mode (or anything else) is caught from a live dashboard instead of a user complaint.

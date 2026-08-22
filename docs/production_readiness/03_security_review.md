# 03 — Security, Authentication & Authorization Review (Phase 1, Agent 3)

Date: 2026-08-22
Scope: `/home/user/test`, branch `claude/nexus-production-readiness-1yxu9y`. Read-only investigation — no application code, dependency version, or config file was modified. No dynamic/live scan was run (no deployment exists yet).

Scope correction carried over from `00_baseline.md`: this repo has no PDF receipt export/reprint/CSV-import feature. The closest real analog — Excel/CSV report **export** (`reports/report_engine.py`, `web/routes/reports_web.py`) — is reviewed under Finding S-07 (CSV injection). There is **no file-upload feature anywhere in the codebase** (confirmed: no `UploadFile`, no `multipart` file field, no `python-multipart` file-handling code outside form-field parsing for the transaction wizard; `python-multipart` in `requirements.txt` is used only for plain HTML `<form>` field parsing, not file uploads).

Every finding below is labeled **CONFIRMED** (verified by reading the actual code path) or **ASSUMED** (would need a live/dynamic test to verify). All file:line references were read directly from the repository at the commit checked out for this session.

---

## Summary

| Severity | Count |
|---|---|
| Critical | 2 |
| High | 4 |
| Medium | 6 |
| Low / Informational | 6 |

**Most severe confirmed finding:** **S-01** — the application auto-seeds well-known default credentials (`admin`/`admin` with full administrator rights, plus three other accounts) into any empty database on **every single app startup**, unconditionally, with no environment gate — and a routine, documented "re-seed" operation (`python -m database.seed`) silently **overwrites an already-changed admin password back to the shipped default**. Combined with S-02 (no startup validation of `SECRET_KEY`), a fresh production deployment run with only the documented setup steps is guaranteed to be trivially compromised on day one.

---

## Verified Findings

### S-01 — CRITICAL — Auto-seeded default administrator credentials, with silent password-reset-back-to-default on re-seed

- **File:Line:** `backend/main.py:27-42` (`_auto_seed()`, called from `lifespan()` at line 42); `database/seed.py:82-133` (`USERS` list and `seed_core_users()`)
- **CONFIRMED.**
- **Mechanism:**
  - `backend/main.py`'s `lifespan()` calls `await _auto_seed()` on **every** process startup, before serving any traffic.
  - `_auto_seed()` checks only "does the `users` table have at least one row?" (`select(User).limit(1)`). If the core database is empty — true for any fresh production deployment that hasn't been pre-populated — it unconditionally calls `database.seed.seed()`.
  - `seed()` creates four accounts with hardcoded passwords: `admin`/`admin` (administrator), `supervisor1`/`super` (supervisor), `cashier1`/`cash1`, `cashier2`/`cash2` (cashiers) — `database/seed.py:82-87`.
  - There is **no** check anywhere in `backend/main.py`, `run_backend.py`, or `backend/core/config.py` for an environment flag (no `APP_ENV`, no `ENVIRONMENT=production` gate, nothing) that would skip or block this seeding step in production.
  - **Worse:** `seed_core_users()` (`database/seed.py:107-133`) does not skip existing usernames — for a username that already exists, it goes into the `else` branch (lines 124-130) and **overwrites that user's password hash back to the hardcoded default** (`user.password_hash = hash_password(password)`) and commits. SETUP.md explicitly documents re-running `python -m database.seed` as the supported way to "re-seed manually ... after wiping a `.db` file" (SETUP.md:66-70) — an operator following that exact instruction on a live production core database would silently reset the `admin` account's password back to `admin`, even if it had been properly changed.
- **Reproduction:** Deploy with an empty/fresh core database (the default for any new install) and start the app with `python run_backend.py` (or any ASGI server pointed at `backend.main:app`). Log in to `/web/login` or `POST /api/v1/auth/login` with `admin`/`admin`. Full administrator access is granted immediately, no setup wizard, no forced change.
- **Impact:** Full compromise — administrator can create/delete/modify any user, reopen closed business days, transfer transactions between business dates, download all financial reports, and read the (informal) audit trail. This is the single most severe exposure in the codebase.
- **Remediation:**
  1. Gate `_auto_seed()` behind an explicit environment flag (e.g., only run when `settings.debug` is `True` **and** an explicit `ALLOW_AUTO_SEED=true` is set), never unconditionally.
  2. Make `seed_core_users()`'s "user already exists" branch a no-op for passwords (never silently reset an existing password), or remove the auto-update branch entirely and require an explicit `--force` CLI flag to reset a password via the seed script.
  3. Add a startup check that refuses to serve traffic if any seeded/default username still holds its shipped default password hash (compare `verify_password("admin", user.password_hash)` etc. for the known defaults and refuse to start, or force a password-change flow).
  4. Document that production deployment must run against a database that was **provisioned with real credentials**, not the demo seed path.
- **Required regression test:** `test_auto_seed_does_not_run_when_production_flag_set` (asserts `_auto_seed()` / app startup does not create default users when the production gate is active) and `test_reseed_does_not_reset_existing_password` (asserts re-running `database.seed.seed()` against a DB with an already-changed `admin` password leaves that password untouched).

---

### S-02 — CRITICAL — No startup validation of `SECRET_KEY`; same key signs both JWTs and web session cookies

- **File:Line:** `backend/core/config.py:19` (hardcoded default `secret_key = "change-this-in-production-a-very-long-secret-key-for-jwt"`); `backend/core/security.py:21,26` (JWT sign/verify uses `settings.secret_key`); `backend/main.py:79-85` (`SessionMiddleware(secret_key=settings.secret_key, ...)`)
- **CONFIRMED.**
- **Mechanism:** `Settings.secret_key` ships a hardcoded, publicly-visible-in-source default. Nothing in `backend/main.py`'s `lifespan()`, `run_backend.py`, or anywhere else checks at startup whether `secret_key` is (a) present/non-empty, (b) different from the shipped default, (c) different from the SETUP.md-documented example value (`brinks-nexus-super-secret-key-change-in-production-2026`, SETUP.md:39), or (d) above a minimum entropy/length. The app will start and serve traffic on any of these weak/default values without warning.
  - **Key reuse across two purposes:** the exact same `settings.secret_key` value both signs JWT bearer tokens (HS256, `backend/core/security.py:21`) for the JSON API **and** signs the `itsdangerous`-based web session cookie (`backend/main.py:81`) for the web portal. A single leaked/guessed/default key compromises both authentication mechanisms simultaneously — an attacker can forge a JWT for any `user_id`/expiry, and separately forge a valid `nexus_web_session` cookie for any `user_id`, achieving full authentication bypass and administrator impersonation on both surfaces.
- **Reproduction:** Deploy without overriding `SECRET_KEY` in `.env` (or with the SETUP.md-documented example value copy-pasted verbatim, which is a real, guessable, published string). Use `python-jose` locally with the known key to mint `jwt.encode({"sub": "<any-admin-user-id>", "exp": <future>}, "change-this-in-production-a-very-long-secret-key-for-jwt", algorithm="HS256")` and present it as a Bearer token, or use `itsdangerous.TimestampSigner` with the same key to forge the session cookie.
- **Impact:** Complete authentication bypass on both the JSON API and the web portal; trivial privilege escalation to administrator.
- **Remediation:** At startup (in `lifespan()` before `init_databases()`/before accepting traffic), assert `settings.secret_key` is non-empty, is not equal to any of the known shipped/documented default strings, and meets a minimum length (e.g. ≥32 bytes of real entropy); raise and refuse to start otherwise. Additionally, use **two independent secrets** — one for JWT signing, one for the session-cookie signer — so a compromise of one does not automatically compromise the other.
- **Required regression test:** `test_app_refuses_to_start_with_default_secret_key` (asserts `lifespan()`/an explicit `validate_settings()` raises when `SECRET_KEY` equals a known-default or is empty) and `test_app_refuses_to_start_with_short_secret_key`.

---

### S-03 — HIGH — No CSRF token on any state-changing web-portal route (partial mitigation via SameSite=Lax only)

- **File:Line:** Every `@router.post(...)` handler in `web/routes/*.py` (e.g. `web/routes/admin_web.py:34,63,78,124,149`; `web/routes/transactions_web.py:88,117,174,247`; `web/routes/duplicates_web.py:41`; `web/routes/notifications_web.py:29`; `web/routes/catalog_web.py:16`); `backend/main.py:79-85` (`SessionMiddleware` configuration).
- **CONFIRMED** (repo-wide `grep -ri csrf` returns zero hits anywhere in the codebase — no token field, no double-submit cookie, no `Origin`/`Referer` check).
- **Mechanism:** The web portal authenticates purely via a cookie (`nexus_web_session`) with no anti-CSRF token embedded in any Jinja2-rendered `<form>`, and no server-side verification of a CSRF token, `Origin`, or `Referer` header on any POST route. The only mitigating control is the cookie's `same_site="lax"` setting (`backend/main.py:83`), which is a real, non-trivial mitigation in modern browsers — SameSite=Lax cookies are not attached to most cross-site POST requests — but it is a browser-level default behavior, not an application-enforced control: it offers no protection on older/non-compliant browsers, is a no-op if a reverse proxy or CDN in front of the app strips/rewrites the `Set-Cookie` header's `SameSite` attribute, and provides no defense-in-depth if a future change ever needs `SameSite=None` (e.g., for an embedded/iframe deployment).
- **Reproduction (would require a live instance to fully verify — see ASSUMED note):** Host a malicious page that auto-submits a form to e.g. `POST /web/admin/users/{id}/active` while a logged-in administrator's browser has an old/non-compliant SameSite implementation, or sits behind an intermediary that drops the `SameSite` attribute.
- **Impact:** State-changing actions (create/delete/deactivate users, close/reopen EOD, transfer transactions between business dates, correct transactions, resolve duplicate flags) could be triggered without the victim's intent, on any client where SameSite=Lax is not honored.
- **Remediation:** Add an explicit CSRF token (e.g. via `itsdangerous`-signed hidden field, or `starlette-csrf`) to every state-changing form and verify it server-side, in addition to (not instead of) SameSite=Lax.
- **Required regression test:** `test_post_without_csrf_token_rejected` for each state-changing web route (parametrized).
- **Note:** JSON API routes (`/api/v1/*`) are not cookie-authenticated (Bearer-token only) and are therefore not CSRF-vulnerable by the classic browser-form vector — this finding is web-portal-only.

---

### S-04 — HIGH — No brute-force protection or rate limiting on login (API and web)

- **File:Line:** `backend/api/routes/auth.py:10-19` (`POST /api/v1/auth/login`); `backend/services/auth_service.py:17-29` (`AuthService.login`); `web/routes/auth_web.py:24-42` (`POST /web/login`)
- **CONFIRMED.** No rate limiter middleware exists anywhere in `backend/main.py` (only `CORSMiddleware` and `SessionMiddleware` are registered), no lockout counter/field on the `User` model (`backend/models/user.py`), no escalating-delay logic in `AuthService.login`, and no per-IP or per-username throttling in either login route.
- **Impact:** Combined with S-01 (known default credentials), S-10 (4-character minimum password), and the absence of any lockout, both login surfaces are open to unthrottled credential-stuffing / brute-force attacks with no application-level friction.
- **Remediation:** Add a rate limiter (per-IP and per-username) with exponential backoff or temporary lockout after N consecutive failures; log and alert on repeated failures; consider a CAPTCHA or delay for the web login form.
- **Required regression test:** `test_login_locks_out_after_repeated_failures` (asserts the Nth consecutive failed login for a username is rejected/delayed regardless of a subsequently-correct password) — this is also a good candidate for a Phase 5 **live** load/abuse test (see Phase 5 tooling section) since true rate-limiting behavior needs to be exercised over real time/concurrency.

---

### S-05 — HIGH — No security headers anywhere (CSP, X-Frame-Options, X-Content-Type-Options, HSTS, Referrer-Policy)

- **File:Line:** `backend/main.py` (only middleware registered: `CORSMiddleware` at line 71, `SessionMiddleware` at line 79 — no header-injection middleware of any kind).
- **CONFIRMED** by reading the full middleware stack in `backend/main.py` and confirming no response-header-setting middleware, no `Content-Security-Policy` string, no `X-Frame-Options` string, anywhere in the repo (`grep` across `backend/` and `web/` for these header names returns zero hits outside this review).
- **Impact:** The server-rendered Jinja2 portal (handling live cash-transaction data) has no clickjacking protection (`X-Frame-Options`/`frame-ancestors`), no MIME-sniffing protection (`X-Content-Type-Options: nosniff`), no XSS-blast-radius reduction (`Content-Security-Policy`), and — assuming eventual HTTPS deployment — no `Strict-Transport-Security` to prevent downgrade/SSL-stripping.
- **Remediation:** Add a small middleware (or `starlette`'s `Middleware` list) that sets, at minimum: `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, a `Content-Security-Policy` appropriate to the portal's actual script/style sources, and `Strict-Transport-Security` (once served over HTTPS, gated the same way `session_cookie_secure` should be — see S-11).
- **Required regression test:** `test_response_includes_security_headers` asserting the presence/value of each header on a representative `/web/*` and `/api/v1/*` response.

---

### S-06 — HIGH — Web session cookie has no explicit `max_age`; defaults to Starlette's 14-day lifetime (vs. the JWT's 8-hour expiry), and cannot be server-side revoked

- **File:Line:** `backend/main.py:79-85` (`SessionMiddleware(...)` call omits `max_age`); confirmed default in installed `starlette` package: `starlette/middleware/sessions.py:21` — `max_age: int | None = 14 * 24 * 60 * 60,  # 14 days, in seconds`.
- **CONFIRMED** by reading both the call site (no `max_age=` argument passed) and the Starlette library default.
- **Mechanism:** `backend/core/security.py:18` sets the JWT expiry explicitly (`settings.access_token_expire_minutes` = 480 minutes / 8 hours). The web session cookie has no equivalent explicit setting and silently inherits Starlette's 14-day default — nearly 42× longer than the API token's exposure window, for what is otherwise described (README.md) as the same authentication tier. Because the cookie is a stateless, `itsdangerous`-signed value (not a server-side session store), the server has no list of "currently valid" cookies to revoke from: `GET /web/logout` (`web/routes/auth_web.py:45-48`) only clears the *current browser's* cookie via `request.session.clear()` — it cannot invalidate a copy of the same signed cookie value that was captured elsewhere (e.g., synced/shared browser profile, disk/memory forensics, an XSS that read `document.cookie` before `HttpOnly` would stop it, or a shared kiosk PC). That captured copy remains valid for up to 14 days or until the underlying account is deactivated.
- **Mitigating factor (CONFIRMED):** `get_web_current_user` (`web/deps.py:15-34`) re-fetches the user row from the database on *every* request and checks `is_active`, so an administrator deactivating a compromised account does cut off that stolen cookie immediately on its next use — the exposure window is bounded by "until noticed and deactivated," not strictly the full 14 days, but there is no proactive way to force-expire a session (e.g., no "log out all other sessions" control, no session ID/JTI denylist).
- **Impact:** A captured session cookie is a longer-lived credential than the JWT bearer token, with no server-side revocation short of disabling the whole account, in a cash-handling application that plausibly runs on shared operator terminals.
- **Remediation:** Set an explicit, short `max_age` on `SessionMiddleware` (e.g. matching or close to the 480-minute JWT expiry, or an explicit idle-timeout), and/or move to a server-side session store keyed by a random session ID so individual sessions can be revoked without disabling the account.
- **Required regression test:** `test_session_cookie_expires_after_configured_max_age` (asserts a cookie older than the configured window is rejected) and `test_logout_does_not_invalidate_a_separately_held_copy_of_the_cookie` (documents current behavior so any future server-side-revocation fix has a test to update).

---

### S-07 — MEDIUM — CSV/Excel formula injection via the `wallet_id` field in exported reports

- **File:Line:** `reports/report_engine.py:118` (`"Wallet Number": t.get("wallet_id") or ""`, written into both `build_transactions_excel` at line 188 (`cell = ws.cell(..., value=val)`) and `build_transactions_csv` at line 209 (`df.to_csv(...)`) with no sanitization); `web/routes/transaction_entry_web.py:89-95` (`_validate_wallet_id`, the only server-side validation this field ever receives).
- **CONFIRMED.**
- **Mechanism:** `_validate_wallet_id` only checks the value is non-empty and ≤50 characters — it places **no restriction on leading characters**. A cashier entering a wallet ID such as `=cmd|'/C calc'!A0` or `=HYPERLINK("http://evil/","x")` gets it stored verbatim on the `Transaction` row (`wallet_id` column). `_build_row()` in `reports/report_engine.py` copies it unmodified into the "Wallet Number" cell of both the `.xlsx` (via `openpyxl`, `cell.value = val` with no leading-character escaping/prefixing) and `.csv` (via `pandas.DataFrame.to_csv`) exports produced by `GET /web/reports/download` (`web/routes/reports_web.py:69-101`, gated to `WebSupervisorOrAbove`). Opening the file in Excel with default settings executes the formula (classic CSV/Excel-formula-injection, CWE-1236).
  - Other candidate fields were checked and ruled out as practical vectors: `bag_number` is regex-constrained to `\d{12,16}` (`transaction_entry_web.py:72-76`, digits only — safe); `customer_name`/`location_name` are not user-editable anywhere in the app (no create/update route exists for `Customer`/`Location` — confirmed by `backend/api/routes/customers.py` exposing GET-only endpoints and by `database/seed.py` being the only place `Customer`/`Location` rows are ever created); `username` requires an administrator to set (`backend/schemas/user.py`) — low practical risk but not entirely excluded, since an admin-created username also flows unsanitized into the "Operator Id" column. The "Comments" and "Adjustment Reason" report columns are currently always hardcoded to `""` (`report_engine.py:135-136` — `correction_reason`/notes are not actually wired into the export at all), so those are not a live vector today.
- **Impact:** A cashier-level account (the lowest-privileged role that can reach the wallet-ID field) can plant a formula that later executes on a supervisor's or administrator's workstation when they export and open a report — potential local command execution, credential/data exfiltration via `HYPERLINK`/`WEBSERVICE`, or link-based phishing, depending on the victim's Excel macro/formula settings.
- **Remediation:** In `_build_row()` (or at the point of writing any cell derived from user input), prefix any string value starting with `=`, `+`, `-`, or `@` with a leading `'` (single quote) — the standard CSV-injection mitigation for both `openpyxl` cell values and `pandas.to_csv` output — or reject/strip such leading characters at input validation time in `_validate_wallet_id`.
- **Required regression test:** `test_wallet_id_starting_with_equals_is_neutralized_in_csv_export` and `test_wallet_id_starting_with_equals_is_neutralized_in_xlsx_export`.

---

### S-08 — MEDIUM — No per-user catalog assignment; any authenticated user (any role) can access any of the 4 catalogs

- **File:Line:** `backend/models/user.py` (no catalog field on `User` at all); `backend/api/deps.py:44-52` (`get_catalog_code` accepts any valid `X-Catalog` header value with no cross-check against the authenticated user); `web/routes/catalog_web.py:16-25` (`POST /web/catalog/select` accepts any `CatalogCode` with no cross-check against the authenticated user).
- **CONFIRMED.** `CatalogCode` (`backend/core/catalogs.py:5-9`) is a closed 4-value enum, so the header/form value itself cannot be used for injection or to reach an arbitrary/unknown database (that part is safe) — but there is no authorization layer restricting *which* of the 4 valid catalogs a given user may choose. Any cashier, supervisor, or administrator account can set `X-Catalog: esnf` (or any of the other 3) on the API, or POST to `/web/catalog/select` with any valid code on the web portal, and immediately operate against that catalog's data — creating transactions, and (role permitting) viewing/exporting its reports.
- **This appears to be an intentional design choice, not an oversight:** README.md states "each is a fully separate dataset ... You can switch catalogs at any time from the sidebar without logging out," which matches the code exactly. This finding is recorded so the business can explicitly confirm whether catalog-crossing by any authenticated staff member is the intended model, or whether catalog-level segregation (e.g., ESNF-team operators should never reach VMS data) is actually required and currently unenforced.
- **Impact (if catalog segregation is in fact required):** A single compromised low-privilege (cashier) credential from any one team reaches all 4 catalogs' customer, transaction, and (for supervisors+) report/statistics data — the "multi-catalog data isolation" is a database-separation implementation detail, not an authorization boundary.
- **Remediation (if segregation is required):** Add a catalog-assignment (or catalog-allowlist) field to `User`, and enforce it in `get_catalog_code`/`get_web_catalog_code` alongside the existing enum validation.
- **Required regression test:** `test_user_cannot_select_catalog_outside_their_assignment` (only meaningful once/if catalog assignment is implemented) — until then, this is a **product decision to confirm**, not a code defect to fix blindly.

---

### S-09 — MEDIUM — No segregation-of-duties check on self-review/self-correction of one's own transaction

- **File:Line:** `backend/services/duplicate_detection_service.py:81-95` (`review_flag`, no comparison of `reviewed_by_user_id` to the flagged transaction's `user_id`); `backend/api/routes/duplicates.py:32-44` and `web/routes/duplicates_web.py:41-54` (both call `review_flag` with only a `SupervisorOrAbove`/`WebSupervisorOrAbove` role check, no ownership check); `backend/services/transaction_service.py:197-257` (`correct_transaction`, no comparison of `supervisor_user_id` to `original.user_id`); `web/routes/transactions_web.py:247-314` (`correct_transaction_submit`, same — only a `WebSupervisorOrAbove` role check).
- **CONFIRMED.** Nothing in either service method, or either route's dependency chain, prevents the acting supervisor/administrator from being the same person who entered the original transaction.
- **Mechanism:** `transaction_entry_web.py`'s transaction-entry wizard is gated only by `WebCurrentUser` (any authenticated role, `web/routes/transaction_entry_web.py:98-107` and throughout), not restricted to cashiers — so a supervisor or administrator account can enter their own cash-count transaction, have it flagged as a duplicate by `DuplicateDetectionService.check_for_duplicate`, and then personally review/dismiss that same flag via `POST /web/duplicates/{flag_id}/review` or the API equivalent, or personally "correct" their own earlier entry via `POST /web/transactions/{transaction_id}/correct` — with no second, independent approver ever involved.
- **Impact:** In a cash-handling application, the ability for one person to both create and later self-approve a correction or duplicate-dismissal on their own work removes a standard internal control (four-eyes / segregation of duties) that this kind of application would normally be expected to enforce, particularly for discrepancy-driving actions.
- **Remediation:** In `review_flag` and `correct_transaction`, reject (or require a second administrator override for) the case where `reviewed_by_user_id`/`supervisor_user_id` equals the original transaction's `user_id`.
- **Required regression test:** `test_supervisor_cannot_review_own_duplicate_flag` and `test_supervisor_cannot_correct_own_transaction`.

---

### S-10 — MEDIUM — Weak password policy: 4-character minimum, no complexity requirement

- **File:Line:** `backend/schemas/user.py:10` (`UserCreate.password: str = Field(..., min_length=4)`), `:20` (`UserUpdate.password`, same constraint)
- **CONFIRMED.** No maximum length, no character-class requirement (no digit/uppercase/symbol requirement), and no check anywhere else in the codebase (service or route layer) that layers on additional policy. `hash_password`/`verify_password` (`backend/core/security.py:8-13`) call `bcrypt.hashpw`/`bcrypt.checkpw` directly with no pre-truncation guard — bcrypt silently ignores any password bytes beyond 72, which is not exploited by the current 4-character-minimum policy but is worth noting if a future change raises the ceiling without also capping/rejecting overlong input.
- **Impact:** Combined with S-04 (no brute-force protection), a 4-character minimum (satisfied trivially by every seeded demo password: `admin`, `super`, `cash1`, `cash2`) makes credential guessing/stuffing materially easier for a cash-handling application's staff accounts.
- **Remediation:** Raise the minimum length (e.g. 10–12+) and/or require a mix of character classes; consider integrating a breached-password check (e.g. HaveIBeenPwned k-anonymity API, offline) for new/changed passwords.
- **Required regression test:** `test_user_creation_rejects_password_below_policy_minimum`.

---

### S-11 — MEDIUM — `session_cookie_secure` defaults to `False`; no startup enforcement tying it to actual deployment scheme

- **File:Line:** `backend/core/config.py:24` (`session_cookie_secure: bool = False`); `backend/main.py:84` (`https_only=settings.session_cookie_secure`)
- **CONFIRMED.** This is purely documentation-enforced today: SETUP.md:48 instructs operators to "set it `true` once the portal is served over HTTPS," but nothing in the code checks or enforces that relationship — the app will happily serve the session cookie without the `Secure` flag over an HTTPS deployment if an operator simply forgets to flip the setting (no equivalent of the S-02/S-01-style startup assertion exists here either). Note the cookie **does** get `HttpOnly` unconditionally (a Starlette `SessionMiddleware` default, confirmed by reading `starlette/middleware/sessions.py:32` — `self.security_flags = "httponly; samesite=" + same_site`), which is a real, functioning mitigation against classic cookie-theft-via-XSS already in place.
- **Impact:** If `SESSION_COOKIE_SECURE` is left at its default `False` in a production deployment that is nonetheless served over HTTPS (a very plausible operator oversight, since the default requires no action to "work" — the app functions fine either way), the session cookie would also be transmitted over any accidental plaintext HTTP connection to the same host (e.g., a misconfigured load balancer path, a user typing `http://` instead of `https://` before a redirect takes effect), exposing it to network-level interception.
- **Remediation:** Same pattern as S-02: add a startup check (or a single `settings.environment` flag that derives `session_cookie_secure` automatically) that refuses to start, or at least logs a loud warning, when running in a mode presumed to be production-facing with `session_cookie_secure=False`.
- **Required regression test:** `test_production_mode_forces_secure_cookie_flag` (once an environment flag exists to test against).

---

### S-12 — MEDIUM — Inconsistent object-read authorization between the API and the web portal for a single transaction

- **File:Line:** `backend/api/routes/transactions.py:54-64` (`GET /api/v1/transactions/{transaction_id}`, gated only by `CurrentUser` — any role) vs. `backend/api/routes/transactions.py:27-51` (`GET /api/v1/transactions/` list, gated by `SupervisorOrAbove`) and `web/routes/transactions_web.py:146-171` (`GET /web/transactions/{transaction_id}`, gated by `WebSupervisorOrAbove`).
- **CONFIRMED.**
- **Mechanism:** A cashier-role JWT can successfully call `GET /api/v1/transactions/{transaction_id}` for **any** transaction UUID in the currently-selected catalog (their own or anyone else's), even though the same cashier cannot list transactions (`GET /api/v1/transactions/` requires supervisor+) and cannot reach the equivalent web page (`GET /web/transactions/{id}` requires supervisor+). The single-object read has no ownership check (`TransactionService.get_transaction`, `backend/services/transaction_service.py:95-96`, returns the row unconditionally to any authenticated caller) and no role check beyond "any authenticated user."
- **Practical exploitability:** low as shipped — `transaction_id` is a random UUIDv4 primary key (`backend/models/transaction.py`), not sequential/guessable — but this is "protection by obscurity of the identifier" rather than an explicit authorization check, and the UUID can leak via URLs, logs, browser history, or another user's screen.
- **Impact:** A cashier who obtains any transaction's UUID (their own, from a receipt/URL, or another's, if leaked) can read its full detail via the JSON API despite having no such access via the web portal or the list endpoint — an authorization inconsistency between the two front-ends the brief specifically asked to check for.
- **Remediation:** Either restrict `GET /api/v1/transactions/{transaction_id}` to `SupervisorOrAbove` (matching the web portal and the list endpoint), or, if cashiers are meant to be able to look up their own past transactions, add an explicit ownership check (`txn.user_id == current_user.id` OR role ≥ supervisor).
- **Required regression test:** `test_cashier_cannot_read_transaction_by_id_via_api` (or, if the intended behavior is "own transactions only," `test_cashier_can_read_own_transaction_but_not_others_via_api`).

---

### S-13 — LOW — `/docs`, `/redoc`, `/openapi.json` are publicly exposed (no auth)

- **File:Line:** `backend/main.py:65-69` (`FastAPI(...)` instantiated with no `docs_url=None`/`redoc_url=None`/`openapi_url=None`, so all three default endpoints remain active and are not wrapped in any auth dependency).
- **CONFIRMED.**
- **Impact:** Anyone can enumerate the complete API surface (every route path, parameter name/type, and response schema, including the `UserCreate`/`LoginRequest` field names) without authenticating — pure reconnaissance value, no secrets are present in the generated schema, but it removes a small amount of obscurity an attacker would otherwise have to earn.
- **Remediation:** Disable or auth-gate these in production (`docs_url=None, redoc_url=None, openapi_url=None`, or mount them behind an admin-only dependency).
- **Required regression test:** `test_docs_endpoints_require_auth_in_production_mode` (once a production-mode flag exists).

---

### S-14 — LOW — `settings.debug=True` by default only controls SQL echo logging (confirmed safe on the HTTP-response-traceback axis)

- **File:Line:** `backend/core/config.py:23` (`debug: bool = True`); `backend/core/database.py:24,41` (`echo=settings.debug` on both the core and every catalog `create_async_engine` call); `backend/main.py:65-69` (`FastAPI(...)` — no `debug=` argument passed at all).
- **CONFIRMED, with a mitigating finding:** `settings.debug` does **not** wire into FastAPI's own `debug` flag (which controls whether uncaught exceptions render a full HTML traceback in the HTTP response) — `FastAPI()` is instantiated without `debug=settings.debug`, so FastAPI/Starlette's own default of `debug=False` applies regardless of this setting, and an uncaught exception returns Starlette's generic, non-leaking 500 response. This directly answers (and substantially narrows) the brief's stack-trace-leakage concern: **confirmed not present** via HTTP responses, independent of the `debug` setting.
- **What `debug=True` (the shipped default) *does* do:** every SQL statement SQLAlchemy executes, including bound parameter values, is echoed to stdout/console (`echo=True`) unless an operator explicitly sets `DEBUG=false` in `.env`. No startup check enforces this either. This is a log-hygiene/data-exposure-via-logs concern (query parameters can include customer IDs, bag numbers, wallet IDs, usernames) rather than an HTTP-response leak.
- **Remediation:** Same startup-validation pattern as S-02/S-11: refuse to start (or loudly warn) if presumed-production mode has `debug=True`. Consider also explicitly setting `debug=False` on the `FastAPI(...)` constructor rather than relying on its own default, so the intent is visible in code.
- **Required regression test:** `test_production_mode_forces_debug_false` (once a production-mode flag exists) and `test_sql_echo_disabled_by_default_in_production_config`.

---

### S-15 — LOW — `passlib[bcrypt]` is a listed but unused/dead dependency; hashing goes through `bcrypt` directly

- **File:Line:** `requirements.txt` (`passlib[bcrypt]==1.7.4`); `backend/core/security.py:3,8-9,12-13` (`import bcrypt`, `bcrypt.hashpw(...)`, `bcrypt.checkpw(...)` — no `passlib.CryptContext` anywhere).
- **CONFIRMED** — repo-wide `grep -ri "passlib\|CryptContext"` finds zero usages outside `requirements.txt` and this review's own documentation. Actual hashing parameters: `bcrypt.gensalt()`'s default cost factor (12 rounds as of the installed `bcrypt` version) — an acceptable, currently-standard work factor.
- **Impact:** Not a vulnerability by itself; it's dependency hygiene — an unused package still ships in the production dependency tree, adding unnecessary attack/CVE surface and maintenance burden for zero functional benefit. Also a documentation-vs-reality mismatch worth cleaning up (README.md:346 says "Both hash passwords with bcrypt," which is accurate for the *library actually used*, but the `passlib` dependency implies otherwise).
- **Remediation:** Remove `passlib[bcrypt]` from `requirements.txt` (out of scope to actually change per this Phase 1 mandate — flagged for whichever phase owns dependency changes), or, if a policy-driven `CryptContext` (with configurable rounds / deprecated-scheme handling) is actually wanted, switch `backend/core/security.py` to use it instead of raw `bcrypt` calls.
- **Required regression test:** none needed (hygiene-only, no behavior change implied without also deciding which library wins).

---

### S-16 — LOW — Audit log is append-only in application code but not independently readable, and not tamper-proof at the storage layer

- **File:Line:** `backend/repositories/core_audit_repository.py:8-21` (`CoreAuditRepository` — only `log()` and `list_recent()` exist, no update/delete method); `backend/repositories/transaction_repository.py:175-188` (`AuditRepository`, same — catalog-level `audit_log` table, only `log()`/`list_recent()`); `backend/models/core_audit.py`, `backend/models/transaction.py:93-104` (`AuditLog`).
- **CONFIRMED append-only at the application/repository layer** — no route in `backend/api/routes/*.py` or `web/routes/*.py` ever calls anything other than `.log()` against either audit table (confirmed by `grep -rn "audit" backend/api/routes web/routes`, which returns only the write call in `web/routes/admin_web.py:133-138` for backup creation).
- **Two gaps noted:**
  1. **No read/browse route exists for either audit log anywhere in the app** — despite `list_recent()` existing on both repositories, nothing in `backend/api/routes/*.py` or `web/routes/*.py` ever calls it. The audit trail is captured but currently has no UI or API surface for an administrator to actually review it — an operational/observability gap, separate from the tampering question.
  2. **Append-only is enforced only by "no code path exists to mutate it," not by any database-level constraint** (no read-only DB role, no `INSERT`-only trigger/permission, no hash-chaining). Both audit tables live in the same SQLite files as everything else; anyone with OS/filesystem access to the `.db` file (or a future SQL-injection vector elsewhere, none found in this review — see S-17) could edit or delete rows directly, and the application would have no way to detect it.
- **Impact:** Low as a direct exploit path (no code-level route to abuse), but worth flagging for defense-in-depth given this is meant to be the audit trail for a cash-handling system.
- **Remediation:** (a) Add an admin-only "view audit log" page/route surfacing `list_recent()`. (b) Consider a lightweight tamper-evidence mechanism (e.g., a running hash chain over audit rows) if the audit trail needs to survive a scenario where the application layer itself is compromised.
- **Required regression test:** `test_audit_log_has_no_update_or_delete_route` (regression guard — asserts this property holds going forward) — this is a "prove the absence of a capability" test, cheap to write and worth keeping.

---

### S-17 — LOW — SQL injection: not found (confirmed absence, noted for completeness)

- **CONFIRMED (negative finding).** All database access reviewed goes through SQLAlchemy's async ORM/Core query builder (`select(...)`, relationship loading, etc.) — no raw SQL string interpolation or f-string-built queries were found anywhere in `backend/` (`grep` for `execute(text(`, f-string/`.format()`-built `SELECT` statements returns zero hits). The one user-supplied value that reaches a query filter as a raw string (`X-Catalog` header / `catalog` session value) is validated against a closed `CatalogCode` enum before ever being used (`backend/core/catalogs.py:5-9`, `backend/api/deps.py:44-52`), not concatenated into SQL. No further action needed; recorded so this axis of the brief is explicitly closed out.

---

## Dependency Posture (versions read from `requirements.txt`, no changes made)

Per the Phase 1 mandate, **no dependency version was changed**; this is an inventory only, with explicit uncertainty where I am not fully confident of a specific CVE.

| Package | Pinned | Note |
|---|---|---|
| `python-jose[cryptography]` | 3.3.0 | Handles JWT sign/verify. The algorithm-confusion class of `python-jose` issues (accepting an attacker-chosen `alg`) is **mitigated here** because `decode_access_token` (`backend/core/security.py:26`) explicitly passes `algorithms=[settings.algorithm]` (always `"HS256"`, `backend/core/config.py:20`) rather than trusting the token's own header — so a classic alg-confusion/`alg: none` bypass is not directly exploitable through this code path as written. `python-jose` is a lightly-maintained package generally; I am **not fully certain** whether 3.3.0 (released ~2021) carries any currently-unpatched CVE and would flag this specifically for a Phase 5 automated dependency scan (`pip-audit`/`safety`) rather than assert a number here. |
| `passlib[bcrypt]` | 1.7.4 | Unused (S-15) — irrelevant to runtime security either way. |
| `fastapi` | 0.115.0 | Reasonably current at time of writing; verify against a CVE database in Phase 5. |
| `sqlalchemy` | 2.0.35 | Current major version; no known-to-me outstanding CVE. |
| `jinja2` | 3.1.6 | Current; Jinja2's own autoescape defaults protect template-rendered user data from XSS as long as `{% autoescape %}` is not disabled anywhere — **ASSUMED safe, not exhaustively verified**: a template-by-template audit of every `{{ ... | safe }}`/`{% autoescape false %}` usage was not performed in this pass (a quick `grep -rn "| safe\|autoescape false" web/templates` returned no hits, which is a good sign, but a dedicated template-injection/XSS pass is recommended as a named Phase 5 follow-up rather than declared fully closed here). |
| `itsdangerous` | 2.2.0 | Current; used correctly (signed, not encrypted — session data is visible to the client, not confidential, in line with typical `itsdangerous` session usage; confirm nothing sensitive beyond `user_id`/`catalog` code/draft-transaction-in-progress data is ever placed in `request.session`, which held true for everything read in this review). |
| `apscheduler`, `alembic`, `pandas`, `openpyxl`, `pyserial`, `pyusb` | various | Not primarily security-relevant to this review's scope (auth/authz); recommend standard Phase 5 scan coverage regardless. |

**Recommendation for Phase 5:** run `pip-audit` (or `safety check`) against the exact pinned `requirements.txt` in an environment matching the target Python version, rather than relying on this review's manual/uncertain recollection of CVE status.

---

## Unverified Assumptions (would require a live/dynamic test — explicitly separated from CONFIRMED findings above)

These are called out wherever relevant above, collected here for visibility:

1. **S-03 (CSRF)** — the *practical* exploitability of the SameSite=Lax-only mitigation (e.g., whether any intermediary in the eventual production topology strips `SameSite`, and whether any target browser/version in actual staff use predates SameSite support) is ASSUMED, not verified against a live deployment.
2. **S-04 (brute-force)** — confirmed the *absence* of rate-limiting code; the practical attack timing/throughput a real attacker could achieve against a live instance is ASSUMED, not measured.
3. **Jinja2 template XSS surface** — a full manual/automated audit of every template for unsafe output was not performed line-by-line across all of `web/templates/`; the `grep` sweep for the most common bypass markers came back clean, but this is ASSUMED sufficient, not exhaustive.
4. **CORS in the actual production topology** — `backend/main.py:71-77` hardcodes `http://localhost:8000`/`http://127.0.0.1:8000` as the only allowed origins. Whether the eventual production frontend origin gets added correctly (and nothing broader) at deploy time is an operational/deployment question, ASSUMED to be handled correctly since no code exists yet to check.
5. **Dependency CVEs** — see the dependency table above; flagged with explicit uncertainty rather than asserted.
6. All 12 authorization scenarios in the next section — each is marked CONFIRMED (code-reading sufficient) or ASSUMED (needs a live authorization-matrix test) individually.

---

## Role / Action Permission Matrix

Roles: **Anon** (no credential), **C** = cashier, **S** = supervisor, **A** = administrator. "✔" = allowed by a server-side role/auth dependency (not merely hidden in the UI); "✘" = blocked server-side; "—" = route does not exist on that surface.

| Action | API route | Web route | Anon | C | S | A | Enforcement |
|---|---|---|:-:|:-:|:-:|:-:|---|
| Login | `POST /api/v1/auth/login` | `POST /web/login` | ✔ (by design) | ✔ | ✔ | ✔ | public, credential-checked in `AuthService.login` |
| Logout | — (stateless JWT, client discards) | `GET /web/logout` | ✔ | ✔ | ✔ | ✔ | public; clears session only |
| Select processing catalog | — (per-request `X-Catalog` header, no "select" step) | `GET/POST /web/catalog[/select]` | ✘ | ✔ (any catalog) | ✔ (any catalog) | ✔ (any catalog) | `WebCurrentUser`; **no catalog-level restriction** (S-08) |
| Create transaction | `POST /api/v1/transactions/` | `POST /web/transactions/new/wizard/complete` | ✘ | ✔ | ✔ | ✔ | `CurrentUser`/`WebCurrentUser` — **not cashier-restricted** (any role can enter transactions, relevant to S-09) |
| Read one transaction | `GET /api/v1/transactions/{id}` | `GET /web/transactions/{id}` | ✘ | **API: ✔** / **Web: ✘** | ✔ | ✔ | Inconsistent — see S-12 |
| List/search transactions | `GET /api/v1/transactions/` | `GET /web/transactions` | ✘ | ✘ | ✔ | ✔ | `SupervisorOrAbove`/`WebSupervisorOrAbove` |
| Transfer transaction's business date | `POST /api/v1/transactions/{id}/transfer` | `POST /web/transactions/{id}/transfer` | ✘ | ✘ | ✘ | ✔ | `AdminOnly`/`WebAdminOnly` |
| Bulk-transfer transactions | — (API has no bulk-transfer route) | `POST /web/transactions/bulk-transfer`, `POST /web/transactions/transfer-cashier` | ✘ | ✘ | ✘ | ✔ | `WebAdminOnly` — **web-only capability, no API equivalent** |
| Correct (reissue) a transaction | — (API has no correct route) | `GET/POST /web/transactions/{id}/correct` | ✘ | ✘ | ✔ | ✔ | `WebSupervisorOrAbove` — **web-only**; no self-correction check (S-09) |
| Read customers/locations | `GET /api/v1/customers/...` | — (used internally by wizard, no standalone list page) | ✘ | ✔ | ✔ | ✔ | `CurrentUser` — any role, read-only reference data |
| List EOD closures / day status | `GET /api/v1/eod/`, `GET /api/v1/eod/status` | `GET /web/eod` | ✘ | ✘ (list)/✔ (status) | ✔ | ✔ | `status` is `CurrentUser`; `list`/page is supervisor+ |
| Close a business day | `POST /api/v1/eod/close` | `POST /web/eod/close` | ✘ | ✘ | ✔ | ✔ | `SupervisorOrAbove`/`WebSupervisorOrAbove` |
| Reopen a closed business day | `POST /api/v1/eod/reopen` | `POST /web/eod/reopen` | ✘ | ✘ | ✘ | ✔ | `AdminOnly`/`WebAdminOnly` |
| List/read duplicate flags | `GET /api/v1/duplicates/...` | `GET /web/duplicates` | ✘ | ✘ | ✔ | ✔ | `SupervisorOrAbove`/`WebSupervisorOrAbove` |
| Review/resolve a duplicate flag | `POST /api/v1/duplicates/{id}/review` | `POST /web/duplicates/{id}/review` | ✘ | ✘ | ✔ | ✔ | `SupervisorOrAbove`/`WebSupervisorOrAbove`; no self-review check (S-09) |
| List/read notifications | `GET /api/v1/notifications/...` | `GET /web/notifications` | ✘ | ✘ | ✔ | ✔ | `SupervisorOrAbove`/`WebSupervisorOrAbove` |
| Resolve a notification | `POST /api/v1/notifications/{id}/resolve` | `POST /web/notifications/{id}/resolve` | ✘ | ✘ | ✔ | ✔ | `SupervisorOrAbove`/`WebSupervisorOrAbove` |
| Processor stats | `GET /api/v1/stats/...` | `GET /web/stats` | ✘ | ✘ | ✔ | ✔ | `SupervisorOrAbove`/`WebSupervisorOrAbove` |
| View/download reports (Excel/CSV) | — (API has no report route) | `GET /web/reports`, `GET /web/reports/download` | ✘ | ✘ | ✔ | ✔ | `WebSupervisorOrAbove` — **web-only**, see S-07 for export-content risk |
| List catalogs | `GET /api/v1/catalogs/` | — | ✘ | ✔ | ✔ | ✔ | `CurrentUser` — any role |
| Create / list users | `POST,GET /api/v1/auth/users` | `GET,POST /web/admin/users` | ✘ | ✘ | ✘ | ✔ | `AdminOnly`/`WebAdminOnly` |
| Activate/deactivate a user | `PATCH /api/v1/auth/users/{id}/active` | `POST /web/admin/users/{id}/active` | ✘ | ✘ | ✘ | ✔ | `AdminOnly`/`WebAdminOnly`; self-deactivation explicitly blocked in `AuthService.set_active` |
| Edit a user (username/role/password) | `PATCH /api/v1/auth/users/{id}` | `POST /web/admin/users/{id}/edit` | ✘ | ✘ | ✘ | ✔ | `AdminOnly`/`WebAdminOnly`; self-demotion-away-from-admin explicitly blocked in `AuthService.update_user` |
| Delete a user | `DELETE /api/v1/auth/users/{id}` | `POST /web/admin/users/{id}/delete` | ✘ | ✘ | ✘ | ✔ | `AdminOnly`/`WebAdminOnly`; self-deletion explicitly blocked |
| Create a database backup | — (API has no backup route) | `GET,POST /web/admin/backup` | ✘ | ✘ | ✘ | ✔ | `WebAdminOnly` — **web-only** |
| API docs / OpenAPI schema | `GET /docs`, `/redoc`, `/openapi.json` | — | **✔ (public, S-13)** | ✔ | ✔ | ✔ | no auth dependency at all |
| Health check | `GET /health` | — | ✔ (by design, no sensitive data) | ✔ | ✔ | ✔ | intentionally public |

Notable patterns visible in the matrix (each already detailed above): (1) several admin/supervisor actions exist **only** on the web portal with no JSON-API equivalent (bulk-transfer, correction, reports, backup) — not a vulnerability by itself, but means any future API-only client cannot perform these actions, and any security control added to one surface must be remembered for the other if the capability is ever mirrored; (2) the one true API/web inconsistency found is the single-transaction read (S-12); (3) every role check that exists is a genuine server-side dependency (`Depends(require_role(...))` / `Depends(require_web_role(...))`), never a client-side/UI-only hide — confirmed by reading every route file's dependency signature, not just its template.

---

## Required Authorization Scenarios — Walkthrough

| # | Scenario | What the code shows | Verified how |
|---|---|---|---|
| 1 | Anonymous access to every route | Every `/api/v1/*` route requires a Bearer credential (`HTTPBearer()` dependency chain, `backend/api/deps.py:12,15-18`); every `/web/*` route (except `/web/login` POST/GET and `/web/logout`) requires a valid session (`get_web_current_user`, redirects to `/web/login` otherwise). Exceptions confirmed public **by design**: `GET /health`, `GET /docs`/`/redoc`/`/openapi.json` (S-13, arguably should be gated), and static assets under `/web/static`. | **CONFIRMED** by reading every route file's dependency signature (see matrix). |
| 2 | Normal (cashier) user calling admin APIs | Blocked server-side: `require_role(UserRole.administrator)` / `require_web_role(UserRole.administrator)` raise 403 for any non-administrator caller, for every admin-only route in the matrix above. | **CONFIRMED** by code reading; a live 403-response test would additionally confirm no route was missed by a naming/refactor mistake — recommend as a Phase 5 **live** authorization-matrix test: `test_full_role_matrix_against_every_route` (parametrized over all routes × all roles × anon). |
| 3 | User changing an object ID (IDOR) | Transaction/duplicate/notification/EOD-closure IDs are UUIDv4 primary keys, not sequential — not practically enumerable. No explicit ownership check exists on `GET /api/v1/transactions/{id}` (S-12) — a cashier who *obtains* another user's UUID can read it via the API (not the web). All other by-ID routes require supervisor+ already, narrowing exposure to a smaller trust tier. | **CONFIRMED** for the code path (no ownership check exists); **ASSUMED** for real-world guessability/leakage likelihood of a UUIDv4 — Phase 5 test: `test_cashier_cannot_read_arbitrary_transaction_id_via_api`. |
| 4 | User accessing another catalog's/customer's records | Confirmed **by design**, not blocked: any authenticated user of any role can switch to any of the 4 catalogs (S-08). `X-Catalog` header validated only against the closed enum, not against the caller's identity. | **CONFIRMED.** |
| 5 | User retaining privileges after role downgrade | `get_current_user`/`get_web_current_user` re-fetch the `User` row from the database on **every** request (`backend/api/deps.py:29-32`; `web/deps.py:29-33`) and the role check (`require_role`/`require_web_role`) evaluates the freshly-fetched `current_user.role` — a role downgrade takes effect on the very next request, for both JWT and session-cookie auth. The JWT payload itself carries no role claim (only `sub`/`exp`, `backend/core/security.py:20`), so there is nothing stale to exploit. | **CONFIRMED** — this scenario is **not** a gap; role changes are enforced live. |
| 6 | Disabled user retaining an active session | Same mechanism as #5: both `get_current_user` and `get_web_current_user` explicitly check `user.is_active` on every request and reject (401 for API, redirect-to-login + session-clear for web) the instant an account is deactivated. | **CONFIRMED** — **not** a gap for *newly issued* requests. Caveat: the underlying JWT/cookie itself is not technically "invalidated" (no denylist), it simply fails the live `is_active` check on next use — functionally equivalent from the attacker's perspective (no further requests succeed), covered fully by S-06's discussion of revocation semantics. |
| 7 | User editing a completed/BALANCED transaction | There is no "edit in place" capability anywhere in the code for any transaction, balanced or not — `Transaction` rows are never mutated via the correction flow; `correct_transaction` always inserts a **new** row and flags the original `is_superseded` (`backend/services/transaction_service.py:197-257`). The only two other mutators are `transfer_business_date` (admin-only, changes `business_date` only) and the (missing) self-correction check (S-09). | **CONFIRMED** — direct in-place editing of a balanced/completed transaction is not possible through any route; the closest analog (correction) is append-only by construction, gated to supervisor+, but lacks a self-correction check (S-09). |
| 8 | User approving their own restricted action | Confirmed gap: **CONFIRMED**, see S-09 — no check anywhere prevents a supervisor/administrator from reviewing their own duplicate flag or correcting their own transaction. | **CONFIRMED.** |
| 9 | Unauthorized report export | `GET /web/reports/download` requires `WebSupervisorOrAbove` — a cashier cannot reach it server-side (confirmed dependency, not just a hidden nav link). No API equivalent exists at all (matrix). | **CONFIRMED** blocked for cashiers. |
| 10 | Unauthorized correction/reversal | `POST /web/transactions/{id}/correct` requires `WebSupervisorOrAbove`; no API route exists for this action at all, so it cannot be reached by any lower-privileged or API-only path. Self-correction gap tracked separately as S-09. | **CONFIRMED** blocked for cashiers/anonymous. |
| 11 | Direct API access bypassing the UI | The JSON API (`/api/v1/*`) is a fully independent, equally-authenticated surface (Bearer JWT), not merely "hidden" behind the web UI — every role check present on the web portal has an equivalent (or, per S-12, a slightly *looser*) check on the API. There is no route reachable only by bypassing client-side JS/UI restrictions; every restriction found in this review is a server-side dependency. | **CONFIRMED** — the one asymmetry found (S-12, single-transaction read looser on the API than on the web) is documented as its own finding rather than restated here. |
| 12 | (Additional, brief-adjacent) Brute-force / credential stuffing at either login surface | No rate limiting/lockout exists on either surface (S-04). | **CONFIRMED absent** in code; **ASSUMED** for real-world attacker throughput — Phase 5 live test: `test_login_rate_limited_under_repeated_failure_load`. |

---

## Phase 5 Automated Tooling Recommendations

**Already available (confirmed):**
- `pytest` + `pytest-asyncio` suite: 127 passed / 1 skipped per `00_baseline.md` — good regression foundation to extend with the specific test names proposed throughout this document (all are new tests, none exist yet for these findings — confirmed by `grep`-ing `tests/*.py` for `csrf`, `rate_limit`, `secret_key`, `catalog.*assign`, `self.*review`, `wallet_id.*formula` — zero hits for any of them).
- In-process ASGI test client (`httpx.AsyncClient` + `ASGITransport`, `tests/conftest.py:60-81`) — fast, but explicitly **not** representative of real HTTP/TLS/network-timing behavior (already flagged by the baseline for load testing; equally relevant here for anything timing-sensitive like brute-force throttling).

**Not yet available, needed for Phase 5:**
1. **Dependency vulnerability scan** — `pip-audit` or `safety check` against the exact pinned `requirements.txt`, in an environment matching the real target Python version (baseline notes the sandbox runs 3.11.15 vs. the README's stated 3.12+ requirement — worth resolving before this scan to avoid false negatives from a mismatched resolver).
2. **Static security analysis** — `bandit` over `backend/`, `web/`, `reports/`, `database/` (not yet run in this review; would likely surface the raw `bcrypt` usage, the hardcoded default secret, and the `SessionMiddleware` config as flaggable patterns, corroborating this manual review).
3. **Secret scan of git history** — `gitleaks` or `trufflehog` against the full commit history (not just the working tree), since this review only confirmed `.env` is absent from the *current* tree and gitignored — it did not exhaustively diff every historical commit for an accidentally-committed-then-removed secret.
4. **Live authorization-matrix test** — a dedicated, parametrized test (`test_full_role_matrix_against_every_route`, proposed above) that spins up a real server process (not the in-process ASGI transport) and, for every route in the matrix above, asserts the exact expected status code for anonymous / cashier / supervisor / administrator callers — this is the only way to fully close out Scenario #2/#11 with certainty rather than manual code-reading, and would also catch any future route added without its intended guard.
5. **Dynamic scan** — explicitly **out of scope for Phase 1** per the task brief (no live deployment exists yet); flagged here only as a Phase 5 candidate once a staging deployment exists (e.g. OWASP ZAP baseline scan against a real HTTP(S) endpoint, to validate S-05's header findings and S-03's CSRF findings against an actual browser-facing target rather than code inspection).
6. **CSV-injection-specific regression test**, already named under S-07, is cheap to add now (no live deployment needed) and should not wait for Phase 5 tooling — it is pure unit-test territory using the existing `reports/report_engine.py` functions directly.

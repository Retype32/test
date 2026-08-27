"""Independent, read-only data-integrity checker for Brink's Nexus
(Phase 3 implementation of
docs/production_readiness/04_postgresql_and_reconciliation.md §4, Agent 4's
design).

Run via `python -m tools.integrity_check --help`. Never imports/boots the
FastAPI app -- it opens its own read-only connections against whatever
`--catalog-database-url`/`--core-database-url` (or `DATABASE_URL_*` env
vars) it's pointed at, so it can run against a live DB, a restored backup,
or a Phase 5 PostgreSQL instance equally.
"""

TOOL_VERSION = "0.1.0"

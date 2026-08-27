"""Load-test harness for Brink's Nexus (Phase 3 implementation of
docs/production_readiness/02_capacity_test_plan.md, Agent 2's design).

Run against a REAL spawned uvicorn process reached over a real socket
(http://127.0.0.1:<port>) -- never in-process ASGI (that's
tests/conftest.py's tool, for functional tests, not this one).

See `python -m tools.loadtest --help` for the CLI entrypoint.
"""

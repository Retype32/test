"""Static configuration shared by every journey/profile: seeded accounts,
catalog codes, and the customer/location pairs known to exist in every
catalog DB (database/seed.py CUSTOMERS list) so journeys never have to
guess an ID that might not exist in the catalog they were pointed at.
"""
from __future__ import annotations

CATALOGS = ("vms", "dayshift", "complete", "esnf")

# database/seed.py:USERS -- present in every fresh install (or the
# app's own auto-seed-on-empty-db path, backend/main.py:_auto_seed).
SEEDED_USERS = {
    "admin": {"password": "admin", "role": "administrator"},
    "supervisor1": {"password": "super", "role": "supervisor"},
    "cashier1": {"password": "cash1", "role": "cashier"},
    "cashier2": {"password": "cash2", "role": "cashier"},
}

# Prefix used for synthetic accounts the harness seeds on top of the 2
# default cashiers when a run needs more VUs than seeded accounts exist
# (plan section 11: "prefer seeding one account per intended VU").
SYNTHETIC_CASHIER_PREFIX = "loadtest_cashier_"
SYNTHETIC_SUPERVISOR_PREFIX = "loadtest_supervisor_"
SYNTHETIC_PASSWORD = "LoadTest#Pass1"

# database/seed.py:CUSTOMERS -- (customer_id, [location_id, ...]).
# Every catalog DB carries the same customer/location seed data.
CUSTOMER_LOCATIONS: list[tuple[str, list[str]]] = [
    ("C001", ["L001", "L002", "L003", "L004"]),
    ("C002", ["L010", "L011", "L012"]),
    ("C003", ["L020", "L021"]),
    ("C004", ["L030", "L031", "L032"]),
    ("12345", ["12345-L1", "12345-L2", "12345-L3", "12345-L4", "12345-L5"]),
    ("12346", ["12346-L1", "12346-L2", "12346-L3", "12346-L4", "12346-L5"]),
    ("12343", ["12343-L1", "12343-L2", "12343-L3", "12343-L4", "12343-L5"]),
    ("12341", ["12341-L1", "12341-L2", "12341-L3", "12341-L4", "12341-L5"]),
    ("12342", ["12342-L1", "12342-L2", "12342-L3", "12342-L4", "12342-L5"]),
]

DEFAULT_CUSTOMER_ID = "C001"
DEFAULT_LOCATION_ID = "L001"

# Denomination values, matching backend/services/transaction_service.py's
# DENOMINATION_VALUES and the wizard's DENOM_FIELDS
# (web/routes/transaction_entry_web.py).
DENOM_FIELD_TO_LABEL = [
    ("count_500", "€500", 500),
    ("count_200", "€200", 200),
    ("count_100", "€100", 100),
    ("count_50", "€50", 50),
    ("count_20", "€20", 20),
    ("count_10", "€10", 10),
    ("count_5", "€5", 5),
    ("count_coins", "Coins", 1),
]

DEFAULT_THINK_TIME_MIN = 0.5
DEFAULT_THINK_TIME_MAX = 2.0

# Environment sandbox note required on every report (plan section 5 /
# section 9 environment.hardware_note). Overridable via --hardware-note.
DEFAULT_HARDWARE_NOTE = (
    "Sandbox container, not production-equivalent hardware. Numbers here "
    "are NOT a production capacity guarantee (plan section 5)."
)

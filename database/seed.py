"""
Run with:  python -m database.seed
Populates the core database with sample users, and every catalog database
with sample customers/locations (plus a handful of demo transactions in the
VMS catalog only, to keep dev setup light).
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from backend.core.database import CoreSessionLocal, get_catalog_sessionmaker, init_databases
from backend.core.catalogs import CatalogCode
from backend.core.security import hash_password
from backend.models.user import User, UserRole
from backend.models.customer import Customer, Location
from backend.models.transaction import Transaction, Denomination, BalanceStatus
from decimal import Decimal
from datetime import datetime, timezone, timedelta


CUSTOMERS = [
    ("C001", "Tesco Ireland Ltd", [
        ("L001", "Tesco Rathmines"),
        ("L002", "Tesco Dundrum"),
        ("L003", "Tesco Tallaght"),
        ("L004", "Tesco Blanchardstown"),
    ]),
    ("C002", "AIB Group plc", [
        ("L010", "AIB Grafton Street"),
        ("L011", "AIB Baggot Street"),
        ("L012", "AIB Cork City"),
    ]),
    ("C003", "Dunnes Stores", [
        ("L020", "Dunnes Cornelscourt"),
        ("L021", "Dunnes Henry Street"),
    ]),
    ("C004", "SuperValu Group", [
        ("L030", "SuperValu Blackrock"),
        ("L031", "SuperValu Swords"),
        ("L032", "SuperValu Naas"),
    ]),
    ("12345", "Aldi Ireland", [
        ("12345-L1", "Aldi Tallaght"),
        ("12345-L2", "Aldi Blanchardstown"),
        ("12345-L3", "Aldi Finglas"),
        ("12345-L4", "Aldi Clondalkin"),
        ("12345-L5", "Aldi Swords"),
    ]),
    ("12346", "Tesco Ireland", [
        ("12346-L1", "Tesco Grafton Street"),
        ("12346-L2", "Tesco Dundrum"),
        ("12346-L3", "Tesco Rathmines"),
        ("12346-L4", "Tesco Stillorgan"),
        ("12346-L5", "Tesco Dun Laoghaire"),
    ]),
    ("12343", "Bank of Ireland", [
        ("12343-L1", "BOI College Green"),
        ("12343-L2", "BOI Baggot Street"),
        ("12343-L3", "BOI O'Connell Street"),
        ("12343-L4", "BOI Ranelagh"),
        ("12343-L5", "BOI Blackrock"),
    ]),
    ("12341", "AIB", [
        ("12341-L1", "AIB Dame Street"),
        ("12341-L2", "AIB Pearse Street"),
        ("12341-L3", "AIB Drumcondra"),
        ("12341-L4", "AIB Donnybrook"),
        ("12341-L5", "AIB Ballsbridge"),
    ]),
    ("12342", "Danske Bank", [
        ("12342-L1", "Danske St Stephen's Green"),
        ("12342-L2", "Danske Camden Street"),
        ("12342-L3", "Danske Sandyford"),
        ("12342-L4", "Danske Malahide"),
        ("12342-L5", "Danske Clontarf"),
    ]),
]

USERS = [
    ("admin", "admin", UserRole.administrator),
    ("supervisor1", "super", UserRole.supervisor),
    ("cashier1", "cash1", UserRole.cashier),
    ("cashier2", "cash2", UserRole.cashier),
]

DENOM_VALUES_MAP = {
    "€500": Decimal("500"), "€200": Decimal("200"), "€100": Decimal("100"),
    "€50": Decimal("50"), "€20": Decimal("20"), "€10": Decimal("10"),
    "€5": Decimal("5"), "Coins": Decimal("1"),
}

SAMPLE_TXNS = [
    ("C001", "L001", "BAG-2026-001", [("€50", 10), ("€20", 5), ("€10", 10)]),
    ("C001", "L002", "BAG-2026-002", [("€100", 5), ("€50", 4), ("€20", 10)]),
    ("C002", "L010", "BAG-2026-003", [("€500", 1), ("€200", 2), ("€100", 3)]),
    ("C003", "L020", "BAG-2026-004", [("€20", 20), ("€10", 15), ("€5", 10)]),
    ("C001", "L003", "BAG-2026-005", [("€50", 8), ("€20", 12), ("Coins", 50)]),
]

# Catalog that gets demo transactions seeded, to keep dev setup light.
DEMO_TRANSACTIONS_CATALOG = CatalogCode.vms


async def seed_core_users() -> dict[str, User]:
    async with CoreSessionLocal() as session:
        result = await session.execute(select(User.username))
        existing_usernames = {r[0] for r in result.fetchall()}

        users_created = {}
        for username, password, role in USERS:
            if username not in existing_usernames:
                user = User(
                    username=username,
                    password_hash=hash_password(password),
                    role=role,
                )
                session.add(user)
                await session.flush()
                users_created[username] = user
                print(f"  + User: {username} ({role.value})")
            else:
                res = await session.execute(select(User).where(User.username == username))
                user = res.scalar_one()
                user.password_hash = hash_password(password)
                await session.flush()
                users_created[username] = user
                print(f"  ~ Updated password: {username}")

        await session.commit()
        return users_created


async def seed_catalog(code: CatalogCode, cashier_username: str | None, cashier_user_id):
    sessionmaker = get_catalog_sessionmaker(code)
    async with sessionmaker() as session:
        print(f"\n[{code.value}]")

        for cid, cname, locations in CUSTOMERS:
            res = await session.execute(select(Customer).where(Customer.customer_id == cid))
            customer = res.scalar_one_or_none()
            if not customer:
                customer = Customer(customer_id=cid, customer_name=cname)
                session.add(customer)
                await session.flush()
                print(f"  + Customer: {cid} — {cname}")
            else:
                print(f"  ~ Customer exists: {cid}")

            for lid, lname in locations:
                res = await session.execute(select(Location).where(Location.location_id == lid))
                loc = res.scalar_one_or_none()
                if not loc:
                    location = Location(location_id=lid, customer_id=cid, location_name=lname)
                    session.add(location)
                    print(f"      + Location: {lid} — {lname}")

        await session.flush()

        if code == DEMO_TRANSACTIONS_CATALOG and cashier_username:
            res = await session.execute(
                select(Transaction).where(Transaction.bag_number.like("BAG-2026-%"))
            )
            existing_bags = {t.bag_number for t in res.scalars().all()}

            for idx, (cid, lid, bag, denoms) in enumerate(SAMPLE_TXNS):
                if bag in existing_bags:
                    print(f"  ~ Transaction exists: {bag}")
                    continue

                total = sum(DENOM_VALUES_MAP[d] * count for d, count in denoms)
                txn = Transaction(
                    user_id=cashier_user_id,
                    username=cashier_username,
                    customer_id=cid,
                    location_id=lid,
                    bag_number=bag,
                    total_value=total,
                    balance_status=BalanceStatus.balanced,
                    created_at=datetime.now(timezone.utc) - timedelta(days=idx),
                )
                session.add(txn)
                await session.flush()

                for denom, count in denoms:
                    value = DENOM_VALUES_MAP[denom] * count
                    d_row = Denomination(
                        transaction_id=txn.transaction_id,
                        denomination=denom,
                        count=count,
                        value=value,
                    )
                    session.add(d_row)

                print(f"  + Transaction: {bag}  (€{total:,.2f})")

        await session.commit()


async def seed():
    await init_databases()

    print("Seeding core users...")
    users_created = await seed_core_users()
    cashier_user = users_created.get("cashier1")

    for code in CatalogCode:
        await seed_catalog(
            code,
            cashier_username=cashier_user.username if cashier_user else None,
            cashier_user_id=cashier_user.id if cashier_user else None,
        )

    print("\nSeed complete.")


if __name__ == "__main__":
    asyncio.run(seed())

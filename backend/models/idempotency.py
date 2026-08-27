import uuid
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import String, DateTime, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from ..core.database import CatalogBase


class IdempotencyKey(CatalogBase):
    """Backstops a retried/double-submitted write (create_transaction,
    correct_transaction) from creating a second, fully-accepted financial
    row -- see docs/production_readiness/01_transaction_integrity.md §6.1/§7
    and 06_consolidated_plan.md §7 for the design this implements.

    `key` is the full scope-qualified string ("{scope}:{client-supplied
    key}"), so it doubles as the primary key while still scoping a client's
    nonce to the one endpoint it was minted for -- a nonce issued for
    wizard_complete can never collide with one issued for
    correct_transaction_submit even if a client (accidentally or otherwise)
    reused the same raw value for both. `scope`/`catalog` are kept as their
    own columns too (matching the schema literally proposed in §6.1) purely
    for introspection/debugging -- the actual uniqueness guarantee comes
    from `key` being the primary key.

    Lives per-catalog DB (never core), alongside the `Transaction` rows it
    protects, so the check-and-insert can share one request-scoped session
    and one commit with the business write it guards (see
    TransactionRepository.create_with_idempotency).
    """

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    catalog: Mapped[str] = mapped_column(String(20), nullable=False)
    scope: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    request_fingerprint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    transaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    # Bounded expiry (24-48h is the range 01_transaction_integrity.md §7
    # recommends) so the table doesn't grow unboundedly; nothing in this
    # phase purges expired rows yet (no scheduler job wired up -- that's
    # eod_scheduler.py/main.py territory, outside this control area's file
    # ownership), but the column is here so that job has something to filter
    # on once it's added.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

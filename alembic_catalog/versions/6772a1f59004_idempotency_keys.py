"""idempotency keys

Revision ID: 6772a1f59004
Revises: f7d0b2e5a913
Create Date: 2026-08-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6772a1f59004'
down_revision: Union[str, Sequence[str], None] = 'f7d0b2e5a913'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Backstops a retried/double-submitted create_transaction or
    # correct_transaction from creating a second, fully-accepted financial
    # row -- see docs/production_readiness/01_transaction_integrity.md §6.1.
    # `key` is the scope-qualified string ("{scope}:{client-supplied key}"),
    # so it is both the primary key and already partitioned per endpoint.
    # Lives per-catalog DB (never core), alongside the Transaction rows it
    # protects.
    op.create_table(
        'idempotency_keys',
        sa.Column('key', sa.String(length=300), nullable=False),
        sa.Column('catalog', sa.String(length=20), nullable=False),
        sa.Column('scope', sa.String(length=100), nullable=False),
        sa.Column('request_fingerprint', sa.String(length=64), nullable=True),
        sa.Column('transaction_id', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )
    op.create_index(op.f('ix_idempotency_keys_scope'), 'idempotency_keys', ['scope'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_idempotency_keys_scope'), table_name='idempotency_keys')
    op.drop_table('idempotency_keys')

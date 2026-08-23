"""unique partial index on original_transaction_id

Revision ID: 049a0efa449b
Revises: 546dfd6ecc88
Create Date: 2026-08-23 00:02:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '049a0efa449b'
down_revision: Union[str, Sequence[str], None] = '546dfd6ecc88'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # H-3: at most one live correction per original row, enforced at the DB
    # level regardless of any application-level TOCTOU race in
    # correct_transaction. Partial (WHERE ... IS NOT NULL) so the many rows
    # that are *not* corrections (NULL) never collide with each other.
    # Replaces the plain, non-unique index the column has carried since
    # e3a9c6d1f480_transaction_correction.py.
    op.drop_index(op.f('ix_transactions_original_transaction_id'), table_name='transactions')
    op.create_index(
        'ix_transactions_original_transaction_id_unique',
        'transactions',
        ['original_transaction_id'],
        unique=True,
        sqlite_where=sa.text('original_transaction_id IS NOT NULL'),
        postgresql_where=sa.text('original_transaction_id IS NOT NULL'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_transactions_original_transaction_id_unique', table_name='transactions')
    op.create_index(
        op.f('ix_transactions_original_transaction_id'), 'transactions', ['original_transaction_id'], unique=False
    )

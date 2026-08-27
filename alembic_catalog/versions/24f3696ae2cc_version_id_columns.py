"""version_id_col optimistic locking columns

Revision ID: 24f3696ae2cc
Revises: 049a0efa449b
Create Date: 2026-08-23 00:03:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '24f3696ae2cc'
down_revision: Union[str, Sequence[str], None] = '049a0efa449b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Optimistic locking (consolidated plan §8 / Agent 1 §8): a version
    # column, not SELECT ... FOR UPDATE -- see backend/models/transaction.py
    # and backend/models/eod.py for the full rationale. server_default so
    # SQLite can add this NOT NULL column to tables that already have rows;
    # every pre-existing row starts at version 1, same as a fresh insert
    # would via the model's Python-side default.
    op.add_column(
        'transactions', sa.Column('version_id', sa.Integer(), nullable=False, server_default='1')
    )
    op.add_column(
        'eod_closures', sa.Column('version_id', sa.Integer(), nullable=False, server_default='1')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('eod_closures', 'version_id')
    op.drop_column('transactions', 'version_id')

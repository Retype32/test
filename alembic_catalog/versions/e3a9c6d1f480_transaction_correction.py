"""transaction correction workflow

Revision ID: e3a9c6d1f480
Revises: a1c7e4f9d2b6
Create Date: 2026-08-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e3a9c6d1f480'
down_revision: Union[str, Sequence[str], None] = 'a1c7e4f9d2b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('transactions', sa.Column('original_transaction_id', sa.Uuid(), nullable=True))
    # server_default so SQLite can add this NOT NULL column to a table that
    # already has rows; existing transactions simply weren't corrections.
    op.add_column(
        'transactions',
        sa.Column('is_superseded', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column('transactions', sa.Column('correction_reason', sa.Text(), nullable=True))
    with op.batch_alter_table('transactions') as batch_op:
        batch_op.create_index(
            op.f('ix_transactions_original_transaction_id'), ['original_transaction_id'], unique=False
        )
        batch_op.create_foreign_key(
            'fk_transactions_original_transaction_id', 'transactions',
            ['original_transaction_id'], ['transaction_id'],
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('transactions') as batch_op:
        batch_op.drop_constraint('fk_transactions_original_transaction_id', type_='foreignkey')
        batch_op.drop_index(op.f('ix_transactions_original_transaction_id'))
    op.drop_column('transactions', 'correction_reason')
    op.drop_column('transactions', 'is_superseded')
    op.drop_column('transactions', 'original_transaction_id')

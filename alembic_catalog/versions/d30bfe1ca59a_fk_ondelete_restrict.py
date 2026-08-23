"""FK ondelete=RESTRICT

Revision ID: d30bfe1ca59a
Revises: 2bde077d70c8
Create Date: 2026-08-23 00:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd30bfe1ca59a'
down_revision: Union[str, Sequence[str], None] = '2bde077d70c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None



# denominations.transaction_id's FK was created unnamed by the initial
# migration (plain sa.ForeignKeyConstraint(...), no name= given) --
# SQLite's reflection returns name=None for it, which Alembic's batch
# `drop_constraint` can't target directly ("Constraint must have a name").
# Passing a naming_convention to batch_alter_table makes Alembic derive a
# predictable name for that anonymous constraint during reflection, which
# can then be dropped by that derived name -- the documented workaround for
# exactly this situation. transactions.original_transaction_id's FK is
# already explicitly named (e3a9c6d1f480_transaction_correction.py), so it
# needs no such trick.
_FK_NAMING_CONVENTION = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}


def upgrade() -> None:
    """Upgrade schema."""
    # PG-9: this is an append-only, audit-oriented schema where rows are
    # never expected to be hard-deleted in normal operation -- a raw DELETE
    # should fail loudly (RESTRICT) rather than cascade or silently no-op.
    with op.batch_alter_table(
        'denominations', recreate='always', naming_convention=_FK_NAMING_CONVENTION
    ) as batch_op:
        batch_op.drop_constraint('fk_denominations_transaction_id_transactions', type_='foreignkey')
        batch_op.create_foreign_key(
            'fk_denominations_transaction_id_transactions',
            'transactions', ['transaction_id'], ['transaction_id'], ondelete='RESTRICT',
        )
    with op.batch_alter_table('transactions', recreate='always') as batch_op:
        batch_op.drop_constraint('fk_transactions_original_transaction_id', type_='foreignkey')
        batch_op.create_foreign_key(
            'fk_transactions_original_transaction_id',
            'transactions', ['original_transaction_id'], ['transaction_id'], ondelete='RESTRICT',
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('transactions', recreate='always') as batch_op:
        batch_op.drop_constraint('fk_transactions_original_transaction_id', type_='foreignkey')
        batch_op.create_foreign_key(
            'fk_transactions_original_transaction_id',
            'transactions', ['original_transaction_id'], ['transaction_id'],
        )
    with op.batch_alter_table('denominations', recreate='always') as batch_op:
        batch_op.drop_constraint('fk_denominations_transaction_id_transactions', type_='foreignkey')
        batch_op.create_foreign_key(
            'fk_denominations_transaction_id_transactions',
            'transactions', ['transaction_id'], ['transaction_id'],
        )

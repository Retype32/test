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


# denominations.transaction_id's FK, and transactions.original_transaction_id's
# FK before e3a9c6d1f480 named it, were created unnamed (plain
# sa.ForeignKeyConstraint(...), no name= given). Each dialect then assigns
# its own default name for an anonymous constraint -- SQLite's reflection
# returns name=None (which batch mode's naming_convention= parameter can
# paper over, since SQLite implements "ALTER" via a table-copy that only
# needs *a* usable name, not the constraint's original one), but PostgreSQL
# auto-assigns a real name at CREATE TABLE time (typically
# "<table>_<column>_fkey") that the naming_convention trick does not
# retroactively rename. Confirmed by running this migration against a real
# PostgreSQL 16 instance during Phase 5 validation: batch_alter_table's
# drop_constraint('fk_denominations_transaction_id_transactions', ...)
# raised "No such constraint" because no constraint by that literal name
# existed -- the real one had Postgres's own auto-generated name instead.
#
# Fix: dialect-branch. SQLite keeps the original, already-proven
# batch_alter_table approach (SQLite has no ALTER TABLE DROP CONSTRAINT at
# all, batch/table-recreate is the only option). PostgreSQL reflects the
# FK's actual current name via SQLAlchemy's inspector at migration-run time
# instead of assuming one, then uses a plain ALTER TABLE (no table
# recreation needed or wanted on a dialect that supports DROP/ADD
# CONSTRAINT natively).
def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def _reflected_fk_name(table_name: str, column_name: str) -> str:
    inspector = sa.inspect(op.get_bind())
    for fk in inspector.get_foreign_keys(table_name):
        if fk.get("constrained_columns") == [column_name]:
            name = fk.get("name")
            if name:
                return name
    raise RuntimeError(
        f"could not reflect the FK on {table_name}.{column_name} -- "
        "expected exactly one to exist from an earlier migration"
    )


def upgrade() -> None:
    """Upgrade schema."""
    # PG-9: this is an append-only, audit-oriented schema where rows are
    # never expected to be hard-deleted in normal operation -- a raw DELETE
    # should fail loudly (RESTRICT) rather than cascade or silently no-op.
    if _is_sqlite():
        _FK_NAMING_CONVENTION = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}
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
    else:
        denom_fk = _reflected_fk_name('denominations', 'transaction_id')
        op.drop_constraint(denom_fk, 'denominations', type_='foreignkey')
        op.create_foreign_key(
            'fk_denominations_transaction_id_transactions',
            'denominations', 'transactions', ['transaction_id'], ['transaction_id'], ondelete='RESTRICT',
        )
        txn_fk = _reflected_fk_name('transactions', 'original_transaction_id')
        op.drop_constraint(txn_fk, 'transactions', type_='foreignkey')
        op.create_foreign_key(
            'fk_transactions_original_transaction_id',
            'transactions', 'transactions', ['original_transaction_id'], ['transaction_id'], ondelete='RESTRICT',
        )


def downgrade() -> None:
    """Downgrade schema."""
    if _is_sqlite():
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
    else:
        op.drop_constraint('fk_transactions_original_transaction_id', 'transactions', type_='foreignkey')
        op.create_foreign_key(
            'fk_transactions_original_transaction_id',
            'transactions', 'transactions', ['original_transaction_id'], ['transaction_id'],
        )
        op.drop_constraint('fk_denominations_transaction_id_transactions', 'denominations', type_='foreignkey')
        op.create_foreign_key(
            'fk_denominations_transaction_id_transactions',
            'denominations', 'transactions', ['transaction_id'], ['transaction_id'],
        )

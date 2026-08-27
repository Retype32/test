"""correction_reason check constraint

Revision ID: 546dfd6ecc88
Revises: 6772a1f59004
Create Date: 2026-08-23 00:01:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '546dfd6ecc88'
down_revision: Union[str, Sequence[str], None] = '6772a1f59004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # PG-5: a correction row (original_transaction_id set) without a
    # non-empty correction_reason silently defeats the append-only
    # workflow's audit intent for any write path that bypasses
    # TransactionService.correct_transaction. SQLite and PostgreSQL both
    # support table-level CHECK constraints identically here -- no dialect
    # branching needed. Batch mode is required on SQLite to add a
    # constraint to an existing table.
    with op.batch_alter_table('transactions') as batch_op:
        batch_op.create_check_constraint(
            'ck_transactions_correction_reason_required',
            "original_transaction_id IS NULL OR "
            "(correction_reason IS NOT NULL AND correction_reason <> '')",
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('transactions') as batch_op:
        batch_op.drop_constraint('ck_transactions_correction_reason_required', type_='check')

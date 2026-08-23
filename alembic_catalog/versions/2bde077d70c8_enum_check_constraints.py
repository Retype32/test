"""enum create_constraint check constraints

Revision ID: 2bde077d70c8
Revises: 24f3696ae2cc
Create Date: 2026-08-23 00:04:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2bde077d70c8'
down_revision: Union[str, Sequence[str], None] = '24f3696ae2cc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # PG-2: SQLAlchemy's SQLite Enum compiler only emits a CHECK constraint
    # when create_constraint=True is explicit on the SAEnum(...) column --
    # without it SQLite silently accepts any string for balance_status/
    # eod status, while PostgreSQL already enforces the same invariant
    # natively via CREATE TYPE ... AS ENUM. Names match exactly what
    # SAEnum(..., create_constraint=True) generates unprompted (the enum
    # type's own `name=`) so a future `alembic revision --autogenerate`
    # sees no drift against the model.
    with op.batch_alter_table('transactions') as batch_op:
        batch_op.create_check_constraint(
            'balancestatus', "balance_status IN ('balanced', 'not_balanced', 'pending')"
        )
    with op.batch_alter_table('eod_closures') as batch_op:
        batch_op.create_check_constraint('eodstatus', "status IN ('closed', 'reopened')")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('eod_closures') as batch_op:
        batch_op.drop_constraint('eodstatus', type_='check')
    with op.batch_alter_table('transactions') as batch_op:
        batch_op.drop_constraint('balancestatus', type_='check')

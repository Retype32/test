"""eod automatic close

Revision ID: f7d0b2e5a913
Revises: e3a9c6d1f480
Create Date: 2026-08-21 00:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f7d0b2e5a913'
down_revision: Union[str, Sequence[str], None] = 'e3a9c6d1f480'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # server_default so SQLite can add this NOT NULL column to a table that
    # already has rows; existing closures were all manual.
    op.add_column(
        'eod_closures',
        sa.Column('closed_automatically', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # Nullable so the nightly scheduler job can close a day with no acting
    # user. SQLite requires batch mode to relax a NOT NULL constraint.
    with op.batch_alter_table('eod_closures') as batch_op:
        batch_op.alter_column('closed_by_user_id', nullable=True)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('eod_closures') as batch_op:
        batch_op.alter_column('closed_by_user_id', nullable=False)
    op.drop_column('eod_closures', 'closed_automatically')

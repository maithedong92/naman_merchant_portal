"""add_stock_reservation_status_to_unified_orders

Revision ID: a1b2c3d4e5f6
Revises: 6b7398920f3d
Create Date: 2026-11-15 09:12:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '6b7398920f3d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add stock_reservation_status column to unified_orders."""
    op.add_column(
        'unified_orders',
        sa.Column(
            'stock_reservation_status',
            sa.String(length=30),
            nullable=False,
            server_default='NONE',
        ),
    )


def downgrade() -> None:
    """Remove stock_reservation_status column from unified_orders."""
    op.drop_column('unified_orders', 'stock_reservation_status')

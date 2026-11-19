"""add_product_conversion_rate

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-11-19 08:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add conversion_rate and conversion_rate_source columns to products."""
    op.add_column(
        'products',
        sa.Column(
            'conversion_rate',
            sa.Numeric(10, 4),
            nullable=False,
            server_default='1.0',
        ),
    )
    op.add_column(
        'products',
        sa.Column(
            'conversion_rate_source',
            sa.String(32),
            nullable=False,
            server_default='DEFAULT',
        ),
    )


def downgrade() -> None:
    """Remove conversion_rate and conversion_rate_source columns from products."""
    op.drop_column('products', 'conversion_rate_source')
    op.drop_column('products', 'conversion_rate')

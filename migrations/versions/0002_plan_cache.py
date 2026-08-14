"""plan cache

Revision ID: 6bba3181ca89
Revises: 0001_initial
Create Date: 2026-08-14 01:26:07.787966
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from neuroloop.persistence.models import JsonType

revision: str = '0002_plan_cache'
down_revision: str | None = '0001_initial'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('plan_cache',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('goal_fingerprint', sa.String(length=80), nullable=False),
    sa.Column('objective', sa.Text(), nullable=False),
    sa.Column('completion_condition', sa.Text(), nullable=False),
    sa.Column('steps', JsonType, nullable=False),
    sa.Column('assumptions', JsonType, nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('successes', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('successes <= attempts', name='ck_plan_cache_counters'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('goal_fingerprint')
    )


def downgrade() -> None:
    op.drop_table('plan_cache')
